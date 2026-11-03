//! 文件服务：所有操作限制在实例根目录内（路径穿越防护是生死线）。

use std::fs;
use std::io::{self, Read, Seek, Write};
use std::path::{Path, PathBuf};

use base64::Engine;
use flate2::read::GzDecoder;
use flate2::write::GzEncoder;
use flate2::Compression;

use crate::error::{AppError, AppResult};

pub const MAX_EDIT_BYTES: u64 = 2 * 1024 * 1024;
pub const MAX_PREVIEW_BYTES: u64 = 8 * 1024 * 1024;

fn img_mime(ext: &str) -> Option<&'static str> {
    Some(match ext {
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "bmp" => "image/bmp",
        "webp" => "image/webp",
        "ico" => "image/x-icon",
        "svg" => "image/svg+xml",
        _ => return None,
    })
}

/// realpath 消解符号链接与 `..`；路径不存在时对最深的已存在祖先求 realpath。
pub fn realpath_loose(path: &Path) -> PathBuf {
    if let Ok(p) = fs::canonicalize(path) {
        return p;
    }
    let mut missing: Vec<String> = Vec::new();
    let mut cur = path.to_path_buf();
    loop {
        match cur.parent() {
            Some(parent) => {
                if let Some(name) = cur.file_name() {
                    missing.push(name.to_string_lossy().to_string());
                }
                if let Ok(p) = fs::canonicalize(parent) {
                    let mut out = p;
                    for seg in missing.iter().rev() {
                        out.push(seg);
                    }
                    return out;
                }
                cur = parent.to_path_buf();
            }
            None => return path.to_path_buf(),
        }
    }
}

pub fn resolve_path(root: &str, rel_path: &str) -> AppResult<PathBuf> {
    let root_real = realpath_loose(Path::new(root));
    let rel = rel_path.trim().trim_start_matches(['/', '\\']);
    let target = realpath_loose(&root_real.join(rel));
    let root_is_fs_root = root_real == Path::new("/");
    if !root_is_fs_root && target != root_real && !target.starts_with(&root_real) {
        return Err(AppError::forbidden("非法路径：禁止访问实例目录之外的内容"));
    }
    Ok(target)
}

pub fn ensure_root(root: &str) {
    let _ = fs::create_dir_all(root);
}

pub fn list_dir(root: &str, rel_path: &str) -> AppResult<Vec<serde_json::Value>> {
    let target = resolve_path(root, rel_path)?;
    if !target.is_dir() {
        return Err(AppError::not_found("目录不存在"));
    }
    let rd = fs::read_dir(&target).map_err(|_| AppError::forbidden("无权限读取该目录"))?;
    let mut entries: Vec<serde_json::Value> = Vec::new();
    for ent in rd.flatten() {
        let name = ent.file_name().to_string_lossy().to_string();
        let Ok(md) = ent.metadata() else { continue };
        let is_dir = md.is_dir();
        let mtime = md
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        #[cfg(unix)]
        let (mode, mode_str) = {
            use std::os::unix::fs::PermissionsExt;
            let m = md.permissions().mode() & 0o7777;
            (format!("{m:04o}"), mode_ls_style(is_dir, m))
        };
        #[cfg(not(unix))]
        let (mode, mode_str) = (String::new(), String::new());
        entries.push(serde_json::json!({
            "name": name,
            "is_dir": is_dir,
            "size": if is_dir { 0 } else { md.len() },
            "mtime": mtime,
            "mode": mode,
            "mode_str": mode_str,
        }));
    }
    entries.sort_by(|a, b| {
        let ad = a["is_dir"].as_bool().unwrap_or(false);
        let bd = b["is_dir"].as_bool().unwrap_or(false);
        let an = a["name"].as_str().unwrap_or("").to_lowercase();
        let bn = b["name"].as_str().unwrap_or("").to_lowercase();
        bd.cmp(&ad).then(an.cmp(&bn))
    });
    Ok(entries)
}

