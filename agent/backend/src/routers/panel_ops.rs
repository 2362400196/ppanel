//! 独立面板扩展路由（移植 app/routers/panel_ops.py）：
//! 租户自助备份、定时任务、站点防护、phpMyAdmin、聚合概览、流量明细、
//! 容器内命令执行、重建容器、Node / Go 依赖管理。
//!
//! 安全边界（与 Python 版一致，核心三条）：
//!   - 备份/恢复的数据库固定为本实例登记库（表清单服务端枚举，备份表名必须是其子集）
//!   - 备份/恢复的容器固定为本实例容器
//!   - 备份文件列表/下载/删除按实例前缀过滤，跨实例文件不可见不可操作
//! 鉴权：复用 panel.rs 的 PanelInst 提取器（Bearer / ?t=）。

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::OnceLock;

use axum::body::Body;
use axum::extract::{Path as AxPath, Query, State};
use axum::http::{header, HeaderMap};
use axum::response::Response;
use axum::routing::{get, post};
use axum::{Json, Router};
use bollard::exec::{CreateExecOptions, StartExecOptions, StartExecResults};
use futures_util::StreamExt;
use regex::Regex;
use serde_json::{json, Value};
use tokio_util::io::ReaderStream;

use crate::error::{AppError, AppResult};
use crate::models::{round1, Instance, InstanceCron};
use crate::services::{backup, catalog, cron, instance, mysql, pma, site};
use crate::routers::panel::PanelInst;
use crate::state::AppState;

// ---------- 小工具 ----------

fn sfield(v: &Value, k: &str) -> String {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string()
}

fn qp(q: &HashMap<String, String>, k: &str, d: &str) -> String {
    q.get(k).cloned().unwrap_or_else(|| d.to_string())
}

async fn stream_file(path: PathBuf, filename: String) -> AppResult<Response> {
    let f = tokio::fs::File::open(&path)
        .await
        .map_err(|_| AppError::internal("文件读取失败"))?;
    let len = f.metadata().await.map(|m| m.len()).unwrap_or(0);
    let body = Body::from_stream(ReaderStream::new(f));
    Response::builder()
        .header(header::CONTENT_TYPE, "application/octet-stream")
        .header(
            header::CONTENT_DISPOSITION,
            format!(
                "attachment; filename=\"{}\"; filename*=UTF-8''{}",
                urlencoding::encode(&filename),
                urlencoding::encode(&filename)
            ),
        )
        .header(header::CONTENT_LENGTH, len.to_string())
        .body(body)
        .map_err(|e| AppError::internal(format!("响应构造失败：{e}")))
}

/// 本实例备份文件全路径：必须匹配本实例前缀（隔离其他实例与节点文件）。
fn own_file(iid: i64, file: &str) -> AppResult<PathBuf> {
    let path = backup::safe_path(file)?;
    let mut ok = file.starts_with(&format!("ppanel-{iid}_")) || file.starts_with(&format!("ppanel_{iid}_"));
    if ok && file.ends_with(".tar") {
        // 容器整包备份仅管理员（节点面板）可用
        ok = false;
    }
    if !ok || !path.is_file() {
        return Err(AppError::not_found("备份文件不存在"));
    }
    Ok(path)
}

fn has_prefix(iid: i64, name: &str) -> bool {
    name.starts_with(&format!("ppanel-{iid}_")) || name.starts_with(&format!("ppanel_{iid}_"))
}

/// 容器是否运行（面板语义：不存在/异常都视为未运行）。
async fn running(state: &AppState, cname: &str) -> bool {
    let Ok(docker) = state.docker.get().await else {
        return false;
    };
    match docker.inspect_container(cname, None).await {
        Ok(info) => info.state.as_ref().and_then(|s| s.running).unwrap_or(false),
        Err(_) => false,
    }
}

/// 容器内执行并取 stdout（非 0 退出码报 500，等价 Python `_php_exec` / `_node_exec`）。
async fn exec_stdout(
    state: &AppState,
    inst: &Instance,
    cmd: Vec<String>,
    workdir: Option<&str>,
) -> AppResult<String> {
    let docker = state.docker.get().await?;
    let cname = instance::container_name(inst.id);
    let exec = docker
        .create_exec(
            &cname,
            CreateExecOptions {
                cmd: Some(cmd),
                attach_stdout: Some(true),
                attach_stderr: Some(true),
                working_dir: workdir.map(|s| s.to_string()),
                ..Default::default()
            },
        )
        .await?;
    let exec_id = exec.id;
    let start = docker
        .start_exec(
            &exec_id,
            Some(StartExecOptions { detach: false, tty: false, output_capacity: None }),
        )
        .await?;
    let mut out = String::new();
    if let StartExecResults::Attached { mut output, .. } = start {
        while let Some(item) = output.next().await {
            if let Ok(line) = item {
                match line {
                    bollard::container::LogOutput::StdOut { message }
                    | bollard::container::LogOutput::Console { message } => {
                        out.push_str(&String::from_utf8_lossy(&message));
                    }
                    _ => {}
                }
            }
        }
    }
    let code = docker
        .inspect_exec(&exec_id)
        .await
        .ok()
        .and_then(|i| i.exit_code)
        .unwrap_or(-1);
    if code != 0 {
        return Err(AppError::internal("容器内命令执行失败"));
    }
    Ok(out.replace("\r\n", "\n").replace('\r', ""))
}

async fn exec_stdout_script(state: &AppState, inst: &Instance, script: &str) -> AppResult<String> {
    exec_stdout(
        state,
        inst,
        vec!["sh".into(), "-c".into(), script.to_string()],
        Some("/app"),
    )
    .await
}

fn ensure_no_running_job(state: &AppState, iid: i64) -> AppResult<()> {
    if state.jobs.has_running(iid) {
        return Err(AppError::conflict("已有任务正在执行，请等待完成后再试"));
    }
    Ok(())
}

// =====================================================================
// 备份：信息 / 列表 / 下载 / 删除
// =====================================================================

