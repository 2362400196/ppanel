//! phpMyAdmin 一键开启/关闭（实例级，移植 app/services/pma_service.py）。
//!
//! enable：生成仅能访问本实例库的临时账号 → 起 phpmyadmin 容器（自动登录 env，
//! label 标记 2 小时过期，供面板 cron 兜底回收）→ 返回访问 URL。
//! disable：删容器 + 删临时账号。

use std::collections::{HashMap, HashSet};
use std::net::SocketAddr;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use bollard::container::{
    Config, CreateContainerOptions, ListContainersOptions, RemoveContainerOptions,
};
use bollard::exec::{CreateExecOptions, StartExecOptions, StartExecResults};
use bollard::models::{HostConfig, PortBinding, RestartPolicy, RestartPolicyNameEnum};
use bollard::Docker;
use futures_util::StreamExt;
use serde_json::{json, Value};
use sqlx::SqlitePool;

use crate::error::{AppError, AppResult};
use crate::models::{Instance, InstanceDb, MySqlService};
use crate::services::mysql::{svc_name, token_urlsafe};
use crate::state::AppState;

pub const PMA_IMAGE: &str = "phpmyadmin:5.2-apache";
/// 临时容器存活秒数（label 标记，面板 cron 兜底回收）。
pub const PMA_TTL: i64 = 2 * 3600;
/// 空闲端口探测段起点 36100-36999。
pub const PMA_PORT_BASE: i64 = 36100;

pub fn container_name(instance_id: i64) -> String {
    format!("ppanel-pma-{instance_id}")
}

// ---------- 内部工具 ----------

async fn container_running(docker: &Docker, name: &str) -> Option<bool> {
    match docker.inspect_container(name, None).await {
        Ok(info) => info.state.as_ref().and_then(|s| s.running),
        Err(_) => None,
    }
}

/// 容器内执行命令，返回 (exit_code, 输出)。
async fn exec_raw(docker: &Docker, name: &str, cmd: Vec<String>) -> Option<(i64, String)> {
    let exec = docker
        .create_exec(
            name,
            CreateExecOptions {
                cmd: Some(cmd),
                attach_stdout: Some(true),
                attach_stderr: Some(true),
                ..Default::default()
            },
        )
        .await
        .ok()?;
    let exec_id = exec.id;
    let start = docker
        .start_exec(
            &exec_id,
            Some(StartExecOptions {
                detach: false,
                tty: false,
                output_capacity: None,
            }),
        )
        .await
        .ok()?;
    let mut out = String::new();
    if let StartExecResults::Attached { mut output, .. } = start {
        while let Some(item) = output.next().await {
            if let Ok(bollard::container::LogOutput::StdOut { message })
            | Ok(bollard::container::LogOutput::StdErr { message })
            | Ok(bollard::container::LogOutput::Console { message })
            | Ok(bollard::container::LogOutput::StdIn { message }) = item
            {
                out.push_str(&String::from_utf8_lossy(&message));
            }
        }
    }
    let code = docker
        .inspect_exec(&exec_id)
        .await
        .ok()
        .and_then(|i| i.exit_code)
        .unwrap_or(-1);
    Some((code, out))
}

async fn pma_image_ready(docker: &Docker) -> bool {
    docker.inspect_image(PMA_IMAGE).await.is_ok()
}

fn loop_busy_sync(port: i64) -> bool {
    let addr = format!("127.0.0.1:{port}");
    let Ok(a) = addr.parse::<SocketAddr>() else {
        return false;
    };
    match std::net::TcpStream::connect_timeout(&a, Duration::from_millis(300)) {
        Ok(_) => true,
        Err(_) => false,
    }
}

/// 已占用的宿主端口（读所有容器的 PortBindings→HostPort）。
async fn used_ports(docker: &Docker) -> HashSet<i64> {
    let mut used = HashSet::new();
    let opts = ListContainersOptions::<String> {
        all: true,
        ..Default::default()
    };
    let Ok(list) = docker.list_containers(Some(opts)).await else {
        return used;
    };
    for c in list {
        if let Some(ports) = c.ports {
            for p in ports {
                if let Some(hp) = p.public_port {
                    used.insert(hp as i64);
                }
            }
        }
    }
    used
}

/// 从 prefer 起最多探测 64 个端口（与 Python `_free_port` 一致）。
async fn free_port(docker: &Docker, prefer: i64) -> AppResult<i64> {
    let used = used_ports(docker).await;
    let mut port = prefer;
    for _ in 0..64 {
        if !used.contains(&port) && !loop_busy_sync(port) {
            return Ok(port);
        }
        if port < PMA_PORT_BASE + 899 {
            port += 1;
        }
    }
    Err(AppError::bad_gateway("无法找到空闲端口（36100-36999 已满）"))
}