/// 八进制权限转 ls 风格字符串：0644 → `-rw-r--r--`，目录 0755 → `drwxr-xr-x`。
#[cfg(unix)]
fn mode_ls_style(is_dir: bool, mode: u32) -> String {
    const TRI: [&str; 8] = ["---", "--x", "-w-", "-wx", "r--", "r-x", "rw-", "rwx"];
    let mut s = String::with_capacity(10);
    s.push(if is_dir { 'd' } else { '-' });
    for shift in [6u32, 3, 0] {
        let bits = ((mode >> shift) & 0o7) as usize;
        s.push_str(TRI[bits]);
    }
    s
}

/// 修改文件/目录权限（八进制，如 755 / 0644）。支持递归。
#[cfg(unix)]
pub fn chmod_path(root: &str, rel_path: &str, mode: u32, recursive: bool) -> AppResult<()> {
    use std::os::unix::fs::PermissionsExt;
    let target = resolve_path(root, rel_path)?;
    if !target.exists() {
        return Err(AppError::not_found("文件或目录不存在"));
    }
    fs::set_permissions(&target, fs::Permissions::from_mode(mode))?;
    if recursive && target.is_dir() {
        chmod_recursive(&target, mode)?;
    }
    Ok(())
}

#[cfg(not(unix))]
pub fn chmod_path(_root: &str, _rel_path: &str, _mode: u32, _recursive: bool) -> AppResult<()> {
    Err(AppError::bad_request("当前平台不支持修改权限"))
}

#[cfg(unix)]
fn chmod_recursive(dir: &Path, mode: u32) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    for ent in fs::read_dir(dir)? {
        let ent = ent?;
        let p = ent.path();
        let md = ent.metadata()?;
        fs::set_permissions(&p, fs::Permissions::from_mode(mode))?;
        if md.is_dir() {
            chmod_recursive(&p, mode)?;
        }
    }
    Ok(())
}

pub fn read_text(root: &str, rel_path: &str) -> AppResult<serde_json::Value> {
    let target = resolve_path(root, rel_path)?;
    if !target.is_file() {
        return Err(AppError::not_found("文件不存在"));
    }
    let size = fs::metadata(&target)?.len();
    if size > MAX_EDIT_BYTES {
        return Err(AppError::too_large("文件过大，仅支持在线编辑 2MB 以内的文本文件"));
    }
    let raw = fs::read(&target)?;
    let content = String::from_utf8(raw)
        .map_err(|_| AppError::bad_request("仅支持 UTF-8 编码的文本文件在线编辑"))?;
    let name = target
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_default();
    Ok(serde_json::json!({"name": name, "size": size, "content": content}))
}

pub fn write_text(root: &str, rel_path: &str, content: &str) -> AppResult<()> {
    let target = resolve_path(root, rel_path)?;
    if content.len() as u64 > MAX_EDIT_BYTES {
        return Err(AppError::too_large("保存内容超过 2MB 限制"));
    }
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(&target, content)?;
    Ok(())
}

pub fn sanitize_name(filename: &str) -> AppResult<String> {
    let base = filename.replace('\\', "/");
    let name = base.rsplit('/').next().unwrap_or("").trim().to_string();
    if name.is_empty() || name == "." || name == ".." || name.contains('\0') {
        return Err(AppError::bad_request("非法文件名"));
    }
    Ok(name)
}

pub fn save_upload(root: &str, rel_dir: &str, filename: &str, data: &[u8], limit: u64) -> AppResult<serde_json::Value> {
    let dir = resolve_path(root, rel_dir)?;
    if !dir.is_dir() {
        return Err(AppError::not_found("目标目录不存在"));
    }
    let name = sanitize_name(filename)?;
    if data.len() as u64 > limit {
        return Err(AppError::too_large(format!(
            "文件超过单文件 {}MB 上限",
            limit / 1024 / 1024
        )));
    }
    let target = dir.join(&name);
    let mut f = fs::File::create(&target)?;
    if let Err(e) = f.write_all(data) {
        drop(f);
        let _ = fs::remove_file(&target);
        return Err(AppError::internal(format!("写入失败：{e}")));
    }
    Ok(serde_json::json!({"name": name, "size": data.len()}))
}

