"""商城对接面 /api/open/*：全部走 X-API-Key（MASTER_API_KEY 未配置则禁用）。

开通 / 查询 / 续期 / 回收；为不存在的用户名自动建号（一次性返回初始密码）。
"""
import secrets
import uuid as uuidlib

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.agent_client import agent_json, resolve_node
from app.auth import hash_password
from app.config import default_start_cmd, settings
from app.database import get_db
from app.models import Instance, Node, OpLog, User
from app.schemas import OpenInstanceCreate, OpenRenew
from app.status_sync import sync_instances_status

router = APIRouter(tags=["open"], prefix="/open")


def require_api_key(x_api_key: str = Header(default="")):
    if not settings.api_key:
        raise HTTPException(status_code=403, detail="商城对接未启用（未配置 MASTER_API_KEY）")
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="X-API-Key 无效")


def _inst_out(inst: Instance, node: Node | None, username: str = "") -> dict:
    return {
        "uuid": inst.uuid,
        "username": username,
        "name": inst.name,
        "image": inst.image,
        "start_cmd": inst.start_cmd,
        "ext_port": inst.ext_port,
        "cpu_limit": inst.cpu_limit,
        "mem_limit": inst.mem_limit,
        "disk_quota": inst.disk_quota,
        "status": inst.status,
        "created_at": inst.created_at.isoformat() if inst.created_at else None,
        "expire_at": inst.expire_at.isoformat() if inst.expire_at else None,
        "node_name": node.name if node else None,
    }


@router.post("/instances", dependencies=[Depends(require_api_key)])
def open_create(body: OpenInstanceCreate, db: Session = Depends(get_db)):
    if body.image not in settings.all_images:
        raise HTTPException(status_code=400, detail="不支持的镜像版本")

    initial_password = None
    user = db.query(User).filter(User.username == body.username).first()
    if not user:
        initial_password = secrets.token_urlsafe(9)
        user = User(username=body.username.strip(),
                    password_hash=hash_password(initial_password), role="user")
        db.add(user)
        db.commit()
        db.refresh(user)

    node = resolve_node(body.node_id, db)
    try:
        agent_data = agent_json(node, "POST", "/agent/instances", json={
            "name": body.name, "image": body.image, "start_cmd": body.start_cmd,
            "cpu_limit": body.cpu_limit, "mem_limit": body.mem_limit,
            "disk_quota": body.disk_quota,
        })
    except HTTPException:
        db.delete(user)  # 自动建号但开通失败时回滚账号
        db.commit()
        raise

    inst = Instance(
        uuid=str(uuidlib.uuid4()),
        user_id=user.id,
        node_id=node.id,
        agent_iid=agent_data["id"],
        name=body.name.strip(),
        image=body.image,
        start_cmd=body.start_cmd.strip() or default_start_cmd(body.image, body.mem_limit),
        ext_port=agent_data.get("ext_port", 0),
        cpu_limit=body.cpu_limit,
        mem_limit=body.mem_limit,
        disk_quota=body.disk_quota,
        status=agent_data.get("status", "created"),
        expire_at=body.expire_at,
    )
    db.add(inst)
    db.commit()
    db.add(OpLog(user_id=user.id, instance_uuid=inst.uuid, action="open_create",
                 detail=f"商城开通 node={node.name} image={inst.image}"))
    db.commit()
    return {"instance": _inst_out(inst, node, user.username),
            "initial_password": initial_password}


@router.get("/instances", dependencies=[Depends(require_api_key)])
def open_list(username: str = "", db: Session = Depends(get_db)):
    sync_instances_status(db)
    q = db.query(Instance)
    if username:
        user = db.query(User).filter(User.username == username).first()
        if not user:
            return []
        q = q.filter(Instance.user_id == user.id)
    rows = q.order_by(Instance.created_at.desc()).all()
    nodes = {n.id: n for n in db.query(Node).all()}
    users = {u.id: u.username for u in db.query(User).all()}
    return [_inst_out(i, nodes.get(i.node_id), users.get(i.user_id, "")) for i in rows]


@router.get("/instances/{instance_uuid}", dependencies=[Depends(require_api_key)])
def open_detail(instance_uuid: str, db: Session = Depends(get_db)):
    inst = db.get(Instance, instance_uuid)
    if not inst:
        raise HTTPException(status_code=404, detail="实例不存在")
    user = db.get(User, inst.user_id)
    node = db.get(Node, inst.node_id)
    sync_instances_status(db)
    db.refresh(inst)
    return _inst_out(inst, node, user.username if user else "")


@router.post("/instances/{instance_uuid}/renew", dependencies=[Depends(require_api_key)])
def open_renew(instance_uuid: str, body: OpenRenew, db: Session = Depends(get_db)):
    inst = db.get(Instance, instance_uuid)
    if not inst:
        raise HTTPException(status_code=404, detail="实例不存在")
    inst.expire_at = body.expire_at
    db.commit()
    db.add(OpLog(instance_uuid=inst.uuid, action="open_renew",
                 detail=f"续期至 {body.expire_at.isoformat()}"))
    db.commit()
    return {"detail": "已续期", "expire_at": inst.expire_at.isoformat()}


@router.delete("/instances/{instance_uuid}", dependencies=[Depends(require_api_key)])
def open_delete(instance_uuid: str, db: Session = Depends(get_db)):
    inst = db.get(Instance, instance_uuid)
    if not inst:
        raise HTTPException(status_code=404, detail="实例不存在")
    node = db.get(Node, inst.node_id)
    agent_json(node, "DELETE", f"/agent/instances/{inst.agent_iid}?purge=1")
    db.delete(inst)
    db.add(OpLog(instance_uuid=instance_uuid, action="open_delete", detail="商城回收"))
    db.commit()
    return {"detail": "实例已回收"}
