"""告警中心接口：事件列表 / 确认 / 手动恢复 / 规则与通知配置。"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.alerts import (DEFAULTS, LEVEL_LABEL, RULE_LABEL, load_config,
                        notify_one, public_config, save_config, summary)
from app.auth import require_admin
from app.database import get_db
from app.models import Alert, utcnow

router = APIRouter(tags=["alerts"], dependencies=[Depends(require_admin)])


def _out(a: Alert) -> dict:
    return {
        "id": a.id,
        "rule": a.rule,
        "rule_label": RULE_LABEL.get(a.rule, a.rule),
        "level": a.level,
        "level_label": LEVEL_LABEL.get(a.level, a.level),
        "scope": a.scope,
        "node_id": a.node_id,
        "node_name": a.node_name,
        "instance_uuid": a.instance_uuid,
        "instance_name": a.instance_name,
        "title": a.title,
        "detail": a.detail,
        "value": a.value,
        "threshold": a.threshold,
        "status": a.status,
        "fired_at": a.fired_at.isoformat() if a.fired_at else None,
        "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
        "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
        "notified_at": a.notified_at.isoformat() if a.notified_at else None,
        "notify_count": a.notify_count,
        "acked_at": a.acked_at.isoformat() if a.acked_at else None,
        "acked_by": a.acked_by,
    }


@router.get("/admin/alerts/summary")
def alerts_summary(db: Session = Depends(get_db)):
    return summary(db)


@router.get("/admin/alerts/config")
def get_config(db: Session = Depends(get_db)):
    return public_config(load_config(db))


class ConfigIn(BaseModel):
    enabled: bool | None = None
    disk_threshold: float | None = Field(default=None, ge=1, le=100)
    disk_critical: float | None = Field(default=None, ge=1, le=100)
    mem_threshold: float | None = Field(default=None, ge=1, le=100)
    mem_critical: float | None = Field(default=None, ge=1, le=100)
    offline_fail_count: int | None = Field(default=None, ge=1, le=60)
    latency_threshold_ms: float | None = Field(default=None, ge=0)
    crash_window_minutes: int | None = Field(default=None, ge=1, le=1440)
    crash_count: int | None = Field(default=None, ge=1, le=100)
    silence_minutes: int | None = Field(default=None, ge=0, le=1440)
    webhook_url: str | None = None
    webhook_type: str | None = None
    webhook_secret: str | None = None
    email_to: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_user: str | None = None
    smtp_pass: str | None = None
    smtp_ssl: bool | None = None


@router.put("/admin/alerts/config")
def put_config(body: ConfigIn, db: Session = Depends(get_db)):
    patch = body.model_dump(exclude_none=True)
    if patch.get("webhook_type") and patch["webhook_type"] not in (
            "generic", "dingtalk", "wechat", "feishu"):
        raise HTTPException(status_code=400, detail="不支持的 webhook 类型")
    # 先基于「合并后的最终值」校验，通过后再落库。
    # 否则非法请求会被写入，导致返回 400 但配置已被改动。
    merged = dict(load_config(db))
    for k, v in patch.items():
        if k in DEFAULTS:
            merged[k] = v
    if merged["disk_critical"] < merged["disk_threshold"]:
        raise HTTPException(status_code=400, detail="磁盘紧急阈值不能低于警告阈值")
    if merged["mem_critical"] < merged["mem_threshold"]:
        raise HTTPException(status_code=400, detail="内存紧急阈值不能低于警告阈值")
    cfg = save_config(db, patch)
    return public_config(cfg)


@router.post("/admin/alerts/test")
def test_notify(db: Session = Depends(get_db)):
    """发一条测试通知，用于验证 webhook / 邮件配置是否可用。"""
    cfg = load_config(db)
    snap = {
        "id": 0, "rule": "node_offline", "level": "warning", "scope": "node",
        "node_id": None, "node_name": "测试节点", "instance_uuid": None,
        "instance_name": "测试实例",
        "title": "这是一条测试告警",
        "detail": "收到这条消息说明通知渠道配置正确，无需处理。",
        "value": 0.0, "threshold": 0.0,
        "fired_at": utcnow().isoformat(),
    }
    configured, err = notify_one(cfg, "fired", snap)
    if not configured:
        raise HTTPException(status_code=400,
                            detail="尚未配置通知渠道，请先填写 Webhook 地址或邮件配置")
    if err:
        raise HTTPException(status_code=502, detail=f"通知发送失败：{err}")
    return {"ok": True, "detail": "测试通知已发送，请到接收端确认"}


@router.get("/admin/alerts")
def list_alerts(
    status: str = Query("active", pattern="^(active|firing|acked|resolved|all)$"),
    level: str | None = Query(None, pattern="^(critical|warning|info)$"),
    node_id: int | None = None,
    rule: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(Alert)
    if status == "active":
        q = q.filter(Alert.status.in_(("firing", "acked")))
    elif status != "all":
        q = q.filter(Alert.status == status)
    if level:
        q = q.filter(Alert.level == level)
    if node_id:
        q = q.filter(Alert.node_id == node_id)
    if rule:
        q = q.filter(Alert.rule == rule)
    total = q.count()
    rows = (q.order_by(Alert.status.asc(), Alert.level.desc(), Alert.fired_at.desc())
            .offset(offset).limit(limit).all())
    return {"total": total, "items": [_out(a) for a in rows]}


@router.post("/admin/alerts/{alert_id}/ack")
def ack_alert(alert_id: int, db: Session = Depends(get_db),
              who: str = "admin"):
    a = db.get(Alert, alert_id)
    if not a:
        raise HTTPException(status_code=404, detail="告警不存在")
    if a.status == "resolved":
        raise HTTPException(status_code=400, detail="该告警已恢复")
    a.status = "acked"
    a.acked_at = utcnow()
    a.acked_by = who
    db.commit()
    return _out(a)


@router.post("/admin/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: int, db: Session = Depends(get_db)):
    a = db.get(Alert, alert_id)
    if not a:
        raise HTTPException(status_code=404, detail="告警不存在")
    if a.status == "resolved":
        return _out(a)
    a.status = "resolved"
    a.resolved_at = utcnow()
    db.commit()
    return _out(a)


@router.delete("/admin/alerts/{alert_id}")
def delete_alert(alert_id: int, db: Session = Depends(get_db)):
    a = db.get(Alert, alert_id)
    if not a:
        raise HTTPException(status_code=404, detail="告警不存在")
    db.delete(a)
    db.commit()
    return {"ok": True}


@router.post("/admin/alerts/purge-resolved")
def purge_resolved(db: Session = Depends(get_db)):
    n = (db.query(Alert)
         .filter(Alert.status == "resolved")
         .delete(synchronize_session=False))
    db.commit()
    return {"ok": True, "deleted": n}


def _now_iso() -> str:
    return datetime.utcnow().isoformat()
