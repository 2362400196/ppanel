//! 独立面板主路由（移植 app/routers/panel.py）：
//! 一个面板令牌绑定一个实例，持有令牌即可登录面板操作该容器，
//! 不依赖主控、无用户体系（开通时下发 panel_token 即可对接任意商城）。
//!
//! 鉴权：REST 走 `Authorization: Bearer <JWT>`，浏览器下载兜底 `?t=`；
//! WS 走 `?ptoken=`（浏览器无法带 header）。到期后按 EXPIRED_PANEL_MODE 降级只读。

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::OnceLock;

use axum::body::Body;
use axum::extract::{FromRequestParts, Multipart, Path as AxPath, Query, State};
use axum::http::request::Parts;
use axum::http::{header, Method};
use axum::response::Response;
use axum::routing::{get, post, put};
use axum::{Json, Router};
use chrono::Utc;
use regex::Regex;
use serde::Deserialize;
use serde_json::{json, Value};
use tokio_util::io::ReaderStream;

use crate::auth::{create_panel_token, panel_session_from_parts, panel_token_fp};
use crate::config::settings;
use crate::error::{AppError, AppResult};
use crate::models::{dt_iso, opt_dt_iso, round1, utcnow, Instance};
use crate::services::{caddy, catalog, cert_job, file, instance, mysql};
use crate::state::AppState;

// =====================================================================
// 鉴权：面板令牌 → 实例（等价于 Python 的 Depends(_dep)）
// =====================================================================

/// 面板已登录实例（含到期降级检查）。
pub struct PanelInst(pub Instance);

fn expired(inst: &Instance) -> bool {
    match inst.expire_at {
        Some(e) => utcnow() >= e,
        None => false,
    }
}

/// 到期后的面板行为：readonly 只放行 GET（可看状态/下载备份），block 全锁。
fn check_expire(method: &Method, inst: &Instance) -> AppResult<()> {
    if !expired(inst) {
        return Ok(());
    }
    if settings().expired_panel_mode != "block" && method == Method::GET {
        return Ok(());
    }
    Err(AppError::forbidden("实例已到期，请联系商家续期"))
}

async fn load_panel_inst(state: &AppState, iid: i64, fp: Option<&str>) -> AppResult<Instance> {
    let inst: Option<Instance> = sqlx::query_as("SELECT * FROM instances WHERE id = ?")
        .bind(iid)
        .fetch_optional(&state.db)
        .await?;
    match inst {
        Some(i) => {
            let cur = i.panel_token.as_deref().unwrap_or("");
            if cur.is_empty() {
                return Err(AppError::unauthorized("面板凭证已失效"));
            }
            // 会话必须绑定当前令牌：管理员重置面板令牌后，旧会话立即作废
            if fp.unwrap_or("") != panel_token_fp(cur) {
                return Err(AppError::unauthorized("面板会话已失效，请重新登录"));
            }
            Ok(i)
        }
        None => Err(AppError::unauthorized("面板凭证已失效")),
    }
}

impl FromRequestParts<AppState> for PanelInst {
    type Rejection = AppError;

    async fn from_request_parts(parts: &mut Parts, state: &AppState) -> Result<Self, Self::Rejection> {
        let (iid, fp) = panel_session_from_parts(parts)
            .ok_or_else(|| AppError::unauthorized("面板未登录或凭证已失效"))?;
        let inst = load_panel_inst(state, iid, fp.as_deref()).await?;
        check_expire(&parts.method, &inst)?;
        Ok(PanelInst(inst))
    }
}

/// 面板实例输出（字段与 Python `_out` 一致）。
pub fn panel_out_public(inst: &Instance) -> Value {
    panel_out(inst)
}

fn panel_out(inst: &Instance) -> Value {
    let mut v = inst.out_json();
    v["owner_ref"] = json!(inst.owner_ref);
    v
}

// =====================================================================
// 登录
// =====================================================================

#[derive(Debug, Deserialize)]
struct LoginIn {
    token: String,
}

async fn panel_login(State(state): State<AppState>, Json(body): Json<LoginIn>) -> AppResult<Json<Value>> {
    let raw = body.token.trim().to_string();
    if raw.len() < 8 || raw.len() > 128 {
        return Err(AppError::bad_request("令牌长度应为 8-128"));
    }
    let mut inst: Option<Instance> =
        sqlx::query_as("SELECT * FROM instances WHERE panel_token = ?")
            .bind(&raw)
            .fetch_optional(&state.db)
            .await?;
    let Some(inst) = inst.as_mut() else {
        return Err(AppError::unauthorized("面板令牌无效"));
    };
    instance::sync_status(&state, inst).await;
    instance::persist_status(&state.db, inst).await?;
    Ok(Json(json!({
        "token": create_panel_token(inst.id, &raw),
        "instance": panel_out(inst),
    })))
}

// =====================================================================
// 实例信息 / 生命周期
// =====================================================================

async fn panel_instance(PanelInst(mut inst): PanelInst, State(state): State<AppState>) -> AppResult<Json<Value>> {
    instance::sync_status(&state, &mut inst).await;
    instance::persist_status(&state.db, &inst).await?;
    Ok(Json(panel_out(&inst)))
}

async fn panel_runtime(PanelInst(inst): PanelInst, State(state): State<AppState>) -> AppResult<Json<Value>> {
    Ok(Json(instance::runtime_versions(&state, &inst).await?))
}

async fn panel_switch_runtime(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    let img = body.get("image").and_then(|v| v.as_str()).unwrap_or("");
    Ok(Json(instance::switch_runtime(&state, &inst, img).await?))
}

#[derive(Debug, Deserialize)]
struct UpdateIn {
    #[serde(default)]
    start_cmd: Option<String>,
    #[serde(default)]
    note: Option<String>,
}

