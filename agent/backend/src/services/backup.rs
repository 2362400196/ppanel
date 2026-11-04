//! 宿主机备份服务：容器整体导出、目录打包、MySQL 导出/恢复、定时任务执行与保留策略。
//!
//! 逐端点对齐 Python 版 app/routers/backup.py + app/services/host_backup_service.py：
//!   备份文件统一落 /opt/ppanel/backups/（自动创建，可用 PPANEL_BACKUP_DIR 覆盖）
//!     容器：  <容器名>_<时间戳>.tar       docker export 文件系统整体导出（流式落盘）
//!     数据库：<库名>_<时间戳>.sql.gz      mysqldump --single-transaction 后容器内 gzip
//!     目录：  <容器名>_<目录名>_<时间戳>.tar.gz   容器内 tar 打包指定目录后取回
//!   恢复：
//!     容器：  tar → docker import 为镜像（注入 sleep 入口）→ 创建新容器（找回数据用）
//!     数据库：sql.gz → put_archive 进 MySQL 容器 → CREATE IF NOT EXISTS + 导入（覆盖）
//!
//! 说明：Python 的 `get_docker_long()` 是长超时客户端，bollard 侧共用同一个懒加载
//! 客户端（无独立超时参数），其余行为保持一致。

use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::Instant;

use axum::body::Bytes;
use bollard::container::{
    Config, CreateContainerOptions, DownloadFromContainerOptions, StartContainerOptions,
    UploadToContainerOptions,
};
use bollard::exec::{CreateExecOptions, StartExecOptions, StartExecResults};
use bollard::image::CreateImageOptions;
use bollard::models::ContainerInspectResponse;
use bollard::Docker;
use chrono::Local;
use futures_util::StreamExt;
use regex::Regex;
use serde_json::{json, Value};
use tokio::io::AsyncWriteExt;

use crate::error::{blocking, AppError, AppResult};
use crate::models::{opt_dt_iso, utcnow, HostBackupJob};
use crate::services::{cos, instance, mysql};
use crate::state::AppState;

// ---------- 常量 / 校验 ----------

/// 备份目录（Python 硬编码 /opt/ppanel/backups，允许环境变量覆盖便于本地开发）。
pub fn backup_root() -> String {
    std::env::var("PPANEL_BACKUP_DIR")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .unwrap_or_else(|| "/opt/ppanel/backups".to_string())
}

pub const BK_KINDS: [&str; 4] = ["container", "dir", "db_full", "db_table"];
pub const DESTS: [&str; 3] = ["local", "cos", "both"];
pub const SYSTEM_DBS: [&str; 4] = ["information_schema", "mysql", "performance_schema", "sys"];

fn re(get: &'static OnceLock<Regex>, pattern: &'static str) -> &'static Regex {
    get.get_or_init(|| Regex::new(pattern).expect("内置正则编译失败"))
}

fn name_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    re(&R, r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
}
fn db_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    re(&R, r"^[A-Za-z0-9_]{1,64}$")
}
fn cname_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    re(&R, r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
}
fn tbl_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    re(&R, r"^[A-Za-z0-9_$]{1,64}$")
}
fn path_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    re(&R, r"^(/[A-Za-z0-9_.-]+)+$")
}

pub fn name_ok(s: &str) -> bool {
    name_re().is_match(s)
}
pub fn db_ok(s: &str) -> bool {
    db_re().is_match(s)
}
pub fn cname_ok(s: &str) -> bool {
    cname_re().is_match(s)
}
pub fn table_ok(s: &str) -> bool {
    tbl_re().is_match(s)
}
pub fn path_ok(s: &str) -> bool {
    path_re().is_match(s)
}

/// 备份文件名白名单 + 防路径穿越（`_safe_path`）。
pub fn safe_path(file: &str) -> AppResult<PathBuf> {
    if !name_ok(file) || file.contains("..") {
        return Err(AppError::bad_request("文件名无效"));
    }
    Ok(Path::new(&backup_root()).join(file))
}

pub fn ts() -> String {
    Local::now().format("%Y%m%d_%H%M%S").to_string()
}

fn rand_hex6() -> String {
    use rand::Rng;
    let mut rng = rand::thread_rng();
    (0..3)
        .map(|_| format!("{:02x}", rng.gen::<u8>()))
        .collect::<String>()
}

/// 文件名 → 列表展示用的分类（database / dir / container / other）。
pub fn file_kind(name: &str) -> &'static str {
    if name.ends_with(".sql.gz") {
        "database"
    } else if name.ends_with(".tar.gz") {
        "dir"
    } else if name.ends_with(".tar") {
        "container"
    } else {
        "other"
    }
}

fn tail(s: &str, n: usize) -> String {
    let chars: Vec<char> = s.chars().collect();
    if chars.len() <= n {
        s.to_string()
    } else {
        chars[chars.len() - n..].iter().collect()
    }
}

