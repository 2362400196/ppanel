//! 节点运维路由（移植 app/routers/{backup,upgrade,host_files,host_tasks,open_api}.py）：
//!   /agent/host/backups*      容器导出 / 目录打包 / 数据库导出与恢复（NodeOrApi）
//!   /agent/host/cos           腾讯云 COS 存储设置（NodeOrApi）
//!   /agent/host/backup-jobs*  定时备份任务 CRUD + 立即执行（NodeOrApi）
//!   /agent/host/files*        宿主机文件管理（NodeAuth，root='/' 复用 file 服务）
//!   /agent/host/tasks/lines   任务终端日志增量拉取（NodeOrApi）
//!   /agent/version|upgrade*   在线升级（NodeAuth）
//!   /open/*                   开放开通面（X-API-Key，未配置 OPEN_API_KEY 时整组禁用）
//!
//! 重活全部落在 services::{backup,cos,file,instance,provision}，本层只做参数校验与响应整形。

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Command;

use axum::body::Body;
use axum::extract::{Multipart, Path as AxPath, Query, State};
use axum::http::{header, HeaderMap};
use axum::response::Response;
use axum::routing::{get, post};
use axum::{Json, Router};
use chrono::Duration as ChronoDuration;
use serde::Deserialize;
use serde_json::{json, Value};
use tokio_util::io::ReaderStream;

use crate::auth::{api_key_ok, NodeAuth, NodeOrApi};
use crate::config::settings;
use crate::error::{AppError, AppJson, AppResult};
use crate::models::{opt_dt_iso, utcnow, HostBackupJob};
use crate::services::{backup, cos, file, instance, provision};
use crate::state::AppState;

// ---------- 通用小工具 ----------

fn tid_of(h: &HeaderMap) -> String {
    h.get("x-task-id")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string()
}

fn qp(q: &HashMap<String, String>, key: &str, default: &str) -> String {
    q.get(key).cloned().unwrap_or_else(|| default.to_string())
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
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "svg" => "image/svg+xml",
        "zip" => "application/zip",
        "gz" | "tgz" => "application/gzip",
        "sql" => "application/sql",
        _ => "application/octet-stream",
    }
}

async fn stream_file(path: PathBuf, filename: String) -> AppResult<Response> {
    let f = tokio::fs::File::open(&path)
        .await
        .map_err(|_| AppError::not_found("文件不存在"))?;
    let len = f.metadata().await.map(|m| m.len()).unwrap_or(0);
    let body = Body::from_stream(ReaderStream::new(f));
    Response::builder()
        .header(header::CONTENT_TYPE, mime_of(&filename))
        .header(header::CONTENT_DISPOSITION, disposition(&filename))
        .header(header::CONTENT_LENGTH, len.to_string())
        .body(body)
        .map_err(|e| AppError::internal(format!("响应构造失败：{e}")))
}

/// JSON body 取值（PATCH 局部更新用，null 视为未提供）。
fn jget<'a>(v: &'a Value, k: &str) -> Option<&'a Value> {
    v.get(k).filter(|x| !x.is_null())
}

fn tail_chars(s: &str, n: usize) -> String {
    let chars: Vec<char> = s.chars().collect();
    if chars.len() <= n {
        s.to_string()
    } else {
        chars[chars.len() - n..].iter().collect()
    }
}

// =====================================================================
// 备份文件：列表 / 下载 / 删除
// =====================================================================

async fn list_backups(_a: NodeOrApi, Query(q): Query<HashMap<String, String>>) -> Json<Value> {
    let page: i64 = qp(&q, "page", "0").parse().unwrap_or(0);
    let page_size: i64 = qp(&q, "page_size", "20").parse().unwrap_or(20);
    Json(backup::list_backups(page, page_size))
}