async fn panel_update(
    PanelInst(mut inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<UpdateIn>,
) -> AppResult<Json<Value>> {
    let mut changed: Vec<&str> = Vec::new();
    if let Some(note) = body.note {
        if note.chars().count() > 255 {
            return Err(AppError::bad_request("备注不超过 255 字符"));
        }
        if note != inst.note {
            inst.note = note;
            changed.push("note");
        }
    }
    if let Some(sc) = body.start_cmd {
        if sc.chars().count() > 512 {
            return Err(AppError::bad_request("启动命令不超过 512 字符"));
        }
        if sc.trim() != inst.start_cmd {
            // 命令烧进容器 entry，需重建容器；运行中也直接应用：停止 → 重建 → 拉起
            // （restarting = 应用崩溃循环，同样视为运行中）
            let was_running = matches!(inst.status.as_str(), "running" | "restarting");
            if was_running {
                instance::stop_instance(&state, &mut inst).await?;
            }
            inst.start_cmd = sc.trim().to_string();
            let cid = instance::recreate_container(&state, &inst).await?;
            inst.container_id = Some(cid);
            if was_running {
                instance::start_instance(&state, &mut inst).await?;
            }
            changed.push("start_cmd");
        }
    }
    if !changed.is_empty() {
        instance::log_op(&state.db, inst.id, "update", &changed.join(",")).await?;
        // persist_status 只写 status/container_id，start_cmd 变更需单独落库，
        // 否则刷新/重启后从 DB 重读又回到旧命令
        if changed.iter().any(|c| *c == "start_cmd") {
            sqlx::query("UPDATE instances SET start_cmd = ? WHERE id = ?")
                .bind(&inst.start_cmd)
                .bind(inst.id)
                .execute(&state.db)
                .await?;
        }
        instance::persist_status(&state.db, &inst).await?;
    }
    Ok(Json(panel_out(&inst)))
}

async fn panel_action(
    PanelInst(mut inst): PanelInst,
    State(state): State<AppState>,
    AxPath(action): AxPath<String>,
) -> AppResult<Json<Value>> {
    if !["start", "stop", "restart"].contains(&action.as_str()) {
        return Err(AppError::not_found("未知操作"));
    }
    match action.as_str() {
        "start" => {
            instance::start_instance(&state, &mut inst).await?;
            instance::persist_status(&state.db, &inst).await?;
            instance::diagnose_startup(&state, &inst).await?;
            instance::log_op(&state.db, inst.id, "start", "").await?;
            instance::persist_status(&state.db, &inst).await?;
            Ok(Json(json!({"status": "running"})))
        }
        "stop" => {
            instance::stop_instance(&state, &mut inst).await?;
            instance::log_op(&state.db, inst.id, "stop", "").await?;
            instance::persist_status(&state.db, &inst).await?;
            Ok(Json(json!({"status": inst.status})))
        }
        _ => {
            instance::restart_instance(&state, &mut inst).await?;
            instance::log_op(&state.db, inst.id, "restart", "").await?;
            instance::persist_status(&state.db, &inst).await?;
            Ok(Json(json!({"status": inst.status})))
        }
    }
}

// =====================================================================
// 共享 MySQL 数据库（一实例一库）
// =====================================================================

async fn panel_database(PanelInst(inst): PanelInst, State(state): State<AppState>) -> AppResult<Json<Value>> {
    let db = mysql::get_instance_db(&state, inst.id).await?;
    let versions = mysql::enabled_versions(&state.db).await?;
    Ok(Json(json!({"db": db, "versions": versions})))
}

async fn panel_db_create(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    let ver = body.get("version").and_then(|v| v.as_str()).unwrap_or("");
    let info = mysql::create_instance_db(&state, inst.id, ver).await?;
    let detail = format!(
        "MySQL {} {}",
        info["version"].as_str().unwrap_or(""),
        info["db_name"].as_str().unwrap_or("")
    );
    instance::log_op(&state.db, inst.id, "db_create", &detail).await?;
    Ok(Json(info))
}

async fn panel_db_reset_password(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
) -> AppResult<Json<Value>> {
    let info = mysql::reset_db_password(&state, inst.id).await?;
    let detail = format!(
        "MySQL {} {}",
        info["version"].as_str().unwrap_or(""),
        info["db_name"].as_str().unwrap_or("")
    );
    instance::log_op(&state.db, inst.id, "db_reset_password", &detail).await?;
    Ok(Json(info))
}

async fn panel_db_switch(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    let ver = body.get("version").and_then(|v| v.as_str()).unwrap_or("");
    let info = mysql::switch_instance_db(&state, inst.id, ver).await?;
    let detail = format!(
        "MySQL → {} {}",
        info["version"].as_str().unwrap_or(""),
        info["db_name"].as_str().unwrap_or("")
    );
    instance::log_op(&state.db, inst.id, "db_switch", &detail).await?;
    Ok(Json(info))
}

async fn panel_db_drop(PanelInst(inst): PanelInst, State(state): State<AppState>) -> AppResult<Json<Value>> {
    let info = mysql::get_instance_db(&state, inst.id).await?;
    mysql::drop_instance_db(&state.db, inst.id).await?;
    let detail = info
        .as_ref()
        .map(|v| {
            format!(
                "MySQL {} {}",
                v["version"].as_str().unwrap_or(""),
                v["db_name"].as_str().unwrap_or("")
            )
        })
        .unwrap_or_default();
    instance::log_op(&state.db, inst.id, "db_drop", &detail).await?;
    Ok(Json(json!({"detail": "数据库已删除"})))
}

// =====================================================================
// 历史用量 / 操作记录 / 实时状态 / 日志
// =====================================================================

async fn panel_metrics(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let hours: f64 = q
        .get("hours")
        .and_then(|v| v.parse::<f64>().ok())
        .unwrap_or(6.0)
        .clamp(0.5, 24.0);
    let since = Utc::now().naive_utc() - chrono::Duration::seconds((hours * 3600.0) as i64);
    let rows: Vec<(Option<chrono::NaiveDateTime>, f64, f64, f64, f64, f64)> = sqlx::query_as(
        "SELECT ts, cpu_percent, mem_used_mb, mem_limit_mb, net_rx_mb, net_tx_mb \
         FROM agent_metrics WHERE instance_id = ? AND ts >= ? ORDER BY ts",
    )
    .bind(inst.id)
    .bind(since)
    .fetch_all(&state.db)
    .await?;
    let points: Vec<Value> = rows
        .into_iter()
        .map(|(ts, cpu, mem, mem_limit, rx, tx)| {
            json!({
                "ts": ts.map(|t| dt_iso(&t)),
                "cpu": crate::models::round2(cpu),
                "mem": round1(mem),
                "mem_limit": round1(mem_limit),
                "rx": (rx * 1000.0).round() / 1000.0,
                "tx": (tx * 1000.0).round() / 1000.0,
            })
        })
        .collect();
    Ok(Json(json!({"points": points})))
}

async fn panel_ops_log(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let limit: i64 = q
        .get("limit")
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(20)
        .clamp(1, 500);
    let rows: Vec<(String, String, Option<chrono::NaiveDateTime>)> = sqlx::query_as(
        "SELECT action, detail, created_at FROM agent_op_logs \
         WHERE instance_id = ? ORDER BY id DESC LIMIT ?",
    )
    .bind(inst.id)
    .bind(limit)
    .fetch_all(&state.db)
    .await?;
    Ok(Json(json!(rows
        .into_iter()
        .map(|(action, detail, created_at)| json!({
            "action": action,
            "detail": detail,
            "created_at": opt_dt_iso(&created_at),
        }))
        .collect::<Vec<_>>())))
}

async fn panel_stats(PanelInst(inst): PanelInst, State(state): State<AppState>) -> Json<Value> {
    Json(instance::container_stats(&state, &inst).await)
}

async fn panel_logs(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let tail: i64 = q.get("tail").and_then(|v| v.parse().ok()).unwrap_or(200);
    let logs = instance::container_logs(&state, &inst, tail).await?;
    Ok(Json(json!({"logs": logs})))
}

// =====================================================================
// 域名与 SSL
// =====================================================================

fn domain_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        // 注意：Rust 的 regex crate 不支持 look-around（`(?=...)`），
        // 用 look-ahead 会在首次调用时 panic，导致域名接口直接断连。
        // 长度约束（4~253）改由 domain_ok 单独判断。
        Regex::new(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
            .expect("内置正则编译失败")
    })
}

