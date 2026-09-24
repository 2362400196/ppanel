"""宿主机文件管理 API：协议与独立面板 /panel/instance/files/* 完全一致。

root='/' 复用 file_service（实例文件同一套实现，含路径防护），
管理员可在整个宿主机文件系统上浏览/编辑/上传/复制/压缩解压。
"""
import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.agent_auth import require_node
from app.services import file_service as fs

router = APIRouter(dependencies=[Depends(require_node)])

# 宿主机保护：拒绝删除系统关键目录（仅删除操作需要）
_PROTECTED = {"/", "/boot", "/dev", "/etc", "/lib", "/proc", "/root",
              "/run", "/sbin", "/sys", "/usr", "/var", "/bin"}


def _guard(path: str) -> str:
    """删除保护：解析后的真实路径不得命中系统关键目录。"""
    real = os.path.realpath("/" + (path or "").lstrip("/"))
    if real in _PROTECTED:
        raise HTTPException(status_code=403, detail="拒绝删除系统关键目录")
    return path


@router.get("/host/files")
def list_files(path: str = "/"):
    return {"path": path, "entries": fs.list_dir("/", path)}


@router.get("/host/files/content")
def file_content(path: str = "/"):
    data = fs.read_text("/", path)
    data["path"] = path
    return data


@router.post("/host/files/save")
def save_file(body: dict = None):
    if not body or not body.get("path"):
        raise HTTPException(status_code=400, detail="参数缺失")
    fs.write_text("/", body["path"], body.get("content", ""))
    return {"detail": "已保存"}


@router.post("/host/files/mkdir")
def make_dir(body: dict = None):
    if not body or not body.get("path"):
        raise HTTPException(status_code=400, detail="参数缺失")
    fs.mkdir("/", body["path"])
    return {"detail": "已创建"}


@router.post("/host/files/rename")
def rename_path(body: dict = None):
    if not body or not body.get("src") or not body.get("dst"):
        raise HTTPException(status_code=400, detail="参数缺失")
    fs.rename("/", body["src"], body["dst"])
    return {"detail": "已重命名"}


@router.post("/host/files/copy")
def copy_path(body: dict = None):
    if not body or not body.get("src") or not body.get("dst"):
        raise HTTPException(status_code=400, detail="参数缺失")
    fs.copy_path("/", body["src"], body["dst"])
    return {"detail": "已复制"}


@router.delete("/host/files")
def delete_path(path: str = ""):
    fs.delete("/", _guard(path))
    return {"detail": "已删除"}


@router.get("/host/files/download")
def download(path: str = ""):
    target = fs.resolve_path("/", path)
    if not os.path.isfile(target):
        raise HTTPException(status_code=400, detail="仅支持下载文件")
    return FileResponse(target, filename=os.path.basename(target))


@router.post("/host/files/zip")
def zip_entries(body: dict = None):
    if not body or not body.get("paths") or not body.get("name"):
        raise HTTPException(status_code=400, detail="参数缺失")
    fs.zip_entries("/", body["paths"], body["name"], body.get("z_type", "tar.gz"))
    return {"detail": "压缩完成"}


@router.post("/host/files/unzip")
def unzip_entry(body: dict = None):
    if not body or not body.get("src"):
        raise HTTPException(status_code=400, detail="参数缺失")
    dest = fs.unzip_entry("/", body["src"])
    return {"detail": "解压完成", "dest": dest}


@router.get("/host/files/path-size")
def path_size(path: str = ""):
    return {"size": fs.dir_size("/", path)}


@router.get("/host/files/preview")
def preview(path: str = ""):
    return fs.image_preview("/", path)


@router.post("/host/files/upload-exists")
def upload_exists(body: dict = None):
    """分片上传断点查询：返回续传起点。"""
    if not body or not body.get("file_name"):
        raise HTTPException(status_code=400, detail="参数缺失")
    return fs.upload_exists("/", body.get("path", "/"),
                            body["file_name"], int(body.get("total_size", 0)))


@router.post("/host/files/upload-chunk")
async def upload_chunk(file: UploadFile = File(None), path: str = Form("/"),
                       file_name: str = Form(...), total_size: int = Form(0),
                       start: int = Form(0)):
    """分片上传：按 start 偏移追加写入（start=0 截断新建）。"""
    if file is None:
        raise HTTPException(status_code=400, detail="未收到分片数据")
    return fs.upload_chunk("/", path, file_name, total_size, start, file.file)