fn head(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// 任务日志中的异常文案：Python `str(HTTPException)` 即 "404: 容器不存在"。
fn err_text(e: &AppError) -> String {
    format!("{}: {}", e.status.as_u16(), e.detail)
}

// ---------- 备份列表 ----------

/// 备份文件列表（按时间倒序）。page>=1 返回分页结构，否则全量（旧调用兼容）。
pub fn list_backups(page: i64, page_size: i64) -> Value {
    let dir = backup_root();
    let _ = std::fs::create_dir_all(&dir);
    let mut items: Vec<Value> = Vec::new();
    if let Ok(rd) = std::fs::read_dir(&dir) {
        for ent in rd.flatten() {
            let name = ent.file_name().to_string_lossy().to_string();
            let Ok(md) = ent.metadata() else { continue };
            if !md.is_file() {
                continue;
            }
            let mtime = md
                .modified()
                .ok()
                .map(|t| chrono::DateTime::<Local>::from(t).format("%Y-%m-%d %H:%M").to_string())
                .unwrap_or_default();
            items.push(json!({
                "file": name,
                "kind": file_kind(&name),
                "size": md.len(),
                "created_at": mtime,
            }));
        }
    }
    items.sort_by(|a, b| {
        b["created_at"]
            .as_str()
            .unwrap_or("")
            .cmp(a["created_at"].as_str().unwrap_or(""))
    });
    if page >= 1 {
        let size = page_size.max(0) as usize;
        let start = ((page - 1) as usize * size).min(items.len());
        let end = (start + size).min(items.len());
        return json!({"backups": items[start..end], "total": items.len(), "dir": dir});
    }
    json!({"backups": items, "dir": dir})
}

// ---------- Docker 小工具 ----------

/// inspect；Ok(None) 表示容器不存在（404），Err 为 Docker 调用失败（502）。
pub async fn inspect_opt(
    docker: &Docker,
    name: &str,
) -> AppResult<Option<ContainerInspectResponse>> {
    match docker.inspect_container(name, None).await {
        Ok(v) => Ok(Some(v)),
        Err(bollard::errors::Error::DockerResponseServerError { status_code: 404, .. }) => Ok(None),
        Err(e) => Err(AppError::bad_gateway(format!("Docker 调用失败：{e}"))),
    }
}

pub fn is_running(c: &ContainerInspectResponse) -> bool {
    c.state.as_ref().and_then(|s| s.running).unwrap_or(false)
}

fn container_display_name(c: &ContainerInspectResponse) -> String {
    c.name
        .clone()
        .map(|n| n.trim_start_matches('/').to_string())
        .filter(|n| !n.is_empty())
        .unwrap_or_else(|| head(c.id.as_deref().unwrap_or("container"), 12))
}

/// `c.exec_run(cmd, demux=True)`：返回 (exit_code, stdout, stderr)。
pub async fn exec_demux(
    docker: &Docker,
    cname: &str,
    cmd: Vec<String>,
) -> AppResult<(i64, String, String)> {
    let exec = docker
        .create_exec(
            cname,
            CreateExecOptions {
                cmd: Some(cmd),
                attach_stdout: Some(true),
                attach_stderr: Some(true),
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
    let mut stdout = String::new();
    let mut stderr = String::new();
    if let StartExecResults::Attached { mut output, .. } = start {
        while let Some(item) = output.next().await {
            match item {
                Ok(bollard::container::LogOutput::StdOut { message })
                | Ok(bollard::container::LogOutput::Console { message }) => {
                    stdout.push_str(&String::from_utf8_lossy(&message));
                }
                Ok(bollard::container::LogOutput::StdErr { message }) => {
                    stderr.push_str(&String::from_utf8_lossy(&message));
                }
                _ => {}
            }
        }
    }
    let code = docker
        .inspect_exec(&exec_id)
        .await
        .ok()
        .and_then(|i| i.exit_code)
        .unwrap_or(-1);
    Ok((code, stdout, stderr))
}

/// `c.exec_run(cmd)`（不 demux）：返回 (exit_code, 合并输出)。
pub async fn exec_combined(
    docker: &Docker,
    cname: &str,
    cmd: Vec<String>,
) -> AppResult<(i64, String)> {
    let (code, out, err) = exec_demux(docker, cname, cmd).await?;
    Ok((code, format!("{out}{err}")))
}

async fn rm_in_container(docker: &Docker, cname: &str, paths: &[String]) {
    let mut cmd = vec!["sh".to_string(), "-c".to_string()];
    cmd.push(format!("rm -f {}", paths.join(" ")));
    let _ = exec_combined(docker, cname, cmd).await;
}

// ---------- 容器内 tar 流 → 落盘内层文件 ----------

/// 把 docker get_archive 的 tar 流写到临时文件，再解出内层第一个普通文件到 dest。
/// （Python 用 tarfile 流式读；这里先落临时 tar 再解析，内存占用恒定。）
/// 有任务上下文的路径走本函数（带进度日志），否则直接用 [`pull_archive_docker`]。
async fn pull_archive(
    state: &AppState,
    cname: &str,
    container_path: &str,
    dest: &Path,
    tid: &str,
) -> AppResult<()> {
    let docker = state.docker.get().await?;
    pull_archive_docker(&docker, cname, container_path, dest, Some(&|mb| {
        state.tasks.log(tid, &format!("拉取中… 已写 {} MB", mb));
    }))
    .await
}

/// 取回实现的公共主体（不依赖任务系统）：删库前快照等无任务上下文的路径复用。
pub(crate) async fn pull_archive_docker(
    docker: &Docker,
    cname: &str,
    container_path: &str,
    dest: &Path,
    progress: Option<&(dyn Fn(u64) + Send + Sync)>,
) -> AppResult<()> {
    let tmp = std::env::temp_dir().join(format!(
        "ppanel_grab_{}.tar",
        uuid::Uuid::new_v4().simple()
    ));
    let write_res: AppResult<()> = {
        async {
            let mut f = tokio::fs::File::create(&tmp)
                .await
                .map_err(|e| AppError::bad_gateway(format!("取回备份失败：{e}")))?;
            let mut stream = docker.download_from_container(
                cname,
                Some(DownloadFromContainerOptions { path: container_path.to_string() }),
            );
            let mut total: u64 = 0;
            let mut mark: u64 = 256 * 1024 * 1024;
            while let Some(chunk) = stream.next().await {
                let bytes = chunk.map_err(|e| AppError::bad_gateway(format!("取回备份失败：{e}")))?;
                f.write_all(&bytes)
                    .await
                    .map_err(|e| AppError::bad_gateway(format!("取回备份失败：{e}")))?;
                total += bytes.len() as u64;
                if total >= mark {
                    if let Some(cb) = progress {
                        cb(total / 1048576);
                    }
                    mark += 256 * 1024 * 1024;
                }
            }
            let _ = f.flush().await;
            Ok(())
        }
        .await
    };
    if let Err(e) = write_res {
        let _ = std::fs::remove_file(&tmp);
        return Err(e);
    }

    let tar_path = tmp.clone();
    let dest_path = dest.to_path_buf();
    let extracted = blocking(move || {
        let f = std::fs::File::open(&tar_path)?;
        let mut ar = tar::Archive::new(f);
        let entries = ar.entries()?;
        for ent in entries {
            let mut ent = ent?;
            if ent.header().entry_type().is_file() {
                let mut out = std::fs::File::create(&dest_path)?;
                std::io::copy(&mut ent, &mut out)?;
                return Ok(true);
            }
        }
        Ok(false)
    })
    .await;
    let _ = std::fs::remove_file(&tmp);
    match extracted {
        Ok(true) => Ok(()),
        Ok(false) => Err(AppError::bad_gateway("容器内未返回打包文件")),
        Err(e) => Err(AppError::bad_gateway(format!("取回备份失败：{}", e.detail))),
    }
}

/// 把宿主机单个文件打成 tar 字节（put_archive 用）。
fn build_single_file_tar(src: &Path, name_in_tar: &str) -> std::io::Result<Vec<u8>> {
    let mut f = std::fs::File::open(src)?;
    let size = f.metadata()?.len();
    let mut buf: Vec<u8> = Vec::new();
    {
        let mut tb = tar::Builder::new(&mut buf);
        let mut header = tar::Header::new_gnu();
        header.set_size(size);
        header.set_mode(0o644);
        header.set_entry_type(tar::EntryType::Regular);
        header.set_cksum();
        tb.append_data(&mut header, name_in_tar, &mut f)?;
        tb.finish()?;
    }
    Ok(buf)
}

// ---------- 数据库服务信息 ----------

/// 去掉 mysql 客户端的密码警告行（Python: 过滤 "Using a password on the command line"）。
fn filtered_output(out: &str) -> String {
    out.lines()
        .filter(|l| !l.contains("Using a password on the command line"))
        .collect::<Vec<_>>()
        .concat()
}

async fn svc_root_password(state: &AppState, ver: &str) -> AppResult<String> {
    Ok(mysql::get_svc_row(&state.db, ver).await?.root_password)
}

/// 运行中的 MySQL 服务容器（不存在/未运行由调用方给出具体文案）。
async fn mysql_running_container(
    state: &AppState,
    ver: &str,
) -> AppResult<ContainerInspectResponse> {
    let docker = state.docker.get().await?;
    match inspect_opt(&docker, &mysql::svc_name(ver)).await? {
        Some(c) => Ok(c),
        None => Err(AppError::not_found("MySQL 容器不存在，请重新启用服务")),
    }
}

// ---------- 容器整体备份 ----------

async fn backup_container_inner(
    state: &AppState,
    container_id: &str,
    tid: &str,
) -> AppResult<Value> {
    let docker = state.docker.get().await?;
    let c = match inspect_opt(&docker, container_id).await? {
        Some(c) => c,
        None => return Err(AppError::not_found("容器不存在")),
    };
    let cname = container_display_name(&c);
    state
        .tasks
        .log(tid, &format!("容器 {cname}，开始 docker export 导出文件系统…"));

    let safe: String = cname
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || ch == '.' || ch == '_' || ch == '-' {
                ch
            } else {
                '_'
            }
        })
        .collect();
    let safe = safe.trim_matches(|ch| ch == '.' || ch == '_').to_string();
    let safe = if safe.is_empty() { "container".to_string() } else { safe };

    let fname = format!("{safe}_{}.tar", ts());
    let dir = backup_root();
    let _ = std::fs::create_dir_all(&dir);
    let dest = Path::new(&dir).join(&fname);

    let export_res: Result<u64, String> = {
        async {
            let mut f = tokio::fs::File::create(&dest)
                .await
                .map_err(|e| format!("{e}"))?;
            let mut stream = docker.export_container(&cname);
            let mut total: u64 = 0;
            let mut mark: u64 = 512 * 1024 * 1024;
            while let Some(chunk) = stream.next().await {
                let bytes = chunk.map_err(|e| format!("{e}"))?;
                f.write_all(&bytes).await.map_err(|e| format!("{e}"))?;
                total += bytes.len() as u64;
                if total >= mark {
                    state.tasks.log(tid, &format!("导出中… 已写 {}MB", total / 1048576));
                    mark += 512 * 1024 * 1024;
                }
            }
            let _ = f.flush().await;
            Ok(total)
        }
        .await
    };
    let total = match export_res {
        Ok(v) => v,
        Err(e) => {
            let _ = std::fs::remove_file(&dest);
            return Err(AppError::bad_gateway(format!("导出容器失败：{e}")));
        }
    };
    // Python 用落盘后的大小报日志（这里用流累计字节，等价）
    state.tasks.log(
        tid,
        &format!("已落盘 {fname}（{:.1} MB）", total as f64 / 1048576.0),
    );
    state.tasks.finish(tid, true, "✔ 容器备份完成");
    Ok(json!({
        "ok": true,
        "file": fname,
        "detail": format!("容器「{cname}」已备份为 {fname}"),
    }))
}

/// POST /host/backups/container
pub async fn backup_container(
    state: &AppState,
    container_id: &str,
    tid: &str,
) -> AppResult<Value> {
    state
        .tasks
        .start(tid, &format!("备份容器 {}", head(container_id, 12)));
    state.tasks.log(tid, "查找容器…");
    let r = backup_container_inner(state, container_id, tid).await;
    if let Err(e) = &r {
        // Python: f"✘ 容器备份失败：{e}"（HTTPException 的 text 即 detail）
        state
            .tasks
            .finish(tid, false, &format!("✘ 容器备份失败：{}", err_text(e)));
    }
    r
}

// ---------- 目录备份 / 恢复 ----------

async fn check_container_dir(
    docker: &Docker,
    cname: &str,
    p: &str,
) -> AppResult<()> {
    let (code, out, _err) = exec_demux(
        docker,
        cname,
        vec!["sh".into(), "-c".into(), format!("test -d '{p}' && echo OK")],
    )
    .await?;
    if code != 0 || out.trim() != "OK" {
        return Err(AppError::not_found(format!("容器内目录 {p} 不存在")));
    }
    Ok(())
}

async fn backup_dir_inner(
    state: &AppState,
    cname: &str,
    raw_path: &str,
    tid: &str,
) -> AppResult<Value> {
    if !cname_ok(cname) {
        return Err(AppError::bad_request("容器名格式无效"));
    }
    let p = raw_path.trim().trim_end_matches('/').to_string();
    if p.contains("..") || !path_ok(&p) {
        return Err(AppError::bad_request("目录路径无效（需为容器内绝对路径）"));
    }
    let docker = state.docker.get().await?;
    let c = inspect_opt(&docker, cname)
        .await?
        .ok_or_else(|| AppError::not_found("容器不存在或已被删除"))?;
    if !is_running(&c) {
        return Err(AppError::bad_gateway("容器未运行，无法备份目录"));
    }
    check_container_dir(&docker, cname, &p).await?;

    let base = p.rsplit_once('/').map(|(_, b)| b.to_string()).unwrap_or_default();
    let tmp = format!("/tmp/ppanel_dirbk_{}_{}.tar.gz", ts(), rand_hex6());
    let fname = format!("{cname}_{base}_{}.tar.gz", ts());
    if !name_ok(&fname) {
        return Err(AppError::bad_request("目录名无法生成合法备份文件名"));
    }
    let dir = backup_root();
    let _ = std::fs::create_dir_all(&dir);
    let dest = Path::new(&dir).join(&fname);

    state
        .tasks
        .start(tid, &format!("备份网站目录 {p}（{cname}）"));
    let r = async {
        state.tasks.log(tid, "容器内打包目录（tar.gz）…");
        let (code, _out, err) = exec_demux(
            &docker,
            cname,
            vec![
                "sh".into(),
                "-c".into(),
                format!("tar -czf {tmp} -C '{p}' ."),
            ],
        )
        .await?;
        if code != 0 {
            return Err(AppError::bad_gateway(format!(
                "目录打包失败：{}",
                tail(err.trim(), 200)
            )));
        }
        state.tasks.log(tid, "打包完成，取回落盘…");
        pull_archive(state, cname, &tmp, &dest, tid).await?;
        let _ = rm_in_container(&docker, cname, &[tmp.clone()]).await;
        let size_kb = std::fs::metadata(&dest).map(|m| m.len() / 1024).unwrap_or(0);
        state.tasks.finish(
            tid,
            true,
            &format!("✔ 目录备份完成：{fname}（{size_kb} KB）"),
        );
        Ok(json!({"detail": format!("目录备份完成：{fname}"), "file": fname}))
    }
    .await;

    if let Err(e) = &r {
        let _ = rm_in_container(&docker, cname, &[tmp.clone()]).await;
        if let Ok(md) = std::fs::metadata(&dest) {
            if md.len() == 0 {
                let _ = std::fs::remove_file(&dest);
            }
        }
        state
            .tasks
            .finish(tid, false, &format!("✘ 目录备份失败：{}", err_text(e)));
    }
    r
}

/// POST /host/backups/dir
pub async fn backup_dir(
    state: &AppState,
    container_id: &str,
    path: &str,
    tid: &str,
) -> AppResult<Value> {
    backup_dir_inner(state, container_id, path, tid).await
}

/// POST /host/backups/restore/dir
pub async fn restore_dir(
    state: &AppState,
    file: &str,
    container_id: &str,
    path: &str,
    tid: &str,
) -> AppResult<Value> {
    if !file.ends_with(".tar.gz") {
        return Err(AppError::bad_request("目录备份必须为 .tar.gz 文件"));
    }
    let target = safe_path(file)?;
    if !cname_ok(container_id) {
        return Err(AppError::bad_request("容器名格式无效"));
    }
    let p = path.trim().trim_end_matches('/').to_string();
    if p.contains("..") || !path_ok(&p) {
        return Err(AppError::bad_request("目标目录无效（需为容器内绝对路径）"));
    }
    let docker = state.docker.get().await?;
    let c = inspect_opt(&docker, container_id)
        .await?
        .ok_or_else(|| AppError::not_found("目标容器不存在或已被删除"))?;
    if !is_running(&c) {
        return Err(AppError::bad_gateway("目标容器未运行"));
    }
    if !target.is_file() {
        return Err(AppError::not_found("备份文件不存在"));
    }

    state.tasks.start(tid, &format!("恢复目录 {file} → {container_id}:{p}"));
    let base_name = target
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_default();
    let r = async {
        state.tasks.log(tid, "上传备份包到容器 /tmp…");
        let buf = build_single_file_tar(&target, &format!("restore_{base_name}"))
            .map_err(|e| AppError::bad_gateway(format!("打包备份包失败：{e}")))?;
        docker
            .upload_to_container(
                container_id,
                Some(UploadToContainerOptions::<String> {
                    path: "/tmp".to_string(),
                    no_overwrite_dir_non_dir: String::new(),
                }),
                Bytes::from(buf),
            )
            .await?;
        let tmp = format!("/tmp/restore_{base_name}");
        state.tasks.log(tid, &format!("容器内解压到 {p}（覆盖同名文件）…"));
        let (code, _out, err) = exec_demux(
            &docker,
            container_id,
            vec![
                "sh".into(),
                "-c".into(),
                format!("mkdir -p '{p}' && tar -xzf {tmp} -C '{p}' && rm -f {tmp}"),
            ],
        )
        .await?;
        if code != 0 {
            let _ = rm_in_container(&docker, container_id, &[tmp]).await;
            return Err(AppError::bad_gateway(format!(
                "解压失败：{}",
                tail(err.trim(), 200)
            )));
        }
        state
            .tasks
            .finish(tid, true, &format!("✔ 目录已恢复至 {container_id}:{p}"));
        Ok(json!({"detail": format!("目录已恢复至 {container_id}:{p}")}))
    }
    .await;
    if let Err(e) = &r {
        state
            .tasks
            .finish(tid, false, &format!("✘ 目录恢复失败：{}", err_text(e)));
    }
    r
}

// ---------- 数据库备份 ----------

async fn backup_database_inner(
    state: &AppState,
    ver: &str,
    dbname: &str,
    tables: &[String],
    tid: &str,
) -> AppResult<Value> {
    state
        .tasks
        .start(tid, &format!("备份数据库 {dbname}"));
    state.tasks.log(tid, &format!("检查 MySQL {ver} 服务…"));
    if !db_ok(dbname) {
        return Err(AppError::bad_request("库名格式无效"));
    }
    let pw = svc_root_password(state, ver).await?;
    let c = mysql_running_container(state, ver).await?;
    if !is_running(&c) {
        return Err(AppError::bad_gateway(format!("MySQL {ver} 服务未运行，无法导出")));
    }
    let docker = state.docker.get().await?;
    let cname = mysql::svc_name(ver);

    // 库不存在时提前给出可读提示（demux 分离 stderr，避免密码警告混入判定）
    let (code, out, _err) = exec_demux(
        &docker,
        &cname,
        vec![
            "sh".into(),
            "-c".into(),
            format!(
                "mysql -uroot --password='{pw}' -N -e \
                 \"SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='{dbname}'\""
            ),
        ],
    )
    .await?;
    if code != 0 || out.trim() != "1" {
        return Err(AppError::not_found(format!(
            "数据库 {dbname} 不存在（可能已被删除），请关闭面板重新打开备份页刷新列表"
        )));
    }

    let tables: Vec<String> = tables
        .iter()
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty())
        .collect();
    for t in &tables {
        if !table_ok(t) {
            return Err(AppError::bad_request(format!("表名无效：{t}")));
        }
    }
    let tbl_part = if tables.is_empty() {
        String::new()
    } else {
        format!(" {}", tables.join(" "))
    };
    state.tasks.log(
        tid,
        &format!(
            "执行 mysqldump（单事务）+ gzip 压缩…（{}）",
            if tables.is_empty() {
                "整库".to_string()
            } else {
                format!("表：{}", tables.join(", "))
            }
        ),
    );

    let stamp = ts();
    let tmp_sql = format!("/tmp/ppanel_bk_{stamp}_{}.sql", rand_hex6());
    let tmp_gz = format!("{tmp_sql}.gz");
    let fname = if tables.is_empty() {
        format!("{dbname}_{stamp}.sql.gz")
    } else {
        let multi = if tables.len() > 1 { "_m" } else { "" };
        format!("{dbname}_{}{multi}_{stamp}.sql.gz", tables[0])
    };
    let dir = backup_root();
    let _ = std::fs::create_dir_all(&dir);
    let dest = Path::new(&dir).join(&fname);

    // 1) 容器内导出：--result-file 直写文件（失败的退出码不会被 gzip 掩盖）
    let (code, out) = exec_combined(
        &docker,
        &cname,
        vec![
            "sh".into(),
            "-c".into(),
            format!(
                "mysqldump -uroot --password='{pw}' --single-transaction --routines --triggers \
                 {dbname}{tbl_part} --result-file={tmp_sql} && gzip -f {tmp_sql}"
            ),
        ],
    )
    .await?;
    if code != 0 {
        let joined = filtered_output(&out);
        return Err(AppError::bad_gateway(format!(
            "mysqldump 失败：{}",
            tail(joined.trim(), 300)
        )));
    }
    state.tasks.log(tid, "mysqldump 完成，从容器取回压缩包…");

    // 2) 从容器取回 gzip（tar 流 → 解出内层文件），失败不留半成品
    let pulled = pull_archive(state, &cname, &tmp_gz, &dest, tid).await;
    let _ = rm_in_container(&docker, &cname, &[tmp_sql.clone(), tmp_gz.clone()]).await;
    if let Err(e) = pulled {
        let _ = std::fs::remove_file(&dest);
        return Err(AppError::bad_gateway(e.detail));
    }

    let size_mb = std::fs::metadata(&dest).map(|m| m.len() as f64 / 1048576.0).unwrap_or(0.0);
    state.tasks.log(tid, &format!("已落盘 {fname}（{size_mb:.1} MB）"));
    state.tasks.finish(tid, true, "✔ 数据库备份完成");
    Ok(json!({
        "ok": true,
        "file": fname,
        "detail": format!("数据库 {dbname} 已备份为 {fname}"),
    }))
}