async fn backup_info(PanelInst(inst): PanelInst, State(state): State<AppState>) -> AppResult<Json<Value>> {
    let iid = inst.id;
    let mut out = json!({"container_running": false, "db": Value::Null});
    out["container_running"] = json!(running(&state, &instance::container_name(iid)).await);

    if let Some(reg) = mysql::get_instance_db(&state, iid).await? {
        let ver = reg["version"].as_str().unwrap_or("").to_string();
        let db_name = reg["db_name"].as_str().unwrap_or("").to_string();
        let mut db_running = false;
        let mut tables: Vec<String> = Vec::new();
        if running(&state, &mysql::svc_name(&ver)).await {
            db_running = true;
            if let Ok(v) = backup::list_tables(&state, &ver, &db_name).await {
                tables = v["tables"]
                    .as_array()
                    .map(|a| a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect())
                    .unwrap_or_default();
            }
        }
        out["db"] = json!({
            "version": ver,
            "db_name": db_name,
            "running": db_running,
            "tables": tables,
        });
    }
    Ok(Json(out))
}

async fn backup_list(PanelInst(inst): PanelInst) -> AppResult<Json<Value>> {
    let iid = inst.id;
    let root = backup::backup_root();
    let dir = std::path::Path::new(&root);
    let _ = std::fs::create_dir_all(dir);
    let mut items: Vec<Value> = Vec::new();
    if let Ok(rd) = std::fs::read_dir(dir) {
        for ent in rd.flatten() {
            let name = ent.file_name().to_string_lossy().to_string();
            if !has_prefix(iid, &name) || name.ends_with(".tar") {
                continue;
            }
            let Ok(md) = ent.metadata() else { continue };
            if !md.is_file() {
                continue;
            }
            let kind = if name.ends_with(".sql.gz") {
                "database"
            } else if name.ends_with(".tar.gz") {
                "dir"
            } else {
                "container"
            };
            let created = md
                .modified()
                .ok()
                .map(|t| {
                    let dt: chrono::DateTime<chrono::Local> = t.into();
                    dt.format("%Y-%m-%d %H:%M").to_string()
                })
                .unwrap_or_default();
            items.push(json!({
                "file": name,
                "kind": kind,
                "size": md.len(),
                "created_at": created,
            }));
        }
    }
    items.sort_by(|a, b| {
        let ka = a["created_at"].as_str().unwrap_or("");
        let kb = b["created_at"].as_str().unwrap_or("");
        kb.cmp(ka)
    });
    Ok(Json(json!({"backups": items})))
}

