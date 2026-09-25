"""独立面板扩展 API（/panel/instance/*）：
租户自助备份、定时任务、站点防护、phpMyAdmin、聚合概览、流量明细、
容器内命令执行、重建容器、Node 依赖管理（npm/pnpm）。

安全边界（核心）：
  - 备份/恢复的数据库固定为本实例登记库 ppanel_<iid>（表清单服务端 SHOW TABLES 枚举，
    备份表名必须是其子集，不可注入其他库/表）
  - 备份/恢复的容器固定为本实例容器 ppanel-<iid>
  - 备份文件列表/下载/删除按实例前缀过滤，跨实例文件不可见不可操作
鉴权：独立面板 JWT（Bearer / ?t=），同 panel.py。
"""
import io
import json
import os
import re
import shutil
import tarfile
import threading
import time
from datetime import datetime

import docker
import ipaddress
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import tasks
from app.database import get_db
from app.docker_client import get_docker, get_docker_long
from app.models import InstanceCron, InstanceSite
from app.routers.backup import (BACKUP_DIR, _NAME_RE, _PATH_RE, _StreamReader,
                                _TBL_RE, _check_container_dir,
                                _cleanup_in_container, _safe_path, _ts)
from app.routers.panel import _dep, _out
from app.services import caddy_service as caddy
from app.services import cron_service as cron
from app.services import instance_service as svc
from app.services import pma_service as pma
from app.services import mysql_service as mysql_svc

router = APIRouter()


# ---------- 私有：本实例的库 / 容器 / 文件边界 ----------

def _own_db(iid: int, db: Session):
    """实例登记库 + MySQL 服务行（未开通/未运行即报错）。"""
    from app.models import InstanceDb, MySqlService
    reg = db.query(InstanceDb).filter(InstanceDb.instance_id == iid).first()
    if not reg:
        raise HTTPException(status_code=400, detail="实例尚未开通数据库，请先在「数据库」页开通")
    svc_row = db.get(MySqlService, reg.version)
    if not svc_row:
        raise HTTPException(status_code=404, detail=f"MySQL {reg.version} 服务未启用")
    try:
        c = get_docker_long().containers.get(mysql_svc.svc_name(reg.version))
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="MySQL 容器不存在，请重新启用服务")
    if c.status != "running":
        raise HTTPException(status_code=502, detail=f"MySQL {reg.version} 服务未运行")
    return reg, svc_row, c


def _own_container(iid: int):
    try:
        c = get_docker_long().containers.get(f"ppanel-{iid}")
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="实例容器不存在或已被删除")
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")
    if c.status != "running":
        raise HTTPException(status_code=502, detail="实例容器未运行")
    return c


def _own_file(iid: int, file: str) -> str:
    """本实例备份文件全路径：必须匹配本实例前缀（隔离其他实例与节点文件）。"""
    path = _safe_path(file)
    ok = (file.startswith(f"ppanel-{iid}_") or file.startswith(f"ppanel_{iid}_"))
    if ok and file.endswith(".tar"):
        ok = False  # 容器整包备份仅管理员（节点面板）可用，租户只开放数据库/目录备份
    if not ok or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="备份文件不存在")
    return path


# ---------- 备份：信息 / 列表 / 下载 / 删除 ----------

@router.get("/panel/instance/backup/info")
def backup_info(inst=Depends(_dep), db: Session = Depends(get_db)):
    iid = inst.id
    out: dict = {"container_running": False, "db": None}
    try:
        c = get_docker().containers.get(f"ppanel-{iid}")
        out["container_running"] = c.status == "running"
    except (docker.errors.NotFound, docker.errors.APIError):
        pass
    from app.models import InstanceDb, MySqlService
    reg = db.query(InstanceDb).filter(InstanceDb.instance_id == iid).first()
    if reg:
        running, tables = False, []
        svc_row = db.get(MySqlService, reg.version)
        if svc_row:
            try:
                mc = get_docker().containers.get(mysql_svc.svc_name(reg.version))
                if mc.status == "running":
                    running = True
                    res = mc.exec_run(["sh", "-c",
                                       f"mysql -uroot --password='{svc_row.root_password}' "
                                       f"-N -e 'SHOW TABLES FROM `{reg.db_name}`'"], demux=True)
                    if res[0] == 0:
                        tables = [l.strip() for l in (res[1][0] or b"").decode(
                            "utf-8", "replace").splitlines() if l.strip()]
            except (docker.errors.NotFound, docker.errors.APIError):
                pass
        out["db"] = {"version": reg.version, "db_name": reg.db_name,
                     "running": running, "tables": tables}
    return out


