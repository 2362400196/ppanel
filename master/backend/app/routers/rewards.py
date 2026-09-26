"""福利中心：每日签到得积分、积分兑换实例天数、等级（累计消费）折扣、优惠券。
规则：
- 签到：每日 +10 积分
- 消费返积分：每实付 1 元 +10 积分（在 shop/buy 中落账）
- 等级：累计实付（分）升 LV1~LV5，等级越高购买折扣越大
- 兑换：100 积分 = 1 天，为指定实例续期
"""
import json
import secrets
import uuid as uuidlib
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent_client import owned_instance
from app.auth import Principal, get_principal, require_admin
from app.database import get_db
from app.models import (AppSetting, Checkin, Coupon, CouponUse, Instance, OpLog,
                        User, utcnow)

router = APIRouter(tags=["rewards"])

TZ = timezone(timedelta(hours=8))  # 签到按北京时间

# 福利规则默认配置（管理员可在后台「福利设置」调整，存 app_settings key=rewards_config）
CFG_KEY = "rewards_config"
DEFAULT_CFG = {
    "checkin_points": 10,       # 每日签到积分
    "earn_per_yuan": 10,        # 每实付 1 元返积分
    "points_per_day": 100,      # 兑换 1 天所需积分
    "levels": [                 # 等级：exp=累计实付分，pct=折扣百分比(100=无折扣)
        {"lv": 1, "exp": 0, "pct": 100},
        {"lv": 2, "exp": 1000, "pct": 98},
        {"lv": 3, "exp": 5000, "pct": 95},
        {"lv": 4, "exp": 20000, "pct": 92},
        {"lv": 5, "exp": 50000, "pct": 88},
    ],
}


def get_cfg(db: Session) -> dict:
    row = db.get(AppSetting, CFG_KEY)
    data = json.loads(row.value) if row and row.value else {}
    cfg = {**DEFAULT_CFG, **data}
    if not isinstance(cfg.get("levels"), list) or not cfg["levels"]:
        cfg["levels"] = DEFAULT_CFG["levels"]
    return cfg


def save_cfg(db: Session, cfg: dict) -> None:
    row = db.get(AppSetting, CFG_KEY)
    if row:
        row.value = json.dumps(cfg, ensure_ascii=False)
    else:
        db.add(AppSetting(key=CFG_KEY, value=json.dumps(cfg, ensure_ascii=False)))
    db.commit()


def _today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def level_of(exp: int, levels: list | None = None) -> tuple[int, int, int]:
    """返回 (等级, 当前档折扣%, 下一档阈值或 None)。levels=[{lv,exp,pct}] 按 exp 升序。"""
    rows = levels or DEFAULT_CFG["levels"]
    lv, pct, nxt = rows[0]["lv"], rows[0]["pct"], None
    for i, row in enumerate(rows):
        if exp >= row["exp"]:
            lv, pct = row["lv"], row["pct"]
            nxt = rows[i + 1]["exp"] if i + 1 < len(rows) else None
    return lv, pct, nxt


def _day_list(user_id: int, db: Session) -> list[str]:
    rows = (db.query(Checkin).filter(Checkin.user_id == user_id)
            .order_by(Checkin.day.desc()).limit(90).all())
    return [r.day for r in rows]