async fn backup_download(
    PanelInst(inst): PanelInst,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Response> {
    let file = qp(&q, "file", "");
    let path = own_file(inst.id, &file)?;
    stream_file(path, file).await
}

async fn backup_delete(
    PanelInst(inst): PanelInst,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let file = qp(&q, "file", "");
    let path = own_file(inst.id, &file)?;
    std::fs::remove_file(path)?;
    Ok(Json(json!({"detail": format!("已删除 {file}")})))
}

// =====================================================================
// 备份：数据库（固定本库） / 目录（固定本容器） / 恢复
// =====================================================================

/// 备份后台任务 ID：panelbk_{instance_id}_{ts}（task-lines 端点按前缀校验归属）。
fn backup_tid(iid: i64) -> String {
    format!("panelbk_{}_{}", iid, backup::ts())
}

/// 大文件备份可能耗时数分钟，改为后台任务：立即返回 task_id，前端轮询日志。
async fn backup_db(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    let reg = mysql::get_instance_db(&state, inst.id)
        .await?
        .ok_or_else(|| AppError::bad_request("实例尚未开通数据库，请先在「数据库」页开通"))?;
    let ver = reg["version"].as_str().unwrap_or("").to_string();
    let db_name = reg["db_name"].as_str().unwrap_or("").to_string();

    // 表名必须来自 SHOW TABLES 结果（服务端枚举，杜绝任意表注入）
    let real = backup::list_tables(&state, &ver, &db_name).await?;
    let real_set: std::collections::HashSet<String> = real["tables"]
        .as_array()
        .map(|a| a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect())
        .unwrap_or_default();

    let tables: Vec<String> = body
        .get("tables")
        .and_then(|v| v.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|x| x.as_str().map(|s| s.trim().to_string()))
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_default();
    for t in &tables {
        if !backup::table_ok(t) || !real_set.contains(t) {
            return Err(AppError::bad_request(format!("表不存在：{t}")));
        }
    }

    let tid = backup_tid(inst.id);
    state.tasks.start(&tid, &format!("备份数据库 {db_name}"));
    let st = state.clone();
    let tid2 = tid.clone();
    tokio::spawn(async move {
        // backup_database 内部已带 start/finish 与逐步日志，失败会自行 finish
        let _ = backup::backup_database(&st, &ver, &db_name, &tables, &tid2).await;
    });
    Ok(Json(json!({
        "task_id": tid,
        "detail": "备份任务已提交，正在后台执行，可在下方日志区查看进度",
    })))
}

async fn backup_dir(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    let p = {
        let raw = sfield(&body, "path").trim().trim_end_matches('/').to_string();
        if raw.is_empty() { "/app".to_string() } else { raw }
    };
    if p.contains("..") || !backup::path_ok(&p) {
        return Err(AppError::bad_request("目录路径无效（需为容器内绝对路径）"));
    }
    let cname = instance::container_name(inst.id);
    if !running(&state, &cname).await {
        return Err(AppError::bad_gateway("实例容器未运行"));
    }
    // 大目录打包耗时较长：后台执行 + 日志轮询，避免请求挂死、失败无从得知
    let tid = backup_tid(inst.id);
    state.tasks.start(&tid, &format!("备份网站目录 {p}"));
    let st = state.clone();
    let cname2 = cname.clone();
    let tid2 = tid.clone();
    tokio::spawn(async move {
        let _ = backup::backup_dir(&st, &cname2, &p, &tid2).await;
    });
    Ok(Json(json!({
        "task_id": tid,
        "detail": "备份任务已提交，正在后台执行，可在下方日志区查看进度",
    })))
}

/// 备份/恢复后台任务的日志增量拉取（按实例前缀校验，防越权读他人日志）。
async fn backup_task_lines(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Query(q): Query<HashMap<String, String>>,
) -> AppResult<Json<Value>> {
    let tid = qp(&q, "task_id", "");
    if !tid.starts_with(&format!("panelbk_{}_", inst.id)) {
        return Err(AppError::bad_request("任务 ID 无效"));
    }
    let since = qp(&q, "since", "0").parse::<usize>().unwrap_or(0);
    Ok(Json(state.tasks.lines(&tid, since)))
}

async fn restore_dir(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    let file = sfield(&body, "file");
    own_file(inst.id, &file)?;
    if !file.ends_with(".tar.gz") {
        return Err(AppError::bad_request("目录备份必须为 .tar.gz 文件"));
    }
    let p = {
        let raw = sfield(&body, "path").trim().trim_end_matches('/').to_string();
        if raw.is_empty() { "/app".to_string() } else { raw }
    };
    if p.contains("..") || !backup::path_ok(&p) {
        return Err(AppError::bad_request("目标目录无效（需为容器内绝对路径）"));
    }
    let cname = instance::container_name(inst.id);
    if !running(&state, &cname).await {
        return Err(AppError::bad_gateway("实例容器未运行"));
    }
    // 大包上传 + 解压耗时较长：后台执行 + 日志轮询
    let tid = backup_tid(inst.id);
    let st = state.clone();
    let cname2 = cname.clone();
    let p2 = p.clone();
    let file2 = file.clone();
    let tid2 = tid.clone();
    tokio::spawn(async move {
        let _ = backup::restore_dir(&st, &file2, &cname2, &p2, &tid2).await;
    });
    Ok(Json(json!({
        "task_id": tid,
        "detail": "恢复任务已提交，正在后台执行，可在下方日志区查看进度",
    })))
}

async fn restore_db(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    let file = sfield(&body, "file");
    own_file(inst.id, &file)?;
    if !file.ends_with(".sql.gz") {
        return Err(AppError::bad_request("数据库备份必须为 .sql.gz 文件"));
    }
    let reg = mysql::get_instance_db(&state, inst.id)
        .await?
        .ok_or_else(|| AppError::bad_request("实例尚未开通数据库，请先在「数据库」页开通"))?;
    let ver = reg["version"].as_str().unwrap_or("").to_string();
    let db_name = reg["db_name"].as_str().unwrap_or("").to_string();
    // 导入大库耗时较长：后台执行 + 日志轮询
    let tid = backup_tid(inst.id);
    let st = state.clone();
    let tid2 = tid.clone();
    tokio::spawn(async move {
        let _ = backup::restore_database(&st, &file, &ver, &db_name, &tid2).await;
    });
    Ok(Json(json!({
        "task_id": tid,
        "detail": "恢复任务已提交，正在后台执行，可在下方日志区查看进度",
    })))
}

// =====================================================================
// 安全快照：破坏性操作（重置/删除/删库）前自动生成的整目录快照
// 文件名：inst{id}_{cname}_{ts}_predestroy.tar.gz，仅本实例可见
// =====================================================================

/// 快照文件归属与格式校验（safe_path 已拦路径穿越）。
fn own_snapshot(iid: i64, file: &str) -> AppResult<PathBuf> {
    let path = backup::safe_path(file)?;
    let ok = file.starts_with(&format!("inst{iid}_"))
        && file.ends_with("_predestroy.tar.gz")
        && path.is_file();
    if !ok {
        return Err(AppError::not_found("快照不存在"));
    }
    Ok(path)
}

async fn snapshot_list(PanelInst(inst): PanelInst) -> AppResult<Json<Value>> {
    let iid = inst.id;
    let root = backup::backup_root();
    let mut items: Vec<Value> = Vec::new();
    if let Ok(rd) = std::fs::read_dir(std::path::Path::new(&root)) {
        for ent in rd.flatten() {
            let name = ent.file_name().to_string_lossy().to_string();
            if !name.starts_with(&format!("inst{iid}_")) || !name.ends_with("_predestroy.tar.gz") {
                continue;
            }
            let Ok(md) = ent.metadata() else { continue };
            if !md.is_file() {
                continue;
            }
            let created = md
                .modified()
                .ok()
                .map(|t| {
                    let dt: chrono::DateTime<chrono::Local> = t.into();
                    dt.format("%Y-%m-%d %H:%M").to_string()
                })
                .unwrap_or_default();
            items.push(json!({
                "file": name,
                "size": md.len(),
                "created_at": created,
            }));
        }
    }
    items.sort_by(|a, b| {
        b["created_at"]
            .as_str()
            .unwrap_or("")
            .cmp(a["created_at"].as_str().unwrap_or(""))
    });
    Ok(Json(json!({ "snapshots": items })))
}

async fn snapshot_restore(
    PanelInst(mut inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    let file = sfield(&body, "file");
    own_snapshot(inst.id, &file)?;
    let out = instance::restore_snapshot(&state, &mut inst, &file).await?;
    Ok(Json(json!({
        "detail": format!("已从快照恢复数据目录，实例状态：{}", out["status"].as_str().unwrap_or("unknown"))
    })))
}

// =====================================================================
// 定时任务
// =====================================================================

fn cron_out(r: &InstanceCron) -> Value {
    json!({
        "id": r.id,
        "schedule": r.schedule,
        "command": r.command,
        "kind": if r.kind.is_empty() { "command".to_string() } else { r.kind.clone() },
        "path": r.path,
        "dest": if r.dest.is_empty() { "local".to_string() } else { r.dest.clone() },
        "enabled": r.enabled != 0,
        "last_run": r.last_run.map(|t| t.format("%m-%d %H:%M").to_string()).unwrap_or_default(),
        "last_status": r.last_status,
        "last_output": tail_chars(&r.last_output, 400),
    })
}

fn tail_chars(s: &str, n: usize) -> String {
    let chars: Vec<char> = s.chars().collect();
    if chars.len() <= n {
        s.to_string()
    } else {
        chars[chars.len() - n..].iter().collect()
    }
}

async fn crons_list(PanelInst(inst): PanelInst, State(state): State<AppState>) -> AppResult<Json<Value>> {
    let rows: Vec<InstanceCron> =
        sqlx::query_as("SELECT * FROM instance_crons WHERE instance_id = ? ORDER BY created_at")
            .bind(inst.id)
            .fetch_all(&state.db)
            .await?;
    Ok(Json(json!({"crons": rows.iter().map(cron_out).collect::<Vec<_>>()})))
}

async fn crons_create(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    let schedule = sfield(&body, "schedule");
    if let Some(err) = cron::validate(&schedule) {
        return Err(AppError::bad_request(format!("cron 表达式无效：{err}")));
    }
    let kind = {
        let k = sfield(&body, "kind");
        if k.is_empty() { "command".to_string() } else { k }
    };
    if !matches!(kind.as_str(), "command" | "dir" | "db" | "both") {
        return Err(AppError::bad_request("任务类型无效"));
    }
    let cmd = sfield(&body, "command").trim().to_string();
    if kind == "command" && (cmd.is_empty() || cmd.chars().count() > 512) {
        return Err(AppError::bad_request("命令不能为空且不超过 512 字符"));
    }
    let path = normalize_cron_path(&sfield(&body, "path"), &kind)?;
    let dest = normalize_cron_dest(&state, &sfield(&body, "dest"), &kind).await?;
    let res = sqlx::query(
        "INSERT INTO instance_crons (instance_id, schedule, command, kind, path, dest, enabled, \
         last_status, last_output, created_at) VALUES (?,?,?,?,?,?,1,'','',?)",
    )
    .bind(inst.id)
    .bind(schedule.trim())
    .bind(&cmd)
    .bind(&kind)
    .bind(&path)
    .bind(&dest)
    .bind(crate::models::utcnow())
    .execute(&state.db)
    .await?;
    let row: InstanceCron = sqlx::query_as("SELECT * FROM instance_crons WHERE id = ?")
        .bind(res.last_insert_rowid())
        .fetch_one(&state.db)
        .await?;
    Ok(Json(cron_out(&row)))
}

/// 备份类任务的目标目录校验与默认值（kind=db 时忽略路径）。
fn normalize_cron_path(raw: &str, kind: &str) -> AppResult<String> {
    if kind != "dir" && kind != "both" {
        return Ok(String::new());
    }
    let p = raw.trim().trim_end_matches('/');
    let p = if p.is_empty() { "/app" } else { p };
    if p.contains("..") || !backup::path_ok(p) {
        return Err(AppError::bad_request("目录路径无效（需为容器内绝对路径，如 /app/uploads）"));
    }
    Ok(p.to_string())
}

/// 备份去向校验：local/cos/both；备份类任务才允许非 local，且 COS 需已启用。
async fn normalize_cron_dest(state: &AppState, raw: &str, kind: &str) -> AppResult<String> {
    let d = if raw.trim().is_empty() { "local" } else { raw.trim() };
    if !matches!(d, "local" | "cos" | "both") {
        return Err(AppError::bad_request("备份去向无效（local / cos / both）"));
    }
    if (kind == "command") && d != "local" {
        return Err(AppError::bad_request("命令任务没有备份去向，请保持本地"));
    }
    if d != "local" {
        let cfg = crate::services::cos::get_config(&state.db).await?;
        if cfg.enabled == 0 {
            return Err(AppError::bad_request(
                "腾讯云 COS 未启用，请先在「备份」页配置并启用，或选择备份到本地",
            ));
        }
    }
    Ok(d.to_string())
}

async fn crons_patch(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    AxPath(cid): AxPath<i64>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    let found: Option<InstanceCron> = sqlx::query_as("SELECT * FROM instance_crons WHERE id = ?")
        .bind(cid)
        .fetch_optional(&state.db)
        .await?;
    let mut row = found
        .filter(|r| r.instance_id == inst.id)
        .ok_or_else(|| AppError::not_found("任务不存在"))?;
    if row.kind.is_empty() {
        row.kind = "command".to_string();
    }
    if row.dest.is_empty() {
        row.dest = "local".to_string();
    }
    if let Some(v) = body.get("schedule").filter(|v| !v.is_null()) {
        let schedule = v.as_str().unwrap_or("").to_string();
        if let Some(err) = cron::validate(&schedule) {
            return Err(AppError::bad_request(format!("cron 表达式无效：{err}")));
        }
        row.schedule = schedule.trim().to_string();
    }
    if let Some(v) = body.get("kind").filter(|v| !v.is_null()) {
        let k = v.as_str().unwrap_or("command").to_string();
        if !matches!(k.as_str(), "command" | "dir" | "db" | "both") {
            return Err(AppError::bad_request("任务类型无效"));
        }
        row.kind = k;
    }
    if let Some(v) = body.get("command").filter(|v| !v.is_null()) {
        let cmd = v.as_str().unwrap_or("").trim().to_string();
        if cmd.chars().count() > 512 {
            return Err(AppError::bad_request("命令不超过 512 字符"));
        }
        row.command = cmd;
    }
    if let Some(v) = body.get("path").filter(|v| !v.is_null()) {
        row.path = normalize_cron_path(v.as_str().unwrap_or(""), &row.kind)?;
    }
    if let Some(v) = body.get("dest").filter(|v| !v.is_null()) {
        row.dest = normalize_cron_dest(&state, v.as_str().unwrap_or(""), &row.kind).await?;
    }
    if row.kind == "command" && row.command.trim().is_empty() {
        return Err(AppError::bad_request("命令不能为空"));
    }
    if let Some(v) = body.get("enabled").filter(|v| !v.is_null()) {
        row.enabled = if v.as_bool().unwrap_or(false) { 1 } else { 0 };
    }
    sqlx::query(
        "UPDATE instance_crons SET schedule = ?, command = ?, kind = ?, path = ?, dest = ?, enabled = ? WHERE id = ?",
    )
    .bind(&row.schedule)
    .bind(&row.command)
    .bind(&row.kind)
    .bind(&row.path)
    .bind(&row.dest)
    .bind(row.enabled)
    .bind(cid)
    .execute(&state.db)
    .await?;
    Ok(Json(cron_out(&row)))
}

async fn crons_delete(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    AxPath(cid): AxPath<i64>,
) -> AppResult<Json<Value>> {
    let found: Option<InstanceCron> = sqlx::query_as("SELECT * FROM instance_crons WHERE id = ?")
        .bind(cid)
        .fetch_optional(&state.db)
        .await?;
    match found.filter(|r| r.instance_id == inst.id) {
        Some(_) => {
            sqlx::query("DELETE FROM instance_crons WHERE id = ?")
                .bind(cid)
                .execute(&state.db)
                .await?;
            Ok(Json(json!({"detail": "已删除"})))
        }
        None => Err(AppError::not_found("任务不存在")),
    }
}

async fn crons_run(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    AxPath(cid): AxPath<i64>,
) -> AppResult<Json<Value>> {
    let found: Option<InstanceCron> = sqlx::query_as("SELECT * FROM instance_crons WHERE id = ?")
        .bind(cid)
        .fetch_optional(&state.db)
        .await?;
    if found.filter(|r| r.instance_id == inst.id).is_none() {
        return Err(AppError::not_found("任务不存在"));
    }
    cron::spawn_run(state.clone(), cid);
    Ok(Json(json!({"detail": "已触发执行，稍后刷新查看结果"})))
}

// =====================================================================
// 站点防护
// =====================================================================

async fn site_get(PanelInst(inst): PanelInst, State(state): State<AppState>) -> AppResult<Json<Value>> {
    Ok(Json(site::site_out(&state, &inst).await?))
}

async fn site_put(
    PanelInst(mut inst): PanelInst,
    State(state): State<AppState>,
    Json(input): Json<site::SiteInput>,
) -> AppResult<Json<Value>> {
    if inst.domain.as_deref().unwrap_or("").trim().is_empty() {
        return Err(AppError::bad_request("请先在「域名与 SSL」绑定域名"));
    }
    let v = site::upsert_site(&state, &inst, input).await?;
    // 站点配置变更可能影响展示（domain/caddy 状态），刷新一次实例快照
    instance::sync_status(&state, &mut inst).await;
    Ok(Json(v))
}

// =====================================================================
// phpMyAdmin
// =====================================================================

fn host_of(h: &HeaderMap) -> String {
    h.get("host")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.split(':').next().unwrap_or("").to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "127.0.0.1".to_string())
}

async fn pma_enable(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    headers: HeaderMap,
) -> AppResult<Json<Value>> {
    Ok(Json(pma::enable(&state, &inst, &host_of(&headers)).await?))
}

async fn pma_disable(PanelInst(inst): PanelInst, State(state): State<AppState>) -> AppResult<Json<Value>> {
    Ok(Json(pma::disable(&state, inst.id).await?))
}

// =====================================================================
// 聚合概览 / 流量明细
// =====================================================================

async fn overview(
    PanelInst(mut inst): PanelInst,
    State(state): State<AppState>,
) -> AppResult<Json<Value>> {
    instance::sync_status(&state, &mut inst).await;
    instance::persist_status(&state.db, &inst).await?;

    let database = mysql::get_instance_db(&state, inst.id)
        .await?
        .map(|r| {
            json!({
                "version": r["version"],
                "db_name": r["db_name"],
                "db_user": r["db_user"],
            })
        })
        .unwrap_or(Value::Null);

    let s = site::site_out(&state, &inst).await?;
    let site_part = json!({
        "mode": s["mode"],
        "domain": s["domain"],
        "caddy": s["caddy"],
    });

    let pma_state = pma::status(&state, inst.id).await;

    let mut n_backups = 0usize;
    if let Ok(rd) = std::fs::read_dir(backup::backup_root()) {
        for ent in rd.flatten() {
            let name = ent.file_name().to_string_lossy().to_string();
            if has_prefix(inst.id, &name) {
                n_backups += 1;
            }
        }
    }
    let n_crons: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM instance_crons WHERE instance_id = ?")
        .bind(inst.id)
        .fetch_one(&state.db)
        .await
        .unwrap_or(0);

    Ok(Json(json!({
        "instance": crate::routers::panel::panel_out_public(&inst),
        "database": database,
        "site": site_part,
        "pma": pma_state,
        "counts": {"backups": n_backups, "crons": n_crons},
    })))
}

async fn traffic(PanelInst(inst): PanelInst) -> Json<Value> {
    let gb = inst.traffic_gb;
    let used = round1(inst.traffic_used_mb);
    let remaining = gb.map(|g| round1((g * 1024.0 - used).max(0.0)));
    let pct = match gb {
        Some(g) if g > 0.0 => Some(round1((used / (g * 1024.0) * 100.0).min(100.0))),
        _ => None,
    };
    Json(json!({
        "limit_gb": gb,
        "used_mb": used,
        "remaining_mb": remaining,
        "used_percent": pct,
        "month": inst.traffic_month,
    }))
}

// =====================================================================
// 容器内命令执行 / 重建容器
// =====================================================================

async fn exec_cmd(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    let command = sfield(&body, "command");
    if command.is_empty() || command.chars().count() > 4096 {
        return Err(AppError::bad_request("命令不能为空且不超过 4096 字符"));
    }
    let timeout = body
        .get("timeout")
        .and_then(|v| v.as_i64())
        .unwrap_or(30)
        .clamp(1, 120) as u64;
    let cname = instance::container_name(inst.id);
    if !running(&state, &cname).await {
        return Err(AppError::bad_gateway("实例容器未运行"));
    }
    let docker = state.docker.get().await?;
    let fut = backup::exec_demux(
        &docker,
        &cname,
        vec!["/bin/sh".into(), "-c".into(), command.clone()],
    );
    let res = tokio::time::timeout(std::time::Duration::from_secs(timeout), fut).await;
    let (code, stdout, stderr) = match res {
        Err(_) => {
            return Ok(Json(json!({
                "timed_out": true,
                "detail": format!(
                    "命令超过 {timeout}s 未结束，已放弃等待（进程仍在容器内运行）"
                ),
            })))
        }
        Ok(Err(e)) => return Err(AppError::bad_gateway(format!("执行失败：{}", e.detail))),
        Ok(Ok(v)) => v,
    };
    instance::log_op(&state.db, inst.id, "exec", &command.chars().take(200).collect::<String>()).await?;
    Ok(Json(json!({
        "timed_out": false,
        "exit_code": code,
        "stdout": truncate(&stdout, 65536),
        "stderr": truncate(&stderr, 65536),
    })))
}

fn truncate(s: &str, max: usize) -> String {
    if s.len() <= max {
        s.to_string()
    } else {
        s.char_indices().take_while(|(i, _)| *i < max).map(|(_, c)| c).collect()
    }
}

async fn rebuild(
    PanelInst(mut inst): PanelInst,
    State(state): State<AppState>,
) -> AppResult<Json<Value>> {
    instance::recreate_container(&state, &inst).await?;
    instance::start_instance(&state, &mut inst).await?;
    instance::diagnose_startup(&state, &inst).await?;
    instance::sync_status(&state, &mut inst).await;
    instance::log_op(&state.db, inst.id, "rebuild", "").await?;
    instance::persist_status(&state.db, &inst).await?;
    Ok(Json(json!({
        "status": inst.status,
        "container_id": inst.container_id,
    })))
}

// =====================================================================
// Node 依赖管理（npm / pnpm）
// =====================================================================

fn require_node(inst: &Instance) -> AppResult<()> {
    if instance::runtime_kind(&inst.image) != "node" {
        return Err(AppError::bad_request("该功能仅支持 Node 环境"));
    }
    Ok(())
}

fn npm_name_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(r"^(@[A-Za-z0-9._\-]+/)?[A-Za-z0-9._\-]+(@[A-Za-z0-9._\-^~><=*,.+]+)?$")
            .expect("内置正则编译失败")
    })
}