@router.get("/panel/instance/backup/list")
def backup_list(inst=Depends(_dep)):
    iid = inst.id
    os.makedirs(BACKUP_DIR, exist_ok=True)
    items = []
    for name in os.listdir(BACKUP_DIR):
        if not (name.startswith(f"ppanel-{iid}_") or name.startswith(f"ppanel_{iid}_")):
            continue
        p = os.path.join(BACKUP_DIR, name)
        if not os.path.isfile(p):
            continue
        if name.endswith(".tar"):
            continue  # 管理员容器整包备份（节点面板功能），对租户隐藏
        kind = ("database" if name.endswith(".sql.gz")
                else "dir" if name.endswith(".tar.gz") else "container")
        st = os.stat(p)
        items.append({"file": name, "kind": kind, "size": st.st_size,
                      "created_at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return {"backups": items}


@router.get("/panel/instance/backup/download")
def backup_download(file: str = "", inst=Depends(_dep)):
    return FileResponse(_own_file(inst.id, file), filename=file)


@router.delete("/panel/instance/backup")
def backup_delete(file: str = "", inst=Depends(_dep)):
    p = _own_file(inst.id, file)
    os.remove(p)
    return {"detail": f"已删除 {file}"}


# ---------- 备份：数据库（表级，固定本库） ----------

class DbBackupIn(BaseModel):
    tables: list[str] = []


@router.post("/panel/instance/backup/db")
def backup_db(body: DbBackupIn, inst=Depends(_dep), db: Session = Depends(get_db)):
    iid = inst.id
    reg, svc_row, mc = _own_db(iid, db)
    # 表名必须来自 SHOW TABLES 结果（服务端枚举，杜绝任意表注入）
    res = mc.exec_run(["sh", "-c",
                       f"mysql -uroot --password='{svc_row.root_password}' "
                       f"-N -e 'SHOW TABLES FROM `{reg.db_name}`'"], demux=True)
    real = {l.strip() for l in (res[1][0] or b"").decode("utf-8", "replace").splitlines() if l.strip()}
    tables = [t.strip() for t in (body.tables or []) if t and t.strip()]
    for t in tables:
        if not _TBL_RE.fullmatch(t) or t not in real:
            raise HTTPException(status_code=400, detail=f"表不存在：{t}")
    tbl_part = (" " + " ".join(tables)) if tables else ""

    tmp_sql = f"/tmp/ppanel_bk_{_ts()}_{os.urandom(3).hex()}.sql"
    tmp_gz = tmp_sql + ".gz"
    fname = f"{reg.db_name}_{_ts()}.sql.gz"
    dest = os.path.join(BACKUP_DIR, fname)
    r2 = mc.exec_run(["sh", "-c",
                      f"mysqldump -uroot --password='{svc_row.root_password}' "
                      f"--single-transaction --routines --triggers {reg.db_name}{tbl_part} "
                      f"--result-file={tmp_sql} && gzip -f {tmp_sql}"])
    if r2.exit_code != 0:
        out = (r2.output or b"").decode("utf-8", "replace")
        lines = [l for l in out.splitlines() if "Using a password" not in l]
        raise HTTPException(status_code=502, detail=f"mysqldump 失败：{''.join(lines).strip()[-300:]}")
    try:
        stream, _stat = mc.get_archive(tmp_gz)
        with open(dest, "wb") as f:
            tf = tarfile.open(fileobj=_StreamReader(stream), mode="r|")
            for m in tf:
                if m.isfile():
                    shutil.copyfileobj(tf.extractfile(m), f, 1024 * 1024)
                    break
        return {"detail": f"数据库备份完成：{fname}", "file": fname}
    except Exception as e:  # noqa: BLE001
        if os.path.exists(dest):
            os.remove(dest)
        raise HTTPException(status_code=502, detail=f"取回备份失败：{e}")
    finally:
        _cleanup_in_container(mc, tmp_sql, tmp_gz)


# ---------- 备份：网站目录（固定本容器） ----------

class DirIn(BaseModel):
    path: str


@router.post("/panel/instance/backup/dir")
def backup_dir(body: DirIn, inst=Depends(_dep)):
    iid = inst.id
    c = _own_container(iid)
    p = body.path.strip().rstrip("/") or "/app"
    if ".." in p or not _PATH_RE.fullmatch(p):
        raise HTTPException(status_code=400, detail="目录路径无效（需为容器内绝对路径）")
    _check_container_dir(c, p)
    parent, base = p.rsplit("/", 1)
    tmp = f"/tmp/ppanel_dirbk_{_ts()}_{os.urandom(3).hex()}.tar.gz"
    fname = f"ppanel-{iid}_{base}_{_ts()}.tar.gz"
    if not _NAME_RE.fullmatch(fname):
        raise HTTPException(status_code=400, detail="目录名无法生成合法备份文件名")
    dest = os.path.join(BACKUP_DIR, fname)
    r = c.exec_run(["sh", "-c", f"tar -czf {tmp} -C '{p}' ."], demux=True)
    if r[0] != 0:
        raise HTTPException(status_code=502, detail="目录打包失败："
                            + (r[1][1] or b"").decode("utf-8", "replace").strip()[-200:])
    try:
        stream, _stat = c.get_archive(tmp)
        with open(dest, "wb") as f:
            tf = tarfile.open(fileobj=_StreamReader(stream), mode="r|")
            for m in tf:
                if m.isfile():
                    shutil.copyfileobj(tf.extractfile(m), f, 1024 * 1024)
                    break
        return {"detail": f"目录备份完成：{fname}", "file": fname}
    except Exception as e:  # noqa: BLE001
        if os.path.exists(dest):
            os.remove(dest)
        raise HTTPException(status_code=502, detail=f"取回备份失败：{e}")
    finally:
        c.exec_run(["sh", "-c", f"rm -f {tmp}"])


# ---------- 恢复：目录（固定本容器） / 数据库（固定本库） ----------

class DirRestoreIn(BaseModel):
    file: str
    path: str


@router.post("/panel/instance/backup/restore/dir")
def restore_dir(body: DirRestoreIn, inst=Depends(_dep)):
    iid = inst.id
    src = _own_file(iid, body.file)
    if not body.file.endswith(".tar.gz"):
        raise HTTPException(status_code=400, detail="目录备份必须为 .tar.gz 文件")
    c = _own_container(iid)
    p = body.path.strip().rstrip("/") or "/app"
    if ".." in p or not _PATH_RE.fullmatch(p):
        raise HTTPException(status_code=400, detail="目标目录无效（需为容器内绝对路径）")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo("restore_" + os.path.basename(body.file))
        ti.size = os.path.getsize(src)
        with open(src, "rb") as f:
            tf.addfile(ti, f)
    buf.seek(0)
    c.put_archive("/tmp", buf.getvalue())
    tmp = "/tmp/restore_" + os.path.basename(body.file)
    r = c.exec_run(["sh", "-c", f"mkdir -p '{p}' && tar -xzf {tmp} -C '{p}' && rm -f {tmp}"], demux=True)
    if r[0] != 0:
        c.exec_run(["sh", "-c", f"rm -f {tmp}"])
        raise HTTPException(status_code=502, detail="解压失败："
                            + (r[1][1] or b"").decode("utf-8", "replace").strip()[-200:])
    return {"detail": f"已恢复至容器 {p}"}


class DbRestoreIn(BaseModel):
    file: str


@router.post("/panel/instance/backup/restore/db")
def restore_db(body: DbRestoreIn, inst=Depends(_dep), db: Session = Depends(get_db)):
    iid = inst.id
    src = _own_file(iid, body.file)
    if not body.file.endswith(".sql.gz"):
        raise HTTPException(status_code=400, detail="数据库备份必须为 .sql.gz 文件")
    reg, svc_row, mc = _own_db(iid, db)
    tmp_gz = f"/tmp/ppanel_restore_{_ts()}_{os.urandom(3).hex()}.sql.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        with open(src, "rb") as f:
            ti = tarfile.TarInfo(os.path.basename(tmp_gz))
            ti.size = os.fstat(f.fileno()).st_size
            tf.addfile(ti, f)
    buf.seek(0)
    mc.put_archive("/tmp", buf.getvalue())
    try:
        r = mc.exec_run(["sh", "-c",
                         f"mysql -uroot --password='{svc_row.root_password}' "
                         f"-e 'CREATE DATABASE IF NOT EXISTS `{reg.db_name}`' "
                         f"&& gzip -t {tmp_gz} "
                         f"&& gunzip -c {tmp_gz} | "
                         f"mysql -uroot --password='{svc_row.root_password}' {reg.db_name}"])
        if r.exit_code != 0:
            out = (r.output or b"").decode("utf-8", "replace")
            lines = [l for l in out.splitlines() if "Using a password" not in l]
            raise HTTPException(status_code=502, detail=f"恢复失败：{''.join(lines).strip()[-300:]}")
        return {"detail": f"已恢复到数据库 {reg.db_name}（覆盖导入）"}
    finally:
        _cleanup_in_container(mc, tmp_gz)


# ---------- 定时任务 ----------

def _cron_out(r: InstanceCron) -> dict:
    return {"id": r.id, "schedule": r.schedule, "command": r.command,
            "enabled": bool(r.enabled),
            "last_run": r.last_run.strftime("%m-%d %H:%M") if r.last_run else "",
            "last_status": r.last_status or "",
            "last_output": (r.last_output or "")[-400:]}


class CronIn(BaseModel):
    schedule: str
    command: str


class CronPatch(BaseModel):
    schedule: str | None = None
    command: str | None = None
    enabled: bool | None = None


@router.get("/panel/instance/crons")
def crons_list(inst=Depends(_dep), db: Session = Depends(get_db)):
    rows = (db.query(InstanceCron).filter(InstanceCron.instance_id == inst.id)
            .order_by(InstanceCron.created_at).all())
    return {"crons": [_cron_out(r) for r in rows]}


@router.post("/panel/instance/crons")
def crons_create(body: CronIn, inst=Depends(_dep), db: Session = Depends(get_db)):
    err = cron.validate(body.schedule)
    if err:
        raise HTTPException(status_code=400, detail=f"cron 表达式无效：{err}")
    cmd = body.command.strip()
    if not cmd or len(cmd) > 512:
        raise HTTPException(status_code=400, detail="命令不能为空且不超过 512 字符")
    r = InstanceCron(instance_id=inst.id, schedule=body.schedule.strip(),
                     command=cmd, enabled=1)
    db.add(r)
    db.commit()
    return _cron_out(r)


@router.patch("/panel/instance/crons/{cid}")
def crons_patch(cid: int, body: CronPatch, inst=Depends(_dep), db: Session = Depends(get_db)):
    r = db.get(InstanceCron, cid)
    if not r or r.instance_id != inst.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    if body.schedule is not None:
        err = cron.validate(body.schedule)
        if err:
            raise HTTPException(status_code=400, detail=f"cron 表达式无效：{err}")
        r.schedule = body.schedule.strip()
    if body.command is not None:
        cmd = body.command.strip()
        if not cmd or len(cmd) > 512:
            raise HTTPException(status_code=400, detail="命令不能为空且不超过 512 字符")
        r.command = cmd
    if body.enabled is not None:
        r.enabled = 1 if body.enabled else 0
    db.commit()
    return _cron_out(r)


@router.delete("/panel/instance/crons/{cid}")
def crons_delete(cid: int, inst=Depends(_dep), db: Session = Depends(get_db)):
    r = db.get(InstanceCron, cid)
    if not r or r.instance_id != inst.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    db.delete(r)
    db.commit()
    return {"detail": "已删除"}


@router.post("/panel/instance/crons/{cid}/run")
def crons_run(cid: int, inst=Depends(_dep), db: Session = Depends(get_db)):
    r = db.get(InstanceCron, cid)
    if not r or r.instance_id != inst.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    threading.Thread(target=cron._run_one, args=(r,), daemon=True).start()
    return {"detail": "已触发执行，稍后刷新查看结果"}


# ---------- 站点防护（basic_auth / IP 黑白名单 / 静态托管） ----------

def _site_out(iid: int, db: Session, inst) -> dict:
    row = db.get(InstanceSite, iid)
    # 无记录时用显式默认值（column default 要 flush 后才落到属性上，直接构造是 None）
    cfg = row or InstanceSite(instance_id=iid, mode="proxy", static_dir="public_html")
    return {"mode": cfg.mode, "static_dir": cfg.static_dir,
            "auth_user": cfg.auth_user, "has_auth": bool(cfg.auth_hash),
            "ip_whitelist": cfg.ip_whitelist, "ip_blacklist": cfg.ip_blacklist,
            "domain": inst.domain or "", "caddy": caddy.caddy_info()}


class SiteIn(BaseModel):
    mode: str = "proxy"
    static_dir: str = "public_html"
    auth_user: str = ""
    auth_pass: str = ""
    ip_whitelist: str = ""
    ip_blacklist: str = ""


def _norm_ips(raw: str) -> str:
    toks = [t for t in (raw or "").replace(",", " ").split() if t]
    for t in toks:
        try:
            ipaddress.ip_network(t, strict=False)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"IP/CIDR 无效：{t}")
    return " ".join(toks)


@router.get("/panel/instance/site")
def site_get(inst=Depends(_dep), db: Session = Depends(get_db)):
    return _site_out(inst.id, db, inst)


@router.put("/panel/instance/site")
def site_put(body: SiteIn, inst=Depends(_dep), db: Session = Depends(get_db)):
    iid = inst.id
    if not inst.domain:
        raise HTTPException(status_code=400, detail="请先在「域名与 SSL」绑定域名")
    if body.mode not in ("proxy", "static"):
        raise HTTPException(status_code=400, detail="mode 仅支持 proxy/static")
    sub = body.static_dir.strip().strip("/")
    if body.mode == "static":
        if ".." in sub or not sub or len(sub) > 200 or "/" in sub:
            raise HTTPException(status_code=400,
                                detail="静态目录须为 /app 下单级目录名（如 public_html）")
        host_dir = os.path.join(inst.host_dir, sub)
        if not os.path.isdir(host_dir):
            raise HTTPException(status_code=400, detail=f"目录不存在：容器内 /app/{sub}")
    wl, bl = _norm_ips(body.ip_whitelist), _norm_ips(body.ip_blacklist)
    if wl and bl:
        raise HTTPException(status_code=400, detail="白名单与黑名单二选一")

    row = db.get(InstanceSite, iid)
    if not row:
        row = InstanceSite(instance_id=iid)
        db.add(row)
    row.mode = body.mode
    row.static_dir = sub or "public_html"
    au = body.auth_user.strip()
    if au and body.auth_pass:
        row.auth_user = au
        row.auth_hash = caddy.hash_password(body.auth_pass)
    elif not au:
        row.auth_user, row.auth_hash = "", ""   # 清空即关闭密码保护
    elif au and not body.auth_pass and not row.auth_hash:
        raise HTTPException(status_code=400, detail="设置了用户名则必须提供密码")
    elif au:
        row.auth_user = au                       # 只改用户名，密码沿用
    row.ip_whitelist, row.ip_blacklist = wl, bl
    row.updated_at = datetime.utcnow()
    db.commit()
    try:
        caddy.write_site(iid, inst.domain, inst.ext_port, {
            "mode": row.mode, "static_dir": row.static_dir,
            "app_root": inst.host_dir,   # 容器 /app 的宿主机挂载源（静态托管 root）
            "auth_user": row.auth_user, "auth_hash": row.auth_hash,
            "ip_whitelist": row.ip_whitelist, "ip_blacklist": row.ip_blacklist})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Caddy 配置写入失败：{e}")
    return _site_out(iid, db, inst)


# ---------- phpMyAdmin ----------

@router.post("/panel/instance/pma/enable")
def pma_enable(request: Request, inst=Depends(_dep), db: Session = Depends(get_db)):
    return pma.enable(inst, request, db)


@router.post("/panel/instance/pma/disable")
def pma_disable(inst=Depends(_dep), db: Session = Depends(get_db)):
    return pma.disable(inst.id, db)


# ---------- 聚合概览（一次调用取全量，供外部系统/仪表盘集成） ----------

@router.get("/panel/instance/overview")
def overview(inst=Depends(_dep), db: Session = Depends(get_db)):
    """实例聚合概览：基础信息 + 资源/流量限额 + 数据库 + 站点/Caddy + PMA + 计数。"""
    from app.models import InstanceDb

    iid = inst.id
    svc.sync_status(inst)
    db.commit()

    reg = db.query(InstanceDb).filter(InstanceDb.instance_id == iid).first()
    database = ({"version": reg.version, "db_name": reg.db_name, "db_user": reg.db_user}
                if reg else None)

    s = _site_out(iid, db, inst)
    site = {"mode": s["mode"], "domain": s["domain"], "caddy": s["caddy"]}

    pma_state = {"active": False, "port": None}
    try:
        pc = get_docker().containers.get(f"ppanel-pma-{iid}")
        if pc.status == "running":
            pma_state["active"] = True
            ports = (pc.attrs.get("NetworkSettings", {}).get("Ports", {})
                     .get("80/tcp")) or []
            if ports:
                pma_state["port"] = int(ports[0].get("HostPort") or 0) or None
    except (docker.errors.NotFound, docker.errors.APIError):
        pass

    try:
        n_backups = len([f for f in os.listdir(BACKUP_DIR)
                         if f.startswith(f"ppanel-{iid}_") or f.startswith(f"ppanel_{iid}_")])
    except OSError:
        n_backups = 0
    n_crons = db.query(InstanceCron).filter(InstanceCron.instance_id == iid).count()

    return {"instance": _out(inst), "database": database, "site": site,
            "pma": pma_state, "counts": {"backups": n_backups, "crons": n_crons}}


# ---------- 流量明细 ----------

@router.get("/panel/instance/traffic")
def traffic(inst=Depends(_dep)):
    """本期流量用量：限额/已用/剩余/百分比（自然月重置；limit 空=不限）。"""
    gb = getattr(inst, "traffic_gb", None)
    used = round(getattr(inst, "traffic_used_mb", 0.0) or 0.0, 1)
    remaining = None if gb is None else max(0.0, round(gb * 1024 - used, 1))
    pct = (min(100.0, round(used / (gb * 1024) * 100, 1))
           if gb and gb > 0 else None)
    return {"limit_gb": gb, "used_mb": used, "remaining_mb": remaining,
            "used_percent": pct, "month": getattr(inst, "traffic_month", "")}


# ---------- 容器内命令执行（自动化集成用） ----------

class ExecIn(BaseModel):
    command: str = Field(min_length=1, max_length=4096)
    timeout: int = Field(default=30, ge=1, le=120)


@router.post("/panel/instance/exec")
def exec_cmd(body: ExecIn, inst=Depends(_dep), db: Session = Depends(get_db)):
    """在自身容器内执行一次性命令，返回退出码与输出（各流截断 64KB）。

    权限不高于文件接口（租户本就可上传可执行代码），仅提供便捷自动化通道；
    每次调用记入操作日志。
    """
    c = _own_container(inst.id)
    result: dict = {}

    def _run():
        try:
            code, out = c.exec_run(["/bin/sh", "-c", body.command], demux=True)
            result["exit_code"] = code
            result["stdout"] = (out[0] or b"")[:65536].decode("utf-8", "replace")
            result["stderr"] = (out[1] or b"")[:65536].decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            result["error"] = str(e)[:300]

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(body.timeout)
    if th.is_alive():
        return {"timed_out": True,
                "detail": f"命令超过 {body.timeout}s 未结束，已放弃等待（进程仍在容器内运行）"}
    if "error" in result:
        raise HTTPException(status_code=502, detail=f"执行失败：{result['error']}")
    svc.log_op(db, inst.id, "exec", detail=body.command[:200])
    db.commit()
    return {"timed_out": False, **result}


# ---------- 重建容器（自救：容器损坏/误删后按原镜像重建，/app 数据保留） ----------

@router.post("/panel/instance/rebuild")
def rebuild(inst=Depends(_dep), db: Session = Depends(get_db)):
    """重建容器：镜像与启动命令不变，数据目录 /app 与外部端口保留。

    运行中需先停止；重建后自动启动并做启动诊断。
    """
    svc.recreate_container(inst)   # running 时内部抛 409
    svc.start_instance(inst)
    svc.diagnose_startup(inst)
    svc.sync_status(inst)
    svc.log_op(db, inst.id, "rebuild")
    db.commit()
    return {"status": inst.status, "container_id": inst.container_id}


# ---------- Node 依赖管理（npm / pnpm；已装检测读宿主机 node_modules，零 exec） ----------

NODE_PKG_CATALOG = [
    {"name": "express", "type": "Web 框架", "desc": "最流行的 Node Web 框架"},
    {"name": "koa", "type": "Web 框架", "desc": "轻量优雅的 Web 框架"},
    {"name": "fastify", "type": "Web 框架", "desc": "高性能 Web 框架"},
    {"name": "@nestjs/core", "type": "Web 框架", "desc": "企业级 NestJS 框架核心"},
    {"name": "socket.io", "type": "实时通信", "desc": "WebSocket 实时通信库"},
    {"name": "ws", "type": "实时通信", "desc": "轻量 WebSocket 客户端/服务端"},
    {"name": "axios", "type": "HTTP", "desc": "最常用的 HTTP 请求库"},
    {"name": "mysql2", "type": "数据库", "desc": "MySQL 驱动（配合共享 MySQL）"},
    {"name": "mongoose", "type": "数据库", "desc": "MongoDB ODM"},
    {"name": "ioredis", "type": "数据库", "desc": "Redis 客户端"},
    {"name": "sequelize", "type": "数据库", "desc": "多数据库 ORM"},
    {"name": "typeorm", "type": "数据库", "desc": "多数据库 ORM（装饰器风格）"},
    {"name": "@prisma/client", "type": "数据库", "desc": "Prisma ORM 客户端"},
    {"name": "jsonwebtoken", "type": "认证", "desc": "JWT 签发与校验"},
    {"name": "multer", "type": "文件", "desc": "Express 文件上传中间件"},
    {"name": "dayjs", "type": "工具", "desc": "轻量日期处理库"},
    {"name": "lodash", "type": "工具", "desc": "常用工具函数集"},
    {"name": "zod", "type": "工具", "desc": "数据校验与类型推导"},
    {"name": "dotenv", "type": "工具", "desc": "读取 .env 环境变量文件"},
    {"name": "chalk", "type": "工具", "desc": "终端彩色输出"},
]

# npm 包名 + 可选版本区间（@scope/name@^1.2.3）
_NPM_NAME_RE = re.compile(
    r"^(@[A-Za-z0-9._\-]+/)?[A-Za-z0-9._\-]+(@[A-Za-z0-9._\-^~><=*,.+]+)?$")
_NPM_MIRRORS = [
    {"name": "淘宝源", "url": "https://registry.npmmirror.com"},
    {"name": "腾讯云", "url": "http://mirrors.cloud.tencent.com/npm/"},
    {"name": "华为云", "url": "https://repo.huaweicloud.com/repository/npm/"},
    {"name": "官方源", "url": "https://registry.npmjs.org"},
]
_NPM_OFFICIAL = "https://registry.npmjs.org"
_NPM_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-_:]+(:\d{2,5})?(/[A-Za-z0-9.\-_/]*)?$")


