"""WebSocket 泵转发：浏览器 → 主控 → 被控，双向透传直到任一侧断开。

前端路径与面板一致（/ws/*），被控路径带 /agent 前缀并以 ?node_token= 鉴权。
"""
import asyncio

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.agent_client import ws_base_url
from app.auth import ws_user
from app.database import SessionLocal
from app.models import Instance, Node

router = APIRouter(tags=["ws"])


async def _relay(client: WebSocket, upstream) -> None:
    async def c2u():
        try:
            while True:
                msg = await client.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
        except Exception:  # noqa: BLE001
            pass

    async def u2c():
        try:
            async for m in upstream:
                if isinstance(m, bytes):
                    await client.send_bytes(m)
                else:
                    await client.send_text(m)
        except Exception:  # noqa: BLE001
            pass

    t1 = asyncio.create_task(c2u())
    t2 = asyncio.create_task(u2c())
    done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    try:
        await upstream.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        await client.close()
    except Exception:  # noqa: BLE001
        pass


def _inst_or_none(db: Session, instance_uuid: str, user, is_admin: bool):
    inst = db.get(Instance, instance_uuid)
    if not inst or (not is_admin and inst.user_id != user.id):
        return None
    return inst


async def _bridge(ws: WebSocket, node: Node, path: str, params: dict) -> None:
    """连接被控 WS 并双向泵；被控不可达时给前端一条可读错误。"""
    from urllib.parse import urlencode

    url = f"{ws_base_url(node)}{path}?{urlencode({'node_token': node.token, **params})}"
    try:
        async with websockets.connect(url, max_size=None, open_timeout=10) as up:
            await ws.accept()
            await _relay(ws, up)
    except Exception:  # noqa: BLE001
        try:
            await ws.accept()
            await ws.send_text("[主控] 被控节点连接失败（不在线或 token 无效）")
            await ws.close(code=4502)
        except Exception:  # noqa: BLE001
            pass


@router.websocket("/ws/instances/{instance_uuid}/logs")
async def ws_logs(ws: WebSocket, instance_uuid: str):
    db: Session = SessionLocal()
    try:
        user = ws_user(ws, db)
        if user is None:
            await ws.close(code=4401)
            return
        inst = _inst_or_none(db, instance_uuid, user, user.role == "admin")
        if not inst:
            await ws.close(code=4404)
            return
        node = db.get(Node, inst.node_id)
        if not node:
            await ws.close(code=4404)
            return
        tail = ws.query_params.get("tail", "200")
        await _bridge(ws, node, f"/agent/ws/instances/{inst.agent_iid}/logs",
                      {"tail": tail})
    except WebSocketDisconnect:
        pass
    finally:
        db.close()


@router.websocket("/ws/instances/{instance_uuid}/exec")
async def ws_exec(ws: WebSocket, instance_uuid: str):
    db: Session = SessionLocal()
    try:
        user = ws_user(ws, db)
        if user is None:
            await ws.close(code=4401)
            return
        inst = _inst_or_none(db, instance_uuid, user, user.role == "admin")
        if not inst:
            await ws.close(code=4404)
            return
        node = db.get(Node, inst.node_id)
        if not node:
            await ws.close(code=4404)
            return
        job_id = ws.query_params.get("job_id", "")
        await _bridge(ws, node, f"/agent/ws/instances/{inst.agent_iid}/exec",
                      {"job_id": job_id})
    except WebSocketDisconnect:
        pass
    finally:
        db.close()


@router.websocket("/ws/docker/pull")
async def ws_docker_pull(ws: WebSocket):
    db: Session = SessionLocal()
    try:
        user = ws_user(ws, db)
        if user is None:
            await ws.close(code=4401)
            return
        if user.role != "admin":
            await ws.close(code=4403)
            return
        node_id = ws.query_params.get("node", "")
        node = db.get(Node, int(node_id)) if node_id.isdigit() else None
        if not node:
            await ws.close(code=4404)
            return
        image = ws.query_params.get("image", "")
        await _bridge(ws, node, "/agent/ws/docker/pull", {"image": image})
    except WebSocketDisconnect:
        pass
    finally:
        db.close()


@router.websocket("/ws/host/term")
async def ws_host_term(ws: WebSocket):
    db: Session = SessionLocal()
    try:
        user = ws_user(ws, db)
        if user is None:
            await ws.close(code=4401)
            return
        if user.role != "admin":
            await ws.close(code=4403)
            return
        node_id = ws.query_params.get("node", "")
        node = db.get(Node, int(node_id)) if node_id.isdigit() else None
        if not node:
            await ws.close(code=4404)
            return
        await _bridge(ws, node, "/agent/ws/docker/term", {})
    except WebSocketDisconnect:
        pass
    finally:
        db.close()