fn npm_url_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(r"^https?://[A-Za-z0-9.\-_:]+(:\d{2,5})?(/[A-Za-z0-9.\-_/]*)?$")
            .expect("内置正则编译失败")
    })
}

const NPM_OFFICIAL: &str = "https://registry.npmjs.org";

/// 从宿主机 node_modules/<name>/package.json 读已装版本（容器未运行也能检测）。
fn pkg_json_version(host_dir: &str, name: &str) -> Option<String> {
    let mut p = PathBuf::from(host_dir);
    p.push("node_modules");
    for seg in name.split('/') {
        p.push(seg);
    }
    p.push("package.json");
    let text = std::fs::read_to_string(&p).ok()?;
    let v: Value = serde_json::from_str(&text).ok()?;
    Some(v.get("version").and_then(|x| x.as_str()).unwrap_or("").to_string())
}

async fn npm_registry_of(state: &AppState, inst: &Instance) -> String {
    match exec_stdout_script(state, inst, "npm config get registry 2>/dev/null").await {
        Ok(out) => {
            let t = out.trim().to_string();
            if t.starts_with("http") { t } else { NPM_OFFICIAL.to_string() }
        }
        Err(_) => NPM_OFFICIAL.to_string(),
    }
}

async fn node_packages(PanelInst(inst): PanelInst, State(state): State<AppState>) -> AppResult<Json<Value>> {
    require_node(&inst)?;
    let host_dir = inst.host_dir.clone();
    let nm = PathBuf::from(&host_dir).join("node_modules");
    let mut installed: Vec<Value> = Vec::new();
    if nm.is_dir() {
        if let Ok(rd) = std::fs::read_dir(&nm) {
            for ent in rd.flatten() {
                let entry = ent.file_name().to_string_lossy().to_string();
                if entry.starts_with('.') {
                    continue;
                }
                let full = ent.path();
                let names: Vec<String> = if entry.starts_with('@') && full.is_dir() {
                    std::fs::read_dir(&full)
                        .map(|rd2| {
                            rd2.flatten()
                                .map(|e| format!("{entry}/{}", e.file_name().to_string_lossy()))
                                .collect()
                        })
                        .unwrap_or_default()
                } else {
                    vec![entry.clone()]
                };
                for name in names {
                    if let Some(ver) = pkg_json_version(&host_dir, &name) {
                        installed.push(json!({"name": name, "version": ver}));
                    }
                }
            }
        }
    }
    installed.sort_by(|a, b| {
        a["name"].as_str().unwrap_or("").cmp(b["name"].as_str().unwrap_or(""))
    });

    let catalog: Vec<Value> = catalog::node_pkgs()
        .into_iter()
        .map(|mut it| {
            let name = it.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string();
            it["installed"] = match pkg_json_version(&host_dir, &name) {
                Some(v) => json!(v),
                None => Value::Null,
            };
            it
        })
        .collect();

    Ok(Json(json!({
        "running": running(&state, &instance::container_name(inst.id)).await,
        "has_package_json": PathBuf::from(&host_dir).join("package.json").is_file(),
        "catalog": catalog,
        "installed": installed,
    })))
}

