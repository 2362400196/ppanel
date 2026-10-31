//! 宿主机安全防护路由（/agent/security）：fail2ban SSH 防爆破 + 防火墙端口策略。
//!
//! 鉴权：X-Node-Token（主控）或 X-API-Key（OPEN_API_KEY / PANEL_API_KEY 开放对接），
//! 对应 Python 的 `require_node_or_api`（NodeOrApi 提取器）。
//! 端点、请求字段、响应字段名与错误文案全部对齐 app/routers/security.py。

use axum::extract::State;
use axum::http::HeaderMap;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::auth::NodeOrApi;
use crate::error::AppResult;
use crate::models::PortRule;
use crate::services::host_ops;
use crate::state::AppState;

#[derive(Debug, Deserialize)]
struct ToggleIn {
    enable: bool,
}

#[derive(Debug, Deserialize)]
struct IpIn {
    ip: String,
}

/// 防火墙规则删除（ufw 用 nums/num，firewalld 用 port/proto）。
#[derive(Debug, Deserialize)]
struct RuleDeleteIn {
    #[serde(default)]
    nums: Vec<i64>,
    #[serde(default)]
    num: i64,
    #[serde(default)]
    port: String,
    #[serde(default = "default_proto")]
    proto: String,
}

fn default_proto() -> String {
    "tcp".to_string()
}

// ---------- fail2ban（SSH 防爆破） ----------

async fn fail2ban_status(_auth: NodeOrApi) -> Json<Value> {
    Json(host_ops::fail2ban_status().await)
}

async fn fail2ban_setup(
    _auth: NodeOrApi,
    State(state): State<AppState>,
    headers: HeaderMap,
) -> AppResult<Json<Value>> {
    let tid = headers
        .get("x-task-id")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    Ok(Json(host_ops::fail2ban_setup(&state, &tid).await?))
}

async fn fail2ban_toggle(_auth: NodeOrApi, Json(body): Json<ToggleIn>) -> AppResult<Json<Value>> {
    if !host_ops::f2b_installed() {
        return Err(crate::error::AppError::bad_request(
            "fail2ban 未安装，请先执行安装/启用",
        ));
    }
    let action = if body.enable { "enable" } else { "disable" };
    let (rc, out) = host_ops::run(&["systemctl", action, "--now", "fail2ban"], 30).await;
    if rc != 0 {
        return Err(crate::error::AppError::bad_gateway(detail_or(&out, "执行失败")));
    }
    Ok(Json(host_ops::fail2ban_status().await))
}

async fn fail2ban_ban(_auth: NodeOrApi, Json(body): Json<IpIn>) -> AppResult<Json<Value>> {
    host_ops::check_ip(&body.ip)?;
    if !host_ops::f2b_active().await {
        return Err(crate::error::AppError::bad_request("fail2ban 服务未运行"));
    }
    let (rc, out) = host_ops::f2b(&["set", "sshd", "banip", &body.ip], 10).await;
    if rc != 0 {
        return Err(crate::error::AppError::bad_gateway(detail_or(&out, "封禁失败")));
    }
    Ok(Json(json!({"ok": true, "detail": format!("已封禁 {}", body.ip)})))
}

async fn fail2ban_unban(_auth: NodeOrApi, Json(body): Json<IpIn>) -> AppResult<Json<Value>> {
    host_ops::check_ip(&body.ip)?;
    if !host_ops::f2b_active().await {
        return Err(crate::error::AppError::bad_request("fail2ban 服务未运行"));
    }
    let (rc, out) = host_ops::f2b(&["set", "sshd", "unbanip", &body.ip], 10).await;
    if rc != 0 {
        return Err(crate::error::AppError::bad_gateway(detail_or(&out, "解封失败")));
    }
    Ok(Json(json!({"ok": true, "detail": format!("已解封 {}", body.ip)})))
}

/// `(out or fallback).strip()[:300]`
fn detail_or(out: &str, fallback: &str) -> String {
    let base = if out.is_empty() { fallback } else { out };
    base.trim().chars().take(300).collect()
}

// ---------- 防火墙（复用 host_ops 的 ufw/firewalld 实现） ----------

async fn firewall_status(_auth: NodeOrApi) -> Json<Value> {
    Json(host_ops::firewall_status().await)
}

async fn firewall_allow(_auth: NodeOrApi, Json(body): Json<PortRule>) -> AppResult<Json<Value>> {
    Ok(Json(host_ops::firewall_allow(&body.port, &body.proto).await?))
}

async fn firewall_deny(_auth: NodeOrApi, Json(body): Json<PortRule>) -> AppResult<Json<Value>> {
    Ok(Json(host_ops::firewall_deny(&body.port, &body.proto).await?))
}

async fn firewall_delete(
    _auth: NodeOrApi,
    Json(body): Json<RuleDeleteIn>,
) -> AppResult<Json<Value>> {
    Ok(Json(
        host_ops::firewall_delete(body.nums, body.num, &body.port, &body.proto).await?,
    ))
}

async fn firewall_toggle(_auth: NodeOrApi, Json(body): Json<ToggleIn>) -> AppResult<Json<Value>> {
    Ok(Json(host_ops::firewall_toggle(body.enable).await?))
}

pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/security/fail2ban", get(fail2ban_status))
        .route("/security/fail2ban/setup", post(fail2ban_setup))
        .route("/security/fail2ban/toggle", post(fail2ban_toggle))
        .route("/security/fail2ban/ban", post(fail2ban_ban))
        .route("/security/fail2ban/unban", post(fail2ban_unban))
        .route("/security/firewall", get(firewall_status))
        .route("/security/firewall/allow", post(firewall_allow))
        .route("/security/firewall/deny", post(firewall_deny))
        .route("/security/firewall/delete", post(firewall_delete))
        .route("/security/firewall/toggle", post(firewall_toggle))
}
