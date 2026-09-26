"""AI 助手：DeepSeek 对话代理（管理员）+ 平台操作工具（Function Calling）。

- API Key 存主控库 app_settings 表（不落前端），对话经主控转发 DeepSeek 并以
  SSE 流式透传给前端（OpenAI 兼容 /chat/completions，stream=true）。
- 工具调用：模型可调用 list_nodes/add_node/list_plans/add_plan/compare_nodes，
  后端在 SSE 生成器内多轮循环执行（最多 6 轮），每步以 {"tool": ...} 事件推给前端。
"""
import json
import secrets
import socket
import string
import time
from datetime import timedelta
from typing import AsyncGenerator

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agent_client import agent_health, agent_json
from app.auth import hash_password, require_admin
from app.database import get_db
from app.models import (AppSetting, Instance, InstanceEvent, Node, OpLog,
                        Plan, User, utcnow)

router = APIRouter(prefix="/ai", tags=["ai"],
                   dependencies=[Depends(require_admin)])

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
ALLOWED_MODELS = ("deepseek-chat", "deepseek-reasoner")
KEY_NAME = "deepseek_api_key"
MAX_TOOL_ROUNDS = 10

SYSTEM_PROMPT = (
    "你是 PPanel 云面板的 AI 助手，内嵌在管理员后台，可以直接操作平台数据。"
    "PPanel 是多租户容器云面板（主控管理用户/节点/实例/商品，被控 agent 管理 Docker、备份、SSL）。\n"
    "可用工具与使用准则：\n"
    "- list_nodes：查询节点及其实例数（回答节点相关问题前先查）。\n"
    "- add_node(name, base_url, token, note?)：接入被控节点。base_url 是被控 agent 地址"
    "（如 http://1.2.3.4:9100），token 是被控的 X-Node-Token。缺必填信息时先向用户询问，不要编造。\n"
    "- list_plans：查询全部商品完整配置（用于回答商品问题或配置对比）。\n"
    "- add_plan(name, cpu核, mem_mb, disk_mb, days天, traffic_gb月流量0不限, price_yuan元, desc?, sort?, image?, node_id?)："
    "新建商城商品。用户给出规格和价格后再创建，缺信息先问；price_yuan 用元（如 9.9）。\n"
    "- compare_nodes：各节点实例分布 + 7 天稳定性评分，用于节点维度对比。\n"
    "- list_instances(keyword?)：查询实例清单（uuid/名称/节点/状态/端口）。\n"
    "- instance_detail(instance_uuid)：实例详情（镜像/规格/状态/端口/到期/流量等）。\n"
    "- instance_power(instance_uuid, action)：启停实例，action=start/stop/restart。用户明确要求时执行。\n"
    "- probe_instance(instance_uuid)：TCP 探测实例对外端口连通性（排查服务不可访问）。\n"
    "- node_test(node_id)：测试节点被控 agent 在线状态与健康信息。\n"
    "- recent_events(limit?)：最近的实例生命周期事件（崩溃/启停），排查异常时先用它。\n"
    "- list_users(keyword?)：查询用户（id/用户名/角色）。\n"
    "- create_user(username, role, password?)：创建用户；不传 password 时工具自动生成强密码并在结果里返回，必须原样转告用户。\n"
    "- list_backups(instance_uuid 或 node_id)：查询某节点的备份文件列表。\n"
    "- create_backup(instance_uuid, kind, db_name?, tables?, path?)：为实例创建备份。"
    "kind=db 备份数据库（整库，tables 非空则表级备份；不确定库名就先不传，多个库时工具会提示）；"
    "kind=dir 备份容器目录（path 默认 /app）。备份需数秒到数十秒，执行后如实汇报生成的文件名。\n"
    "- restore_backup(file, kind, …)：从备份恢复。⚠ 覆盖性操作：恢复数据库会覆盖目标库现有数据、"
    "恢复目录会覆盖同名文件。执行前必须先向用户复述目标与后果，拿到明确的确认答复（如「确认恢复」）才能调用；"
    "kind=db 需 version（如 5.7/8.0）与 db_name；kind=dir 需 instance_uuid（path 默认 /app）；"
    "kind=container 会从 .tar 导入镜像创建新容器（name 为新容器名，用于找回数据，不影响原容器）。\n"
    "规则：涉及创建操作，用户指令明确（参数齐全）就执行，完成后用一两句话汇报结果；"
    "参数不全或含糊时列出所缺信息向用户确认，绝不猜测 token、价格等关键值。"
    "配置对比用 Markdown 表格输出。全程简体中文、简洁专业。"
)

