"""宿主机运维转发：/api/host/*?node=<id> → 被控 /agent/host/*（仅 admin）；
安全防护：/api/security/*?node=<id> → 被控 /agent/security/*。
带 X-Task-Id 头的请求会被包装成任务：主控侧记录转发过程，被控侧记录操作步骤。"""
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

router = APIRouter(tags=["host"], dependencies=[Depends(require_admin)])

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


def _resp_detail(code: int, content: bytes) -> str:
    """从上游 JSON 响应里提取 detail 摘要，用于任务日志。"""
    try:
        d = json.loads(content).get("detail")
        return str(d)[:200] if d else ""
    except Exception:  # noqa: BLE001
        return ""


async def _forward(n: Node, request: Request, upstream_path: str) -> Response:
    tid = request.headers.get("x-task-id") or ""
    t0 = time.time()
    if tid:
        tasks.start(tid, f"节点 #{n.id} {n.name or ''}：{upstream_path.rsplit('/', 1)[-1] or upstream_path}")
        tasks.log(tid, f"转发请求到节点（{request.method} {upstream_path}）…")
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "node"]
    body = await request.body()
    headers = {
        "X-Node-Token": n.token,
        "content-type": request.headers.get("content-type", "application/json"),
    }
    if tid:
        headers["X-Task-Id"] = tid  # 透传：被控 handler 据此记录操作步骤日志
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.request(request.method, agent_url(n, upstream_path),
                                     params=params, content=body, headers=headers)
    except httpx.HTTPError as e:
        if tid:
            tasks.finish(tid, False, f"✘ 节点不可达：{e.__class__.__name__}")
        raise HTTPException(status_code=502, detail="被控节点不可达")
    # 被控 401（node_token 不匹配）不能透传：前端 axios 会把任何 401 当成
    # 主控登录失效而清 token 跳登录页。上游认证失败按 502 报。
    code = 502 if r.status_code == 401 else r.status_code
    if tid:
        cost = time.time() - t0
        detail = _resp_detail(code, r.content)
        if 200 <= code < 300:
            if detail:
                tasks.log(tid, f"节点返回 {code}（{cost:.1f}s）：{detail}")
            else:
                tasks.log(tid, f"节点返回 {code}（{cost:.1f}s）")
            tasks.finish(tid, True)
        else:
            tasks.finish(tid, False, f"✘ 节点返回 {code}（{cost:.1f}s）{('：' + detail) if detail else ''}")
    return Response(content=r.content, status_code=code,
                    media_type=r.headers.get("content-type", "application/json"))


@router.api_route("/host/{path:path}", methods=list(FORWARD_METHODS))
async def host_proxy(path: str, request: Request,
                     node: str = "", db: Session = Depends(get_db)):
    n = _node_or_404(node, db)
    return await _forward(n, request, f"/agent/host/{path}")


@router.api_route("/security/{path:path}", methods=list(FORWARD_METHODS))
async def security_proxy(path: str, request: Request,
                         node: str = "", db: Session = Depends(get_db)):
    """安全防护（fail2ban / 防火墙 open 复用接口）转发。"""
    n = _node_or_404(node, db)
    return await _forward(n, request, f"/agent/security/{path}")