pub fn mkdir(root: &str, rel_path: &str) -> AppResult<()> {
    let target = resolve_path(root, rel_path)?;
    if target.exists() {
        return Err(AppError::conflict("目录或文件已存在"));
    }
    fs::create_dir_all(&target)?;
    Ok(())
}

pub fn rename(root: &str, rel_src: &str, rel_dst: &str) -> AppResult<()> {
    let src = resolve_path(root, rel_src)?;
    let dst = resolve_path(root, rel_dst)?;
    if src == realpath_loose(Path::new(root)) {
        return Err(AppError::bad_request("不能重命名根目录"));
    }
    if !src.exists() {
        return Err(AppError::not_found("源文件或目录不存在"));
    }
    if dst.exists() {
        return Err(AppError::conflict("目标名称已存在"));
    }
    if let Some(parent) = dst.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::rename(&src, &dst)?;
    Ok(())
}

pub fn delete(root: &str, rel_path: &str) -> AppResult<()> {
    let target = resolve_path(root, rel_path)?;
    if target == realpath_loose(Path::new(root)) {
        return Err(AppError::bad_request("不能删除根目录"));
    }
    if !target.exists() {
        return Err(AppError::not_found("文件或目录不存在"));
    }
    if target.is_dir() {
        fs::remove_dir_all(&target)?;
    } else {
        fs::remove_file(&target)?;
    }
    Ok(())
}

pub fn copy_path(root: &str, rel_src: &str, rel_dst: &str) -> AppResult<()> {
    let src = resolve_path(root, rel_src)?;
    let dst = resolve_path(root, rel_dst)?;
    if src == realpath_loose(Path::new(root)) {
        return Err(AppError::bad_request("不能复制根目录"));
    }
    if !src.exists() {
        return Err(AppError::not_found("源文件或目录不存在"));
    }
    if dst.exists() {
        return Err(AppError::conflict("目标名称已存在"));
    }
    if let Some(parent) = dst.parent() {
        fs::create_dir_all(parent)?;
    }
    copy_recursive(&src, &dst)?;
    Ok(())
}

fn copy_recursive(src: &Path, dst: &Path) -> io::Result<()> {
    if src.is_dir() {
        fs::create_dir_all(dst)?;
        for ent in fs::read_dir(src)?.flatten() {
            copy_recursive(&ent.path(), &dst.join(ent.file_name()))?;
        }
    } else {
        fs::copy(src, dst)?;
    }
    Ok(())
}

fn walk_files(dir: &Path, out: &mut Vec<PathBuf>) {
    if let Ok(rd) = fs::read_dir(dir) {
        for ent in rd.flatten() {
            let p = ent.path();
            if p.is_dir() {
                walk_files(&p, out);
            } else {
                out.push(p);
            }
        }
    }
}

fn rel_of<'a>(path: &'a Path, base: &'a Path) -> String {
    path.strip_prefix(base)
        .map(|p| p.to_string_lossy().replace('\\', "/"))
        .unwrap_or_else(|_| path.to_string_lossy().replace('\\', "/"))
}