TOOLS = [
    {"type": "function", "function": {"name": "list_nodes", "description": "查询所有节点及其实例数量",
     "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "add_node", "description": "接入一个新的被控节点",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string", "description": "节点名称，唯一"},
         "base_url": {"type": "string", "description": "被控 agent 地址，如 http://1.2.3.4:9100"},
         "token": {"type": "string", "description": "被控的 X-Node-Token"},
         "note": {"type": "string", "description": "备注，可选"}}, "required": ["name", "base_url", "token"]}}},
    {"type": "function", "function": {"name": "list_plans", "description": "查询全部商品的完整配置（含上下架状态）",
     "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "add_plan", "description": "新建商城商品套餐",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string"}, "desc": {"type": "string"},
         "cpu": {"type": "number", "description": "CPU 核数"},
         "mem_mb": {"type": "integer", "description": "内存 MB"},
         "disk_mb": {"type": "integer", "description": "磁盘 MB"},
         "days": {"type": "integer", "description": "有效期天数"},
         "traffic_gb": {"type": "integer", "description": "月流量 GB，0=不限"},
         "price_yuan": {"type": "number", "description": "售价（元）"},
         "sort": {"type": "integer", "description": "排序，越小越靠前"},
         "image": {"type": "string", "description": "运行环境镜像，留空用默认"},
         "node_id": {"type": "integer", "description": "绑定节点 id，留空自动分配"}},
         "required": ["name", "price_yuan"]}}},
    {"type": "function", "function": {"name": "compare_nodes", "description": "节点维度对比：实例分布与稳定性评分",
     "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "list_instances", "description": "查询实例清单（名称/节点/状态/端口/uuid）",
     "parameters": {"type": "object", "properties": {
         "keyword": {"type": "string", "description": "按实例名模糊过滤，可选"}}, "required": []}}},
    {"type": "function", "function": {"name": "list_backups", "description": "查询某节点的备份文件列表（按时间倒序）",
     "parameters": {"type": "object", "properties": {
         "instance_uuid": {"type": "string", "description": "实例 uuid，可自动定位其节点"},
         "node_id": {"type": "integer", "description": "节点 id，与 instance_uuid 二选一"}}, "required": []}}},
    {"type": "function", "function": {"name": "create_backup", "description": "为实例创建备份（数据库或容器目录）",
     "parameters": {"type": "object", "properties": {
         "instance_uuid": {"type": "string", "description": "实例 uuid（list_instances 可查）"},
         "kind": {"type": "string", "enum": ["db", "dir"], "description": "db=数据库备份，dir=容器目录备份"},
         "db_name": {"type": "string", "description": "kind=db 时目标库名；不传且实例仅一个库时自动选定"},
         "tables": {"type": "array", "items": {"type": "string"}, "description": "kind=db 时表级备份的表名列表，空=整库"},
         "path": {"type": "string", "description": "kind=dir 时容器内绝对路径，默认 /app"}},
         "required": ["instance_uuid", "kind"]}}},
    {"type": "function", "function": {"name": "restore_backup", "description": "从备份恢复（覆盖性操作，须先获用户确认）",
     "parameters": {"type": "object", "properties": {
         "file": {"type": "string", "description": "备份文件名（list_backups 可查）"},
         "kind": {"type": "string", "enum": ["db", "dir", "container"]},
         "instance_uuid": {"type": "string", "description": "定位节点；kind=dir 时即恢复目标实例"},
         "node_id": {"type": "integer", "description": "节点 id（kind=db/container 时用，与 instance_uuid 二选一）"},
         "version": {"type": "string", "description": "kind=db 时 MySQL 版本，如 5.7 / 8.0"},
         "db_name": {"type": "string", "description": "kind=db 时目标库（覆盖导入）"},
         "path": {"type": "string", "description": "kind=dir 时容器内目标目录，默认 /app"},
         "name": {"type": "string", "description": "kind=container 时新容器名"}},
         "required": ["file", "kind"]}}},
    {"type": "function", "function": {"name": "instance_detail", "description": "查询单个实例的详细信息",
     "parameters": {"type": "object", "properties": {
         "instance_uuid": {"type": "string"}}, "required": ["instance_uuid"]}}},
    {"type": "function", "function": {"name": "instance_power", "description": "启动/停止/重启实例",
     "parameters": {"type": "object", "properties": {
         "instance_uuid": {"type": "string"},
         "action": {"type": "string", "enum": ["start", "stop", "restart"]}},
         "required": ["instance_uuid", "action"]}}},
    {"type": "function", "function": {"name": "probe_instance", "description": "TCP 探测实例对外端口是否可访问",
     "parameters": {"type": "object", "properties": {
         "instance_uuid": {"type": "string"}}, "required": ["instance_uuid"]}}},
    {"type": "function", "function": {"name": "node_test", "description": "测试节点被控 agent 是否在线并返回健康信息",
     "parameters": {"type": "object", "properties": {
         "node_id": {"type": "integer"}}, "required": ["node_id"]}}},
    {"type": "function", "function": {"name": "recent_events", "description": "最近的实例生命周期事件（崩溃/人为停止/启动）",
     "parameters": {"type": "object", "properties": {
         "limit": {"type": "integer", "description": "条数，默认 20，最大 50"}}, "required": []}}},
    {"type": "function", "function": {"name": "list_users", "description": "查询用户列表",
     "parameters": {"type": "object", "properties": {
         "keyword": {"type": "string", "description": "按用户名模糊过滤，可选"}}, "required": []}}},
    {"type": "function", "function": {"name": "create_user", "description": "创建面板用户；不传密码时自动生成强密码",
     "parameters": {"type": "object", "properties": {
         "username": {"type": "string"},
         "role": {"type": "string", "enum": ["user", "admin"]},
         "password": {"type": "string", "description": "可选；不传则自动生成并返回"}},
         "required": ["username", "role"]}}},
]


