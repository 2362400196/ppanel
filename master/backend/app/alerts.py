"""告警中心：规则检测 → 去重状态机 → 多渠道通知。

设计要点
--------
1. 检测在主控侧完成，被控无需改动：心跳循环每轮已采集各节点的可达性、
   内存/磁盘水位与异常摘要，这里直接复用。
2. 同一 dedup_key 同时只保留一条未恢复记录（firing/acked），避免重复轰炸；
   指标回落即置 resolved，再次触发会新开一条，历史可追溯。
3. 通知（HTTP/SMTP）一律在数据库事务之外发送 —— 主控曾因把网络 I/O 放在
   事务里导致 `database is locked`，此处不再重犯。
4. 未配置通知渠道时告警依然落库，前端红点可用。
"""
import base64
import hashlib
import hmac
import json
import logging
import smtplib
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Alert, AppSetting, Instance, InstanceEvent, Node, NodeHeartbeat, utcnow

log = logging.getLogger("ppanel.alerts")

CONFIG_KEY = "alert_config"
RETAIN_DAYS = 90

DEFAULTS = {
    "enabled": True,
    # 资源水位（百分比）
    "disk_threshold": 85,
    "disk_critical": 92,
    "mem_threshold": 90,
    "mem_critical": 96,
    # 节点可达性：连续多少次心跳失败才判定离线（60s/次）
    "offline_fail_count": 2,
    "latency_threshold_ms": 3000,
    # 实例反复崩溃：窗口内崩溃次数
    "crash_window_minutes": 30,
    "crash_count": 3,
    # 未恢复时重复通知的最小间隔（分钟），0 = 只通知一次
    "silence_minutes": 30,
    # 通知渠道
    "webhook_url": "",
    "webhook_type": "generic",   # generic / dingtalk / wechat / feishu
    "webhook_secret": "",
    "email_to": "",
    "smtp_host": "",
    "smtp_port": 465,
    "smtp_user": "",
    "smtp_pass": "",
    "smtp_ssl": True,
}

RULE_LABEL = {
    "node_offline": "节点离线",
    "disk_high": "磁盘空间不足",
    "mem_high": "内存占用过高",
    "service_bad": "节点服务异常",
    "instance_crash": "实例反复崩溃",
    "node_latency": "节点响应缓慢",
}

LEVEL_LABEL = {"critical": "紧急", "warning": "警告", "info": "提示"}


# ---------- 配置读写 ----------

def load_config(db: Session) -> dict:
    row = db.get(AppSetting, CONFIG_KEY)
    cfg = dict(DEFAULTS)
    if row and row.value:
        try:
            stored = json.loads(row.value)
            if isinstance(stored, dict):
                # smtp_pass 在接口层以掩码返回，写回时为空串表示"不修改"
                for k, v in stored.items():
                    if k in DEFAULTS:
                        cfg[k] = v
        except (json.JSONDecodeError, TypeError):
            log.warning("alert_config 解析失败，使用默认值")
    return cfg


def save_config(db: Session, patch: dict) -> dict:
    cfg = load_config(db)
    for k, v in (patch or {}).items():
        if k not in DEFAULTS:
            continue
        if k == "smtp_pass" and v == "":
            continue  # 前端留空表示保持原值
        cfg[k] = v
    row = db.get(AppSetting, CONFIG_KEY)
    if row is None:
        row = AppSetting(key=CONFIG_KEY)
        db.add(row)
    row.value = json.dumps(cfg, ensure_ascii=False)
    db.commit()
    return cfg


def public_config(cfg: dict) -> dict:
    """对外返回：密码只给"是否已设置"，不回显明文。"""
    out = {k: v for k, v in cfg.items() if k != "smtp_pass"}
    out["smtp_pass_set"] = bool(cfg.get("smtp_pass"))
    return out


# ---------- 检测 ----------

def _level_of(value: float, warn: float, crit: float) -> str | None:
    if value >= crit:
        return "critical"
    if value >= warn:
        return "warning"
    return None