async fn download_backup(
    _a: NodeOrApi,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Response> {
    let name = qp(&q, "file", "");
    let target = backup::safe_path(&name)?;
    if !target.is_file() {
        return Err(AppError::not_found("备份文件不存在"));
    }
    stream_file(target, name).await
}

async fn delete_backup(
    _a: NodeOrApi,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let name = qp(&q, "file", "");
    let target = backup::safe_path(&name)?;
    if target.is_file() {
        std::fs::remove_file(&target)?;
        return Ok(Json(json!({"detail": format!("已删除 {name}")})));
    }
    Err(AppError::not_found("备份文件不存在"))
}

async fn backup_tables(
    _a: NodeOrApi,
    State(state): State<AppState>,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let ver = qp(&q, "version", "");
    let dbname = qp(&q, "db_name", "");
    Ok(Json(backup::list_tables(&state, &ver, &dbname).await?))
}

async fn backup_dbs(_a: NodeOrApi, State(state): State<AppState>) -> AppResult<Json<Value>> {
    Ok(Json(backup::backup_db_options(&state).await?))
}

#[derive(Debug, Deserialize)]
struct ContainerIn {
    container_id: String,
}

#[derive(Debug, Deserialize)]
struct DirIn {
    container_id: String,
    path: String,
}

#[derive(Debug, Deserialize)]
struct DirRestoreIn {
    file: String,
    container_id: String,
    path: String,
}

#[derive(Debug, Deserialize)]
struct DbIn {
    version: String,
    db_name: String,
    #[serde(default)]
    tables: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct RestoreContainerIn {
    file: String,
    name: String,
}

#[derive(Debug, Deserialize)]
struct RestoreDbIn {
    file: String,
    version: String,
    db_name: String,
}

async fn backup_container_ep(
    _a: NodeOrApi,
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<ContainerIn>,
) -> AppResult<Json<Value>> {
    Ok(Json(
        backup::backup_container(&state, &body.container_id, &tid_of(&headers)).await?,
    ))
}

async fn backup_dir_ep(
    _a: NodeOrApi,
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<DirIn>,
) -> AppResult<Json<Value>> {
    let p = body.path.trim().trim_end_matches('/').to_string();
    if p.contains("..") || !backup::path_ok(&p) {
        return Err(AppError::bad_request("目录路径无效（需为容器内绝对路径）"));
    }
    Ok(Json(
        backup::backup_dir(&state, &body.container_id, &p, &tid_of(&headers)).await?,
    ))
}

async fn restore_dir_ep(
    _a: NodeOrApi,
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<DirRestoreIn>,
) -> AppResult<Json<Value>> {
    Ok(Json(
        backup::restore_dir(
            &state,
            &body.file,
            &body.container_id,
            &body.path,
            &tid_of(&headers),
        )
        .await?,
    ))
}

async fn backup_database_ep(
    _a: NodeOrApi,
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<DbIn>,
) -> AppResult<Json<Value>> {
    let tables: Vec<String> = body
        .tables
        .iter()
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty())
        .collect();
    Ok(Json(
        backup::backup_database(&state, &body.version, &body.db_name, &tables, &tid_of(&headers))
            .await?,
    ))
}

async fn restore_container_ep(
    _a: NodeOrApi,
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<RestoreContainerIn>,
) -> AppResult<Json<Value>> {
    Ok(Json(
        backup::restore_container(&state, &body.file, &body.name, &tid_of(&headers)).await?,
    ))
}

async fn restore_database_ep(
    _a: NodeOrApi,
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<RestoreDbIn>,
) -> AppResult<Json<Value>> {
    Ok(Json(
        backup::restore_database(&state, &body.file, &body.version, &body.db_name, &tid_of(&headers))
            .await?,
    ))
}

// =====================================================================
// 腾讯云 COS 存储设置（节点级单行配置）
// =====================================================================

#[derive(Debug, Deserialize)]
struct CosIn {
    #[serde(default)]
    secret_id: String,
    #[serde(default)]
    secret_key: String,
    #[serde(default)]
    bucket: String,
    #[serde(default)]
    region: String,
    #[serde(default = "default_prefix")]
    prefix: String,
    #[serde(default = "one")]
    keep_local: i64,
    #[serde(default)]
    enabled: i64,
}

fn default_prefix() -> String {
    "ppanel-backups".to_string()
}
fn one() -> i64 {
    1
}

async fn get_cos(_a: NodeOrApi, State(state): State<AppState>) -> AppResult<Json<Value>> {
    let cfg = cos::get_config(&state.db).await?;
    let key = cfg.secret_key.clone();
    Ok(Json(json!({
        "secret_id": cfg.secret_id,
        "secret_key_masked": cos::masked_key(&key),
        "has_key": !key.is_empty(),
        "bucket": cfg.bucket,
        "region": cfg.region,
        "prefix": cfg.prefix,
        "keep_local": cfg.keep_local != 0,
        "enabled": cfg.enabled != 0,
    })))
}

async fn put_cos(
    _a: NodeOrApi,
    State(state): State<AppState>,
    Json(body): Json<CosIn>,
) -> AppResult<Json<Value>> {
    let mut cfg = cos::get_config(&state.db).await?;
    if !body.secret_id.trim().is_empty() {
        cfg.secret_id = body.secret_id.trim().to_string();
    }
    if !body.secret_key.trim().is_empty() {
        cfg.secret_key = body.secret_key.trim().to_string();
    }
    cfg.bucket = body.bucket.trim().to_string();
    cfg.region = body.region.trim().to_string();
    let p = body.prefix.trim().to_string();
    cfg.prefix = if p.is_empty() { "ppanel-backups".to_string() } else { p };
    cfg.keep_local = if body.keep_local != 0 { 1 } else { 0 };
    cfg.enabled = if body.enabled != 0 { 1 } else { 0 };
    write_cos(&state, &cfg).await?;

    if cfg.enabled != 0 {
        match cos::test_conn(&cfg).await {
            Ok(r) => Ok(Json(json!({
                "ok": true,
                "message": r.get("message").and_then(|v| v.as_str()).unwrap_or(""),
            }))),
            Err(e) => {
                // 连通性校验失败 → 保持关闭，避免「看似启用实际传不上去」
                cfg.enabled = 0;
                write_cos(&state, &cfg).await?;
                Err(AppError::bad_request(format!(
                    "配置已保存，但连通性校验失败，未启用：{e}"
                )))
            }
        }
    } else {
        Ok(Json(json!({"ok": true, "message": "配置已保存（未启用）"})))
    }
}

async fn write_cos(state: &AppState, cfg: &crate::models::CosConfig) -> AppResult<()> {
    sqlx::query(
        "UPDATE cos_config SET secret_id = ?, secret_key = ?, bucket = ?, region = ?, \
         prefix = ?, keep_local = ?, enabled = ?, updated_at = ? WHERE id = 1",
    )
    .bind(&cfg.secret_id)
    .bind(&cfg.secret_key)
    .bind(&cfg.bucket)
    .bind(&cfg.region)
    .bind(&cfg.prefix)
    .bind(cfg.keep_local)
    .bind(cfg.enabled)
    .bind(utcnow())
    .execute(&state.db)
    .await?;
    Ok(())
}

// =====================================================================
// 定时备份任务
// =====================================================================

#[derive(Debug, Deserialize)]
struct BackupJobIn {
    schedule: String,
    kind: String,
    target: String,
    #[serde(default)]
    dir_path: String,
    #[serde(default)]
    table_name: String,
    #[serde(default = "keep5")]
    keep: i64,
    #[serde(default = "local_dest")]
    dest: String,
}

fn keep5() -> i64 {
    5
}
fn local_dest() -> String {
    "local".to_string()
}

async fn list_backup_jobs(_a: NodeOrApi, State(state): State<AppState>) -> AppResult<Json<Value>> {
    let rows: Vec<HostBackupJob> =
        sqlx::query_as("SELECT * FROM host_backup_jobs ORDER BY created_at")
            .fetch_all(&state.db)
            .await?;
    Ok(Json(json!({
        "jobs": rows.iter().map(backup::job_out).collect::<Vec<_>>(),
    })))
}

async fn backup_jobs_meta(_a: NodeOrApi, State(state): State<AppState>) -> AppResult<Json<Value>> {
    Ok(Json(backup::jobs_meta(&state).await?))
}

async fn create_backup_job(
    _a: NodeOrApi,
    State(state): State<AppState>,
    Json(body): Json<BackupJobIn>,
) -> AppResult<Json<Value>> {
    backup::validate_job(
        &state,
        &body.schedule,
        &body.kind,
        &body.target,
        &body.dir_path,
        &body.table_name,
        body.keep,
        &body.dest,
    )
    .await?;

    // 表级任务但未选表 → 按整库（与 Python 版一致）
    let kind = if body.kind == "db_table" && body.table_name.trim().is_empty() {
        "db_full".to_string()
    } else {
        body.kind.clone()
    };
    let res = sqlx::query(
        "INSERT INTO host_backup_jobs (schedule, kind, target, dir_path, table_name, keep, dest, \
         enabled, last_status, last_output, created_at) VALUES (?,?,?,?,?,?,?,1,'','',?)",
    )
    .bind(body.schedule.trim())
    .bind(&kind)
    .bind(body.target.trim())
    .bind(body.dir_path.trim())
    .bind(body.table_name.trim())
    .bind(body.keep)
    .bind(&body.dest)
    .bind(utcnow())
    .execute(&state.db)
    .await?;
    let jid = res.last_insert_rowid();
    let job: HostBackupJob = sqlx::query_as("SELECT * FROM host_backup_jobs WHERE id = ?")
        .bind(jid)
        .fetch_one(&state.db)
        .await?;

    // 创建即跑一次首份备份；失败只写任务状态，不影响创建结果
    let st = state.clone();
    tokio::spawn(async move {
        let _ = backup::run_job(&st, jid, "").await;
    });
    Ok(Json(backup::job_out(&job)))
}

async fn update_backup_job(
    _a: NodeOrApi,
    State(state): State<AppState>,
    AxPath(jid): AxPath<i64>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    let mut job: HostBackupJob = sqlx::query_as("SELECT * FROM host_backup_jobs WHERE id = ?")
        .bind(jid)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::not_found("任务不存在"))?;

    if let Some(v) = jget(&body, "schedule") {
        let sched = v.as_str().unwrap_or("").trim().to_string();
        if let Some(err) = crate::services::cron::validate(&sched) {
            return Err(AppError::bad_request(err));
        }
        job.schedule = sched;
    }
    if let Some(v) = jget(&body, "enabled") {
        job.enabled = if v.as_bool().unwrap_or(false) { 1 } else { 0 };
    }
    if let Some(v) = jget(&body, "keep") {
        let keep = v.as_i64().ok_or_else(|| AppError::bad_request("保留份数无效"))?;
        if !(1..=100).contains(&keep) {
            return Err(AppError::bad_request("保留份数应为 1-100"));
        }
        job.keep = keep;
    }
    if let Some(v) = jget(&body, "dest") {
        let dest = v.as_str().unwrap_or("").to_string();
        if !backup::DESTS.contains(&dest.as_str()) {
            return Err(AppError::bad_request("备份目的地无效"));
        }
        if dest == "cos" || dest == "both" {
            let cfg = cos::get_config(&state.db).await?;
            if cfg.enabled == 0 {
                return Err(AppError::bad_request(
                    "腾讯云 COS 未启用，请先在「存储设置」中保存并启用",
                ));
            }
        }
        job.dest = dest;
    }

    sqlx::query("UPDATE host_backup_jobs SET schedule = ?, enabled = ?, keep = ?, dest = ? WHERE id = ?")
        .bind(&job.schedule)
        .bind(job.enabled)
        .bind(job.keep)
        .bind(&job.dest)
        .bind(jid)
        .execute(&state.db)
        .await?;
    Ok(Json(backup::job_out(&job)))
}