fn node_names(raw: &str) -> AppResult<Vec<String>> {
    let names: Vec<String> = raw
        .replace(',', " ")
        .split_whitespace()
        .map(|s| s.to_string())
        .collect();
    if names.is_empty() {
        return Err(AppError::bad_request("请输入依赖名称"));
    }
    for n in &names {
        if !npm_name_re().is_match(n) {
            return Err(AppError::bad_request(format!("非法的依赖名称：{n}")));
        }
    }
    Ok(names)
}

fn manager_of(body: &Value) -> AppResult<String> {
    let m = body
        .get("manager")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .unwrap_or("npm")
        .to_string();
    if m != "npm" && m != "pnpm" {
        return Err(AppError::bad_request("包管理器仅支持 npm / pnpm"));
    }
    Ok(m)
}

async fn node_install_pkgs(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    require_node(&inst)?;
    let manager = manager_of(&body)?;
    let names = node_names(body.get("names").and_then(|v| v.as_str()).unwrap_or(""))?;
    if !running(&state, &instance::container_name(inst.id)).await {
        return Err(AppError::conflict("实例未运行，请先启动实例"));
    }
    ensure_no_running_job(&state, inst.id)?;
    let reg = npm_registry_of(&state, &inst).await;
    let reg_flag = if reg != NPM_OFFICIAL { format!(" --registry={reg}") } else { String::new() };
    let joined = names.join(" ");
    let script = if manager == "pnpm" {
        format!(
            "command -v pnpm >/dev/null || npm install -g pnpm{reg_flag}\npnpm add {joined}{reg_flag}"
        )
    } else {
        format!("npm install {joined}{reg_flag}")
    };
    let job = instance::start_exec_job(
        &state,
        &inst,
        vec!["sh".into(), "-c".into(), script],
        &format!("{manager} install {joined}"),
    )
    .await?;
    instance::log_op(&state.db, inst.id, "node_deps_install", &joined).await?;
    Ok(Json(json!({"job_id": job.id})))
}