/// 域名合法性：长度 4~253 + 正则（RFC 1123 子域规则，首尾不能是连字符）。
fn domain_ok(s: &str) -> bool {
    (4..=253).contains(&s.len()) && domain_re().is_match(s)
}

async fn panel_get_domain(PanelInst(inst): PanelInst, State(state): State<AppState>) -> AppResult<Json<Value>> {
    let _ = &state;
    let domain = inst.domain.clone().unwrap_or_default().trim().to_string();
    let info = caddy::caddy_info().await;
    let ip = caddy::public_ip().await;
    let mut out = json!({
        "domain": domain,
        "caddy": info,
        "https_url": if domain.is_empty() { String::new() } else { format!("https://{domain}") },
        "http_url": match (&ip, inst.ext_port) {
            (Some(ip), p) if p > 0 => format!("http://{ip}:{p}"),
            _ => String::new(),
        },
        "ssl_active": !domain.is_empty() && info.get("active").and_then(|v| v.as_bool()).unwrap_or(false),
        // 证书现状（签发进度页与状态行共用）
        "cert": crate::services::cert_job::cert_summary(&domain).await,
        // 是否存在正在跑的绑定流程
        "job_status": crate::services::cert_job::status_of(inst.id),
    });
    if !domain.is_empty() {
        let (ok, msg) = caddy::check_dns(&domain).await;
        out["dns_ok"] = json!(ok);
        out["dns_msg"] = json!(msg);
    }
    Ok(Json(out))
}

async fn panel_set_domain(
    PanelInst(mut inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    let raw = body
        .get("domain")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_lowercase();

    if raw.is_empty() {
        if inst.domain.is_some() {
            let old = inst.domain.clone().unwrap_or_default();
            cert_job::begin(inst.id, "");
            cert_job::info(inst.id, &format!("开始解绑域名 {old}"));
            cert_job::info(inst.id, "移除 Caddy 站点配置并重载 …");
            caddy::remove_site(inst.id).await?;
            instance::log_op(&state.db, inst.id, "domain_unbind", &old).await?;
            sqlx::query("UPDATE instances SET domain = NULL WHERE id = ?")
                .bind(inst.id)
                .execute(&state.db)
                .await?;
            inst.domain = None;
            cert_job::ok(inst.id, "站点配置已移除，https 立即失效（证书文件仍保留，重绑可复用）");
            cert_job::finish(inst.id, true, "解绑完成");
        }
        return Ok(Json(json!({"domain": ""})));
    }

    if !domain_ok(&raw) {
        return Err(AppError::bad_request("域名格式不正确，如 app.example.com"));
    }
    let taken: Option<i64> =
        sqlx::query_scalar("SELECT id FROM instances WHERE domain = ? AND id != ? LIMIT 1")
            .bind(&raw)
            .bind(inst.id)
            .fetch_optional(&state.db)
            .await?;
    if taken.is_some() {
        return Err(AppError::conflict("该域名已被其他实例绑定"));
    }
    if !caddy::caddy_installed() {
        return Err(AppError::bad_request(
            "节点未安装 Caddy，无法自动签发证书（联系管理员安装）",
        ));
    }
    if inst.ext_port <= 0 {
        return Err(AppError::bad_request("实例没有外部端口，无法反代"));
    }

    // 校验都是毫秒级，保持在请求内同步返回；真正耗时的 DNS 复核 + 写配置 + 等签发
    // 放到后台任务里跑，前端轮询 /panel/instance/domain/log 看进度。
    cert_job::begin(inst.id, &raw);
    let st = state.clone();
    let dom = raw.clone();
    tokio::spawn(async move {
        cert_job::bind(st, inst, dom).await;
    });

    Ok(Json(json!({
        "domain": raw,
        "pending": true,
        "message": "已提交，正在签发证书",
    })))
}

/// 域名绑定 / 证书签发进度日志（`since` = 已读条数）。
async fn panel_domain_log(
    PanelInst(inst): PanelInst,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let since = q
        .get("since")
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(0);
    Ok(Json(cert_job::snapshot(inst.id, since)))
}

// =====================================================================
// 依赖安装（Python / pip）
// =====================================================================

fn ensure_no_running_job(state: &AppState, iid: i64) -> AppResult<()> {
    if state.jobs.has_running(iid) {
        return Err(AppError::conflict("已有任务正在执行，请等待完成后再试"));
    }
    Ok(())
}

async fn panel_install(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let name = q.get("file").cloned().unwrap_or_else(|| "requirements.txt".to_string());
    if name.is_empty() || name.contains('/') || name.contains('\\') || name.contains("..") {
        return Err(AppError::bad_request("非法文件名"));
    }
    ensure_no_running_job(&state, inst.id)?;
    let job = instance::start_install(&state, &inst, &name).await?;
    instance::log_op(&state.db, inst.id, "deps_install", &name).await?;
    Ok(Json(json!({"job_id": job.id})))
}

fn name_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(r"^[A-Za-z0-9][A-Za-z0-9_.\[\]=<>!~+,\-]*$").expect("内置正则编译失败")
    })
}

fn check_names(raw: &str) -> AppResult<Vec<String>> {
    let names: Vec<String> = raw
        .replace(',', " ")
        .split_whitespace()
        .map(|s| s.to_string())
        .collect();
    if names.is_empty() {
        return Err(AppError::bad_request("请输入依赖名称"));
    }
    for n in &names {
        if !name_re().is_match(n) {
            return Err(AppError::bad_request(format!("非法的依赖名称：{n}")));
        }
    }
    Ok(names)
}

async fn panel_install_pkgs(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    ensure_no_running_job(&state, inst.id)?;
    let raw = body.get("names").and_then(|v| v.as_str()).unwrap_or("");
    let names = check_names(raw)?;
    let mut args = vec!["install".to_string()];
    args.extend(names.iter().cloned());
    let job = instance::start_pip(&state, &inst, &args).await?;
    instance::log_op(&state.db, inst.id, "deps_install", &names.join(" ")).await?;
    Ok(Json(json!({"job_id": job.id})))
}

async fn panel_uninstall_pkgs(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    ensure_no_running_job(&state, inst.id)?;
    let raw = body.get("names").and_then(|v| v.as_str()).unwrap_or("");
    let names = check_names(raw)?;
    let mut args = vec!["uninstall".to_string(), "-y".to_string()];
    args.extend(names.iter().cloned());
    let job = instance::start_pip(&state, &inst, &args).await?;
    instance::log_op(&state.db, inst.id, "deps_uninstall", &names.join(" ")).await?;
    Ok(Json(json!({"job_id": job.id})))
}

