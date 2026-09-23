from sqlalchemy.orm import Session

from app.models import OpLog


def log_op(db: Session, action: str, user_id: int | None = None,
           instance_id: int | None = None, detail: str = "") -> None:
    db.add(OpLog(user_id=user_id, instance_id=instance_id, action=action, detail=detail))
    db.commit()
