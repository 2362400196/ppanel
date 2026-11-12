import re
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth import Principal, create_token, get_principal, hash_password, verify_password
from app.config import settings
from app.database import get_db
from app.models import User
from app.schemas import ChangePasswordIn, LoginIn, RegisterIn, UserOut

router = APIRouter(tags=["auth"])

# 用户名：字母/数字/下划线/中划线/点，3-32 位，须以字母或数字开头
_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")

# 注册限流：同一 IP 每小时最多 N 次（进程内即可，主控单副本部署）
_reg_hits: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "") or "unknown"


def _check_rate(ip: str) -> None:
    limit = max(0, settings.register_ip_hourly_limit)
    if limit <= 0:
        return
    now = time.time()
    q = _reg_hits[ip]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(status_code=429, detail="注册过于频繁，请稍后再试")
    q.append(now)


@router.get("/auth/register-config")
def register_config():
    """注册入口配置：前端据此决定是否显示「注册」页签、是否显示邀请码。"""
    return {
        "enabled": settings.allow_register,
        "need_invite": bool(settings.register_invite_code),
        "min_password": 8,
    }


@router.post("/auth/register")
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    if not settings.allow_register:
        raise HTTPException(status_code=403, detail="当前已关闭自助注册，请联系管理员开通")

    username = body.username.strip()
    if not _USERNAME_RE.match(username):
        raise HTTPException(
            status_code=400,
            detail="用户名需 3-32 位，仅支持字母、数字、下划线、中划线和点，且以字母或数字开头",
        )
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="密码至少 8 位")

    _check_rate(_client_ip(request))

    if settings.register_invite_code:
        if (body.invite_code or "").strip() != settings.register_invite_code:
            raise HTTPException(status_code=400, detail="邀请码不正确")

    exists = db.query(User).filter(User.username == username).first()
    if exists:
        raise HTTPException(status_code=400, detail="该用户名已被占用")

    user = User(username=username, password_hash=hash_password(body.password), role="user")
    db.add(user)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=400, detail="该用户名已被占用")
    db.refresh(user)
    return {"token": create_token(user), "user": UserOut.model_validate(user).model_dump()}


@router.post("/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == body.username).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"token": create_token(user), "user": UserOut.model_validate(user).model_dump()}


@router.post("/auth/change-password")
def change_password(body: ChangePasswordIn, p: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    if p.user is None:  # X-API-Key 调用方无密码
        raise HTTPException(status_code=400, detail="API Key 调用方无密码")
    if not verify_password(body.old_password, p.user.password_hash):
        raise HTTPException(status_code=400, detail="原密码错误")
    p.user.password_hash = hash_password(body.new_password)
    db.commit()
    return {"detail": "密码已修改"}


@router.get("/me")
def me(p: Principal = Depends(get_principal)):
    if p.user is None:  # X-API-Key 调用方（商城系统）
        return {"username": "api-key", "role": "admin", "via_api_key": True}
    u = UserOut.model_validate(p.user).model_dump()
    u["via_api_key"] = False
    return u