/// POST /host/backups/database
pub async fn backup_database(
    state: &AppState,
    ver: &str,
    dbname: &str,
    tables: &[String],
    tid: &str,
) -> AppResult<Value> {
    let r = backup_database_inner(state, ver, dbname, tables, tid).await;
    if let Err(e) = &r {
        state
            .tasks
            .finish(tid, false, &format!("✘ 数据库备份失败：{}", err_text(e)));
    }
    r
}

/// GET /host/backups/tables：列出库内所有表。
pub async fn list_tables(state: &AppState, ver: &str, dbname: &str) -> AppResult<Value> {
    if !db_ok(dbname) {
        return Err(AppError::bad_request("库名格式无效"));
    }
    let pw = svc_root_password(state, ver).await?;
    let c = mysql_running_container(state, ver).await?;
    if !is_running(&c) {
        return Err(AppError::bad_gateway(format!("MySQL {ver} 服务未运行")));
    }
    let docker = state.docker.get().await?;
    let (code, out, _err) = exec_demux(
        &docker,
        &mysql::svc_name(ver),
        vec![
            "sh".into(),
            "-c".into(),
            format!("mysql -uroot --password='{pw}' -N -e 'SHOW TABLES FROM {dbname}'"),
        ],
    )
    .await?;
    if code != 0 {
        return Err(AppError::not_found(format!("库 {dbname} 不存在或服务异常")));
    }
    let tables: Vec<String> = out
        .lines()
        .map(|l| l.trim().to_string())
        .filter(|l| !l.is_empty())
        .collect();
    Ok(json!({"tables": tables}))
}