def _require_node(inst) -> None:
    if svc.runtime_kind(inst.image) != "node":
        raise HTTPException(status_code=400, detail="该功能仅支持 Node 环境")


def _node_container(inst):
    """运行中的实例容器（未运行抛 409）。"""
    client = get_docker()
    try:
        c = client.containers.get(svc.container_name(inst.id))
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=409, detail="实例未运行，请先启动实例")
    if c.status != "running":
        raise HTTPException(status_code=409, detail="实例未运行，请先启动实例")
    return client, c


def _node_exec(client, cid: str, script: str) -> str:
    exec_id = client.api.exec_create(cid, ["sh", "-c", script], workdir="/app")["Id"]
    out, _err = client.api.exec_start(exec_id, demux=True)
    return (out or b"").decode("utf-8", "replace")


def _npm_registry_of(client, cid: str) -> str:
    try:
        out = _node_exec(client, cid, "npm config get registry 2>/dev/null").strip()
        return out if out.startswith("http") else _NPM_OFFICIAL
    except Exception:  # noqa: BLE001
        return _NPM_OFFICIAL


def _pkg_json_version(host_dir: str, name: str):
    """从宿主机 node_modules 读已装版本（容器未运行也能检测）。"""
    pj = os.path.join(host_dir, "node_modules", *name.split("/"), "package.json")
    if not os.path.isfile(pj):
        return None
    try:
        return json.load(open(pj, encoding="utf-8")).get("version", "")
    except Exception:  # noqa: BLE001
        return ""


