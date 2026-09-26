"""钱包 + 微信充值：
- 钱包：余额、流水；商城购买从余额扣款（不足则提示先充值）
- 充值：微信 Native 扫码下单 → 前端轮询查单 → SUCCESS 入账余额（微信侧为准，无法伪造）

安全设计：入账只由「微信查单返回 SUCCESS」驱动（幂等），不依赖回调验签；
扣款/入账均以分为单位（balance_cents），杜绝浮点误差。
"""
import secrets
import time
import uuid as uuidlib
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import wxpay_service
from app.agent_client import agent_json, resolve_node
from app.auth import Principal, get_principal, require_admin
from app.config import default_start_cmd, env_display_name
from app.database import get_db
from app.models import (Coupon, CouponUse, Instance, OpLog, Order, Plan, User,
                        utcnow)

router = APIRouter(tags=["pay"])


# ---------- 管理员：支付配置 ----------

@router.get("/admin/pay/config", dependencies=[Depends(require_admin)])
def pay_config(db: Session = Depends(get_db)):
    cfg = wxpay_service.get_config(db)
    pem = str(cfg.get("private_key_pem", "") or "")
    return {
        "enabled": bool(cfg.get("enabled")),
        "appid": cfg.get("appid", ""),
        "mchid": cfg.get("mchid", ""),
        "serial": cfg.get("serial", ""),
        "notify_url": cfg.get("notify_url", ""),
        "has_key": bool(pem.strip()),
    }


class PayConfigIn(BaseModel):
    enabled: bool = False
    appid: str = ""
    mchid: str = ""
    serial: str = ""
    private_key_pem: str = ""     # 可选：不传则保留原值
    notify_url: str = ""


@router.put("/admin/pay/config", dependencies=[Depends(require_admin)])
def save_pay_config(body: PayConfigIn, db: Session = Depends(get_db)):
    cfg = wxpay_service.get_config(db)
    cfg.update({
        "enabled": body.enabled,
        "appid": body.appid.strip(),
        "mchid": body.mchid.strip(),
        "serial": body.serial.strip(),
        "notify_url": body.notify_url.strip(),
    })
    if body.private_key_pem.strip():
        pem = body.private_key_pem.strip()
        try:  # 私钥格式校验，避免存进无效内容
            from cryptography.hazmat.primitives import serialization
            serialization.load_pem_private_key(pem.encode(), password=None)
        except Exception:
            raise HTTPException(status_code=400, detail="私钥格式无效（需 PEM 格式 apiclient_key.pem 内容）")
        cfg["private_key_pem"] = pem
    wxpay_service.save_config(db, cfg)
    return {"detail": "微信支付配置已保存",
            "enabled": wxpay_service.is_enabled(db)}


