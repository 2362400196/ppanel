"""被控 Agent 鉴权：主控持有 NODE_TOKEN，通过 X-Node-Token 头（REST）或
?node_token=（WS）调用。Agent 无用户概念。"""
from fastapi import Header, HTTPException, WebSocket

from app.config import settings


def require_node(x_node_token: str = Header(default="", alias="X-Node-Token")) -> None:
    if not settings.node_token or x_node_token != settings.node_token:
        raise HTTPException(status_code=401, detail="节点凭证无效")


def ws_node_ok(ws: WebSocket) -> bool:
    return bool(settings.node_token) and ws.query_params.get("node_token") == settings.node_token
