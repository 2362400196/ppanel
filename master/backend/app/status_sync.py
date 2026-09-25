"""实例状态快照同步：按节点批量拉取被控实例列表，回写主控库（尽力而为）。

顺带同步 started_at（被控容器本次启动时间）：running 存值、停止置空，
供前端计算"稳定运行时长"（停止归零）。
"""
from datetime import datetime
import re

from sqlalchemy.orm import Session

from app.agent_client import agent_json
from app.models import Instance, Node


def _parse_ts(v):
    """解析被控 ISO 时间为 naive UTC datetime；失败返回 None。

    Docker 的 StartedAt 带纳秒（9 位小数），Python 3.10 fromisoformat
    最多接受 6 位，先截断到微秒。"""
    if not v:
        return None
    try:
        s = re.sub(r"\.(\d{6})\d+", r".\1", str(v))
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        return None


def sync_instances_status(db: Session) -> None:
    nodes = db.query(Node).filter(Node.enabled == True).all()  # noqa: E712
    for node in nodes:
        try:
            rows = agent_json(node, "GET", "/agent/instances", timeout=8)
        except Exception:  # noqa: BLE001 节点挂了不阻塞列表展示
            continue
        if not isinstance(rows, list):
            continue
        by_iid = {r.get("id"): r for r in rows if isinstance(r, dict) and "id" in r}
        instances = db.query(Instance).filter(Instance.node_id == node.id).all()
        dirty = False
        for inst in instances:
            row = by_iid.get(inst.agent_iid)
            if not row:
                if inst.status != "missing":
                    inst.status = "missing"  # 被控侧已无此实例
                    inst.started_at = None
                    dirty = True
                continue
            if row.get("status") and row["status"] != inst.status:
                old = inst.status
                inst.status = row["status"]
                # 非人为 running→exited 视为崩溃（主动 stop 由 instances 接口记 planned 事件）
                if old == "running" and row["status"] == "exited":
                    from app.stability import record_event
                    record_event(db, inst, "crash", "被控回报实例退出")
                dirty = True
            if row.get("ext_port") and row["ext_port"] != inst.ext_port:
                inst.ext_port = row["ext_port"]
                dirty = True
            # 运行时长锚点：以被控容器的真实 StartedAt 为准（Docker 重启/自愈自动更新）
            st = _parse_ts(row.get("started_at")) if row.get("status") == "running" else None
            if st != inst.started_at:
                inst.started_at = st
                dirty = True
        if dirty:
            db.commit()
