"""被控 Agent REST API（前缀 /agent，节点级鉴权）：
health 心跳、实例环境生命周期、实例文件、Docker 运维（复用 docker_admin handlers）。
"""
import os
import shutil
import time

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.config import settings
from app.database import get_db
from app.models import Instance
from app.routers import docker_admin
from app.schemas import (CopyIn, InstanceCreate, InstanceOut, InstanceUpdate,
                         MkdirIn, RenameIn, SaveIn, UnzipIn, UploadExistsIn,
                         ZipIn)
from app.services import file_service as fs
from app.services import instance_service as svc

router = APIRouter()

# 新实例的默认入口文件：常驻 HTTP 服务，保证“创建→启动→访问端口”开箱即通
_DEFAULT_MAIN = '''from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = "PPanel instance is running. Edit /app/main.py and restart.\\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):
        pass


print("PPanel instance starting on 0.0.0.0:8000 ...")
HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
'''


def _inst(instance_id: int, db: Session) -> Instance:
    inst = db.get(Instance, instance_id)
    if not inst:
        raise HTTPException(status_code=404, detail="实例不存在")
    return inst


# ---------- 心跳 ----------

@router.get("/health")
def agent_health(db: Session = Depends(get_db)):
    client = svc.try_get_docker()
    info = {"docker_ok": client is not None}
    if client is not None:
        try:
            d = client.info()
            v = client.version()
            info.update({
                "server_version": v.get("Version"),
                "os": d.get("OperatingSystem"),
                "cpus": d.get("NCPU"),
                "mem_total_gb": round((d.get("MemTotal") or 0) / 1024 ** 3, 1),
                "containers_running": d.get("ContainersRunning", 0),
                "images": d.get("Images", 0),
            })
        except Exception:  # noqa: BLE001
            info["docker_ok"] = False
    info["instances"] = db.query(Instance.id).count()
    info["port_start"] = settings.port_start
    info["port_end"] = settings.port_end
    info["python_images"] = settings.python_images
    return info


# ---------- 实例环境 ----------

@router.post("/instances")
def agent_create(body: InstanceCreate, db: Session = Depends(get_db)):
    if body.image not in settings.python_images:
        raise HTTPException(status_code=400, detail="不支持的镜像版本")

    client = svc.get_docker()
    svc.ensure_image(client, body.image)

    port = svc.alloc_port(db)
    # user_id=1：Agent 本地库的占位账号（Agent 无用户概念）
    inst = Instance(
        user_id=1,
        name=body.name.strip(),
        image=body.image,
        start_cmd=body.start_cmd.strip() or "python main.py",
        ext_port=port,
        cpu_limit=body.cpu_limit,
        mem_limit=body.mem_limit,
        disk_quota=body.disk_quota,
        status="creating",
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)

    inst.host_dir = svc.host_dir_for(inst.id)
    try:
        os.makedirs(inst.host_dir, exist_ok=True)
        if not os.path.exists(os.path.join(inst.host_dir, "main.py")):
            fs.write_text(inst.host_dir, "/main.py", _DEFAULT_MAIN)
        inst.container_id = svc.create_container(inst)
        inst.status = "created"
        db.commit()
    except HTTPException:
        _rollback(db, inst)
        raise
    except Exception as e:  # noqa: BLE001
        _rollback(db, inst)
        raise HTTPException(status_code=502, detail=f"创建实例失败：{e}")
    return InstanceOut.model_validate(inst).model_dump()


def _rollback(db: Session, inst: Instance) -> None:
    db.delete(inst)
    db.commit()
    if inst.host_dir:
        shutil.rmtree(inst.host_dir, ignore_errors=True)


@router.get("/instances")
def agent_list(db: Session = Depends(get_db)):
    instances = db.query(Instance).order_by(Instance.id.desc()).all()
    for inst in instances:
        svc.sync_status(inst)
    db.commit()
    return [InstanceOut.model_validate(i).model_dump() for i in instances]