def _collect(db: Session, cfg: dict, now: datetime) -> tuple[dict[str, dict], list[dict]]:
    """返回 (本轮仍在触发中的告警 key -> payload, 明细列表)。"""
    active: dict[str, dict] = {}
    nodes = db.query(Node).filter(Node.enabled == True).all()  # noqa: E712
    need = max(int(cfg["offline_fail_count"]), 1)

    for node in nodes:
        hbs = (db.query(NodeHeartbeat)
               .filter(NodeHeartbeat.node_id == node.id)
               .order_by(NodeHeartbeat.ts.desc())
               .limit(max(need, 3)).all())
        if not hbs:
            continue  # 刚接入还没采样，不判定

        # 1) 离线：最近 need 次连续失败
        recent = hbs[:need]
        if len(recent) >= need and all(not h.ok for h in recent):
            reason = next((h.detail for h in recent if h.detail), "") or "心跳无响应"
            active[f"node_offline:{node.id}"] = {
                "rule": "node_offline", "level": "critical", "scope": "node",
                "node_id": node.id, "node_name": node.name,
                "title": f"节点 {node.name} 离线",
                "detail": f"连续 {len(recent)} 次心跳失败：{reason}",
                "value": float(len(recent)), "threshold": float(need),
            }

        # 最近一条成功心跳（资源类规则只认成功样本，避免离线时误报资源告警）
        last_ok = next((h for h in hbs if h.ok), None)
        if not last_ok:
            continue

        if last_ok.disk_percent is not None:
            lv = _level_of(last_ok.disk_percent, cfg["disk_threshold"], cfg["disk_critical"])
            if lv:
                active[f"disk_high:{node.id}"] = {
                    "rule": "disk_high", "level": lv, "scope": "node",
                    "node_id": node.id, "node_name": node.name,
                    "title": f"节点 {node.name} 磁盘使用率 {last_ok.disk_percent:.1f}%",
                    "detail": (f"磁盘使用率 {last_ok.disk_percent:.1f}%，"
                               f"警告阈值 {cfg['disk_threshold']}%、"
                               f"紧急阈值 {cfg['disk_critical']}%。"
                               "写满后实例将无法写入数据。"),
                    "value": last_ok.disk_percent,
                    "threshold": cfg["disk_critical"] if lv == "critical" else cfg["disk_threshold"],
                }

        if last_ok.mem_percent is not None:
            lv = _level_of(last_ok.mem_percent, cfg["mem_threshold"], cfg["mem_critical"])
            if lv:
                active[f"mem_high:{node.id}"] = {
                    "rule": "mem_high", "level": lv, "scope": "node",
                    "node_id": node.id, "node_name": node.name,
                    "title": f"节点 {node.name} 内存使用率 {last_ok.mem_percent:.1f}%",
                    "detail": (f"内存使用率 {last_ok.mem_percent:.1f}%，"
                               f"警告阈值 {cfg['mem_threshold']}%、"
                               f"紧急阈值 {cfg['mem_critical']}%。"
                               "持续偏高可能触发 OOM。"),
                    "value": last_ok.mem_percent,
                    "threshold": cfg["mem_critical"] if lv == "critical" else cfg["mem_threshold"],
                }

        # 2) 服务异常：心跳 detail 里有 Docker 不可用等摘要
        detail_l = (last_ok.detail or "").lower()
        if detail_l and ("docker" in detail_l or "连接被拒" in detail_l or "refused" in detail_l):
            active[f"service_bad:{node.id}"] = {
                "rule": "service_bad", "level": "critical", "scope": "node",
                "node_id": node.id, "node_name": node.name,
                "title": f"节点 {node.name} 服务异常",
                "detail": f"心跳报告异常：{last_ok.detail}",
                "value": None, "threshold": None,
            }

        # 3) 响应缓慢
        if last_ok.latency_ms and last_ok.latency_ms >= cfg["latency_threshold_ms"]:
            active[f"node_latency:{node.id}"] = {
                "rule": "node_latency", "level": "warning", "scope": "node",
                "node_id": node.id, "node_name": node.name,
                "title": f"节点 {node.name} 响应缓慢 {last_ok.latency_ms:.0f}ms",
                "detail": (f"心跳延迟 {last_ok.latency_ms:.0f}ms，"
                           f"超过阈值 {cfg['latency_threshold_ms']}ms。"),
                "value": last_ok.latency_ms, "threshold": cfg["latency_threshold_ms"],
            }

    # 4) 实例反复崩溃：窗口内 crash 次数达阈值
    since = now - timedelta(minutes=int(cfg["crash_window_minutes"]))
    rows = (db.query(InstanceEvent.instance_uuid, func.count(InstanceEvent.id))
            .filter(InstanceEvent.event == "crash", InstanceEvent.ts >= since)
            .group_by(InstanceEvent.instance_uuid).all())
    for uuid_, cnt in rows:
        if cnt < int(cfg["crash_count"]):
            continue
        inst = db.query(Instance).filter(Instance.uuid == uuid_).first()
        name = inst.name if inst else uuid_[:8]
        active[f"instance_crash:{uuid_}"] = {
            "rule": "instance_crash", "level": "warning", "scope": "instance",
            "node_id": inst.node_id if inst else None,
            "node_name": "",
            "instance_uuid": uuid_, "instance_name": name,
            "title": f"实例 {name} 反复崩溃",
            "detail": (f"{cfg['crash_window_minutes']} 分钟内崩溃 {cnt} 次，"
                       f"阈值 {cfg['crash_count']} 次。请检查启动命令与资源限制。"),
            "value": float(cnt), "threshold": float(cfg["crash_count"]),
        }

    # 4c) 当前正处于崩溃重启循环的实例：不必等崩溃次数攒够阈值，服务此刻已经不可用
    for inst in db.query(Instance).filter(Instance.status == "restarting").all():
        active[f"instance_crash:{inst.uuid}"] = {
            "rule": "instance_crash", "level": "critical", "scope": "instance",
            "node_id": inst.node_id, "node_name": "",
            "instance_uuid": inst.uuid, "instance_name": inst.name,
            "title": f"实例 {inst.name} 处于崩溃重启循环",
            "detail": "容器反复退出并被自动拉起，服务当前不可用。请查看实例日志确认启动命令或代码错误。",
            "value": None, "threshold": None,
        }

    return active


