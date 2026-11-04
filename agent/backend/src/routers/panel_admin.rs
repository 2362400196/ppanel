//! 独立面板本地管理 API：/panel-admin/*
//!
//! 让独立面板脱离主控也能独立运转——创建/删除/启停/重置实例、签发面板令牌，
//! 全部由本机管理员会话完成。与主控编排入口（/agent/*，X-Node-Token）共用同一套
//! 实例服务层（provision / instance），区别只在鉴权面与实例来源标记：
//! - 本地创建的实例 source='local'，只出现在本机管理面；
//! - 主控创建的实例 source='master'（默认），主控照常编排，本地管理面同样可见可管。
//!
//! 管理员口令：AGENT_ADMIN_PASSWORD 环境变量，未配置则首次启动自动生成并打印到日志
//! （见 db::ensure_local_admin）。

use axum::{
    extract::{Path, Query, State},
    routing::{get, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::auth::LocalAdmin;
use crate::error::{AppError, AppResult};
use crate::services::{instance, provision};
use crate::state::AppState;

// ---------- 登录 ----------

#[derive(Debug, Deserialize)]
struct LoginIn {
    password: String,
}

async fn login(State(state): State<AppState>, Json(body): Json<LoginIn>) -> AppResult<Json<Value>> {
    let row: Option<(String,)> =
        sqlx::query_as("SELECT password_hash FROM users WHERE username = '__local_admin__'")
            .fetch_optional(&state.db)
            .await?;
    let Some((hash,)) = row else {
        return Err(AppError::unauthorized("本地管理员未初始化"));
    };
    // 统一错误信息，不区分"口令错误/未初始化"，避免探测
    if !crate::auth::verify_password(body.password.trim(), &hash) {
        return Err(AppError::unauthorized("口令错误"));
    }
    Ok(Json(json!({
        "token": crate::auth::create_admin_token(),
    })))
}

// ---------- 实例列表 / 创建 ----------

async fn list_instances(_a: LocalAdmin, State(state): State<AppState>) -> AppResult<Json<Value>> {
    let mut rows: Vec<crate::models::Instance> =
        sqlx::query_as("SELECT * FROM instances ORDER BY id DESC")
            .fetch_all(&state.db)
            .await?;
    let mut out: Vec<Value> = Vec::new();
    for inst in rows.iter_mut() {
        let started = instance::sync_status(&state, inst).await;
        let _ = instance::persist_status(&state.db, inst).await;
        let mut data = inst.out_json();
        data["started_at"] = json!(started);
        data["has_token"] = json!(inst.panel_token.as_deref().map(|t| !t.is_empty()).unwrap_or(false));
        out.push(data);
    }
    Ok(Json(Value::Array(out)))
}

#[derive(Debug, Deserialize)]
struct InstanceCreate {
    name: String,
    image: String,
    #[serde(default)]
    cpu_limit: f64,
    #[serde(default)]
    mem_limit: i64,
    #[serde(default)]
    disk_quota: i64,
    #[serde(default)]
    expire_at: Option<String>,
    /// 创建的同时签发面板令牌（本地开通常用：拿到令牌直接进面板）
    #[serde(default)]
    issue_token: bool,
}

async fn create_instance(
    _a: LocalAdmin,
    State(state): State<AppState>,
    Json(body): Json<InstanceCreate>,
) -> AppResult<Json<Value>> {
    let name = body.name.trim().to_string();
    if name.chars().count() < 1 || name.chars().count() > 64 {
        return Err(AppError::new(422, "实例名称长度需为 1-64 字符"));
    }
    let cpu = if body.cpu_limit > 0.0 { body.cpu_limit } else { 1.0 };
    let mem = if body.mem_limit > 0 { body.mem_limit } else { 512 };
    let disk = if body.disk_quota > 0 { body.disk_quota } else { 2048 };
    if !(0.1..=16.0).contains(&cpu) {
        return Err(AppError::new(422, "CPU 限制需在 0.1-16 之间"));
    }
    if !(64..=32768).contains(&mem) {
        return Err(AppError::new(422, "内存限制需在 64-32768 MB 之间"));
    }
    if !(256..=1048576).contains(&disk) {
        return Err(AppError::new(422, "磁盘配额需在 256-1048576 MB 之间"));
    }
    if !instance::allowed_images(&state).await.contains(&body.image) {
        return Err(AppError::bad_request("不支持的镜像版本"));
    }
    let expire_at = match body.expire_at.as_deref() {
        Some(s) if !s.trim().is_empty() => match parse_dt(s) {
            Some(v) => Some(v),
            None => return Err(AppError::new(422, "到期时间格式无效")),
        },
        _ => None,
    };
    let req = provision::ProvisionReq {
        user_id: 1, // Agent 本地库占位账号
        name,
        image: body.image,
        start_cmd: String::new(), // 空 = 按镜像取默认入口命令
        cpu,
        mem,
        disk_quota: disk,
        note: String::new(),
        owner_ref: String::new(),
        expire_at,
        traffic_gb: None,
        issue_panel_token: body.issue_token,
    };
    let mut inst = provision::create_instance(&state, req, "本地创建实例").await?;
    // 标记来源：local = 独立面板本地创建，不落主控库（主控不可见）
    sqlx::query("UPDATE instances SET source = 'local' WHERE id = ?")
        .bind(inst.id)
        .execute(&state.db)
        .await?;
    inst.source = Some("local".to_string());
    let mut out = inst.out_json();
    if body.issue_token {
        out["panel_token"] = json!(inst.panel_token.clone().unwrap_or_default());
    }
    Ok(Json(out))
}

fn parse_dt(s: &str) -> Option<chrono::NaiveDateTime> {
    let s = s.trim();
    if s.is_empty() {
        return None;
    }
    if let Ok(v) = chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S%.f") {
        return Some(v);
    }
    if let Ok(v) = chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S") {
        return Some(v);
    }
    if let Ok(v) = chrono::NaiveDate::parse_from_str(s, "%Y-%m-%d") {
        return Some(v.and_hms_opt(0, 0, 0)?);
    }
    if let Ok(v) = chrono::DateTime::parse_from_rfc3339(s) {
        return Some(v.naive_utc());
    }
    None
}

