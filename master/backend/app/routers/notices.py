"""站内信：到期提醒 / 暂停通知 / 额度告警的用户侧只读接口（写入口在 lifecycle）。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import Principal, get_principal
from app.database import get_db
from app.models import Notice, utcnow

router = APIRouter(tags=["notices"])


@router.get("/notices")
def my_notices(limit: int = 30, p: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    rows = (db.query(Notice).filter(Notice.user_id == p.user_id)
            .order_by(Notice.created_at.desc(), Notice.id.desc())
            .limit(min(max(limit, 1), 100)).all())
    return [{"id": n.id, "level": n.level, "title": n.title, "content": n.content,
             "read": n.read_at is not None,
             "created_at": n.created_at.isoformat() if n.created_at else None}
            for n in rows]


@router.get("/notices/unread-count")
def unread_count(p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    n = (db.query(Notice).filter(Notice.user_id == p.user_id, Notice.read_at == None)  # noqa: E711
         .count())
    return {"count": n}


@router.post("/notices/{notice_id}/read")
def mark_read(notice_id: int, p: Principal = Depends(get_principal),
              db: Session = Depends(get_db)):
    n = db.get(Notice, notice_id)
    if not n or n.user_id != p.user_id:
        raise HTTPException(status_code=404, detail="通知不存在")
    if n.read_at is None:
        n.read_at = utcnow()
        db.commit()
    return {"detail": "ok"}


@router.post("/notices/read-all")
def mark_all_read(p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    (db.query(Notice).filter(Notice.user_id == p.user_id, Notice.read_at == None)  # noqa: E711
     .update({Notice.read_at: utcnow()}, synchronize_session=False))
    db.commit()
    return {"detail": "ok"}