def _snapshot(a: Alert) -> dict:
    return {
        "id": a.id, "rule": a.rule, "level": a.level, "scope": a.scope,
        "node_id": a.node_id, "node_name": a.node_name,
        "instance_uuid": a.instance_uuid, "instance_name": a.instance_name,
        "title": a.title, "detail": a.detail,
        "value": a.value, "threshold": a.threshold,
        "fired_at": a.fired_at.isoformat() if a.fired_at else None,
    }


def evaluate(db: Session, cfg: dict, now: datetime | None = None) -> list[tuple[str, dict]]:
    """执行一轮检测，落库状态变更，返回待通知事件 [(action, snapshot)]。

    action: fired / refired / resolved
    """
    now = now or utcnow()
    active = _collect(db, cfg, now)
    events: list[tuple[str, dict]] = []

    for key, payload in active.items():
        row = (db.query(Alert)
               .filter(Alert.dedup_key == key,
                       Alert.status.in_(("firing", "acked")))
               .first())
        if row is None:
            row = Alert(dedup_key=key, fired_at=now, last_seen_at=now,
                        status="firing", **payload)
            db.add(row)
            db.flush()
            events.append(("fired", _snapshot(row)))
            continue

        changed = (row.level != payload.get("level")
                   or row.value != payload.get("value"))
        row.last_seen_at = now
        row.value = payload.get("value")
        row.level = payload.get("level", row.level)
        row.detail = payload.get("detail", row.detail)
        row.title = payload.get("title", row.title)
        # 未恢复时的重复通知（含级别升级）；升级到 critical 必须立即通知
        silence = int(cfg["silence_minutes"] or 0)
        last_notify = row.notified_at or row.fired_at
        due = bool(silence) and (now - last_notify) >= timedelta(minutes=silence)
        if due or (changed and payload.get("level") == "critical"):
            events.append(("refired", _snapshot(row)))

    # 不在本轮 active 里的未恢复记录 → 恢复
    for row in (db.query(Alert)
                .filter(Alert.status.in_(("firing", "acked")))
                .all()):
        if row.dedup_key in active:
            continue
        row.status = "resolved"
        row.resolved_at = now
        events.append(("resolved", _snapshot(row)))

    # 滚动清理
    db.query(Alert).filter(Alert.status == "resolved",
                           Alert.resolved_at < now - timedelta(days=RETAIN_DAYS)).delete()
    db.commit()
    return events


# ---------- 通知 ----------

def _fmt_text(action: str, s: dict) -> str:
    label = {"fired": "告警", "refired": "告警持续", "resolved": "已恢复"}.get(action, action)
    lvl = LEVEL_LABEL.get(s.get("level"), s.get("level") or "")
    rule = RULE_LABEL.get(s.get("rule"), s.get("rule") or "")
    lines = [f"【{label}】{s.get('title') or ''}",
             f"级别：{lvl}    类型：{rule}"]
    if s.get("node_name"):
        lines.append(f"节点：{s['node_name']}")
    if s.get("instance_name"):
        lines.append(f"实例：{s['instance_name']}")
    if s.get("value") is not None and s.get("threshold") is not None:
        lines.append(f"实测：{s['value']}    阈值：{s['threshold']}")
    if s.get("detail"):
        lines.append(f"说明：{s['detail']}")
    if s.get("fired_at"):
        lines.append(f"触发时间：{s['fired_at']}Z")
    lines.append("—— PPanel 告警中心")
    return "\n".join(lines)


