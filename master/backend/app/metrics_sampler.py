"""用量指标采样：定时拉取各节点运行中实例的 stats 落库，供前端画历史曲线。

独立线程里跑同步 DB/HTTP，避免阻塞事件循环；节点异常跳过，尽力而为。
"""
import asyncio
import logging
from datetime import timedelta

from app.agent_client import agent_json
from app.config import settings
from app.database import SessionLocal
from app.models import Instance, Metric, Node, utcnow

log = logging.getLogger("ppanel.metrics")


def _sample_once() -> None:
    db = SessionLocal()
    try:
        now = utcnow()
        nodes = db.query(Node).filter(Node.enabled == True).all()  # noqa: E712
        for node in nodes:
            try:
                rows = agent_json(node, "GET", "/agent/instances", timeout=8)
            except Exception:  # noqa: BLE001 节点不可达时跳过
                continue
            if not isinstance(rows, list):
                continue
            by_iid = {r.get("id"): r for r in rows if isinstance(r, dict)}
            for inst in db.query(Instance).filter(Instance.node_id == node.id).all():
                row = by_iid.get(inst.agent_iid)
                if not row or row.get("status") != "running":
                    continue
                try:
                    st = agent_json(node, "GET",
                                    f"/agent/instances/{inst.agent_iid}/stats", timeout=8)
                except Exception:  # noqa: BLE001
                    continue
                db.add(Metric(
                    instance_uuid=inst.uuid,
                    ts=now,
                    cpu_percent=float(st.get("cpu_percent") or 0),
                    mem_used_mb=float(st.get("mem_usage_mb") or 0),
                    mem_limit_mb=float(st.get("mem_limit_mb") or inst.mem_limit),
                    net_rx_mb=float(st.get("net_rx_mb") or 0),
                    net_tx_mb=float(st.get("net_tx_mb") or 0),
                ))
        cutoff = now - timedelta(hours=settings.metrics_retention_hours)
        db.query(Metric).filter(Metric.ts < cutoff).delete()
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("metrics sample failed")
    finally:
        db.close()


async def metrics_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_sample_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("metrics loop iteration error")
        await asyncio.sleep(settings.metrics_interval_seconds)