// ---------- 可备份的实例数据库清单 ----------

/// GET /host/backups/dbs
pub async fn backup_db_options(state: &AppState) -> AppResult<Value> {
    let rows: Vec<(String, String)> =
        sqlx::query_as("SELECT version, db_name FROM instance_dbs ORDER BY version, db_name")
            .fetch_all(&state.db)
            .await?;
    let docker = state.docker.try_get().await;
    let mut existing: std::collections::HashMap<String, Vec<String>> = Default::default();
    if let Some(docker) = docker {
        let mut versions: Vec<String> = rows.iter().map(|r| r.0.clone()).collect();
        versions.sort();
        versions.dedup();
        for ver in versions {
            let Ok(Some(c)) = inspect_opt(&docker, &mysql::svc_name(&ver)).await else {
                continue;
            };
            if !is_running(&c) {
                continue;
            }
            let Ok(pw) = svc_root_password(state, &ver).await else {
                continue;
            };
            let Ok((code, out, _)) = exec_demux(
                &docker,
                &mysql::svc_name(&ver),
                vec![
                    "sh".into(),
                    "-c".into(),
                    format!("mysql -uroot --password='{pw}' -N -e 'SHOW DATABASES'"),
                ],
            )
            .await
            else {
                continue;
            };
            if code != 0 {
                continue;
            }
            let mut names: Vec<String> = Vec::new();
            for n in out.split_whitespace() {
                if !n.is_empty() && !SYSTEM_DBS.contains(&n) {
                    names.push(n.to_string());
                }
            }
            existing.insert(ver, names);
        }
    }
    let mut out = Vec::new();
    for (ver, dbname) in rows {
        let running = existing.contains_key(&ver);
        if running {
            // 服务在运行但库已不存在 → 隐藏
            if !existing[&ver].iter().any(|n| n == &dbname) {
                continue;
            }
        }
        out.push(json!({"version": ver, "db_name": dbname, "running": running}));
    }
    Ok(json!({"dbs": out}))
}

