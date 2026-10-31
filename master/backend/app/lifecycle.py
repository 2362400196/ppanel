"""售卖生命周期巡检（独立线程，每 60s 一轮，尽力而为）：

1. 到期提醒：到期前 remind_days 天各发一条站内信（每天最多一条）
2. 到期处置：suspend=停机保数据（写 suspended_at/suspend_reason，面板侧同步只读）；
   off=只提醒不动容器
3. 宽限回收：到期超过 expire_grace_days 天 → 删除实例与数据（purge=1），0=永不删除
4. 流量执行：月流量超 80% 提醒，超 100% 按 traffic_action 停机
5. 磁盘执行：超 95% 提醒，超 100% 按 disk_action 停机（磁盘统计来自被控 stats）

设计约束：所有动作幂等——暂停看 suspended_at 是否已写，删除看代理 404 兜底；
通知去重按「实例+类型+当天/当月」查重，避免每轮轰炸。
"""
import logging
import threading
import time as time_mod
from datetime import timedelta

from app.agent_client import agent_json
from app.config import settings
from app.database import SessionLocal
from app.models import (Alert, Domain, Instance, Node, Notice, OpLog, utcnow)

log = logging.getLogger("ppanel.lifecycle")

LOOP_SECONDS = 60


# ---------- 小工具 ----------

def _notice(db, inst: Instance, level: str, title: str, content: str,
            mode: str = "always") -> None:
    """发站内信。mode：once=同实例同标题只发一条；daily=每天最多一条；always=每轮都发。"""
    q = db.query(Notice).filter(Notice.user_id == inst.user_id,
                                Notice.title == title)
    if inst.uuid:
        q = q.filter(Notice.instance_uuid == inst.uuid)
    if mode == "daily":
        day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        q = q.filter(Notice.created_at >= day_start)
    elif mode == "once":
        pass  # 只按 实例+标题 判重
    else:
        db.add(Notice(user_id=inst.user_id, instance_uuid=inst.uuid, level=level,
                      title=title, content=content))
        return
    if not q.first():
        db.add(Notice(user_id=inst.user_id, instance_uuid=inst.uuid, level=level,
                      title=title, content=content))


def _alert(db, inst: Instance, rule: str, level: str, title: str, detail: str,
           value: float | None = None, threshold: float | None = None) -> None:
    """管理员告警：同 dedup_key 已有 firing 记录则只刷新 last_seen。"""
    key = f"{rule}-{inst.uuid}"
    row = (db.query(Alert).filter(Alert.dedup_key == key, Alert.status == "firing").first())
    if row:
        row.last_seen_at = utcnow()
        return
    node = db.get(Node, inst.node_id)
    db.add(Alert(dedup_key=key, rule=rule, level=level, scope="instance",
                 node_id=inst.node_id, node_name=node.name if node else "",
                 instance_uuid=inst.uuid, instance_name=inst.name,
                 title=title, detail=detail, value=value, threshold=threshold))


def _resolve_alert(db, inst: Instance, rule: str) -> None:
    row = (db.query(Alert).filter(Alert.dedup_key == f"{rule}-{inst.uuid}",
                                  Alert.status == "firing").first())
    if row:
        row.status = "resolved"
        row.resolved_at = utcnow()


def _suspend(db, inst: Instance, node, reason: str, title: str, content: str) -> None:
    """平台暂停：停容器（尽力而为）+ 打标记 + 通知。幂等。"""
    if not inst.suspended_at:
        if inst.status == "running":
            try:
                agent_json(node, "POST", f"/agent/instances/{inst.agent_iid}/stop", timeout=15)
            except Exception:  # noqa: BLE001 停不掉下一轮再试
                log.warning("suspend stop failed iid=%s", inst.agent_iid)
        inst.suspended_at = utcnow()
        inst.suspend_reason = reason
        inst.status = "exited"
        db.add(OpLog(instance_uuid=inst.uuid, action="platform_suspend",
                     user_id=inst.user_id, detail=reason))
    _notice(db, inst, "warning", title, content, mode="daily")