@router.get("/instances/{instance_id}")
def agent_detail(instance_id: int, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    svc.sync_status(inst)
    db.commit()
    data = InstanceOut.model_validate(inst).model_dump()
    data["host_dir"] = inst.host_dir
    return data


@router.delete("/instances/{instance_id}")
def agent_delete(instance_id: int, purge: int = 0, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    host_dir = inst.host_dir
    svc.remove_container(inst)
    db.delete(inst)
    db.commit()
    if purge and host_dir:
        shutil.rmtree(host_dir, ignore_errors=True)
    return {"detail": "实例已回收", "host_dir": host_dir,
            "purged": bool(purge and host_dir)}


@router.post("/instances/{instance_id}/start")
def agent_start(instance_id: int, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    svc.start_instance(inst)
    db.commit()
    svc.diagnose_startup(inst)  # 秒退直接带回原因（缺文件/报错）
    return {"status": "running"}


@router.post("/instances/{instance_id}/stop")
def agent_stop(instance_id: int, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    svc.stop_instance(inst)
    db.commit()
    return {"status": inst.status}


@router.post("/instances/{instance_id}/restart")
def agent_restart(instance_id: int, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    svc.restart_instance(inst)
    db.commit()
    return {"status": inst.status}


@router.patch("/instances/{instance_id}")
def agent_update(instance_id: int, body: InstanceUpdate, db: Session = Depends(get_db)):
    """主控转发来的实例编辑：start_cmd 变更需重建容器。"""
    inst = _inst(instance_id, db)
    if body.name is not None:
        inst.name = body.name.strip()
    if body.note is not None:
        inst.note = body.note
    if body.start_cmd is not None and body.start_cmd.strip() != inst.start_cmd:
        inst.start_cmd = body.start_cmd.strip()
        svc.recreate_container(inst)  # 命令烧进容器，需要重建
    db.commit()
    return InstanceOut.model_validate(inst).model_dump()


@router.get("/instances/{instance_id}/stats")
def agent_stats(instance_id: int, db: Session = Depends(get_db)):
    return svc.container_stats(_inst(instance_id, db))


@router.get("/instances/{instance_id}/logs")
def agent_logs(instance_id: int, tail: int = 200, db: Session = Depends(get_db)):
    return {"logs": svc.container_logs(_inst(instance_id, db), tail)}


@router.post("/instances/{instance_id}/deps/install")
def agent_install(instance_id: int, file: str = "requirements.txt",
                  db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    if "/" in file or "\\" in file or ".." in file or not file:
        raise HTTPException(status_code=400, detail="非法文件名")
    job = svc.start_install(inst, file)
    return {"job_id": job.id}


@router.post("/instances/{instance_id}/panel-token")
def agent_panel_token(instance_id: int, reset: int = 0, db: Session = Depends(get_db)):
    """生成/重置独立面板令牌：主控或开通系统据此下发单容器面板入口。"""
    import secrets as _secrets

    inst = _inst(instance_id, db)
    if reset or not inst.panel_token:
        inst.panel_token = _secrets.token_urlsafe(24)
        db.commit()
    return {"panel_token": inst.panel_token}


@router.delete("/instances/{instance_id}/panel-token")
def agent_panel_revoke(instance_id: int, db: Session = Depends(get_db)):
    """吊销面板令牌：清空后所有已发面板凭证立即失效。"""
    inst = _inst(instance_id, db)
    inst.panel_token = None
    db.commit()
    return {"detail": "面板令牌已吊销"}


# ---------- 实例文件（Agent 本机路径直接操作） ----------

def _root(inst: Instance) -> str:
    fs.ensure_root(inst.host_dir)
    return inst.host_dir


@router.get("/instances/{instance_id}/files")
def agent_files(instance_id: int, path: str = "/", db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    entries = fs.list_dir(_root(inst), path)
    return {"path": path, "entries": entries}


@router.get("/instances/{instance_id}/files/content")
def agent_file_content(instance_id: int, path: str, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    data = fs.read_text(_root(inst), path)
    data["path"] = path
    return data


@router.put("/instances/{instance_id}/files/upload")
def agent_upload(instance_id: int, path: str = "/",
                 file: UploadFile = None, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    if file is None:
        raise HTTPException(status_code=400, detail="未收到文件")
    limit = settings.upload_limit_mb * 1024 * 1024
    return fs.save_upload(_root(inst), path, file.filename, file.file, limit)


@router.post("/instances/{instance_id}/files/save")
def agent_save(instance_id: int, body: SaveIn, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    fs.write_text(_root(inst), body.path, body.content)
    return {"detail": "已保存"}


@router.post("/instances/{instance_id}/files/mkdir")
def agent_mkdir(instance_id: int, body: MkdirIn, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    fs.mkdir(_root(inst), body.path)
    return {"detail": "已创建"}


@router.post("/instances/{instance_id}/files/rename")
def agent_rename(instance_id: int, body: RenameIn, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    fs.rename(_root(inst), body.src, body.dst)
    return {"detail": "已重命名"}


@router.delete("/instances/{instance_id}/files")
def agent_delete_path(instance_id: int, path: str, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    fs.delete(_root(inst), path)
    return {"detail": "已删除"}


@router.get("/instances/{instance_id}/files/download")
def agent_download(instance_id: int, path: str, db: Session = Depends(get_db)):
    from fastapi.responses import FileResponse
    inst = _inst(instance_id, db)
    root = _root(inst)
    target = fs.resolve_path(root, path)
    if not os.path.isfile(target):
        raise HTTPException(status_code=400, detail="仅支持下载文件")
    return FileResponse(target, filename=os.path.basename(target))


@router.get("/instances/{instance_id}/files/backup")
def agent_backup(instance_id: int, db: Session = Depends(get_db)):
    """整目录打包下载：响应发送完毕后后台清理临时包。"""
    from fastapi.responses import FileResponse
    inst = _inst(instance_id, db)
    tmp_path = fs.create_backup(_root(inst))
    fname = f"instance_{inst.id}_backup_{time.strftime('%Y%m%d_%H%M%S')}.tar.gz"
    return FileResponse(
        tmp_path, filename=fname, media_type="application/gzip",
        background=BackgroundTask(_cleanup, tmp_path))


def _cleanup(path: str) -> None:
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@router.post("/instances/{instance_id}/files/restore")
def agent_restore(instance_id: int, file: UploadFile = None,
                  db: Session = Depends(get_db)):
    """从 tar.gz 恢复实例目录：运行中禁止（避免文件边写边换）。"""
    inst = _inst(instance_id, db)
    if file is None:
        raise HTTPException(status_code=400, detail="未收到文件")
    svc.sync_status(inst)
    if inst.status == "running":
        raise HTTPException(status_code=409, detail="实例运行中，请先停止后再恢复")
    return fs.restore_backup(_root(inst), file.file, inst.disk_quota * 1024 * 1024)


@router.post("/instances/{instance_id}/files/copy")
def agent_copy(instance_id: int, body: CopyIn, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    fs.copy_path(_root(inst), body.src, body.dst)
    return {"detail": "已复制"}


@router.post("/instances/{instance_id}/files/zip")
def agent_zip(instance_id: int, body: ZipIn, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    fs.zip_entries(_root(inst), body.paths, body.name, body.z_type)
    return {"detail": "压缩完成"}


@router.post("/instances/{instance_id}/files/unzip")
def agent_unzip(instance_id: int, body: UnzipIn, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    dest = fs.unzip_entry(_root(inst), body.src)
    return {"detail": "解压完成", "dest": dest}


@router.get("/instances/{instance_id}/files/path-size")
def agent_path_size(instance_id: int, path: str, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    return {"size": fs.dir_size(_root(inst), path)}


@router.get("/instances/{instance_id}/files/preview")
def agent_preview(instance_id: int, path: str, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    return fs.image_preview(_root(inst), path)


@router.post("/instances/{instance_id}/files/upload-exists")
def agent_upload_exists(instance_id: int, body: UploadExistsIn,
                        db: Session = Depends(get_db)):
    """分片上传断点查询：返回续传起点。"""
    inst = _inst(instance_id, db)
    return fs.upload_exists(_root(inst), body.path, body.file_name, body.total_size)


@router.post("/instances/{instance_id}/files/upload-chunk")
async def agent_upload_chunk(instance_id: int, file: UploadFile = None,
                             path: str = Form("/"), file_name: str = Form(...),
                             total_size: int = Form(0), start: int = Form(0),
                             db: Session = Depends(get_db)):
    """分片上传：按 start 偏移追加写入（start=0 截断新建）。"""
    inst = _inst(instance_id, db)
    if file is None:
        raise HTTPException(status_code=400, detail="未收到分片数据")
    return fs.upload_chunk(_root(inst), path, file_name, total_size, start, file.file)


# ---------- Docker 运维（复用 docker_admin 的 handler，鉴权由 router 级 require_node 承担） ----------

router.get("/docker/info")(docker_admin.docker_info)
router.get("/docker/containers")(docker_admin.list_containers)
router.post("/docker/containers/{cid}/start")(docker_admin.container_start)
router.post("/docker/containers/{cid}/stop")(docker_admin.container_stop)
router.post("/docker/containers/{cid}/restart")(docker_admin.container_restart)
router.delete("/docker/containers/{cid}")(docker_admin.container_remove)
router.get("/docker/containers/{cid}/logs")(docker_admin.container_logs)
router.get("/docker/images")(docker_admin.list_images)
router.delete("/docker/images/{ref:path}")(docker_admin.remove_image)
router.post("/docker/images/prune")(docker_admin.prune_images)
router.post("/docker/images/pull")(docker_admin.pull_image)
router.get("/docker/settings")(docker_admin.get_settings)
router.put("/docker/settings")(docker_admin.put_settings)
