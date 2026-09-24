"""商城商品管理：管理员增删改查 + 上下架；商城页读取上架中的商品。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.database import get_db
from app.models import Plan
from app.schemas import PlanIn, PlanOut

router = APIRouter(tags=["plans"], dependencies=[Depends(require_admin)])
pub = APIRouter(tags=["plans"])


@pub.get("/plans")
def list_public_plans(db: Session = Depends(get_db)):
    """商城货架：仅上架商品，按 sort 升序。"""
    rows = db.query(Plan).filter(Plan.enabled == True).order_by(Plan.sort, Plan.id).all()  # noqa: E712
    return [PlanOut.model_validate(p).model_dump() for p in rows]


@router.get("/admin/plans")
def list_all_plans(db: Session = Depends(get_db)):
    rows = db.query(Plan).order_by(Plan.sort, Plan.id).all()
    return [PlanOut.model_validate(p).model_dump() for p in rows]


@router.post("/admin/plans")
def create_plan(body: PlanIn, db: Session = Depends(get_db)):
    plan = Plan(**body.model_dump())
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return PlanOut.model_validate(plan).model_dump()


@router.put("/admin/plans/{plan_id}")
def update_plan(plan_id: int, body: PlanIn, db: Session = Depends(get_db)):
    plan = db.get(Plan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="商品不存在")
    for k, v in body.model_dump().items():
        setattr(plan, k, v)
    db.commit()
    db.refresh(plan)
    return PlanOut.model_validate(plan).model_dump()


@router.post("/admin/plans/{plan_id}/toggle")
def toggle_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.get(Plan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="商品不存在")
    plan.enabled = not plan.enabled
    db.commit()
    return {"id": plan.id, "enabled": plan.enabled}


@router.delete("/admin/plans/{plan_id}")
def delete_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.get(Plan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="商品不存在")
    db.delete(plan)
    db.commit()
    return {"detail": "已删除"}