def _get_key(db: Session) -> str:
    row = db.get(AppSetting, KEY_NAME)
    return (row.value if row else "") or ""


@router.get("/config")
def ai_config(db: Session = Depends(get_db)):
    key = _get_key(db)
    return {"has_key": bool(key),
            "key_masked": (key[:6] + "****" + key[-4:]) if len(key) > 12 else "****"}


class KeyIn(BaseModel):
    api_key: str


@router.put("/config")
def save_key(body: KeyIn, db: Session = Depends(get_db)):
    k = body.api_key.strip()
    if not k:
        raise HTTPException(status_code=400, detail="API Key 不能为空")
    row = db.get(AppSetting, KEY_NAME)
    if row:
        row.value = k
    else:
        db.add(AppSetting(key=KEY_NAME, value=k))
    db.commit()
    return {"detail": "API Key 已保存"}


# ---------- 工具执行（同步、操作主控库） ----------

def _norm_base_url(url: str) -> str:
    # 与 nodes_admin 一致：localhost 统一为 127.0.0.1
    return url.strip().rstrip("/").replace("localhost", "127.0.0.1")


def _node_stats(db: Session, with_score: bool = False) -> list[dict]:
    data = []
    for n in db.query(Node).order_by(Node.id).all():
        q = db.query(Instance).filter(Instance.node_id == n.id)
        item = {"id": n.id, "name": n.name, "base_url": n.base_url, "enabled": n.enabled,
                "note": n.note, "instances": q.count(),
                "running": q.filter(Instance.status == "running").count()}
        data.append(item)
    if with_score and data:
        try:
            from app.stability import all_nodes_stability
            scores = all_nodes_stability(db, 7)   # 一次拉全，避免逐节点重算
            for item in data:
                s = scores.get(str(item["id"])) or {}
                item["stability_score"] = s.get("score")            # 0-100，None=观察中
                item["stability_label"] = s.get("grade_label")
                item["online_rate"] = s.get("online_rate")          # 7 天心跳在线率 %
                item["crashes_7d"] = s.get("crashes")
        except Exception:
            pass
    return data


