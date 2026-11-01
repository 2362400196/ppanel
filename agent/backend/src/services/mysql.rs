//! 节点级共享 MySQL 服务（移植 app/services/mysql_service.py）：
//! 每版本一个 MySQL 容器，多实例共享进程、各持独立库。
//!
//! 启用 = 跑 mysql:{ver} 容器（镜像必须已本地拉取，绝不自动 pull）；
//! 实例容器 join ppanel-net 后用容器名直连（内网），外网直连走映射的宿主端口
//! （所有库共用一个端口，按账号鉴权）。实例回收时联动删库（drop_instance_db）。

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};

use base64::Engine;
use bollard::container::{
    Config, CreateContainerOptions, LogsOptions, RemoveContainerOptions, StopContainerOptions,
};
use bollard::exec::{CreateExecOptions, StartExecOptions, StartExecResults};
use bollard::models::{HostConfig, PortBinding, RestartPolicy, RestartPolicyNameEnum};
use bollard::volume::CreateVolumeOptions;
use bollard::Docker;
use futures_util::StreamExt;
use serde_json::{json, Value};
use sqlx::SqlitePool;

use crate::config::settings;
use crate::error::{AppError, AppResult};
use crate::models::{utcnow, InstanceDb, MySqlService};
use crate::state::AppState;

/// MySQL 服务固定 768M（swap 同值）
const MEM_LIMIT: i64 = 768 * 1024 * 1024;

// ---------- 基础工具 ----------

/// 模块级 Docker 句柄：需要 pool-only 签名的函数（如 drop_instance_db）也能连 Docker。
static DOCKER: OnceLock<Arc<crate::docker::DockerHandle>> = OnceLock::new();

pub fn docker_handle() -> Arc<crate::docker::DockerHandle> {
    DOCKER
        .get_or_init(|| crate::docker::DockerHandle::new())
        .clone()
}