async fn node_install_from_pkg(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    require_node(&inst)?;
    let manager = manager_of(&body)?;
    if !PathBuf::from(&inst.host_dir).join("package.json").is_file() {
        return Err(AppError::bad_request("实例根目录没有 package.json（先在文件管理上传）"));
    }
    if !running(&state, &instance::container_name(inst.id)).await {
        return Err(AppError::conflict("实例未运行，请先启动实例"));
    }
    ensure_no_running_job(&state, inst.id)?;
    let reg = npm_registry_of(&state, &inst).await;
    let reg_flag = if reg != NPM_OFFICIAL { format!(" --registry={reg}") } else { String::new() };
    let script = if manager == "pnpm" {
        format!(
            "command -v pnpm >/dev/null || npm install -g pnpm{reg_flag}\npnpm install{reg_flag}"
        )
    } else {
        format!("npm install{reg_flag}")
    };
    let job = instance::start_exec_job(
        &state,
        &inst,
        vec!["sh".into(), "-c".into(), script],
        &format!("{manager} install"),
    )
    .await?;
    instance::log_op(&state.db, inst.id, "node_deps_install", "package.json").await?;
    Ok(Json(json!({"job_id": job.id})))
}

async fn node_uninstall_pkgs(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    require_node(&inst)?;
    let manager = manager_of(&body)?;
    let names = node_names(body.get("names").and_then(|v| v.as_str()).unwrap_or(""))?;
    if !running(&state, &instance::container_name(inst.id)).await {
        return Err(AppError::conflict("实例未运行，请先启动实例"));
    }
    ensure_no_running_job(&state, inst.id)?;
    let joined = names.join(" ");
    let script = if manager == "pnpm" {
        format!("command -v pnpm >/dev/null || npm install -g pnpm\npnpm remove {joined}")
    } else {
        format!("npm uninstall {joined}")
    };
    let job = instance::start_exec_job(
        &state,
        &inst,
        vec!["sh".into(), "-c".into(), script],
        &format!("{manager} remove {joined}"),
    )
    .await?;
    instance::log_op(&state.db, inst.id, "node_deps_uninstall", &joined).await?;
    Ok(Json(json!({"job_id": job.id})))
}