pub fn zip_entries(root: &str, rel_paths: &[String], rel_name: &str, z_type: &str) -> AppResult<()> {
    let dst = resolve_path(root, rel_name)?;
    if dst.exists() {
        return Err(AppError::conflict("同名压缩包已存在"));
    }
    let root_real = realpath_loose(Path::new(root));
    let mut sources = Vec::new();
    for p in rel_paths {
        let target = resolve_path(root, p)?;
        if target == root_real {
            return Err(AppError::bad_request("不能压缩根目录本身"));
        }
        if !target.exists() {
            return Err(AppError::not_found(format!("不存在：{p}")));
        }
        sources.push(target);
    }
    if let Some(parent) = dst.parent() {
        fs::create_dir_all(parent)?;
    }
    let res: AppResult<()> = (|| {
        if z_type == "zip" {
            let f = fs::File::create(&dst)?;
            let mut zw = zip::ZipWriter::new(f);
            let opts = zip::write::SimpleFileOptions::default()
                .compression_method(zip::CompressionMethod::Deflated);
            for target in &sources {
                if target.is_dir() {
                    let base = target.parent().unwrap_or(Path::new("/"));
                    let mut files = Vec::new();
                    walk_files(target, &mut files);
                    for f in files {
                        let name = rel_of(&f, base);
                        zw.start_file(name, opts)?;
                        let mut fh = fs::File::open(&f)?;
                        io::copy(&mut fh, &mut zw)?;
                    }
                } else {
                    let name = target.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
                    zw.start_file(name, opts)?;
                    let mut fh = fs::File::open(target)?;
                    io::copy(&mut fh, &mut zw)?;
                }
            }
            zw.finish()?;
        } else if z_type == "tar.gz" || z_type == "tgz" {
            let f = fs::File::create(&dst)?;
            let enc = GzEncoder::new(f, Compression::default());
            let mut tb = tar::Builder::new(enc);
            for target in &sources {
                let name = target.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
                if target.is_dir() {
                    tb.append_dir_all(&name, target)?;
                } else {
                    tb.append_path_with_name(target, &name)?;
                }
            }
            tb.finish()?;
        } else {
            return Err(AppError::bad_request("仅支持 tar.gz / zip 格式"));
        }
        Ok(())
    })();
    if res.is_err() && dst.exists() {
        let _ = fs::remove_file(&dst);
    }
    res
}

fn safe_join(base: &Path, name: &str) -> AppResult<Option<PathBuf>> {
    let norm = name.replace('\\', "/");
    let norm = norm.trim_start_matches('/');
    let mut out = base.to_path_buf();
    for part in norm.split('/') {
        if part.is_empty() || part == "." {
            continue;
        }
        if part == ".." {
            return Err(AppError::bad_request(format!("压缩包含非法路径：{name}")));
        }
        out.push(part);
    }
    Ok(Some(out))
}

pub fn unzip_entry(root: &str, rel_src: &str) -> AppResult<String> {
    let src = resolve_path(root, rel_src)?;
    if !src.is_file() {
        return Err(AppError::not_found("压缩包不存在"));
    }
    let dest_dir = src.parent().unwrap_or(Path::new("/")).to_path_buf();
    let lname = src.file_name().map(|s| s.to_string_lossy().to_lowercase()).unwrap_or_default();
    if lname.ends_with(".tar.gz") || lname.ends_with(".tgz") || lname.ends_with(".tar") {
        let f = fs::File::open(&src)?;
        let reader: Box<dyn Read> = if lname.ends_with(".tar") {
            Box::new(f)
        } else {
            Box::new(GzDecoder::new(f))
        };
        extract_tar(reader, &dest_dir).map_err(|e| {
            if e.status == axum::http::StatusCode::BAD_REQUEST {
                e
            } else {
                AppError::internal("解压失败")
            }
        })?;
    } else if lname.ends_with(".zip") {
        let f = fs::File::open(&src)?;
        let mut zr = zip::ZipArchive::new(f).map_err(|_| AppError::internal("解压失败"))?;
        for i in 0..zr.len() {
            let mut file = zr.by_index(i).map_err(|_| AppError::internal("解压失败"))?;
            let Some(out) = safe_join(&dest_dir, file.name())? else { continue };
            if file.is_dir() {
                fs::create_dir_all(&out)?;
            } else {
                if let Some(parent) = out.parent() {
                    fs::create_dir_all(parent)?;
                }
                let mut fh = fs::File::create(&out)?;
                io::copy(&mut file, &mut fh)?;
            }
        }
    } else {
        return Err(AppError::bad_request("仅支持 tar.gz / tar / zip 格式"));
    }
    Ok(dest_dir.to_string_lossy().to_string())
}