async fn delete_backup_job(
    _a: NodeOrApi,
    State(state): State<AppState>,
    AxPath(jid): AxPath<i64>,
) -> AppResult<Json<Value>> {
    let res = sqlx::query("DELETE FROM host_backup_jobs WHERE id = ?")
        .bind(jid)
        .execute(&state.db)
        .await?;
    if res.rows_affected() == 0 {
        return Err(AppError::not_found("任务不存在"));
    }
    Ok(Json(json!({"ok": true})))
}

async fn run_backup_job(
    _a: NodeOrApi,
    State(state): State<AppState>,
    AxPath(jid): AxPath<i64>,
    headers: HeaderMap,
) -> AppResult<Json<Value>> {
    let r = backup::run_job(&state, jid, &tid_of(&headers)).await;
    if r.get("ok").and_then(|v| v.as_bool()) != Some(true) {
        return Err(AppError::bad_gateway(
            r.get("error").and_then(|v| v.as_str()).unwrap_or("执行失败"),
        ));
    }
    Ok(Json(r))
}

// =====================================================================
// 宿主机文件管理（root = "/"，与实例文件同一套实现）
// =====================================================================

const HOST_ROOT: &str = "/";

/// 删除保护：解析后的真实路径不得命中系统关键目录。
fn guard(path: &str) -> AppResult<String> {
    let rel = format!("/{}", path.trim_start_matches('/'));
    let real = file::realpath_loose(Path::new(&rel));
    let real_s = real.to_string_lossy().to_string();
    const PROTECTED: [&str; 13] = [
        "/", "/boot", "/dev", "/etc", "/lib", "/proc", "/root", "/run", "/sbin", "/sys", "/usr",
        "/var", "/bin",
    ];
    if PROTECTED.contains(&real_s.as_str()) {
        return Err(AppError::forbidden("拒绝删除系统关键目录"));
    }
    Ok(path.to_string())
}