async fn panel_deps_installed(PanelInst(inst): PanelInst, State(state): State<AppState>) -> AppResult<Json<Value>> {
    Ok(Json(json!({"packages": instance::pip_list(&state, &inst).await?})))
}

fn require_python(inst: &Instance) -> AppResult<()> {
    if instance::is_php(&inst.image) {
        return Err(AppError::bad_request("该功能仅支持 Python 环境"));
    }
    Ok(())
}

/// 容器内执行并取 stdout（等价 Python `_php_exec`：非 0 退出码报 500）。
async fn exec_stdout(state: &AppState, inst: &Instance, cmd: Vec<String>) -> AppResult<String> {
    let (code, out) = instance::exec_capture(
        state,
        inst,
        cmd,
        std::time::Duration::from_secs(120),
    )
    .await?;
    if code != 0 {
        return Err(AppError::internal("容器内命令执行失败"));
    }
    Ok(out.replace("\r\n", "\n").replace('\r', ""))
}

/// 容器是否在运行 + 容器 ID（不存在则 running=false）。
async fn container_state(state: &AppState, iid: i64) -> (bool, String) {
    let Ok(docker) = state.docker.get().await else {
        return (false, String::new());
    };
    match docker.inspect_container(&instance::container_name(iid), None).await {
        Ok(info) => (
            info.state.as_ref().and_then(|s| s.running).unwrap_or(false),
            info.id.unwrap_or_default(),
        ),
        Err(_) => (false, String::new()),
    }
}

async fn panel_py_packages(PanelInst(inst): PanelInst, State(state): State<AppState>) -> AppResult<Json<Value>> {
    require_python(&inst)?;
    let (running, _cid) = container_state(&state, inst.id).await;
    let mut installed: HashSet<String> = HashSet::new();
    if running {
        let out = exec_stdout(
            &state,
            &inst,
            vec!["pip".into(), "list".into(), "--format=freeze".into()],
        )
        .await?;
        for line in out.lines() {
            if let Some((name, _)) = line.split_once("==") {
                installed.insert(name.trim().to_lowercase());
            }
        }
    }
    let catalog: Vec<Value> = catalog::py_pkgs()
        .into_iter()
        .map(|mut it| {
            let name = it.get("name").and_then(|v| v.as_str()).unwrap_or("").to_lowercase();
            it["installed"] = if running {
                json!(installed.contains(&name))
            } else {
                Value::Null
            };
            it
        })
        .collect();
    Ok(Json(json!({
        "running": running,
        "catalog": catalog,
        "total_installed": if running { json!(installed.len()) } else { Value::Null },
    })))
}

fn url_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(r"^https://[A-Za-z0-9.\-_]+(:\d{2,5})?/[A-Za-z0-9.\-_/]*$")
            .expect("内置正则编译失败")
    })
}

async fn pip_mirror_of(state: &AppState, inst: &Instance) -> String {
    let out = exec_stdout(
        state,
        inst,
        vec![
            "sh".into(),
            "-c".into(),
            "cat /etc/pip.conf 2>/dev/null || true".into(),
        ],
    )
    .await
    .unwrap_or_default();
    for line in out.lines() {
        let ln = line.trim();
        if ln.to_lowercase().starts_with("index-url") {
            if let Some((_, v)) = ln.split_once('=') {
                return v.trim().to_string();
            }
        }
    }
    "https://pypi.org/simple".to_string()
}

async fn panel_pip_mirror_get(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
) -> AppResult<Json<Value>> {
    require_python(&inst)?;
    let mirrors = catalog::py_mirrors();
    let (running, _) = container_state(&state, inst.id).await;
    if !running {
        return Ok(Json(json!({"running": false, "current": Value::Null, "mirrors": mirrors})));
    }
    let cur = pip_mirror_of(&state, &inst).await;
    Ok(Json(json!({"running": true, "current": cur, "mirrors": mirrors})))
}

async fn panel_pip_mirror_put(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    require_python(&inst)?;
    let url = body.get("url").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
    if !url_re().is_match(&url) {
        return Err(AppError::bad_request("镜像地址格式不正确（需 https 链接）"));
    }
    let (running, _) = container_state(&state, inst.id).await;
    if !running {
        return Err(AppError::conflict("实例未运行，请先启动实例"));
    }
    let host = url
        .split_once("//")
        .map(|(_, r)| r)
        .unwrap_or("")
        .split('/')
        .next()
        .unwrap_or("")
        .to_string();
    let conf = format!("[global]\nindex-url = {url}\ntrusted-host = {host}\n");
    exec_stdout(
        &state,
        &inst,
        vec![
            "sh".into(),
            "-c".into(),
            format!("printf '{}' > /etc/pip.conf", conf.replace('\'', "")),
        ],
    )
    .await?;
    let named = catalog::mirror_name(catalog::py_mirrors(), &url);
    instance::log_op(&state.db, inst.id, "pip_mirror", &named).await?;
    Ok(Json(json!({"current": url})))
}

// =====================================================================
// PHP 扩展（php -m 检测 + pecl / 源码编译在线安装）
// =====================================================================

fn require_php(inst: &Instance) -> AppResult<()> {
    if !instance::is_php(&inst.image) {
        return Err(AppError::bad_request("该功能仅支持 PHP 环境"));
    }
    Ok(())
}

fn pecl_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"^[A-Za-z][A-Za-z0-9_\-]*$").expect("内置正则编译失败"))
}

fn func_name_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$").expect("内置正则编译失败"))
}

async fn panel_php_extensions(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
) -> AppResult<Json<Value>> {
    require_php(&inst)?;
    let (running, _cid) = container_state(&state, inst.id).await;
    let mut modules: Vec<String> = Vec::new();
    let mut zend: Vec<String> = Vec::new();
    let mut disabled: Vec<String> = Vec::new();

    if running {
        let out = exec_stdout(&state, &inst, vec!["php".into(), "-m".into()]).await?;
        let mut target = &mut modules;
        for line in out.lines() {
            let ln = line.trim();
            if ln.is_empty() {
                continue;
            }
            if ln.starts_with("[Zend Modules]") {
                target = &mut zend;
                continue;
            }
            target.push(ln.to_string());
        }
        disabled = instance::effective_disabled(&inst);
    }

    // 名字归一：php -m 的 Zend 段显示 "Zend OPcache"，目录名是 opcache
    let mut enabled: HashSet<String> = modules.iter().map(|m| m.to_lowercase()).collect();
    for z in &zend {
        enabled.insert(z.to_lowercase().trim_start_matches("zend ").to_string());
    }

    let list: Vec<Value> = catalog::php_ext()
        .into_iter()
        .map(|mut it| {
            let name = it.get("name").and_then(|v| v.as_str()).unwrap_or("").to_lowercase();
            it["enabled"] = if running {
                json!(enabled.contains(&name))
            } else {
                Value::Null
            };
            it
        })
        .collect();

    Ok(Json(json!({
        "running": running,
        "modules": modules,
        "zend": zend,
        "catalog": list,
        "disabled": disabled,
    })))
}

