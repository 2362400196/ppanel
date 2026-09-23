"""节点管理：接入/编辑/删除被控 Agent，附健康探测。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.agent_client import agent_health
from app.auth import require_admin
from app.database import get_db
from app.models import Instance, Node
from app.schemas import NodeCreate, NodeUpdate

router = APIRouter(tags=["nodes"], dependencies=[Depends(require_admin)])


def _norm_base_url(url: str) -> str:
    # localhost 在 Windows 上有 IPv6 回退延迟，入库时统一为 127.0.0.1
    return url.strip().rstrip("/").replace("localhost", "127.0.0.1")


def _out(node: Node, health: dict | None = None) -> dict:
    data = {
        "id": node.id,
        "name": node.name,
        "base_url": node.base_url,
        "enabled": node.enabled,
        "note": node.note,
        "created_at": node.created_at.isoformat() if node.created_at else None,
        "online": health is not None,
    }
    if health:
        data["health"] = health
    return data


@router.get("/admin/nodes")
def list_nodes(db: Session = Depends(get_db)):
    nodes = db.query(Node).order_by(Node.id).all()
    return [_out(n, agent_health(n)) for n in nodes]


@router.post("/admin/nodes")
def create_node(body: NodeCreate, db: Session = Depends(get_db)):
    if db.query(Node).filter(Node.name == body.name.strip()).first():
        raise HTTPException(status_code=409, detail="节点名称已存在")
    node = Node(name=body.name.strip(), base_url=_norm_base_url(body.base_url),
                token=body.token.strip(), note=body.note, enabled=body.enabled)
    db.add(node)
    db.commit()
    db.refresh(node)
    return _out(node, agent_health(node))


@router.patch("/admin/nodes/{node_id}")
def update_node(node_id: int, body: NodeUpdate, db: Session = Depends(get_db)):
    node = db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    if body.name is not None and body.name.strip() != node.name:
        if db.query(Node).filter(Node.name == body.name.strip()).first():
            raise HTTPException(status_code=409, detail="节点名称已存在")
        node.name = body.name.strip()
    if body.base_url is not None:
        node.base_url = _norm_base_url(body.base_url)
    if body.token is not None:
        node.token = body.token.strip()
    if body.note is not None:
        node.note = body.note
    if body.enabled is not None:
        node.enabled = body.enabled
    db.commit()
    db.refresh(node)
    return _out(node, agent_health(node))


@router.delete("/admin/nodes/{node_id}")
def delete_node(node_id: int, db: Session = Depends(get_db)):
    node = db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    count = db.query(Instance).filter(Instance.node_id == node_id).count()
    if count:
        raise HTTPException(status_code=409, detail=f"节点下仍有 {count} 个实例，禁止删除")
    db.delete(node)
    db.commit()
    return {"detail": "节点已删除"}


@router.post("/admin/nodes/{node_id}/test")
def test_node(node_id: int, db: Session = Depends(get_db)):
    node = db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    health = agent_health(node)
    if health is None:
        return {"online": False, "detail": "节点不可达或鉴权失败"}
    return {"online": True, "health": health}
