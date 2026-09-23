"""文件管理器 API：直接操作宿主机挂载目录，容器停止状态下依然可用。"""
import os

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Instance
from app.routers.instances import get_owned_instance
from app.schemas import MkdirIn, RenameIn, SaveIn
from app.services import file_service as fs

router = APIRouter(tags=["files"])


def _root(inst: Instance) -> str:
    fs.ensure_root(inst.host_dir)
    return inst.host_dir


@router.get("/instances/{instance_id}/files")
def list_files(instance_id: int, path: str = "/",
               inst: Instance = Depends(get_owned_instance)):
    entries = fs.list_dir(_root(inst), path)
    return {"path": path, "entries": entries}


@router.get("/instances/{instance_id}/files/content")
def file_content(instance_id: int, path: str,
                 inst: Instance = Depends(get_owned_instance)):
    data = fs.read_text(_root(inst), path)
    data["path"] = path
    return data


@router.put("/instances/{instance_id}/files/upload")
def upload_file(instance_id: int, path: str = "/", file: UploadFile = None,
                inst: Instance = Depends(get_owned_instance)):
    if file is None:
        raise HTTPException(status_code=400, detail="未收到文件")
    limit = settings.upload_limit_mb * 1024 * 1024
    result = fs.save_upload(_root(inst), path, file.filename, file.file, limit)
    return result


@router.post("/instances/{instance_id}/files/save")
def save_file(instance_id: int, body: SaveIn, inst: Instance = Depends(get_owned_instance)):
    fs.write_text(_root(inst), body.path, body.content)
    return {"detail": "已保存"}


@router.post("/instances/{instance_id}/files/mkdir")
def mkdir(instance_id: int, body: MkdirIn, inst: Instance = Depends(get_owned_instance)):
    fs.mkdir(_root(inst), body.path)
    return {"detail": "已创建"}


@router.post("/instances/{instance_id}/files/rename")
def rename(instance_id: int, body: RenameIn, inst: Instance = Depends(get_owned_instance)):
    fs.rename(_root(inst), body.src, body.dst)
    return {"detail": "已重命名"}


@router.delete("/instances/{instance_id}/files")
def delete_path(instance_id: int, path: str, inst: Instance = Depends(get_owned_instance)):
    fs.delete(_root(inst), path)
    return {"detail": "已删除"}


@router.get("/instances/{instance_id}/files/download")
def download(instance_id: int, path: str, inst: Instance = Depends(get_owned_instance)):
    root = _root(inst)
    target = fs.resolve_path(root, path)
    if not os.path.isfile(target):
        raise HTTPException(status_code=400, detail="仅支持下载文件")
    return FileResponse(target, filename=os.path.basename(target))