/// 解包：拒绝绝对路径/`..`，跳过链接。
fn extract_tar(reader: Box<dyn Read>, dest: &Path) -> AppResult<()> {
    let mut archive = tar::Archive::new(reader);
    let entries = archive.entries().map_err(|_| AppError::internal("解压失败"))?;
    for entry in entries {
        let mut entry = entry.map_err(|_| AppError::internal("解压失败"))?;
        let path = entry.path().map(|p| p.to_string_lossy().to_string()).unwrap_or_default();
        if entry.header().entry_type().is_symlink() || entry.header().entry_type().is_hard_link() {
            continue;
        }
        let Some(out) = safe_join(dest, &path)? else { continue };
        if entry.header().entry_type().is_dir() {
            fs::create_dir_all(&out)?;
        } else {
            if let Some(parent) = out.parent() {
                fs::create_dir_all(parent)?;
            }
            let mut fh = fs::File::create(&out)?;
            io::copy(&mut entry, &mut fh)?;
        }
    }
    Ok(())
}

pub fn dir_size(root: &str, rel_path: &str) -> AppResult<u64> {
    let target = resolve_path(root, rel_path)?;
    if !target.exists() {
        return Err(AppError::not_found("路径不存在"));
    }
    if target.is_file() {
        return Ok(fs::metadata(&target).map(|m| m.len()).unwrap_or(0));
    }
    let mut total = 0u64;
    let mut files = Vec::new();
    walk_files(&target, &mut files);
    for f in files {
        total += fs::metadata(&f).map(|m| m.len()).unwrap_or(0);
    }
    Ok(total)
}

pub fn image_preview(root: &str, rel_path: &str) -> AppResult<serde_json::Value> {
    let ext = Path::new(rel_path)
        .extension()
        .map(|s| s.to_string_lossy().to_lowercase())
        .unwrap_or_default();
    let mime = img_mime(&ext).ok_or_else(|| AppError::bad_request("不支持的图片格式"))?;
    let target = resolve_path(root, rel_path)?;
    if !target.is_file() {
        return Err(AppError::not_found("文件不存在"));
    }
    let size = fs::metadata(&target)?.len();
    if size > MAX_PREVIEW_BYTES {
        return Err(AppError::too_large("图片超过 8MB，无法预览"));
    }
    let b64 = base64::engine::general_purpose::STANDARD.encode(fs::read(&target)?);
    let name = target.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
    Ok(serde_json::json!({"name": name, "url": format!("data:{mime};base64,{b64}")}))
}

pub fn upload_exists(root: &str, rel_dir: &str, file_name: &str, total_size: i64) -> AppResult<serde_json::Value> {
    let rel = if rel_dir.trim_matches('/').is_empty() {
        file_name.to_string()
    } else {
        format!("{}/{}", rel_dir.trim_end_matches('/'), file_name)
    };
    let target = resolve_path(root, &rel)?;
    if target.is_file() {
        let mut start = fs::metadata(&target).map(|m| m.len()).unwrap_or(0);
        if total_size > 0 && start as i64 > total_size {
            start = 0;
        }
        return Ok(serde_json::json!({"status": true, "start": start}));
    }
    Ok(serde_json::json!({"status": true, "start": 0}))
}

pub fn upload_chunk(
    root: &str,
    rel_dir: &str,
    file_name: &str,
    _total_size: i64,
    start: i64,
    data: &[u8],
) -> AppResult<serde_json::Value> {
    if Path::new(file_name).file_name().map(|s| s.to_string_lossy().to_string()) != Some(file_name.to_string())
        || file_name == "." || file_name == ".."
    {
        return Err(AppError::bad_request("文件名不合法"));
    }
    let rel = if rel_dir.trim_matches('/').is_empty() {
        file_name.to_string()
    } else {
        format!("{}/{}", rel_dir.trim_end_matches('/'), file_name)
    };
    let target = resolve_path(root, &rel)?;
    if target.is_dir() {
        return Err(AppError::bad_request("同名目录已存在"));
    }
    if start < 0 {
        return Err(AppError::bad_request("偏移无效"));
    }
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)?;
    }
    if start == 0 {
        fs::write(&target, data)?;
    } else {
        if !target.is_file() {
            return Err(AppError::conflict("续传文件不存在，请从头上传"));
        }
        let cur = fs::metadata(&target)?.len() as i64;
        if cur > start {
            let f = fs::OpenOptions::new().write(true).open(&target)?;
            f.set_len(start as u64)?;
        } else if cur < start {
            return Err(AppError::conflict("上传偏移超前，请从头上传"));
        }
        let mut f = fs::OpenOptions::new().append(true).open(&target)?;
        f.write_all(data)?;
    }
    Ok(serde_json::json!({"status": true, "start": start + data.len() as i64}))
}

