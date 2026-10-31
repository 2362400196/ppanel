//! 被控 Agent 节点管理路由（移植 app/routers/agent.py + app/routers/docker_admin.py）：
//! 心跳、实例生命周期、实例文件、宿主机防火墙，以及 Docker 运维（/docker/*）。
//! 前缀由挂载方提供（最终挂到 /agent 下）。

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::task::{Context, Poll};
use std::time::Duration;

use axum::body::{Body, Bytes};
use axum::extract::multipart::Field;
use axum::extract::{Multipart, Path, Query, State};
use axum::http::{header, HeaderMap};
use axum::response::Response;
use axum::routing::{delete, get, post, put};
use axum::{Json, Router};
use bollard::container::{
    ListContainersOptions, LogsOptions, RemoveContainerOptions, StopContainerOptions,
};
use bollard::image::{CreateImageOptions, PruneImagesOptions, RemoveImageOptions};
use bollard::Docker;
use chrono::TimeZone;
use futures_util::StreamExt;
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio_util::io::ReaderStream;

use crate::auth::NodeAuth;
use crate::config::settings;
use crate::error::{AppError, AppResult};
use crate::models::PortRule;
use crate::services::{file, host_ops, instance, mysql, provision};
use crate::state::AppState;

// ---------- 通用工具 ----------

fn tid_of(h: &HeaderMap) -> String {
    h.get("x-task-id")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string()
}

fn is_404(e: &bollard::errors::Error) -> bool {
    matches!(
        e,
        bollard::errors::Error::DockerResponseServerError { status_code: 404, .. }
    )
}

fn docker_fail(e: bollard::errors::Error) -> AppError {
    AppError::bad_gateway(format!("Docker 调用失败：{e}"))
}

fn jstr(v: &Value, k: &str) -> String {
    v.get(k)
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .to_string()
}

fn jint(v: &Value, k: &str) -> i64 {
    v.get(k)
        .and_then(|x| x.as_i64())
        .or_else(|| v.get(k).and_then(|x| x.as_f64()).map(|f| f as i64))
        .unwrap_or(0)
}

