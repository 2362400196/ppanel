"""实例编排与转发：
写操作在主控留档（uuid/user/节点映射），执行转发到被控 /agent/instances/{agent_iid}/*。
文件类接口原样透传（上传下载经主控中转）。
"""
import asyncio
import re
import time
import uuid as uuidlib
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import tasks
from app.agent_client import (_error_detail, agent_json, agent_request,
                              owned_instance, resolve_node)
from app.auth import Principal, get_principal
from app.config import default_start_cmd, settings
from app.database import get_db
from app.models import (Domain, Instance, Metric, Node, OpLog, User, utcnow)
from app.schemas import InstanceCreate, InstanceUpdate, InstallIn
from app.status_sync import sync_instances_status

router = APIRouter(tags=["instances"])


def _oplog(db: Session, action: str, **kw) -> None:
    db.add(OpLog(action=action, **kw))


def _out(inst: Instance, node: Node | None, extra: dict | None = None) -> dict:
    data = {
        "id": inst.uuid,           # 前端以 uuid 为主键
        "name": inst.name,
        "image": inst.image,
        "start_cmd": inst.start_cmd,
        "ext_port": inst.ext_port,
        "cpu_limit": inst.cpu_limit,
        "mem_limit": inst.mem_limit,
        "disk_quota": inst.disk_quota,
        "note": inst.note,
        "status": inst.status,
        "started_at": inst.started_at.isoformat() + "Z" if inst.started_at else None,
        "created_at": inst.created_at.isoformat() if inst.created_at else None,
        "expire_at": inst.expire_at.isoformat() if inst.expire_at else None,
        "traffic_gb": getattr(inst, "traffic_gb", None),
        "user_id": inst.user_id,
        "node_id": inst.node_id,
        "node_name": node.name if node else None,
    }
    if extra:
        data.update(extra)
    return data


def _node_of(db: Session, inst: Instance) -> Node | None:
    return db.get(Node, inst.node_id)


@router.get("/images")
def list_images(p: Principal = Depends(get_principal)):
    return settings.all_images