// ---------- 容器备份恢复（import → 新容器） ----------

/// POST /host/backups/restore/container
pub async fn restore_container(
    state: &AppState,
    file: &str,
    name: &str,
    tid: &str,
) -> AppResult<Value> {
    state.tasks.start(tid, &format!("恢复容器备份 {file}"));
    let r = async {
        let src = safe_path(file)?;
        if !file.ends_with(".tar") || !src.is_file() {
            return Err(AppError::bad_request("容器备份必须为 .tar 文件"));
        }
        let name = name.trim().to_string();
        if !cname_ok(&name) {
            return Err(AppError::bad_request("容器名无效（字母数字开头，可含 _ . -）"));
        }
        let docker = state.docker.get().await?;
        if inspect_opt(&docker, &name).await?.is_some() {
            return Err(AppError::conflict(format!("容器 {name} 已存在，请换一个名字")));
        }

        let stamp = ts();
        let repo = "ppanel-restore".to_string();
        let tag = stamp.to_lowercase();
        let image = format!("{repo}:{tag}");
        state
            .tasks
            .log(tid, &format!("docker import 导入镜像 {image}（大包耗时较久）…"));
        let bytes = Bytes::from(
            tokio::fs::read(&src)
                .await
                .map_err(|e| AppError::bad_gateway(format!("导入镜像失败：{e}")))?,
        );
        let mut stream = docker.create_image(
            Some(CreateImageOptions {
                from_src: "-".to_string(),
                repo: repo.clone(),
                tag: tag.clone(),
                changes: vec!["CMD [\"/bin/sh\", \"-c\", \"sleep infinity\"]"],
                ..Default::default()
            }),
            Some(bytes),
            None,
        );
        while let Some(item) = stream.next().await {
            if let Err(e) = item {
                return Err(AppError::bad_gateway(format!("导入镜像失败：{e}")));
            }
        }
        state.tasks.log(tid, "镜像导入完成，创建新容器…");

        // 确认导入成功（否则建容器会误走 registry 拉取）
        if let Err(e) = docker.inspect_image(&image).await {
            return Err(AppError::bad_gateway(format!(
                "镜像已导入为 {image}，但创建容器失败：{e}"
            )));
        }
        // bollard 无 containers.run 组合接口：先 create 再 start
        if let Err(e) = docker
            .create_container(
                Some(CreateContainerOptions {
                    name: name.clone(),
                    platform: None,
                }),
                Config::<String> {
                    image: Some(image.clone()),
                    ..Default::default()
                },
            )
            .await
        {
            return Err(AppError::bad_gateway(format!(
                "镜像已导入为 {image}，但创建容器失败：{e}"
            )));
        }
        if let Err(e) = docker
            .start_container(&name, None::<StartContainerOptions<String>>)
            .await
        {
            return Err(AppError::bad_gateway(format!(
                "镜像已导入为 {image}，但创建容器失败：{e}"
            )));
        }
        state
            .tasks
            .finish(tid, true, &format!("✔ 已创建容器 {name}（sleep 入口，供取回数据）"));
        Ok(json!({
            "ok": true,
            "container": name,
            "image": image,
            "detail": format!("已恢复为新容器 {name}（sleep 入口，供取回数据）"),
        }))
    }
    .await;
    if let Err(e) = &r {
        state
            .tasks
            .finish(tid, false, &format!("✘ 容器恢复失败：{}", err_text(e)));
    }
    r
}

