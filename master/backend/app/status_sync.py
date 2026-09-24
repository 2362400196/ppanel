"""实例状态快照同步：按节点批量拉取被控实例列表，回写主控库（尽力而为）。"""
from sqlalchemy.orm import Session

from app.agent_client import agent_json
from app.models import Instance, Node


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
                    dirty = True
                continue
            if row.get("status") and row["status"] != inst.status:
                inst.status = row["status"]
                dirty = True
            if row.get("ext_port") and row["ext_port"] != inst.ext_port:
                inst.ext_port = row["ext_port"]
                dirty = True
        if dirty:
            db.commit()
