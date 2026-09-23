import os
import shutil

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import Principal, get_principal
from app.config import settings
from app.database import get_db
from app.models import Instance, User
from app.schemas import InstanceCreate, InstanceOut, InstanceUpdate, InstallIn
from app.services import instance_service as svc
from app.services import oplog

router = APIRouter(tags=["instances"])


def get_owned_instance(instance_id: int,
                       p: Principal = Depends(get_principal),
                       db: Session = Depends(get_db)) -> Instance:
    """安全红线 5：user 只能碰自己的实例（非归属一律 404，避免枚举）。"""
    inst = db.get(Instance, instance_id)
    if not inst:
        raise HTTPException(status_code=404, detail="实例不存在")
    if not p.is_admin and inst.user_id != p.user_id:
        raise HTTPException(status_code=404, detail="实例不存在")
    return inst


@router.get("/images")
def list_images(p: Principal = Depends(get_principal)):
    """可用 Python 版本（用户创建实例表单使用）。"""
    return settings.python_images


@router.get("/instances")
def my_instances(all: int = 0, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    q = db.query(Instance)
    if not (p.is_admin and all == 1):
        q = q.filter(Instance.user_id == p.user_id)
    instances = q.order_by(Instance.id.desc()).all()
    for inst in instances:
        svc.sync_status(inst)
    db.commit()
    return [InstanceOut.model_validate(i).model_dump() for i in instances]


@router.post("/instances")
def create_instance(body: InstanceCreate, p: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    if body.image not in settings.python_images:
        raise HTTPException(status_code=400, detail="不支持的镜像版本")

    if p.via_api_key:
        # 预留：商城通过 X-API-Key 为指定用户开通
        if not body.user_id or not db.get(User, body.user_id):
            raise HTTPException(status_code=400, detail="user_id 无效")
        owner_id = body.user_id
    else:
        owner_id = p.user_id

    client = svc.get_docker()  # Docker 不可用直接 503
    svc.ensure_image(client, body.image)  # 阻塞拉镜像，首次可能较慢

    port = svc.alloc_port(db)
    inst = Instance(
        user_id=owner_id,
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
        inst.container_id = svc.create_container(inst)
        inst.status = "created"
        db.commit()
    except HTTPException:
        _rollback(db, inst)
        raise
    except Exception as e:  # noqa: BLE001
        _rollback(db, inst)
        raise HTTPException(status_code=502, detail=f"创建实例失败：{e}")

    oplog.log_op(db, "create_instance", user_id=owner_id, instance_id=inst.id,
                 detail=f"{inst.image} port={inst.ext_port}")
    return InstanceOut.model_validate(inst).model_dump()


def _rollback(db: Session, inst: Instance) -> None:
    db.delete(inst)
    db.commit()
    if inst.host_dir:
        shutil.rmtree(inst.host_dir, ignore_errors=True)


@router.get("/instances/{instance_id}")
def instance_detail(instance_id: int, inst: Instance = Depends(get_owned_instance),
                    db: Session = Depends(get_db)):
    svc.sync_status(inst)
    db.commit()
    data = InstanceOut.model_validate(inst).model_dump()
    data["host_dir"] = inst.host_dir
    return data


@router.delete("/instances/{instance_id}")
def delete_instance(instance_id: int, inst: Instance = Depends(get_owned_instance),
                    p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    host_dir = inst.host_dir
    svc.remove_container(inst)
    db.delete(inst)
    db.commit()
    oplog.log_op(db, "delete_instance", user_id=p.user_id, instance_id=instance_id,
                 detail=f"容器已删除，宿主机目录保留：{host_dir}")
    return {"detail": "实例已删除，宿主机目录已保留", "host_dir": host_dir}


@router.post("/instances/{instance_id}/start")
def start(instance_id: int, inst: Instance = Depends(get_owned_instance),
          p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    svc.start_instance(inst)
    db.commit()
    svc.diagnose_startup(inst)  # 秒退直接带回原因（缺文件/报错）
    oplog.log_op(db, "start", user_id=p.user_id, instance_id=inst.id)
    return {"status": "running"}


@router.post("/instances/{instance_id}/stop")
def stop(instance_id: int, inst: Instance = Depends(get_owned_instance),
         p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    svc.stop_instance(inst)
    db.commit()
    oplog.log_op(db, "stop", user_id=p.user_id, instance_id=inst.id)
    return {"status": inst.status}


@router.post("/instances/{instance_id}/restart")
def restart(instance_id: int, inst: Instance = Depends(get_owned_instance),
            p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    svc.restart_instance(inst)
    db.commit()
    oplog.log_op(db, "restart", user_id=p.user_id, instance_id=inst.id)
    return {"status": inst.status}


@router.patch("/instances/{instance_id}")
def update_instance(instance_id: int, body: InstanceUpdate,
                    inst: Instance = Depends(get_owned_instance),
                    p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    old_cmd = inst.start_cmd
    if body.name is not None:
        inst.name = body.name.strip()
    if body.note is not None:
        inst.note = body.note
    if body.start_cmd is not None and body.start_cmd.strip() != old_cmd:
        inst.start_cmd = body.start_cmd.strip()
        svc.recreate_container(inst)  # 命令烧进容器，需要重建
    db.commit()
    oplog.log_op(db, "update_instance", user_id=p.user_id, instance_id=inst.id,
                 detail=f"start_cmd={inst.start_cmd}")
    return InstanceOut.model_validate(inst).model_dump()


@router.get("/instances/{instance_id}/stats")
def stats(inst: Instance = Depends(get_owned_instance)):
    return svc.container_stats(inst)


@router.get("/instances/{instance_id}/logs")
def logs(instance_id: int, tail: int = 200, inst: Instance = Depends(get_owned_instance)):
    return {"logs": svc.container_logs(inst, tail)}


@router.post("/instances/{instance_id}/deps/install")
def install_deps(instance_id: int, body: InstallIn, inst: Instance = Depends(get_owned_instance)):
    file = body.file.strip()
    if "/" in file or "\\" in file or ".." in file or not file:
        raise HTTPException(status_code=400, detail="非法文件名")
    job = svc.start_install(inst, file)
    return {"job_id": job.id}
