"""文件服务：所有操作都在实例根目录内进行，路径穿越防护是生死线。"""
import base64
import os
import shutil
import tarfile
import tempfile
import zipfile

from fastapi import HTTPException

# 在线编辑的文件大小上限
MAX_EDIT_BYTES = 2 * 1024 * 1024
# 图片预览上限
MAX_PREVIEW_BYTES = 8 * 1024 * 1024
IMG_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
    ".ico": "image/x-icon", ".svg": "image/svg+xml",
}


def resolve_path(root: str, rel_path: str) -> str:
    """把用户传入的相对路径解析为实例根目录内的绝对路径。

    安全红线 1：realpath 消解符号链接与 ..，结果必须落在实例根目录内，否则 403。
    """
    root_real = os.path.realpath(root)
    rel = (rel_path or "").strip().lstrip("/\\")
    target = os.path.realpath(os.path.join(root_real, rel))
    if target != root_real and not target.startswith(root_real + os.sep):
        raise HTTPException(status_code=403, detail="非法路径：禁止访问实例目录之外的内容")
    return target


def ensure_root(root: str) -> None:
    os.makedirs(root, exist_ok=True)


def list_dir(root: str, rel_path: str) -> list[dict]:
    target = resolve_path(root, rel_path)
    if not os.path.isdir(target):
        raise HTTPException(status_code=404, detail="目录不存在")
    entries = []
    try:
        names = os.listdir(target)
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权限读取该目录")
    for name in names:
        full = os.path.join(target, name)
        try:
            st = os.stat(full)
            entries.append({
                "name": name,
                "is_dir": os.path.isdir(full),
                "size": 0 if os.path.isdir(full) else st.st_size,
                "mtime": st.st_mtime,
            })
        except OSError:
            continue
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].casefold()))
    return entries


def read_text(root: str, rel_path: str) -> dict:
    target = resolve_path(root, rel_path)
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="文件不存在")
    size = os.path.getsize(target)
    if size > MAX_EDIT_BYTES:
        raise HTTPException(status_code=413, detail="文件过大，仅支持在线编辑 2MB 以内的文本文件")
    with open(target, "rb") as f:
        raw = f.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="仅支持 UTF-8 编码的文本文件在线编辑")
    return {"name": os.path.basename(target), "size": size, "content": content}


def write_text(root: str, rel_path: str, content: str) -> None:
    target = resolve_path(root, rel_path)
    if len(content.encode("utf-8")) > MAX_EDIT_BYTES:
        raise HTTPException(status_code=413, detail="保存内容超过 2MB 限制")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as f:
        f.write(content)


def save_upload(root: str, rel_dir: str, filename: str, src_file, limit: int) -> dict:
    """保存上传的临时文件（src_file 为已落盘的 SpooledTemporaryFile），边写边限制大小。"""
    target_dir = resolve_path(root, rel_dir)
    if not os.path.isdir(target_dir):
        raise HTTPException(status_code=404, detail="目标目录不存在")
    name = sanitize_name(filename)
    target = os.path.join(target_dir, name)
    written = 0
    try:
        with open(target, "wb") as out:
            src_file.seek(0)
            while True:
                chunk = src_file.read(1024 * 256)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise HTTPException(
                        status_code=413, detail=f"文件超过单文件 {limit // 1024 // 1024}MB 上限")
                out.write(chunk)
    except HTTPException:
        if os.path.exists(target):
            os.remove(target)
        raise
    return {"name": name, "size": written}


def sanitize_name(filename: str) -> str:
    name = os.path.basename((filename or "").replace("\\", "/")).strip()
    if not name or name in (".", "..") or "\x00" in name:
        raise HTTPException(status_code=400, detail="非法文件名")
    return name


def mkdir(root: str, rel_path: str) -> None:
    target = resolve_path(root, rel_path)
    if os.path.exists(target):
        raise HTTPException(status_code=409, detail="目录或文件已存在")
    os.makedirs(target)


def rename(root: str, rel_src: str, rel_dst: str) -> None:
    src = resolve_path(root, rel_src)
    dst = resolve_path(root, rel_dst)
    if src == root:
        raise HTTPException(status_code=400, detail="不能重命名根目录")
    if not os.path.exists(src):
        raise HTTPException(status_code=404, detail="源文件或目录不存在")
    if os.path.exists(dst):
        raise HTTPException(status_code=409, detail="目标名称已存在")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.rename(src, dst)


