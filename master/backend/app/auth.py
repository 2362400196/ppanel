from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, WebSocket
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def create_token(user: User) -> str:
    payload = {
        "sub": str(user.id),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> int | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        return int(payload.get("sub"))
    except (jwt.PyJWTError, TypeError, ValueError):
        return None


@dataclass
class Principal:
    """请求主体：JWT 登录用户，或商城对接的 X-API-Key（视为 admin）。"""
    user: User | None
    user_id: int | None
    is_admin: bool
    via_api_key: bool = False


def get_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    api_key = request.headers.get("X-API-Key")
    if settings.api_key and api_key and api_key == settings.api_key:
        return Principal(user=None, user_id=None, is_admin=True, via_api_key=True)

    authorization = request.headers.get("Authorization", "")
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not token:
        # 兼容 <img src=...?t=JWT> 场景（如 AI 生成的支付二维码），图片请求带不上 header
        token = request.query_params.get("t") or ""
    if token:
        user_id = decode_token(token)
        if user_id is not None:
            user = db.get(User, user_id)
            if user:
                return Principal(user=user, user_id=user.id, is_admin=user.role == "admin")

    raise HTTPException(status_code=401, detail="未登录或凭证已失效")


def require_admin(p: Principal = Depends(get_principal)) -> Principal:
    if not p.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return p


def ws_user(ws: WebSocket, db: Session) -> User | None:
    """WebSocket 握手鉴权：浏览器无法带 header，通过 ?token= 传递。"""
    token = ws.query_params.get("token", "")
    user_id = decode_token(token)
    if user_id is None:
        return None
    return db.get(User, user_id)