fn fmt_size(n: i64) -> String {
    if n >= 1024i64.pow(3) {
        format!("{:.2} GB", n as f64 / 1024f64.powi(3))
    } else {
        format!("{:.1} MB", n as f64 / 1024f64.powi(2))
    }
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

fn disposition(name: &str) -> String {
    let enc = urlencoding::encode(name);
    format!("attachment; filename=\"{enc}\"; filename*=UTF-8''{enc}")
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

/// 临时文件流：响应发送完毕（Drop）即清理备份包。
struct TempFileStream {
    inner: ReaderStream<tokio::fs::File>,
    path: PathBuf,
}

impl futures_util::Stream for TempFileStream {
    type Item = Result<Bytes, std::io::Error>;

    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let this = self.as_mut().get_mut();
        Pin::new(&mut this.inner).poll_next(cx)
    }
}

impl Drop for TempFileStream {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}

// ---------- 心跳 ----------

async fn health(_a: NodeAuth, State(state): State<AppState>) -> AppResult<Json<Value>> {
    let docker = state.docker.try_get().await;
    let mut out = serde_json::Map::new();
    out.insert("docker_ok".into(), json!(docker.is_some()));
    if let Some(d) = docker {
        match (d.info().await, d.version().await) {
            (Ok(info), Ok(ver)) => {
                let i = serde_json::to_value(info).unwrap_or(Value::Null);
                let v = serde_json::to_value(ver).unwrap_or(Value::Null);
                out.insert("server_version".into(), v.get("Version").cloned().unwrap_or(Value::Null));
                out.insert("os".into(), i.get("OperatingSystem").cloned().unwrap_or(Value::Null));
                out.insert("cpus".into(), i.get("NCPU").cloned().unwrap_or(Value::Null));
                out.insert(
                    "mem_total_gb".into(),
                    json!(crate::models::round1(jint(&i, "MemTotal") as f64 / 1024f64.powi(3))),
                );
                out.insert(
                    "containers_running".into(),
                    json!(jint(&i, "ContainersRunning")),
                );
                out.insert("images".into(), json!(jint(&i, "Images")));
            }
            _ => {
                out.insert("docker_ok".into(), json!(false));
            }
        }
    }
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM instances")
        .fetch_one(&state.db)
        .await?;
    out.insert("instances".into(), json!(count));
    let s = settings();
    out.insert("port_start".into(), json!(s.port_start));
    out.insert("port_end".into(), json!(s.port_end));
    out.insert("python_images".into(), json!(s.python_images));
    out.insert("php_images".into(), json!(s.php_images));
    Ok(Json(Value::Object(out)))
}

// ---------- 实例生命周期 ----------

#[derive(Debug, Deserialize)]
struct InstanceCreate {
    name: String,
    image: String,
    #[serde(default = "d_cpu")]
    cpu_limit: f64,
    #[serde(default = "d_mem")]
    mem_limit: i64,
    #[serde(default = "d_disk")]
    disk_quota: i64,
    #[serde(default)]
    start_cmd: String,
    #[serde(default)]
    expire_at: Option<String>,
    #[serde(default)]
    traffic_gb: Option<f64>,
}

fn d_cpu() -> f64 {
    1.0
}
fn d_mem() -> i64 {
    512
}
fn d_disk() -> i64 {
    2048
}

/// 解析 pydantic 风格 ISO 时间（可带时区/Z，统一转 UTC naive）。
fn parse_dt(raw: &str) -> Option<chrono::NaiveDateTime> {
    let s = raw.trim();
    if s.is_empty() {
        return None;
    }
    if let Ok(v) = chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S%.f") {
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

async fn create_instance(
    _a: NodeAuth,
    State(state): State<AppState>,
    Json(body): Json<InstanceCreate>,
) -> AppResult<Json<Value>> {
    // pydantic Field 约束（错误体为字符串，Python 为数组，见差异说明）
    if body.name.chars().count() < 1 || body.name.chars().count() > 64 {
        return Err(AppError::new(422, "实例名称长度需为 1-64 字符"));
    }
    if !(0.1..=16.0).contains(&body.cpu_limit) {
        return Err(AppError::new(422, "CPU 限制需在 0.1-16 之间"));
    }
    if !(64..=32768).contains(&body.mem_limit) {
        return Err(AppError::new(422, "内存限制需在 64-32768 MB 之间"));
    }
    if !(256..=1048576).contains(&body.disk_quota) {
        return Err(AppError::new(422, "磁盘配额需在 256-1048576 MB 之间"));
    }
    if body.start_cmd.chars().count() > 512 {
        return Err(AppError::new(422, "启动命令最长 512 字符"));
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
        user_id: 1, // Agent 本地库占位账号（Agent 无用户概念）
        name: body.name.clone(),
        image: body.image.clone(),
        start_cmd: body.start_cmd.clone(),
        cpu: body.cpu_limit,
        mem: body.mem_limit,
        disk_quota: body.disk_quota,
        note: String::new(),
        owner_ref: String::new(),
        expire_at,
        traffic_gb: body.traffic_gb,
        issue_panel_token: false,
    };
    let inst = provision::create_instance(&state, req, "创建实例").await?;
    Ok(Json(inst.out_json()))
}

async fn list_instances(_a: NodeAuth, State(state): State<AppState>) -> AppResult<Json<Value>> {
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
        out.push(data);
    }
    Ok(Json(Value::Array(out)))
}

async fn instance_detail(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
) -> AppResult<Json<Value>> {
    let mut inst = instance::fetch_instance(&state.db, id).await?;
    let started = instance::sync_status(&state, &mut inst).await;
    let _ = instance::persist_status(&state.db, &inst).await;
    Ok(Json(inst.detail_json(started)))
}

#[derive(Debug, Deserialize, Default)]
struct PurgeQ {
    #[serde(default)]
    purge: i64,
}

async fn delete_instance(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Query(q): Query<PurgeQ>,
) -> AppResult<Json<Value>> {
    let inst = instance::fetch_instance(&state.db, id).await?;
    let host_dir = inst.host_dir.clone();
    // 联动回收实例数据库（与面板删除路径一致，失败不阻塞）
    let _ = mysql::drop_instance_db(&state.db, id).await;
    instance::remove_container(&state, &inst).await?;
    sqlx::query("DELETE FROM instances WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await?;
    let purged = q.purge != 0 && !host_dir.is_empty();
    if purged {
        let _ = std::fs::remove_dir_all(&host_dir);
    }
    Ok(Json(json!({
        "detail": "实例已回收",
        "host_dir": host_dir,
        "purged": purged,
    })))
}

async fn instance_start(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
) -> AppResult<Json<Value>> {
    let mut inst = instance::fetch_instance(&state.db, id).await?;
    instance::start_instance(&state, &mut inst).await?;
    instance::persist_status(&state.db, &inst).await?;
    instance::diagnose_startup(&state, &inst).await?;
    Ok(Json(json!({"status": "running"})))
}

async fn instance_stop(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
) -> AppResult<Json<Value>> {
    let mut inst = instance::fetch_instance(&state.db, id).await?;
    instance::stop_instance(&state, &mut inst).await?;
    instance::persist_status(&state.db, &inst).await?;
    Ok(Json(json!({"status": inst.status})))
}

async fn instance_restart(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
) -> AppResult<Json<Value>> {
    let mut inst = instance::fetch_instance(&state.db, id).await?;
    instance::restart_instance(&state, &mut inst).await?;
    instance::persist_status(&state.db, &inst).await?;
    Ok(Json(json!({"status": inst.status})))
}

/// 一键重置：清空数据目录并重建容器，恢复到刚创建的样子（规格/端口/令牌不变）。
async fn instance_reset(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
) -> AppResult<Json<Value>> {
    let mut inst = instance::fetch_instance(&state.db, id).await?;
    let data = instance::reset_instance(&state, &mut inst).await?;
    Ok(Json(data))
}

async fn instance_runtime(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
) -> AppResult<Json<Value>> {
    let inst = instance::fetch_instance(&state.db, id).await?;
    Ok(Json(instance::runtime_versions(&state, &inst).await?))
}

#[derive(Debug, Deserialize)]
struct RuntimeIn {
    image: String,
}

async fn instance_switch_runtime(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Json(body): Json<RuntimeIn>,
) -> AppResult<Json<Value>> {
    let inst = instance::fetch_instance(&state.db, id).await?;
    Ok(Json(instance::switch_runtime(&state, &inst, &body.image).await?))
}

#[derive(Debug, Deserialize, Default)]
struct InstanceUpdate {
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    start_cmd: Option<String>,
    #[serde(default)]
    note: Option<String>,
    // 规格调整（升配）：改 CPU/内存需重建容器，磁盘只改配额数字；运行中拒绝
    #[serde(default)]
    cpu_limit: Option<f64>,
    #[serde(default)]
    mem_limit: Option<i64>,
    #[serde(default)]
    disk_quota: Option<i64>,
}

async fn instance_update(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Json(body): Json<InstanceUpdate>,
) -> AppResult<Json<Value>> {
    let mut inst = instance::fetch_instance(&state.db, id).await?;
    if let Some(name) = body.name.as_deref() {
        let n = name.trim().to_string();
        if n.is_empty() || n.chars().count() > 64 {
            return Err(AppError::new(422, "实例名称长度需为 1-64 字符"));
        }
        inst.name = n;
    }
    if let Some(note) = body.note.as_deref() {
        if note.chars().count() > 255 {
            return Err(AppError::new(422, "备注最长 255 字符"));
        }
        inst.note = note.to_string();
    }
    let mut rebuilt = false;
    if let Some(cmd) = body.start_cmd.as_deref() {
        let c = cmd.trim().to_string();
        // 空 = 恢复平台默认（按入口文件判断），允许
        if c.chars().count() > 512 {
            return Err(AppError::new(422, "启动命令长度需为 0-512 字符"));
        }
        if c != inst.start_cmd {
            inst.start_cmd = c;
            // 命令烧进容器，需要重建（与 Python 一致：不额外改 status）
            inst.container_id = Some(instance::recreate_container(&state, &inst).await?);
            rebuilt = true;
        }
    }
    // 规格调整：CPU/内存烧在 HostConfig 上，改动需重建容器（调用方先 stop）
    let mut spec_changed = false;
    if let Some(cpu) = body.cpu_limit {
        if !(0.1..=16.0).contains(&cpu) {
            return Err(AppError::new(422, "CPU 限制需在 0.1-16 之间"));
        }
        if cpu != inst.cpu_limit {
            inst.cpu_limit = cpu;
            spec_changed = true;
        }
    }
    if let Some(mem) = body.mem_limit {
        if !(64..=32768).contains(&mem) {
            return Err(AppError::new(422, "内存限制需在 64-32768 MB 之间"));
        }
        if mem != inst.mem_limit {
            inst.mem_limit = mem;
            spec_changed = true;
        }
    }
    if let Some(disk) = body.disk_quota {
        if !(256..=1048576).contains(&disk) {
            return Err(AppError::new(422, "磁盘配额需在 256-1048576 MB 之间"));
        }
        if disk != inst.disk_quota {
            inst.disk_quota = disk;
            spec_changed = true;
        }
    }
    if spec_changed {
        let docker = state.docker.get().await?;
        let name = instance::container_name(inst.id);
        let running = docker
            .inspect_container(&name, None)
            .await
            .ok()
            .and_then(|i| i.state)
            .map(|s| s.running.unwrap_or(false))
            .unwrap_or(false);
        if running {
            return Err(AppError::conflict("调整规格前请先停止实例"));
        }
        if docker.inspect_container(&name, None).await.is_ok() {
            inst.container_id = Some(instance::recreate_container(&state, &inst).await?);
            rebuilt = true;
        }
    }
    sqlx::query(
        "UPDATE instances SET name = ?, note = ?, start_cmd = ?, cpu_limit = ?, mem_limit = ?, \
         disk_quota = ?, status = ?, container_id = ? WHERE id = ?",
    )
    .bind(&inst.name)
    .bind(&inst.note)
    .bind(&inst.start_cmd)
    .bind(inst.cpu_limit)
    .bind(inst.mem_limit)
    .bind(inst.disk_quota)
    .bind(&inst.status)
    .bind(inst.container_id.as_deref().unwrap_or(""))
    .bind(inst.id)
    .execute(&state.db)
    .await?;
    let _ = rebuilt;
    Ok(Json(inst.out_json()))
}

async fn instance_stats(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
) -> AppResult<Json<Value>> {
    let inst = instance::fetch_instance(&state.db, id).await?;
    Ok(Json(instance::container_stats(&state, &inst).await))
}

#[derive(Debug, Deserialize, Default)]
struct TailQ {
    #[serde(default = "d_tail")]
    tail: i64,
}
fn d_tail() -> i64 {
    200
}

async fn instance_logs(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Query(q): Query<TailQ>,
) -> AppResult<Json<Value>> {
    let inst = instance::fetch_instance(&state.db, id).await?;
    Ok(Json(json!({"logs": instance::container_logs(&state, &inst, q.tail).await?})))
}

#[derive(Debug, Deserialize, Default)]
struct InstallQ {
    #[serde(default)]
    file: Option<String>,
}

async fn instance_install(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Query(q): Query<InstallQ>,
) -> AppResult<Json<Value>> {
    let inst = instance::fetch_instance(&state.db, id).await?;
    let file_name = q
        .file
        .unwrap_or_else(|| "requirements.txt".to_string())
        .trim()
        .to_string();
    if file_name.is_empty()
        || file_name.contains('/')
        || file_name.contains('\\')
        || file_name.contains("..")
    {
        return Err(AppError::bad_request("非法文件名"));
    }
    let job = instance::start_install(&state, &inst, &file_name).await?;
    Ok(Json(json!({"job_id": job.id})))
}

#[derive(Debug, Deserialize, Default)]
struct ResetQ {
    #[serde(default)]
    reset: i64,
}

async fn panel_token(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Query(q): Query<ResetQ>,
) -> AppResult<Json<Value>> {
    let mut inst = instance::fetch_instance(&state.db, id).await?;
    if q.reset != 0 || inst.panel_token.is_none() {
        inst.panel_token = Some(crate::auth::new_panel_token());
        sqlx::query("UPDATE instances SET panel_token = ? WHERE id = ?")
            .bind(inst.panel_token.as_deref())
            .bind(id)
            .execute(&state.db)
            .await?;
    }
    Ok(Json(json!({"panel_token": inst.panel_token})))
}

async fn panel_revoke(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
) -> AppResult<Json<Value>> {
    let inst = instance::fetch_instance(&state.db, id).await?;
    sqlx::query("UPDATE instances SET panel_token = NULL WHERE id = ?")
        .bind(inst.id)
        .execute(&state.db)
        .await?;
    Ok(Json(json!({"detail": "面板令牌已吊销"})))
}

// ---------- 实例文件 ----------

fn qp(q: &HashMap<String, String>, key: &str) -> AppResult<String> {
    q.get(key)
        .cloned()
        .ok_or_else(|| AppError::new(422, format!("缺少参数：{key}")))
}

fn qp_or(q: &HashMap<String, String>, key: &str, default: &str) -> String {
    q.get(key).cloned().unwrap_or_else(|| default.to_string())
}

async fn root_of(state: &AppState, id: i64) -> AppResult<(crate::models::Instance, String)> {
    let inst = instance::fetch_instance(&state.db, id).await?;
    let root = if inst.host_dir.is_empty() {
        instance::host_dir_for(inst.id)
    } else {
        inst.host_dir.clone()
    };
    file::ensure_root(&root);
    Ok((inst, root))
}

async fn list_files(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let (_inst, root) = root_of(&state, id).await?;
    let path = qp_or(&q, "path", "/");
    let entries = tokio::task::spawn_blocking({
        let root = root.clone();
        let path = path.clone();
        move || file::list_dir(&root, &path)
    })
    .await
    .map_err(|e| AppError::internal(format!("任务执行失败：{e}")))??;
    Ok(Json(json!({"path": path, "entries": entries})))
}

async fn file_content(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let (_inst, root) = root_of(&state, id).await?;
    let path = qp(&q, "path")?;
    let mut data = tokio::task::spawn_blocking({
        let root = root.clone();
        let path = path.clone();
        move || file::read_text(&root, &path)
    })
    .await
    .map_err(|e| AppError::internal(format!("任务执行失败：{e}")))??;
    data["path"] = json!(path);
    Ok(Json(data))
}

/// 流式写盘：边读分片边写文件，超过 UPLOAD_LIMIT_MB 立即中止并清理半成品。
async fn save_streamed(
    root: &str,
    rel_dir: &str,
    filename: &str,
    field: &mut Field<'_>,
    limit: u64,
) -> AppResult<Value> {
    let dir = file::resolve_path(root, rel_dir)?;
    if !dir.is_dir() {
        return Err(AppError::not_found("目标目录不存在"));
    }
    let name = file::sanitize_name(filename)?;
    let target = dir.join(&name);
    let mut f = tokio::fs::File::create(&target)
        .await
        .map_err(|e| AppError::internal(format!("写入失败：{e}")))?;
    let mut size: u64 = 0;
    loop {
        let chunk = match field.chunk().await {
            Ok(Some(c)) => c,
            Ok(None) => break,
            Err(e) => {
                drop(f);
                let _ = tokio::fs::remove_file(&target).await;
                return Err(AppError::bad_request(format!("上传数据解析失败：{e}")));
            }
        };
        size += chunk.len() as u64;
        if size > limit {
            drop(f);
            let _ = tokio::fs::remove_file(&target).await;
            return Err(AppError::too_large(format!(
                "文件超过单文件 {}MB 上限",
                limit / 1024 / 1024
            )));
        }
        if let Err(e) = f.write_all(&chunk).await {
            drop(f);
            let _ = tokio::fs::remove_file(&target).await;
            return Err(AppError::internal(format!("写入失败：{e}")));
        }
    }
    drop(f);
    Ok(json!({"name": name, "size": size}))
}

async fn upload_file(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Query(q): Query<HashMap<String, String>>,
    mut multipart: Multipart,
) -> AppResult<Json<Value>> {
    let (_inst, root) = root_of(&state, id).await?;
    let mut dir = qp_or(&q, "path", "/");
    let limit = (settings().upload_limit_mb * 1024 * 1024) as u64;
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
        if saved.is_none() && (fname == "file" || file_name.is_empty() == false) {
            if file_name.is_empty() {
                continue;
            }
            saved = Some(save_streamed(&root, &dir, &file_name, &mut field, limit).await?);
        } else {
            let _ = field.bytes().await;
        }
    }
    match saved {
        Some(v) => Ok(Json(v)),
        None => Err(AppError::bad_request("未收到文件")),
    }
}

#[derive(Debug, Deserialize)]
struct SaveIn {
    path: String,
    content: String,
}

async fn save_file(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Json(body): Json<SaveIn>,
) -> AppResult<Json<Value>> {
    let (_inst, root) = root_of(&state, id).await?;
    tokio::task::spawn_blocking({
        let root = root.clone();
        let path = body.path.clone();
        let content = body.content.clone();
        move || file::write_text(&root, &path, &content)
    })
    .await
    .map_err(|e| AppError::internal(format!("任务执行失败：{e}")))??;
    Ok(Json(json!({"detail": "已保存"})))
}

#[derive(Debug, Deserialize)]
struct MkdirIn {
    path: String,
}

async fn make_dir(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Json(body): Json<MkdirIn>,
) -> AppResult<Json<Value>> {
    let (_inst, root) = root_of(&state, id).await?;
    tokio::task::spawn_blocking({
        let root = root.clone();
        let path = body.path.clone();
        move || file::mkdir(&root, &path)
    })
    .await
    .map_err(|e| AppError::internal(format!("任务执行失败：{e}")))??;
    Ok(Json(json!({"detail": "已创建"})))
}

#[derive(Debug, Deserialize)]
struct RenameIn {
    src: String,
    dst: String,
}

async fn rename_path(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Json(body): Json<RenameIn>,
) -> AppResult<Json<Value>> {
    let (_inst, root) = root_of(&state, id).await?;
    tokio::task::spawn_blocking({
        let root = root.clone();
        let src = body.src.clone();
        let dst = body.dst.clone();
        move || file::rename(&root, &src, &dst)
    })
    .await
    .map_err(|e| AppError::internal(format!("任务执行失败：{e}")))??;
    Ok(Json(json!({"detail": "已重命名"})))
}

async fn delete_path(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let (_inst, root) = root_of(&state, id).await?;
    let path = qp(&q, "path")?;
    tokio::task::spawn_blocking({
        let root = root.clone();
        let path = path.clone();
        move || file::delete(&root, &path)
    })
    .await
    .map_err(|e| AppError::internal(format!("任务执行失败：{e}")))??;
    Ok(Json(json!({"detail": "已删除"})))
}

async fn download_file(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Response> {
    let (_inst, root) = root_of(&state, id).await?;
    let path = qp(&q, "path")?;
    let target = file::resolve_path(&root, &path)?;
    if !target.is_file() {
        return Err(AppError::bad_request("仅支持下载文件"));
    }
    let name = target
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "download".to_string());
    let mime = mime_of(&name);
    stream_file(target, name, mime).await
}

async fn backup_download(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
) -> AppResult<Response> {
    let (inst, root) = root_of(&state, id).await?;
    let tmp = tokio::task::spawn_blocking({
        let root = root.clone();
        move || file::create_backup(&root)
    })
    .await
    .map_err(|e| AppError::internal(format!("任务执行失败：{e}")))??;
    let f = tokio::fs::File::open(&tmp)
        .await
        .map_err(|e| AppError::internal(format!("备份包读取失败：{e}")))?;
    let len = f.metadata().await.map(|m| m.len()).unwrap_or(0);
    let fname = format!(
        "instance_{}_backup_{}.tar.gz",
        inst.id,
        chrono::Local::now().format("%Y%m%d_%H%M%S")
    );
    let body = Body::from_stream(TempFileStream {
        inner: ReaderStream::new(f),
        path: tmp,
    });
    Response::builder()
        .header(header::CONTENT_TYPE, "application/gzip")
        .header(header::CONTENT_DISPOSITION, disposition(&fname))
        .header(header::CONTENT_LENGTH, len.to_string())
        .body(body)
        .map_err(|e| AppError::internal(format!("响应构造失败：{e}")))
}

async fn restore_backup(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    mut multipart: Multipart,
) -> AppResult<Json<Value>> {
    let (mut inst, root) = root_of(&state, id).await?;
    let mut data: Option<Bytes> = None;
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::bad_request(format!("上传数据解析失败：{e}")))?
    {
        if data.is_none() && field.file_name().is_some() {
            data = Some(
                field
                    .bytes()
                    .await
                    .map_err(|e| AppError::bad_request(format!("上传数据解析失败：{e}")))?,
            );
        } else {
            let _ = field.bytes().await;
        }
    }
    let Some(data) = data else {
        return Err(AppError::bad_request("未收到文件"));
    };
    instance::sync_status(&state, &mut inst).await;
    if inst.status == "running" {
        return Err(AppError::conflict("实例运行中，请先停止后再恢复"));
    }
    let max_bytes = (inst.disk_quota.max(0) as u64) * 1024 * 1024;
    let out = tokio::task::spawn_blocking({
        let root = root.clone();
        move || file::restore_backup(&root, &data, max_bytes)
    })
    .await
    .map_err(|e| AppError::internal(format!("任务执行失败：{e}")))??;
    Ok(Json(out))
}