async fn npm_mirror_get(PanelInst(inst): PanelInst, State(state): State<AppState>) -> AppResult<Json<Value>> {
    require_node(&inst)?;
    let mirrors = catalog::npm_mirrors();
    if !running(&state, &instance::container_name(inst.id)).await {
        return Ok(Json(json!({"running": false, "current": Value::Null, "mirrors": mirrors})));
    }
    let cur = npm_registry_of(&state, &inst).await;
    Ok(Json(json!({"running": true, "current": cur, "mirrors": mirrors})))
}

async fn npm_mirror_put(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    require_node(&inst)?;
    let url = sfield(&body, "url").trim().to_string();
    if !npm_url_re().is_match(&url) {
        return Err(AppError::bad_request("镜像地址格式不正确（http/https 链接）"));
    }
    if !running(&state, &instance::container_name(inst.id)).await {
        return Err(AppError::conflict("实例未运行，请先启动实例"));
    }
    exec_stdout_script(&state, &inst, &format!("npm config set registry {url}")).await?;
    let named = catalog::mirror_name(catalog::npm_mirrors(), &url);
    instance::log_op(&state.db, inst.id, "npm_mirror", &named).await?;
    Ok(Json(json!({"current": url})))
}

// =====================================================================
// Go 依赖管理（go.mod / GOPROXY）
// =====================================================================

fn require_go(inst: &Instance) -> AppResult<()> {
    if instance::runtime_kind(&inst.image) != "go" {
        return Err(AppError::bad_request("该功能仅支持 Go 环境"));
    }
    Ok(())
}

fn go_url_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(r"^https?://[A-Za-z0-9.\-_:]+(:\d{2,5})?(/[A-Za-z0-9.\-_/]*)?(,direct)?$")
            .expect("内置正则编译失败")
    })
}

const GO_DEFAULT_PROXY: &str = "https://proxy.golang.org,direct";