async fn panel_php_disabled_get(PanelInst(inst): PanelInst) -> AppResult<Json<Value>> {
    require_php(&inst)?;
    Ok(Json(json!({
        "catalog": catalog::php_funcs(),
        "disabled": instance::effective_disabled(&inst),
        "is_custom": !inst.disabled_funcs.is_empty(),
    })))
}

/// 写回禁用函数并重建容器（运行中则停 → 重建 → 起 → 诊断）。
async fn apply_disabled(
    state: &AppState,
    inst: &mut Instance,
    funcs: &[String],
) -> AppResult<bool> {
    // 禁用函数存独立字段（disabled_funcs 列），由容器入口动态注入 -d 参数；
    // 启动命令保持干净——这就是「PHP 不该有一长串启动命令」的语义保证。
    inst.disabled_funcs = funcs.join(",");
    let was_running = inst.status == "running";
    sqlx::query("UPDATE instances SET disabled_funcs = ? WHERE id = ?")
        .bind(&inst.disabled_funcs)
        .bind(inst.id)
        .execute(&state.db)
        .await?;
    if was_running {
        instance::stop_instance(state, inst).await?;
    }
    instance::recreate_container(state, inst).await?;
    if was_running {
        instance::start_instance(state, inst).await?;
        instance::diagnose_startup(state, inst).await?;
    }
    instance::persist_status(&state.db, inst).await?;
    Ok(was_running)
}