def _resume(db, inst: Instance, node) -> None:
    """恢复运行（续费/管理员解除）：清标记并拉起容器。"""
    was_suspended = bool(inst.suspended_at)
    inst.suspended_at = None
    inst.suspend_reason = ""
    if was_suspended:
        try:
            agent_json(node, "POST", f"/agent/instances/{inst.agent_iid}/start", timeout=30)
            inst.status = "running"
        except Exception:  # noqa: BLE001
            log.warning("resume start failed iid=%s", inst.agent_iid)
        db.add(OpLog(instance_uuid=inst.uuid, action="platform_resume",
                     user_id=inst.user_id))


# ---------- 巡检 ----------

def _remind_and_expire(db) -> None:
    """到期提醒 + 到期暂停 + 宽限回收。"""
    remind = [int(x) for x in settings.expire_remind_days.split(",") if x.strip().isdigit()]
    now = utcnow()
    for inst in db.query(Instance).all():
        if not inst.expire_at:
            continue
        node = db.get(Node, inst.node_id)
        if not node or not node.enabled:
            continue
        left = (inst.expire_at - now).total_seconds() / 86400.0

        # 到期前提醒
        if left > 0:
            for d in remind:
                if left <= d:
                    _notice(db, inst, "warning",
                            f"实例「{inst.name}」将在 {d} 天内到期",
                            f"到期时间 {inst.expire_at:%Y-%m-%d %H:%M}。"
                            "请及时到商城续费，到期后实例将暂停（数据保留）。",
                            mode="once")
                    break
            continue

        # 已到期
        _notice(db, inst, "critical", f"实例「{inst.name}」已到期",
                f"已于 {inst.expire_at:%Y-%m-%d %H:%M} 到期"
                + (f"，宽限期 {settings.expire_grace_days} 天，"
                   f"逾期未续费将删除实例与全部数据" if settings.expire_grace_days > 0 else ""),
                mode="daily")
        if settings.expire_action != "suspend":
            continue
        _suspend(db, inst, node, "expire",
                 f"实例「{inst.name}」已到期暂停",
                 "实例已停止以释放资源，数据仍在。请到商城续费后自动恢复运行"
                 + (f"；超过宽限期 {settings.expire_grace_days} 天未续费将被删除"
                    if settings.expire_grace_days > 0 else ""))

        # 宽限期结束 → 回收
        if settings.expire_grace_days > 0:
            overdue = now - inst.suspended_at if inst.suspended_at else now - inst.expire_at
            if overdue >= timedelta(days=settings.expire_grace_days):
                try:
                    agent_json(node, "DELETE",
                               f"/agent/instances/{inst.agent_iid}?purge=1", timeout=30)
                except Exception as e:  # noqa: BLE001 404=被控已无记录，照常回收主控
                    if "404" not in str(e):
                        log.warning("purge failed iid=%s: %s", inst.agent_iid, e)
                        continue
                name = inst.name
                for d in db.query(Domain).filter(Domain.instance_uuid == inst.uuid).all():
                    db.delete(d)
                db.delete(inst)
                db.add(OpLog(action="platform_purge", instance_uuid=None,
                             user_id=inst.user_id,
                             detail=f"到期超宽限回收实例 {name}"))
                db.add(Notice(user_id=inst.user_id, instance_uuid=None, level="critical",
                              title=f"实例「{name}」已到期回收",
                              content=f"到期后超过 {settings.expire_grace_days} 天宽限期未续费，"
                                      f"实例与数据已删除。如需继续使用请重新购买。"))
                log.info("purged expired instance %s", name)


