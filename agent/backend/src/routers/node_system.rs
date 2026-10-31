//! 宿主系统运维接口：系统概览、进程、systemd 服务、监听端口、系统日志、登录审计。
//!
//! 鉴权与其它节点接口一致（`X-Node-Token`），由主控后台经 `/api/host/*?node=<id>` 转发。
//! 本模块只做「机器级」的读与受控写（服务的启停/自启），不触碰实例目录与容器生命周期。

use axum::{
    extract::Query,
    routing::{get, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::Value;

use crate::auth::NodeAuth;
use crate::error::{AppError, AppResult};
use crate::services::host_info;
use crate::state::AppState;

#[derive(Debug, Deserialize, Default)]
struct ProcQuery {
    #[serde(default)]
    sort: Option<String>,
    #[serde(default)]
    limit: Option<usize>,
}

async fn system(_a: NodeAuth) -> Json<Value> {
    Json(host_info::system().await)
}

async fn processes(_a: NodeAuth, Query(q): Query<ProcQuery>) -> Json<Value> {
    let sort = q.sort.unwrap_or_else(|| "cpu".to_string());
    let limit = q.limit.unwrap_or(30).clamp(1, 200);
    Json(host_info::processes(&sort, limit).await)
}

async fn services(_a: NodeAuth) -> Json<Value> {
    Json(host_info::services().await)
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

async fn ports(_a: NodeAuth) -> Json<Value> {
    Json(host_info::ports().await)
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
    Json(host_info::journal(q.unit.as_deref(), n).await)
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

pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/host/system", get(system))
        .route("/host/processes", get(processes))
        .route("/host/services", get(services))
        .route("/host/services/action", post(service_action))
        .route("/host/ports", get(ports))
        .route("/host/ports/kill", post(port_kill))
        .route("/host/mem/cleanup", post(mem_cleanup))
        .route("/host/journal", get(journal))
        .route("/host/users", get(users))
        .route("/host/ssh-logins", get(ssh_logins))
}