@router.get("/rewards/overview")
def overview(p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    user = db.get(User, p.user_id)
    cfg = get_cfg(db)
    lv, pct, nxt = level_of(user.level_exp if user else 0, cfg["levels"])
    days = _day_list(p.user_id, db)
    return {
        "points": user.points if user else 0,
        "level": lv, "level_exp": user.level_exp if user else 0,
        "discount_pct": pct, "next_level_exp": nxt,
        "checked_today": _today() in days,
        "checkin_days": len(days),
        "recent_days": days[:7],       # 最近签到日（倒序）
        "points_per_day": cfg["points_per_day"],
        "checkin_points": cfg["checkin_points"],
    }


@router.post("/rewards/checkin")
def checkin(p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    day = _today()
    if db.query(Checkin).filter(Checkin.user_id == p.user_id, Checkin.day == day).first():
        raise HTTPException(status_code=400, detail="今天已经签到过了，明天再来")
    cfg = get_cfg(db)
    pts = cfg["checkin_points"]
    user = db.get(User, p.user_id)
    user.points += pts
    db.add(Checkin(user_id=p.user_id, day=day, points=pts))
    db.add(OpLog(user_id=p.user_id, action="checkin",
                 detail=f"签到 +{pts}积分 现有 {user.points}"))
    db.commit()
    return {"detail": f"签到成功，+{pts} 积分", "points": user.points}


class RedeemIn(BaseModel):
    instance_uuid: str
    days: int = Field(ge=1, le=30)


@router.post("/rewards/redeem")
def redeem(body: RedeemIn, p: Principal = Depends(get_principal),
           db: Session = Depends(get_db)):
    """积分兑换：为实例续期（100 积分/天）。"""
    inst = owned_instance(body.instance_uuid, p.user_id, p.is_admin, db)
    cfg = get_cfg(db)
    cost = body.days * cfg["points_per_day"]
    user = db.get(User, p.user_id)
    if user.points < cost:
        raise HTTPException(status_code=400,
                            detail=f"积分不足：需要 {cost}，当前 {user.points}（每天需 {cfg['points_per_day']}）")
    old = inst.expire_at
    base = old if (old and old > utcnow()) else utcnow()
    inst.expire_at = base + timedelta(days=body.days)
    user.points -= cost
    db.add(OpLog(user_id=p.user_id, instance_uuid=inst.uuid, action="points_redeem",
                 detail=f"积分兑换 {body.days}天 -{cost}积分 现有 {user.points} "
                        f"新到期 {inst.expire_at.isoformat()}"))
    db.commit()
    return {"detail": f"兑换成功，「{inst.name}」已续期 {body.days} 天",
            "points": user.points, "expire_at": inst.expire_at.isoformat()}


# ---------- 优惠券 ----------

def _coupon_out(c: Coupon) -> dict:
    return {"id": c.id, "code": c.code, "amount_cents": c.amount_cents,
            "min_spend_cents": c.min_spend_cents, "total": c.total, "used": c.used,
            "left": max(0, c.total - c.used),
            "expire_at": c.expire_at.isoformat() if c.expire_at else None,
            "enabled": bool(c.enabled), "note": c.note,
            "created_at": c.created_at.isoformat() if c.created_at else None}


def _valid(coupon: Coupon) -> str | None:
    if not coupon.enabled:
        return "优惠券已停用"
    if coupon.used >= coupon.total:
        return "优惠券已抢完"
    if coupon.expire_at and coupon.expire_at < utcnow():
        return "优惠券已过期"
    return None


class CouponCheckIn(BaseModel):
    code: str
    plan_id: int


@router.post("/rewards/coupon/check")
def coupon_check(body: CouponCheckIn, p: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    """购买前试算：校验券可用性，返回可抵扣金额（基于商品原价门槛、抵扣在等级折扣后）。"""
    from app.models import Plan
    plan = db.get(Plan, body.plan_id)
    if not plan or not plan.enabled:
        raise HTTPException(status_code=404, detail="商品不存在或已下架")
    c = db.query(Coupon).filter(Coupon.code == body.code.strip().upper()).first()
    if not c:
        raise HTTPException(status_code=404, detail="优惠券码不存在")
    if err := _valid(c):
        raise HTTPException(status_code=400, detail=err)
    if plan.price_cents < c.min_spend_cents:
        raise HTTPException(status_code=400,
                            detail=f"未达使用门槛（需满 ¥{c.min_spend_cents / 100:.0f}）")
    user = db.get(User, p.user_id)
    _, pct, _ = level_of(user.level_exp, get_cfg(db)["levels"])
    after = plan.price_cents * pct // 100
    cut = min(c.amount_cents, after)
    return {"coupon": _coupon_out(c), "cut_cents": cut,
            "pay_cents": max(0, after - cut)}


class CouponIn(BaseModel):
    amount_yuan: float = Field(gt=0, le=10000)
    min_spend_yuan: float = Field(ge=0, le=100000, default=0)
    total: int = Field(ge=1, le=10000, default=10)
    expire_days: int = Field(ge=1, le=365, default=30)
    note: str = Field(default="", max_length=128)


@router.get("/admin/rewards/coupons", dependencies=[Depends(require_admin)])
def admin_coupons(db: Session = Depends(get_db)):
    rows = db.query(Coupon).order_by(Coupon.id.desc()).limit(200).all()
    return [_coupon_out(c) for c in rows]


@router.post("/admin/rewards/coupons", dependencies=[Depends(require_admin)])
def admin_create_coupon(body: CouponIn, db: Session = Depends(get_db)):
    code = f"PP{secrets.token_hex(4).upper()}"   # PP + 8 位随机
    c = Coupon(code=code, amount_cents=round(body.amount_yuan * 100),
               min_spend_cents=round(body.min_spend_yuan * 100),
               total=body.total,
               expire_at=utcnow() + timedelta(days=body.expire_days),
               note=body.note.strip())
    db.add(c)
    db.commit()
    return _coupon_out(c)


@router.post("/admin/rewards/coupons/{cid}/disable", dependencies=[Depends(require_admin)])
def admin_disable_coupon(cid: int, db: Session = Depends(get_db)):
    c = db.get(Coupon, cid)
    if not c:
        raise HTTPException(status_code=404, detail="优惠券不存在")
    c.enabled = 0
    db.commit()
    return {"detail": "已停用"}


# ---------- 管理员：福利规则设置 ----------

@router.get("/admin/rewards/config", dependencies=[Depends(require_admin)])
def admin_get_cfg(db: Session = Depends(get_db)):
    return get_cfg(db)


class LevelIn(BaseModel):
    lv: int = Field(ge=1, le=9)
    exp: int = Field(ge=0)           # 累计实付（分）
    pct: int = Field(ge=50, le=100)  # 折扣百分比，100=无折扣


class RewardsCfgIn(BaseModel):
    checkin_points: int = Field(ge=0, le=100000)
    earn_per_yuan: int = Field(ge=0, le=100000)
    points_per_day: int = Field(ge=1, le=1000000)
    levels: list[LevelIn]


@router.put("/admin/rewards/config", dependencies=[Depends(require_admin)])
def admin_save_cfg(body: RewardsCfgIn, db: Session = Depends(get_db)):
    if len(body.levels) < 1:
        raise HTTPException(status_code=400, detail="至少保留一个等级")
    rows = sorted(body.levels, key=lambda x: x.exp)
    for a, b in zip(rows, rows[1:]):
        if a.exp == b.exp:
            raise HTTPException(status_code=400, detail="等级经验阈值不能重复")
    cfg = {
        "checkin_points": body.checkin_points,
        "earn_per_yuan": body.earn_per_yuan,
        "points_per_day": body.points_per_day,
        "levels": [r.model_dump() for r in rows],
    }
    save_cfg(db, cfg)
    return {"detail": "福利规则已保存"}