#[derive(Debug, Deserialize)]
struct CopyIn {
    src: String,
    dst: String,
}

async fn copy_path(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Json(body): Json<CopyIn>,
) -> AppResult<Json<Value>> {
    let (_inst, root) = root_of(&state, id).await?;
    tokio::task::spawn_blocking({
        let root = root.clone();
        let src = body.src.clone();
        let dst = body.dst.clone();
        move || file::copy_path(&root, &src, &dst)
    })
    .await
    .map_err(|e| AppError::internal(format!("任务执行失败：{e}")))??;
    Ok(Json(json!({"detail": "已复制"})))
}

#[derive(Debug, Deserialize)]
struct ZipIn {
    paths: Vec<String>,
    name: String,
    #[serde(default = "d_ztype")]
    z_type: String,
}
fn d_ztype() -> String {
    "tar.gz".to_string()
}

async fn zip_paths(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Json(body): Json<ZipIn>,
) -> AppResult<Json<Value>> {
    let (_inst, root) = root_of(&state, id).await?;
    tokio::task::spawn_blocking({
        let root = root.clone();
        let paths = body.paths.clone();
        let name = body.name.clone();
        let ztype = body.z_type.clone();
        move || file::zip_entries(&root, &paths, &name, &ztype)
    })
    .await
    .map_err(|e| AppError::internal(format!("任务执行失败：{e}")))??;
    Ok(Json(json!({"detail": "压缩完成"})))
}

