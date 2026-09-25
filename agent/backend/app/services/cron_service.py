"""实例定时任务：5 段 cron 表达式 + 宿主调度循环（docker exec 进实例容器执行）。

不依赖 croniter：自实现字段匹配（*、*/n、a-b、a,b、a-b/n；dow 0/7=周日）。
调度循环与 ppanel-agent 同生命周期，每 20s 醒一次：
  - enabled 的任务在其 cron 命中的整分钟触发一次（按 last_run 去重）
  - 实例已到期 → 跳过
  - 容器未运行 → 记 fail（不阻塞）
  - 同时清理过期 phpMyAdmin 临时容器（见 pma_service）
"""
import threading
import time
import traceback
from datetime import datetime, timedelta

from app.database import SessionLocal
from app.docker_client import get_docker, get_docker_long
from app.models import Instance, InstanceCron, utcnow

_SCHED_WAIT = 20


def _field_hit(expr: str, val: int, lo: int, hi: int) -> bool:
    expr = expr.strip()
    if expr == "*":
        return True
    ok = False
    for part in expr.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            try:
                step = max(1, int(step_s))
            except ValueError:
                return False
        if part == "*":
            rng = range(lo, hi + 1, step)
        elif "-" in part:
            a, b = part.split("-", 1)
            try:
                rng = range(int(a), int(b) + 1, step)
            except ValueError:
                return False
        else:
            try:
                rng = range(int(part), int(part) + 1, step)
            except ValueError:
                return False
        if val in rng:
            ok = True
    return ok


def cron_match(schedule: str, now: datetime) -> bool:
    """严格校验 + 匹配 5 段表达式；非法表达式返回 False。"""
    f = (schedule or "").split()
    if len(f) != 5:
        return False
    dow = (now.isoweekday() % 7)  # 周一=1…周日=7 → 0=周日
    return (_field_hit(f[0], now.minute, 0, 59)
            and _field_hit(f[1], now.hour, 0, 23)
            and _field_hit(f[2], now.day, 1, 31)
            and _field_hit(f[3], now.month, 1, 12)
            and _field_hit(f[4], dow, 0, 6))


def validate(schedule: str) -> str | None:
    """字段级可达性校验：枚举分/时/日/月/周各自可命中的值，
    存在任一「全字段同时命中」的时刻即合法（与 cron_match 的 AND 语义严格一致）。
    之前的固定采样点（4时×4分）会误杀 2:00 这类不落采样网格的表达式。"""
    f = (schedule or "").split()
    if len(f) != 5:
        return "格式须为 5 段：分 时 日 月 周"
    if not any(_field_hit(f[0], m, 0, 59) for m in range(60)):
        return "分钟字段在任何分钟都不会触发"
    if not any(_field_hit(f[1], h, 0, 23) for h in range(24)):
        return "小时字段在任何小时都不会触发"
    base = datetime.now().date()
    for days in range(731):  # 两年内找一天满足 日+月+周 组合（拦 2 月 30 日这类永不存在）
        t = base + timedelta(days=days)
        if (_field_hit(f[2], t.day, 1, 31) and _field_hit(f[3], t.month, 1, 12)
                and _field_hit(f[4], (t.isoweekday() % 7), 0, 6)):
            return None
    return "日/月/周组合在可预见时间内不会出现（例如 2 月 31 日）"


def _expired(inst: Instance, now: datetime) -> bool:
    return bool(inst.expire_at) and now >= inst.expire_at


def _run_one(row: InstanceCron) -> None:
    """在工作线程里执行单条任务并回写结果。"""
    db = SessionLocal()
    try:
        r = db.get(InstanceCron, row.id)
        if not r or not r.enabled:
            return
        out, status = "", "fail"
        try:
            c = get_docker_long().containers.get(f"ppanel-{r.instance_id}")
            if c.status != "running":
                out, status = "容器未运行", "fail"
            else:
                res = c.exec_run(["sh", "-c", r.command], demux=True)
                stdout = (res[1][0] or b"") if res[0] == 0 else b""
                stderr = (res[1][1] or b"")
                out = (stdout + stderr).decode("utf-8", "replace").strip()
                status = "ok" if res[0] == 0 else "fail"
        except Exception as e:  # noqa: BLE001
            out = str(e)[:500]
        r.last_run = utcnow()
        r.last_status = status
        r.last_output = out[:2000]
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    finally:
        db.close()


def _tick() -> None:
    now = utcnow().replace(second=0, microsecond=0)
    minute_start = now
    db = SessionLocal()
    try:
        rows = db.query(InstanceCron).filter(InstanceCron.enabled == 1).all()
        for row in rows:
            if not cron_match(row.schedule, now):
                continue
            if row.last_run and row.last_run >= minute_start:
                continue  # 本分钟已触发过
            inst = db.get(Instance, row.instance_id)
            if not inst or _expired(inst, now):
                continue
            threading.Thread(target=_run_one, args=(row,), daemon=True).start()
    finally:
        db.close()


def _cleanup_pma() -> None:
    """清理过期的 phpMyAdmin 临时容器（label ppanel.pma-expire < now）。"""
    try:
        client = get_docker()
        now = time.time()
        for c in client.containers.list(
                all=True, filters={"label": "ppanel.managed=true"}):
            exp = (c.labels or {}).get("ppanel.pma-expire")
            if not exp:
                continue
            try:
                if float(exp) < now:
                    user = (c.labels or {}).get("ppanel.pma-user", "")
                    c.remove(force=True)
                    if user:
                        _drop_pma_user(user)
            except (ValueError, TypeError):
                continue
    except Exception:  # noqa: BLE001
        pass


def _drop_pma_user(user: str) -> None:
    """删除 pma 临时账号（root 凭证从 mysql_services 取）。"""
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_]{1,64}", user or ""):
        return
    from app.database import SessionLocal as _SL
    from app.models import MySqlService as _MS
    from app.services import mysql_service as _ms
    db = _SL()
    try:
        for svc_row in db.query(_MS).all():
            try:
                c = get_docker().containers.get(_ms.svc_name(svc_row.version))
                if c.status != "running":
                    continue
                c.exec_run(["sh", "-c",
                            f"mysql -uroot --password='{svc_row.root_password}' "
                            f"-e \"DROP USER IF EXISTS '{user}'@'%'\""])
            except Exception:  # noqa: BLE001
                continue
    finally:
        db.close()


def cron_loop() -> None:
    """agent lifespan 后台线程入口：任务调度 + pma 过期清理 + 管理员定时备份。"""
    while True:
        try:
            _tick()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        try:
            from app.services import host_backup_service
            host_backup_service.tick_all()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        try:
            _cleanup_pma()
        except Exception:  # noqa: BLE001
            pass
        try:
            time.sleep(_SCHED_WAIT)
        except KeyboardInterrupt:
            return
