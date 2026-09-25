"""节点稳定性评分：心跳采集 + 崩溃事件 + 资源水位 → 100 分制评分。

维度与权重（方案 2026-09-25 定稿）：
  实例崩溃率 40%   崩溃次数 ÷（实例数 × 天数），封顶后线性扣分
  Agent 在线率 25% 心跳成功比
  资源水位 20%     内存 / 磁盘峰值超阈值线性扣分（OOM / 爆盘风险）
  服务异常 15%     心跳 detail 中的服务级故障（Docker 不可用等）占比

崩溃只计"非人为"：用户/管理员主动 stop 会落 stopped_planned 事件，
评分时排除；同一实例 10 分钟去重，rebuild 类瞬时抖动不计。
心跳滚动保留 14 天，事件保留 30 天。
"""
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.agent_client import agent_json
from app.database import SessionLocal
from app.models import Instance, InstanceEvent, Node, NodeHeartbeat, utcnow

RETAIN_HB_DAYS = 14
RETAIN_EV_DAYS = 30
CRASH_DEDUP_SEC = 600          # 同实例崩溃事件去重窗口
GRADE = ((90, "excellent", "优秀"), (75, "good", "良好"),
         (60, "fair", "一般"), (0, "poor", "异常"))


# ---------- 事件记录（status_sync / instances 操作落库） ----------

def record_event(db: Session, inst: Instance, event: str, detail: str = "") -> None:
    """写实例事件；crash 按 10 分钟窗口去重，且排除最近人为 stop 的实例。"""
    ts = utcnow()
    if event == "crash":
        recent_stop = (db.query(InstanceEvent)
                       .filter(InstanceEvent.instance_uuid == inst.uuid,
                               InstanceEvent.event == "stopped_planned",
                               InstanceEvent.ts >= ts - timedelta(seconds=CRASH_DEDUP_SEC + 300))
                       .first())
        if recent_stop:
            return
        dup = (db.query(InstanceEvent)
               .filter(InstanceEvent.instance_uuid == inst.uuid,
                       InstanceEvent.event == "crash",
                       InstanceEvent.ts >= ts - timedelta(seconds=CRASH_DEDUP_SEC))
               .first())
        if dup:
            return
    db.add(InstanceEvent(instance_uuid=inst.uuid, node_id=inst.node_id,
                         ts=ts, event=event, detail=detail[:255]))


# ---------- 心跳循环 ----------

def heartbeat_loop():
    """每 60s 对所有启用节点 ping + 采样资源水位；滚动清理旧数据。"""
    while True:
        try:
            _beat_once()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(60)


def _beat_once():
    db = SessionLocal()
    try:
        now = utcnow()
        for node in db.query(Node).filter(Node.enabled == True).all():  # noqa: E712
            hb = NodeHeartbeat(node_id=node.id, ts=now)
            t0 = time.monotonic()
            try:
                agent_json(node, "GET", "/agent/ping", timeout=5)
                hb.ok = True
                hb.latency_ms = round((time.monotonic() - t0) * 1000, 1)
                try:
                    stats = agent_json(node, "GET", "/agent/host/stats", timeout=5)
                    hb.mem_percent = (stats.get("mem") or {}).get("percent")
                    hb.disk_percent = (stats.get("disk") or {}).get("percent")
                except Exception:  # noqa: BLE001 ping 通但资源接口失败不记为宕机
                    pass
            except Exception as e:  # noqa: BLE001
                hb.ok = False
                hb.detail = str(e)[:200]
            db.add(hb)
        db.commit()
        # 滚动清理
        db.query(NodeHeartbeat).filter(
            NodeHeartbeat.ts < now - timedelta(days=RETAIN_HB_DAYS)).delete()
        db.query(InstanceEvent).filter(
            InstanceEvent.ts < now - timedelta(days=RETAIN_EV_DAYS)).delete()
        db.commit()
    finally:
        db.close()


# ---------- 评分 ----------