@router.get("/panel/instance/node/packages")
def node_packages(dep=Depends(_dep)):
    """Node 依赖页：候选目录（已装三态）+ 全量已装列表 + package.json 存在性。"""
    _require_node(dep)
    nm = os.path.join(dep.host_dir, "node_modules")
    installed: list = []
    if os.path.isdir(nm):
        for entry in os.listdir(nm):
            if entry.startswith("."):
                continue
            full = os.path.join(nm, entry)
            scopes = ([(f"{entry}/{sub}", os.path.join(full, sub))
                       for sub in os.listdir(full)]
                      if entry.startswith("@") and os.path.isdir(full)
                      else [(entry, full)])
            for name, d in scopes:
                ver = _pkg_json_version(dep.host_dir, name)
                if ver is not None:
                    installed.append({"name": name, "version": ver})
    installed.sort(key=lambda x: x["name"])
    running = False
    try:
        running = get_docker().containers.get(svc.container_name(dep.id)).status == "running"
    except Exception:  # noqa: BLE001
        pass
    catalog = [{**it, "installed": _pkg_json_version(dep.host_dir, it["name"])}
               for it in NODE_PKG_CATALOG]
    return {"running": running,
            "has_package_json": os.path.isfile(os.path.join(dep.host_dir, "package.json")),
            "catalog": catalog, "installed": installed}