#[derive(Debug, Deserialize)]
struct UnzipIn {
    src: String,
}

async fn unzip_path(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Json(body): Json<UnzipIn>,
) -> AppResult<Json<Value>> {
    let (_inst, root) = root_of(&state, id).await?;
    let dest = tokio::task::spawn_blocking({
        let root = root.clone();
        let src = body.src.clone();
        move || file::unzip_entry(&root, &src)
    })
    .await
    .map_err(|e| AppError::internal(format!("任务执行失败：{e}")))??;
    Ok(Json(json!({"detail": "解压完成", "dest": dest})))
}

async fn path_size(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let (_inst, root) = root_of(&state, id).await?;
    let path = qp(&q, "path")?;
    let size = tokio::task::spawn_blocking({
        let root = root.clone();
        let path = path.clone();
        move || file::dir_size(&root, &path)
    })
    .await
    .map_err(|e| AppError::internal(format!("任务执行失败：{e}")))??;
    Ok(Json(json!({"size": size})))
}

async fn preview_image(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let (_inst, root) = root_of(&state, id).await?;
    let path = qp(&q, "path")?;
    let out = tokio::task::spawn_blocking({
        let root = root.clone();
        let path = path.clone();
        move || file::image_preview(&root, &path)
    })
    .await
    .map_err(|e| AppError::internal(format!("任务执行失败：{e}")))??;
    Ok(Json(out))
}

#[derive(Debug, Deserialize)]
struct UploadExistsIn {
    #[serde(default = "d_slash")]
    path: String,
    file_name: String,
    #[serde(default)]
    total_size: i64,
}
fn d_slash() -> String {
    "/".to_string()
}

async fn upload_exists(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    Json(body): Json<UploadExistsIn>,
) -> AppResult<Json<Value>> {
    let (_inst, root) = root_of(&state, id).await?;
    let out = tokio::task::spawn_blocking({
        let root = root.clone();
        move || file::upload_exists(&root, &body.path, &body.file_name, body.total_size)
    })
    .await
    .map_err(|e| AppError::internal(format!("任务执行失败：{e}")))??;
    Ok(Json(out))
}