async fn host_list_files(
    _a: NodeAuth,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let path = qp(&q, "path", "/");
    let entries = file::list_dir(HOST_ROOT, &path)?;
    Ok(Json(json!({"path": path, "entries": entries})))
}

async fn host_file_content(
    _a: NodeAuth,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let path = qp(&q, "path", "/");
    let mut data = file::read_text(HOST_ROOT, &path)?;
    data["path"] = json!(path);
    Ok(Json(data))
}

async fn host_save_file(_a: NodeAuth, Json(body): Json<Value>) -> AppResult<Json<Value>> {
    let path = body
        .get("path")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| AppError::bad_request("参数缺失"))?
        .to_string();
    let content = body
        .get("content")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    crate::error::blocking(move || file::write_text(HOST_ROOT, &path, &content)).await?;
    Ok(Json(json!({"detail": "已保存"})))
}

async fn host_mkdir(_a: NodeAuth, Json(body): Json<Value>) -> AppResult<Json<Value>> {
    let path = body
        .get("path")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| AppError::bad_request("参数缺失"))?
        .to_string();
    crate::error::blocking(move || file::mkdir(HOST_ROOT, &path)).await?;
    Ok(Json(json!({"detail": "已创建"})))
}

async fn host_rename(_a: NodeAuth, Json(body): Json<Value>) -> AppResult<Json<Value>> {
    let src = body.get("src").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let dst = body.get("dst").and_then(|v| v.as_str()).unwrap_or("").to_string();
    if src.is_empty() || dst.is_empty() {
        return Err(AppError::bad_request("参数缺失"));
    }
    crate::error::blocking(move || file::rename(HOST_ROOT, &src, &dst)).await?;
    Ok(Json(json!({"detail": "已重命名"})))
}

async fn host_copy(_a: NodeAuth, Json(body): Json<Value>) -> AppResult<Json<Value>> {
    let src = body.get("src").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let dst = body.get("dst").and_then(|v| v.as_str()).unwrap_or("").to_string();
    if src.is_empty() || dst.is_empty() {
        return Err(AppError::bad_request("参数缺失"));
    }
    crate::error::blocking(move || file::copy_path(HOST_ROOT, &src, &dst)).await?;
    Ok(Json(json!({"detail": "已复制"})))
}

async fn host_delete(
    _a: NodeAuth,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let path = guard(&qp(&q, "path", ""))?;
    crate::error::blocking(move || file::delete(HOST_ROOT, &path)).await?;
    Ok(Json(json!({"detail": "已删除"})))
}

async fn host_download(
    _a: NodeAuth,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Response> {
    let path = qp(&q, "path", "");
    let target = file::resolve_path(HOST_ROOT, &path)?;
    if !target.is_file() {
        return Err(AppError::bad_request("仅支持下载文件"));
    }
    let name = target
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "download".to_string());
    stream_file(target, name).await
}

async fn host_zip(_a: NodeAuth, Json(body): Json<Value>) -> AppResult<Json<Value>> {
    let paths: Vec<String> = body
        .get("paths")
        .and_then(|v| v.as_array())
        .map(|a| a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect())
        .unwrap_or_default();
    let name = body.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let z_type = body
        .get("z_type")
        .and_then(|v| v.as_str())
        .unwrap_or("tar.gz")
        .to_string();
    if paths.is_empty() || name.is_empty() {
        return Err(AppError::bad_request("参数缺失"));
    }
    crate::error::blocking(move || file::zip_entries(HOST_ROOT, &paths, &name, &z_type)).await?;
    Ok(Json(json!({"detail": "压缩完成"})))
}