/// 取实例数据库登记 + 对应 MySQL 服务登记（校验服务与容器在跑）。
async fn db_of(state: &AppState, instance_id: i64) -> AppResult<(InstanceDb, MySqlService, Docker)> {
    let reg = sqlx::query_as::<_, InstanceDb>("SELECT * FROM instance_dbs WHERE instance_id = ?")
        .bind(instance_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::bad_request("实例尚未开通数据库，请先在「数据库」页开通"))?;
    let svc = sqlx::query_as::<_, MySqlService>("SELECT * FROM mysql_services WHERE version = ?")
        .bind(&reg.version)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::not_found(format!("MySQL {} 服务未启用", reg.version)))?;
    let docker = state.docker.get().await?;
    match container_running(&docker, &svc_name(&svc.version)).await {
        None => return Err(AppError::not_found("MySQL 容器不存在")),
        Some(false) => return Err(AppError::bad_gateway("MySQL 服务未运行")),
        Some(true) => {}
    }
    Ok((reg, svc, docker))
}

/// 删临时账号：遍历所有在跑的 MySQL 服务，逐个尝试（失败跳过）。
async fn drop_user(docker: &Docker, pool: &SqlitePool, user: &str) {
    if user.is_empty()
        || user.chars().count() > 64
        || !user.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
    {
        return;
    }
    let rows = sqlx::query_as::<_, MySqlService>("SELECT * FROM mysql_services")
        .fetch_all(pool)
        .await
        .unwrap_or_default();
    for svc in rows {
        let name = svc_name(&svc.version);
        if container_running(docker, &name).await != Some(true) {
            continue;
        }
        let sql = format!(
            "mysql -uroot --password='{}' -e \"DROP USER IF EXISTS '{user}'@'%'\"",
            svc.root_password
        );
        let _ = exec_raw(docker, &name, vec!["sh".into(), "-c".into(), sql]).await;
    }
}

/// 清理旧 pma 容器与账号（重复开启/关闭时）。
async fn remove_old(docker: &Docker, pool: &SqlitePool, instance_id: i64) -> AppResult<()> {
    let name = container_name(instance_id);
    let info = match docker.inspect_container(&name, None).await {
        Ok(i) => i,
        Err(e) if matches!(e, bollard::errors::Error::DockerResponseServerError { status_code: 404, .. }) => {
            return Ok(());
        }
        Err(e) => {
            return Err(AppError::bad_gateway(format!(
                "清理旧 phpMyAdmin 容器失败：{e}"
            )))
        }
    };
    let user = info
        .config
        .as_ref()
        .and_then(|c| c.labels.as_ref())
        .and_then(|l| l.get("ppanel.pma-user").cloned())
        .unwrap_or_default();
    if let Err(e) = docker
        .remove_container(
            &name,
            Some(RemoveContainerOptions {
                force: true,
                ..Default::default()
            }),
        )
        .await
    {
        return Err(AppError::bad_gateway(format!(
            "清理旧 phpMyAdmin 容器失败：{e}"
        )));
    }
    if !user.is_empty() {
        drop_user(docker, pool, &user).await;
    }
    Ok(())
}

// ---------- 公共 API ----------

/// phpMyAdmin 镜像是否已本地可用（未拉取则前端提示管理员预拉取）。
pub async fn available(state: &AppState) -> bool {
    match state.docker.get().await {
        Ok(docker) => pma_image_ready(&docker).await,
        Err(_) => false,
    }
}

/// 临时容器状态：active / 端口。
pub async fn status(state: &AppState, instance_id: i64) -> Value {
    let Ok(docker) = state.docker.get().await else {
        return json!({"active": false, "port": Value::Null});
    };
    let name = container_name(instance_id);
    let Ok(info) = docker.inspect_container(&name, None).await else {
        return json!({"active": false, "port": Value::Null});
    };
    let running = info
        .state
        .as_ref()
        .and_then(|s| s.running)
        .unwrap_or(false);
    if !running {
        return json!({"active": false, "port": Value::Null});
    }
    let port = info
        .network_settings
        .and_then(|n| n.ports)
        .and_then(|p| p.get("80/tcp").cloned())
        .flatten()
        .and_then(|b| b.first().and_then(|x| x.host_port.clone()))
        .and_then(|p| p.parse::<i64>().ok());
    json!({"active": true, "port": port})
}

/// 访问地址。
pub fn access_url(host: &str, port: i64) -> String {
    format!("http://{host}:{port}/")
}

/// 当前访问地址（容器在跑时返回 Some）。
pub async fn current_url(state: &AppState, instance_id: i64, host: &str) -> Option<String> {
    let v = status(state, instance_id).await;
    if v["active"].as_bool() != Some(true) {
        return None;
    }
    v["port"].as_i64().map(|p| access_url(host, p))
}