def node_stability(db: Session, node_id: int, window_days: int = 7) -> dict:
    """计算单节点稳定性评分（窗口内实时计算，数据量小直接查）。"""
    now = utcnow()
    since = now - timedelta(days=window_days)
    inst_count = (db.query(Instance)
                  .filter(Instance.node_id == node_id,
                          Instance.created_at <= now).count())

    hbs = (db.query(NodeHeartbeat)
           .filter(NodeHeartbeat.node_id == node_id,
                   NodeHeartbeat.ts >= since).all())
    total, ok_n = len(hbs), sum(1 for h in hbs if h.ok)
    online_rate = (ok_n / total * 100) if total else None
    mem_peak = max((h.mem_percent or 0) for h in hbs) if hbs else None
    disk_peak = max((h.disk_percent or 0) for h in hbs) if hbs else None
    svc_bad = sum(1 for h in hbs if h.detail and ("docker" in h.detail.lower()
                                                  or "连接被拒" in h.detail))

    crashes = (db.query(InstanceEvent)
               .filter(InstanceEvent.node_id == node_id,
                       InstanceEvent.event == "crash",
                       InstanceEvent.ts >= since).count())

    # 数据不足（新节点）：心跳样本 < 10 条（约 10 分钟）→ 观察中
    observed = total >= 10

    # ---- 分项得分 ----
    # 1) 崩溃率：允许基线 0.02 次/实例/天（约一月一次/实例），每超出 0.1 扣 1 分
    inst_days = max(inst_count * window_days, 1)
    crash_rate = crashes / inst_days
    crash_score = max(0, 100 - max(0, (crash_rate - 0.02) / 0.1) * 10) if crashes else 100
    crash_score = min(100, max(0, crash_score))
    # 2) 在线率
    online_score = online_rate if online_rate is not None else None
    # 3) 资源水位：>85% 起扣，每 1% 扣 1 分；磁盘 >90% 起扣
    def _res(v, warn):
        if v is None:
            return None
        over = max(0, v - warn)
        return max(0, 100 - over * 10)
    mem_score = _res(mem_peak, 85)
    disk_score = _res(disk_peak, 90)
    res_parts = [s for s in (mem_score, disk_score) if s is not None]
    res_score = (sum(res_parts) / len(res_parts)) if res_parts else None
    # 4) 服务异常
    svc_score = (100 - (svc_bad / total * 100) * 2) if total else None

    parts = {"crash": crash_score, "online": online_score,
             "resource": res_score, "service": svc_score}
    weights = {"crash": 0.4, "online": 0.25, "resource": 0.2, "service": 0.15}
    have = {k: v for k, v in parts.items() if v is not None}
    if not have or not observed:
        return {"score": None, "grade": "observing", "grade_label": "观察中",
                "window_days": window_days, "instances": inst_count,
                "crashes": crashes, "online_rate": round(online_rate, 1) if online_rate is not None else None,
                "mem_peak": mem_peak, "disk_peak": disk_peak,
                "parts": {k: round(v, 1) for k, v in have.items()},
                "last_crash_at": _last_crash(db, node_id)}
    score = round(sum(have[k] * weights[k] for k in have)
                  / sum(weights[k] for k in have), 1)
    for floor, g, label in GRADE:
        if score >= floor:
            grade, grade_label = g, label
            break
    return {"score": score, "grade": grade, "grade_label": grade_label,
            "window_days": window_days, "instances": inst_count,
            "crashes": crashes,
            "online_rate": round(online_rate, 1) if online_rate is not None else None,
            "mem_peak": mem_peak, "disk_peak": disk_peak,
            "parts": {k: round(v, 1) for k, v in have.items()},
            "last_crash_at": _last_crash(db, node_id)}


def _last_crash(db: Session, node_id: int):
    ev = (db.query(InstanceEvent)
          .filter(InstanceEvent.node_id == node_id,
                  InstanceEvent.event == "crash")
          .order_by(InstanceEvent.ts.desc()).first())
    return ev.ts.isoformat() + "Z" if ev else None


def all_nodes_stability(db: Session, window_days: int = 7) -> dict:
    """所有节点的稳定性评分（节点列表徽标一次拉全）。"""
    return {str(n.id): node_stability(db, n.id, window_days)
            for n in db.query(Node).filter(Node.enabled == True).all()}  # noqa: E712