def _inst_node(db: Session, uuid: str):
    inst = db.query(Instance).filter(Instance.uuid == str(uuid or "")).first()
    if not inst:
        return None, None, "实例不存在（可先用 list_instances 查询）"
    return inst, db.get(Node, inst.node_id), None


def _exec_tool(db: Session, name: str, p: dict) -> dict:
    try:
        if name == "list_nodes":
            return {"nodes": _node_stats(db)}
        if name == "add_node":
            nm = str(p.get("name", "")).strip()
            base = str(p.get("base_url", "")).strip()
            token = str(p.get("token", "")).strip()
            if not nm or not base or not token:
                return {"error": "name / base_url / token 均为必填"}
            if db.query(Node).filter(Node.name == nm).first():
                return {"error": f"节点名称「{nm}」已存在"}
            node = Node(name=nm, base_url=_norm_base_url(base), token=token,
                        note=str(p.get("note", "") or ""), enabled=True)
            db.add(node)
            db.commit()
            db.refresh(node)
            return {"detail": f"节点「{nm}」已创建（id={node.id}）", "id": node.id}
        if name == "list_plans":
            rows = db.query(Plan).order_by(Plan.sort, Plan.id).all()
            return {"plans": [{"id": r.id, "name": r.name, "desc": r.desc, "cpu": r.cpu,
                               "mem_mb": r.mem, "disk_mb": r.disk, "days": r.days,
                               "traffic_gb": r.traffic_gb, "price_yuan": r.price_cents / 100,
                               "sort": r.sort, "image": r.image, "node_id": r.node_id,
                               "enabled": r.enabled} for r in rows]}
        if name == "compare_nodes":
            return {"nodes": _node_stats(db, with_score=True)}
        if name == "add_plan":
            nm = str(p.get("name", "")).strip()
            if not nm:
                return {"error": "商品名称必填"}

            def _i(key, default):
                try:
                    return int(float(p.get(key, default)))
                except (TypeError, ValueError):
                    return default

            def _f(key, default):
                try:
                    return float(p.get(key, default))
                except (TypeError, ValueError):
                    return default

            node_id = p.get("node_id")
            if node_id is not None and not db.get(Node, int(node_id)):
                return {"error": f"节点 id={node_id} 不存在"}
            plan = Plan(
                name=nm, desc=str(p.get("desc", "") or ""),
                cpu=_f("cpu", 1.0), mem=_i("mem_mb", 512), disk=_i("disk_mb", 2048),
                days=_i("days", 30), traffic_gb=_i("traffic_gb", 0),
                price_cents=int(round(_f("price_yuan", 5.0) * 100)),
                sort=_i("sort", 0), image=str(p.get("image", "") or ""),
                node_id=int(node_id) if node_id is not None else None,
                enabled=bool(p.get("enabled", True)),
            )
            db.add(plan)
            db.commit()
            db.refresh(plan)
            return {"detail": (f"商品「{nm}」已创建（id={plan.id}）：{plan.cpu}核/{plan.mem}MB内存/"
                               f"{plan.disk}MB磁盘/{plan.days}天/月流量{plan.traffic_gb or '不限'}GB/"
                               f"{plan.price_cents / 100}元"), "id": plan.id}
        if name == "list_instances":
            q = db.query(Instance)
            kw = str(p.get("keyword", "") or "").strip().lower()
            if kw:
                q = q.filter(Instance.name.ilike(f"%{kw}%"))
            nodes = {n.id: n for n in db.query(Node).all()}
            return {"instances": [
                {"uuid": i.uuid, "name": i.name,
                 "node": nodes[i.node_id].name if i.node_id in nodes else f"节点#{i.node_id}",
                 "status": i.status, "ext_port": i.ext_port,
                 "created_at": i.created_at.isoformat() if i.created_at else None}
                for i in q.order_by(Instance.created_at.desc()).limit(50).all()]}
        if name == "list_backups":
            node = None
            if p.get("instance_uuid"):
                inst = db.query(Instance).filter(Instance.uuid == str(p["instance_uuid"])).first()
                if not inst:
                    return {"error": "实例不存在"}
                node = db.get(Node, inst.node_id)
            elif p.get("node_id") is not None:
                node = db.get(Node, int(p["node_id"]))
            else:
                all_nodes = db.query(Node).all()
                if len(all_nodes) == 1:
                    node = all_nodes[0]
                else:
                    return {"error": "存在多个节点，请指定 instance_uuid 或 node_id"}
            if not node:
                return {"error": "节点不存在"}
            data = agent_json(node, "GET", "/agent/host/backups",
                              params={"page": 1, "page_size": 50})
            return {"total": data.get("total"), "backups": data.get("backups", [])[:50]}
        if name == "create_backup":
            inst = db.query(Instance).filter(
                Instance.uuid == str(p.get("instance_uuid", ""))).first()
            if not inst:
                return {"error": "实例不存在（可先用 list_instances 查询）"}
            node = db.get(Node, inst.node_id)
            kind = str(p.get("kind", ""))
            if kind == "db":
                # 被控实时返回可备份库清单 [{version, db_name, running}]
                rows = agent_json(node, "GET", "/agent/host/backups/dbs").get("dbs", [])
                db_name = str(p.get("db_name", "") or "").strip()
                if db_name:
                    opt = next((r for r in rows if r.get("db_name") == db_name), None)
                    if not opt:
                        return {"error": f"库 {db_name} 不可备份，可用：{[r['db_name'] for r in rows]}"}
                elif len(rows) == 1:
                    opt = rows[0]
                else:
                    return {"error": f"请指定 db_name，可用：{[r['db_name'] for r in rows]}"}
                res = agent_json(node, "POST", "/agent/host/backups/database",
                                 json={"version": opt["version"], "db_name": opt["db_name"],
                                       "tables": [str(t) for t in (p.get("tables") or [])]},
                                 timeout=300)
                return {"detail": res.get("detail", "数据库备份完成"), "file": res.get("file")}
            if kind == "dir":
                res = agent_json(node, "POST", "/agent/host/backups/dir",
                                 json={"container_id": f"ppanel-{inst.agent_iid}",
                                       "path": str(p.get("path", "") or "/app")},
                                 timeout=300)
                return {"detail": res.get("detail", "目录备份完成"), "file": res.get("file")}
            return {"error": "kind 只支持 db（数据库）或 dir（容器目录）"}
        if name == "restore_backup":
            f = str(p.get("file", "")).strip()
            if not f:
                return {"error": "file 必填（list_backups 可查）"}
            kind = str(p.get("kind", ""))
            # 定位节点：dir 用实例定位；db/container 可用实例或节点
            if p.get("instance_uuid"):
                inst, node, err = _inst_node(db, p["instance_uuid"])
                if err:
                    return {"error": err}
            elif p.get("node_id") is not None:
                node = db.get(Node, int(p["node_id"]))
                inst = None
            else:
                return {"error": "请指定 instance_uuid 或 node_id"}
            if not node:
                return {"error": "节点不存在"}
            if kind == "db":
                version = str(p.get("version", "")).strip()
                db_name = str(p.get("db_name", "")).strip()
                if not version or not db_name:
                    return {"error": "恢复数据库需要 version（如 5.7/8.0）和 db_name"}
                res = agent_json(node, "POST", "/agent/host/backups/restore/database",
                                 json={"file": f, "version": version, "db_name": db_name},
                                 timeout=300)
                return {"detail": res.get("detail", "数据库已恢复")}
            if kind == "dir":
                if not inst:
                    return {"error": "目录恢复必须指定 instance_uuid（恢复进该实例容器）"}
                path = str(p.get("path", "") or "/app")
                res = agent_json(node, "POST", "/agent/host/backups/restore/dir",
                                 json={"file": f, "container_id": f"ppanel-{inst.agent_iid}",
                                       "path": path}, timeout=300)
                return {"detail": res.get("detail", "目录已恢复")}
            if kind == "container":
                cname = str(p.get("name", "")).strip()
                if not cname:
                    return {"error": "容器恢复需要 name（新容器名）"}
                res = agent_json(node, "POST", "/agent/host/backups/restore/container",
                                 json={"file": f, "name": cname}, timeout=600)
                return {"detail": res.get("detail", "容器备份已恢复为新容器"), "container": cname}
            return {"error": "kind 只支持 db / dir / container"}
        if name == "instance_detail":
            inst, node, err = _inst_node(db, p.get("instance_uuid"))
            if err:
                return {"error": err}
            item = {"uuid": inst.uuid, "name": inst.name, "image": inst.image,
                    "status": inst.status, "ext_port": inst.ext_port,
                    "cpu_limit": inst.cpu_limit, "mem_limit": inst.mem_limit,
                    "disk_quota": inst.disk_quota, "traffic_gb": inst.traffic_gb,
                    "started_at": inst.started_at.isoformat() if inst.started_at else None,
                    "created_at": inst.created_at.isoformat() if inst.created_at else None,
                    "expire_at": inst.expire_at.isoformat() if inst.expire_at else None,
                    "node": node.name if node else None}
            try:  # 被控实时状态（容器是否真在跑）
                data = agent_json(node, "GET", f"/agent/instances/{inst.agent_iid}")
                item["agent_status"] = data.get("status")
            except HTTPException as e:
                item["agent_error"] = str(e.detail) if getattr(e, "detail", None) else "节点不可达"
            return item
        if name == "instance_power":
            action = str(p.get("action", ""))
            if action not in ("start", "stop", "restart"):
                return {"error": "action 只支持 start/stop/restart"}
            inst, node, err = _inst_node(db, p.get("instance_uuid"))
            if err:
                return {"error": err}
            data = agent_json(node, "POST", f"/agent/instances/{inst.agent_iid}/{action}")
            if data.get("status"):
                inst.status = data["status"]
                db.commit()
            label = {"start": "已启动", "stop": "已停止", "restart": "已重启"}[action]
            return {"detail": f"实例「{inst.name}」{label}", "status": inst.status}
        if name == "probe_instance":
            inst, node, err = _inst_node(db, p.get("instance_uuid"))
            if err:
                return {"error": err}
            if not node or not inst.ext_port:
                return {"error": "实例未分配外部端口"}
            from urllib.parse import urlsplit
            host = urlsplit(node.base_url).hostname or ""
            if host == "localhost":
                host = "127.0.0.1"  # Windows 下 IPv6 优先会导致超时
            t0 = time.perf_counter()
            try:
                with socket.create_connection((host, inst.ext_port), timeout=2.5):
                    return {"ok": True, "ms": round((time.perf_counter() - t0) * 1000),
                            "detail": f"{host}:{inst.ext_port} 连通正常"}
            except OSError:
                return {"ok": False, "detail": f"{host}:{inst.ext_port} 不通（实例未运行或端口未放行）"}
        if name == "node_test":
            node = db.get(Node, int(p.get("node_id", 0)))
            if not node:
                return {"error": "节点不存在"}
            health = agent_health(node)
            if health is None:
                return {"online": False, "detail": f"节点「{node.name}」不可达或鉴权失败"}
            return {"online": True, "node": node.name, "health": health}
        if name == "recent_events":
            limit = max(1, min(int(p.get("limit", 20) or 20), 50))
            rows = (db.query(InstanceEvent)
                    .order_by(InstanceEvent.ts.desc()).limit(limit).all())
            insts = {i.uuid: i.name for i in db.query(Instance).all()}
            return {"events": [{"instance": insts.get(r.instance_uuid, r.instance_uuid[:8]),
                                "event": r.event, "detail": r.detail,
                                "ts": r.ts.isoformat() if r.ts else None} for r in rows]}
        if name == "list_users":
            q = db.query(User)
            kw = str(p.get("keyword", "") or "").strip().lower()
            if kw:
                q = q.filter(User.username.ilike(f"%{kw}%"))
            return {"users": [{"id": u.id, "username": u.username, "role": u.role}
                              for u in q.order_by(User.id).limit(50).all()]}
        if name == "create_user":
            uname = str(p.get("username", "")).strip()
            role = str(p.get("role", "user"))
            if not uname:
                return {"error": "用户名必填"}
            if role not in ("user", "admin"):
                return {"error": "role 只能是 user 或 admin"}
            if db.query(User).filter(User.username == uname).first():
                return {"error": f"用户名「{uname}」已存在"}
            pwd = str(p.get("password", "") or "").strip()
            generated = False
            if not pwd:
                # 自动生成 12 位强密码（字母+数字），结果里返回给 AI 转告用户
                alphabet = string.ascii_letters + string.digits
                pwd = "".join(secrets.choice(alphabet) for _ in range(12))
                generated = True
            user = User(username=uname, password_hash=hash_password(pwd), role=role)
            db.add(user)
            db.commit()
            db.refresh(user)
            out = {"detail": f"用户「{uname}」已创建（角色 {role}）", "id": user.id}
            if generated:
                out["generated_password"] = pwd
            return out
        return {"error": f"未知工具 {name}"}
    except Exception as e:  # noqa: BLE001
        db.rollback()
        return {"error": str(e)}


