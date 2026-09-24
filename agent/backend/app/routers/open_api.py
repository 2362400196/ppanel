"""开放开通面 /open/*：任何商城/开通系统凭 X-API-Key 对接（OPEN_API_KEY，空=整组禁用）。

协议（与主控 /api/open/* 同构，供第三方生态直连被控）：
    POST   /open/provision                    开通 → panel_url + panel_token
    GET    /open/instances/{iid}              查询（状态/到期/对账单号）
    POST   /open/instances/{iid}/renew        续期 {days}
    DELETE /open/instances/{iid}?purge=1      回收（purge=1 连目录一起删）
    POST   /open/instances/{iid}/token-reset  重置面板令牌
"""
import os
import secrets
import shutil
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import AgentOpLog, Instance
from app.routers.agent import _rollback
from app.services import instance_service as svc
from app.services import mysql_service as mysql_svc

router = APIRouter()


def _auth(x_api_key: str = Header(default="")) -> None:
    if not settings.open_api_key:
        raise HTTPException(status_code=403, detail="开通接口未启用（未配置 OPEN_API_KEY）")
    if not secrets.compare_digest(x_api_key, settings.open_api_key):
        raise HTTPException(status_code=401, detail="API Key 无效")


def _inst(iid: int, db: Session) -> Instance:
    inst = db.get(Instance, iid)
    if not inst:
        raise HTTPException(status_code=404, detail="实例不存在")
    return inst


def _log(db: Session, iid: int, action: str, detail: str) -> None:
    db.add(AgentOpLog(instance_id=iid, action=action, detail=detail))


def _panel_url(request: Request) -> str:
    if settings.panel_public_url:
        return settings.panel_public_url.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", "http")
    host = request.headers.get("host") or f"127.0.0.1:{settings.agent_port}"
    return f"{proto}://{host}/panel"


def _expire_at(days: int):
    from app.models import utcnow
    return utcnow() + timedelta(days=days)


def _issued(inst: Instance, request: Request) -> dict:
    return {
        "iid": inst.id,
        "name": inst.name,
        "panel_url": _panel_url(request),
        "panel_token": inst.panel_token,
        "ext_port": inst.ext_port,
        "expire_at": inst.expire_at.isoformat() if inst.expire_at else None,
        "owner_ref": inst.owner_ref,
    }


class ProvisionIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    image: str = Field(min_length=1, max_length=128)
    days: int = Field(default=30, ge=1, le=3650)
    cpu: float = Field(default=1.0, gt=0, le=8)
    mem: int = Field(default=512, ge=64, le=16384)          # MB
    disk_quota: int = Field(default=2048, ge=128, le=102400)
    start_cmd: str = Field(default="", max_length=512)  # 空则按镜像取默认（python main.py / php -S …）
    owner_ref: str = Field(default="", max_length=128)


@router.post("/open/provision", dependencies=[Depends(_auth)])
def open_provision(body: ProvisionIn, request: Request, db: Session = Depends(get_db)):
    if body.image not in svc.allowed_images():
        raise HTTPException(status_code=400, detail="不支持的镜像版本")

    client = svc.get_docker()
    svc.ensure_image(client, body.image)
    port = svc.alloc_port(db)
    inst = Instance(
        user_id=1,  # Agent 本地库占位账号
        name=body.name.strip(),
        image=body.image,
        start_cmd=body.start_cmd.strip() or svc.default_start_cmd(body.image, body.mem),
        ext_port=port,
        cpu_limit=body.cpu,
        mem_limit=body.mem,
        disk_quota=body.disk_quota,
        status="creating",
        expire_at=_expire_at(body.days),
        owner_ref=body.owner_ref.strip(),
        panel_token=secrets.token_urlsafe(24),
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)

    inst.host_dir = svc.host_dir_for(inst.id)
    try:
        os.makedirs(inst.host_dir, exist_ok=True)
        svc.ensure_entry_file(inst.host_dir, body.image)
        inst.container_id = svc.create_container(inst)
        inst.status = "created"
        _log(db, inst.id, "open.provision",
             f"商城开通 days={body.days} owner_ref={inst.owner_ref}")
        db.commit()
    except HTTPException:
        db.rollback()
        _rollback(db, inst)
        raise
    except Exception as e:  # noqa: BLE001
        db.rollback()
        _rollback(db, inst)
        raise HTTPException(status_code=502, detail=f"创建实例失败：{e}")
    return _issued(inst, request)


@router.get("/open/instances/{iid}", dependencies=[Depends(_auth)])
def open_query(iid: int, request: Request, db: Session = Depends(get_db)):
    inst = _inst(iid, db)
    svc.sync_status(inst)
    db.commit()
    data = _issued(inst, request)
    data.update({
        "status": inst.status,
        "image": inst.image,
        "cpu_limit": inst.cpu_limit,
        "mem_limit": inst.mem_limit,
        "stats": svc.container_stats(inst),
    })
    return data


class RenewIn(BaseModel):
    days: int = Field(ge=1, le=3650)


@router.post("/open/instances/{iid}/renew", dependencies=[Depends(_auth)])
def open_renew(iid: int, body: RenewIn, db: Session = Depends(get_db)):
    from app.models import utcnow
    inst = _inst(iid, db)
    base = max(inst.expire_at, utcnow()) if inst.expire_at else utcnow()
    inst.expire_at = base + timedelta(days=body.days)
    _log(db, iid, "open.renew", f"续期 {body.days} 天 → {inst.expire_at.isoformat()}")
    db.commit()
    return {"iid": iid, "expire_at": inst.expire_at.isoformat()}


@router.delete("/open/instances/{iid}", dependencies=[Depends(_auth)])
def open_reclaim(iid: int, purge: int = 0, db: Session = Depends(get_db)):
    import docker as dockerlib

    inst = _inst(iid, db)
    try:
        client = svc.get_docker()
        client.containers.get(svc.container_name(inst.id)).remove(force=True)
    except dockerlib.errors.NotFound:
        pass  # 容器本就不存在，视为回收成功
    except dockerlib.errors.APIError as e:
        # 删除失败不能静默吞掉：残留容器会让下次同 ID 开通撞名
        raise HTTPException(status_code=502, detail=f"删除容器失败，请重试：{e}")
    if purge:
        shutil.rmtree(inst.host_dir, ignore_errors=True)
    mysql_svc.drop_instance_db(db, inst)  # 联动回收实例数据库（尽力而为，不阻塞回收）
    _log(db, iid, "open.reclaim", f"商城回收 purge={bool(purge)}")
    db.delete(inst)
    db.commit()
    return {"ok": True, "iid": iid, "purged": bool(purge)}


@router.post("/open/instances/{iid}/token-reset", dependencies=[Depends(_auth)])
def open_token_reset(iid: int, request: Request, db: Session = Depends(get_db)):
    inst = _inst(iid, db)
    inst.panel_token = secrets.token_urlsafe(24)
    _log(db, iid, "open.token_reset", "商城重置面板令牌")
    db.commit()
    return _issued(inst, request)