pub fn create_backup(root: &str) -> AppResult<PathBuf> {
    let root_real = realpath_loose(Path::new(root));
    if !root_real.is_dir() {
        return Err(AppError::not_found("实例目录不存在"));
    }
    let tmp_path = std::env::temp_dir().join(format!("ppanel_backup_{}.tar.gz", uuid::Uuid::new_v4().simple()));
    let res: AppResult<()> = (|| {
        let f = fs::File::create(&tmp_path)?;
        let enc = GzEncoder::new(f, Compression::default());
        let mut tb = tar::Builder::new(enc);
        for ent in fs::read_dir(&root_real)?.flatten() {
            let name = ent.file_name().to_string_lossy().to_string();
            let p = ent.path();
            if p.is_dir() {
                tb.append_dir_all(&name, &p)?;
            } else {
                tb.append_path_with_name(&p, &name)?;
            }
        }
        tb.finish()?;
        Ok(())
    })();
    if res.is_err() {
        let _ = fs::remove_file(&tmp_path);
    }
    res.map(|_| tmp_path)
}

pub fn restore_backup(root: &str, data: &[u8], max_bytes: u64) -> AppResult<serde_json::Value> {
    let root_real = realpath_loose(Path::new(root));
    fs::create_dir_all(&root_real)?;
    let tmp = root_real
        .parent()
        .unwrap_or(Path::new("/"))
        .join(format!(".restore_{}_{}", root_real.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default(), std::process::id()));
    let _ = fs::remove_dir_all(&tmp);
    fs::create_dir_all(&tmp)?;
    let result: AppResult<()> = (|| {
        // 边解边统计解压总量，超配额立即中止（防止 ZIP 炸弹）
        let cursor = io::Cursor::new(data);
        let mut archive = tar::Archive::new(GzDecoder::new(cursor));
        let mut total = 0u64;
        let entries = archive.entries().map_err(|_| AppError::internal("解包失败"))?;
        for entry in entries {
            let mut entry = entry.map_err(|_| AppError::internal("解包失败"))?;
            let path = entry.path().map(|p| p.to_string_lossy().to_string()).unwrap_or_default();
            if entry.header().entry_type().is_symlink() || entry.header().entry_type().is_hard_link() {
                continue;
            }
            total += entry.header().size().unwrap_or(0);
            if total > max_bytes {
                return Err(AppError::too_large(format!(
                    "解压后总量超过配额上限（{}MB）",
                    max_bytes / 1024 / 1024
                )));
            }
            let Some(out) = safe_join(&tmp, &path)? else { continue };
            if entry.header().entry_type().is_dir() {
                fs::create_dir_all(&out)?;
            } else {
                if let Some(parent) = out.parent() {
                    fs::create_dir_all(parent)?;
                }
                let mut fh = fs::File::create(&out)?;
                io::copy(&mut entry, &mut fh)?;
            }
        }
        // 解包成功后替换
        if let Ok(rd) = fs::read_dir(&root_real) {
            for ent in rd.flatten() {
                let p = ent.path();
                if p.is_dir() && !p.is_symlink() {
                    let _ = fs::remove_dir_all(&p);
                } else {
                    let _ = fs::remove_file(&p);
                }
            }
        }
        if let Ok(rd) = fs::read_dir(&tmp) {
            for ent in rd.flatten() {
                let name = ent.file_name();
                let _ = fs::rename(ent.path(), root_real.join(name));
            }
        }
        Ok(())
    })();
    let _ = fs::remove_dir_all(&tmp);
    result.map(|_| serde_json::json!({"detail": "恢复完成，旧目录内容已被替换"}))
}

/// 用 Seek 读取大文件（备份下载等）。
#[allow(dead_code)]
pub fn open_read(path: &Path) -> AppResult<fs::File> {
    let mut f = fs::File::open(path)?;
    f.rewind()?;
    Ok(f)
}