def _node_names(raw: str) -> list:
    names = [n for n in str(raw or "").replace(",", " ").split() if n]
    if not names:
        raise HTTPException(status_code=400, detail="请输入依赖名称")
    for n in names:
        if not _NPM_NAME_RE.match(n):
            raise HTTPException(status_code=400, detail=f"非法的依赖名称：{n}")
    return names


def _node_job(inst, desc: str, script: str) -> dict:
    if svc.has_running_job(inst.id):
        raise HTTPException(status_code=409, detail="已有任务正在执行，请等待完成后再试")
    job = svc.start_exec(inst, desc, ["sh", "-c", script])
    return {"job_id": job.id}


@router.post("/panel/instance/node/deps/install-pkgs")
def node_install_pkgs(body: dict, dep=Depends(_dep), db: Session = Depends(get_db)):
    """npm/pnpm 在线安装（支持 @scope/name@version），实时日志走 ExecJob WS。"""
    _require_node(dep)
    body = body or {}
    manager = body.get("manager") or "npm"
    if manager not in ("npm", "pnpm"):
        raise HTTPException(status_code=400, detail="包管理器仅支持 npm / pnpm")
    names = _node_names(body.get("names"))
    client, c = _node_container(dep)
    reg = _npm_registry_of(client, c.id)
    reg_flag = f" --registry={reg}" if reg != _NPM_OFFICIAL else ""
    if manager == "pnpm":
        script = (f"command -v pnpm >/dev/null || npm install -g pnpm{reg_flag}\n"
                  f"pnpm add {' '.join(names)}{reg_flag}")
    else:
        script = f"npm install {' '.join(names)}{reg_flag}"
    r = _node_job(dep, f"{manager} install {' '.join(names)}", script)
    svc.log_op(db, dep.id, "node_deps_install", detail=" ".join(names))
    db.commit()
    return r


