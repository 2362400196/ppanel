//! Caddy 反向代理与自动 HTTPS（移植 app/services/caddy_service.py）。
//!
//! 一个实例绑定一个域名：写 `/etc/caddy/sites/ppanel-<id>.caddy` 反代到
//! `127.0.0.1:<ext_port>`，Caddy 自动申请/续期证书。
//! 支持两种形态：宿主机 systemd 安装的 caddy，或由面板托管的 Caddy 容器
//! （`CADDY_CONTAINER`，默认 `ppanel-caddy`）；容器形态通过 `docker exec ... caddy reload`
//! 重载，也可用 Admin API（`CADDY_ADMIN`，默认 `http://127.0.0.1:2019`）推送 JSON 配置。
//! 未安装 Caddy 时状态接口如实返回，前端引导安装。

use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use regex::Regex;
use serde_json::{json, Value};

use crate::error::{AppError, AppResult};
use crate::services::host_ops;

pub const SITES_DIR: &str = "/etc/caddy/sites";
pub const MAIN_CONF: &str = "/etc/caddy/Caddyfile";

/// 实例站点渲染配置（供 caddy 生成 Caddyfile 用）。
#[derive(Debug, Clone, Default)]
pub struct SiteCfg {
    /// proxy / static
    pub mode: String,
    /// 容器 /app 下的相对目录（static 模式）
    pub static_dir: String,
    /// 容器 /app 的宿主机挂载源（static 模式必需）
    pub app_root: String,
    pub auth_user: String,
    /// bcrypt 哈希
    pub auth_hash: String,
    /// 空格分隔 CIDR
    pub ip_whitelist: String,
    pub ip_blacklist: String,
}

static DOCKER: OnceLock<Arc<crate::docker::DockerHandle>> = OnceLock::new();
static PUB_IP: OnceLock<Mutex<Option<(String, Instant)>>> = OnceLock::new();

fn docker_handle() -> Arc<crate::docker::DockerHandle> {
    DOCKER
        .get_or_init(|| crate::docker::DockerHandle::new())
        .clone()
}

/// Caddy 容器名（环境变量可覆盖，默认 ppanel-caddy）。
pub fn caddy_container() -> String {
    std::env::var("CADDY_CONTAINER")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .unwrap_or_else(|| "ppanel-caddy".to_string())
}

/// Caddy Admin API 基址（默认 http://127.0.0.1:2019）。
pub fn admin_url() -> String {
    std::env::var("CADDY_ADMIN")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .unwrap_or_else(|| "http://127.0.0.1:2019".to_string())
}

pub fn domain_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        // Rust regex 不支持 look-around，长度约束由 domain_ok 判断
        Regex::new(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
            .expect("内置域名正则编译失败")
    })
}

/// 域名合法性：长度 4~253 + 正则。
pub fn domain_ok(s: &str) -> bool {
    (4..=253).contains(&s.len()) && domain_re().is_match(s)
}

/// 宿主机是否安装了 caddy 可执行文件。
pub fn caddy_installed() -> bool {
    host_ops::resolve_bin("caddy") != "caddy"
}