async fn panel_php_disabled_put(
    PanelInst(mut inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    require_php(&inst)?;
    ensure_no_running_job(&state, inst.id)?;
    let arr = body
        .get("functions")
        .and_then(|v| v.as_array())
        .ok_or_else(|| AppError::bad_request("参数格式错误"))?;
    let mut clean: Vec<String> = Vec::new();
    for it in arr {
        let f = it.as_str().unwrap_or("").trim().to_string();
        if f.is_empty() {
            continue;
        }
        if !func_name_re().is_match(&f) {
            return Err(AppError::bad_request(format!("非法函数名：{f}")));
        }
        if !clean.contains(&f) {
            clean.push(f);
        }
    }
    if clean.len() > 60 {
        return Err(AppError::bad_request("禁用函数数量过多"));
    }
    let restarted = apply_disabled(&state, &mut inst, &clean).await?;
    instance::log_op(&state.db, inst.id, "disabled_funcs", &format!("{} 个", clean.len())).await?;
    Ok(Json(json!({
        "disabled": clean,
        "restarted": restarted,
        "status": inst.status,
    })))
}

async fn panel_php_disabled_reset(
    PanelInst(mut inst): PanelInst,
    State(state): State<AppState>,
) -> AppResult<Json<Value>> {
    require_php(&inst)?;
    ensure_no_running_job(&state, inst.id)?;
    let default = instance::default_disabled_funcs();
    let restarted = apply_disabled(&state, &mut inst, &default).await?;
    instance::log_op(&state.db, inst.id, "disabled_funcs", "恢复节点默认").await?;
    Ok(Json(json!({
        "disabled": default,
        "restarted": restarted,
        "status": inst.status,
    })))
}

/// 扩展安装命令（core = docker-php-ext-install，pecl = pecl install + 启用）。
fn ext_install_cmd(
    pecl: &str,
    deps: &str,
    interactive: bool,
    core: bool,
    configure: &str,
    pre: &str,
) -> Vec<String> {
    let mut parts: Vec<String> = Vec::new();
    if !deps.is_empty() {
        parts.push(format!("apt-get update -qq && apt-get install -y -qq {deps}"));
    }
    if core {
        if !configure.is_empty() {
            parts.push(format!("docker-php-ext-configure {pecl} {configure}"));
        }
        parts.push(format!("docker-php-ext-install -j$(nproc) {pecl}"));
        return vec!["sh".into(), "-c".into(), parts.join(" && ")];
    }
    if !pre.is_empty() {
        parts.push(format!("(pecl install {pre} && docker-php-ext-enable {pre}) || true"));
    }
    // test 文件重定向到 /tmp：绕开 PEAR 向 /usr/local/lib/php/test 写入的权限问题
    parts.push("mkdir -p /tmp/pear-tests && pear config-set test_dir /tmp/pear-tests".to_string());
    // 已编译过（上次成功但未启用/被卸载）时跳过重编译，直接走到启用步骤
    let mut pecl_cmd = format!(
        "(pecl install {pecl} || {{ pecl list | grep -q {pecl} && \
         echo '[面板] 扩展已编译过，跳过重新编译' || exit 1; }}) \
         || docker-php-ext-install -j$(nproc) {pecl}"
    );
    if interactive {
        pecl_cmd = format!("printf '\\n\\n\\n\\n\\n\\n' | {pecl_cmd}");
    }
    parts.push(pecl_cmd);
    parts.push(format!("docker-php-ext-enable {pecl} 2>/dev/null || true"));
    vec!["sh".into(), "-c".into(), parts.join(" && ")]
}

/// 解密 loader（ionCube / SourceGuardian）：下载官方预编译包并按 PHP 版本放置 .so。
fn loader_install_cmd(kind: &str) -> AppResult<Vec<String>> {
    let script = match kind {
        "ioncube" => concat!(
            "cd /tmp && rm -rf ioncube loaders.tgz && ",
            "curl -fsSL https://downloads.ioncube.com/loader_downloads/ioncube_loaders_lin_x86-64.tar.gz -o loaders.tgz && ",
            "tar xzf loaders.tgz && ",
            "V=$(php -r 'echo PHP_MAJOR_VERSION.\".\".PHP_MINOR_VERSION;') && ",
            "SO=$(ls ioncube/ioncube_loader_lin_$V.so) && ",
            "D=$(php-config --extension-dir) && cp \"$SO\" \"$D/\" && ",
            "printf 'zend_extension=%s/ioncube_loader_lin_%s.so\\n' \"$D\" \"$V\" > /usr/local/etc/php/conf.d/01-ioncube.ini && ",
            "php -v | grep -qi ioncube && echo '[面板] ionCube Loader 已就位（重启实例后生效）'"
        ),
        "sg" => concat!(
            "cd /tmp && rm -rf sgloaders loaders.tgz && ",
            "curl -fsSL https://www.sourceguardian.com/loaders/download/loaders.linux-x86_64.tar.gz -o loaders.tgz && ",
            "mkdir sgloaders && tar xzf loaders.tgz -C sgloaders && ",
            "V=$(php -r 'echo PHP_MAJOR_VERSION.\".\".PHP_MINOR_VERSION;') && ",
            "SO=$(ls sgloaders/ixed.$V.lin) && ",
            "D=$(php-config --extension-dir) && cp \"$SO\" \"$D/\" && ",
            "printf 'zend_extension=%s/ixed.%s.lin\\n' \"$D\" \"$V\" > /usr/local/etc/php/conf.d/01-swoole_loader.ini && ",
            "php -v | grep -qi sourceguardian && echo '[面板] SourceGuardian Loader 已就位（重启实例后生效）'"
        ),
        other => return Err(AppError::internal(format!("未知 loader: {other}"))),
    };
    Ok(vec!["sh".into(), "-c".into(), script.to_string()])
}

fn sfield(v: &Value, k: &str) -> String {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string()
}

async fn panel_php_ext_install(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    require_php(&inst)?;
    ensure_no_running_job(&state, inst.id)?;
    let name = sfield(&body, "name").trim().to_string();
    if !pecl_re().is_match(&name) {
        return Err(AppError::bad_request("非法扩展名（示例：redis、xdebug、imagick）"));
    }
    let item = catalog::php_ext_by_name(&name);
    if item.as_ref().map(|i| i.get("builtin").and_then(|v| v.as_bool()) == Some(true)).unwrap_or(false) {
        return Err(AppError::bad_request(format!("{name} 为官方内置扩展，无需安装")));
    }
    if item.as_ref().map(|i| i.get("manual").and_then(|v| v.as_bool()) == Some(true)).unwrap_or(false) {
        return Err(AppError::bad_request(format!(
            "{name} 需管理员构建进镜像后使用，请联系管理员"
        )));
    }

    if let Some(loader) = item
        .as_ref()
        .and_then(|i| i.get("loader").and_then(|v| v.as_str()))
        .map(|s| s.to_string())
    {
        let cmd = loader_install_cmd(&loader)?;
        let job = instance::start_exec_job(&state, &inst, cmd, &format!("安装扩展 {name}")).await?;
        instance::log_op(&state.db, inst.id, "deps_install", &format!("loader:{loader}")).await?;
        return Ok(Json(json!({"job_id": job.id})));
    }

    let pecl = item
        .as_ref()
        .and_then(|i| i.get("pecl").and_then(|v| v.as_str()))
        .unwrap_or(&name)
        .to_string();
    let deps = item.as_ref().map(|i| sfield(i, "deps")).unwrap_or_default();
    let configure = item.as_ref().map(|i| sfield(i, "configure")).unwrap_or_default();
    let pre = item.as_ref().map(|i| sfield(i, "pre")).unwrap_or_default();
    let interactive = item
        .as_ref()
        .map(|i| i.get("interactive").and_then(|v| v.as_bool()) == Some(true))
        .unwrap_or(false);
    let core = item
        .as_ref()
        .map(|i| i.get("core").and_then(|v| v.as_bool()) == Some(true))
        .unwrap_or(false);

    let cmd = ext_install_cmd(&pecl, &deps, interactive, core, &configure, &pre);
    let job = instance::start_exec_job(&state, &inst, cmd, &format!("安装扩展 {name}")).await?;
    let kind = if core { "core" } else { "pecl" };
    instance::log_op(&state.db, inst.id, "deps_install", &format!("{kind}:{pecl}")).await?;
    Ok(Json(json!({"job_id": job.id})))
}

async fn panel_php_ext_uninstall(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    require_php(&inst)?;
    let name = sfield(&body, "name").trim().to_string();
    if !pecl_re().is_match(&name) {
        return Err(AppError::bad_request("非法扩展名"));
    }
    let item = catalog::php_ext_by_name(&name);
    let unsupported = item
        .as_ref()
        .map(|i| {
            i.get("builtin").and_then(|v| v.as_bool()) == Some(true)
                || i.get("manual").and_then(|v| v.as_bool()) == Some(true)
        })
        .unwrap_or(false);
    if unsupported {
        return Err(AppError::bad_request(format!("{name} 不支持在线卸载")));
    }
    let pecl = item
        .as_ref()
        .and_then(|i| i.get("pecl").and_then(|v| v.as_str()))
        .unwrap_or(&name)
        .to_string();
    let (running, _) = container_state(&state, inst.id).await;
    if !running {
        return Err(AppError::conflict("容器未运行，请先启动实例"));
    }
    // 卸载 = 移除 ini 停用（保留 .so，重装无需再编译）
    exec_stdout(
        &state,
        &inst,
        vec![
            "sh".into(),
            "-c".into(),
            format!(
                "rm -f /usr/local/etc/php/conf.d/docker-php-ext-{pecl}.ini \
                 /usr/local/etc/php/conf.d/*{pecl}*.ini && echo OK"
            ),
        ],
    )
    .await?;
    instance::log_op(&state.db, inst.id, "deps_uninstall", &format!("pecl:{pecl}")).await?;
    Ok(Json(json!({"detail": format!("{name} 已停用，重启实例后生效")})))
}

// =====================================================================
// 文件管理（root = 实例宿主机目录）
// =====================================================================

fn inst_root(inst: &Instance) -> String {
    file::ensure_root(&inst.host_dir);
    inst.host_dir.clone()
}

fn disposition(name: &str) -> String {
    let enc = urlencoding::encode(name);
    format!("attachment; filename=\"{enc}\"; filename*=UTF-8''{enc}")
}

fn mime_of(name: &str) -> &'static str {
    let ext = name.rsplit('.').next().unwrap_or("").to_ascii_lowercase();
    match ext.as_str() {
        "txt" | "log" | "md" | "ini" | "conf" => "text/plain; charset=utf-8",
        "json" => "application/json",
        "html" | "htm" => "text/html; charset=utf-8",
        "css" => "text/css",
        "js" => "application/javascript",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "svg" => "image/svg+xml",
        "pdf" => "application/pdf",
        "zip" => "application/zip",
        "gz" | "tgz" => "application/gzip",
        _ => "application/octet-stream",
    }
}

async fn stream_file(path: PathBuf, filename: String, ctype: &str) -> AppResult<Response> {
    let f = tokio::fs::File::open(&path)
        .await
        .map_err(|_| AppError::internal("文件读取失败"))?;
    let len = f.metadata().await.map(|m| m.len()).unwrap_or(0);
    let body = Body::from_stream(ReaderStream::new(f));
    Response::builder()
        .header(header::CONTENT_TYPE, ctype)
        .header(header::CONTENT_DISPOSITION, disposition(&filename))
        .header(header::CONTENT_LENGTH, len.to_string())
        .body(body)
        .map_err(|e| AppError::internal(format!("响应构造失败：{e}")))
}

async fn panel_files(
    PanelInst(inst): PanelInst,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let path = q.get("path").cloned().unwrap_or_else(|| "/".to_string());
    let entries = file::list_dir(&inst_root(&inst), &path)?;
    Ok(Json(json!({"path": path, "entries": entries})))
}