async fn upload_chunk(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(id): Path<i64>,
    mut multipart: Multipart,
) -> AppResult<Json<Value>> {
    let (_inst, root) = root_of(&state, id).await?;
    let mut path = "/".to_string();
    let mut file_name = String::new();
    let mut total_size: i64 = 0;
    let mut start: i64 = 0;
    let mut data: Option<Bytes> = None;
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::bad_request(format!("上传数据解析失败：{e}")))?
    {
        let name = field.name().unwrap_or("").to_string();
        match name.as_str() {
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
    let Some(d) = data else {
        return Err(AppError::bad_request("未收到分片数据"));
    };
    let out = tokio::task::spawn_blocking({
        let root = root.clone();
        move || file::upload_chunk(&root, &path, &file_name, total_size, start, &d)
    })
    .await
    .map_err(|e| AppError::internal(format!("任务执行失败：{e}")))??;
    Ok(Json(out))
}

// ---------- 宿主机资源仪表盘（/proc 零依赖采集，仅 Linux） ----------

fn read_proc(path: &str) -> String {
    std::fs::read_to_string(path).unwrap_or_default()
}

/// 两次 /proc/stat 采样算 CPU 使用率（阻塞约 0.25s）。
fn cpu_percent_sample() -> (Option<f64>, Option<i64>) {
    let times = || -> Option<(i64, i64)> {
        let text = read_proc("/proc/stat");
        let first = text.lines().next()?;
        if !first.starts_with("cpu ") {
            return None;
        }
        let vals: Vec<i64> = first
            .split_whitespace()
            .skip(1)
            .filter_map(|x| x.parse::<i64>().ok())
            .collect();
        if vals.is_empty() {
            return None;
        }
        let idle = vals.get(3).copied().unwrap_or(0) + vals.get(4).copied().unwrap_or(0);
        Some((idle, vals.iter().sum()))
    };
    let (idle1, total1) = match times() {
        Some(v) => v,
        None => return (None, None),
    };
    std::thread::sleep(Duration::from_millis(250));
    let (idle2, total2) = match times() {
        Some(v) => v,
        None => return (None, None),
    };
    let dt = total2 - total1;
    let di = idle2 - idle1;
    if dt <= 0 {
        return (None, None);
    }
    let cores = read_proc("/proc/stat")
        .lines()
        .filter(|l| {
            l.starts_with("cpu")
                && l.chars().nth(3).map(|c| c.is_ascii_digit()).unwrap_or(false)
        })
        .count() as i64;
    (
        Some(crate::models::round1((1.0 - di as f64 / dt as f64) * 100.0)),
        if cores > 0 { Some(cores) } else { None },
    )
}

/// 磁盘占用（等价 shutil.disk_usage；非 Unix 返回 None）。
fn disk_usage(path: &str) -> Option<Value> {
    #[cfg(unix)]
    {
        let c = std::ffi::CString::new(path).ok()?;
        let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
        if unsafe { libc::statvfs(c.as_ptr(), &mut st) } != 0 {
            return None;
        }
        let total = st.f_blocks as i64 * st.f_frsize as i64;
        if total <= 0 {
            return None;
        }
        let free = st.f_bavail as i64 * st.f_frsize as i64;
        let used = total - free;
        Some(json!({
            "total": total,
            "used": used,
            "percent": crate::models::round1(used as f64 / total as f64 * 100.0),
        }))
    }
    #[cfg(not(unix))]
    {
        let _ = path;
        None
    }
}

/// 宿主机资源快照：CPU / 内存 / 磁盘 / 负载 / 运行时长 / 容器概况。
async fn host_stats(_a: NodeAuth, State(state): State<AppState>) -> AppResult<Json<Value>> {
    let (cpu, cores) = tokio::task::spawn_blocking(cpu_percent_sample)
        .await
        .unwrap_or((None, None));

    let mut mem: Option<Value> = None;
    let mut info: HashMap<String, i64> = HashMap::new();
    for line in read_proc("/proc/meminfo").lines() {
        let (k, v) = match line.split_once(':') {
            Some(kv) => kv,
            None => continue,
        };
        if let Some(first) = v.split_whitespace().next() {
            if let Ok(n) = first.parse::<i64>() {
                info.insert(k.trim().to_string(), n * 1024);
            }
        }
    }
    let total = info.get("MemTotal").copied().unwrap_or(0);
    let avail = info.get("MemAvailable").copied().unwrap_or(0);
    if total > 0 {
        mem = Some(json!({
            "total": total,
            "used": total - avail,
            "percent": crate::models::round1((total - avail) as f64 / total as f64 * 100.0),
        }));
    }

    let disk: Option<Value> = {
        let root = settings().data_root.clone();
        tokio::task::spawn_blocking(move || disk_usage(&root))
            .await
            .unwrap_or(None)
    };

    let load: Option<Vec<String>> = {
        let parts: Vec<String> = read_proc("/proc/loadavg")
            .split_whitespace()
            .take(3)
            .map(|s| s.to_string())
            .collect();
        if parts.is_empty() {
            None
        } else {
            Some(parts)
        }
    };

    let uptime: Option<i64> = read_proc("/proc/uptime")
        .split_whitespace()
        .next()
        .and_then(|s| s.parse::<f64>().ok())
        .map(|f| f as i64);

    let mut cont: Option<Value> = None;
    if let Some(docker) = state.docker.try_get().await {
        if let Ok(i) = docker.info().await {
            let i = serde_json::to_value(i).unwrap_or(Value::Null);
            cont = Some(json!({
                "running": jint(&i, "ContainersRunning"),
                "paused": jint(&i, "ContainersPaused"),
                "stopped": jint(&i, "ContainersStopped"),
                "images": jint(&i, "Images"),
                "total": jint(&i, "Containers"),
            }));
        }
    }

    Ok(Json(json!({
        "cpu": {"percent": cpu, "cores": cores},
        "mem": mem,
        "disk": disk,
        "load": load,
        "uptime": uptime,
        "containers": cont,
    })))
}

// ---------- 宿主机防火墙 ----------

async fn firewall_status(_a: NodeAuth) -> AppResult<Json<Value>> {
    Ok(Json(host_ops::firewall_status().await))
}

async fn firewall_allow(_a: NodeAuth, Json(body): Json<PortRule>) -> AppResult<Json<Value>> {
    Ok(Json(host_ops::firewall_allow(&body.port, &body.proto).await?))
}

async fn firewall_deny(_a: NodeAuth, Json(body): Json<PortRule>) -> AppResult<Json<Value>> {
    Ok(Json(host_ops::firewall_deny(&body.port, &body.proto).await?))
}

#[derive(Debug, Deserialize, Default)]
struct RuleDelete {
    #[serde(default)]
    nums: Vec<i64>,
    #[serde(default)]
    num: i64,
    #[serde(default)]
    port: String,
    #[serde(default = "d_proto")]
    proto: String,
}
fn d_proto() -> String {
    "tcp".to_string()
}

async fn firewall_delete(_a: NodeAuth, Json(body): Json<RuleDelete>) -> AppResult<Json<Value>> {
    Ok(Json(
        host_ops::firewall_delete(body.nums, body.num, &body.port, &body.proto).await?,
    ))
}

#[derive(Debug, Deserialize)]
struct FwToggle {
    enable: bool,
}

async fn firewall_toggle(_a: NodeAuth, Json(body): Json<FwToggle>) -> AppResult<Json<Value>> {
    Ok(Json(host_ops::firewall_toggle(body.enable).await?))
}

// ---------- Docker 概览 ----------

async fn docker_info(_a: NodeAuth, State(state): State<AppState>) -> AppResult<Json<Value>> {
    let docker = state.docker.get().await?;
    let ver = docker.version().await.map_err(docker_fail)?;
    let info = docker.info().await.map_err(docker_fail)?;
    let v = serde_json::to_value(ver).unwrap_or(Value::Null);
    let i = serde_json::to_value(info).unwrap_or(Value::Null);
    let os = format!("{} {}", jstr(&i, "OperatingSystem"), jstr(&i, "OSType"))
        .trim()
        .to_string();
    let data = host_ops::read_daemon_json().await;
    Ok(Json(json!({
        "host_kind": host_ops::host_kind(),
        "server_version": v.get("Version").cloned().unwrap_or(Value::Null),
        "api_version": v.get("ApiVersion").cloned().unwrap_or(Value::Null),
        "os": os,
        "arch": v.get("Arch").cloned().unwrap_or(Value::Null),
        "kernel": i.get("KernelVersion").cloned().unwrap_or(Value::Null),
        "storage_driver": i.get("Driver").cloned().unwrap_or(Value::Null),
        "cpus": i.get("NCPU").cloned().unwrap_or(Value::Null),
        "mem_total_gb": crate::models::round1(jint(&i, "MemTotal") as f64 / 1024f64.powi(3)),
        "containers_running": jint(&i, "ContainersRunning"),
        "containers_paused": jint(&i, "ContainersPaused"),
        "containers_stopped": jint(&i, "ContainersStopped"),
        "images": jint(&i, "Images"),
        "daemon_json_path": host_ops::DAEMON_JSON,
        "registry_mirrors": data.get("registry-mirrors").cloned().unwrap_or(json!([])),
    })))
}

fn fmt_ports_list(ports: &Value) -> String {
    let mut parts: Vec<String> = Vec::new();
    if let Some(arr) = ports.as_array() {
        for p in arr {
            if let Some(public) = p.get("PublicPort").and_then(|x| x.as_i64()) {
                let private = p.get("PrivatePort").and_then(|x| x.as_i64()).unwrap_or(0);
                let ty = p
                    .get("Type")
                    .and_then(|x| x.as_str())
                    .unwrap_or("tcp")
                    .to_string();
                parts.push(format!("{public}->{private}/{ty}"));
            }
        }
    } else if let Some(map) = ports.as_object() {
        for (inner, binds) in map {
            if let Some(arr) = binds.as_array() {
                for b in arr {
                    if let Some(hp) = b.get("HostPort").and_then(|x| x.as_str()) {
                        parts.push(format!("{hp}->{inner}"));
                    }
                }
            }
        }
    }
    let uniq: Vec<String> = {
        let mut seen = HashSet::new();
        parts.into_iter().filter(|x| seen.insert(x.clone())).collect()
    };
    if uniq.is_empty() {
        "-".to_string()
    } else {
        uniq.join(", ")
    }
}

async fn list_containers(_a: NodeAuth, State(state): State<AppState>) -> AppResult<Json<Value>> {
    let docker = state.docker.get().await?;
    let opts = ListContainersOptions::<String> {
        all: true,
        ..Default::default()
    };
    let list = docker.list_containers(Some(opts)).await.map_err(docker_fail)?;
    let prefix = instance::container_prefix();
    let known = [prefix, "ppanel-".to_string()];
    let mut rows: Vec<Value> = Vec::new();
    for c in list {
        let a = serde_json::to_value(&c).unwrap_or(Value::Null);
        let id = jstr(&a, "Id");
        let id = if id.len() > 12 { id[..12].to_string() } else { id };
        let name = {
            let n = jstr(&a, "Name").trim_start_matches('/').to_string();
            if !n.is_empty() {
                n
            } else {
                a.get("Names")
                    .and_then(|x| x.as_array())
                    .and_then(|arr| arr.first())
                    .and_then(|x| x.as_str())
                    .map(|s| s.trim_start_matches('/').to_string())
                    .unwrap_or_default()
            }
        };
        let state_s = jstr(&a, "State");
        let status = {
            let st = jstr(&a, "Status");
            if st.is_empty() {
                state_s.clone()
            } else {
                st
            }
        };
        let created = a.get("Created").cloned().unwrap_or_else(|| json!(""));
        let ports_val = a.get("Ports").cloned().unwrap_or(Value::Null);
        rows.push(json!({
            "id": id,
            "name": name,
            "image": jstr(&a, "Image"),
            "state": state_s,
            "status": status,
            "ports": fmt_ports_list(&ports_val),
            "created": created,
            "is_panel": known.iter().any(|p| name.starts_with(p.as_str())),
        }));
    }
    Ok(Json(Value::Array(rows)))
}

fn epoch_iso(sec: i64) -> String {
    chrono::Local
        .timestamp_opt(sec, 0)
        .single()
        .map(|d| d.format("%Y-%m-%dT%H:%M:%S").to_string())
        .unwrap_or_default()
}

async fn inspect_container(docker: &Docker, cid: &str) -> AppResult<Value> {
    match docker.inspect_container(cid, None).await {
        Ok(v) => Ok(serde_json::to_value(v).unwrap_or(Value::Null)),
        Err(e) if is_404(&e) => Err(AppError::not_found("容器不存在")),
        Err(e) => Err(docker_fail(e)),
    }
}

async fn container_start(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(cid): Path<String>,
) -> AppResult<Json<Value>> {
    let docker = state.docker.get().await?;
    inspect_container(&docker, &cid).await?;
    docker
        .start_container(&cid, None::<bollard::container::StartContainerOptions<String>>)
        .await
        .map_err(|e| AppError::bad_gateway(format!("启动失败：{e}")))?;
    Ok(Json(json!({"ok": true, "detail": "已启动"})))
}

async fn container_stop(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(cid): Path<String>,
) -> AppResult<Json<Value>> {
    let docker = state.docker.get().await?;
    inspect_container(&docker, &cid).await?;
    docker
        .stop_container(&cid, Some(StopContainerOptions { t: 10 }))
        .await
        .map_err(|e| AppError::bad_gateway(format!("停止失败：{e}")))?;
    Ok(Json(json!({"ok": true, "detail": "已停止"})))
}

async fn container_restart(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(cid): Path<String>,
) -> AppResult<Json<Value>> {
    let docker = state.docker.get().await?;
    inspect_container(&docker, &cid).await?;
    docker
        .restart_container(&cid, Some(bollard::container::RestartContainerOptions { t: 10 }))
        .await
        .map_err(|e| AppError::bad_gateway(format!("重启失败：{e}")))?;
    Ok(Json(json!({"ok": true, "detail": "已重启"})))
}

#[derive(Debug, Deserialize, Default)]
struct ForceQ {
    #[serde(default)]
    force: bool,
}

async fn container_remove(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(cid): Path<String>,
    Query(q): Query<ForceQ>,
) -> AppResult<Json<Value>> {
    let docker = state.docker.get().await?;
    let info = inspect_container(&docker, &cid).await?;
    let running = info
        .get("State")
        .and_then(|s| s.get("Running"))
        .and_then(|b| b.as_bool())
        .unwrap_or(false);
    if running && !q.force {
        return Err(AppError::conflict("容器运行中，请先停止或使用强制删除"));
    }
    docker
        .remove_container(
            &cid,
            Some(RemoveContainerOptions {
                force: q.force,
                ..Default::default()
            }),
        )
        .await
        .map_err(|e| AppError::bad_gateway(format!("删除失败：{e}")))?;
    Ok(Json(json!({"ok": true, "detail": "容器已删除"})))
}

async fn container_logs(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(cid): Path<String>,
    Query(q): Query<TailQ>,
) -> AppResult<Json<Value>> {
    let docker = state.docker.get().await?;
    inspect_container(&docker, &cid).await?;
    let tail = q.tail.max(1).min(5000);
    let mut stream = docker.logs(
        &cid,
        Some(LogsOptions::<String> {
            follow: false,
            stdout: true,
            stderr: true,
            since: 0,
            until: 0,
            timestamps: false,
            tail: tail.to_string(),
        }),
    );
    let mut text = String::new();
    while let Some(item) = stream.next().await {
        match item {
            Ok(bollard::container::LogOutput::StdOut { message })
            | Ok(bollard::container::LogOutput::StdErr { message })
            | Ok(bollard::container::LogOutput::Console { message })
            | Ok(bollard::container::LogOutput::StdIn { message }) => {
                text.push_str(&String::from_utf8_lossy(&message));
            }
            Err(e) => return Err(AppError::bad_gateway(format!("读取日志失败：{e}"))),
        }
    }
    Ok(Json(json!({"logs": text})))
}

// ---------- 镜像管理 ----------

fn image_row(inspect: &Value) -> Value {
    let tags: Vec<String> = match inspect.get("RepoTags").and_then(|x| x.as_array()) {
        Some(arr) if !arr.is_empty() => arr
            .iter()
            .filter_map(|x| x.as_str().map(|s| s.to_string()))
            .collect(),
        _ => vec!["<none>:<none>".to_string()],
    };
    let first = tags.first().cloned().unwrap_or_else(|| "<none>:<none>".into());
    let (repo, tag) = match first.rsplit_once(':') {
        Some((r, t)) => (r.to_string(), t.to_string()),
        None => (first.clone(), "latest".to_string()),
    };
    let size = inspect.get("Size").and_then(|x| x.as_i64()).unwrap_or(0);
    let created = inspect.get("Created").and_then(|x| x.as_i64()).unwrap_or(0);
    let id = jstr(inspect, "Id").replace("sha256:", "");
    let id = if id.len() > 12 { id[..12].to_string() } else { id };
    json!({
        "id": id,
        "repo": repo,
        "tag": tag,
        "full_name": first,
        "tags": tags,
        "size_bytes": size,
        "size": fmt_size(size),
        "created": if created > 0 { epoch_iso(created) } else { String::new() },
    })
}

async fn list_images(_a: NodeAuth, State(state): State<AppState>) -> AppResult<Json<Value>> {
    let docker = state.docker.get().await?;
    let images = docker.list_images::<String>(None).await.map_err(docker_fail)?;
    let mut rows: Vec<Value> = Vec::new();
    for img in images {
        let a = serde_json::to_value(&img).unwrap_or(Value::Null);
        rows.push(image_row(&a));
    }
    rows.sort_by(|x, y| {
        jstr(x, "repo").cmp(&jstr(y, "repo"))
    });
    Ok(Json(Value::Array(rows)))
}

async fn remove_image(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(reference): Path<String>,
    Query(q): Query<ForceQ>,
) -> AppResult<Json<Value>> {
    let docker = state.docker.get().await?;
    let res = docker
        .remove_image(
            &reference,
            Some(RemoveImageOptions {
                force: q.force,
                ..Default::default()
            }),
            None,
        )
        .await;
    match res {
        Ok(_) => Ok(Json(json!({"ok": true, "detail": format!("镜像 {reference} 已删除")}))),
        Err(e) if is_404(&e) => Err(AppError::not_found("镜像不存在")),
        Err(e) => {
            let msg = e.to_string();
            let low = msg.to_lowercase();
            if low.contains("conflict") || low.contains("in use") {
                return Err(AppError::conflict(
                    "镜像正被容器使用，请先删除相关容器或使用强制删除",
                ));
            }
            Err(AppError::bad_gateway(format!("删除失败：{msg}")))
        }
    }
}

async fn prune_images(_a: NodeAuth, State(state): State<AppState>) -> AppResult<Json<Value>> {
    let docker = state.docker.get().await?;
    let res = docker
        .prune_images::<String>(Some(PruneImagesOptions {
            filters: {
                let mut m: HashMap<String, Vec<String>> = HashMap::new();
                m.insert("dangling".into(), vec!["true".into()]);
                m
            },
        }))
        .await
        .map_err(|e| AppError::bad_gateway(format!("清理失败：{e}")))?;
    let v = serde_json::to_value(res).unwrap_or(Value::Null);
    let n = v
        .get("ImagesDeleted")
        .and_then(|x| x.as_array())
        .map(|a| a.len())
        .unwrap_or(0);
    let mb = crate::models::round1(jint(&v, "SpaceReclaimed") as f64 / 1024f64 / 1024f64);
    Ok(Json(json!({
        "ok": true,
        "detail": format!("已清理 {n} 个悬空镜像，释放 {mb:.1} MB"),
    })))
}

#[derive(Debug, Deserialize)]
struct PullReq {
    image: String,
}

async fn pull_image(
    _a: NodeAuth,
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<PullReq>,
) -> AppResult<Json<Value>> {
    let tid = tid_of(&headers);
    let image = body.image.trim().to_string();
    if image.is_empty() || image.chars().count() > 200 {
        return Err(AppError::new(422, "镜像名长度需为 1-200 字符"));
    }
    if !tid.is_empty() {
        state.tasks.start(&tid, &format!("拉取镜像 {image}"));
        state.tasks.log(&tid, "向仓库请求镜像层…");
    }
    let docker = state.docker.get().await?;
    let mut stream = docker.create_image(
        Some(CreateImageOptions {
            from_image: image.clone(),
            ..Default::default()
        }),
        None,
        None,
    );
    let mut pull_err: Option<String> = None;
    while let Some(item) = stream.next().await {
        match item {
            Ok(info) => {
                let v = serde_json::to_value(&info).unwrap_or(Value::Null);
                if let Some(err) = v.get("error").and_then(|x| x.as_str()) {
                    pull_err = Some(err.to_string());
                    break;
                }
            }
            Err(e) => {
                let msg = e.to_string();
                if !tid.is_empty() {
                    state.tasks.finish(&tid, false, &format!("✘ 拉取失败：{msg}"));
                }
                if is_404(&e) {
                    return Err(AppError::not_found(format!("镜像 {image} 不存在于仓库")));
                }
                return Err(AppError::bad_gateway(format!("拉取失败：{msg}")));
            }
        }
    }
    if let Some(err) = pull_err {
        if !tid.is_empty() {
            state.tasks.finish(&tid, false, &format!("✘ 拉取失败：{err}"));
        }
        let low = err.to_lowercase();
        if low.contains("not found") || low.contains("manifest unknown") {
            return Err(AppError::not_found(format!("镜像 {image} 不存在于仓库")));
        }
        return Err(AppError::bad_gateway(format!("拉取失败：{err}")));
    }
    let inspect = docker
        .inspect_image(&image)
        .await
        .map_err(|e| AppError::bad_gateway(format!("拉取失败：{e}")))?;
    let row = image_row(&serde_json::to_value(inspect).unwrap_or(Value::Null));
    if !tid.is_empty() {
        state.tasks.log(
            &tid,
            &format!("镜像层全部就位（{}）", jstr(&row, "size")),
        );
        state.tasks.finish(&tid, true, "✔ 拉取完成");
    }
    Ok(Json(json!({"ok": true, "detail": "拉取完成", "image": row})))
}

// ---------- 磁盘占用 / 一键清理 ----------

async fn docker_df(_a: NodeAuth, State(state): State<AppState>) -> AppResult<Json<Value>> {
    let docker = state.docker.get().await?;
    let df = docker.df().await.map_err(docker_fail)?;
    let v = serde_json::to_value(df).unwrap_or(Value::Null);
    let sum = |items: Option<&Vec<Value>>| -> Value {
        let arr = items.cloned().unwrap_or_default();
        let total: i64 = arr.iter().map(|i| jint(i, "Size")).sum();
        json!({"count": arr.len(), "size": total, "size_text": fmt_size(total)})
    };
    let images = sum(v.get("Images").and_then(|x| x.as_array()));
    let containers = sum(v.get("Containers").and_then(|x| x.as_array()));
    let build_cache = sum(v.get("BuildCache").and_then(|x| x.as_array()));
    let volumes = v.get("Volumes").and_then(|x| x.as_array()).cloned().unwrap_or_default();
    let vol_total: i64 = volumes
        .iter()
        .map(|x| x.get("UsageData").map(|u| jint(u, "Size")).unwrap_or(0))
        .sum();
    Ok(Json(json!({
        "images": images,
        "containers": containers,
        "volumes": {"count": volumes.len(), "size": vol_total, "size_text": fmt_size(vol_total)},
        "build_cache": build_cache,
    })))
}

#[derive(Debug, Deserialize, Default)]
struct PruneReq {
    #[serde(default)]
    builder: bool,
}

async fn system_prune(
    _a: NodeAuth,
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<PruneReq>,
) -> AppResult<Json<Value>> {
    let tid = tid_of(&headers);
    if !tid.is_empty() {
        state.tasks.start(&tid, "一键清理（停止容器/悬空镜像/无用网络）");
    }
    let docker = state.docker.get().await?;
    let mut freed: Vec<(&str, usize)> = Vec::new();
    let mut total: i64 = 0;
    if !tid.is_empty() {
        state.tasks.log(&tid, "清理停止的容器…");
    }
    let c = docker
        .prune_containers::<String>(None)
        .await
        .map_err(|e| AppError::bad_gateway(format!("清理失败：{e}")))?;
    let cv = serde_json::to_value(c).unwrap_or(Value::Null);
    freed.push((
        "containers",
        cv.get("ContainersDeleted")
            .and_then(|x| x.as_array())
            .map(|a| a.len())
            .unwrap_or(0),
    ));
    total += jint(&cv, "SpaceReclaimed");

    if !tid.is_empty() {
        state.tasks.log(&tid, "清理悬空镜像…");
    }
    let i = docker
        .prune_images::<String>(Some(PruneImagesOptions {
            filters: {
                let mut m: HashMap<String, Vec<String>> = HashMap::new();
                m.insert("dangling".into(), vec!["true".into()]);
                m
            },
        }))
        .await
        .map_err(|e| AppError::bad_gateway(format!("清理失败：{e}")))?;
    let iv = serde_json::to_value(i).unwrap_or(Value::Null);
    freed.push((
        "images",
        iv.get("ImagesDeleted")
            .and_then(|x| x.as_array())
            .map(|a| a.len())
            .unwrap_or(0),
    ));
    total += jint(&iv, "SpaceReclaimed");

    if !tid.is_empty() {
        state.tasks.log(&tid, "清理无用网络…");
    }
    let n = docker
        .prune_networks::<String>(None)
        .await
        .map_err(|e| AppError::bad_gateway(format!("清理失败：{e}")))?;
    let nv = serde_json::to_value(n).unwrap_or(Value::Null);
    freed.push((
        "networks",
        nv.get("NetworksDeleted")
            .and_then(|x| x.as_array())
            .map(|a| a.len())
            .unwrap_or(0),
    ));

    if body.builder {
        if !tid.is_empty() {
            state.tasks.log(&tid, "清理构建缓存…");
        }
        // bollard 0.18 未暴露 POST /build/prune，构建缓存清理记为 0（见差异说明）
        freed.push(("build_cache", 0));
    }

    let parts = freed
        .iter()
        .map(|(k, v)| format!("{k} {v} 项"))
        .collect::<Vec<_>>()
        .join("、");
    let detail = format!("已清理 {parts}，释放 {}", fmt_size(total));
    if !tid.is_empty() {
        state.tasks.finish(&tid, true, &format!("✔ {detail}"));
    }
    Ok(Json(json!({"ok": true, "detail": detail})))
}

// ---------- 镜像加速设置 ----------

async fn get_settings(_a: NodeAuth) -> AppResult<Json<Value>> {
    let data = host_ops::read_daemon_json().await;
    let mut keys: Vec<String> = data
        .as_object()
        .map(|m| m.keys().cloned().collect())
        .unwrap_or_default();
    keys.sort();
    Ok(Json(json!({
        "host_kind": host_ops::host_kind(),
        "daemon_json_path": host_ops::DAEMON_JSON,
        "registry_mirrors": data.get("registry-mirrors").cloned().unwrap_or(json!([])),
        "raw_keys": keys,
    })))
}

#[derive(Debug, Deserialize, Default)]
struct MirrorSettings {
    #[serde(default)]
    registry_mirrors: Vec<String>,
    #[serde(default = "d_true")]
    restart: bool,
}
fn d_true() -> bool {
    true
}

async fn put_settings(
    _a: NodeAuth,
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<MirrorSettings>,
) -> AppResult<Json<Value>> {
    let tid = tid_of(&headers);
    if !tid.is_empty() {
        state.tasks.start(&tid, "保存镜像加速配置");
    }
    if body.registry_mirrors.len() > 20 {
        return Err(AppError::new(422, "镜像加速地址最多 20 条"));
    }
    let mirrors: Vec<String> = body
        .registry_mirrors
        .iter()
        .map(|m| m.trim().trim_end_matches('/').to_string())
        .filter(|m| !m.is_empty())
        .collect();
    if !tid.is_empty() {
        state.tasks.log(&tid, "写入 daemon.json…");
    }
    let mut data = host_ops::read_daemon_json().await;
    if data.is_null() {
        data = json!({});
    }
    if mirrors.is_empty() {
        if let Some(obj) = data.as_object_mut() {
            obj.remove("registry-mirrors");
        }
    } else {
        data["registry-mirrors"] = json!(mirrors);
    }
    if let Err(e) = host_ops::write_daemon_json(&data).await {
        if !tid.is_empty() {
            state.tasks.finish(&tid, false, &format!("✘ 保存失败：{}", e.detail));
        }
        return Err(e);
    }

    if body.restart {
        if !tid.is_empty() {
            state.tasks.log(&tid, "重启 Docker 守护进程（最长 30s）…");
        }
        state.docker.reset().await;
        tokio::spawn(async { host_ops::restart_dockerd().await });
        let mut ok = false;
        for _ in 0..60 {
            tokio::time::sleep(Duration::from_millis(500)).await;
            state.docker.reset().await;
            if state.docker.get().await.is_ok() {
                ok = true;
                break;
            }
        }
        if !tid.is_empty() {
            state.tasks.log(
                &tid,
                if ok {
                    "Docker 已重启恢复响应"
                } else {
                    "Docker 30s 内未恢复响应"
                },
            );
        }
        if !ok {
            if !tid.is_empty() {
                state.tasks.finish(&tid, false, "✘ 保存失败：Docker 未恢复");
            }
            return Err(AppError::bad_gateway(
                "daemon.json 已写入，但 Docker 重启后未在 30s 内恢复，请到宿主机检查",
            ));
        }
    }
    if !tid.is_empty() {
        state.tasks.finish(
            &tid,
            true,
            &format!(
                "✔ 镜像加速配置已保存{}",
                if body.restart { "，Docker 已重启生效" } else { "" }
            ),
        );
    }
    Ok(Json(json!({
        "ok": true,
        "detail": "镜像加速配置已保存，Docker 已重启生效",
        "registry_mirrors": mirrors,
    })))
}

// ---------- PHP 增强镜像构建（内存任务字典 + 轮询进度） ----------

const PHP_DOCKERFILE: &str = r#"ARG BASE
FROM ${BASE}
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpng-dev libjpeg-dev libfreetype6-dev libzip-dev libicu-dev \
    && docker-php-ext-configure gd --with-jpeg --with-freetype \
    && docker-php-ext-install -j"$(nproc)" mysqli pdo_mysql gd zip bcmath intl sockets exif opcache \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
"#;

struct PhpBuildJob {
    id: String,
    image: String,
    lines: Mutex<Vec<String>>,
    done: AtomicBool,
    exit_code: Mutex<Option<i64>>,
}

impl PhpBuildJob {
    fn emit(&self, text: impl Into<String>) {
        let mut l = self.lines.lock().unwrap();
        if l.len() >= 8000 {
            l.remove(0);
        }
        l.push(text.into());
    }
    fn finish(&self, code: i64) {
        *self.exit_code.lock().unwrap() = Some(code);
        self.done.store(true, Ordering::SeqCst);
    }
}

fn php_jobs() -> &'static Mutex<HashMap<String, Arc<PhpBuildJob>>> {
    static JOBS: OnceLock<Mutex<HashMap<String, Arc<PhpBuildJob>>>> = OnceLock::new();
    JOBS.get_or_init(|| Mutex::new(HashMap::new()))
}