// ---------- 数据库恢复 ----------

/// POST /host/backups/restore/database
pub async fn restore_database(
    state: &AppState,
    file: &str,
    ver: &str,
    dbname: &str,
    tid: &str,
) -> AppResult<Value> {
    state.tasks.start(tid, &format!("恢复数据库备份 {file}"));
    state.tasks.log(tid, &format!("目标：MySQL {ver} / {dbname}"));
    let r = async {
        let src = safe_path(file)?;
        if !file.ends_with(".sql.gz") || !src.is_file() {
            return Err(AppError::bad_request("数据库备份必须为 .sql.gz 文件"));
        }
        if !db_ok(dbname) {
            return Err(AppError::bad_request("库名格式无效"));
        }
        let pw = svc_root_password(state, ver).await?;
        let c = mysql_running_container(state, ver).await?;
        if !is_running(&c) {
            return Err(AppError::bad_gateway(format!("MySQL {ver} 服务未运行，无法恢复")));
        }
        let docker = state.docker.get().await?;
        let cname = mysql::svc_name(ver);
        let tmp_gz = format!("/tmp/ppanel_restore_{}_{}.sql.gz", ts(), rand_hex6());
        state.tasks.log(tid, "上传备份到 MySQL 容器 /tmp…");

        let tar_name = tmp_gz.rsplit('/').next().unwrap_or(&tmp_gz).to_string();
        let buf = build_single_file_tar(&src, &tar_name)
            .map_err(|e| AppError::bad_gateway(format!("上传备份到 MySQL 容器失败：{e}")))?;
        docker
            .upload_to_container(
                &cname,
                Some(UploadToContainerOptions::<String> {
                    path: "/tmp".to_string(),
                    no_overwrite_dir_non_dir: String::new(),
                }),
                Bytes::from(buf),
            )
            .await
            .map_err(|e| AppError::bad_gateway(format!("上传备份到 MySQL 容器失败：{e}")))?;

        state
            .tasks
            .log(tid, "建库（如不存在）并导入，gzip 完整性校验后灌入…");
        let (code, out) = exec_combined(
            &docker,
            &cname,
            vec![
                "sh".into(),
                "-c".into(),
                format!(
                    "mysql -uroot --password='{pw}' -e 'CREATE DATABASE IF NOT EXISTS {dbname}' \
                     && gzip -t {tmp_gz} && gunzip -c {tmp_gz} | mysql -uroot --password='{pw}' {dbname}"
                ),
            ],
        )
        .await?;
        let _ = rm_in_container(&docker, &cname, &[tmp_gz.clone()]).await;
        if code != 0 {
            let joined = filtered_output(&out);
            return Err(AppError::bad_gateway(format!(
                "恢复失败：{}",
                tail(joined.trim(), 300)
            )));
        }
        state
            .tasks
            .finish(tid, true, &format!("✔ 已恢复到 {ver} 的 {dbname} 库（覆盖导入）"));
        Ok(json!({
            "ok": true,
            "db": dbname,
            "detail": format!("备份已恢复到 {ver} 的 {dbname} 库（覆盖导入）"),
        }))
    }
    .await;
    if let Err(e) = &r {
        state
            .tasks
            .finish(tid, false, &format!("✘ 数据库恢复失败：{}", err_text(e)));
    }
    r
}

// ---------- 定时备份任务 ----------

/// 任务输出结构（字段与 Python `_job_out` 一致）。
pub fn job_out(j: &HostBackupJob) -> Value {
    json!({
        "id": j.id,
        "schedule": j.schedule,
        "kind": j.kind,
        "target": j.target,
        "dir_path": j.dir_path,
        "table_name": j.table_name,
        "keep": j.keep,
        "dest": if j.dest.is_empty() { "local".to_string() } else { j.dest.clone() },
        "enabled": j.enabled != 0,
        "last_run": opt_dt_iso(&j.last_run),
        "last_status": j.last_status,
        "last_output": j.last_output,
    })
}

