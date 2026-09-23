from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import Principal, create_token, get_principal, verify_password
from app.database import get_db
from app.models import User
from app.schemas import LoginIn, UserOut

router = APIRouter(tags=["auth"])


@router.post("/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == body.username).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"token": create_token(user), "user": UserOut.model_validate(user).model_dump()}


@router.get("/me")
def me(p: Principal = Depends(get_principal)):
    if p.user is None:  # X-API-Key 调用方（商城系统）
        return {"username": "api-key", "role": "admin", "via_api_key": True}
    u = UserOut.model_validate(p.user).model_dump()
    u["via_api_key"] = False
    return u