@router.post("/panel/instance/node/deps/install")
def node_install_from_pkg(body: dict, dep=Depends(_dep), db: Session = Depends(get_db)):
    """按实例根目录 package.json 安装全部依赖（npm install / pnpm install）。"""
    _require_node(dep)
    body = body or {}
    manager = body.get("manager") or "npm"
    if manager not in ("npm", "pnpm"):
        raise HTTPException(status_code=400, detail="包管理器仅支持 npm / pnpm")
    if not os.path.isfile(os.path.join(dep.host_dir, "package.json")):
        raise HTTPException(status_code=400, detail="实例根目录没有 package.json（先在文件管理上传）")
    client, c = _node_container(dep)
    reg = _npm_registry_of(client, c.id)
    reg_flag = f" --registry={reg}" if reg != _NPM_OFFICIAL else ""
    if manager == "pnpm":
        script = (f"command -v pnpm >/dev/null || npm install -g pnpm{reg_flag}\n"
                  f"pnpm install{reg_flag}")
    else:
        script = f"npm install{reg_flag}"
    r = _node_job(dep, f"{manager} install", script)
    svc.log_op(db, dep.id, "node_deps_install", detail="package.json")
    db.commit()
    return r


@router.post("/panel/instance/node/deps/uninstall")
def node_uninstall_pkgs(body: dict, dep=Depends(_dep), db: Session = Depends(get_db)):
    """卸载依赖（npm uninstall / pnpm remove）。"""
    _require_node(dep)
    body = body or {}
    manager = body.get("manager") or "npm"
    if manager not in ("npm", "pnpm"):
        raise HTTPException(status_code=400, detail="包管理器仅支持 npm / pnpm")
    names = _node_names(body.get("names"))
    client, c = _node_container(dep)
    if manager == "pnpm":
        script = (f"command -v pnpm >/dev/null || npm install -g pnpm\n"
                  f"pnpm remove {' '.join(names)}")
    else:
        script = f"npm uninstall {' '.join(names)}"
    r = _node_job(dep, f"{manager} remove {' '.join(names)}", script)
    svc.log_op(db, dep.id, "node_deps_uninstall", detail=" ".join(names))
    db.commit()
    return r


