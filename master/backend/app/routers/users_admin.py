from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import hash_password, require_admin
from app.config import settings
from app.database import get_db
from app.models import User
from app.schemas import UserCreate, UserOut

router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/admin/users")
def list_users(page: int = 0, page_size: int = 20, keyword: str = "",
               db: Session = Depends(get_db)):
    """用户列表。带 page 参数返回 {items, total}（分页视图）；否则全量数组（旧调用兼容）。"""
    q = db.query(User).order_by(User.id)
    k = (keyword or "").strip()
    if k:
        q = q.filter(User.username.ilike(f"%{k}%"))
    if page >= 1:
        total = q.count()
        users = q.offset((page - 1) * page_size).limit(page_size).all()
        return {"items": [UserOut.model_validate(u).model_dump() for u in users], "total": total}
    return [UserOut.model_validate(u).model_dump() for u in q.all()]


@router.post("/admin/users")
def create_user(body: UserCreate, db: Session = Depends(get_db)):
    if body.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="角色只能是 admin 或 user")
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status_code=409, detail="用户名已存在")
    user = User(
        username=body.username.strip(),
        password_hash=hash_password(body.password),
        role=body.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user).model_dump()


@router.get("/admin/images")
def admin_images():
    return settings.all_images