/// 在任意容器内执行命令，返回 (exit_code, 输出文本)；容器不存在返回 None。
async fn docker_exec(container: &str, cmd: Vec<String>) -> Option<(i64, String)> {
    use bollard::exec::{CreateExecOptions, StartExecOptions, StartExecResults};
    use futures_util::StreamExt;

    let docker = docker_handle().get().await.ok()?;
    let exec = docker
        .create_exec(
            container,
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

/// 本机回环探测端口是否被占用（0.5s 超时）。
pub async fn port_in_use(port: u16) -> bool {
    let addr = format!("127.0.0.1:{port}");
    match addr.parse::<SocketAddr>() {
        Ok(a) => tokio::time::timeout(
            Duration::from_millis(500),
            tokio::net::TcpStream::connect(a),
        )
        .await
        .map(|r| r.is_ok())
        .unwrap_or(false),
        Err(_) => false,
    }
}

async fn container_state() -> Option<(bool, String)> {
    let docker = docker_handle().get().await.ok()?;
    let name = caddy_container();
    let info = docker.inspect_container(&name, None).await.ok()?;
    let running = info
        .state
        .as_ref()
        .and_then(|s| s.running)
        .unwrap_or(false);
    let id = info.id.unwrap_or_default();
    Some((running, id))
}

/// Caddy 安装/运行状态与版本；未运行时给出可读原因。
pub async fn caddy_info() -> Value {
    // 优先看托管容器（容器形态），其次看宿主机 systemd
    if let Some((running, _)) = container_state().await {
        let ver = match docker_exec(
            &caddy_container(),
            vec!["caddy".to_string(), "version".to_string()],
        )
        .await
        {
            Some((_, out)) => out
                .split_whitespace()
                .next()
                .unwrap_or("")
                .trim()
                .to_string(),
            None => String::new(),
        };
        let reason = if running {
            String::new()
        } else if port_in_use(80).await || port_in_use(443).await {
            "80/443 端口被宿主机其他 Web 服务占用（如 nginx/宝塔），Caddy 无法绑定。\
             需停用占用方，或由该服务自行配置反代与证书"
                .to_string()
        } else {
            "服务未运行（可执行 docker start ppanel-caddy 启动）".to_string()
        };
        return json!({"installed": true, "active": running, "version": ver, "reason": reason});
    }

    if !caddy_installed() {
        return json!({"installed": false, "active": false, "version": "", "reason": ""});
    }
    let mut ver = String::new();
    let (rc, out) = host_ops::run(&["caddy", "version"], 5).await;
    if rc == 0 {
        ver = out.split_whitespace().next().unwrap_or("").to_string();
    }
    let (rc, out) = host_ops::run(&["systemctl", "is-active", "caddy"], 5).await;
    let state = out.trim().to_string();
    let active = rc == 0 && state == "active";
    let reason = if active {
        String::new()
    } else if port_in_use(80).await || port_in_use(443).await {
        "80/443 端口被宿主机其他 Web 服务占用（如 nginx/宝塔），Caddy 无法绑定。\
         需停用占用方，或由该服务自行配置反代与证书"
            .to_string()
    } else if state == "failed" {
        "服务启动失败（可执行 systemctl restart caddy 重试，journalctl -u caddy 查看详情）".to_string()
    } else {
        "服务未运行（可执行 systemctl restart caddy 启动）".to_string()
    };
    json!({"installed": true, "active": active, "version": ver, "reason": reason})
}

/// 本机公网出口 IP（缓存 10 分钟）；探测失败返回 None。
pub async fn public_ip() -> Option<String> {
    let cache = PUB_IP.get_or_init(|| Mutex::new(None));
    if let Ok(g) = cache.lock() {
        if let Some((ip, at)) = g.as_ref() {
            if at.elapsed().as_secs() < 600 {
                return Some(ip.clone());
            }
        }
    }
    for url in ["https://api.ipify.org", "https://ifconfig.me/ip"] {
        let (rc, out) = host_ops::run(&["curl", "-fs", "--max-time", "3", url], 6).await;
        let ip = out.trim().to_string();
        if rc == 0 && Regex::new(r"^\d{1,3}(\.\d{1,3}){3}$").unwrap().is_match(&ip) {
            if let Ok(mut g) = cache.lock() {
                *g = Some((ip.clone(), Instant::now()));
            }
            return Some(ip);
        }
    }
    None
}

/// 校验域名 A 记录是否指向本机公网 IP；无法取公网 IP 时仅做解析检查。
pub async fn check_dns(domain: &str) -> (bool, String) {
    let resolved = match tokio::net::lookup_host(format!("{domain}:443")).await {
        Ok(mut it) => it
            .find(|s| s.ip().is_ipv4())
            .or_else(|| None)
            .map(|s| s.ip().to_string()),
        Err(_) => None,
    };
    let Some(resolved) = resolved else {
        return (false, "域名无法解析，请先到 DNS 服务商添加 A 记录".to_string());
    };
    match public_ip().await {
        None => (
            true,
            format!("解析到 {resolved}（未能探测本机公网 IP，跳过比对）"),
        ),
        Some(ip) => {
            if resolved != ip {
                (
                    false,
                    format!("解析到 {resolved}，与本机公网 IP {ip} 不一致，证书将无法签发"),
                )
            } else {
                (true, format!("解析正确（{resolved}）"))
            }
        }
    }
}

pub fn site_file(instance_id: i64) -> PathBuf {
    Path::new(SITES_DIR).join(format!("ppanel-{instance_id}.caddy"))
}

/// 生成 bcrypt 哈希（Caddy basic_auth 要求；Python 用 `caddy hash-password`，结果等价）。
pub fn hash_password(plaintext: &str) -> AppResult<String> {
    bcrypt::hash(plaintext, bcrypt::DEFAULT_COST)
        .map_err(|_| AppError::internal("生成密码哈希失败"))
}

fn ip_matcher(name: &str, cidrs: &str, negate: bool) -> Vec<String> {
    let toks: Vec<String> = cidrs
        .replace(',', " ")
        .split_whitespace()
        .map(|t| t.to_string())
        .collect();
    if toks.is_empty() {
        return vec![];
    }
    let kw = if negate { "not remote_ip" } else { "remote_ip" };
    vec![format!("    @{} {} {}", name, kw, toks.join(" "))]
}

/// 渲染实例站点 Caddyfile 文本（与 Python write_site 一致）。
pub fn render_site(instance_id: i64, domain: &str, ext_port: i64, cfg: &SiteCfg) -> String {
    let mut body: Vec<String> = vec![
        format!("# ppanel 实例 {instance_id} 自动生成"),
        format!("{domain} {{"),
    ];
    let wl = cfg.ip_whitelist.trim();
    let bl = cfg.ip_blacklist.trim();
    if !wl.is_empty() {
        // 白名单：不在名单内一律拒绝（优先于黑名单）
        let mut m = ip_matcher(&format!("wl_{instance_id}"), wl, true);
        body.append(&mut m);
        body.push(format!("    abort @wl_{instance_id}"));
    } else if !bl.is_empty() {
        let mut m = ip_matcher(&format!("bl_{instance_id}"), bl, false);
        body.append(&mut m);
        body.push(format!("    abort @bl_{instance_id}"));
    }
    let au = cfg.auth_user.trim();
    let ah = cfg.auth_hash.trim();
    if !au.is_empty() && !ah.is_empty() {
        body.push("    basic_auth {".to_string());
        body.push(format!("        {au} {ah}"));
        body.push("    }".to_string());
    }
    if cfg.mode == "static" {
        let sub = cfg.static_dir.trim().trim_matches('/').replace("..", "");
        let sub = if sub.is_empty() { "public_html".to_string() } else { sub };
        let base = if cfg.app_root.trim().is_empty() {
            format!("/opt/ppanel/data/{instance_id}")
        } else {
            cfg.app_root.trim_end_matches('/').to_string()
        };
        body.push(format!("    root * {base}/{sub}"));
        body.push("    file_server".to_string());
    } else {
        body.push(format!("    reverse_proxy 127.0.0.1:{ext_port}"));
    }
    body.push("}".to_string());
    body.join("\n") + "\n"
}

/// 写入实例站点配置并 reload。
pub async fn write_site(
    state: &crate::state::AppState,
    instance_id: i64,
    domain: &str,
    ext_port: i64,
    cfg: &SiteCfg,
) -> AppResult<()> {
    let _ = state;
    write_site_cfg(instance_id, domain, ext_port, cfg).await
}

/// 不依赖 AppState 的写配置入口（供启动期/独立任务使用）。
pub async fn write_site_cfg(
    instance_id: i64,
    domain: &str,
    ext_port: i64,
    cfg: &SiteCfg,
) -> AppResult<()> {
    if !domain_ok(domain) {
        return Err(AppError::bad_request(
            "域名格式不正确（示例：panel.example.com）",
        ));
    }
    ensure_dirs().await;
    let conf = render_site(instance_id, domain, ext_port, cfg);
    let path = site_file(instance_id);
    let tmp = with_ext(&path, "tmp");
    tokio::fs::write(&tmp, conf.as_bytes())
        .await
        .map_err(|e| AppError::internal(format!("Caddy 配置写入失败：{e}")))?;
    tokio::fs::rename(&tmp, &path)
        .await
        .map_err(|e| AppError::internal(format!("Caddy 配置写入失败：{e}")))?;
    reload().await
}

fn with_ext(path: &Path, ext: &str) -> PathBuf {
    let mut s = path.to_string_lossy().to_string();
    s.push('.');
    s.push_str(ext);
    PathBuf::from(s)
}

/// 删除实例反代配置并 reload Caddy（文件不存在则静默）。
pub async fn remove_site(instance_id: i64) -> AppResult<()> {
    let path = site_file(instance_id);
    if tokio::fs::metadata(&path).await.is_ok() {
        tokio::fs::remove_file(&path)
            .await
            .map_err(|e| AppError::internal(format!("Caddy 配置删除失败：{e}")))?;
        reload().await?;
    }
    Ok(())
}

/// 路由 ensure（= 写配置并重载），面板侧公共入口。
pub async fn ensure_route(
    state: &crate::state::AppState,
    instance_id: i64,
    domain: &str,
    ext_port: i64,
    cfg: &SiteCfg,
) -> AppResult<()> {
    write_site(state, instance_id, domain, ext_port, cfg).await
}

/// 路由 remove，面板侧公共入口。
pub async fn remove_route(instance_id: i64) -> AppResult<()> {
    remove_site(instance_id).await
}

/// 重载 Caddy：容器内 `caddy reload` 优先，其次 systemd reload/restart。
pub async fn reload() -> AppResult<()> {
    if let Some((running, _)) = container_state().await {
        if running {
            let (rc, out) = match docker_exec(
                &caddy_container(),
                vec![
                    "caddy".to_string(),
                    "reload".to_string(),
                    "--config".to_string(),
                    MAIN_CONF.to_string(),
                ],
            )
            .await
            {
                Some(v) => v,
                None => return Err(AppError::internal("Caddy 容器不存在")),
            };
            if rc == 0 {
                return Ok(());
            }
            return Err(AppError::internal(format!(
                "Caddy 重载失败：{}",
                out.trim().chars().take(200).collect::<String>()
            )));
        }
        return Err(AppError::internal("Caddy 容器未运行"));
    }
    if !caddy_installed() {
        return Err(AppError::internal("Caddy 未安装"));
    }
    let (rc, out) = host_ops::run(&["systemctl", "reload", "caddy"], 10).await;
    let mut rc = rc;
    let mut out = out;
    if rc != 0 {
        let (r2, o2) = host_ops::run(&["systemctl", "restart", "caddy"], 15).await;
        rc = r2;
        out = o2;
    }
    if rc != 0 {
        return Err(AppError::internal(format!(
            "Caddy 重载失败：{}",
            out.trim().chars().take(200).collect::<String>()
        )));
    }
    Ok(())
}

/// 首次使用时准备 sites 目录并让主配置 include 它。
pub async fn ensure_dirs() {
    let _ = tokio::fs::create_dir_all(SITES_DIR).await;
    if let Ok(body) = tokio::fs::read_to_string(MAIN_CONF).await {
        if !body.contains("import /etc/caddy/sites/*") {
            if let Ok(mut f) = std::fs::OpenOptions::new().append(true).open(MAIN_CONF) {
                use std::io::Write;
                let _ = f.write_all(b"\nimport /etc/caddy/sites/*\n");
            }
        }
    }
}

/// 通过 Caddy Admin API 载入完整 JSON 配置（容器形态可选用）。
pub async fn admin_load(config: &Value) -> AppResult<()> {
    let url = format!("{}/load", admin_url().trim_end_matches('/'));
    let client = reqwest::Client::new();
    let resp = client
        .post(&url)
        .header("Content-Type", "application/json")
        .json(config)
        .timeout(Duration::from_secs(10))
        .send()
        .await
        .map_err(|e| AppError::bad_gateway(format!("Caddy Admin API 调用失败：{e}")))?;
    if !resp.status().is_success() {
        let code = resp.status().as_u16();
        let body = resp.text().await.unwrap_or_default();
        return Err(AppError::new(
            code,
            format!("Caddy Admin API 载入失败：{}", body.trim().chars().take(200).collect::<String>()),
        ));
    }
    Ok(())
}

/// 生成单条站点路由的 Caddy JSON（供 Admin API 形态使用）。
pub fn route_json(
    domain: &str,
    ext_port: i64,
    cfg: &SiteCfg,
    upstream_host: &str,
) -> Value {
    let mut route = json!({
        "match": [{"host": [domain]}],
    });
    let mut handlers: Vec<Value> = Vec::new();
    let wl = cfg.ip_whitelist.trim();
    let bl = cfg.ip_blacklist.trim();
    if !wl.is_empty() || !bl.is_empty() {
        let (list, negate) = if !wl.is_empty() { (wl, true) } else { (bl, false) };
        let cidrs: Vec<String> = list
            .replace(',', " ")
            .split_whitespace()
            .map(|x| x.to_string())
            .collect();
        let m = if negate {
            json!({"not": [{"remote_ip": {"ranges": cidrs}}]})
        } else {
            json!({"remote_ip": {"ranges": cidrs}})
        };
        handlers.push(json!({"handler": "abort", "match": [m]}));
    }
    if !cfg.auth_user.trim().is_empty() && !cfg.auth_hash.trim().is_empty() {
        let mut accounts = serde_json::Map::new();
        accounts.insert(
            cfg.auth_user.trim().to_string(),
            json!({"password": cfg.auth_hash.trim()}),
        );
        handlers.push(json!({"handler": "authentication", "providers": {"http_basic": {"accounts": accounts}}}));
    }
    if cfg.mode == "static" {
        let sub = cfg.static_dir.trim().trim_matches('/').replace("..", "");
        let sub = if sub.is_empty() { "public_html".into() } else { sub };
        let base = if cfg.app_root.trim().is_empty() {
            String::new()
        } else {
            cfg.app_root.trim_end_matches('/').to_string()
        };
        handlers.push(
            json!({"handler": "rewrite", "uri": format!("/{sub}/{{http.request.uri.path}}")}),
        );
        handlers.push(json!({"handler": "file_server", "root": if base.is_empty() { format!("/{sub}") } else { format!("{base}/{sub}") }}));
    } else {
        let host = if upstream_host.trim().is_empty() { "127.0.0.1" } else { upstream_host.trim() };
        handlers.push(json!({"handler": "reverse_proxy", "upstreams": [{"dial": format!("{host}:{ext_port}")}]}));
    }
    route["handle"] = json!(handlers);
    route
}

/// 站点是否已落盘。
pub fn site_exists(instance_id: i64) -> bool {
    site_file(instance_id).is_file()
}

// =====================================================================
// 证书观测：签发进度需要知道证书落没落盘、是谁签的、什么时候到期，
// 以及 Caddy 自己打出的 ACME 日志（面板「签发日志」直接展示）。
// =====================================================================

/// Caddy 数据目录下证书根目录的候选（systemd 安装 / root / caddy 用户 / 自定义）。
pub fn cert_roots() -> Vec<PathBuf> {
    let mut roots = vec![
        PathBuf::from("/var/lib/caddy/.local/share/caddy/certificates"),
        PathBuf::from("/root/.local/share/caddy/certificates"),
        PathBuf::from("/home/caddy/.local/share/caddy/certificates"),
    ];
    for key in ["CADDY_DATA_DIR", "XDG_DATA_HOME"] {
        if let Ok(v) = std::env::var(key) {
            let v = v.trim();
            if v.is_empty() {
                continue;
            }
            let base = PathBuf::from(v);
            roots.push(if base.ends_with("certificates") {
                base
            } else {
                base.join("caddy").join("certificates")
            });
        }
    }
    roots
}

/// 查找某域名的证书文件：`<root>/<acme-ca>/<domain>/<domain>.crt`。
pub async fn find_cert(domain: &str) -> Option<PathBuf> {
    if domain.trim().is_empty() {
        return None;
    }
    for root in cert_roots() {
        let mut rd = match tokio::fs::read_dir(&root).await {
            Ok(x) => x,
            Err(_) => continue,
        };
        while let Ok(Some(ent)) = rd.next_entry().await {
            let p = ent
                .path()
                .join(domain)
                .join(format!("{domain}.crt"));
            if tokio::fs::metadata(&p).await.is_ok() {
                return Some(p);
            }
        }
    }
    None
}

/// 解析证书：返回 (签发者, 到期日 YYYY-MM-DD)；没有 openssl 时返回 None。
pub async fn cert_meta(path: &Path) -> Option<(String, String)> {
    let p = path.to_string_lossy().to_string();
    let (rc, out) = host_ops::run(
        &["openssl", "x509", "-in", &p, "-noout", "-issuer", "-enddate"],
        5,
    )
    .await;
    if rc != 0 || out.trim().is_empty() {
        return None;
    }
    let mut issuer = String::new();
    let mut not_after = String::new();
    for line in out.lines() {
        let line = line.trim();
        if let Some(rest) = line.strip_prefix("issuer=") {
            // openssl 1.x 输出 `O=Let's Encrypt`，3.x 输出 `O = Let's Encrypt`，两种都要认
            let mut org = String::new();
            let mut cn = String::new();
            for part in rest.split(',') {
                let Some((k, v)) = part.split_once('=') else { continue };
                let key = k.trim();
                let val = v.trim();
                if key == "O" && org.is_empty() {
                    org = val.to_string();
                } else if key == "CN" && cn.is_empty() {
                    cn = val.to_string();
                }
            }
            issuer = if org.is_empty() { cn } else { org };
            if issuer.is_empty() {
                issuer = rest.trim().to_string();
            }
        } else if let Some(rest) = line.strip_prefix("notAfter=") {
            not_after = rest.trim().to_string();
        }
    }
    let pretty = chrono::NaiveDateTime::parse_from_str(&not_after, "%b %e %H:%M:%S %Y GMT")
        .map(|d| d.format("%Y-%m-%d").to_string())
        .unwrap_or(not_after);
    Some((issuer, pretty))
}

/// Caddy 从 `epoch` 起的原始日志（容器形态走 docker logs，否则 journalctl）。
pub async fn log_since(epoch: i64) -> String {
    if let Some((running, _)) = container_state().await {
        if running {
            if let Ok(docker) = docker_handle().get().await {
                use bollard::container::LogsOptions;
                use futures_util::StreamExt;
                let mut stream = docker.logs(
                    &caddy_container(),
                    Some(LogsOptions::<String> {
                        follow: false,
                        stdout: true,
                        stderr: true,
                        since: epoch,
                        until: 0,
                        timestamps: false,
                        tail: "400".to_string(),
                    }),
                );
                let mut out = String::new();
                while let Some(item) = stream.next().await {
                    if let Ok(bollard::container::LogOutput::StdOut { message })
                    | Ok(bollard::container::LogOutput::StdErr { message })
                    | Ok(bollard::container::LogOutput::Console { message }) = item
                    {
                        out.push_str(&String::from_utf8_lossy(&message));
                        out.push('\n');
                    }
                }
                return out;
            }
        }
    }
    let since = format!("@{epoch}");
    let (rc, out) = host_ops::run(
        &["journalctl", "-u", "caddy", "--since", &since, "--no-pager", "-o", "cat"],
        8,
    )
    .await;
    if rc == 0 {
        out
    } else {
        String::new()
    }
}

/// 严格校验（rustls 根证书）地访问 `https://<domain>/`：证书不可信即失败。
/// 返回 HTTP 状态码；连接失败/证书无效返回 None。
pub async fn https_probe(domain: &str) -> Option<u16> {
    static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    let client = CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .timeout(Duration::from_secs(8))
            // 只看 TLS 是否可用，不跟跳转（Caddy 可能 308 到 https）
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .unwrap_or_else(|_| reqwest::Client::new())
    });
    match client.get(format!("https://{domain}/")).send().await {
        Ok(r) => Some(r.status().as_u16()),
        Err(_) => None,
    }
}