/// 解析 go.mod：module 名 / go 版本 / require 依赖列表（无文件返回 None）。
fn parse_go_mod(path: &std::path::Path) -> Option<Value> {
    let text = std::fs::read_to_string(path).ok()?;
    let mut module = String::new();
    let mut go_ver = String::new();
    let mut requires: Vec<Value> = Vec::new();
    let mut in_block = false;
    for line in text.lines() {
        let s = line.trim();
        if s.is_empty() || s.starts_with("//") {
            continue;
        }
        if let Some(rest) = s.strip_prefix("module ") {
            module = rest.trim().to_string();
        } else if let Some(rest) = s.strip_prefix("go ") {
            go_ver = rest.trim().to_string();
        } else if s.starts_with("require (") {
            in_block = true;
        } else if in_block && s == ")" {
            in_block = false;
        } else if in_block {
            let parts: Vec<&str> = s.split_whitespace().collect();
            if parts.len() >= 2 {
                requires.push(json!({
                    "name": parts[0],
                    "version": parts[1],
                    "indirect": line.contains("// indirect"),
                }));
            }
        } else if s.starts_with("require ") {
            let parts: Vec<&str> = s.split_whitespace().collect();
            if parts.len() >= 3 {
                requires.push(json!({
                    "name": parts[1],
                    "version": parts[2],
                    "indirect": line.contains("// indirect"),
                }));
            }
        }
    }
    Some(json!({"module": module, "go": go_ver, "requires": requires}))
}

async fn go_mod_get(PanelInst(inst): PanelInst, State(state): State<AppState>) -> AppResult<Json<Value>> {
    require_go(&inst)?;
    let data = parse_go_mod(&PathBuf::from(&inst.host_dir).join("go.mod"));
    Ok(Json(json!({
        "running": running(&state, &instance::container_name(inst.id)).await,
        "go_mod": data,
        "proxies": catalog::go_proxies(),
    })))
}

async fn go_mod_tidy(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
) -> AppResult<Json<Value>> {
    require_go(&inst)?;
    if !running(&state, &instance::container_name(inst.id)).await {
        return Err(AppError::conflict("实例未运行，请先启动实例"));
    }
    ensure_no_running_job(&state, inst.id)?;
    let script = "[ -f go.mod ] || go mod init ppanel-app\ngo mod tidy";
    let job = instance::start_exec_job(
        &state,
        &inst,
        vec!["sh".into(), "-c".into(), script.to_string()],
        "go mod tidy",
    )
    .await?;
    instance::log_op(&state.db, inst.id, "go_mod_tidy", "").await?;
    Ok(Json(json!({"job_id": job.id})))
}

async fn go_proxy_get(PanelInst(inst): PanelInst, State(state): State<AppState>) -> AppResult<Json<Value>> {
    require_go(&inst)?;
    let proxies = catalog::go_proxies();
    if !running(&state, &instance::container_name(inst.id)).await {
        return Ok(Json(json!({"running": false, "current": Value::Null, "proxies": proxies})));
    }
    let current = match exec_stdout_script(&state, &inst, "go env GOPROXY 2>/dev/null").await {
        Ok(out) => {
            let t = out.trim().to_string();
            if t.starts_with("http") { t } else { GO_DEFAULT_PROXY.to_string() }
        }
        Err(_) => GO_DEFAULT_PROXY.to_string(),
    };
    Ok(Json(json!({"running": true, "current": current, "proxies": proxies})))
}

async fn go_proxy_put(
    PanelInst(inst): PanelInst,
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> AppResult<Json<Value>> {
    require_go(&inst)?;
    let url = sfield(&body, "url").trim().to_string();
    if !go_url_re().is_match(&url) {
        return Err(AppError::bad_request(
            "代理地址格式不正确（https 链接，可带 ,direct 后缀）",
        ));
    }
    if !running(&state, &instance::container_name(inst.id)).await {
        return Err(AppError::conflict("实例未运行，请先启动实例"));
    }
    exec_stdout_script(&state, &inst, &format!("go env -w GOPROXY={url}")).await?;
    let named = catalog::mirror_name(catalog::go_proxies(), &url);
    instance::log_op(&state.db, inst.id, "go_proxy", &named).await?;
    Ok(Json(json!({"current": url})))
}

// =====================================================================
// 路由表（全部挂在根下，路径自带 /panel 前缀）
// =====================================================================

pub fn routes() -> Router<AppState> {
    Router::new()
        // 备份
        .route("/panel/instance/backup/info", get(backup_info))
        .route("/panel/instance/backup/list", get(backup_list))
        .route("/panel/instance/backup/download", get(backup_download))
        .route("/panel/instance/backup", axum::routing::delete(backup_delete))
        .route("/panel/instance/backup/db", post(backup_db))
        .route("/panel/instance/backup/dir", post(backup_dir))
        .route("/panel/instance/backup/restore/dir", post(restore_dir))
        .route("/panel/instance/backup/restore/db", post(restore_db))
        .route("/panel/instance/backup/task-lines", get(backup_task_lines))
        .route("/panel/instance/snapshots", get(snapshot_list))
        .route("/panel/instance/snapshots/restore", post(snapshot_restore))
        // 定时任务
        .route(
            "/panel/instance/crons",
            get(crons_list).post(crons_create),
        )
        .route(
            "/panel/instance/crons/{cid}",
            axum::routing::patch(crons_patch).delete(crons_delete),
        )
        .route("/panel/instance/crons/{cid}/run", post(crons_run))
        // 站点防护
        .route("/panel/instance/site", get(site_get).put(site_put))
        // phpMyAdmin
        .route("/panel/instance/pma/enable", post(pma_enable))
        .route("/panel/instance/pma/disable", post(pma_disable))
        // 概览 / 流量 / 执行 / 重建
        .route("/panel/instance/overview", get(overview))
        .route("/panel/instance/traffic", get(traffic))
        .route("/panel/instance/exec", post(exec_cmd))
        .route("/panel/instance/rebuild", post(rebuild))
        // Node
        .route("/panel/instance/node/packages", get(node_packages))
        .route("/panel/instance/node/deps/install-pkgs", post(node_install_pkgs))
        .route("/panel/instance/node/deps/install", post(node_install_from_pkg))
        .route("/panel/instance/node/deps/uninstall", post(node_uninstall_pkgs))
        .route(
            "/panel/instance/npm/mirror",
            get(npm_mirror_get).put(npm_mirror_put),
        )
        // Go
        .route("/panel/instance/go/mod", get(go_mod_get))
        .route("/panel/instance/go/mod/tidy", post(go_mod_tidy))
        .route(
            "/panel/instance/go/proxy",
            get(go_proxy_get).put(go_proxy_put),
        )
}