def _post_webhook(cfg: dict, action: str, s: dict) -> None:
    url = (cfg.get("webhook_url") or "").strip()
    if not url:
        return
    kind = (cfg.get("webhook_type") or "generic").strip().lower()
    text = _fmt_text(action, s)

    if kind == "dingtalk":
        body = {"msgtype": "text", "text": {"content": text}}
    elif kind == "wechat":
        body = {"msgtype": "text", "text": {"content": text}}
    elif kind == "feishu":
        body = {"msg_type": "text", "content": {"text": text}}
    else:
        body = {"event": "ppanel.alert", "action": action, **s,
                "level_label": LEVEL_LABEL.get(s.get("level"), ""),
                "rule_label": RULE_LABEL.get(s.get("rule"), ""),
                "text": text, "sent_at": utcnow().isoformat() + "Z"}

    if kind in ("dingtalk", "feishu"):
        secret = (cfg.get("webhook_secret") or "").strip()
        if secret:
            ts = str(int(time.time() * 1000))
            if kind == "dingtalk":
                sign = base64.b64encode(
                    hmac.new(secret.encode(), f"{ts}\n{secret}".encode(),
                             hashlib.sha256).digest()).decode()
                url = f"{url}&timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"
            else:
                sign = base64.b64encode(
                    hmac.new(f"{ts}\n{secret}".encode(), b"", hashlib.sha256).digest()).decode()
                body = dict(body, timestamp=ts, sign=sign)

    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=6) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"webhook 返回 {resp.status}")


def _send_mail(cfg: dict, action: str, s: dict) -> None:
    to_addr = (cfg.get("email_to") or "").strip()
    host = (cfg.get("smtp_host") or "").strip()
    if not to_addr or not host:
        return
    text = _fmt_text(action, s)
    msg = MIMEText(text, "plain", "utf-8")
    label = {"fired": "告警", "refired": "告警持续", "resolved": "已恢复"}.get(action, action)
    msg["Subject"] = Header(f"[PPanel {label}] {s.get('title') or ''}", "utf-8")
    msg["From"] = (cfg.get("smtp_user") or "").strip() or f"ppanel@{host}"
    msg["To"] = to_addr
    port = int(cfg.get("smtp_port") or 465)
    user = (cfg.get("smtp_user") or "").strip()
    pwd = cfg.get("smtp_pass") or ""
    if cfg.get("smtp_ssl", True):
        with smtplib.SMTP_SSL(host, port, timeout=8,
                              context=ssl.create_default_context()) as smtp:
            if user:
                smtp.login(user, pwd)
            smtp.sendmail(msg["From"], [x.strip() for x in to_addr.split(",") if x.strip()],
                          msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=8) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if user:
                smtp.login(user, pwd)
            smtp.sendmail(msg["From"], [x.strip() for x in to_addr.split(",") if x.strip()],
                          msg.as_string())


def notify_one(cfg: dict, action: str, snap: dict) -> tuple[bool, str]:
    """发一条通知，返回 (是否配置了渠道, 错误摘要)。"""
    has_webhook = bool((cfg.get("webhook_url") or "").strip())
    has_mail = bool((cfg.get("email_to") or "").strip() and (cfg.get("smtp_host") or "").strip())
    if not (has_webhook or has_mail):
        return False, "未配置通知渠道"
    errs = []
    if has_webhook:
        try:
            _post_webhook(cfg, action, snap)
        except Exception as e:  # noqa: BLE001 单渠道失败不影响另一个
            log.warning("webhook 通知失败：%s", e)
            errs.append(f"webhook: {e}")
    if has_mail:
        try:
            _send_mail(cfg, action, snap)
        except Exception as e:  # noqa: BLE001
            log.warning("邮件通知失败：%s", e)
            errs.append(f"邮件: {e}")
    return True, "; ".join(errs)[:300]


def run_round() -> None:
    """心跳循环每轮调用：检测 + 通知。整个过程不持有数据库事务做网络 I/O。"""
    db = SessionLocal()
    try:
        cfg = load_config(db)
        if not cfg.get("enabled"):
            return
        events = evaluate(db, cfg)
    except Exception:  # noqa: BLE001 告警失败绝不影响心跳
        log.exception("告警检测失败")
        return
    finally:
        db.close()

    if not events:
        return

    sent_ids: list[int] = []
    for action, snap in events:
        ok, err = notify_one(cfg, action, snap)
        if ok and not err:
            sent_ids.append(snap["id"])
        elif ok and err:
            log.warning("告警 %s 部分渠道失败：%s", snap["id"], err)

    if sent_ids:
        db2 = SessionLocal()
        try:
            now = utcnow()
            for a in db2.query(Alert).filter(Alert.id.in_(sent_ids)).all():
                a.notified_at = now
                a.notify_count = (a.notify_count or 0) + 1
            db2.commit()
        except Exception:  # noqa: BLE001
            db2.rollback()
            log.exception("告警通知状态回写失败")
        finally:
            db2.close()


# ---------- 查询辅助 ----------

def summary(db: Session) -> dict:
    rows = (db.query(Alert.level, Alert.status, func.count(Alert.id))
            .filter(Alert.status.in_(("firing", "acked")))
            .group_by(Alert.level, Alert.status).all())
    by_level = {"critical": 0, "warning": 0, "info": 0}
    total = 0
    for level, _status, cnt in rows:
        by_level[level] = by_level.get(level, 0) + cnt
        total += cnt
    return {"total": total, "by_level": by_level}
