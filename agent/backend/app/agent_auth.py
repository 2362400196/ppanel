"""被控 Agent 鉴权：主控持有 NODE_TOKEN，通过 X-Node-Token 头（REST）或
?node_token=（WS）调用。Agent 无用户概念。"""
import secrets

from fastapi import Header, HTTPException, WebSocket

from app.config import settings


def require_node(x_node_token: str = Header(default="", alias="X-Node-Token")) -> None:
    if not settings.node_token or x_node_token != settings.node_token:
        raise HTTPException(status_code=401, detail="节点凭证无效")


def require_node_or_api(
    x_node_token: str = Header(default="", alias="X-Node-Token"),
    x_api_key: str = Header(default="", alias="X-API-Key"),
) -> None:
    """宿主机管理接口凭证：X-Node-Token（主控）或 X-API-Key（OPEN_API_KEY /
    PANEL_API_KEY 开放对接）任一匹配即放行。"""
    if settings.node_token and secrets.compare_digest(x_node_token, settings.node_token):
        return
    for key in (settings.open_api_key, settings.api_key):
        if key and secrets.compare_digest(x_api_key, key):
            return
    raise HTTPException(status_code=401, detail="凭证无效（需 X-Node-Token 或 X-API-Key）")


def ws_node_ok(ws: WebSocket) -> bool:
    return bool(settings.node_token) and ws.query_params.get("node_token") == settings.node_token