async fn host_unzip(_a: NodeAuth, Json(body): Json<Value>) -> AppResult<Json<Value>> {
    let src = body.get("src").and_then(|v| v.as_str()).unwrap_or("").to_string();
    if src.is_empty() {
        return Err(AppError::bad_request("参数缺失"));
    }
    let dest = crate::error::blocking(move || file::unzip_entry(HOST_ROOT, &src)).await?;
    Ok(Json(json!({"detail": "解压完成", "dest": dest})))
}

async fn host_path_size(
    _a: NodeAuth,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let path = qp(&q, "path", "");
    let size = file::dir_size(HOST_ROOT, &path)?;
    Ok(Json(json!({"size": size})))
}

async fn host_preview(
    _a: NodeAuth,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let path = qp(&q, "path", "");
    Ok(Json(file::image_preview(HOST_ROOT, &path)?))
}

async fn host_upload_exists(_a: NodeAuth, Json(body): Json<Value>) -> AppResult<Json<Value>> {
    let file_name = body
        .get("file_name")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| AppError::bad_request("参数缺失"))?
        .to_string();
    let path = body.get("path").and_then(|v| v.as_str()).unwrap_or("/").to_string();
    let total = body.get("total_size").and_then(|v| v.as_i64()).unwrap_or(0);
    Ok(Json(file::upload_exists(HOST_ROOT, &path, &file_name, total)?))
}

async fn host_upload_chunk(_a: NodeAuth, mut multipart: Multipart) -> AppResult<Json<Value>> {
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
    let d = data.ok_or_else(|| AppError::bad_request("未收到分片数据"))?;
    let out = crate::error::blocking(move || {
        file::upload_chunk(HOST_ROOT, &path, &file_name, total_size, start, &d)
    })
    .await?;
    Ok(Json(out))
}

async fn host_task_lines(
    _a: NodeOrApi,
    State(state): State<AppState>,
    Query(q): Query<HashMap<String, String>>,
) -> Json<Value> {
    let tid = qp(&q, "task_id", "");
    let since: usize = qp(&q, "since", "0").parse().unwrap_or(0);
    Json(state.tasks.lines(&tid, since))
}

// =====================================================================
// 在线升级（git 部署才可升级）
// =====================================================================

/// 向上查找 .git 得到部署仓库根（找不到说明非 git 部署）。
fn repo_root() -> String {
    let mut starts: Vec<PathBuf> = Vec::new();
    if let Ok(d) = std::env::current_dir() {
        starts.push(d);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(p) = exe.parent() {
            starts.push(p.to_path_buf());
        }
    }
    for start in starts {
        let mut d = Some(start);
        while let Some(cur) = d {
            if cur.join(".git").is_dir() {
                return cur.to_string_lossy().to_string();
            }
            d = cur.parent().map(|p| p.to_path_buf());
        }
    }
    String::new()
}

fn run_cmd(cmd: &[&str], cwd: Option<&str>) -> (bool, String) {
    let mut c = Command::new(cmd[0]);
    if let Some(dir) = cwd {
        c.current_dir(dir);
    }
    c.args(&cmd[1..]);
    match c.output() {
        Ok(o) => {
            let mut out = String::from_utf8_lossy(&o.stdout).to_string();
            out.push_str(&String::from_utf8_lossy(&o.stderr));
            (o.status.success(), tail_chars(out.trim(), 500))
        }
        Err(e) => (false, tail_chars(&e.to_string(), 300)),
    }
}

fn git(root: &str, args: &[&str]) -> (bool, String) {
    let mut full: Vec<&str> = vec!["git", "-C", root];
    full.extend_from_slice(args);
    run_cmd(&full, None)
}

async fn version(_a: NodeAuth) -> Json<Value> {
    let root = repo_root();
    if root.is_empty() {
        return Json(json!({
            "mode": "unknown",
            "upgradable": false,
            "reason": "非 git 部署（手动拷贝/容器），请用安装脚本部署以支持在线升级",
        }));
    }
    let (ok1, cur) = git(&root, &["rev-parse", "--short", "HEAD"]);
    let (ok2, br) = git(&root, &["rev-parse", "--abbrev-ref", "HEAD"]);
    let (ok3, ls) = git(&root, &["ls-remote", "origin", "HEAD"]);
    let remote = if ok3 && !ls.trim().is_empty() {
        ls.split_whitespace().next().map(|s| s.chars().take(7).collect::<String>())
    } else {
        None
    };
    let cur_v = if ok1 { Some(cur.trim().to_string()) } else { None };
    let upgradable = match (&cur_v, &remote) {
        (Some(c), Some(r)) => c != r,
        _ => false,
    };
    Json(json!({
        "mode": "git",
        "branch": if ok2 { br.trim().to_string() } else { String::new() },
        "current": cur_v,
        "remote": remote,
        "upgradable": upgradable,
    }))
}

async fn upgrade_status(_a: NodeAuth, State(state): State<AppState>) -> Json<Value> {
    Json(state.upgrade.status_json())
}

async fn upgrade_start(_a: NodeAuth, State(state): State<AppState>) -> AppResult<Json<Value>> {
    {
        let mut g = state.upgrade.inner.lock().unwrap();
        if g.running {
            return Ok(Json(json!({"detail": "升级正在进行中", "running": true})));
        }
        g.running = true;
        g.stage = "拉取代码".to_string();
        g.log.clear();
        g.ok = None;
        g.error.clear();
        g.started_at = now_secs();
        g.finished_at = 0.0;
    }
    let st = state.clone();
    tokio::task::spawn_blocking(move || upgrade_pipeline(&st));
    Ok(Json(json!({
        "detail": "升级已开始，节点将在完成后自动重启（约半分钟）",
        "running": true,
    })))
}

