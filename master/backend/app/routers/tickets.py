"""工单：用户提交问题与需求，管理员回复处理。
会话式（气泡消息），支持图片/文件附件与 Markdown 代码；关闭后只读；管理员可见全部工单。
"""
import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import Principal, get_principal
from app.database import get_db
from app.models import Ticket, TicketFile, TicketMsg, User, utcnow

router = APIRouter(tags=["tickets"])

UPLOAD_DIR = os.path.join("data", "ticket_files")  # 与 master.db 同级的数据目录
MAX_FILE = 10 * 1024 * 1024  # 10MB
# 扩展名白名单：图片 / 文本代码 / 常见文档压缩
IMG_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
FILE_EXTS = IMG_EXTS | {
    "txt", "md", "log", "csv", "pdf",
    "py", "js", "ts", "json", "yaml", "yml", "html", "css", "go", "php", "sql",
    "sh", "bat", "ps1", "conf", "ini", "toml", "env",
    "zip", "gz", "tar", "rar", "7z",
    "docx", "xlsx", "pptx",
}


def _own(ticket_id: int, p: Principal, db: Session) -> Ticket:
    t = db.get(Ticket, ticket_id)
    if not t or (t.user_id != p.user_id and not p.is_admin):
        raise HTTPException(status_code=404, detail="工单不存在")
    return t


def _file_out(f: TicketFile | None) -> dict | None:
    if not f:
        return None
    return {"id": f.id, "name": f.orig_name, "size": f.size,
            "is_image": bool(f.is_image)}


def _out(t: Ticket, last: TicketMsg | None, username: str = "") -> dict:
    return {"id": t.id, "title": t.title, "status": t.status, "username": username,
            "last_msg": last.content[:60] if last else "",
            "last_by_admin": bool(last and last.is_admin),
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None}


def _list(db: Session, p: Principal, all: int):
    q = db.query(Ticket)
    if not (p.is_admin and all == 1):
        q = q.filter(Ticket.user_id == p.user_id)
    rows = q.order_by(Ticket.updated_at.desc()).limit(200).all()
    users = {u.id: u.username for u in db.query(User).all()}
    out = []
    for t in rows:
        last = (db.query(TicketMsg).filter(TicketMsg.ticket_id == t.id)
                .order_by(TicketMsg.id.desc()).first())
        out.append(_out(t, last, users.get(t.user_id, f"#{t.user_id}")))
    return out


class TicketIn(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=4000)


@router.get("/tickets")
def my_tickets(all: int = 0, p: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    return _list(db, p, all)


@router.post("/tickets")
def create_ticket(body: TicketIn, p: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)):
    t = Ticket(user_id=p.user_id, title=body.title.strip())
    db.add(t)
    db.flush()
    db.add(TicketMsg(ticket_id=t.id, user_id=p.user_id, content=body.content.strip()))
    db.commit()
    return {"id": t.id, "detail": "工单已提交"}


@router.get("/tickets/{ticket_id}")
def ticket_detail(ticket_id: int, p: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)):
    t = _own(ticket_id, p, db)
    msgs = (db.query(TicketMsg).filter(TicketMsg.ticket_id == t.id)
            .order_by(TicketMsg.id.asc()).all())
    files = {f.id: f for f in db.query(TicketFile).filter(TicketFile.ticket_id == t.id).all()}
    username = db.get(User, t.user_id).username
    out = []
    for m in msgs:
        item = {"id": m.id, "content": m.content, "is_admin": bool(m.is_admin),
                "is_mine": m.user_id == p.user_id, "file": None,
                "created_at": m.created_at.isoformat() if m.created_at else None}
        if m.file_id and m.file_id in files:
            item["file"] = _file_out(files[m.file_id])
        out.append(item)
    return {**_out(t, msgs[-1] if msgs else None, username), "messages": out}


class ReplyIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


@router.post("/tickets/{ticket_id}/reply")
def reply_ticket(ticket_id: int, body: ReplyIn, p: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    t = _own(ticket_id, p, db)
    if t.status == "closed":
        raise HTTPException(status_code=400, detail="工单已关闭，如需继续请重新提交")
    db.add(TicketMsg(ticket_id=t.id, user_id=p.user_id, is_admin=1 if p.is_admin else 0,
                     content=body.content.strip()))
    t.updated_at = utcnow()
    db.commit()
    return {"detail": "已回复"}


@router.post("/tickets/{ticket_id}/close")
def close_ticket(ticket_id: int, p: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    t = _own(ticket_id, p, db)
    t.status = "closed"
    t.updated_at = utcnow()
    db.commit()
    return {"detail": "工单已关闭"}


@router.post("/tickets/{ticket_id}/files")
async def upload_file(ticket_id: int, file: UploadFile,
                      p: Principal = Depends(get_principal),
                      db: Session = Depends(get_db)):
    """上传附件并直接作为一条消息发出（图片/文件，10MB 内）。"""
    t = _own(ticket_id, p, db)
    if t.status == "closed":
        raise HTTPException(status_code=400, detail="工单已关闭，无法上传附件")
    orig = os.path.basename(file.filename or "")
    ext = orig.rsplit(".", 1)[-1].lower() if "." in orig else ""
    if ext not in FILE_EXTS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型 .{ext}")
    data = await file.read()
    if len(data) > MAX_FILE:
        raise HTTPException(status_code=400, detail="文件不能超过 10MB")
    if not data:
        raise HTTPException(status_code=400, detail="空文件")

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    stored = f"{secrets.token_hex(12)}.{ext}"   # 随机名存储，杜绝路径/重名问题
    with open(os.path.join(UPLOAD_DIR, stored), "wb") as fp:
        fp.write(data)
    f = TicketFile(ticket_id=t.id, orig_name=orig, stored_name=stored,
                   size=len(data), is_image=1 if ext in IMG_EXTS else 0)
    db.add(f)
    db.flush()
    db.add(TicketMsg(ticket_id=t.id, user_id=p.user_id,
                     is_admin=1 if p.is_admin else 0, content="", file_id=f.id))
    t.updated_at = utcnow()
    db.commit()
    return {"detail": "附件已发送", "file": _file_out(f)}


@router.get("/tickets/files/{file_id}")
def download_file(file_id: int, p: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)):
    """附件下载/预览：仅工单归属用户与管理员可访问。"""
    f = db.get(TicketFile, file_id)
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")
    _own(f.ticket_id, p, db)
    path = os.path.join(UPLOAD_DIR, f.stored_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="文件已丢失")
    return FileResponse(path, filename=f.orig_name)