def _quota_check(db) -> None:
    """流量 / 磁盘额度执行：读被控 stats，超阈值提醒或停机。"""
    for inst in db.query(Instance).all():
        node = db.get(Node, inst.node_id)
        if not node or not node.enabled or inst.status != "running":
            continue
        if not inst.traffic_gb and not inst.disk_quota:
            continue
        try:
            st = agent_json(node, "GET", f"/agent/instances/{inst.agent_iid}/stats", timeout=8)
        except Exception:  # noqa: BLE001
            continue

        # ---- 流量 ----
        if inst.traffic_gb:
            used_gb = float(st.get("traffic_used_mb") or 0) / 1024.0
            quota = float(inst.traffic_gb)
            pct = used_gb / quota * 100 if quota > 0 else 0
            if pct >= 100:
                _alert(db, inst, "traffic_over", "critical",
                       f"实例「{inst.name}」流量超限",
                       f"本月已用 {used_gb:.1f}GB / {quota:.0f}GB", used_gb, quota)
                _notice(db, inst, "critical", f"实例「{inst.name}」本月流量已用完",
                        f"已用 {used_gb:.1f}GB / {quota:.0f}GB。"
                        + ("实例已暂停，下月自动恢复或联系管理员。" if settings.traffic_action == "stop"
                           else "请注意控制用量。"), mode="once")
                if settings.traffic_action == "stop":
                    _suspend(db, inst, node, "traffic",
                             f"实例「{inst.name}」流量超限暂停",
                             "本月流量额度已用完，实例已暂停。下月 1 日自动恢复，或联系管理员提升额度。")
            elif pct >= 80:
                _notice(db, inst, "warning", f"实例「{inst.name}」流量已用 80%",
                        f"本月已用 {used_gb:.1f}GB / {quota:.0f}GB，请注意控制用量。",
                        mode="daily")
                _alert(db, inst, "traffic_over", "warning",
                       f"实例「{inst.name}」流量用量超 80%",
                       f"本月已用 {used_gb:.1f}GB / {quota:.0f}GB", used_gb, quota)
            else:
                _resolve_alert(db, inst, "traffic_over")

        # ---- 磁盘（/app + 容器可写层）----
        if inst.disk_quota:
            used_mb = (float(st.get("disk_used_mb") or 0)
                       + float(st.get("rootfs_used_mb") or 0))
            quota_mb = float(inst.disk_quota)
            pct = used_mb / quota_mb * 100 if quota_mb > 0 else 0
            if pct >= 100:
                _alert(db, inst, "disk_over", "critical",
                       f"实例「{inst.name}」磁盘超限",
                       f"已用 {used_mb:.0f}MB / {quota_mb:.0f}MB", used_mb, quota_mb)
                _notice(db, inst, "critical", f"实例「{inst.name}」磁盘已满",
                        f"已用 {used_mb:.0f}MB / {quota_mb:.0f}MB。"
                        + ("实例已暂停，请清理文件后联系管理员恢复，或升配磁盘。" if settings.disk_action == "stop"
                           else "写入可能失败，请及时清理。"))
                if settings.disk_action == "stop":
                    _suspend(db, inst, node, "disk",
                             f"实例「{inst.name}」磁盘超限暂停",
                             "磁盘用量已超配额，实例已暂停。请清理文件（回收站/旧备份/日志）后联系管理员恢复。")
            elif pct >= 95:
                _notice(db, inst, "warning", f"实例「{inst.name}」磁盘即将写满",
                        f"已用 {used_mb:.0f}MB / {quota_mb:.0f}MB，请及时清理。",
                        mode="daily")
                _alert(db, inst, "disk_over", "warning",
                       f"实例「{inst.name}」磁盘用量超 95%",
                       f"已用 {used_mb:.0f}MB / {quota_mb:.0f}MB", used_mb, quota_mb)
            else:
                _resolve_alert(db, inst, "disk_over")

        # ---- 每月 1 日自动恢复流量暂停 ----
        if inst.suspend_reason == "traffic" and utcnow().day == 1:
            _resume(db, inst, node)
            _notice(db, inst, "info", f"实例「{inst.name}」已恢复运行",
                    "本月流量额度已重置，实例自动恢复。")
        db.commit()


def _tick() -> None:
    db = SessionLocal()
    try:
        _remind_and_expire(db)
        _quota_check(db)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("lifecycle tick failed")
    finally:
        db.close()


def lifecycle_loop() -> None:
    while True:
        _tick()
        time_mod.sleep(LOOP_SECONDS)


def start() -> threading.Thread:
    t = threading.Thread(target=lifecycle_loop, daemon=True, name="lifecycle")
    t.start()
    return t