async fn panel_file_content(
    PanelInst(inst): PanelInst,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let path = q.get("path").cloned().unwrap_or_else(|| "/".to_string());
    let mut data = file::read_text(&inst_root(&inst), &path)?;
    data["path"] = json!(path);
    Ok(Json(data))
}

async fn panel_upload(
    PanelInst(inst): PanelInst,
    Query(q): Query<HashMap<String, String>>,
    mut multipart: Multipart,
) -> AppResult<Json<Value>> {
    let mut dir = q.get("path").cloned().unwrap_or_else(|| "/".to_string());
    let limit = (settings().upload_limit_mb * 1024 * 1024) as u64;
    let root = inst_root(&inst);
    let mut saved: Option<Value> = None;
    while let Some(mut field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::bad_request(format!("上传数据解析失败：{e}")))?
    {
        let fname = field.name().unwrap_or("").to_string();
        if fname == "path" {
            if let Ok(t) = field.text().await {
                if !t.trim().is_empty() {
                    dir = t;
                }
            }
            continue;
        }
        let file_name = field.file_name().map(|s| s.to_string()).unwrap_or_default();
        if saved.is_none() && !file_name.is_empty() {
            let mut data: Vec<u8> = Vec::new();
            while let Some(chunk) = field
                .chunk()
                .await
                .map_err(|e| AppError::bad_request(format!("上传数据解析失败：{e}")))?
            {
                data.extend_from_slice(&chunk);
                if data.len() as u64 > limit {
                    return Err(AppError::too_large(format!(
                        "文件超过上限 {}MB",
                        settings().upload_limit_mb
                    )));
                }
            }
            let r = root.clone();
            let d = dir.clone();
            saved = Some(
                crate::error::blocking(move || file::save_upload(&r, &d, &file_name, &data, limit))
                    .await?,
            );
        } else {
            let _ = field.bytes().await;
        }
    }
    match saved {
        Some(v) => Ok(Json(v)),
        None => Err(AppError::bad_request("未收到文件")),
    }
}

async fn panel_save(PanelInst(inst): PanelInst, Json(body): Json<Value>) -> AppResult<Json<Value>> {
    let path = body
        .get("path")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| AppError::bad_request("参数缺失"))?
        .to_string();
    let content = body.get("content").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let root = inst_root(&inst);
    crate::error::blocking(move || file::write_text(&root, &path, &content)).await?;
    Ok(Json(json!({"detail": "已保存"})))
}

async fn panel_mkdir(PanelInst(inst): PanelInst, Json(body): Json<Value>) -> AppResult<Json<Value>> {
    let path = body
        .get("path")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| AppError::bad_request("参数缺失"))?
        .to_string();
    let root = inst_root(&inst);
    crate::error::blocking(move || file::mkdir(&root, &path)).await?;
    Ok(Json(json!({"detail": "已创建"})))
}

async fn panel_rename(PanelInst(inst): PanelInst, Json(body): Json<Value>) -> AppResult<Json<Value>> {
    let src = sfield(&body, "src");
    let dst = sfield(&body, "dst");
    if src.is_empty() || dst.is_empty() {
        return Err(AppError::bad_request("参数缺失"));
    }
    let root = inst_root(&inst);
    crate::error::blocking(move || file::rename(&root, &src, &dst)).await?;
    Ok(Json(json!({"detail": "已重命名"})))
}

async fn panel_delete_path(
    PanelInst(inst): PanelInst,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let path = q.get("path").cloned().unwrap_or_default();
    let root = inst_root(&inst);
    crate::error::blocking(move || file::delete(&root, &path)).await?;
    Ok(Json(json!({"detail": "已删除"})))
}

async fn panel_download(
    PanelInst(inst): PanelInst,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Response> {
    let path = q.get("path").cloned().unwrap_or_default();
    let target = file::resolve_path(&inst_root(&inst), &path)?;
    if !target.is_file() {
        return Err(AppError::bad_request("仅支持下载文件"));
    }
    let name = target
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "download".to_string());
    let ctype = mime_of(&name);
    stream_file(target, name, ctype).await
}

async fn panel_backup(PanelInst(inst): PanelInst) -> AppResult<Response> {
    let root = inst_root(&inst);
    let tmp = file::create_backup(&root)?;
    let fname = format!(
        "instance_{}_backup_{}.tar.gz",
        inst.id,
        chrono::Local::now().format("%Y%m%d_%H%M%S")
    );
    // 响应发完后清理临时包（延迟删除，避免打断正在传输的流）
    let cleanup = tmp.clone();
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_secs(600)).await;
        let _ = tokio::fs::remove_file(&cleanup).await;
    });
    stream_file(tmp, fname, "application/gzip").await
}

async fn panel_restore(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    mut multipart: Multipart,
) -> AppResult<Json<Value>> {
    let mut data: Option<Vec<u8>> = None;
    let mut fname = String::new();
    while let Some(mut field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::bad_request(format!("上传数据解析失败：{e}")))?
    {
        if field.name().unwrap_or("") == "file" || !field.file_name().unwrap_or("").is_empty() {
            fname = field.file_name().unwrap_or("").to_string();
            let mut buf: Vec<u8> = Vec::new();
            while let Some(chunk) = field
                .chunk()
                .await
                .map_err(|e| AppError::bad_request(format!("上传数据解析失败：{e}")))?
            {
                buf.extend_from_slice(&chunk);
            }
            data = Some(buf);
            break;
        }
        let _ = field.bytes().await;
    }
    let bytes = data.ok_or_else(|| AppError::bad_request("未收到文件"))?;

    let mut inst2 = inst.clone();
    instance::sync_status(&state, &mut inst2).await;
    if inst2.status == "running" {
        return Err(AppError::conflict("实例运行中，请先停止后再恢复"));
    }
    let root = inst_root(&inst);
    let max = (inst.disk_quota.max(1) * 1024 * 1024) as u64;
    let out = crate::error::blocking(move || file::restore_backup(&root, &bytes, max)).await?;
    instance::log_op(&state.db, inst.id, "restore", &fname).await?;
    Ok(Json(out))
}

async fn panel_copy(PanelInst(inst): PanelInst, Json(body): Json<Value>) -> AppResult<Json<Value>> {
    let src = sfield(&body, "src");
    let dst = sfield(&body, "dst");
    if src.is_empty() || dst.is_empty() {
        return Err(AppError::bad_request("参数缺失"));
    }
    let root = inst_root(&inst);
    crate::error::blocking(move || file::copy_path(&root, &src, &dst)).await?;
    Ok(Json(json!({"detail": "已复制"})))
}

