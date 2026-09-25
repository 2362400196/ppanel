"""节点管理员定时备份：cron 调度，任意容器 / 容器目录 / 数据库（整库或表级）。

执行直接复用 backup.py 的备份路由函数（_tid(None) 有保护，request=None 即无任务
日志）；HTTPException 原样抛出，由 run_job 捕获写回 last_status/last_output。
调度匹配复用 cron_service.cron_match；接入 cron_service.cron_loop 每轮 tick。
"""
import threading
from datetime import datetime

from sqlalchemy.orm import Session as OrmSession

from app.database import SessionLocal
from app.models import HostBackupJob, utcnow
from app.services import cron_service


def _perform(job: HostBackupJob, request) -> dict:
    """执行一次备份，返回备份路由的结果 dict（file/detail）。request 传 None 或假对象。"""
    from app.routers import backup as bk
    from app.routers.backup import ContainerIn, DbIn, DirIn

    if job.kind == "container":
        return bk.backup_container(ContainerIn(container_id=job.target), request)
    if job.kind == "dir":
        return bk.backup_dir(DirIn(container_id=job.target, path=job.dir_path or "/app"), request)
    ver, _, dbname = job.target.partition(":")
    tables = [job.table_name] if (job.kind == "db_table" and job.table_name) else []
    db = SessionLocal()
    try:
        return bk.backup_database(DbIn(version=ver, db_name=dbname, tables=tables), db, request)
    finally:
        db.close()


def _apply_keep(job: HostBackupJob) -> int:
    """只保留本任务最近 keep 份备份，返回删除数量。

    按任务的产出模式精确筛选（表级备份文件名带表名，与整库互不干扰）。"""
    import os
    import re as _re
    from app.routers.backup import BACKUP_DIR

    try:
        keep = max(1, int(job.keep or 5))
    except (TypeError, ValueError):
        keep = 5
    ts = r"\d{8}_\d{6}"
    if job.kind == "container":
        pat = _re.compile(_re.escape(job.target) + rf"_{ts}\.tar$")
    elif job.kind == "dir":
        base = (job.dir_path or "/app").strip().rstrip("/").rsplit("/", 1)[-1] or "app"
        pat = _re.compile(_re.escape(job.target) + "_" + _re.escape(base) + rf"_{ts}\.tar\.gz$")
    else:
        dbname = job.target.partition(":")[2]
        if job.kind == "db_full":
            pat = _re.compile(_re.escape(dbname) + rf"_{ts}\.sql\.gz$")
        else:
            pat = _re.compile(_re.escape(dbname) + "_" + _re.escape(job.table_name)
                              + rf"(_m)?_{ts}\.sql\.gz$")
    try:
        files = [f for f in os.listdir(BACKUP_DIR)
                 if pat.fullmatch(f) and os.path.isfile(os.path.join(BACKUP_DIR, f))]
    except OSError:
        return 0
    files.sort(key=lambda f: os.path.getmtime(os.path.join(BACKUP_DIR, f)), reverse=True)
    removed = 0
    for old in files[keep:]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
            removed += 1
        except OSError:
            pass
    return removed


def run_job(job_id: int, request=None) -> dict:
    """执行一个定时备份任务（调度与「立即执行」共用），写回最近执行状态。

    dest=cos/both 时备份落盘后上传腾讯云 COS；dest=cos 且配置不保留本地副本时
    上传成功即删除本地文件。上传失败整个任务记 fail（备份文件仍在本地可手动补传）。"""
    db: OrmSession = SessionLocal()
    try:
        job = db.get(HostBackupJob, job_id)
        if not job:
            return {"ok": False, "error": "任务不存在"}
        job.last_status, job.last_output = "running", ""
        db.commit()
        t0 = datetime.now()
        try:
            r = _perform(job, request)
            file = r.get("file", "")
            extra = ""
            if job.dest in ("cos", "both") and file:
                import os
                from app.services import cos_service
                cfg = cos_service.get_config()
                if not cfg.enabled:
                    raise ValueError("腾讯云 COS 未启用（存储设置中已关闭），上传中止")
                up = cos_service.upload(cfg, file)
                extra = f"；已上传 COS（{up['key']}，{up['size']}B）"
                if job.dest == "cos" and not cfg.keep_local:
                    try:
                        os.remove(file)
                        extra += "，本地副本已按配置清理"
                    except OSError:
                        pass
            removed = _apply_keep(job)
            job.last_status = "ok"
            size = r.get("detail", "")
            job.last_output = (f"{size}{extra}"
                               + (f"；已按保留策略清理 {removed} 份旧备份" if removed else "")
                               + f"（耗时 {(datetime.now() - t0).total_seconds():.1f}s）")
            return {"ok": True, "file": file, "detail": job.last_output}
        except Exception as e:  # noqa: BLE001
            job.last_status = "fail"
            job.last_output = str(e)[:2000]
            return {"ok": False, "error": str(e)[:500]}
        finally:
            job.last_run = utcnow()
            db.commit()
    finally:
        db.close()


def _spawn(job_id: int) -> None:
    threading.Thread(target=run_job, args=(job_id,), daemon=True,
                     name=f"hostbk-{job_id}").start()


def tick_all() -> None:
    """cron_loop 每轮调用：命中调度点的任务开线程执行（last_run 去重）。"""
    now = utcnow()
    db: OrmSession = SessionLocal()
    try:
        for job in db.query(HostBackupJob).filter(HostBackupJob.enabled == 1).all():
            if not cron_service.cron_match(job.schedule, now):
                continue
            if job.last_run and job.last_run >= now.replace(second=0, microsecond=0):
                continue  # 同一分钟内已触发过
            _spawn(job.id)
    finally:
        db.close()