fn now_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn set_stage(state: &AppState, stage: &str) {
    state.upgrade.inner.lock().unwrap().stage = stage.to_string();
}

fn finish_upgrade(state: &AppState, ok: bool, error: &str, stage: &str) {
    let mut g = state.upgrade.inner.lock().unwrap();
    g.ok = Some(ok);
    if !error.is_empty() {
        g.error = error.chars().take(300).collect();
    }
    g.stage = stage.to_string();
    g.finished_at = now_secs();
    g.running = false;
}

/// 升级流水线：拉代码 → 编译（Rust 版用 cargo 取代 Python 版的 uv sync + 导入自检）→ 重启服务。
fn upgrade_pipeline(state: &AppState) {
    let root = repo_root();
    if root.is_empty() {
        state.upgrade.log("非 git 部署，无法在线升级");
        finish_upgrade(state, false, "非 git 部署，无法在线升级", "失败");
        return;
    }
    let backend = format!("{root}/agent/backend");
    let (ok_old, old_hash) = git(&root, &["rev-parse", "--short", "HEAD"]);
    let old_hash = if ok_old { old_hash.trim().to_string() } else { String::new() };

    let res: Result<(), String> = (|| {
        set_stage(state, "拉取代码");
        state.upgrade.log("恢复本地改动、清理残留状态…");
        let _ = git(&root, &["merge", "--abort"]);
        let _ = git(&root, &["checkout", "--", "."]);
        let (mut ok, mut out) = git(&root, &["pull", "--ff-only"]);
        if !ok {
            state.upgrade.log(&format!("pull 失败，硬对齐远程：{}", tail_chars(&out, 200)));
            let (ok_b, br) = git(&root, &["rev-parse", "--abbrev-ref", "HEAD"]);
            let _ = git(&root, &["fetch", "origin"]);
            let branch = if ok_b && !br.trim().is_empty() {
                format!("origin/{}", br.trim())
            } else {
                "origin/main".to_string()
            };
            let r = git(&root, &["reset", "--hard", &branch]);
            ok = r.0;
            out = r.1;
        }
        if !ok {
            return Err(format!("代码拉取失败：{out}"));
        }
        let (okc, cur) = git(&root, &["rev-parse", "--short", "HEAD"]);
        let ver = if okc { cur.trim().to_string() } else { "?".to_string() };
        state.upgrade.log(&format!("代码已更新到 {ver}"));

        set_stage(state, "编译");
        if Path::new(&backend).join("Cargo.toml").is_file() {
            state.upgrade.log("cargo build --release（首次编译耗时较长）…");
            let (bok, bout) = run_cmd(&["cargo", "build", "--release"], Some(&backend));
            if !bok {
                if !old_hash.is_empty() {
                    state.upgrade.log(&format!("回滚代码到 {old_hash}…"));
                    let _ = git(&root, &["reset", "--hard", &old_hash]);
                    let _ = run_cmd(&["cargo", "build", "--release"], Some(&backend));
                    state.upgrade.log("已回滚：服务继续运行旧版本，可排查后重试升级");
                }
                return Err(format!("编译失败，已回滚到升级前版本：{}", tail_chars(&bout, 150)));
            }
            state.upgrade.log("编译通过");
        } else {
            state.upgrade.log("未找到 Cargo.toml，跳过编译");
        }

        set_stage(state, "重启服务");
        let svc = std::env::var("PPANEL_AGENT_SERVICE").unwrap_or_else(|_| "ppanel-agent".to_string());
        if host_has("systemctl") {
            let _ = Command::new("systemctl").args(["restart", &svc]).spawn();
            state.upgrade.log(&format!("已触发 systemctl restart {svc}"));
        } else {
            state.upgrade.log("无 systemd 环境，请手动重启 agent 进程生效");
        }
        Ok(())
    })();

    match res {
        Ok(()) => finish_upgrade(state, true, "", "完成"),
        Err(e) => {
            state.upgrade.log(&format!("升级失败：{e}"));
            finish_upgrade(state, false, &e, "失败");
        }
    }
}

fn host_has(bin: &str) -> bool {
    std::env::var("PATH")
        .map(|p| {
            p.split(if cfg!(windows) { ';' } else { ':' })
                .any(|d| Path::new(d).join(bin).is_file())
        })
        .unwrap_or(false)
}

// =====================================================================
// 开放开通面 /open/*（X-API-Key，未配置 OPEN_API_KEY 时整组禁用）
// =====================================================================

fn open_api_guard(h: &HeaderMap) -> AppResult<()> {
    if settings().open_api_key.is_empty() {
        return Err(AppError::forbidden("开通接口未启用（未配置 OPEN_API_KEY）"));
    }
    let key = h.get("x-api-key").and_then(|v| v.to_str().ok()).unwrap_or("");
    if api_key_ok(key) {
        Ok(())
    } else {
        Err(AppError::unauthorized("API Key 无效"))
    }
}

/// 以提取器形式做开放面鉴权：声明在 `Json` 之前，保证「未配置 OPEN_API_KEY → 403」
/// 优先于 body 反序列化错误返回（与 Python 版 `Depends(_auth)` 的执行顺序一致）。
pub struct OpenApiAuth;

