"""Docker 运维转发：/api/docker/*?node=<id> → 被控 /agent/docker/*（仅 admin）。
带 X-Task-Id 头的请求会被包装成任务（同 host_proxy）。"""
import json
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app import tasks
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
    tid = request.headers.get("x-task-id") or ""
    t0 = time.time()
    if tid:
        tasks.start(tid, f"节点 #{n.id} {n.name or ''}：{path.rsplit('/', 1)[-1]}")
        tasks.log(tid, f"转发请求到节点（{request.method} /docker/{path}）…")
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "node"]
    body = await request.body()
    headers = {
        "X-Node-Token": n.token,
        "content-type": request.headers.get("content-type", "application/json"),
    }
    if tid:
        headers["X-Task-Id"] = tid
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.request(request.method, agent_url(n, f"/agent/docker/{path}"),
                                     params=params, content=body, headers=headers)
    except httpx.HTTPError:
        if tid:
            tasks.finish(tid, False, "✘ 节点不可达")
        raise HTTPException(status_code=502, detail="被控节点不可达")
    # 被控 401（node_token 不匹配）不能透传：前端 axios 会把任何 401 当成
    # 主控登录失效而清 token 跳登录页。上游认证失败按 502 报。
    code = 502 if r.status_code == 401 else r.status_code
    if tid:
        cost = time.time() - t0
        try:
            detail = str(json.loads(r.content).get("detail") or "")[:200]
        except Exception:  # noqa: BLE001
            detail = ""
        if 200 <= code < 300:
            tasks.log(tid, f"节点返回 {code}（{cost:.1f}s）" + (f"：{detail}" if detail else ""))
            tasks.finish(tid, True)
        else:
            tasks.finish(tid, False, f"✘ 节点返回 {code}（{cost:.1f}s）{('：' + detail) if detail else ''}")
    return Response(content=r.content, status_code=code,
                    media_type=r.headers.get("content-type", "application/json"))
