"""宿主机运维转发：/api/host/*?node=<id> → 被控 /agent/host/*（仅 admin）；
安全防护：/api/security/*?node=<id> → 被控 /agent/security/*。
带 X-Task-Id 头的请求会被包装成任务：主控侧记录转发过程，被控侧记录操作步骤。"""
import json
import re
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app import tasks
from app.agent_client import agent_url
from app.auth import require_admin
from app.database import get_db
from app.models import Instance, InstanceArchive, Node, User

router = APIRouter(tags=["host"], dependencies=[Depends(require_admin)])

FORWARD_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")

# 备份文件名 → 实例 ID：ppanel-43_app_* / ppanel_2_predrop_* / inst52_ppanel-52_*
# agentdb_*.db 是节点自身数据库，不带实例 ID。
_BK_IID = re.compile(r"^(?:ppanel[-_]|inst)(\d+)(?:_|$)")


def _backup_category(name: str) -> tuple[str, str]:
    """业务分类与用途说明。原 kind 字段保持不变（节点面板恢复流程依赖它），
    这里只用于备份中心的页签分组与展示。"""
    n = name.lower()
    if n.startswith("agentdb_") and n.endswith(".db"):
        return "database", "节点自身数据库备份"
    if n.endswith(".image.tar"):
        return "image", "镜像快照导出包"
    if "predestroy" in n:
        return "snapshot", "删除实例前自动存档（含全部数据）"
    if "predrop" in n or "predb_" in n:
        return "database", "删除数据库前自动存档"
    if n.endswith(".sql.gz"):
        return "database", "MySQL 数据库导出"
    if n.endswith(".tar.gz"):
        return "dir", "容器内目录打包"
    if n.endswith(".tar"):
        return "container", "整容器导出"
    return "other", "其他备份产物"


def _enrich_backups(content: bytes, node: Node, db: Session) -> bytes:
    """给节点备份文件补上「属于哪个用户 / 哪个实例 / 哪个节点」。

    被控只知道文件名里的 agent_iid，用户与实例自定义名是主控独有的数据。
    跨机部署下 agent_iid 在不同节点会重复，一律带 node_id 过滤；
    已删除实例查回收归档表，仍查不到则标注为「实例 #N（已删除）」。
    """
    try:
        data = json.loads(content)
    except Exception:  # noqa: BLE001
        return content
    if not isinstance(data, dict) or not isinstance(data.get("backups"), list):
        return content
    items = data["backups"]

    iids = set()
    for b in items:
        if isinstance(b, dict):
            m = _BK_IID.match(str(b.get("file") or ""))
            if m:
                iids.add(int(m.group(1)))

    live: dict[int, tuple[Instance, str]] = {}
    if iids:
        q = (db.query(Instance, User.username)
               .outerjoin(User, User.id == Instance.user_id)
               .filter(Instance.node_id == node.id,
                       Instance.agent_iid.in_(iids)))
        for inst, uname in q.all():
            live.setdefault(inst.agent_iid,
                            (inst, uname or f"user#{inst.user_id}"))

    archived: dict[int, InstanceArchive] = {}
    missing = iids - set(live)
    if missing:
        for a in (db.query(InstanceArchive)
                    .filter(InstanceArchive.node_id == node.id,
                            InstanceArchive.agent_iid.in_(missing))
                    .order_by(InstanceArchive.id.desc()).all()):
            archived.setdefault(a.agent_iid, a)

    for b in items:
        if not isinstance(b, dict):
            continue
        name = str(b.get("file") or "")
        m = _BK_IID.match(name)
        iid = int(m.group(1)) if m else None
        category, desc = _backup_category(name)
        b["category"] = category
        b["desc"] = desc
        b["node_id"] = node.id
        b["node_name"] = node.name
        b["agent_iid"] = iid
        hit = live.get(iid) if iid is not None else None
        a = archived.get(iid) if (iid is not None and not hit) else None
        if hit:
            inst, uname = hit
            b.update(owner=uname, owner_id=inst.user_id, inst_name=inst.name,
                     instance_uuid=inst.uuid, source_state="live")
        elif a:
            b.update(owner=a.username, owner_id=a.user_id, inst_name=a.instance_name,
                     instance_uuid=a.instance_uuid, source_state="archived")
        elif iid is not None:
            b.update(owner="", owner_id=None, inst_name=f"实例 #{iid}",
                     instance_uuid="", source_state="deleted")
        else:
            b.update(owner="", owner_id=None, inst_name="节点级备份",
                     instance_uuid="", source_state="node")
    try:
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    except Exception:  # noqa: BLE001
        return content


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
    resp = await _forward(n, request, f"/agent/host/{path}")
    if request.method == "GET" and path.strip("/") == "backups" and resp.status_code == 200:
        return Response(content=_enrich_backups(resp.body, n, db),
                        status_code=resp.status_code,
                        media_type=resp.media_type)
    return resp


@router.api_route("/security/{path:path}", methods=list(FORWARD_METHODS))
async def security_proxy(path: str, request: Request,
                         node: str = "", db: Session = Depends(get_db)):
    """安全防护（fail2ban / 防火墙 open 复用接口）转发。"""
    n = _node_or_404(node, db)
    return await _forward(n, request, f"/agent/security/{path}")