impl axum::extract::FromRequestParts<AppState> for OpenApiAuth {
    type Rejection = AppError;

    async fn from_request_parts(
        parts: &mut axum::http::request::Parts,
        _s: &AppState,
    ) -> Result<Self, Self::Rejection> {
        open_api_guard(&parts.headers)?;
        Ok(OpenApiAuth)
    }
}

fn panel_url(h: &HeaderMap) -> String {
    if !settings().panel_public_url.is_empty() {
        return settings().panel_public_url.trim_end_matches('/').to_string();
    }
    let proto = h
        .get("x-forwarded-proto")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("http");
    let host = h
        .get("host")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
        .unwrap_or_else(|| format!("127.0.0.1:{}", settings().agent_port));
    format!("{proto}://{host}/panel")
}

fn issued(inst: &crate::models::Instance, h: &HeaderMap) -> Value {
    json!({
        "iid": inst.id,
        "name": inst.name,
        "panel_url": panel_url(h),
        "panel_token": inst.panel_token,
        "ext_port": inst.ext_port,
        "expire_at": opt_dt_iso(&inst.expire_at),
        "owner_ref": inst.owner_ref,
    })
}

async fn op_log(state: &AppState, iid: i64, action: &str, detail: &str) {
    let _ = sqlx::query(
        "INSERT INTO agent_op_logs (instance_id, action, detail, created_at) VALUES (?,?,?,?)",
    )
    .bind(iid)
    .bind(action)
    .bind(detail)
    .bind(utcnow())
    .execute(&state.db)
    .await;
}

#[derive(Debug, Deserialize)]
struct ProvisionIn {
    name: String,
    image: String,
    #[serde(default = "days30")]
    days: i64,
    #[serde(default = "cpu1")]
    cpu: f64,
    #[serde(default = "mem512")]
    mem: i64,
    #[serde(default = "disk2048")]
    disk_quota: i64,
    #[serde(default)]
    start_cmd: String,
    #[serde(default)]
    owner_ref: String,
}

fn days30() -> i64 {
    30
}
fn cpu1() -> f64 {
    1.0
}
fn mem512() -> i64 {
    512
}
fn disk2048() -> i64 {
    2048
}

async fn open_provision(
    State(state): State<AppState>,
    _auth: OpenApiAuth,
    headers: HeaderMap,
    AppJson(body): AppJson<ProvisionIn>,
) -> AppResult<Json<Value>> {
    let name = body.name.trim().to_string();
    if name.is_empty() || name.chars().count() > 64 {
        return Err(AppError::bad_request("名称长度应为 1-64"));
    }
    if body.image.is_empty() || body.image.chars().count() > 128 {
        return Err(AppError::bad_request("镜像长度应为 1-128"));
    }
    if !(1..=3650).contains(&body.days) {
        return Err(AppError::bad_request("有效期天数应为 1-3650"));
    }
    if !(body.cpu > 0.0 && body.cpu <= 8.0) {
        return Err(AppError::bad_request("CPU 上限应为 (0,8]"));
    }
    if !(64..=16384).contains(&body.mem) {
        return Err(AppError::bad_request("内存应为 64-16384 MB"));
    }
    if !(128..=102400).contains(&body.disk_quota) {
        return Err(AppError::bad_request("磁盘配额应为 128-102400 MB"));
    }

    let allowed = instance::allowed_images(&state).await;
    if !allowed.contains(&body.image) {
        return Err(AppError::bad_request("不支持的镜像版本"));
    }

    // 默认启动命令不落库：空 = 平台默认（entry_cmd 按入口文件判断），保持「空=默认」语义统一
    let start_cmd = body.start_cmd.trim().to_string();

    let req = provision::ProvisionReq {
        user_id: 1, // Agent 本地库占位账号
        name,
        image: body.image.clone(),
        start_cmd,
        cpu: body.cpu,
        mem: body.mem,
        disk_quota: body.disk_quota,
        note: String::new(),
        owner_ref: body.owner_ref.trim().to_string(),
        expire_at: Some(utcnow() + ChronoDuration::days(body.days)),
        traffic_gb: None,
        issue_panel_token: true,
    };
    let inst = provision::create_instance(&state, req, "open.provision").await?;
    op_log(
        &state,
        inst.id,
        "open.provision",
        &format!("商城开通 days={} owner_ref={}", body.days, inst.owner_ref),
    )
    .await;
    Ok(Json(issued(&inst, &headers)))
}

async fn open_query(
    State(state): State<AppState>,
    _auth: OpenApiAuth,
    headers: HeaderMap,
    AxPath(iid): AxPath<i64>,
) -> AppResult<Json<Value>> {
    let mut inst = instance::fetch_instance(&state.db, iid).await?;
    instance::sync_status(&state, &mut inst).await;
    instance::persist_status(&state.db, &inst).await?;
    let mut data = issued(&inst, &headers);
    let stats = instance::container_stats(&state, &inst).await;
    data["status"] = json!(inst.status);
    data["image"] = json!(inst.image);
    data["cpu_limit"] = json!(inst.cpu_limit);
    data["mem_limit"] = json!(inst.mem_limit);
    data["stats"] = stats;
    Ok(Json(data))
}

#[derive(Debug, Deserialize)]
struct RenewIn {
    days: i64,
}