@router.get("/panel/instance/npm/mirror")
def npm_mirror_get(dep=Depends(_dep)):
    """npm registry 镜像源（pnpm 同样受其影响——执行时显式带上 --registry）。"""
    _require_node(dep)
    try:
        _client, c = _node_container(dep)
    except HTTPException:
        return {"running": False, "current": None, "mirrors": _NPM_MIRRORS}
    return {"running": True, "current": _npm_registry_of(_client, c.id),
            "mirrors": _NPM_MIRRORS}


@router.put("/panel/instance/npm/mirror")
def npm_mirror_put(body: dict, dep=Depends(_dep), db: Session = Depends(get_db)):
    """设置 npm registry（写容器 /root/.npmrc；重建容器后恢复默认，可重设）。"""
    _require_node(dep)
    url = str((body or {}).get("url", "")).strip()
    if not _NPM_URL_RE.match(url):
        raise HTTPException(status_code=400, detail="镜像地址格式不正确（http/https 链接）")
    _client, c = _node_container(dep)
    try:
        _node_exec(_client, c.id, f"npm config set registry {url}")
        named = next((m["name"] for m in _NPM_MIRRORS if m["url"] == url), url)
        svc.log_op(db, dep.id, "npm_mirror", detail=named)
        db.commit()
        return {"current": url}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"设置镜像失败：{e}")