#[derive(Debug, Deserialize)]
struct PhpBuildReq {
    base: String,
}

async fn php_build_worker(job: Arc<PhpBuildJob>, base: String, target: String) {
    job.emit(format!("[构建] {base} → {target}"));
    job.emit("[构建] 编译扩展：mysqli pdo_mysql gd zip bcmath intl sockets exif opcache");
    let spawn = tokio::process::Command::new("docker")
        .args([
            "build",
            "--network=host",
            "-t",
            &target,
            "--build-arg",
            &format!("BASE={base}"),
            "-",
        ])
        .env("DOCKER_BUILDKIT", "0")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn();
    let Ok(mut child) = spawn else {
        job.emit("[构建] 异常：无法启动 docker build");
        job.finish(-1);
        return;
    };
    if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.write_all(PHP_DOCKERFILE.as_bytes()).await;
        let _ = stdin.shutdown().await;
    }
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    let out_task = async {
        if let Some(out) = stdout {
            let mut rd = BufReader::new(out).lines();
            while let Ok(Some(line)) = rd.next_line().await {
                job.emit(line);
            }
        }
    };
    let err_task = async {
        if let Some(err) = stderr {
            let mut rd = BufReader::new(err).lines();
            while let Ok(Some(line)) = rd.next_line().await {
                job.emit(line);
            }
        }
    };
    tokio::join!(out_task, err_task);
    let code = match child.wait().await {
        Ok(st) => st.code().unwrap_or(-1) as i64,
        Err(_) => -1,
    };
    if code == 0 {
        job.emit(format!("[构建] 完成！镜像已加入运行环境白名单：{target}"));
    } else {
        job.emit(format!("[构建] 失败（exit={code}）"));
    }
    job.finish(code);
}

