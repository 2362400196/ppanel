//! 宿主系统运维接口：系统概览、进程、systemd 服务、监听端口、系统日志、登录审计，
//! 以及运维级监控（资源曲线、流量、阈值告警、证书到期）。
//!
//! 鉴权与其它节点接口一致（`X-Node-Token`），由主控后台经 `/api/host/*?node=<id>` 转发。
//! 本模块只做「机器级」的读与受控写（服务的启停/自启、进程终止），不触碰实例目录与容器生命周期。

use axum::{
    extract::{Path, Query, State},
    routing::{delete, get, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::auth::NodeAuth;
use crate::error::{AppError, AppResult};
use crate::models::round1;
use crate::services::{host_certs, host_info, host_metrics, host_ops};
use crate::state::AppState;

#[derive(Debug, Deserialize, Default)]
struct ProcQuery {
    #[serde(default)]
    sort: Option<String>,
    #[serde(default)]
    limit: Option<usize>,
}

/// `ps` 的 etime（`[[dd-]hh:]mm:ss`）→ 秒，用于新面板的「运行时长」。
fn etime_secs(e: &str) -> i64 {
    let (days, rest) = match e.split_once('-') {
        Some((d, r)) => (d.parse::<i64>().unwrap_or(0), r),
        None => (0, e),
    };
    let parts: Vec<i64> = rest.split(':').filter_map(|x| x.parse::<i64>().ok()).collect();
    let (h, m, s) = match parts.len() {
        3 => (parts[0], parts[1], parts[2]),
        2 => (0, parts[0], parts[1]),
        1 => (0, 0, parts[0]),
        _ => (0, 0, 0),
    };
    days * 86400 + h * 3600 + m * 60 + s
}

async fn system(_a: NodeAuth) -> Json<Value> {
    Json(host_info::system().await)
}

async fn processes(_a: NodeAuth, Query(q): Query<ProcQuery>) -> Json<Value> {
    let sort = q.sort.unwrap_or_else(|| "cpu".to_string());
    let limit = q.limit.unwrap_or(200).clamp(1, 200);
    let mut v = host_info::processes(&sort, limit).await;
    // 新面板契约（与 Python 版 /host/processes 对齐）：rows + rss_mb/uptime_s。
    // items 保留给既有「系统监控」页，双写避免破坏旧调用方。
    if let Some(items) = v.get("items").and_then(|x| x.as_array()).cloned() {
        let rows: Vec<Value> = items
            .iter()
            .map(|it| {
                json!({
                    "pid": it.get("pid").cloned().unwrap_or(json!(0)),
                    "user": it.get("user").cloned().unwrap_or(json!("")),
                    "cpu": it.get("cpu").cloned().unwrap_or(json!(0)),
                    "mem": it.get("mem").cloned().unwrap_or(json!(0)),
                    "rss_mb": round1(
                        it.get("rss").and_then(|x| x.as_u64()).unwrap_or(0) as f64 / 1048576.0
                    ),
                    "state": it.get("state").cloned().unwrap_or(json!("")),
                    "uptime_s": etime_secs(it.get("etime").and_then(|x| x.as_str()).unwrap_or("")),
                    "cmd": it.get("cmd").cloned().unwrap_or(json!("")),
                })
            })
            .collect();
        v["rows"] = json!(rows);
    }
    Json(v)
}

async fn services(_a: NodeAuth) -> Json<Value> {
    let mut v = host_info::services().await;
    // 新面板读 rows[].desc；旧字段 description 保持不变
    if let Some(items) = v.get("items").and_then(|x| x.as_array()).cloned() {
        let rows: Vec<Value> = items
            .iter()
            .map(|it| {
                json!({
                    "unit": it.get("unit").cloned().unwrap_or(json!("")),
                    "load": it.get("load").cloned().unwrap_or(json!("")),
                    "active": it.get("active").cloned().unwrap_or(json!("")),
                    "sub": it.get("sub").cloned().unwrap_or(json!("")),
                    "desc": it.get("description").cloned().unwrap_or(json!("")),
                    "enabled": it.get("enabled").cloned().unwrap_or(json!("-")),
                })
            })
            .collect();
        v["rows"] = json!(rows);
    }
    Json(v)
}

#[derive(Debug, Deserialize)]
struct ServiceActionIn {
    unit: String,
    action: String,
}

async fn service_action(_a: NodeAuth, Json(body): Json<ServiceActionIn>) -> AppResult<Json<Value>> {
    host_info::service_action(&body.unit, &body.action)
        .await
        .map(Json)
        .map_err(|e| {
            if e.contains("无效") || e.contains("不支持") {
                AppError::bad_request(e)
            } else {
                AppError::bad_gateway(e)
            }
        })
}

/// 新面板使用 RESTful 风格：POST /host/services/{unit}/{action}
async fn service_action_path(
    _a: NodeAuth,
    Path((unit, action)): Path<(String, String)>,
) -> AppResult<Json<Value>> {
    let v = host_info::service_action(&unit, &action)
        .await
        .map_err(|e| {
            if e.contains("无效") || e.contains("不支持") {
                AppError::bad_request(e)
            } else {
                AppError::bad_gateway(e)
            }
        })?;
    // 前端读 data.detail
    Ok(Json(json!({
        "detail": format!("{unit} {action} 执行成功"),
        "unit": unit,
        "action": action,
        "result": v,
    })))
}

async fn ports(_a: NodeAuth) -> Json<Value> {
    let mut v = host_info::ports().await;
    // 新面板契约：rows，port 为字符串（前端做 includes 搜索）、无进程时 process="-"
    if let Some(items) = v.get("items").and_then(|x| x.as_array()).cloned() {
        let rows: Vec<Value> = items
            .iter()
            .map(|it| {
                let proc_name = it
                    .get("process")
                    .and_then(|x| x.as_str())
                    .filter(|s| !s.is_empty())
                    .unwrap_or("-");
                json!({
                    "proto": it.get("proto").cloned().unwrap_or(json!("")),
                    "port": it.get("port").map(|p| p.to_string().trim_matches('"').to_string())
                        .unwrap_or_default(),
                    "addr": it.get("addr").cloned().unwrap_or(json!("")),
                    "process": proc_name,
                    "pid": it.get("pid").cloned().unwrap_or(json!(0)),
                })
            })
            .collect();
        v["rows"] = json!(rows);
        v["tool"] = json!("ss");
    }
    Json(v)
}

#[derive(Debug, Deserialize)]
struct PortKillIn {
    port: u16,
    #[serde(default)]
    proto: Option<String>,
}

async fn port_kill(_a: NodeAuth, Json(body): Json<PortKillIn>) -> AppResult<Json<Value>> {
    // port_kill 的错误本身就是 AppError（bad_request），直接透传
    host_info::port_kill(body.port, body.proto.as_deref().unwrap_or("tcp"))
        .await
        .map(Json)
}

/// 系统关键进程：终止它们会断 SSH / 断 Docker / 断 agent 自身。
const PROTECTED_PROCS: &[&str] = &[
    "systemd",
    "init",
    "sshd",
    "dockerd",
    "containerd",
    "ppanel-agent",
];

#[derive(Debug, Deserialize)]
struct ProcKillIn {
    pid: i64,
    #[serde(default)]
    sig: Option<String>,
}

/// 发送信号（拆分出来是为了让非 unix 平台也能编译通过）。
#[cfg(unix)]
fn send_signal(pid: i64, sig_raw: &str) -> AppResult<()> {
    let sig = if sig_raw == "KILL" {
        libc::SIGKILL
    } else {
        libc::SIGTERM
    };
    let rc = unsafe { libc::kill(pid as libc::pid_t, sig) };
    if rc != 0 {
        let err = std::io::Error::last_os_error();
        return Err(if err.raw_os_error() == Some(libc::EPERM) {
            AppError::forbidden("无权限终止该进程")
        } else {
            AppError::bad_gateway(format!("终止失败：{err}"))
        });
    }
    Ok(())
}

#[cfg(not(unix))]
fn send_signal(_pid: i64, _sig_raw: &str) -> AppResult<()> {
    Err(AppError::bad_request("当前平台不支持进程终止"))
}

/// 按 PID 终止进程（新面板「进程管理」页）。与按端口终止互补：
/// 这里保护 pid<=1、自身、以及 sshd/dockerd/systemd 等关键进程。
async fn process_kill(_a: NodeAuth, Json(body): Json<ProcKillIn>) -> AppResult<Json<Value>> {
    let sig_raw = body
        .sig
        .as_deref()
        .unwrap_or("TERM")
        .trim()
        .to_uppercase();
    if body.pid <= 1 {
        return Err(AppError::bad_request("不允许终止 pid<=1 的进程"));
    }
    if body.pid == std::process::id() as i64 {
        return Err(AppError::bad_request("不允许终止面板进程"));
    }
    if !matches!(sig_raw.as_str(), "TERM" | "KILL") {
        return Err(AppError::bad_request("信号仅支持 TERM/KILL"));
    }
    let comm = std::fs::read_to_string(format!("/proc/{}/comm", body.pid))
        .unwrap_or_default()
        .trim()
        .to_string();
    if PROTECTED_PROCS.contains(&comm.as_str()) {
        return Err(AppError::forbidden(format!(
            "{comm} 是系统关键进程，禁止从这里终止（请用 systemctl）"
        )));
    }
    if !std::path::Path::new(&format!("/proc/{}", body.pid)).exists() {
        return Err(AppError::not_found("进程不存在（可能已退出）"));
    }
    send_signal(body.pid, &sig_raw)?;
    Ok(Json(json!({
        "detail": format!("已向进程 {} 发送 {} 信号", body.pid, sig_raw),
        "pid": body.pid,
        "sig": sig_raw,
    })))
}

async fn mem_cleanup(_a: NodeAuth) -> Json<Value> {
    Json(host_info::mem_cleanup().await)
}

#[derive(Debug, Deserialize, Default)]
struct JournalQuery {
    #[serde(default)]
    unit: Option<String>,
    #[serde(default)]
    lines: Option<usize>,
}

async fn journal(_a: NodeAuth, Query(q): Query<JournalQuery>) -> Json<Value> {
    let n = q.lines.unwrap_or(200);
    let mut v = host_info::journal(q.unit.as_deref(), n).await;
    // 新面板读 text（整段日志）；lines 数组保留给旧页面
    if let Some(lines) = v.get("lines").and_then(|x| x.as_array()) {
        let text: Vec<String> = lines
            .iter()
            .filter_map(|l| l.as_str().map(|s| s.to_string()))
            .collect();
        v["text"] = json!(text.join("\n"));
    }
    Json(v)
}

async fn users(_a: NodeAuth) -> Json<Value> {
    Json(host_info::users().await)
}

#[derive(Debug, Deserialize, Default)]
struct LoginQuery {
    #[serde(default)]
    limit: Option<usize>,
}

async fn ssh_logins(_a: NodeAuth, Query(q): Query<LoginQuery>) -> Json<Value> {
    Json(host_info::ssh_logins(q.limit.unwrap_or(100)).await)
}

// ---------- 登录审计（last / lastb，新面板契约） ----------

/// 解析 `last -F` 输出：user tty host 完整时间 剩余段。
fn parse_last(out: &str) -> Vec<Value> {
    let pat = regex::Regex::new(
        r"^(\S+)\s+(\S+)\s+(\S+)\s+(\w{3}\s+\w{3}\s+\d{1,2}\s+[\d:]+\s+\d{4})\s+(.*)$",
    )
    .unwrap();
    let mut rows = Vec::new();
    for line in out.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with("wtmp begins") || line.starts_with("btmp begins") {
            continue;
        }
        let Some(c) = pat.captures(line) else { continue };
        let tail = c.get(5).map(|m| m.as_str()).unwrap_or("").trim().to_string();
        let status = if tail.contains("still logged in") {
            "online"
        } else if tail.starts_with('-') {
            "logout"
        } else {
            "abnormal"
        };
        rows.push(json!({
            "user": c.get(1).map(|m| m.as_str()).unwrap_or(""),
            "tty": c.get(2).map(|m| m.as_str()).unwrap_or(""),
            "source": c.get(3).map(|m| m.as_str()).unwrap_or(""),
            "time": c.get(4).map(|m| m.as_str()).unwrap_or(""),
            "status": status,
            "tail": tail,
        }));
    }
    rows
}

async fn logins(_a: NodeAuth) -> Json<Value> {
    let (rc, out) = host_ops::run(&["last", "-F", "-n", "50"], 10).await;
    let logins = if rc == 0 { parse_last(&out) } else { Vec::new() };
    let mut failed: Vec<Value> = Vec::new();
    if std::path::Path::new("/var/log/btmp").exists() {
        let (rc2, out2) = host_ops::run(&["lastb", "-F", "-n", "50"], 10).await;
        if rc2 == 0 {
            failed = parse_last(&out2);
        }
    }
    Json(json!({
        "logins": logins,
        "failed": failed,
        "available": rc == 0 && std::path::Path::new("/var/log/wtmp").exists(),
    }))
}

// ---------- 监控曲线 / 流量 / 告警 / 证书 ----------

#[derive(Debug, Deserialize, Default)]
struct MetricsQuery {
    #[serde(default)]
    hours: Option<i64>,
}

async fn metrics(
    _a: NodeAuth,
    State(state): State<AppState>,
    Query(q): Query<MetricsQuery>,
) -> Json<Value> {
    Json(host_metrics::aggregate(&state.db, q.hours.unwrap_or(24)).await)
}

async fn traffic(_a: NodeAuth, State(state): State<AppState>) -> Json<Value> {
    Json(host_metrics::traffic(&state.db).await)
}

async fn alerts_get(_a: NodeAuth, State(state): State<AppState>) -> AppResult<Json<Value>> {
    host_metrics::alerts_get(&state.db).await.map(Json)
}

async fn alerts_put(
    _a: NodeAuth,
    State(state): State<AppState>,
    Json(body): Json<host_metrics::AlertIn>,
) -> AppResult<Json<Value>> {
    host_metrics::alerts_put(&state.db, &body).await.map(Json)
}

async fn alerts_test(_a: NodeAuth, State(state): State<AppState>) -> AppResult<Json<Value>> {
    host_metrics::alerts_test(&state.db).await.map(Json)
}

async fn alerts_clear_logs(_a: NodeAuth, State(state): State<AppState>) -> AppResult<Json<Value>> {
    host_metrics::alerts_clear_logs(&state.db).await.map(Json)
}

async fn certs(_a: NodeAuth, State(state): State<AppState>) -> Json<Value> {
    Json(host_certs::certs(&state.db).await)
}

pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/host/system", get(system))
        .route("/host/processes", get(processes))
        .route("/host/processes/kill", post(process_kill))
        .route("/host/services", get(services))
        .route("/host/services/action", post(service_action))
        .route("/host/services/{unit}/{action}", post(service_action_path))
        .route("/host/ports", get(ports))
        .route("/host/ports/kill", post(port_kill))
        .route("/host/mem/cleanup", post(mem_cleanup))
        .route("/host/journal", get(journal))
        .route("/host/users", get(users))
        .route("/host/ssh-logins", get(ssh_logins))
        .route("/host/logins", get(logins))
        .route("/host/metrics", get(metrics))
        .route("/host/traffic", get(traffic))
        .route("/host/alerts", get(alerts_get).put(alerts_put))
        .route("/host/alerts/test", post(alerts_test))
        .route("/host/alerts/logs", delete(alerts_clear_logs))
        .route("/host/certs", get(certs))
}