@router.get("/instances")
def my_instances(all: int = 0, page: int = 0, page_size: int = 20, keyword: str = "",
                 p: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    """实例列表。带 page 参数（管理端分页视图）返回 {items, total}；
    不带则返回全量数组（用户端列表量小，由前端 UITable 自动分页）。"""
    sync_instances_status(db)
    q = db.query(Instance)
    if not (p.is_admin and all == 1):
        q = q.filter(Instance.user_id == p.user_id)
    # 搜索下推：实例名 / 节点名 / 归属用户名
    k = (keyword or "").strip()
    if k:
        node_ids = [n.id for n in db.query(Node).filter(Node.name.ilike(f"%{k}%")).all()]
        user_ids = [u.id for u in db.query(User).filter(User.username.ilike(f"%{k}%")).all()]
        cond = [Instance.name.ilike(f"%{k}%")]
        if node_ids:
            cond.append(Instance.node_id.in_(node_ids))
        if user_ids:
            cond.append(Instance.user_id.in_(user_ids))
        q = q.filter(or_(*cond))
    q = q.order_by(Instance.created_at.desc())
    if page >= 1:
        total = q.count()
        items = q.offset((page - 1) * page_size).limit(page_size).all()
        nodes = {n.id: n for n in db.query(Node).all()}
        uids = list({i.user_id for i in items}) or [0]
        users_map = {u.id: u.username for u in db.query(User).filter(User.id.in_(uids)).all()}
        return {"items": [_out(i, nodes.get(i.node_id),
                               {"owner": users_map.get(i.user_id)}) for i in items],
                "total": total}
    nodes = {n.id: n for n in db.query(Node).all()}
    return [_out(i, nodes.get(i.node_id)) for i in q.all()]


@router.post("/instances")
async def create_instance(body: InstanceCreate, p: Principal = Depends(get_principal),
                          db: Session = Depends(get_db), request: Request = None):
    tid = (request.headers.get("x-task-id") if request else "") or ""
    if tid:
        tasks.start(tid, f"开通实例 {body.name}（{body.image}）")
    try:
        if tid:
            tasks.log(tid, "校验镜像与规格…")
        if body.image not in settings.all_images:
            raise HTTPException(status_code=400, detail="不支持的镜像版本")

        if p.via_api_key:
            if not body.user_id or not db.get(User, body.user_id):
                raise HTTPException(status_code=400, detail="user_id 无效")
            owner_id = body.user_id
        else:
            owner_id = p.user_id

        if tid:
            tasks.log(tid, "选取节点…")
        node = resolve_node(body.node_id, db)

        if tid:
            tasks.log(tid, f"请求节点「{node.name}」创建容器（镜像预拉 / 端口分配 / 建容器）…")
        # 交给被控创建（镜像预拉、端口分配、容器创建都在被控完成）
        agent_data = agent_json(node, "POST", "/agent/instances", json={
            "name": body.name, "image": body.image, "start_cmd": body.start_cmd,
            "cpu_limit": body.cpu_limit, "mem_limit": body.mem_limit,
            "disk_quota": body.disk_quota,
            "expire_at": body.expire_at.isoformat() if body.expire_at else None,
            "traffic_gb": body.traffic_gb,
        })
        if tid:
            tasks.log(tid, f"节点创建成功：端口 {agent_data.get('ext_port', 0)}，落库…")

        inst = Instance(
            uuid=str(uuidlib.uuid4()),
            user_id=owner_id,
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
            traffic_gb=body.traffic_gb,
        )
        db.add(inst)
        db.commit()
        _oplog(db, "create_instance", user_id=owner_id, instance_uuid=inst.uuid,
               detail=f"node={node.name} image={inst.image} port={inst.ext_port}")
        db.commit()
        if tid:
            tasks.finish(tid, True, f"✔ 实例已开通（{inst.uuid[:8]}…，端口 {inst.ext_port}）")
        return _out(inst, node)
    except Exception as e:  # noqa: BLE001
        if tid:
            tasks.finish(tid, False, f"✘ 开通失败：{e}")
        raise


@router.get("/instances/{instance_uuid}")
def instance_detail(instance_uuid: str, p: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    node = _node_of(db, inst)
    try:
        data = agent_json(node, "GET", f"/agent/instances/{inst.agent_iid}")
        inst.status = data.get("status", inst.status)
        db.commit()
        data.pop("id", None)
    except HTTPException:
        data = {}
    return _out(inst, node, data)


@router.patch("/instances/{instance_uuid}")
def update_instance(instance_uuid: str, body: InstanceUpdate,
                    p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    node = _node_of(db, inst)
    agent_json(node, "PATCH", f"/agent/instances/{inst.agent_iid}",
               json=body.model_dump(exclude_none=True))
    if body.name is not None:
        inst.name = body.name.strip()
    if body.note is not None:
        inst.note = body.note
    if body.start_cmd is not None:
        inst.start_cmd = body.start_cmd.strip()
    db.commit()
    _oplog(db, "update_instance", user_id=p.user_id, instance_uuid=inst.uuid,
           detail=f"start_cmd={inst.start_cmd}")
    db.commit()
    return _out(inst, node)


@router.delete("/instances/{instance_uuid}")
def delete_instance(instance_uuid: str, p: Principal = Depends(get_principal),
                    db: Session = Depends(get_db), request: Request = None):
    tid = (request.headers.get("x-task-id") if request else "") or ""
    if tid:
        tasks.start(tid, f"回收实例 {instance_uuid[:8]}…")
    try:
        inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
        node = _node_of(db, inst)
        detail = "被控容器与数据已一并回收"
        if tid:
            tasks.log(tid, f"通知节点「{node.name}」回收容器与数据…")
        try:
            agent_json(node, "DELETE", f"/agent/instances/{inst.agent_iid}?purge=1")
        except HTTPException as e:
            if e.status_code != 404:
                raise
            # 被控已无此实例记录（容器可能已被管理员清理 / 被控重装过），
            # 此时不能卡死用户：跳过被控回收，仅清理主控数据
            detail = "被控已无此实例记录（容器可能已被清理），仅清理了主控数据"
            if tid:
                tasks.log(tid, f"节点返回 404：{detail}")
        if tid:
            tasks.log(tid, "清理主控记录…")
        db.delete(inst)
        _oplog(db, "delete_instance", user_id=p.user_id, instance_uuid=inst.uuid,
               detail=detail)
        db.commit()
        if tid:
            tasks.finish(tid, True, f"✔ 实例已回收（{detail}）")
        return {"detail": "实例已回收"}
    except Exception as e:  # noqa: BLE001
        if tid:
            tasks.finish(tid, False, f"✘ 回收失败：{e}")
        raise


def _action(instance_uuid: str, action: str, p: Principal, db: Session) -> dict:
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    node = _node_of(db, inst)
    data = agent_json(node, "POST", f"/agent/instances/{inst.agent_iid}/{action}")
    if data.get("status"):
        inst.status = data["status"]
    _oplog(db, action, user_id=p.user_id, instance_uuid=inst.uuid)
    if action == "stop":  # 人为停止：供稳定性评分排除崩溃误判
        from app.stability import record_event
        record_event(db, inst, "stopped_planned", f"user={p.user_id}")
    db.commit()
    return data


@router.post("/instances/{instance_uuid}/start")
def start(instance_uuid: str, p: Principal = Depends(get_principal),
          db: Session = Depends(get_db)):
    return _action(instance_uuid, "start", p, db)


@router.post("/instances/{instance_uuid}/stop")
def stop(instance_uuid: str, p: Principal = Depends(get_principal),
         db: Session = Depends(get_db)):
    return _action(instance_uuid, "stop", p, db)


@router.post("/instances/{instance_uuid}/restart")
def restart(instance_uuid: str, p: Principal = Depends(get_principal),
            db: Session = Depends(get_db)):
    return _action(instance_uuid, "restart", p, db)


@router.post("/instances/{instance_uuid}/panel-token")
def panel_token(instance_uuid: str, reset: int = 0, p: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    """生成/重置独立面板令牌：用户凭令牌访问被控 /panel 操作单个容器。"""
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    node = _node_of(db, inst)
    data = agent_json(node, "POST",
                      f"/agent/instances/{inst.agent_iid}/panel-token?reset={reset}")
    base = node.base_url.rstrip("/").replace("localhost", "127.0.0.1")
    _oplog(db, "panel_token", user_id=p.user_id, instance_uuid=inst.uuid,
           detail="reset" if reset else "issue")
    db.commit()
    return {"panel_token": data["panel_token"], "panel_url": f"{base}/panel"}


@router.get("/instances/{instance_uuid}/stats")
def stats(instance_uuid: str, p: Principal = Depends(get_principal),
          db: Session = Depends(get_db)):
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    return agent_json(_node_of(db, inst), "GET", f"/agent/instances/{inst.agent_iid}/stats")


@router.post("/instances/{instance_uuid}/probe")
async def probe(instance_uuid: str, p: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    """测连通：TCP 探测实例对外端口（node_host:ext_port），2.5s 超时。"""
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    node = _node_of(db, inst)
    if not node or not inst.ext_port:
        raise HTTPException(status_code=400, detail="实例未分配外部端口")
    host = urlsplit(node.base_url).hostname or ""
    if host == "localhost":  # Windows 下 IPv6 优先会导致超时
        host = "127.0.0.1"
    t0 = time.perf_counter()
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, inst.ext_port),
                                      timeout=2.5)
        w.close()
        return {"ok": True, "ms": round((time.perf_counter() - t0) * 1000)}
    except (OSError, asyncio.TimeoutError):
        return {"ok": False}


@router.get("/instances/{instance_uuid}/logs")
def logs(instance_uuid: str, tail: int = 200, p: Principal = Depends(get_principal),
         db: Session = Depends(get_db)):
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    return agent_json(_node_of(db, inst), "GET",
                      f"/agent/instances/{inst.agent_iid}/logs?tail={tail}")


@router.post("/instances/{instance_uuid}/deps/install")
def install_deps(instance_uuid: str, body: InstallIn, p: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    return agent_json(_node_of(db, inst), "POST",
                      f"/agent/instances/{inst.agent_iid}/deps/install",
                      json={"file": body.file})


# ---------- 文件接口（原样转发，上传经主控中转） ----------

@router.get("/instances/{instance_uuid}/files")
def files_list(instance_uuid: str, path: str = "/",
               p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    return agent_json(_node_of(db, inst), "GET",
                      f"/agent/instances/{inst.agent_iid}/files?path={path}")


@router.get("/instances/{instance_uuid}/files/content")
def file_content(instance_uuid: str, path: str,
                 p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    return agent_json(_node_of(db, inst), "GET",
                      f"/agent/instances/{inst.agent_iid}/files/content?path={path}")


@router.put("/instances/{instance_uuid}/files/upload")
async def file_upload(instance_uuid: str, path: str = "/",
                      file: UploadFile = None, p: Principal = Depends(get_principal),
                      db: Session = Depends(get_db)):
    if file is None:
        raise HTTPException(status_code=400, detail="未收到文件")
    limit = settings.transfer_limit_mb * 1024 * 1024
    data = bytearray()
    while True:
        chunk = await file.read(1024 * 512)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(status_code=413,
                                detail=f"文件超过 {settings.transfer_limit_mb}MB 中转上限")
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    node = _node_of(db, inst)
    return agent_json(node, "PUT", f"/agent/instances/{inst.agent_iid}/files/upload",
                      params={"path": path},
                      files={"file": (file.filename, bytes(data), file.content_type or "application/octet-stream")})


@router.post("/instances/{instance_uuid}/files/save")
def file_save(instance_uuid: str, body: dict, p: Principal = Depends(get_principal),
              db: Session = Depends(get_db)):
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    return agent_json(_node_of(db, inst), "POST",
                      f"/agent/instances/{inst.agent_iid}/files/save", json=body)


@router.post("/instances/{instance_uuid}/files/mkdir")
def file_mkdir(instance_uuid: str, body: dict, p: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    return agent_json(_node_of(db, inst), "POST",
                      f"/agent/instances/{inst.agent_iid}/files/mkdir", json=body)


@router.post("/instances/{instance_uuid}/files/rename")
def file_rename(instance_uuid: str, body: dict, p: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    return agent_json(_node_of(db, inst), "POST",
                      f"/agent/instances/{inst.agent_iid}/files/rename", json=body)


@router.delete("/instances/{instance_uuid}/files")
def file_delete(instance_uuid: str, path: str,
                p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    return agent_json(_node_of(db, inst), "DELETE",
                      f"/agent/instances/{inst.agent_iid}/files?path={path}")


@router.get("/instances/{instance_uuid}/files/download")
def file_download(instance_uuid: str, path: str,
                  p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """下载经主控中转（内存缓冲，受 TRANSFER_LIMIT_MB 约束）。"""
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    node = _node_of(db, inst)
    resp = agent_request(node, "GET", f"/agent/instances/{inst.agent_iid}/files/download",
                         params={"path": path}, timeout=settings.agent_timeout * 4)
    if resp.status_code >= 400:
        code = 502 if resp.status_code == 401 else resp.status_code  # 被控401不透传，防前端误判登录失效
        raise HTTPException(status_code=code, detail="文件下载失败")
    headers = {}
    cd = resp.headers.get("content-disposition")
    if cd:
        headers["content-disposition"] = cd
    media = resp.headers.get("content-type", "application/octet-stream")
    return Response(content=resp.content, media_type=media, headers=headers)


# ---------- 用量历史（采样任务落库，此处查询） ----------

@router.get("/instances/{instance_uuid}/metrics")
def metrics_history(instance_uuid: str, hours: float = 6,
                    p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    hours = min(max(hours, 0.25), float(settings.metrics_retention_hours))
    since = utcnow() - timedelta(hours=hours)
    rows = (db.query(Metric)
            .filter(Metric.instance_uuid == inst.uuid, Metric.ts >= since)
            .order_by(Metric.ts.asc()).all())
    # 网络计数是累计值 → 换算成区间速率 KB/s
    points, prev = [], None
    for r in rows:
        rate_rx = rate_tx = 0.0
        if prev is not None:
            dt = (r.ts - prev.ts).total_seconds()
            if dt > 0:
                rate_rx = max((r.net_rx_mb - prev.net_rx_mb) * 1024.0 / dt, 0.0)
                rate_tx = max((r.net_tx_mb - prev.net_tx_mb) * 1024.0 / dt, 0.0)
        points.append({
            "ts": r.ts.isoformat(),
            "cpu_percent": r.cpu_percent,
            "mem_used_mb": r.mem_used_mb,
            "mem_limit_mb": r.mem_limit_mb,
            "net_rx_kb_s": round(rate_rx, 1),
            "net_tx_kb_s": round(rate_tx, 1),
        })
        prev = r
    return {"points": points, "interval": settings.metrics_interval_seconds}


# ---------- 备份 / 恢复（经主控中转字节流） ----------

def _buffer_upload(file: UploadFile) -> bytes:
    limit = settings.transfer_limit_mb * 1024 * 1024
    data = bytearray()
    while True:
        chunk = file.file.read(1024 * 512)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(status_code=413,
                                detail=f"文件超过 {settings.transfer_limit_mb}MB 中转上限")
    return bytes(data)


@router.get("/instances/{instance_uuid}/files/backup")
def backup_download(instance_uuid: str, p: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    node = _node_of(db, inst)
    resp = agent_request(node, "GET", f"/agent/instances/{inst.agent_iid}/files/backup",
                         timeout=settings.agent_timeout * 6)
    if resp.status_code >= 400:
        code = 502 if resp.status_code == 401 else resp.status_code  # 被控401不透传，防前端误判登录失效
        raise HTTPException(status_code=code, detail=_error_detail(resp))
    headers = {"content-disposition": resp.headers.get(
        "content-disposition", 'attachment; filename="instance-backup.tar.gz"')}
    return Response(content=resp.content, media_type="application/gzip", headers=headers)


@router.post("/instances/{instance_uuid}/files/restore")
async def backup_restore(instance_uuid: str, file: UploadFile = None,
                         p: Principal = Depends(get_principal),
                         db: Session = Depends(get_db)):
    if file is None:
        raise HTTPException(status_code=400, detail="未收到文件")
    data = _buffer_upload(file)
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    node = _node_of(db, inst)
    resp = agent_request(node, "POST", f"/agent/instances/{inst.agent_iid}/files/restore",
                         files={"file": (file.filename, data, "application/gzip")},
                         timeout=settings.agent_timeout * 6)
    if resp.status_code >= 400:
        code = 502 if resp.status_code == 401 else resp.status_code  # 被控401不透传，防前端误判登录失效
        raise HTTPException(status_code=code, detail=_error_detail(resp))
    try:
        return resp.json()
    except ValueError:
        return {"detail": "恢复完成"}


# ---------- 操作记录 ----------

@router.get("/instances/{instance_uuid}/ops")
def instance_ops(instance_uuid: str, limit: int = 20,
                 p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    rows = (db.query(OpLog).filter(OpLog.instance_uuid == inst.uuid)
            .order_by(OpLog.created_at.desc(), OpLog.id.desc())
            .limit(min(max(limit, 1), 100)).all())
    return [{"id": r.id, "action": r.action, "detail": r.detail,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows]


# ---------- 自定义域名绑定（Host 头区分，反代到实例对外端口） ----------

_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME_RE = re.compile(rf"^{_LABEL}(\.{_LABEL})+$")


@router.get("/instances/{instance_uuid}/domain")
def get_domain(instance_uuid: str, p: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    d = db.query(Domain).filter(Domain.instance_uuid == inst.uuid).first()
    return {"hostname": d.hostname if d else None}


@router.put("/instances/{instance_uuid}/domain")
def put_domain(instance_uuid: str, body: dict, p: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    inst = owned_instance(instance_uuid, p.user_id, p.is_admin, db)
    host = (body.get("hostname") or "").strip().lower().rstrip(".")
    existing = db.query(Domain).filter(Domain.instance_uuid == inst.uuid).first()

    if not host:  # 空值视为解绑
        if existing:
            db.delete(existing)
            _oplog(db, "unbind_domain", user_id=p.user_id, instance_uuid=inst.uuid,
                   detail=existing.hostname)
            db.commit()
        return {"hostname": None}

    if not _HOSTNAME_RE.match(host):
        raise HTTPException(status_code=400, detail="域名格式不合法（示例：myapp.localhost）")
    conflict = (db.query(Domain)
                .filter(Domain.hostname == host, Domain.instance_uuid != inst.uuid)
                .first())
    if conflict:
        raise HTTPException(status_code=409, detail="该域名已被其他实例绑定")

    if existing:
        existing.hostname = host
    else:
        db.add(Domain(instance_uuid=inst.uuid, hostname=host))
    _oplog(db, "bind_domain", user_id=p.user_id, instance_uuid=inst.uuid, detail=host)
    db.commit()
    return {"hostname": host}