async fn php_image_build(
    _a: NodeAuth,
    State(state): State<AppState>,
    Json(body): Json<PhpBuildReq>,
) -> AppResult<Json<Value>> {
    let base = body.base.trim().to_string();
    if !settings().php_images.contains(&base) {
        return Err(AppError::bad_request("仅支持基于官方 PHP 白名单镜像构建"));
    }
    let ver = base
        .split_once(':')
        .map(|(_, v)| v.replace("-cli", ""))
        .unwrap_or_else(|| "8.2".to_string());
    let target = format!("ppanel-php:{ver}-full");

    let docker = state.docker.get().await?;
    match docker.inspect_image(&base).await {
        Ok(_) => {}
        Err(e) if is_404(&e) => {
            return Err(AppError::not_found(format!(
                "基础镜像 {base} 未拉取，请先拉取镜像"
            )))
        }
        Err(e) => return Err(docker_fail(e)),
    }

    {
        let jobs = php_jobs().lock().unwrap();
        for j in jobs.values() {
            if j.image == target && !j.done.load(Ordering::SeqCst) {
                return Err(AppError::conflict(format!("{target} 正在构建中，请稍候")));
            }
        }
    }
    {
        // 清理历史已完成任务，保持注册表精简
        let mut jobs = php_jobs().lock().unwrap();
        jobs.retain(|_, j| !j.done.load(Ordering::SeqCst));
    }

    let id = uuid::Uuid::new_v4().simple().to_string();
    let job = Arc::new(PhpBuildJob {
        id: id.clone(),
        image: target.clone(),
        lines: Mutex::new(Vec::new()),
        done: AtomicBool::new(false),
        exit_code: Mutex::new(None),
    });
    php_jobs().lock().unwrap().insert(id.clone(), job.clone());
    tokio::spawn(php_build_worker(job, base, target.clone()));
    Ok(Json(json!({"ok": true, "job_id": id, "image": target})))
}