/// 校验任务入参（与 Python `_validate_job_in` 顺序/文案一致）。
pub async fn validate_job(
    state: &AppState,
    schedule: &str,
    kind: &str,
    target: &str,
    dir_path: &str,
    table_name: &str,
    keep: i64,
    dest: &str,
) -> AppResult<()> {
    let schedule = schedule.trim();
    if let Some(err) = crate::services::cron::validate(schedule) {
        return Err(AppError::bad_request(err));
    }
    if !BK_KINDS.contains(&kind) {
        return Err(AppError::bad_request("备份类型无效"));
    }
    if !DESTS.contains(&dest) {
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
    if !(1..=100).contains(&keep) {
        return Err(AppError::bad_request("保留份数应为 1-100"));
    }
    if kind == "container" || kind == "dir" {
        if !cname_ok(target) {
            return Err(AppError::bad_request("容器名格式无效"));
        }
        if kind == "dir" {
            let p = if dir_path.trim().is_empty() { "/app" } else { dir_path.trim() };
            if !p.starts_with('/') || p.split('/').any(|seg| seg == "..") {
                return Err(AppError::bad_request("目录必须是容器内绝对路径且不能含 .."));
            }
        }
    } else {
        let (ver, dbname) = match target.split_once(':') {
            Some((v, d)) => (v.to_string(), d.to_string()),
            None => (String::new(), String::new()),
        };
        if ver.is_empty() || dbname.is_empty() {
            return Err(AppError::bad_request(
                "目标格式应为 版本:库名（如 5.7:ppanel_1）",
            ));
        }
        if !db_ok(&dbname) {
            return Err(AppError::bad_request("库名格式无效"));
        }
        mysql::get_svc_row(&state.db, &ver).await?;
        if kind == "db_table" && !table_name.trim().is_empty() && !table_ok(table_name.trim()) {
            return Err(AppError::bad_request(format!("表名无效：{}", table_name.trim())));
        }
    }
    Ok(())
}

fn is_ts(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() == 15
        && b[8] == b'_'
        && b[..8].iter().all(|c| c.is_ascii_digit())
        && b[9..].iter().all(|c| c.is_ascii_digit())
}

/// 只保留本任务最近 keep 份备份，返回删除数量（按任务产出模式精确筛选）。
pub fn apply_keep(job: &HostBackupJob) -> i64 {
    let keep = if job.keep == 0 { 5 } else { job.keep.max(1) } as usize;
    let dir = backup_root();
    let matches = |name: &str| -> bool {
        match job.kind.as_str() {
            "container" => {
                let Some(rest) = name.strip_prefix(&format!("{}_", job.target)) else {
                    return false;
                };
                let Some(rest) = rest.strip_suffix(".tar") else { return false };
                is_ts(rest)
            }
            "dir" => {
                let base = {
                    let raw = if job.dir_path.is_empty() { "/app" } else { &job.dir_path };
                    let b = raw.trim().trim_end_matches('/').rsplit('/').next().unwrap_or("");
                    if b.is_empty() { "app".to_string() } else { b.to_string() }
                };
                let Some(rest) = name.strip_prefix(&format!("{}_{}_", job.target, base)) else {
                    return false;
                };
                let Some(rest) = rest.strip_suffix(".tar.gz") else { return false };
                is_ts(rest)
            }
            _ => {
                let dbname = job.target.split_once(':').map(|(_, d)| d).unwrap_or("");
                let Some(rest) = name.strip_prefix(&format!("{dbname}_")) else {
                    return false;
                };
                let Some(rest) = rest.strip_suffix(".sql.gz") else { return false };
                if job.kind == "db_full" {
                    is_ts(rest)
                } else {
                    // 表级：<table>_<ts> 或 <table>_m_<ts>
                    let Some(rest2) = rest.strip_prefix(&format!("{}_", job.table_name)) else {
                        return false;
                    };
                    if is_ts(rest2) {
                        return true;
                    }
                    match rest2.strip_prefix("_m_") {
                        Some(r) => is_ts(r),
                        None => false,
                    }
                }
            }
        }
    };
    let mut files: Vec<(String, u64)> = Vec::new();
    if let Ok(rd) = std::fs::read_dir(&dir) {
        for ent in rd.flatten() {
            let name = ent.file_name().to_string_lossy().to_string();
            let Ok(md) = ent.metadata() else { continue };
            if !md.is_file() || !matches(&name) {
                continue;
            }
            let mtime = md
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_secs())
                .unwrap_or(0);
            files.push((name, mtime));
        }
    }
    files.sort_by(|a, b| b.1.cmp(&a.1));
    let mut removed = 0;
    for (name, _) in files.into_iter().skip(keep) {
        if std::fs::remove_file(Path::new(&dir).join(&name)).is_ok() {
            removed += 1;
        }
    }
    removed
}

/// 执行一次备份（供定时任务复用），返回备份路由的结果（file/detail）。
pub async fn perform_job(
    state: &AppState,
    job: &HostBackupJob,
    tid: &str,
) -> AppResult<Value> {
    match job.kind.as_str() {
        "container" => backup_container(state, &job.target, tid).await,
        "dir" => {
            let p = if job.dir_path.is_empty() { "/app".to_string() } else { job.dir_path.clone() };
            backup_dir(state, &job.target, &p, tid).await
        }
        _ => {
            let (ver, dbname) = job
                .target
                .split_once(':')
                .map(|(v, d)| (v.to_string(), d.to_string()))
                .unwrap_or_default();
            let tables = if job.kind == "db_table" && !job.table_name.is_empty() {
                vec![job.table_name.clone()]
            } else {
                Vec::new()
            };
            backup_database(state, &ver, &dbname, &tables, tid).await
        }
    }
}

/// 执行一个定时备份任务（调度与「立即执行」共用），写回最近执行状态。
pub async fn run_job(state: &AppState, job_id: i64, tid: &str) -> Value {
    let job = sqlx::query_as::<_, HostBackupJob>("SELECT * FROM host_backup_jobs WHERE id = ?")
        .bind(job_id)
        .fetch_optional(&state.db)
        .await
        .ok()
        .flatten();
    let Some(job) = job else {
        return json!({"ok": false, "error": "任务不存在"});
    };
    let _ = sqlx::query("UPDATE host_backup_jobs SET last_status = 'running', last_output = '' WHERE id = ?")
        .bind(job_id)
        .execute(&state.db)
        .await;

    let t0 = Instant::now();
    let result = async {
        let r = perform_job(state, &job, tid).await?;
        let file = r["file"].as_str().unwrap_or("").to_string();
        let mut extra = String::new();
        if (job.dest == "cos" || job.dest == "both") && !file.is_empty() {
            let cfg = cos::get_config(&state.db).await?;
            if cfg.enabled == 0 {
                return Err(AppError::bad_request(
                    "腾讯云 COS 未启用（存储设置中已关闭），上传中止",
                ));
            }
            // Python 直接把相对文件名交给 COS（依赖 CWD），这里按备份目录拼接以修正该缺陷
            let local_path = Path::new(&backup_root()).join(&file);
            let up = cos::upload(&cfg, &local_path.to_string_lossy(), None)
                .await
                .map_err(AppError::bad_request)?;
            extra = format!(
                "；已上传 COS（{}，{}B）",
                up["key"].as_str().unwrap_or(""),
                up["size"].as_u64().unwrap_or(0)
            );
            if job.dest == "cos" && cfg.keep_local == 0 {
                if std::fs::remove_file(&local_path).is_ok() {
                    extra.push_str("，本地副本已按配置清理");
                }
            }
        }
        let removed = apply_keep(&job);
        Ok::<(String, String), AppError>((file, format_output(&r, &extra, removed, t0)))
    }
    .await;

    match result {
        Ok((file, output)) => {
            let _ = sqlx::query(
                "UPDATE host_backup_jobs SET last_status = 'ok', last_output = ?, last_run = ? WHERE id = ?",
            )
            .bind(&output)
            .bind(utcnow())
            .bind(job_id)
            .execute(&state.db)
            .await;
            json!({"ok": true, "file": file, "detail": output})
        }
        Err(e) => {
            let detail = head(&e.detail, 2000);
            let _ = sqlx::query(
                "UPDATE host_backup_jobs SET last_status = 'fail', last_output = ?, last_run = ? WHERE id = ?",
            )
            .bind(&detail)
            .bind(utcnow())
            .bind(job_id)
            .execute(&state.db)
            .await;
            json!({"ok": false, "error": head(&e.detail, 500)})
        }
    }
}

fn format_output(r: &Value, extra: &str, removed: i64, t0: Instant) -> String {
    let size = r["detail"].as_str().unwrap_or("");
    let keep_part = if removed > 0 {
        format!("；已按保留策略清理 {removed} 份旧备份")
    } else {
        String::new()
    };
    format!(
        "{size}{extra}{keep_part}（耗时 {:.1}s）",
        t0.elapsed().as_secs_f64()
    )
}

// ---------- 新建任务候选清单 ----------

