"""被控（Agent）调用客户端：REST 用 httpx，统一带上 X-Node-Token。"""
import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Instance, Node


def _norm_base(url: str) -> str:
    # Windows 上 localhost 先试 IPv6(::1) 再回退，每请求多 ~2s；统一落 IPv4
    return url.rstrip("/").replace("localhost", "127.0.0.1")


def ws_base_url(node: Node) -> str:
    base = _norm_base(node.base_url)
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):]
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):]
    return base


def agent_url(node: Node, path: str) -> str:
    return f"{_norm_base(node.base_url)}{path}"


def agent_request(node: Node, method: str, path: str, *, params=None, json=None,
                  files=None, data=None, content=None, headers=None,
                  timeout: float | None = None) -> httpx.Response:
    h = {"X-Node-Token": node.token}
    if headers:
        h.update(headers)
    try:
        return httpx.request(
            method, agent_url(node, path),
            params=params, json=json, files=files, data=data, content=content,
            headers=h, timeout=timeout or settings.agent_timeout,
        )
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="被控节点不可达")


def agent_json(node: Node, method: str, path: str, **kw) -> dict | list:
    resp = agent_request(node, method, path, **kw)
    if resp.status_code >= 400:
        detail = _error_detail(resp)
        raise HTTPException(status_code=resp.status_code, detail=detail)
    try:
        return resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="被控返回了无效数据")


def _error_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("detail"):
            return data["detail"]
    except ValueError:
        pass
    return f"被控返回 {resp.status_code}"


def agent_health(node: Node) -> dict | None:
    """心跳探测：不可达返回 None（不打异常，供列表展示）。"""
    try:
        resp = agent_request(node, "GET", "/agent/health",
                             timeout=settings.agent_health_timeout)
        if resp.status_code == 200:
            return resp.json()
    except (httpx.HTTPError, HTTPException):
        pass
    return None


def resolve_node(node_id: int | None, db: Session, *, enabled_only: bool = True) -> Node:
    """选择节点：显式 node_id 优先，否则取第一个可用节点。"""
    if node_id:
        node = db.get(Node, node_id)
        if not node:
            raise HTTPException(status_code=404, detail="节点不存在")
        if enabled_only and not node.enabled:
            raise HTTPException(status_code=409, detail="节点已停用")
        return node
    node = db.query(Node).filter(Node.enabled == True).order_by(Node.id).first()  # noqa: E712
    if not node:
        raise HTTPException(status_code=503, detail="没有可用节点，请先在节点管理中接入被控")
    return node


def owned_instance(instance_uuid: str, user_id: int | None, is_admin: bool,
                   db: Session) -> Instance:
    """uuid 归属校验：非归属一律 404，避免枚举。"""
    inst = db.get(Instance, instance_uuid)
    if not inst:
        raise HTTPException(status_code=404, detail="实例不存在")
    if not is_admin and inst.user_id != user_id:
        raise HTTPException(status_code=404, detail="实例不存在")
    node = db.get(Node, inst.node_id)
    if not node or (enabled_only_fail(node)):
        raise HTTPException(status_code=404, detail="实例所在节点不可用")
    return inst


def enabled_only_fail(node: Node) -> bool:
    return not node.enabled
