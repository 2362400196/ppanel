"""微信支付 V3（Native 扫码）：RSA2048-SHA256 签名、下单、查单、二维码。

配置存主控库 app_settings（wxpay_config），含商户私钥 PEM 文本，不下发前端。
查单以微信侧为准驱动订单状态与开通（回调不参与开通，无需验签，无法伪造）。
"""
import base64
import io
import json
import secrets
import time

import httpx
import qrcode
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import AppSetting

API = "https://api.mch.weixin.qq.com"
CFG_KEY = "wxpay_config"

_REQUIRED = ("appid", "mchid", "serial", "private_key_pem", "notify_url")


def get_config(db: Session) -> dict:
    row = db.get(AppSetting, CFG_KEY)
    return json.loads(row.value) if row and row.value else {}


def save_config(db: Session, cfg: dict) -> None:
    row = db.get(AppSetting, CFG_KEY)
    if row:
        row.value = json.dumps(cfg, ensure_ascii=False)
    else:
        db.add(AppSetting(key=CFG_KEY, value=json.dumps(cfg, ensure_ascii=False)))
    db.commit()


def is_enabled(db: Session) -> bool:
    cfg = get_config(db)
    if not cfg.get("enabled"):
        return False
    return all(str(cfg.get(k, "")).strip() for k in _REQUIRED)


def _private_key(cfg: dict):
    return serialization.load_pem_private_key(
        str(cfg["private_key_pem"]).encode(), password=None)


def _build_auth(cfg: dict, method: str, url_path: str, body_str: str) -> str:
    nonce = secrets.token_hex(16).upper()
    ts = int(time.time())
    message = f"{method}\n{url_path}\n{ts}\n{nonce}\n{body_str}\n"
    signature = base64.b64encode(
        _private_key(cfg).sign(message.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    ).decode()
    return (f'WECHATPAY2-SHA256-RSA2048 mchid="{cfg["mchid"]}",nonce_str="{nonce}",'
            f'signature="{signature}",timestamp="{ts}",serial_no="{cfg["serial"]}"')


async def wx_request(db: Session, method: str, url_path: str, body_obj=None) -> dict:
    cfg = get_config(db)
    if not all(str(cfg.get(k, "")).strip() for k in _REQUIRED):
        raise HTTPException(status_code=400, detail="微信支付配置不完整，请联系管理员")
    body_str = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":")) if body_obj else ""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": _build_auth(cfg, method, url_path, body_str),
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.request(method, API + url_path, headers=headers,
                                 content=body_str if method != "GET" else None)
    try:
        data = r.json()
    except Exception:
        data = {}
    if r.status_code >= 400:
        msg = (data.get("message") or data.get("code") or f"微信接口错误 {r.status_code}")
        raise HTTPException(status_code=502, detail=f"微信支付：{msg}")
    return data


async def native_order(db: Session, out_trade_no: str, amount_cents: int,
                       description: str) -> str:
    """Native 下单，返回 code_url（weixin://…）。"""
    cfg = get_config(db)
    data = await wx_request(db, "POST", "/v3/pay/transactions/native", {
        "appid": cfg["appid"], "mchid": cfg["mchid"],
        "description": description[:60],
        "out_trade_no": out_trade_no,
        "notify_url": cfg["notify_url"],
        "amount": {"total": int(amount_cents), "currency": "CNY"},
    })
    return data.get("code_url") or ""


async def query_order(db: Session, out_trade_no: str) -> dict:
    """按商户单号查单，返回微信侧订单数据（含 trade_state）。"""
    cfg = get_config(db)
    return await wx_request(
        db, "GET",
        f"/v3/pay/transactions/out-trade-no/{out_trade_no}?mchid={cfg['mchid']}")


async def refund(db: Session, out_trade_no: str, refund_cents: int,
                 total_cents: int) -> dict:
    """申请退款（原路退回用户微信零钱），返回微信退款单据。

    out_refund_no 商户退款单号自动生成；同一笔交易可多次部分退款（累计不超 total）。
    """
    out_refund_no = f"RF{time.strftime('%Y%m%d%H%M%S')}{secrets.token_hex(4).upper()}"
    return await wx_request(db, "POST", "/v3/refund/domestic/refunds", {
        "out_trade_no": out_trade_no,
        "out_refund_no": out_refund_no,
        "amount": {"refund": int(refund_cents), "total": int(total_cents),
                   "currency": "CNY"},
    })


def qr_png(text: str) -> bytes:
    """code_url → 二维码 PNG（薄荷绿码点，与面板主题呼应）。"""
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0F7A63", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