async fn panel_zip(PanelInst(inst): PanelInst, Json(body): Json<Value>) -> AppResult<Json<Value>> {
    let paths: Vec<String> = body
        .get("paths")
        .and_then(|v| v.as_array())
        .map(|a| a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect())
        .unwrap_or_default();
    let name = sfield(&body, "name");
    let z_type = body
        .get("z_type")
        .and_then(|v| v.as_str())
        .unwrap_or("tar.gz")
        .to_string();
    if paths.is_empty() || name.is_empty() {
        return Err(AppError::bad_request("参数缺失"));
    }
    let root = inst_root(&inst);
    crate::error::blocking(move || file::zip_entries(&root, &paths, &name, &z_type)).await?;
    Ok(Json(json!({"detail": "压缩完成"})))
}

async fn panel_unzip(PanelInst(inst): PanelInst, Json(body): Json<Value>) -> AppResult<Json<Value>> {
    let src = sfield(&body, "src");
    if src.is_empty() {
        return Err(AppError::bad_request("参数缺失"));
    }
    let root = inst_root(&inst);
    let dest = crate::error::blocking(move || file::unzip_entry(&root, &src)).await?;
    Ok(Json(json!({"detail": "解压完成", "dest": dest})))
}

async fn panel_path_size(
    PanelInst(inst): PanelInst,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let path = q.get("path").cloned().unwrap_or_default();
    let size = file::dir_size(&inst_root(&inst), &path)?;
    Ok(Json(json!({"size": size})))
}

async fn panel_preview(
    PanelInst(inst): PanelInst,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let path = q.get("path").cloned().unwrap_or_default();
    Ok(Json(file::image_preview(&inst_root(&inst), &path)?))
}

async fn panel_upload_exists(PanelInst(inst): PanelInst, Json(body): Json<Value>) -> AppResult<Json<Value>> {
    let file_name = body
        .get("file_name")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| AppError::bad_request("参数缺失"))?
        .to_string();
    let path = body.get("path").and_then(|v| v.as_str()).unwrap_or("/").to_string();
    let total = body.get("total_size").and_then(|v| v.as_i64()).unwrap_or(0);
    Ok(Json(file::upload_exists(&inst_root(&inst), &path, &file_name, total)?))
}

async fn panel_upload_chunk(
    PanelInst(inst): PanelInst,
    mut multipart: Multipart,
) -> AppResult<Json<Value>> {
    let mut path = "/".to_string();
    let mut file_name = String::new();
    let mut total_size: i64 = 0;
    let mut start: i64 = 0;
    let mut data: Option<axum::body::Bytes> = None;
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::bad_request(format!("上传数据解析失败：{e}")))?
    {
        match field.name().unwrap_or("") {
            "path" => {
                if let Ok(t) = field.text().await {
                    if !t.trim().is_empty() {
                        path = t;
                    }
                }
            }
            "file_name" => {
                if let Ok(t) = field.text().await {
                    file_name = t;
                }
            }
            "total_size" => {
                if let Ok(t) = field.text().await {
                    total_size = t.trim().parse().unwrap_or(0);
                }
            }
            "start" => {
                if let Ok(t) = field.text().await {
                    start = t.trim().parse().unwrap_or(0);
                }
            }
            "file" => {
                data = Some(
                    field
                        .bytes()
                        .await
                        .map_err(|e| AppError::bad_request(format!("上传数据解析失败：{e}")))?,
                );
            }
            _ => {
                let _ = field.bytes().await;
            }
        }
    }
    let d = data.ok_or_else(|| AppError::bad_request("未收到分片数据"))?;
    let root = inst_root(&inst);
    let out = crate::error::blocking(move || {
        file::upload_chunk(&root, &path, &file_name, total_size, start, &d)
    })
    .await?;
    Ok(Json(out))
}

// =====================================================================
// 路由表
// =====================================================================

/// 独立面板 REST 路由（挂在根下，前缀 /panel 已含在路径里）。
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/panel/login", post(panel_login))
        .route("/panel/instance", get(panel_instance).patch(panel_update))
        // 运行环境
        .route(
            "/panel/instance/runtime",
            get(panel_runtime).post(panel_switch_runtime),
        )
        // 数据库
        .route(
            "/panel/instance/database",
            get(panel_database).post(panel_db_create).delete(panel_db_drop),
        )
        .route(
            "/panel/instance/database/reset-password",
            post(panel_db_reset_password),
        )
        .route("/panel/instance/database/switch", post(panel_db_switch))
        // 观测
        .route("/panel/instance/metrics", get(panel_metrics))
        .route("/panel/instance/ops", get(panel_ops_log))
        .route("/panel/instance/stats", get(panel_stats))
        .route("/panel/instance/logs", get(panel_logs))
        // 域名
        .route(
            "/panel/instance/domain",
            get(panel_get_domain).put(panel_set_domain),
        )
        .route("/panel/instance/domain/log", get(panel_domain_log))
        // 依赖
        .route("/panel/instance/deps/install", post(panel_install))
        .route("/panel/instance/deps/install-pkgs", post(panel_install_pkgs))
        .route("/panel/instance/deps/uninstall", post(panel_uninstall_pkgs))
        .route("/panel/instance/deps/installed", get(panel_deps_installed))
        .route("/panel/instance/py/packages", get(panel_py_packages))
        .route(
            "/panel/instance/pip/mirror",
            get(panel_pip_mirror_get).put(panel_pip_mirror_put),
        )
        // PHP 扩展 / 禁用函数
        .route("/panel/instance/php/extensions", get(panel_php_extensions))
        .route(
            "/panel/instance/php/extensions/install",
            post(panel_php_ext_install),
        )
        .route(
            "/panel/instance/php/extensions/uninstall",
            post(panel_php_ext_uninstall),
        )
        .route(
            "/panel/instance/php/disabled",
            get(panel_php_disabled_get).put(panel_php_disabled_put),
        )
        .route(
            "/panel/instance/php/disabled/reset",
            post(panel_php_disabled_reset),
        )
        // 文件
        .route(
            "/panel/instance/files",
            get(panel_files).delete(panel_delete_path),
        )
        .route("/panel/instance/files/content", get(panel_file_content))
        .route("/panel/instance/files/upload", put(panel_upload))
        .route("/panel/instance/files/save", post(panel_save))
        .route("/panel/instance/files/mkdir", post(panel_mkdir))
        .route("/panel/instance/files/rename", post(panel_rename))
        .route("/panel/instance/files/download", get(panel_download))
        .route("/panel/instance/files/backup", get(panel_backup))
        .route("/panel/instance/files/restore", post(panel_restore))
        .route("/panel/instance/files/copy", post(panel_copy))
        .route("/panel/instance/files/zip", post(panel_zip))
        .route("/panel/instance/files/unzip", post(panel_unzip))
        .route("/panel/instance/files/path-size", get(panel_path_size))
        .route("/panel/instance/files/preview", get(panel_preview))
        .route(
            "/panel/instance/files/upload-exists",
            post(panel_upload_exists),
        )
        .route(
            "/panel/instance/files/upload-chunk",
            post(panel_upload_chunk),
        )
        // 通配动作放最后（start / stop / restart）
        .route("/panel/instance/{action}", post(panel_action))
}