def delete(root: str, rel_path: str) -> None:
    target = resolve_path(root, rel_path)
    if target == root:
        raise HTTPException(status_code=400, detail="不能删除根目录")
    if not os.path.exists(target):
        raise HTTPException(status_code=404, detail="文件或目录不存在")
    if os.path.isdir(target):
        shutil.rmtree(target)
    else:
        os.unlink(target)


# ---------- 复制 / 压缩 / 解压 / 目录大小 / 图片预览 ----------

def copy_path(root: str, rel_src: str, rel_dst: str) -> None:
    src = resolve_path(root, rel_src)
    dst = resolve_path(root, rel_dst)
    if src == root:
        raise HTTPException(status_code=400, detail="不能复制根目录")
    if not os.path.exists(src):
        raise HTTPException(status_code=404, detail="源文件或目录不存在")
    if os.path.exists(dst):
        raise HTTPException(status_code=409, detail="目标名称已存在")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isdir(src):
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def zip_entries(root: str, rel_paths: list[str], rel_name: str, z_type: str) -> None:
    """把多个文件/目录压缩为实例目录内的一个压缩包（支持 tar.gz / zip）。"""
    dst = resolve_path(root, rel_name)
    if os.path.exists(dst):
        raise HTTPException(status_code=409, detail="同名压缩包已存在")
    sources = []
    for p in rel_paths:
        target = resolve_path(root, p)
        if target == root:
            raise HTTPException(status_code=400, detail="不能压缩根目录本身")
        if not os.path.exists(target):
            raise HTTPException(status_code=404, detail=f"不存在：{p}")
        sources.append(target)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        if z_type == "zip":
            with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
                for target in sources:
                    if os.path.isdir(target):
                        base = os.path.dirname(target)
                        for cur, _dirs, files in os.walk(target):
                            for f in files:
                                full = os.path.join(cur, f)
                                zf.write(full, os.path.relpath(full, base))
                    else:
                        zf.write(target, os.path.basename(target))
        elif z_type in ("tar.gz", "tgz"):
            with tarfile.open(dst, "w:gz") as tf:
                for target in sources:
                    tf.add(target, arcname=os.path.basename(target))
        else:
            raise HTTPException(status_code=400, detail="仅支持 tar.gz / zip 格式")
    except HTTPException:
        if os.path.exists(dst):
            os.remove(dst)
        raise
    except Exception:
        if os.path.exists(dst):
            os.remove(dst)
        raise HTTPException(status_code=500, detail="压缩失败")


def unzip_entry(root: str, rel_src: str) -> str:
    """解压压缩包到其所在目录，返回解压目标目录。"""
    src = resolve_path(root, rel_src)
    if not os.path.isfile(src):
        raise HTTPException(status_code=404, detail="压缩包不存在")
    dest_dir = os.path.dirname(src)
    name = os.path.basename(src).lower()
    try:
        if name.endswith((".tar.gz", ".tgz", ".tar")):
            mode = "r:gz" if name.endswith((".tar.gz", ".tgz")) else "r:"
            with tarfile.open(src, mode) as tf:
                for m in _safe_members(tf):
                    tf.extract(m, dest_dir)
        elif name.endswith(".zip"):
            with zipfile.ZipFile(src) as zf:
                for info in zf.infolist():
                    parts = info.filename.replace("\\", "/").split("/")
                    if any(p == ".." for p in parts):
                        raise HTTPException(status_code=400, detail=f"压缩包含非法路径：{info.filename}")
                zf.extractall(dest_dir)
        else:
            raise HTTPException(status_code=400, detail="仅支持 tar.gz / tar / zip 格式")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="解压失败")
    return dest_dir


def dir_size(root: str, rel_path: str) -> int:
    target = resolve_path(root, rel_path)
    if not os.path.exists(target):
        raise HTTPException(status_code=404, detail="路径不存在")
    total = 0
    if os.path.isfile(target):
        return os.path.getsize(target)
    for cur, _dirs, files in os.walk(target):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(cur, f))
            except OSError:
                continue
    return total


