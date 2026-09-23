"""Docker 运维转发：/api/docker/*?node=<id> → 被控 /agent/docker/*（仅 admin）。"""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.agent_client import agent_url
from app.auth import require_admin
from app.database import get_db
from app.models import Node

router = APIRouter(tags=["docker"], dependencies=[Depends(require_admin)])

FORWARD_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")


def _node_or_404(node: str, db: Session) -> Node:
    if not node:
        raise HTTPException(status_code=400, detail="缺少节点参数 node")
    if not node.isdigit():
        raise HTTPException(status_code=400, detail="节点参数无效")
    n = db.get(Node, int(node))
    if not n:
        raise HTTPException(status_code=404, detail="节点不存在")
    return n


@router.api_route("/docker/{path:path}", methods=list(FORWARD_METHODS))
async def docker_proxy(path: str, request: Request,
                       node: str = "", db: Session = Depends(get_db)):
    n = _node_or_404(node, db)
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "node"]
    body = await request.body()
    headers = {
        "X-Node-Token": n.token,
        "content-type": request.headers.get("content-type", "application/json"),
    }
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.request(request.method, agent_url(n, f"/agent/docker/{path}"),
                                     params=params, content=body, headers=headers)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="被控节点不可达")
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))