/// 打开临时 phpMyAdmin：清理旧容器/账号 → 建独立账号 → 起容器 → 返回访问地址。
pub async fn enable(state: &AppState, inst: &Instance, host: &str) -> AppResult<Value> {
    let (reg, svc, docker) = db_of(state, inst.id).await?;
    remove_old(&docker, &state.db, inst.id).await?;

    let user = format!("pma{}_{}", inst.id, token_urlsafe(4).replace('-', "").replace('_', ""));
    let user: String = user.chars().take(64).collect();
    let password = token_urlsafe(16);
    // 库名不加反引号（ppanel_1 已过白名单）——反引号在 sh -c 双引号里会被解释成命令替换
    let sql = format!(
        "mysql -uroot --password='{}' -e \"CREATE USER IF NOT EXISTS '{user}'@'%' IDENTIFIED BY '{password}';GRANT ALL PRIVILEGES ON {}.* TO '{user}'@'%';FLUSH PRIVILEGES;\"",
        svc.root_password, reg.db_name
    );
    let Some((code, out)) = exec_raw(&docker, &svc_name(&svc.version), vec!["sh".into(), "-c".into(), sql]).await
    else {
        return Err(AppError::bad_gateway(format!(
            "创建临时账号失败：{}",
            "MySQL 容器不存在"
        )));
    };
    if code != 0 {
        let tail: String = out
            .trim()
            .chars()
            .rev()
            .take(200)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect();
        return Err(AppError::bad_gateway(format!("创建临时账号失败：{tail}")));
    }

    let port = free_port(&docker, PMA_PORT_BASE + inst.id).await?;
    let expire = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64 + PMA_TTL)
        .unwrap_or(PMA_TTL);

    let mut port_bindings: HashMap<String, Option<Vec<PortBinding>>> = HashMap::new();
    port_bindings.insert(
        "80/tcp".to_string(),
        Some(vec![PortBinding {
            host_ip: None,
            host_port: Some(port.to_string()),
        }]),
    );
    let mut labels: HashMap<String, String> = HashMap::new();
    labels.insert("ppanel.managed".to_string(), "true".to_string());
    labels.insert("ppanel.pma-expire".to_string(), expire.to_string());
    labels.insert("ppanel.pma-user".to_string(), user.clone());
    labels.insert("ppanel.instance".to_string(), inst.id.to_string());

    let config = Config {
        image: Some(PMA_IMAGE.to_string()),
        env: Some(vec![
            format!("PMA_HOST={}", svc_name(&svc.version)),
            format!("PMA_USER={user}"),
            format!("PMA_PASSWORD={password}"),
        ]),
        labels: Some(labels),
        host_config: Some(HostConfig {
            port_bindings: Some(port_bindings),
            network_mode: Some(crate::config::settings().docker_network.clone()),
            restart_policy: Some(RestartPolicy {
                name: Some(RestartPolicyNameEnum::UNLESS_STOPPED),
                maximum_retry_count: None,
            }),
            ..Default::default()
        }),
        ..Default::default()
    };
    let opts = CreateContainerOptions {
        name: container_name(inst.id),
        platform: None,
    };
    if !pma_image_ready(&docker).await {
        drop_user(&docker, &state.db, &user).await;
        return Err(AppError::bad_gateway(format!(
            "镜像 {PMA_IMAGE} 未预拉取，请在节点面板拉取后重试"
        )));
    }
    if let Err(e) = docker.create_container(Some(opts), config).await {
        drop_user(&docker, &state.db, &user).await;
        return Err(AppError::bad_gateway(format!(
            "启动 phpMyAdmin 容器失败：{e}"
        )));
    }
    if let Err(e) = docker
        .start_container(
            &container_name(inst.id),
            None::<bollard::container::StartContainerOptions<String>>,
        )
        .await
    {
        let _ = docker
            .remove_container(
                &container_name(inst.id),
                Some(RemoveContainerOptions {
                    force: true,
                    ..Default::default()
                }),
            )
            .await;
        drop_user(&docker, &state.db, &user).await;
        return Err(AppError::bad_gateway(format!(
            "启动 phpMyAdmin 容器失败：{e}"
        )));
    }

    Ok(json!({
        "url": access_url(host, port),
        "port": port,
        "expire_in": PMA_TTL,
        "db": reg.db_name,
        "detail": format!("phpMyAdmin 已开启（{} 小时后自动关闭）", PMA_TTL / 3600),
    }))
}

/// 关闭临时 phpMyAdmin：删容器 + 删临时账号。
pub async fn disable(state: &AppState, instance_id: i64) -> AppResult<Value> {
    let docker = state.docker.get().await?;
    remove_old(&docker, &state.db, instance_id).await?;
    Ok(json!({"detail": "phpMyAdmin 已关闭（临时容器与账号已删除）"}))
}

/// 实例删除时联动（容器 + 账号），失败不阻塞。
pub async fn drop(state: &AppState, instance_id: i64) {
    if let Ok(docker) = state.docker.get().await {
        let _ = remove_old(&docker, &state.db, instance_id).await;
    }
}