async fn open_renew(
    State(state): State<AppState>,
    _auth: OpenApiAuth,
    AxPath(iid): AxPath<i64>,
    AppJson(body): AppJson<RenewIn>,
) -> AppResult<Json<Value>> {
    if !(1..=3650).contains(&body.days) {
        return Err(AppError::bad_request("有效期天数应为 1-3650"));
    }
    let _inst = instance::fetch_instance(&state.db, iid).await?;
    let now = utcnow();
    let base: Option<chrono::NaiveDateTime> =
        sqlx::query_scalar("SELECT expire_at FROM instances WHERE id = ?")
            .bind(iid)
            .fetch_optional(&state.db)
            .await?
            .flatten();
    let base = match base {
        Some(e) if e > now => e,
        _ => now,
    };
    let new_expire = base + ChronoDuration::days(body.days);
    sqlx::query("UPDATE instances SET expire_at = ? WHERE id = ?")
        .bind(new_expire)
        .bind(iid)
        .execute(&state.db)
        .await?;
    op_log(
        &state,
        iid,
        "open.renew",
        &format!("续期 {} 天 → {}", body.days, crate::models::dt_iso(&new_expire)),
    )
    .await;
    Ok(Json(json!({
        "iid": iid,
        "expire_at": crate::models::dt_iso(&new_expire),
    })))
}

async fn open_reclaim(
    State(state): State<AppState>,
    _auth: OpenApiAuth,
    AxPath(iid): AxPath<i64>,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let purge_raw = qp(&q, "purge", "0");
    let purge = purge_raw == "1" || purge_raw.eq_ignore_ascii_case("true");
    let inst = instance::fetch_instance(&state.db, iid).await?;
    provision::delete_instance_full(&state, &inst, purge).await?;
    op_log(&state, iid, "open.reclaim", &format!("商城回收 purge={purge}")).await;
    Ok(Json(json!({"ok": true, "iid": iid, "purged": purge})))
}

async fn open_token_reset(
    State(state): State<AppState>,
    _auth: OpenApiAuth,
    headers: HeaderMap,
    AxPath(iid): AxPath<i64>,
) -> AppResult<Json<Value>> {
    let _ = instance::fetch_instance(&state.db, iid).await?;
    let token = provision::reset_panel_token(&state.db, iid).await?;
    op_log(&state, iid, "open.token_reset", "商城重置面板令牌").await;
    let inst = instance::fetch_instance(&state.db, iid).await?;
    let mut v = issued(&inst, &headers);
    v["panel_token"] = json!(token);
    Ok(Json(v))
}

// =====================================================================
// 路由表
// =====================================================================

/// 挂在 /agent 下的运维端点（备份 / COS / 定时任务 / 宿主机文件 / 升级）。
pub fn routes() -> Router<AppState> {
    Router::new()
        // 备份文件
        .route("/host/backups", get(list_backups).delete(delete_backup))
        .route("/host/backups/download", get(download_backup))
        .route("/host/backups/tables", get(backup_tables))
        .route("/host/backups/dbs", get(backup_dbs))
        .route("/host/backups/container", post(backup_container_ep))
        .route("/host/backups/dir", post(backup_dir_ep))
        .route("/host/backups/database", post(backup_database_ep))
        .route("/host/backups/restore/dir", post(restore_dir_ep))
        .route("/host/backups/restore/container", post(restore_container_ep))
        .route("/host/backups/restore/database", post(restore_database_ep))
        // COS 存储
        .route("/host/cos", get(get_cos).put(put_cos))
        // 定时备份
        .route(
            "/host/backup-jobs",
            get(list_backup_jobs).post(create_backup_job),
        )
        .route("/host/backup-jobs/meta", get(backup_jobs_meta))
        .route(
            "/host/backup-jobs/{jid}",
            axum::routing::patch(update_backup_job).delete(delete_backup_job),
        )
        .route("/host/backup-jobs/{jid}/run", post(run_backup_job))
        // 宿主机文件
        .route("/host/files", get(host_list_files).delete(host_delete))
        .route("/host/files/content", get(host_file_content))
        .route("/host/files/save", post(host_save_file))
        .route("/host/files/mkdir", post(host_mkdir))
        .route("/host/files/rename", post(host_rename))
        .route("/host/files/copy", post(host_copy))
        .route("/host/files/download", get(host_download))
        .route("/host/files/zip", post(host_zip))
        .route("/host/files/unzip", post(host_unzip))
        .route("/host/files/path-size", get(host_path_size))
        .route("/host/files/preview", get(host_preview))
        .route("/host/files/upload-exists", post(host_upload_exists))
        .route("/host/files/upload-chunk", post(host_upload_chunk))
        // 任务终端
        .route("/host/tasks/lines", get(host_task_lines))
        // 在线升级
        .route("/version", get(version))
        .route("/upgrade/status", get(upgrade_status))
        .route("/upgrade", post(upgrade_start))
}

/// 开放开通面（不带 /agent 前缀，直接挂根）。
pub fn open_routes() -> Router<AppState> {
    Router::new()
        .route("/open/provision", post(open_provision))
        .route("/open/instances/{iid}", get(open_query).delete(open_reclaim))
        .route("/open/instances/{iid}/renew", post(open_renew))
        .route("/open/instances/{iid}/token-reset", post(open_token_reset))
}