class ChatIn(BaseModel):
    messages: list[dict]          # [{role, content}]，不含 system
    model: str = "deepseek-chat"


@router.post("/chat")
async def ai_chat(body: ChatIn, db: Session = Depends(get_db)):
    key = _get_key(db)
    if not key:
        raise HTTPException(status_code=400, detail="未配置 DeepSeek API Key，请先在右上角设置")
    if body.model not in ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail="不支持的模型")
    # 只保留 user/assistant 消息，限制轮数与长度，防上下文炸掉
    history = [m for m in body.messages
               if m.get("role") in ("user", "assistant") and m.get("content")][-40:]
    payload_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + [
        {"role": m["role"], "content": str(m.get("content", ""))[:8000]} for m in history
    ]

    def _sse(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    async def gen() -> AsyncGenerator[str, None]:
        try:
            for _round in range(MAX_TOOL_ROUNDS + 1):
                tool_agg: dict[int, dict] = {}
                finish = ""
                async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15)) as client:
                    async with client.stream(
                        "POST", DEEPSEEK_URL,
                        headers={"Authorization": f"Bearer {key}",
                                 "Content-Type": "application/json"},
                        json={"model": body.model, "messages": payload_messages,
                              "stream": True, "tools": TOOLS},
                    ) as resp:
                        if resp.status_code != 200:
                            detail = (await resp.aread()).decode(errors="replace")[:300]
                            try:
                                detail = json.loads(detail).get("error", {}).get("message", detail)
                            except Exception:
                                pass
                            yield _sse({"error": f"DeepSeek {resp.status_code}: {detail}"})
                            return
                        async for line in resp.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            payload = line[6:].strip()
                            if payload == "[DONE]":
                                break
                            try:
                                obj = json.loads(payload)
                            except Exception:
                                continue
                            ch = (obj.get("choices") or [{}])[0]
                            delta = ch.get("delta") or {}
                            if delta.get("content"):
                                yield line + "\n\n"   # 正文增量原样透传
                            for tc in delta.get("tool_calls") or []:
                                idx = int(tc.get("index", 0))
                                agg = tool_agg.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                                if tc.get("id"):
                                    agg["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    agg["name"] += fn["name"]
                                if fn.get("arguments"):
                                    agg["arguments"] += fn["arguments"]
                            if ch.get("finish_reason"):
                                finish = ch["finish_reason"]
                if finish != "tool_calls" or not tool_agg:
                    return  # 正常结束（正文已流式发出）
                # 补 assistant tool_calls 消息，执行工具后进入下一轮
                calls = [tool_agg[i] for i in sorted(tool_agg)]
                payload_messages.append({
                    "role": "assistant", "content": "",
                    "tool_calls": [{"id": a["id"] or f"call_{i}", "type": "function",
                                    "function": {"name": a["name"],
                                                 "arguments": a["arguments"] or "{}"}}
                                   for i, a in enumerate(calls)]})
                for i, a in enumerate(calls):
                    try:
                        params = json.loads(a["arguments"]) if a["arguments"].strip() else {}
                    except Exception:
                        params = {}
                    yield _sse({"tool": {"name": a["name"], "status": "running"}})
                    result = _exec_tool(db, a["name"], params)
                    ok = "error" not in result
                    yield _sse({"tool": {"name": a["name"],
                                         "status": "ok" if ok else "fail",
                                         "detail": result.get("detail") or result.get("error", "")}})
                    payload_messages.append({
                        "role": "tool",
                        "tool_call_id": a["id"] or f"call_{i}",
                        "content": json.dumps(result, ensure_ascii=False)})
            yield _sse({"error": "工具调用轮数超限，已中断"})
        except httpx.HTTPError as e:
            yield _sse({"error": f"连接 DeepSeek 失败：{e}"})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