@router.get("/pay/status")
def pay_status(p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """商城据此判断：启用 → 余额扣款；未启用 → 免费直接开通。"""
    return {"enabled": wxpay_service.is_enabled(db)}


# ---------- 用户：钱包 ----------

def _own_order(no: str, user_id: int, db: Session) -> Order:
    order = db.query(Order).filter(Order.out_trade_no == no).first()
    if not order or order.user_id != user_id:
        raise HTTPException(status_code=404, detail="订单不存在")
    return order


def _order_out(r: Order, username: str = "") -> dict:
    return {"out_trade_no": r.out_trade_no, "kind": r.kind, "username": username,
            "plan_name": r.plan_name, "amount_cents": r.amount_cents, "status": r.status,
            "instance_uuid": r.instance_uuid,
            "created_at": r.created_at.isoformat() if r.created_at else None}


@router.get("/wallet")
def wallet(p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    user = db.get(User, p.user_id)
    return {"balance_cents": user.balance_cents if user else 0,
            "pay_enabled": wxpay_service.is_enabled(db)}


@router.get("/wallet/records")
def wallet_records(p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    rows = (db.query(Order).filter(Order.user_id == p.user_id)
            .order_by(Order.id.desc()).limit(100).all())
    return [_order_out(r) for r in rows]


class RechargeIn(BaseModel):
    amount_cents: int  # 分


@router.post("/wallet/recharge")
async def wallet_recharge(body: RechargeIn, p: Principal = Depends(get_principal),
                          db: Session = Depends(get_db)):
    """创建微信充值单（Native 扫码）。"""
    if not wxpay_service.is_enabled(db):
        raise HTTPException(status_code=400, detail="微信充值未开启，请联系管理员")
    if body.amount_cents < 1 or body.amount_cents > 1000000:  # 0.01元 ~ 1万元
        raise HTTPException(status_code=400, detail="充值金额需在 0.01 元 ~ 10000 元之间")
    no = f"RC{int(time.time() * 1000)}{secrets.token_hex(3).upper()}"
    code_url = await wxpay_service.native_order(
        db, no, body.amount_cents, f"PPanel钱包充值")
    db.add(Order(out_trade_no=no, user_id=p.user_id, kind="recharge",
                 plan_name="钱包充值", amount_cents=body.amount_cents, code_url=code_url))
    db.commit()
    return {"out_trade_no": no, "code_url": code_url, "amount_cents": body.amount_cents}


@router.get("/wallet/recharge/{no}/qrcode")
def recharge_qr(no: str, p: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    order = _own_order(no, p.user_id, db)
    if not order.code_url:
        raise HTTPException(status_code=404, detail="订单无支付码")
    return Response(wxpay_service.qr_png(order.code_url), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.get("/wallet/recharge/{no}")
async def recharge_state(no: str, p: Principal = Depends(get_principal),
                         db: Session = Depends(get_db)):
    """轮询充值单：微信 SUCCESS → 余额入账（幂等，仅首次）。"""
    order = _own_order(no, p.user_id, db)
    balance = db.get(User, p.user_id).balance_cents
    state = ""
    if order.status == "pending":
        try:
            data = await wxpay_service.query_order(db, no)
            state = data.get("trade_state") or ""
            if state == "SUCCESS":
                payer_total = (data.get("amount") or {}).get("payer_total")
                order.status = "paid"
                order.paid_at = utcnow()
                if payer_total:  # 实付为准（防止折扣/改价差异），差额忽略记日志
                    order.amount_cents = payer_total
                user = db.get(User, p.user_id)
                user.balance_cents += order.amount_cents
                balance = user.balance_cents
                db.add(OpLog(user_id=p.user_id, action="wallet_recharge",
                             detail=f"微信充值 +{order.amount_cents / 100}元 order={no}"))
                db.commit()
        except HTTPException:
            pass   # 查单失败（网络等）：保持 pending，前端继续轮询
    elif order.status == "paid":
        state = "SUCCESS"
    elif order.status == "failed":
        state = "PAYERROR"
    return {"trade_state": state, "balance_cents": balance}


# ---------- 用户：商城余额购买 ----------

class BuyIn(BaseModel):
    plan_id: int
    coupon_code: str = ""   # 可选优惠券码（先等级折扣，再券抵扣）


def _provision(db: Session, plan: Plan, user_id: int) -> Instance:
    """按商品规格开通实例（商城购买共用）。"""
    suffix = secrets.token_hex(2)
    expire = utcnow() + timedelta(days=plan.days)
    node = resolve_node(plan.node_id, db)
    image = plan.image or "python:3.11-slim"
    env_name = f"{env_display_name(image)}-面板-{suffix}"
    agent_data = agent_json(node, "POST", "/agent/instances", json={
        "name": env_name, "image": image, "start_cmd": "",
        "cpu_limit": plan.cpu, "mem_limit": plan.mem, "disk_quota": plan.disk,
        "expire_at": expire.isoformat(), "traffic_gb": plan.traffic_gb or None,
    })
    inst = Instance(
        uuid=str(uuidlib.uuid4()), user_id=user_id, node_id=node.id,
        agent_iid=agent_data["id"], name=env_name, image=image,
        start_cmd=default_start_cmd(image, plan.mem),
        ext_port=agent_data.get("ext_port", 0),
        cpu_limit=plan.cpu, mem_limit=plan.mem, disk_quota=plan.disk,
        status=agent_data.get("status", "created"),
        expire_at=expire, traffic_gb=plan.traffic_gb or None,
    )
    db.add(inst)
    db.flush()
    return inst


@router.post("/shop/buy")
def shop_buy(body: BuyIn, p: Principal = Depends(get_principal),
             db: Session = Depends(get_db)):
    """商城购买：钱包余额扣款（不足 402 引导充值）。
    价格链：商品原价 → 等级折扣 → 优惠券抵扣 = 实付；实付返积分并累加等级经验。
    """
    from app.routers.rewards import _valid, get_cfg, level_of
    plan = db.get(Plan, body.plan_id)
    if not plan or not plan.enabled:
        raise HTTPException(status_code=404, detail="商品不存在或已下架")
    user = db.get(User, p.user_id)

    # 等级折扣（等级表来自福利设置）
    cfg = get_cfg(db)
    _, pct, _ = level_of(user.level_exp, cfg["levels"])
    pay = plan.price_cents * pct // 100

    # 优惠券（一次性核销，抵扣在折扣后金额上）
    coupon = cut = None
    if body.coupon_code.strip():
        code = body.coupon_code.strip().upper()
        coupon = db.query(Coupon).filter(Coupon.code == code).first()
        if not coupon:
            raise HTTPException(status_code=404, detail="优惠券码不存在")
        if err := _valid(coupon):
            raise HTTPException(status_code=400, detail=err)
        if plan.price_cents < coupon.min_spend_cents:
            raise HTTPException(status_code=400,
                                detail=f"未达优惠券使用门槛（需满 ¥{coupon.min_spend_cents / 100:.0f}）")
        cut = min(coupon.amount_cents, pay)
        pay = max(0, pay - cut)

    if user.balance_cents < pay:
        need = (pay - user.balance_cents) / 100
        raise HTTPException(status_code=402,
                            detail=f"余额不足，还差 ¥{need:.2f}，请先到钱包充值")

    inst = _provision(db, plan, p.user_id)
    earned = pay // 100 * cfg["earn_per_yuan"]       # 消费返积分（按元取整）
    user.balance_cents -= pay
    user.points += earned
    user.level_exp += pay                            # 累计实付升级
    no = f"SP{int(time.time() * 1000)}{secrets.token_hex(3).upper()}"
    if coupon is not None and cut:
        coupon.used += 1
        db.add(CouponUse(coupon_id=coupon.id, user_id=p.user_id, order_no=no,
                         amount_cents=cut))
    db.add(Order(out_trade_no=no, user_id=p.user_id, kind="shop", plan_id=plan.id,
                 plan_name=plan.name, amount_cents=pay, status="paid",
                 paid_at=utcnow(), instance_uuid=inst.uuid))
    detail = (f"钱包购买 plan={plan.name} 原价 ¥{plan.price_cents / 100:.2f} "
              f"等级{pct // 100 and pct}% 折后 ¥{plan.price_cents * pct // 100 / 100:.2f}"
              + (f" 券抵 ¥{cut / 100:.2f}" if cut else "")
              + f" 实付 -{pay / 100:.2f}元 余额 {user.balance_cents / 100:.2f}元 "
                f"+{earned}积分 经验{user.level_exp}")
    db.add(OpLog(user_id=p.user_id, instance_uuid=inst.uuid, action="wallet_buy",
                 detail=detail))
    db.commit()
    return {"instance_uuid": inst.uuid, "name": inst.name,
            "balance_cents": user.balance_cents, "paid_cents": pay,
            "discount_pct": pct, "coupon_cut_cents": cut or 0,
            "points_earned": earned,
            "expire_at": inst.expire_at.isoformat() if inst.expire_at else None}


# ---------- 管理员：流水 ----------

@router.get("/admin/pay/orders", dependencies=[Depends(require_admin)])
def all_orders(db: Session = Depends(get_db)):
    rows = db.query(Order).order_by(Order.id.desc()).limit(200).all()
    users = {u.id: u for u in db.query(User).all()}
    out = []
    for r in rows:
        u = users.get(r.user_id)
        item = _order_out(r, u.username if u else f"#{r.user_id}")
        item["balance_cents"] = u.balance_cents if u else 0
        out.append(item)
    return out


class AdjustIn(BaseModel):
    user_id: int
    amount_cents: int        # 正=加余额，负=减余额
    note: str = ""           # 备注（进流水说明）


@router.post("/admin/pay/adjust", dependencies=[Depends(require_admin)])
def admin_adjust(body: AdjustIn, db: Session = Depends(get_db)):
    """管理员手动调整用户余额（加减均可，减后不能为负）。"""
    if body.amount_cents == 0:
        raise HTTPException(status_code=400, detail="调整金额不能为 0")
    user = db.get(User, body.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.balance_cents + body.amount_cents < 0:
        raise HTTPException(status_code=400,
                            detail=f"减后余额不能为负（当前余额 ¥{user.balance_cents / 100:.2f}）")
    user.balance_cents += body.amount_cents
    no = f"AD{int(time.time() * 1000)}{secrets.token_hex(3).upper()}"
    db.add(Order(out_trade_no=no, user_id=user.id, kind="admin", plan_name=body.note.strip() or "管理员调整",
                 amount_cents=abs(body.amount_cents), status="paid", paid_at=utcnow()))
    db.add(OpLog(user_id=user.id, action="balance_adjust",
                 detail=f"管理员调整余额 {'+' if body.amount_cents > 0 else ''}"
                        f"{body.amount_cents / 100}元 余额 {user.balance_cents / 100}元 {body.note.strip()}"))
    db.commit()
    return {"detail": f"已{'加' if body.amount_cents > 0 else '减'} "
                      f"¥{abs(body.amount_cents) / 100:.2f}（{user.username}）",
            "balance_cents": user.balance_cents}


class RefundIn(BaseModel):
    out_trade_no: str        # 商户订单号
    amount_cents: int        # 退款金额（分），默认全额由前端计算传入


@router.post("/admin/pay/refund", dependencies=[Depends(require_admin)])
async def admin_refund(body: RefundIn, db: Session = Depends(get_db)):
    """管理员退款：充值订单原路退回用户微信，同时等额扣减用户钱包余额。

    部分退款允许（累计不超订单金额）；退款后订单标记 refunded 并生成退款流水。
    """
    order = db.query(Order).filter(Order.out_trade_no == body.out_trade_no).first()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.kind != "recharge" or order.status != "paid":
        raise HTTPException(status_code=400, detail="仅「已入账」的充值订单可退款")
    refund = int(body.amount_cents)
    if refund <= 0 or refund > order.amount_cents:
        raise HTTPException(status_code=400,
                            detail=f"退款金额需在 ¥0.01 ~ ¥{order.amount_cents / 100:.2f} 之间")
    if not wxpay_service.is_enabled(db):
        raise HTTPException(status_code=400, detail="微信支付未启用，无法原路退款")

    user = db.get(User, order.user_id)
    if user and user.balance_cents < refund:
        raise HTTPException(status_code=400,
                            detail=f"用户余额 ¥{user.balance_cents / 100:.2f} 不足以扣回退款 "
                                   f"¥{refund / 100:.2f}（可能已消费），请先用「余额」扣减后再退款")

    await wxpay_service.refund(db, order.out_trade_no, refund, order.amount_cents)
    if user:
        user.balance_cents -= refund
    order.status = "refunded"
    no = f"RF{int(time.time() * 1000)}{secrets.token_hex(3).upper()}"
    db.add(Order(out_trade_no=no, user_id=order.user_id, kind="refund",
                 plan_name=f"退款 {order.out_trade_no[:20]}",
                 amount_cents=refund, status="paid", paid_at=utcnow()))
    db.add(OpLog(user_id=order.user_id, action="admin_refund",
                 detail=f"订单 {order.out_trade_no} 退款 {refund / 100}元 "
                        f"余额 {(user.balance_cents if user else 0) / 100}元"))
    db.commit()
    return {"detail": f"退款成功：¥{refund / 100:.2f} 已原路退回用户微信" +
                      (f"，余额已扣至 ¥{user.balance_cents / 100:.2f}" if user else ""),
            "balance_cents": user.balance_cents if user else 0}