# ---------- Go 依赖管理（go mod；Go 依赖声明在 go.mod，无"逐包安装"概念） ----------

_GO_PROXIES = [
    {"name": "七牛 goproxy.cn", "url": "https://goproxy.cn,direct"},
    {"name": "goproxy.io", "url": "https://goproxy.io,direct"},
    {"name": "阿里云", "url": "https://mirrors.aliyun.com/goproxy/,direct"},
    {"name": "官方源", "url": "https://proxy.golang.org,direct"},
]
_GO_DEFAULT_PROXY = "https://proxy.golang.org,direct"
_GO_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-_:]+(:\d{2,5})?(/[A-Za-z0-9.\-_/]*)?(,direct)?$")


def _require_go(inst) -> None:
    if svc.runtime_kind(inst.image) != "go":
        raise HTTPException(status_code=400, detail="该功能仅支持 Go 环境")


def _go_mod_path(inst):
    return os.path.join(inst.host_dir, "go.mod")


def _parse_go_mod(path: str) -> dict | None:
    """解析 go.mod：module 名 / go 版本 / require 依赖列表（无文件返回 None）。"""
    if not os.path.isfile(path):
        return None
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except Exception:  # noqa: BLE001
        return None
    module, go_ver = "", ""
    requires: list = []
    in_block = False
    for ln in lines:
        s = ln.strip()
        if s.startswith("//") or not s:
            continue
        if s.startswith("module "):
            module = s.split(None, 1)[1].strip()
        elif s.startswith("go "):
            go_ver = s.split(None, 1)[1].strip()
        elif s.startswith("require ("):
            in_block = True
        elif in_block and s == ")":
            in_block = False
        elif in_block:
            parts = s.split()
            if len(parts) >= 2:
                requires.append({"name": parts[0], "version": parts[1],
                                 "indirect": "// indirect" in ln})
        elif s.startswith("require "):
            parts = s.split()
            if len(parts) >= 3:
                requires.append({"name": parts[1], "version": parts[2],
                                 "indirect": "// indirect" in ln})
    return {"module": module, "go": go_ver, "requires": requires}


@router.get("/panel/instance/go/mod")
def go_mod_get(dep=Depends(_dep)):
    """Go 依赖页：go.mod 解析（module/go 版本/require 列表）+ 运行状态。"""
    _require_go(dep)
    data = _parse_go_mod(_go_mod_path(dep))
    running = False
    try:
        running = get_docker().containers.get(svc.container_name(dep.id)).status == "running"
    except Exception:  # noqa: BLE001
        pass
    return {"running": running, "go_mod": data,
            "proxies": _GO_PROXIES}


@router.post("/panel/instance/go/mod/tidy")
def go_mod_tidy(dep=Depends(_dep), db: Session = Depends(get_db)):
    """同步依赖：go mod tidy（无 go.mod 时自动 go mod init），实时日志走 ExecJob WS。"""
    _require_go(dep)
    _client, c = _node_container(dep)
    script = ("[ -f go.mod ] || go mod init ppanel-app\n"
              "go mod tidy")
    if svc.has_running_job(dep.id):
        raise HTTPException(status_code=409, detail="已有任务正在执行，请等待完成后再试")
    job = svc.start_exec(dep, "go mod tidy", ["sh", "-c", script])
    svc.log_op(db, dep.id, "go_mod_tidy")
    db.commit()
    return {"job_id": job.id}


@router.get("/panel/instance/go/proxy")
def go_proxy_get(dep=Depends(_dep)):
    """GOPROXY 模块代理（编译/拉依赖时生效）。"""
    _require_go(dep)
    try:
        _client, c = _node_container(dep)
    except HTTPException:
        return {"running": False, "current": None, "proxies": _GO_PROXIES}
    try:
        out = _node_exec(_client, c.id, "go env GOPROXY 2>/dev/null").strip()
        current = out if out.startswith("http") else _GO_DEFAULT_PROXY
    except Exception:  # noqa: BLE001
        current = _GO_DEFAULT_PROXY
    return {"running": True, "current": current, "proxies": _GO_PROXIES}


@router.put("/panel/instance/go/proxy")
def go_proxy_put(body: dict, dep=Depends(_dep), db: Session = Depends(get_db)):
    """设置 GOPROXY（go env -w 写容器层配置；重建容器后恢复默认，可重设）。"""
    _require_go(dep)
    url = str((body or {}).get("url", "")).strip()
    if not _GO_URL_RE.match(url):
        raise HTTPException(status_code=400, detail="代理地址格式不正确（https 链接，可带 ,direct 后缀）")
    _client, c = _node_container(dep)
    try:
        _node_exec(_client, c.id, f"go env -w GOPROXY={url}")
        named = next((m["name"] for m in _GO_PROXIES if m["url"] == url), url)
        svc.log_op(db, dep.id, "go_proxy", detail=named)
        db.commit()
        return {"current": url}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"设置代理失败：{e}")