/// secrets.token_urlsafe(n) 等价实现。
pub fn token_urlsafe(n: usize) -> String {
    use rand::Rng;
    let mut rng = rand::thread_rng();
    let bytes: Vec<u8> = (0..n).map(|_| rng.gen::<u8>()).collect();
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

pub fn svc_name(ver: &str) -> String {
    format!("{}mysql-{}", crate::services::instance::container_prefix(), ver)
}

pub fn vol_name(ver: &str) -> String {
    format!("ppanel-mysql-{ver}-data")
}

fn ver_sort_key(v: &str) -> (u8, f64, String) {
    match v.parse::<f64>() {
        Ok(f) => (0, f, v.to_string()), // 纯数字版本按数值排
        Err(_) => (1, 0.0, v.to_string()), // latest 等非数字 tag 排在后面
    }
}

// ---------- 内部工具 ----------

/// 取已启用的 MySQL 服务登记（404 与 Python 文案一致）。
pub async fn get_svc_row(pool: &SqlitePool, ver: &str) -> AppResult<MySqlService> {
    sqlx::query_as::<_, MySqlService>("SELECT * FROM mysql_services WHERE version = ?")
        .bind(ver)
        .fetch_optional(pool)
        .await?
        .ok_or_else(|| AppError::not_found(format!("MySQL {ver} 服务未启用")))
}

async fn container_running(docker: &Docker, name: &str) -> Option<bool> {
    match docker.inspect_container(name, None).await {
        Ok(info) => info
            .state
            .as_ref()
            .map(|s| s.running.unwrap_or(false)),
        Err(_) => None,
    }
}

async fn logs_tail(docker: &Docker, name: &str, tail: i64) -> String {
    let mut stream = docker.logs(
        name,
        Some(LogsOptions::<String> {
            follow: false,
            stdout: true,
            stderr: true,
            since: 0,
            until: 0,
            timestamps: false,
            tail: tail.clamp(1, 5000).to_string(),
        }),
    );
    let mut out = String::new();
    while let Some(item) = stream.next().await {
        if let Ok(bollard::container::LogOutput::StdOut { message })
        | Ok(bollard::container::LogOutput::StdErr { message })
        | Ok(bollard::container::LogOutput::Console { message })
        | Ok(bollard::container::LogOutput::StdIn { message }) = item
        {
            out.push_str(&String::from_utf8_lossy(&message));
        }
    }
    out
}

/// 在指定容器内执行命令（列表参数不过 shell，无注入面）。返回 (exit_code, 输出)。
async fn exec_raw(docker: &Docker, name: &str, cmd: Vec<String>) -> AppResult<(i64, String)> {
    if container_running(docker, name).await.is_none() {
        return Err(AppError::not_found("MySQL 容器不存在，请重新启用服务"));
    }
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
        .map_err(|e| AppError::bad_gateway(format!("容器执行失败：{e}")))?;
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
        .map_err(|e| AppError::bad_gateway(format!("容器执行失败：{e}")))?;
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
    Ok((code, out))
}

/// 在 MySQL 容器内执行 SQL，返回输出文本。
async fn sql_raw(docker: &Docker, name: &str, root_password: &str, sql: &str) -> AppResult<String> {
    let (code, out) = exec_raw(
        docker,
        name,
        vec![
            "mysql".into(),
            "-uroot".into(),
            format!("--password={root_password}"),
            "-e".into(),
            sql.to_string(),
        ],
    )
    .await?;
    if code != 0 {
        // 去掉 mysql 密码的固定 Warning 行，保留真实错误
        let lines: String = out
            .lines()
            .filter(|l| !l.contains("Using a password on the command line"))
            .collect::<Vec<_>>()
            .concat();
        let detail = if lines.trim().is_empty() {
            format!("exit={code}")
        } else {
            lines.trim().to_string()
        };
        return Err(AppError::bad_gateway(format!("MySQL 执行失败：{detail}")));
    }
    Ok(out)
}

fn port_free(port: u16) -> bool {
    std::net::TcpListener::bind(("0.0.0.0", port)).is_ok()
}

/// 外网直连端口：优先 3306，被占用则在实例端口段内找空闲口。
fn pick_port() -> AppResult<u16> {
    if port_free(3306) {
        return Ok(3306);
    }
    let s = settings();
    for p in s.port_start..=s.port_end {
        if port_free(p) {
            return Ok(p);
        }
    }
    Err(AppError::unavailable("无可用端口：3306 与实例端口段均被占用"))
}

fn which(binname: &str) -> Option<String> {
    let path = std::env::var("PATH").unwrap_or_default();
    let sep = if cfg!(windows) { ';' } else { ':' };
    for dir in path.split(sep).filter(|d| !d.is_empty()) {
        let cand = Path::new(dir).join(binname);
        if cand.is_file() {
            return Some(cand.to_string_lossy().to_string());
        }
    }
    for pre in ["/usr/sbin", "/sbin", "/usr/local/sbin"] {
        let cand = format!("{pre}/{binname}");
        if Path::new(&cand).exists() {
            return Some(cand);
        }
    }
    None
}

/// 防火墙放行外网直连端口（ufw / firewalld 自动探测，列表参数防注入）。
/// 未安装防火墙工具或执行失败均跳过——不阻塞启用流程。
fn fw_allow(port: u16) {
    let run = |bin: &str, args: &[String]| {
        let _ = std::process::Command::new(bin).args(args).output();
    };
    if let Some(ufw) = which("ufw") {
        run(&ufw, &["allow".to_string(), format!("{port}/tcp")]);
        return;
    }
    if let Some(fw) = which("firewall-cmd") {
        run(&fw, &["--permanent".to_string(), format!("--add-port={port}/tcp")]);
        run(&fw, &["--reload".to_string()]);
    }
}

/// 循环 mysqladmin ping 直到 mysqld 就绪（首次启动需初始化数据目录）。
async fn wait_ready(
    state: &AppState,
    docker: &Docker,
    ver: &str,
    root_password: &str,
    timeout: i64,
    tid: &str,
) -> AppResult<()> {
    let name = svc_name(ver);
    let deadline = Instant::now() + Duration::from_secs(timeout as u64);
    let mut last = String::new();
    while Instant::now() < deadline {
        match container_running(docker, &name).await {
            None => {
                return Err(AppError::not_found("MySQL 容器不存在，请重新启用服务"));
            }
            Some(false) => {
                let logs = logs_tail(docker, &name, 20).await;
                let tail: String = logs
                    .trim()
                    .chars()
                    .rev()
                    .take(400)
                    .collect::<Vec<_>>()
                    .into_iter()
                    .rev()
                    .collect();
                return Err(AppError::bad_gateway(format!(
                    "MySQL 容器启动失败：{tail}"
                )));
            }
            Some(true) => {}
        }
        if let Ok((code, out)) = exec_raw(
            docker,
            &name,
            vec![
                "mysqladmin".into(),
                "ping".into(),
                "-uroot".into(),
                format!("-p{root_password}"),
                "--silent".into(),
            ],
        )
        .await
        {
            last = out;
            if code == 0 {
                let elapsed = timeout - deadline.saturating_duration_since(Instant::now()).as_secs() as i64;
                state.tasks.log(tid, &format!("mysqld 就绪（等待 {elapsed}s）"));
                return Ok(());
            }
        }
        let left = deadline.saturating_duration_since(Instant::now()).as_secs() as i64;
        if !tid.is_empty() && left % 10 == 0 {
            state
                .tasks
                .log(tid, &format!("等待 mysqld 初始化…（剩余 {left}s 内）"));
        }
        let _ = last;
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
    Err(AppError::new(
        504,
        format!("MySQL 在 {timeout}s 内未就绪，请稍后在服务列表查看状态"),
    ))
}

/// 真实 SQL 校验 root 密码（mysqladmin ping 对 access denied 也算存活，不可靠）。
async fn sql_ok(docker: &Docker, ver: &str, password: &str) -> bool {
    sql_raw(docker, &svc_name(ver), password, "SELECT 1")
        .await
        .is_ok()
}

/// 复用旧数据卷导致 root 密码失配时：停主容器 → skip-grant 临时实例重置 → 复原。
async fn reset_root_password(state: &AppState, ver: &str, new_password: &str, tid: &str) -> AppResult<()> {
    let docker = state.docker.get().await?;
    let main = svc_name(ver);
    let _ = docker
        .stop_container(&main, Some(StopContainerOptions { t: 10 }))
        .await;
    let tmp_name = format!("{main}-resetpw");
    let _ = docker
        .remove_container(
            &tmp_name,
            Some(RemoveContainerOptions {
                force: true,
                ..Default::default()
            }),
        )
        .await;
    state.tasks.log(tid, "启动 skip-grant 临时实例…");

    let tmp_config = Config {
        image: Some(format!("mysql:{ver}")),
        cmd: Some(vec![
            "--skip-grant-tables".into(),
            "--skip-networking".into(),
        ]),
        host_config: Some(HostConfig {
            binds: Some(vec![format!("{}:/var/lib/mysql:rw", vol_name(ver))]),
            network_mode: Some(settings().docker_network.clone()),
            ..Default::default()
        }),
        ..Default::default()
    };
    let opts = CreateContainerOptions {
        name: tmp_name.clone(),
        platform: None,
    };
    if let Err(e) = docker.create_container(Some(opts), tmp_config).await {
        return Err(AppError::bad_gateway(format!("创建临时实例失败：{e}")));
    }
    let start_res = docker
        .start_container(&tmp_name, None::<bollard::container::StartContainerOptions<String>>)
        .await;
    if start_res.is_err() {
        let _ = docker
            .remove_container(
                &tmp_name,
                Some(RemoveContainerOptions {
                    force: true,
                    ..Default::default()
                }),
            )
            .await;
        return Err(AppError::bad_gateway("创建临时实例失败：启动失败".to_string()));
    }

    let mut ready = false;
    for _ in 0..60 {
        tokio::time::sleep(Duration::from_secs(1)).await;
        if let Ok((code, _)) = exec_raw(
            &docker,
            &tmp_name,
            vec![
                "sh".into(),
                "-c".into(),
                "mysql -uroot -N -e 'SELECT 1'".into(),
            ],
        )
        .await
        {
            if code == 0 {
                ready = true;
                break;
            }
        }
    }
    if !ready {
        let _ = docker
            .remove_container(
                &tmp_name,
                Some(RemoveContainerOptions {
                    force: true,
                    ..Default::default()
                }),
            )
            .await;
        return Err(AppError::bad_gateway("root 密码重置超时：临时实例未就绪"));
    }

    // skip-grant 模式下必须先 FLUSH PRIVILEGES 才能执行 ALTER USER
    let script = format!(
        "mysql -uroot -e \"FLUSH PRIVILEGES; ALTER USER 'root'@'%' IDENTIFIED BY '{new_password}'; \
         ALTER USER 'root'@'localhost' IDENTIFIED BY '{new_password}'; FLUSH PRIVILEGES;\""
    );
    let res = exec_raw(&docker, &tmp_name, vec!["sh".into(), "-c".into(), script]).await;
    let del = docker
        .remove_container(
            &tmp_name,
            Some(RemoveContainerOptions {
                force: true,
                ..Default::default()
            }),
        )
        .await;
    let _ = del;
    match res {
        Ok((0, _)) => {}
        Ok((code, out)) => {
            let tail: String = out.chars().rev().take(200).collect::<Vec<_>>().into_iter().rev().collect();
            return Err(AppError::bad_gateway(format!(
                "root 密码重置失败：{tail}（exit={code}）"
            )));
        }
        Err(e) => return Err(e),
    }

    state.tasks.log(tid, "重启 MySQL 服务…");
    docker
        .start_container(&main, None::<bollard::container::StartContainerOptions<String>>)
        .await
        .map_err(|e| AppError::bad_gateway(format!("启动 MySQL 服务失败：{e}")))?;
    wait_ready(state, &docker, ver, new_password, 90, tid).await?;
    if !sql_ok(&docker, ver, new_password).await {
        return Err(AppError::bad_gateway(
            "root 密码重置后仍无法连接，请查看容器日志",
        ));
    }
    Ok(())
}

// ---------- 服务生命周期（/agent/docker/mysql/*） ----------

/// 本地已拉取的 mysql 镜像 tag 集合（如 5.7 / 8.0 / 8.4 / latest）。
async fn pulled_versions(docker: &Docker) -> HashSet<String> {
    let mut out = HashSet::new();
    if let Ok(images) = docker.list_images::<String>(None).await {
        for img in images {
            for tag in img.repo_tags {
                if let Some(v) = tag.strip_prefix("mysql:") {
                    if !v.is_empty() && v != "<none>" {
                        out.insert(v.to_string());
                    }
                }
            }
        }
    }
    out
}

/// 启用 MySQL 服务（阻塞等待 mysqld 就绪，最长 ~90s）。
pub async fn enable_mysql(state: &AppState, ver: &str, tid: &str) -> AppResult<Value> {
    if get_svc_row(&state.db, ver).await.is_ok() {
        return Err(AppError::conflict(format!("MySQL {ver} 已启用")));
    }
    let docker = state.docker.get().await?;
    let image = format!("mysql:{ver}");
    match docker.inspect_image(&image).await {
        Ok(_) => {}
        Err(bollard::errors::Error::DockerResponseServerError { status_code: 404, .. }) => {
            return Err(AppError::not_found(format!(
                "镜像 {image} 未拉取，请先在镜像页拉取"
            )));
        }
        Err(e) => return Err(AppError::bad_gateway(format!("Docker 调用失败：{e}"))),
    }
    state
        .tasks
        .log(tid, &format!("镜像 {image} 就绪，选取外网端口…"));

    let root_password = token_urlsafe(24);
    let host_port = pick_port()?;

    if docker.inspect_volume(&vol_name(ver)).await.is_err() {
        let mut opts = CreateVolumeOptions::<String>::default();
        opts.name = vol_name(ver);
        docker
            .create_volume(opts)
            .await
            .map_err(|e| AppError::bad_gateway(format!("Docker 调用失败：{e}")))?;
    }
    state
        .tasks
        .log(tid, &format!("数据卷 {} 就绪，端口 {host_port}", vol_name(ver)));

    // 同名容器残留（如 DB 记录丢失）：先删旧容器再建（数据在卷里，不丢）
    let _ = docker
        .remove_container(
            &svc_name(ver),
            Some(RemoveContainerOptions {
                force: true,
                ..Default::default()
            }),
        )
        .await;

    let mut port_bindings: HashMap<String, Option<Vec<PortBinding>>> = HashMap::new();
    port_bindings.insert(
        "3306/tcp".to_string(),
        Some(vec![PortBinding {
            host_ip: None,
            host_port: Some(host_port.to_string()),
        }]),
    );
    let config = Config {
        image: Some(image.clone()),
        env: Some(vec![format!("MYSQL_ROOT_PASSWORD={root_password}")]),
        host_config: Some(HostConfig {
            binds: Some(vec![format!("{}:/var/lib/mysql:rw", vol_name(ver))]),
            port_bindings: Some(port_bindings),
            network_mode: Some(settings().docker_network.clone()),
            nano_cpus: Some(1_i64 * 1_000_000_000),
            memory: Some(MEM_LIMIT),
            memory_swap: Some(MEM_LIMIT),
            pids_limit: Some(settings().pids_limit),
            restart_policy: Some(RestartPolicy {
                name: Some(RestartPolicyNameEnum::UNLESS_STOPPED),
                maximum_retry_count: None,
            }),
            privileged: Some(false),
            ..Default::default()
        }),
        ..Default::default()
    };
    let opts = CreateContainerOptions {
        name: svc_name(ver),
        platform: None,
    };
    let created = docker
        .create_container(Some(opts), config)
        .await
        .map_err(|e| AppError::bad_gateway(format!("创建 MySQL 容器失败：{e}")))?;
    docker
        .start_container(
            &svc_name(ver),
            None::<bollard::container::StartContainerOptions<String>>,
        )
        .await
        .map_err(|e| AppError::bad_gateway(format!("创建 MySQL 容器失败：{e}")))?;
    state.tasks.log(
        tid,
        &format!("容器 {} 已创建，等待 mysqld 就绪（首次初始化较慢）…", svc_name(ver)),
    );

    wait_ready(state, &docker, ver, &root_password, 90, tid).await?;

    // 数据卷复用（停用未删卷→重新启用）时 MYSQL_ROOT_PASSWORD 不生效，卷内旧 root 密码
    // 会与登记密码失配——这里校验并自动重置
    if !sql_ok(&docker, ver, &root_password).await {
        state
            .tasks
            .log(tid, "检测到数据卷已有旧数据（root 密码失配），自动重置 root 密码…");
        reset_root_password(state, ver, &root_password, tid).await?;
    }

    fw_allow(host_port);

    sqlx::query(
        "INSERT INTO mysql_services (version, container_id, root_password, host_port, created_at) \
         VALUES (?,?,?,?,?)",
    )
    .bind(ver)
    .bind(created.id)
    .bind(&root_password)
    .bind(host_port as i64)
    .bind(utcnow())
    .execute(&state.db)
    .await?;

    Ok(json!({
        "ok": true,
        "version": ver,
        "host_port": host_port,
        "root_password": root_password,
    }))
}

/// 停用 MySQL 服务（校验该版本下已无实例库）。
pub async fn disable_mysql(state: &AppState, ver: &str, purge: bool, tid: &str) -> AppResult<Value> {
    get_svc_row(&state.db, ver).await?;
    let n: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM instance_dbs WHERE version = ?")
        .bind(ver)
        .fetch_one(&state.db)
        .await?;
    if n > 0 {
        return Err(AppError::bad_request(format!(
            "该版本下还有 {n} 个实例数据库，请先删除后再停用"
        )));
    }
    let docker = state.docker.get().await?;
    state.tasks.log(tid, "删除 MySQL 容器…");
    let name = svc_name(ver);
    if docker.inspect_container(&name, None).await.is_ok() {
        docker
            .remove_container(
                &name,
                Some(RemoveContainerOptions {
                    force: true,
                    ..Default::default()
                }),
            )
            .await
            .map_err(|e| AppError::bad_gateway(format!("删除容器失败：{e}")))?;
    }
    if purge {
        state.tasks.log(tid, "删除数据卷（不可恢复）…");
        if docker.inspect_volume(&vol_name(ver)).await.is_ok() {
            docker
                .remove_volume(&vol_name(ver), None)
                .await
                .map_err(|e| AppError::bad_gateway(format!("删除数据卷失败：{e}")))?;
        }
    }
    sqlx::query("DELETE FROM mysql_services WHERE version = ?")
        .bind(ver)
        .execute(&state.db)
        .await?;
    let suffix = if purge { "，数据卷已删除" } else { "（数据卷保留）" };
    Ok(json!({"ok": true, "detail": format!("MySQL {ver} 已停用{suffix}")}))
}

/// 已启用的 MySQL 版本（面板建库下拉用）。
pub async fn enabled_versions(pool: &SqlitePool) -> AppResult<Vec<String>> {
    Ok(sqlx::query_scalar("SELECT version FROM mysql_services ORDER BY version")
        .fetch_all(pool)
        .await?)
}

/// 版本 = 节点已拉取的 mysql 镜像 tag ∪ 已启用版本（管理员视图）。
pub async fn list_services(state: &AppState) -> AppResult<Value> {
    let docker = state.docker.try_get().await;
    let pulled = match &docker {
        Some(d) => pulled_versions(d).await,
        None => HashSet::new(),
    };
    let enabled: HashSet<String> = sqlx::query_scalar("SELECT version FROM mysql_services")
        .fetch_all(&state.db)
        .await?
        .into_iter()
        .collect();
    let mut versions: Vec<String> = pulled.union(&enabled).cloned().collect();
    versions.sort_by(|a, b| {
        let ka = ver_sort_key(a);
        let kb = ver_sort_key(b);
        ka.0.cmp(&kb.0)
            .then(ka.1.partial_cmp(&kb.1).unwrap_or(std::cmp::Ordering::Equal))
            .then(ka.2.cmp(&kb.2))
    });

    let rows: Vec<(String, i64)> = sqlx::query_as("SELECT version, id FROM instance_dbs")
        .fetch_all(&state.db)
        .await?;
    let mut counts: HashMap<String, i64> = HashMap::new();
    for (v, _) in rows {
        *counts.entry(v).or_insert(0) += 1;
    }

    let mut out: Vec<Value> = Vec::new();
    for ver in versions {
        let svc_row = sqlx::query_as::<_, MySqlService>("SELECT * FROM mysql_services WHERE version = ?")
            .bind(&ver)
            .fetch_optional(&state.db)
            .await?;
        let mut running = false;
        if let (Some(d), Some(_)) = (&docker, &svc_row) {
            running = container_running(d, &svc_name(&ver)).await.unwrap_or(false);
        }
        out.push(json!({
            "version": ver,
            "image": format!("mysql:{ver}"),
            "enabled": svc_row.is_some(),
            "running": running,
            "host_port": svc_row.as_ref().map(|r| r.host_port),
            "db_count": counts.get(&ver).copied().unwrap_or(0),
        }));
    }
    Ok(Value::Array(out))
}

// ---------- 实例数据库（面板 / 实例回收联动） ----------

async fn host_port_of(pool: &SqlitePool, ver: &str) -> Option<i64> {
    sqlx::query_scalar::<_, i64>("SELECT host_port FROM mysql_services WHERE version = ?")
        .bind(ver)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten()
}

async fn conn_info(pool: &SqlitePool, row: &InstanceDb) -> Value {
    json!({
        "version": row.version,
        "db_name": row.db_name,
        "db_user": row.db_user,
        "db_password": row.db_password,
        "internal_host": svc_name(&row.version),
        "internal_port": 3306,
        "host_port": host_port_of(pool, &row.version).await,
    })
}

async fn db_row(pool: &SqlitePool, instance_id: i64) -> AppResult<Option<InstanceDb>> {
    Ok(sqlx::query_as::<_, InstanceDb>("SELECT * FROM instance_dbs WHERE instance_id = ?")
        .bind(instance_id)
        .fetch_optional(pool)
        .await?)
}

fn drop_sql(db_name: &str, db_user: &str) -> String {
    format!("DROP DATABASE IF EXISTS `{db_name}`; DROP USER IF EXISTS '{db_user}'@'%';")
}

/// 一实例一库：db=ppanel_{iid}，user=ppanel_u{iid}，密码随机（无引号注入风险）。
pub async fn create_instance_db(state: &AppState, instance_id: i64, ver: &str) -> AppResult<Value> {
    let svc = get_svc_row(&state.db, ver).await?;
    if db_row(&state.db, instance_id).await?.is_some() {
        return Err(AppError::conflict("该实例已有数据库，如需更换请先删除数据库"));
    }
    let db_name = format!("ppanel_{instance_id}");
    let db_user = format!("ppanel_u{instance_id}");
    let db_password = token_urlsafe(18);
    let docker = state.docker.get().await?;
    let sql = format!(
        "CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; \
         CREATE USER IF NOT EXISTS '{db_user}'@'%' IDENTIFIED BY '{db_password}'; \
         ALTER USER '{db_user}'@'%' IDENTIFIED BY '{db_password}'; \
         GRANT ALL PRIVILEGES ON `{db_name}`.* TO '{db_user}'@'%'; FLUSH PRIVILEGES;"
    );
    sql_raw(&docker, &svc_name(ver), &svc.root_password, &sql).await?;

    let id: i64 = sqlx::query_scalar(
        "INSERT INTO instance_dbs (instance_id, version, db_name, db_user, db_password, created_at) \
         VALUES (?,?,?,?,?,?) RETURNING id",
    )
    .bind(instance_id)
    .bind(ver)
    .bind(&db_name)
    .bind(&db_user)
    .bind(&db_password)
    .bind(utcnow())
    .fetch_one(&state.db)
    .await?;
    let row = sqlx::query_as::<_, InstanceDb>("SELECT * FROM instance_dbs WHERE id = ?")
        .bind(id)
        .fetch_one(&state.db)
        .await?;
    Ok(conn_info(&state.db, &row).await)
}

/// 实例当前数据库发放信息（未开库返回 None）；附带服务运行状态。
pub async fn get_instance_db(state: &AppState, instance_id: i64) -> AppResult<Option<Value>> {
    let Some(row) = db_row(&state.db, instance_id).await? else {
        return Ok(None);
    };
    let mut info = conn_info(&state.db, &row).await;
    let mut running = false;
    if let Ok(docker) = state.docker.get().await {
        running = container_running(&docker, &svc_name(&row.version))
            .await
            .unwrap_or(false);
    }
    info["service_running"] = json!(running);
    Ok(Some(info))
}

/// 重置实例数据库密码（ALTER USER + 同步登记）。
pub async fn reset_db_password(state: &AppState, instance_id: i64) -> AppResult<Value> {
    let Some(row) = db_row(&state.db, instance_id).await? else {
        return Err(AppError::not_found("该实例尚未开通数据库"));
    };
    let svc = get_svc_row(&state.db, &row.version).await?;
    let new_pwd = token_urlsafe(18);
    let docker = state.docker.get().await?;
    sql_raw(
        &docker,
        &svc_name(&row.version),
        &svc.root_password,
        &format!(
            "ALTER USER '{}'@'%' IDENTIFIED BY '{new_pwd}'; FLUSH PRIVILEGES;",
            row.db_user
        ),
    )
    .await?;
    sqlx::query("UPDATE instance_dbs SET db_password = ? WHERE instance_id = ?")
        .bind(&new_pwd)
        .bind(instance_id)
        .execute(&state.db)
        .await?;
    let mut row = row;
    row.db_password = new_pwd;
    Ok(conn_info(&state.db, &row).await)
}

async fn table_count_raw(docker: &Docker, ver: &str, root_password: &str, db_name: &str) -> AppResult<i64> {
    let (code, out) = exec_raw(
        docker,
        &svc_name(ver),
        vec![
            "mysql".into(),
            "-uroot".into(),
            format!("--password={root_password}"),
            "-N".into(),
            "-e".into(),
            format!(
                "SELECT COUNT(*) FROM information_schema.tables \
                 WHERE table_schema='{db_name}' AND table_type='BASE TABLE'"
            ),
        ],
    )
    .await?;
    if code != 0 {
        return Err(AppError::bad_gateway(format!("查询 {ver} 表数量失败")));
    }
    let lines: Vec<String> = out
        .lines()
        .map(|l| l.trim().to_string())
        .filter(|l| !l.is_empty() && !l.contains("Warning"))
        .collect();
    Ok(lines
        .last()
        .and_then(|l| l.parse::<i64>().ok())
        .unwrap_or(0))
}

/// 切换数据库版本（带数据迁移）：导出旧库 → 目标容器建库导入 → 表数校验 → 删旧库。
pub async fn switch_instance_db(state: &AppState, instance_id: i64, target_ver: &str) -> AppResult<Value> {
    let Some(row) = db_row(&state.db, instance_id).await? else {
        return Err(AppError::not_found("该实例尚未开通数据库"));
    };
    if target_ver == row.version {
        return Err(AppError::bad_request("已在该版本上，请选择其他版本"));
    }
    let src = get_svc_row(&state.db, &row.version).await?;
    let dst = get_svc_row(&state.db, target_ver).await?;
    let docker = state.docker.get().await?;
    let src_c = svc_name(&row.version);
    let dst_c = svc_name(target_ver);
    if container_running(&docker, &src_c).await != Some(true) {
        return Err(AppError::bad_gateway("当前版本的 MySQL 服务未运行，无法导出"));
    }
    if container_running(&docker, &dst_c).await != Some(true) {
        return Err(AppError::bad_gateway(format!("MySQL {target_ver} 服务未运行")));
    }
    let new_pwd = token_urlsafe(18);

    // 1) 旧容器内导出到临时文件（mysqldump，exit_code 可校验）
    let dump = format!(
        "mysqldump -uroot --password='{}' --single-transaction --routines --triggers {} > /tmp/ppanel_switch_dump.sql",
        src.root_password, row.db_name
    );
    let (code, out) = exec_raw(&docker, &src_c, vec!["sh".into(), "-c".into(), dump]).await?;
    if code != 0 {
        let lines: String = out
            .lines()
            .filter(|l| !l.to_lowercase().contains("command line interface"))
            .collect::<Vec<_>>()
            .join("\n");
        let tail: String = lines
            .trim()
            .chars()
            .rev()
            .take(300)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect();
        return Err(AppError::bad_gateway(format!("导出旧库失败：{tail}")));
    }

    // 2) 目标容器建库 + 建同名账号（沿用 db_name/db_user，应用只改 host）
    let create_sql = format!(
        "CREATE DATABASE IF NOT EXISTS `{}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; \
         CREATE USER IF NOT EXISTS '{}'@'%' IDENTIFIED BY '{new_pwd}'; \
         GRANT ALL PRIVILEGES ON `{}`.* TO '{}'@'%'; FLUSH PRIVILEGES;",
        row.db_name, row.db_user, row.db_name, row.db_user
    );
    if let Err(e) = sql_raw(&docker, &dst_c, &dst.root_password, &create_sql).await {
        let _ = exec_raw(
            &docker,
            &src_c,
            vec![
                "sh".into(),
                "-c".into(),
                "rm -f /tmp/ppanel_switch_dump.sql".into(),
            ],
        )
        .await;
        return Err(e);
    }

    // 3) 旧容器经内网把数据泵入目标容器（两容器同在网络，容器名可解析）
    let import = format!(
        "mysql -h{} -uroot --password='{}' {} < /tmp/ppanel_switch_dump.sql",
        dst_c, dst.root_password, row.db_name
    );
    let (code, out) = exec_raw(&docker, &src_c, vec!["sh".into(), "-c".into(), import]).await?;
    if code != 0 {
        // 清理半成品目标库，保留原库
        let _ = sql_raw(&docker, &dst_c, &dst.root_password, &drop_sql(&row.db_name, &row.db_user)).await;
        let _ = exec_raw(
            &docker,
            &src_c,
            vec![
                "sh".into(),
                "-c".into(),
                "rm -f /tmp/ppanel_switch_dump.sql".into(),
            ],
        )
        .await;
        let lines: String = out
            .lines()
            .filter(|l| !l.to_lowercase().contains("command line interface"))
            .collect::<Vec<_>>()
            .join("\n");
        let tail: String = lines
            .trim()
            .chars()
            .rev()
            .take(300)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect();
        return Err(AppError::bad_gateway(format!(
            "导入 MySQL {target_ver} 失败（原库未动）：{tail}"
        )));
    }

    // 4) 表数一致性校验（空库也算通过）
    let old_n = table_count_raw(&docker, &row.version, &src.root_password, &row.db_name).await?;
    let new_n = table_count_raw(&docker, target_ver, &dst.root_password, &row.db_name).await?;
    let _ = exec_raw(
        &docker,
        &src_c,
        vec![
            "sh".into(),
            "-c".into(),
            "rm -f /tmp/ppanel_switch_dump.sql".into(),
        ],
    )
    .await;
    if new_n != old_n {
        let _ = sql_raw(&docker, &dst_c, &dst.root_password, &drop_sql(&row.db_name, &row.db_user)).await;
        return Err(AppError::bad_gateway(format!(
            "迁移校验失败：旧库 {old_n} 张表 / 新库 {new_n} 张表，已保留原库，请重试"
        )));
    }

    // 5) 删旧库旧账号（失败不阻塞切换，残留由停用服务时兜底）
    let _ = sql_raw(&docker, &src_c, &src.root_password, &drop_sql(&row.db_name, &row.db_user)).await;

    // 6) 更新发放记录（新密码）
    sqlx::query("UPDATE instance_dbs SET version = ?, db_password = ? WHERE instance_id = ?")
        .bind(target_ver)
        .bind(&new_pwd)
        .bind(instance_id)
        .execute(&state.db)
        .await?;
    let mut row = row;
    row.version = target_ver.to_string();
    row.db_password = new_pwd;
    Ok(conn_info(&state.db, &row).await)
}

/// 删除实例的数据库（实例删除/商城回收联动）。尽力而为：MySQL 不可用不阻塞实例回收。
pub async fn drop_instance_db(pool: &SqlitePool, instance_id: i64) -> AppResult<()> {
    let Some(row) = db_row(pool, instance_id).await? else {
        return Ok(());
    };
    // 安全网：删库前先整库导出一份到备份目录。MySQL 服务可用但导出失败时中止
    // （库与账号保持原样，可重试）；服务不可用时无从导出，按原逻辑继续。
    pre_drop_snapshot(pool, &row).await?;
    if let Ok(docker) = docker_handle().get().await {
        if let Ok(svc) = get_svc_row(pool, &row.version).await {
            let _ = sql_raw(
                &docker,
                &svc_name(&row.version),
                &svc.root_password,
                &drop_sql(&row.db_name, &row.db_user),
            )
            .await;
        }
    }
    sqlx::query("DELETE FROM instance_dbs WHERE instance_id = ?")
        .bind(instance_id)
        .execute(pool)
        .await?;
    Ok(())
}

/// 删库前快照：mysqldump --single-transaction 整库导出到备份目录（保留最近 10 份）。
/// MySQL 容器不可用时直接放行（无从导出）；导出失败返回 Err，调用方应中止删库。
async fn pre_drop_snapshot(pool: &SqlitePool, row: &InstanceDb) -> AppResult<()> {
    let Ok(docker) = docker_handle().get().await else {
        return Ok(());
    };
    let Ok(svc) = get_svc_row(pool, &row.version).await else {
        return Ok(());
    };
    let cname = svc_name(&row.version);
    let Ok(Some(c)) = crate::services::backup::inspect_opt(&docker, &cname).await else {
        return Ok(());
    };
    if !crate::services::backup::is_running(&c) {
        return Ok(());
    }
    let pw = &svc.root_password;
    let dbname = &row.db_name;
    let stamp = format!("{}_{}", crate::services::backup::ts(), token_urlsafe(4));
    let tmp_sql = format!("/tmp/ppanel_predrop_{stamp}.sql");
    let tmp_gz = format!("{tmp_sql}.gz");
    let (code, out) = exec_raw(
        &docker,
        &cname,
        vec![
            "sh".into(),
            "-c".into(),
            format!(
                "mysqldump -uroot --password='{pw}' --single-transaction --routines --triggers \
                 {dbname} --result-file={tmp_sql} && gzip -f {tmp_sql}"
            ),
        ],
    )
    .await?;
    if code != 0 {
        let msg = out.lines().filter(|l| !l.contains("Using a password")).collect::<Vec<_>>().join(" ");
        return Err(AppError::bad_gateway(format!(
            "删库前自动备份失败（{}），为保护数据已中止删除，可重试或先手动备份",
            msg.chars().take(200).collect::<String>()
        )));
    }
    let root = crate::services::backup::backup_root();
    let _ = std::fs::create_dir_all(&root);
    let fname = format!("{dbname}_predrop_{stamp}.sql.gz");
    let dest = std::path::Path::new(&root).join(&fname);
    let pulled = crate::services::backup::pull_archive_docker(&docker, &cname, &tmp_gz, &dest, None).await;
    let _ = exec_raw(
        &docker,
        &cname,
        vec!["sh".into(), "-c".into(), format!("rm -f {tmp_sql} {tmp_gz}")],
    )
    .await;
    pulled?;
    crate::services::backup::cleanup_snapshots("predrop", ".sql.gz", crate::services::backup::SNAPSHOT_KEEP);
    println!("[agent] 删库前快照完成：{fname}");
    Ok(())
}

// ---------- SQL / 容器执行（面板扩展用） ----------

/// 在指定版本的 MySQL 容器内执行 SQL（返回输出文本）。
pub async fn exec_sql(state: &AppState, ver: &str, root_password: &str, sql: &str) -> AppResult<String> {
    let docker = state.docker.get().await?;
    sql_raw(&docker, &svc_name(ver), root_password, sql).await
}

/// 在指定版本的 MySQL 容器内执行任意命令（列表参数不过 shell）。
pub async fn exec_cmd(state: &AppState, ver: &str, cmd: Vec<String>) -> AppResult<(i64, String)> {
    let docker = state.docker.get().await?;
    exec_raw(&docker, &svc_name(ver), cmd).await
}

/// 查询某库的 BASE TABLE 数量。
pub async fn table_count(
    state: &AppState,
    ver: &str,
    root_password: &str,
    db_name: &str,
) -> AppResult<i64> {
    let docker = state.docker.get().await?;
    table_count_raw(&docker, ver, root_password, db_name).await
}