// ---------- 生命周期动作 ----------

#[derive(Debug, Deserialize)]
struct ActionIn {
    action: String,
}

async fn instance_action(
    _a: LocalAdmin,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Json(body): Json<ActionIn>,
) -> AppResult<Json<Value>> {
    let mut inst = instance::fetch_instance(&state.db, id).await?;
    let status = match body.action.as_str() {
        "start" => {
            instance::start_instance(&state, &mut inst).await?;
            instance::diagnose_startup(&state, &inst).await?;
            "running".to_string()
        }
        "stop" => {
            instance::stop_instance(&state, &mut inst).await?;
            inst.status.clone()
        }
        "restart" => {
            instance::restart_instance(&state, &mut inst).await?;
            "running".to_string()
        }
        "reset" => {
            instance::reset_instance(&state, &mut inst).await?;
            inst.status.clone()
        }
        _ => return Err(AppError::bad_request("action 仅支持 start/stop/restart/reset")),
    };
    instance::persist_status(&state.db, &inst).await?;
    Ok(Json(json!({"status": status})))
}

#[derive(Debug, Deserialize, Default)]
struct PurgeQ {
    #[serde(default)]
    purge: i64,
}

async fn delete_instance(
    _a: LocalAdmin,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Query(q): Query<PurgeQ>,
) -> AppResult<Json<Value>> {
    let inst = instance::fetch_instance(&state.db, id).await?;
    let host_dir = inst.host_dir.clone();
    let purge = q.purge != 0 && !host_dir.is_empty();
    if purge {
        // 安全网：删除前先快照数据目录，失败则整体中止（什么都不删，可重试）
        instance::snapshot_host_dir(&inst).await?;
    }
    let _ = crate::services::mysql::drop_instance_db(&state.db, id).await;
    instance::remove_container(&state, &inst).await?;
    sqlx::query("DELETE FROM instances WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await?;
    let purged = purge;
    if purged {
        let _ = std::fs::remove_dir_all(&host_dir);
    }
    Ok(Json(json!({
        "detail": "实例已回收",
        "host_dir": host_dir,
        "purged": purged,
    })))
}

// ---------- 改名 ----------

#[derive(Debug, Deserialize)]
struct RenameIn {
    name: String,
}

async fn rename_instance(
    _a: LocalAdmin,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Json(body): Json<RenameIn>,
) -> AppResult<Json<Value>> {
    let name = body.name.trim().to_string();
    if name.is_empty() || name.chars().count() > 64 {
        return Err(AppError::bad_request("名称需为 1-64 个字符"));
    }
    let r = sqlx::query("UPDATE instances SET name = ? WHERE id = ?")
        .bind(&name)
        .bind(id)
        .execute(&state.db)
        .await?;
    if r.rows_affected() == 0 {
        return Err(AppError::not_found("实例不存在"));
    }
    Ok(Json(json!({"detail": "已改名", "name": name})))
}

// ---------- 面板令牌 ----------

#[derive(Debug, Deserialize, Default)]
struct TokenQ {
    #[serde(default)]
    reset: i64,
}

async fn instance_token(
    _a: LocalAdmin,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Query(q): Query<TokenQ>,
) -> AppResult<Json<Value>> {
    let inst = instance::fetch_instance(&state.db, id).await?;
    let token = if q.reset != 0 || inst.panel_token.as_deref().map(|t| t.is_empty()).unwrap_or(true) {
        provision::reset_panel_token(&state.db, id).await?
    } else {
        inst.panel_token.clone().unwrap_or_default()
    };
    Ok(Json(json!({
        "panel_token": token,
        "panel_url": "/panel",
        "reset": q.reset != 0,
    })))
}

// ---------- 镜像清单 ----------

async fn list_images(_a: LocalAdmin, State(state): State<AppState>) -> AppResult<Json<Value>> {
    let images = instance::allowed_images(&state).await;
    Ok(Json(json!({ "images": images })))
}

pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/panel-admin/login", post(login))
        .route("/panel-admin/instances", get(list_instances).post(create_instance))
        .route("/panel-admin/instances/{id}/action", post(instance_action))
        .route(
            "/panel-admin/instances/{id}/token",
            get(instance_token).post(instance_token),
        )
        .route(
            "/panel-admin/instances/{id}",
            axum::routing::patch(rename_instance).delete(delete_instance),
        )
        .route("/panel-admin/images", get(list_images))
}