/// GET /host/backup-jobs/meta：全部容器 + 各 MySQL 版本的库列表（排除系统库）。
pub async fn jobs_meta(state: &AppState) -> AppResult<Value> {
    let docker = state.docker.get().await?;
    let list = docker
        .list_containers(Some(bollard::container::ListContainersOptions::<String> {
            all: true,
            ..Default::default()
        }))
        .await?;
    let containers: Vec<Value> = list
        .iter()
        .map(|c| {
            let name = c
                .names
                .as_ref()
                .and_then(|n| n.first())
                .map(|n| n.trim_start_matches('/').to_string())
                .unwrap_or_default();
            json!({
                "name": name,
                "status": c.state.clone().unwrap_or_default(),
                "image": c.image.clone().unwrap_or_default(),
            })
        })
        .collect();

    let rows: Vec<crate::models::MySqlService> =
        sqlx::query_as("SELECT * FROM mysql_services").fetch_all(&state.db).await?;
    let mut mysql: Vec<Value> = Vec::new();
    for row in rows {
        let mut entry = json!({"version": row.version, "running": false, "dbs": []});
        if let Ok(Some(c)) = inspect_opt(&docker, &mysql::svc_name(&row.version)).await {
            if is_running(&c) {
                entry["running"] = json!(true);
                if let Ok((code, out, _)) = exec_demux(
                    &docker,
                    &mysql::svc_name(&row.version),
                    vec![
                        "sh".into(),
                        "-c".into(),
                        format!(
                            "mysql -uroot --password='{}' -N -e 'SHOW DATABASES'",
                            row.root_password
                        ),
                    ],
                )
                .await
                {
                    if code == 0 {
                        let dbs: Vec<String> = out
                            .lines()
                            .map(|l| l.trim().to_string())
                            .filter(|l| !l.is_empty() && !SYSTEM_DBS.contains(&l.as_str()))
                            .collect();
                        entry["dbs"] = json!(dbs);
                    }
                }
            }
        }
        mysql.push(entry);
    }
    Ok(json!({"containers": containers, "mysql": mysql}))
}

/// 供 cron 调度复用：实例容器名（默认前缀 ppanel-，可被 CONTAINER_PREFIX 覆盖）。
pub fn instance_container(instance_id: i64) -> String {
    instance::container_name(instance_id)
}

// ---------- 破坏性操作前的自动快照（安全网） ----------
//
// 数据不能丢：重置实例、删除实例、删除数据库这类不可逆操作，执行前一律先
// 自动快照到备份目录。快照失败则中止操作（数据保持原样，可重试）。

/// 自动快照保留份数（按 tag+后缀各自计数）。
pub const SNAPSHOT_KEEP: usize = 10;

fn snap_label(label: &str) -> String {
    let s: String = label
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || ch == '_' || ch == '-' {
                ch
            } else {
                '_'
            }
        })
        .collect();
    if s.is_empty() {
        "item".to_string()
    } else {
        s
    }
}

/// 同步把目录打成 tar.gz 落到备份根目录：`{label}_{ts}_predestroy.tar.gz`。
/// 阻塞操作，异步上下文请经 `crate::error::blocking` 调用。
/// 失败返回 Err，调用方应中止后续的破坏性操作。
pub fn snapshot_dir_blocking(dir: &Path, label: &str) -> AppResult<String> {
    if !dir.is_dir() {
        return Err(AppError::bad_gateway("数据目录不存在，无法生成快照"));
    }
    let root = backup_root();
    std::fs::create_dir_all(&root)
        .map_err(|e| AppError::bad_gateway(format!("备份目录创建失败：{e}")))?;
    let fname = format!("{}_{}_predestroy.tar.gz", snap_label(label), ts());
    let dest = Path::new(&root).join(&fname);
    let f = std::fs::File::create(&dest)
        .map_err(|e| AppError::bad_gateway(format!("快照文件创建失败：{e}")))?;
    let enc = flate2::write::GzEncoder::new(f, flate2::Compression::fast());
    let mut tb = tar::Builder::new(enc);
    tb.follow_symlinks(false);
    if let Err(e) = tb.append_dir_all(".", dir) {
        drop(tb);
        let _ = std::fs::remove_file(&dest);
        return Err(AppError::bad_gateway(format!("目录快照失败：{e}")));
    }
    let enc = match tb.into_inner() {
        Ok(e) => e,
        Err(e) => {
            let _ = std::fs::remove_file(&dest);
            return Err(AppError::bad_gateway(format!("目录快照失败：{e}")));
        }
    };
    enc.finish()
        .map_err(|e| AppError::bad_gateway(format!("目录快照失败：{e}")))?;
    cleanup_snapshots("predestroy", ".tar.gz", SNAPSHOT_KEEP);
    Ok(fname)
}

/// 把 tar.gz 快照解包覆盖到宿主目录（快照恢复用）。
/// tar crate 的 unpack 默认拒绝路径穿越条目（../ 等），异常归档会直接报错。
/// 阻塞操作，异步上下文请经 `crate::error::blocking` 调用。
pub fn restore_snapshot_blocking(archive: &Path, dest: &Path) -> AppResult<()> {
    if !archive.is_file() {
        return Err(AppError::not_found("快照文件不存在"));
    }
    std::fs::create_dir_all(dest)
        .map_err(|e| AppError::bad_gateway(format!("数据目录创建失败：{e}")))?;
    let f = std::fs::File::open(archive)
        .map_err(|e| AppError::bad_gateway(format!("快照文件打开失败：{e}")))?;
    let gz = flate2::read::GzDecoder::new(f);
    let mut tb = tar::Archive::new(gz);
    tb.set_preserve_permissions(true);
    tb.set_overwrite(true);
    tb.unpack(dest)
        .map_err(|e| AppError::bad_gateway(format!("快照解包失败：{e}")))?;
    Ok(())
}

/// 按标记清理旧快照：文件名含 `tag` 且以 `ext` 结尾的，按修改时间保留最近 keep 份。
/// 返回删除数量。自动快照与自备份共用（手动备份的文件名不含标记，不受影响）。
pub fn cleanup_snapshots(tag: &str, ext: &str, keep: usize) -> usize {
    let root = backup_root();
    let mut files: Vec<(String, u64)> = Vec::new();
    if let Ok(rd) = std::fs::read_dir(&root) {
        for ent in rd.flatten() {
            let name = ent.file_name().to_string_lossy().to_string();
            if !name.contains(tag) || !name.ends_with(ext) {
                continue;
            }
            let Ok(md) = ent.metadata() else { continue };
            if !md.is_file() {
                continue;
            }
            let mtime = md
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_secs())
                .unwrap_or(0);
            files.push((name, mtime));
        }
    }
    files.sort_by(|a, b| b.1.cmp(&a.1));
    let mut removed = 0;
    for (name, _) in files.into_iter().skip(keep) {
        if std::fs::remove_file(Path::new(&root).join(&name)).is_ok() {
            removed += 1;
        }
    }
    removed
}