def image_preview(root: str, rel_path: str) -> dict:
    ext = os.path.splitext(rel_path)[1].lower()
    if ext not in IMG_MIME:
        raise HTTPException(status_code=400, detail="不支持的图片格式")
    target = resolve_path(root, rel_path)
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="文件不存在")
    size = os.path.getsize(target)
    if size > MAX_PREVIEW_BYTES:
        raise HTTPException(status_code=413, detail="图片超过 8MB，无法预览")
    with open(target, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return {"name": os.path.basename(target), "url": f"data:{IMG_MIME[ext]};base64,{b64}"}


# ---------- 分片上传（宝塔式追加协议，支持断点续传） ----------

def upload_exists(root: str, rel_dir: str, file_name: str, total_size: int) -> dict:
    """查询目标文件断点：已传字节数即续传起点；超过总大小则重传。"""
    target = resolve_path(root, os.path.join(rel_dir, file_name)) if rel_dir else resolve_path(root, file_name)
    if os.path.isfile(target):
        start = os.path.getsize(target)
        if total_size and start > total_size:
            start = 0
        return {"status": True, "start": start}
    return {"status": True, "start": 0}


def upload_chunk(root: str, rel_dir: str, file_name: str, total_size: int,
                 start: int, stream) -> dict:
    """按 start 偏移写入分片：start=0 截断新建，>0 追加（先对齐已有长度）。"""
    target = resolve_path(root, os.path.join(rel_dir, file_name)) if rel_dir else resolve_path(root, file_name)
    if os.path.isdir(target):
        raise HTTPException(status_code=400, detail="同名目录已存在")
    if start < 0:
        raise HTTPException(status_code=400, detail="偏移无效")
    if os.path.basename(file_name) != file_name or file_name in (".", ".."):
        raise HTTPException(status_code=400, detail="文件名不合法")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if start == 0:
        mode = "wb"
    else:
        if not os.path.isfile(target):
            raise HTTPException(status_code=409, detail="续传文件不存在，请从头上传")
        cur = os.path.getsize(target)
        if cur > start:
            os.truncate(target, start)  # 断点后残留脏数据，截断对齐
        elif cur < start:
            raise HTTPException(status_code=409, detail="上传偏移超前，请从头上传")
        mode = "ab"
    written = 0
    try:
        with open(target, mode) as f:
            while True:
                block = stream.read(256 * 1024)
                if not block:
                    break
                f.write(block)
                written += len(block)
    except OSError:
        raise HTTPException(status_code=500, detail="写入失败（磁盘已满或权限不足）")
    return {"status": True, "start": start + written}


# ---------- 备份 / 恢复（整个实例目录 tar.gz） ----------

def create_backup(root: str) -> str:
    """把实例根目录打包为 tar.gz，返回临时文件路径（调用方负责用完删除）。"""
    root_real = os.path.realpath(root)
    if not os.path.isdir(root_real):
        raise HTTPException(status_code=404, detail="实例目录不存在")
    fd, tmp_path = tempfile.mkstemp(prefix="ppanel_backup_", suffix=".tar.gz")
    os.close(fd)
    try:
        with tarfile.open(tmp_path, "w:gz") as tf:
            for name in os.listdir(root_real):
                tf.add(os.path.join(root_real, name), arcname=name)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return tmp_path


def _safe_members(tf: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """解包前的安全审查：拒绝绝对路径 / .. 穿越 / 链接（防逃逸）。"""
    members = []
    for m in tf.getmembers():
        name = m.name.replace("\\", "/").lstrip("/")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise HTTPException(status_code=400, detail=f"压缩包含非法路径：{name}")
        if m.issym() or m.islnk():
            continue  # 跳过软硬链接，避免目录逃逸
        m.name = "/".join(parts)
        members.append(m)
    return members


def _extract_all(tf: tarfile.TarFile, path: str) -> None:
    try:
        tf.extractall(path, filter="data")  # Python 3.12+
    except TypeError:
        tf.extractall(path)


def restore_backup(root: str, src_file, max_bytes: int) -> dict:
    """从上传的 tar.gz 恢复实例目录：先解压到临时目录校验，再整体替换。"""
    root_real = os.path.realpath(root)
    os.makedirs(root_real, exist_ok=True)
    src_file.seek(0)
    tmp = os.path.join(os.path.dirname(root_real),
                       f".restore_{os.path.basename(root_real)}_{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        with tarfile.open(fileobj=src_file, mode="r:gz") as tf:
            members = _safe_members(tf)
            total = sum(m.size for m in members)
            if total > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"解压后总量 {total // 1024 // 1024}MB 超过配额上限")
            _extract_all(tf, tmp)
        # 校验通过后替换：清空旧内容 → 挪入新内容
        for name in os.listdir(root_real):
            p = os.path.join(root_real, name)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
        for name in os.listdir(tmp):
            shutil.move(os.path.join(tmp, name), os.path.join(root_real, name))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"detail": "恢复完成，旧目录内容已被替换"}