async fn php_image_build_status(
    _a: NodeAuth,
    Path(job_id): Path<String>,
) -> AppResult<Json<Value>> {
    let job = php_jobs().lock().unwrap().get(&job_id).cloned();
    let Some(job) = job else {
        return Err(AppError::not_found("构建任务不存在或已被清理"));
    };
    let lines = job.lines.lock().unwrap().clone();
    Ok(Json(json!({
        "lines": lines,
        "done": job.done.load(Ordering::SeqCst),
        "exit_code": job.exit_code.lock().unwrap().clone(),
        "image": job.image,
    })))
}

// ---------- 节点级共享 MySQL ----------

async fn mysql_list(_a: NodeAuth, State(state): State<AppState>) -> AppResult<Json<Value>> {
    Ok(Json(mysql::list_services(&state).await?))
}

async fn mysql_enable(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(ver): Path<String>,
    headers: HeaderMap,
) -> AppResult<Json<Value>> {
    let tid = tid_of(&headers);
    if !tid.is_empty() {
        state.tasks.start(&tid, &format!("启用 MySQL {ver} 服务"));
        state.tasks.log(&tid, "检查本地镜像与端口（绝不自动拉取）…");
    }
    match mysql::enable_mysql(&state, &ver, &tid).await {
        Ok(v) => {
            let port = v.get("host_port").cloned().unwrap_or(Value::Null);
            if !tid.is_empty() {
                state
                    .tasks
                    .finish(&tid, true, &format!("✔ MySQL {ver} 已启用，外网端口 {port}"));
            }
            Ok(Json(v))
        }
        Err(e) => {
            if !tid.is_empty() {
                state
                    .tasks
                    .finish(&tid, false, &format!("✘ 启用失败：{}", e.detail));
            }
            Err(e)
        }
    }
}

async fn mysql_disable(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(ver): Path<String>,
    headers: HeaderMap,
    body: Option<Json<Value>>,
) -> AppResult<Json<Value>> {
    let tid = tid_of(&headers);
    let purge = body
        .as_ref()
        .and_then(|b| b.get("purge"))
        .and_then(|p| p.as_bool())
        .unwrap_or(false);
    if !tid.is_empty() {
        state.tasks.start(
            &tid,
            &format!(
                "停用 MySQL {ver} 服务{}",
                if purge { "（同时删除数据卷）" } else { "" }
            ),
        );
    }
    match mysql::disable_mysql(&state, &ver, purge, &tid).await {
        Ok(v) => {
            if !tid.is_empty() {
                let d = v
                    .get("detail")
                    .and_then(|x| x.as_str())
                    .unwrap_or("已停用")
                    .to_string();
                state.tasks.finish(&tid, true, &format!("✔ {d}"));
            }
            Ok(Json(v))
        }
        Err(e) => {
            if !tid.is_empty() {
                state
                    .tasks
                    .finish(&tid, false, &format!("✘ 停用失败：{}", e.detail));
            }
            Err(e)
        }
    }
}

async fn mysql_root_password(
    _a: NodeAuth,
    State(state): State<AppState>,
    Path(ver): Path<String>,
) -> AppResult<Json<Value>> {
    let row = mysql::get_svc_row(&state.db, &ver).await?;
    Ok(Json(json!({
        "version": ver,
        "root_password": row.root_password,
        "host_port": row.host_port,
    })))
}

// 说明：/instances/{id}/site 与 /instances/{id}/pma 在 Python agent.py 中并不存在
// （面板侧是进程内直接调用 caddy_service / pma_service）。为保持端点逐一对齐，
// 这里不额外暴露 HTTP 路由，仅通过 services::{site,pma} 的公共函数供 /panel/* 使用。

// ---------- 路由表 ----------

pub fn routes() -> Router<crate::state::AppState> {
    Router::new()
        // 心跳
        .route("/health", get(health))
        // 实例
        .route("/instances", get(list_instances).post(create_instance))
        .route(
            "/instances/{instance_id}",
            get(instance_detail)
                .patch(instance_update)
                .delete(delete_instance),
        )
        .route("/instances/{instance_id}/start", post(instance_start))
        .route("/instances/{instance_id}/stop", post(instance_stop))
        .route("/instances/{instance_id}/restart", post(instance_restart))
        .route("/instances/{instance_id}/reset", post(instance_reset))
        .route(
            "/instances/{instance_id}/runtime",
            get(instance_runtime).post(instance_switch_runtime),
        )
        .route("/instances/{instance_id}/stats", get(instance_stats))
        .route("/instances/{instance_id}/logs", get(instance_logs))
        .route("/instances/{instance_id}/deps/install", post(instance_install))
        .route(
            "/instances/{instance_id}/panel-token",
            post(panel_token).delete(panel_revoke),
        )
        // 实例文件（24 个端点）
        .route("/instances/{instance_id}/files", get(list_files).delete(delete_path))
        .route("/instances/{instance_id}/files/content", get(file_content))
        .route("/instances/{instance_id}/files/upload", put(upload_file))
        .route("/instances/{instance_id}/files/save", post(save_file))
        .route("/instances/{instance_id}/files/mkdir", post(make_dir))
        .route("/instances/{instance_id}/files/rename", post(rename_path))
        .route("/instances/{instance_id}/files/download", get(download_file))
        .route("/instances/{instance_id}/files/backup", get(backup_download))
        .route("/instances/{instance_id}/files/restore", post(restore_backup))
        .route("/instances/{instance_id}/files/copy", post(copy_path))
        .route("/instances/{instance_id}/files/zip", post(zip_paths))
        .route("/instances/{instance_id}/files/unzip", post(unzip_path))
        .route("/instances/{instance_id}/files/path-size", get(path_size))
        .route("/instances/{instance_id}/files/preview", get(preview_image))
        .route(
            "/instances/{instance_id}/files/upload-exists",
            post(upload_exists),
        )
        .route("/instances/{instance_id}/files/upload-chunk", post(upload_chunk))
        // 宿主机防火墙
        .route("/host/stats", get(host_stats))
        .route("/host/firewall", get(firewall_status))
        .route("/host/firewall/allow", post(firewall_allow))
        .route("/host/firewall/deny", post(firewall_deny))
        .route("/host/firewall/delete", post(firewall_delete))
        .route("/host/firewall/toggle", post(firewall_toggle))
        // Docker 运维（最终挂到 /agent/docker/*）
        .route("/docker/info", get(docker_info))
        .route("/docker/containers", get(list_containers))
        .route("/docker/containers/{cid}/start", post(container_start))
        .route("/docker/containers/{cid}/stop", post(container_stop))
        .route("/docker/containers/{cid}/restart", post(container_restart))
        .route("/docker/containers/{cid}", delete(container_remove))
        .route("/docker/containers/{cid}/logs", get(container_logs))
        .route("/docker/images", get(list_images))
        .route("/docker/images/{*reference}", delete(remove_image))
        .route("/docker/images/prune", post(prune_images))
        .route("/docker/images/pull", post(pull_image))
        .route("/docker/settings", get(get_settings).put(put_settings))
        .route("/docker/df", get(docker_df))
        .route("/docker/system/prune", post(system_prune))
        .route("/docker/php-images/build", post(php_image_build))
        .route(
            "/docker/php-images/build/{job_id}",
            get(php_image_build_status),
        )
        .route("/docker/mysql", get(mysql_list))
        .route("/docker/mysql/{ver}/enable", post(mysql_enable))
        .route("/docker/mysql/{ver}/disable", post(mysql_disable))
        .route(
            "/docker/mysql/{ver}/root-password",
            get(mysql_root_password),
        )
}
