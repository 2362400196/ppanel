"""节点备份 API（/agent/host/backups/*）：容器整体导出 + 实例数据库导出/恢复。

备份文件统一落 /opt/ppanel/backups/（自动创建）：
  容器：  <容器名>_<时间戳>.tar     docker export 文件系统整体导出（流式落盘）
  数据库：<库名>_<时间戳>.sql.gz    mysqldump --single-transaction 管道 gzip
  目录：  <容器名>_<目录名>_<时间戳>.tar.gz   容器内 tar 打包指定目录
恢复：
  容器：  tar → docker import 为镜像（注入 sleep 入口）→ 创建新容器（找回数据用）
  数据库：sql.gz → put_archive 进 MySQL 容器 → CREATE IF NOT EXISTS + 导入（覆盖目标库）
定时备份：/host/backup-jobs（管理员 cron 任务，调度在 host_backup_service）。
鉴权与 security 一致：X-Node-Token（主控）或 X-API-Key（开放对接）任一即可。
"""
import io
import os
import re as _re
import shutil
import tarfile
import threading
import time
from datetime import datetime

import docker
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import tasks
from app.agent_auth import require_node_or_api
from app.database import SessionLocal, get_db
from app.docker_client import get_docker, get_docker_long
from app.models import InstanceDb, MySqlService
from app.services import mysql_service as mysql_svc

router = APIRouter(dependencies=[Depends(require_node_or_api)])

BACKUP_DIR = "/opt/ppanel/backups"

# 备份文件名白名单：字母数字开头，仅允许 _ . -（download/delete 防路径穿越）
_NAME_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
# 库名白名单（进入 mysqldump shell 命令，必须严格校验）
_DB_RE = _re.compile(r"^[A-Za-z0-9_]{1,64}$")
# Docker 容器名白名单
_CNAME_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# 表名白名单（mysqldump 命令行拼接，必须严格校验；$ 允许用于临时表）
_TBL_RE = _re.compile(r"^[A-Za-z0-9_$]{1,64}$")
# 容器内目录路径白名单：绝对路径、多段、无 ..（进 shell 命令，严格校验）
_PATH_RE = _re.compile(r"^(/[A-Za-z0-9_.-]+)+$")


def _safe_path(file: str) -> str:
    name = file or ""
    if not _NAME_RE.fullmatch(name) or ".." in name:
        raise HTTPException(status_code=400, detail="文件名无效")
    return os.path.join(BACKUP_DIR, name)


def _ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _tid(request: Request) -> str:
    """前端带 X-Task-Id 头时注册任务（终端浮层轮询展示），否则返回空串。"""
    tid = (request.headers.get("x-task-id") if request else "") or ""
    return tid


class ContainerIn(BaseModel):
    container_id: str


class DbIn(BaseModel):
    version: str
    db_name: str
    tables: list[str] = []   # 表级备份：只导出这些表；空 = 整库


# ---------- 备份列表 / 下载 / 删除 ----------

@router.get("/host/backups")
def list_backups(page: int = 0, page_size: int = 20):
    """备份文件列表（按时间倒序）。带 page 参数返回 {backups(当前页), total, dir}；
    不带则返回全量（旧调用兼容）。主控经 host_proxy 原样透传分页参数。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    items = []
    for name in os.listdir(BACKUP_DIR):
        p = os.path.join(BACKUP_DIR, name)
        if not os.path.isfile(p):
            continue
        kind = ("database" if name.endswith(".sql.gz")
                else "dir" if name.endswith(".tar.gz")
                else "container" if name.endswith(".tar") else "other")
        st = os.stat(p)
        items.append({
            "file": name, "kind": kind, "size": st.st_size,
            "created_at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    items.sort(key=lambda x: x["created_at"], reverse=True)
    if page >= 1:
        start = (page - 1) * page_size
        return {"backups": items[start:start + page_size], "total": len(items),
                "dir": BACKUP_DIR}
    return {"backups": items, "dir": BACKUP_DIR}


@router.get("/host/backups/download")
def download_backup(file: str = ""):
    target = _safe_path(file)
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="备份文件不存在")
    return FileResponse(target, filename=file)


@router.delete("/host/backups")
def delete_backup(file: str = ""):
    target = _safe_path(file)
    if os.path.isfile(target):
        os.remove(target)
        return {"detail": f"已删除 {file}"}
    raise HTTPException(status_code=404, detail="备份文件不存在")


class DirIn(BaseModel):
    container_id: str
    path: str


class DirRestoreIn(BaseModel):
    file: str
    container_id: str
    path: str


def _check_container_dir(c, p: str) -> None:
    """校验容器内目录存在；p 已过 _PATH_RE（无 shell 元字符）。"""
    chk = c.exec_run(["sh", "-c", f"test -d '{p}' && echo OK"], demux=True)
    if chk[0] != 0 or (chk[1][0] or b"").strip() != b"OK":
        raise HTTPException(status_code=404, detail=f"容器内目录 {p} 不存在")


@router.get("/host/backups/tables")
def list_tables(version: str = "", db_name: str = "", db: Session = Depends(get_db)):
    """列出库内所有表（前端表级备份多选用）。"""
    if not _DB_RE.fullmatch(db_name or ""):
        raise HTTPException(status_code=400, detail="库名格式无效")
    svc_row = db.get(MySqlService, version or "")
    if not svc_row:
        raise HTTPException(status_code=404, detail=f"MySQL {version} 服务未启用")
    try:
        c = get_docker_long().containers.get(mysql_svc.svc_name(version))
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="MySQL 容器不存在，请重新启用服务")
    if c.status != "running":
        raise HTTPException(status_code=502, detail=f"MySQL {version} 服务未运行")
    res = c.exec_run(["sh", "-c",
                      f"mysql -uroot --password='{svc_row.root_password}' -N -e 'SHOW TABLES FROM {db_name}'"],
                     demux=True)
    if res[0] != 0:
        raise HTTPException(status_code=404, detail=f"库 {db_name} 不存在或服务异常")
    tables = [l.strip() for l in (res[1][0] or b"").decode("utf-8", "replace").splitlines() if l.strip()]
    return {"tables": tables}


@router.post("/host/backups/dir")
def backup_dir(body: DirIn, request: Request = None):
    """网站目录备份：容器内 tar.gz 打包指定目录后取回落盘，体积远小于整容器 export。"""
    tid = _tid(request)
    cname = body.container_id
    if not _CNAME_RE.fullmatch(cname):
        raise HTTPException(status_code=400, detail="容器名格式无效")
    p = body.path.strip().rstrip("/")
    if ".." in p or not _PATH_RE.fullmatch(p):
        raise HTTPException(status_code=400, detail="目录路径无效（需为容器内绝对路径）")
    try:
        c = get_docker_long().containers.get(cname)
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="容器不存在或已被删除")
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")
    if c.status != "running":
        raise HTTPException(status_code=502, detail="容器未运行，无法备份目录")
    _check_container_dir(c, p)

    parent, base = p.rsplit("/", 1)
    tmp = f"/tmp/ppanel_dirbk_{_ts()}_{os.urandom(3).hex()}.tar.gz"
    fname = f"{cname}_{base}_{_ts()}.tar.gz"
    if not _NAME_RE.fullmatch(fname):
        raise HTTPException(status_code=400, detail="目录名无法生成合法备份文件名")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, fname)
    if tid:
        tasks.start(tid, f"备份网站目录 {p}（{cname}）")
    try:
        if tid:
            tasks.log(tid, "容器内打包目录（tar.gz）…")
        res = c.exec_run(["sh", "-c", f"tar -czf {tmp} -C '{p}' ."], demux=True)
        if res[0] != 0:
            raise HTTPException(status_code=502, detail="目录打包失败："
                                + (res[1][1] or b"").decode("utf-8", "replace").strip()[-200:])
        if tid:
            tasks.log(tid, "打包完成，取回落盘…")
        stream, _stat = c.get_archive(tmp)
        try:
            total = 0
            mark = 256 * 1024 * 1024
            done = False
            with open(dest, "wb") as f:
                tf = tarfile.open(fileobj=_StreamReader(stream), mode="r|")
                for m in tf:
                    if m.isfile():
                        src = tf.extractfile(m)
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                            total += len(chunk)
                            if total >= mark and tid:
                                tasks.log(tid, f"拉取中… 已写 {total // 1048576} MB")
                                mark += 256 * 1024 * 1024
                        done = True
                        break
                if not done:
                    raise HTTPException(status_code=502, detail="容器内未返回打包文件")
        finally:
            c.exec_run(["sh", "-c", f"rm -f {tmp}"])
        if tid:
            tasks.finish(tid, True, f"✔ 目录备份完成：{fname}（{os.path.getsize(dest) // 1024} KB）")
        return {"detail": f"目录备份完成：{fname}", "file": fname}
    except Exception as e:  # noqa: BLE001
        if tid:
            tasks.finish(tid, False,
                         f"✘ 目录备份失败：{e.detail if isinstance(e, HTTPException) else e}")
        try:
            if os.path.exists(dest) and os.path.getsize(dest) == 0:
                os.remove(dest)
        except OSError:
            pass
        raise


@router.post("/host/backups/restore/dir")
def restore_dir(body: DirRestoreIn, request: Request = None):
    """网站目录恢复：gz 上传进容器 /tmp → 容器内解压到目标目录（覆盖同名文件，不删其他文件）。"""
    tid = _tid(request)
    if not body.file.endswith(".tar.gz"):
        raise HTTPException(status_code=400, detail="目录备份必须为 .tar.gz 文件")
    target = _safe_path(body.file)
    cname = body.container_id
    if not _CNAME_RE.fullmatch(cname):
        raise HTTPException(status_code=400, detail="容器名格式无效")
    p = body.path.strip().rstrip("/")
    if ".." in p or not _PATH_RE.fullmatch(p):
        raise HTTPException(status_code=400, detail="目标目录无效（需为容器内绝对路径）")
    try:
        c = get_docker_long().containers.get(cname)
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="目标容器不存在或已被删除")
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")
    if c.status != "running":
        raise HTTPException(status_code=502, detail="目标容器未运行")
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="备份文件不存在")
    if tid:
        tasks.start(tid, f"恢复目录 {body.file} → {cname}:{p}")
    try:
        if tid:
            tasks.log(tid, "上传备份包到容器 /tmp…")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            ti = tarfile.TarInfo("restore_" + os.path.basename(body.file))
            ti.size = os.path.getsize(target)
            with open(target, "rb") as f:
                tf.addfile(ti, f)
        buf.seek(0)
        c.put_archive("/tmp", buf.getvalue())
        tmp = "/tmp/restore_" + os.path.basename(body.file)
        if tid:
            tasks.log(tid, f"容器内解压到 {p}（覆盖同名文件）…")
        res = c.exec_run(["sh", "-c", f"mkdir -p '{p}' && tar -xzf {tmp} -C '{p}' && rm -f {tmp}"], demux=True)
        if res[0] != 0:
            c.exec_run(["sh", "-c", f"rm -f {tmp}"])
            raise HTTPException(status_code=502, detail="解压失败："
                                + (res[1][1] or b"").decode("utf-8", "replace").strip()[-200:])
        if tid:
            tasks.finish(tid, True, f"✔ 目录已恢复至 {cname}:{p}")
        return {"detail": f"目录已恢复至 {cname}:{p}"}
    except Exception as e:  # noqa: BLE001
        if tid:
            tasks.finish(tid, False,
                         f"✘ 目录恢复失败：{e.detail if isinstance(e, HTTPException) else e}")
        raise


# ---------- 容器备份：docker export 流式落盘 ----------

@router.post("/host/backups/container")
def backup_container(body: ContainerIn, request: Request):
    tid = _tid(request)
    if tid:
        tasks.start(tid, f"备份容器 {body.container_id[:12]}")
        tasks.log(tid, "查找容器…")
    try:
        client = get_docker_long()
        try:
            c = client.containers.get(body.container_id)
        except docker.errors.NotFound:
            raise HTTPException(status_code=404, detail="容器不存在")
        except docker.errors.APIError as e:
            raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")
        if tid:
            tasks.log(tid, f"容器 {c.name}，开始 docker export 导出文件系统…")

        safe = _re.sub(r"[^A-Za-z0-9._-]", "_", c.name or (c.id or "container")[:12]).strip("._") or "container"
        fname = f"{safe}_{_ts()}.tar"
        os.makedirs(BACKUP_DIR, exist_ok=True)
        dest = os.path.join(BACKUP_DIR, fname)
        total = 0
        mark = 512 * 1024 * 1024
        try:
            with open(dest, "wb") as f:
                for chunk in c.export(chunk_size=2 * 1024 * 1024):  # 流式写盘
                    f.write(chunk)
                    total += len(chunk)
                    if total >= mark:  # chunk 实际粒度不定，按真实字节每 ~512MB 报一次
                        tasks.log(tid, f"导出中… 已写 {total // 1048576}MB")
                        mark += 512 * 1024 * 1024
        except (docker.errors.APIError, OSError) as e:
            try:
                os.remove(dest)  # 半成品不留垃圾
            except OSError:
                pass
            raise HTTPException(status_code=502, detail=f"导出容器失败：{e}")
        size_mb = os.path.getsize(dest) / 1048576
        if tid:
            tasks.log(tid, f"已落盘 {fname}（{size_mb:.1f} MB）")
            tasks.finish(tid, True, "✔ 容器备份完成")
        return {"ok": True, "file": fname,
                "detail": f"容器「{c.name}」已备份为 {fname}"}
    except Exception as e:  # noqa: BLE001
        if tid:
            tasks.finish(tid, False, f"✘ 容器备份失败：{e}")
        raise


# ---------- 数据库备份：mysqldump | gzip → 容器内 tmp → get_archive 取回 ----------

class _StreamReader:
    """把 docker get_archive 的字节流生成器包装成 tarfile 可读的 file-like。"""

    def __init__(self, gen):
        self._gen = gen
        self._buf = b""

    def read(self, n=-1):
        if n == 0:
            return b""
        while n < 0 or len(self._buf) < n:
            try:
                self._buf += next(self._gen)
            except StopIteration:
                break
        if n < 0:
            out, self._buf = self._buf, b""
        else:
            out, self._buf = self._buf[:n], self._buf[n:]
        return out


def _cleanup_in_container(c, *paths: str) -> None:
    try:
        c.exec_run(["sh", "-c", "rm -f " + " ".join(paths)])
    except docker.errors.APIError:
        pass


@router.post("/host/backups/database")
def backup_database(body: DbIn, db: Session = Depends(get_db), request: Request = None):
    tid = _tid(request)
    if tid:
        tasks.start(tid, f"备份数据库 {body.db_name}")
        tasks.log(tid, f"检查 MySQL {body.version} 服务…")
    try:
        ver, dbname = body.version, body.db_name
        if not _DB_RE.fullmatch(dbname):
            raise HTTPException(status_code=400, detail="库名格式无效")
        svc_row = db.get(MySqlService, ver)
        if not svc_row:
            raise HTTPException(status_code=404, detail=f"MySQL {ver} 服务未启用")
        try:
            c = get_docker_long().containers.get(mysql_svc.svc_name(ver))
        except docker.errors.NotFound:
            raise HTTPException(status_code=404, detail="MySQL 容器不存在，请重新启用服务")
        except docker.errors.APIError as e:
            raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")
        if c.status != "running":
            raise HTTPException(status_code=502, detail=f"MySQL {ver} 服务未运行，无法导出")
        # 库不存在时提前给出可读提示（库可能已被删除，而前端下拉列表是旧数据）
        # demux 分离 stderr：mysql 的 "Using a password" 警告会混入 output 导致误判
        chk = c.exec_run(["sh", "-c",
                          f"mysql -uroot --password='{svc_row.root_password}' -N -e "
                          f"\"SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='{dbname}'\""],
                         demux=True)
        if chk[0] != 0 or (chk[1][0] or b"").strip() != b"1":
            raise HTTPException(status_code=404,
                                detail=f"数据库 {dbname} 不存在（可能已被删除），请关闭面板重新打开备份页刷新列表")
        tables = [t.strip() for t in (body.tables or []) if t and t.strip()]
        for t in tables:
            if not _TBL_RE.fullmatch(t):
                raise HTTPException(status_code=400, detail=f"表名无效：{t}")
        tbl_part = (" " + " ".join(tables)) if tables else ""
        if tid:
            tasks.log(tid, "执行 mysqldump（单事务）+ gzip 压缩…（"
                      + (f"表：{', '.join(tables)}" if tables else "整库") + "）")

        tmp_sql = f"/tmp/ppanel_bk_{_ts()}_{os.urandom(3).hex()}.sql"
        tmp_gz = tmp_sql + ".gz"
        # 表级备份文件名带表名（多表取首个+_m 标记），与整库备份可区分（定时任务的保留策略按此筛选）
        fname = (f"{dbname}_{tables[0]}{'_m' if len(tables) > 1 else ''}_{_ts()}.sql.gz"
                 if tables else f"{dbname}_{_ts()}.sql.gz")
        os.makedirs(BACKUP_DIR, exist_ok=True)
        dest = os.path.join(BACKUP_DIR, fname)

        # 1) 容器内导出：--result-file 直写文件（不用管道，mysqldump 失败不会
        #    被 gzip 的 0 退出码掩盖）→ 成功后 gzip 压缩
        res = c.exec_run(["sh", "-c",
                          f"mysqldump -uroot --password='{svc_row.root_password}' "
                          f"--single-transaction --routines --triggers {dbname}{tbl_part} "
                          f"--result-file={tmp_sql} && gzip -f {tmp_sql}"])
        out = res.output.decode("utf-8", "replace") if isinstance(res.output, bytes) else str(res.output)
        if res.exit_code != 0:
            lines = [l for l in out.splitlines() if "Using a password on the command line" not in l]
            raise HTTPException(status_code=502, detail=f"mysqldump 失败：{''.join(lines).strip()[-300:]}")
        if tid:
            tasks.log(tid, "mysqldump 完成，从容器取回压缩包…")

        # 2) 从容器取回 gzip 文件（tar 流 → 解出内层文件），失败不留半成品
        try:
            stream, _stat = c.get_archive(tmp_gz)
            done = False
            with open(dest, "wb") as f:
                tf = tarfile.open(fileobj=_StreamReader(stream), mode="r|")
                try:
                    for m in tf:
                        if m.isfile():
                            shutil.copyfileobj(tf.extractfile(m), f, 1024 * 1024)
                            done = True
                            break
                finally:
                    tf.close()
            if not done:
                raise RuntimeError("备份流中没有文件")
        except Exception as e:  # noqa: BLE001
            try:
                os.remove(dest)
            except OSError:
                pass
            _cleanup_in_container(c, tmp_sql, tmp_gz)
            raise HTTPException(status_code=502, detail=f"取回备份失败：{e}")
        finally:
            _cleanup_in_container(c, tmp_sql, tmp_gz)

        if tid:
            tasks.log(tid, f"已落盘 {fname}（{os.path.getsize(dest) / 1048576:.1f} MB）")
            tasks.finish(tid, True, "✔ 数据库备份完成")
        return {"ok": True, "file": fname,
                "detail": f"数据库 {dbname} 已备份为 {fname}"}
    except Exception as e:  # noqa: BLE001
        if tid:
            tasks.finish(tid, False, f"✘ 数据库备份失败：{e}")
        raise


# ---------- 备份选项：可备份的实例数据库清单 ----------

@router.get("/host/backups/dbs")
def backup_db_options(db: Session = Depends(get_db)):
    rows = (db.query(InstanceDb.version, InstanceDb.db_name)
            .order_by(InstanceDb.version, InstanceDb.db_name).all())
    client = None
    try:
        client = get_docker_long()
    except HTTPException:
        pass
    # 登记表可能残留已删除的库（如实例被清理后 InstanceDb 未同步），
    # 下拉只显示 MySQL 中真实存在的库：对运行中的服务实时 SHOW DATABASES 求交集。
    # 登记行本身保留——数据库恢复功能重建同名库后仍需它能出现在下拉里。
    existing: dict = {}
    if client is not None:
        for ver in {r.version for r in rows}:
            try:
                c = client.containers.get(mysql_svc.svc_name(ver))
                if c.status != "running":
                    continue
                pw_row = db.get(MySqlService, ver)
                res = c.exec_run(["sh", "-c",
                                  f"mysql -uroot --password='{pw_row.root_password}' -N -e 'SHOW DATABASES'"],
                                 demux=True)  # 分离 stderr，避免密码警告行混进库名列表
                names = ((res[1][0] if res[0] == 0 else b"") or b"").decode("utf-8", "replace").split()
                existing[ver] = {n for n in names if n and n not in (
                    "information_schema", "mysql", "performance_schema", "sys")}
            except (docker.errors.NotFound, docker.errors.APIError):
                continue
    out = []
    for r in rows:
        running = r.version in existing
        if running and r.db_name not in existing[r.version]:
            continue  # 服务在运行但库已不存在 → 隐藏
        out.append({"version": r.version, "db_name": r.db_name, "running": running})
    return {"dbs": out}


# ---------- 恢复：容器 tar → import 为镜像 → 创建新容器 ----------

class RestoreContainerIn(BaseModel):
    file: str
    name: str


@router.post("/host/backups/restore/container")
def restore_container(body: RestoreContainerIn, request: Request):
    tid = _tid(request)
    if tid:
        tasks.start(tid, f"恢复容器备份 {body.file}")
    try:
        src = _safe_path(body.file)
        if not body.file.endswith(".tar") or not os.path.isfile(src):
            raise HTTPException(status_code=400, detail="容器备份必须为 .tar 文件")
        name = (body.name or "").strip()
        if not _CNAME_RE.fullmatch(name):
            raise HTTPException(status_code=400, detail="容器名无效（字母数字开头，可含 _ . -）")
        client = get_docker_long()
        try:
            client.containers.get(name)
            raise HTTPException(status_code=409, detail=f"容器 {name} 已存在，请换一个名字")
        except docker.errors.NotFound:
            pass
        except docker.errors.APIError as e:
            raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")

        ts = _ts()
        repo, tag = "ppanel-restore", ts.lower()
        image = f"{repo}:{tag}"
        if tid:
            tasks.log(tid, f"docker import 导入镜像 {image}（大包耗时较久）…")
        try:
            # export 的 tar 只含文件系统：import 为镜像并注入 sleep 入口，
            # 容器保持存活供 exec 进去取回数据（完整恢复服务请按原配置重建）
            client.api.import_image_from_file(
                src, repository=repo, tag=tag,
                changes=['CMD ["/bin/sh", "-c", "sleep infinity"]'])
        except docker.errors.APIError as e:
            raise HTTPException(status_code=502, detail=f"导入镜像失败：{e}")
        if tid:
            tasks.log(tid, "镜像导入完成，创建新容器…")
        try:
            client.images.get(image)  # 确认导入成功（否则 run 会误走 registry 拉取）
            client.containers.run(image, name=name, detach=True)
        except docker.errors.APIError as e:
            raise HTTPException(status_code=502,
                                detail=f"镜像已导入为 {image}，但创建容器失败：{e}")
        if tid:
            tasks.finish(tid, True, f"✔ 已创建容器 {name}（sleep 入口，供取回数据）")
        return {"ok": True, "container": name, "image": image,
                "detail": f"已恢复为新容器 {name}（sleep 入口，供取回数据）"}
    except Exception as e:  # noqa: BLE001
        if tid:
            tasks.finish(tid, False, f"✘ 容器恢复失败：{e}")
        raise


# ---------- 恢复：sql.gz → 传入 MySQL 容器 → 建库 + 导入（覆盖） ----------

class RestoreDbIn(BaseModel):
    file: str
    version: str
    db_name: str


@router.post("/host/backups/restore/database")
def restore_database(body: RestoreDbIn, db: Session = Depends(get_db), request: Request = None):
    tid = _tid(request)
    if tid:
        tasks.start(tid, f"恢复数据库备份 {body.file}")
        tasks.log(tid, f"目标：MySQL {body.version} / {body.db_name}")
    try:
        src = _safe_path(body.file)
        if not body.file.endswith(".sql.gz") or not os.path.isfile(src):
            raise HTTPException(status_code=400, detail="数据库备份必须为 .sql.gz 文件")
        dbname = body.db_name
        if not _DB_RE.fullmatch(dbname):
            raise HTTPException(status_code=400, detail="库名格式无效")
        svc_row = db.get(MySqlService, body.version)
        if not svc_row:
            raise HTTPException(status_code=404, detail=f"MySQL {body.version} 服务未启用")
        try:
            c = get_docker_long().containers.get(mysql_svc.svc_name(body.version))
        except docker.errors.NotFound:
            raise HTTPException(status_code=404, detail="MySQL 容器不存在，请重新启用服务")
        except docker.errors.APIError as e:
            raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")
        if c.status != "running":
            raise HTTPException(status_code=502, detail=f"MySQL {body.version} 服务未运行，无法恢复")

        tmp_gz = f"/tmp/ppanel_restore_{_ts()}_{os.urandom(3).hex()}.sql.gz"
        if tid:
            tasks.log(tid, "上传备份到 MySQL 容器 /tmp…")
        # 打包为单文件 tar 推进容器（流式读盘打包，仅多一份等大小内存缓冲）
        try:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tf:
                with open(src, "rb") as f:
                    ti = tarfile.TarInfo(os.path.basename(tmp_gz))  # 解包后路径须与命令一致
                    ti.size = os.fstat(f.fileno()).st_size
                    tf.addfile(ti, f)
            c.put_archive("/tmp", buf.getvalue())
        except (OSError, docker.errors.APIError) as e:
            raise HTTPException(status_code=502, detail=f"上传备份到 MySQL 容器失败：{e}")

        try:
            if tid:
                tasks.log(tid, "建库（如不存在）并导入，gzip 完整性校验后灌入…")
            # 先建库再导入：gzip -t 校验包完整，管道退出码取自 mysql（真实反映导入成败）
            res = c.exec_run(["sh", "-c",
                              f"mysql -uroot --password='{svc_row.root_password}' "
                              f"-e 'CREATE DATABASE IF NOT EXISTS {dbname}' "
                              f"&& gzip -t {tmp_gz} "
                              f"&& gunzip -c {tmp_gz} | "
                              f"mysql -uroot --password='{svc_row.root_password}' {dbname}"])
            out = res.output.decode("utf-8", "replace") if isinstance(res.output, bytes) else str(res.output)
            if res.exit_code != 0:
                lines = [l for l in out.splitlines() if "Using a password on the command line" not in l]
                raise HTTPException(status_code=502, detail=f"恢复失败：{''.join(lines).strip()[-300:]}")
        finally:
            _cleanup_in_container(c, tmp_gz)
        if tid:
            tasks.finish(tid, True, f"✔ 已恢复到 {body.version} 的 {dbname} 库（覆盖导入）")
        return {"ok": True, "db": dbname,
                "detail": f"备份已恢复到 {body.version} 的 {dbname} 库（覆盖导入）"}
    except Exception as e:  # noqa: BLE001
        if tid:
            tasks.finish(tid, False, f"✘ 数据库恢复失败：{e}")
        raise


# ---------- 定时备份任务：管理员 cron，任意容器 / 目录 / 数据库（整库或表级） ----------

from app.models import HostBackupJob, MySqlService  # noqa: E402
from app.services import cron_service as _cron_svc  # noqa: E402
from app.services import cos_service as _cos_svc  # noqa: E402

_BK_KINDS = ("container", "dir", "db_full", "db_table")
_SYSTEM_DBS = {"information_schema", "mysql", "performance_schema", "sys"}


# ---------- 腾讯云 COS 存储设置（节点级，单行配置） ----------

class CosIn(BaseModel):
    secret_id: str = ""
    secret_key: str = ""          # 传空 = 沿用已保存的密钥
    bucket: str = ""
    region: str = ""
    prefix: str = "ppanel-backups"
    keep_local: int = 1
    enabled: int = 0


@router.get("/host/cos")
def get_cos():
    cfg = _cos_svc.get_config()
    key = cfg.secret_key or ""
    masked = (key[:4] + "****" + key[-4:]) if len(key) > 8 else ("****" if key else "")
    return {"secret_id": cfg.secret_id, "secret_key_masked": masked,
            "has_key": bool(key), "bucket": cfg.bucket, "region": cfg.region,
            "prefix": cfg.prefix, "keep_local": bool(cfg.keep_local),
            "enabled": bool(cfg.enabled)}


@router.put("/host/cos")
def put_cos(body: CosIn, request: Request = None):
    db = SessionLocal()
    try:
        cfg = _cos_svc.get_config(db)
        if body.secret_id.strip():
            cfg.secret_id = body.secret_id.strip()
        if body.secret_key.strip():  # 留空 = 沿用旧密钥
            cfg.secret_key = body.secret_key.strip()
        cfg.bucket = body.bucket.strip()
        cfg.region = body.region.strip()
        cfg.prefix = body.prefix.strip() or "ppanel-backups"
        cfg.keep_local = 1 if body.keep_local else 0
        cfg.enabled = 1 if body.enabled else 0
        db.commit()
        if cfg.enabled:
            try:
                r = _cos_svc.test_conn(cfg)
            except Exception as e:  # noqa: BLE001
                cfg.enabled = 0  # 启用时连通性校验失败 → 保持关闭，避免"看似启用实际传不上去"
                db.commit()
                raise HTTPException(status_code=400, detail=f"配置已保存，但连通性校验失败，未启用：{e}")
            return {"ok": True, "message": r["message"]}
        return {"ok": True, "message": "配置已保存（未启用）"}
    finally:
        db.close()


class BackupJobIn(BaseModel):
    schedule: str
    kind: str                     # container / dir / db_full / db_table
    target: str                   # 容器名；或 "版本:库名"
    dir_path: str = ""            # kind=dir：容器内目录（默认 /app）
    table_name: str = ""          # kind=db_table：表名（空=整库）
    keep: int = 5                 # 保留最近 N 份
    dest: str = "local"           # local / cos / both


_DESTS = ("local", "cos", "both")


def _job_out(j: HostBackupJob) -> dict:
    return {"id": j.id, "schedule": j.schedule, "kind": j.kind, "target": j.target,
            "dir_path": j.dir_path, "table_name": j.table_name, "keep": j.keep,
            "dest": j.dest or "local",
            "enabled": bool(j.enabled), "last_run": j.last_run.isoformat() if j.last_run else None,
            "last_status": j.last_status, "last_output": j.last_output or ""}


def _validate_job_in(body: BackupJobIn) -> None:
    if _cron_svc.validate(body.schedule.strip()):
        raise HTTPException(status_code=400, detail=_cron_svc.validate(body.schedule.strip()))
    if body.kind not in _BK_KINDS:
        raise HTTPException(status_code=400, detail="备份类型无效")
    if body.dest not in _DESTS:
        raise HTTPException(status_code=400, detail="备份目的地无效")
    if body.dest in ("cos", "both"):
        cfg = _cos_svc.get_config()
        if not cfg or not cfg.enabled:
            raise HTTPException(status_code=400,
                                detail="腾讯云 COS 未启用，请先在「存储设置」中保存并启用")
    if not (1 <= body.keep <= 100):
        raise HTTPException(status_code=400, detail="保留份数应为 1-100")
    if body.kind in ("container", "dir"):
        if not _CNAME_RE.fullmatch(body.target):
            raise HTTPException(status_code=400, detail="容器名格式无效")
        if body.kind == "dir":
            p = body.dir_path.strip() or "/app"
            if not p.startswith("/") or ".." in p.split("/"):
                raise HTTPException(status_code=400, detail="目录必须是容器内绝对路径且不能含 ..")
    else:
        ver, _, dbname = body.target.partition(":")
        if not ver or not dbname:
            raise HTTPException(status_code=400, detail="目标格式应为 版本:库名（如 5.7:ppanel_1）")
        if not _DB_RE.fullmatch(dbname):
            raise HTTPException(status_code=400, detail="库名格式无效")
        db2 = SessionLocal()
        try:
            if not db2.get(MySqlService, ver):
                raise HTTPException(status_code=404, detail=f"MySQL {ver} 服务未启用")
        finally:
            db2.close()
        if body.kind == "db_table" and body.table_name and not _TBL_RE.fullmatch(body.table_name):
            raise HTTPException(status_code=400, detail=f"表名无效：{body.table_name}")


@router.get("/host/backup-jobs")
def list_backup_jobs():
    db = SessionLocal()
    try:
        rows = db.query(HostBackupJob).order_by(HostBackupJob.created_at).all()
        return {"jobs": [_job_out(j) for j in rows]}
    finally:
        db.close()


@router.get("/host/backup-jobs/meta")
def backup_jobs_meta():
    """新建任务的候选清单：全部容器 + 各 MySQL 版本的库列表（排除系统库）。"""
    client = get_docker()
    containers = [{"name": c.name, "status": c.status,
                   "image": (c.image.tags or [""])[0]}
                  for c in client.containers.list(all=True)]
    mysql = []
    db = SessionLocal()
    try:
        for svc_row in db.query(MySqlService).all():
            entry = {"version": svc_row.version, "running": False, "dbs": []}
            try:
                c = client.containers.get(mysql_svc.svc_name(svc_row.version))
                if c.status == "running":
                    entry["running"] = True
                    res = c.exec_run(["sh", "-c",
                                      f"mysql -uroot --password='{svc_row.root_password}' "
                                      f"-N -e 'SHOW DATABASES'"], demux=True)
                    if res[0] == 0:
                        entry["dbs"] = [l.strip() for l in (res[1][0] or b"").decode(
                            "utf-8", "replace").splitlines()
                            if l.strip() and l.strip() not in _SYSTEM_DBS]
            except docker.errors.NotFound:
                pass
            mysql.append(entry)
    finally:
        db.close()
    return {"containers": containers, "mysql": mysql}


@router.post("/host/backup-jobs")
def create_backup_job(body: BackupJobIn, request: Request = None):
    _validate_job_in(body)
    db = SessionLocal()
    try:
        j = HostBackupJob(schedule=body.schedule.strip(), kind=body.kind,
                          target=body.target.strip(), dir_path=body.dir_path.strip(),
                          table_name=body.table_name.strip(), keep=body.keep,
                          dest=body.dest)
        db.add(j)
        db.commit()
        if body.kind == "db_table" and not body.table_name.strip():
            j.kind = "db_full"  # 表级任务但未选表 → 按整库
            db.commit()
        out = _job_out(j)
    finally:
        db.close()
    _spawn_thread_safe(j.id)
    return out


def _spawn_thread_safe(job_id: int) -> None:
    """创建即跑一次首份备份；失败只写任务状态，不影响创建结果。"""
    from app.services import host_backup_service
    threading.Thread(target=host_backup_service.run_job, args=(job_id,),
                     daemon=True, name=f"hostbk-run-{job_id}").start()


@router.patch("/host/backup-jobs/{jid}")
def update_backup_job(jid: int, body: dict, request: Request = None):
    db = SessionLocal()
    try:
        j = db.get(HostBackupJob, jid)
        if not j:
            raise HTTPException(status_code=404, detail="任务不存在")
        if "schedule" in body:
            err = _cron_svc.validate(str(body["schedule"]).strip())
            if err:
                raise HTTPException(status_code=400, detail=err)
            j.schedule = str(body["schedule"]).strip()
        if "enabled" in body:
            j.enabled = 1 if body["enabled"] else 0
        if "keep" in body:
            try:
                keep = int(body["keep"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="保留份数无效")
            if not (1 <= keep <= 100):
                raise HTTPException(status_code=400, detail="保留份数应为 1-100")
            j.keep = keep
        if "dest" in body:
            if body["dest"] not in _DESTS:
                raise HTTPException(status_code=400, detail="备份目的地无效")
            if body["dest"] in ("cos", "both"):
                cfg = _cos_svc.get_config()
                if not cfg or not cfg.enabled:
                    raise HTTPException(status_code=400,
                                        detail="腾讯云 COS 未启用，请先在「存储设置」中保存并启用")
            j.dest = body["dest"]
        db.commit()
        return _job_out(j)
    finally:
        db.close()


@router.delete("/host/backup-jobs/{jid}")
def delete_backup_job(jid: int):
    db = SessionLocal()
    try:
        j = db.get(HostBackupJob, jid)
        if not j:
            raise HTTPException(status_code=404, detail="任务不存在")
        db.delete(j)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.post("/host/backup-jobs/{jid}/run")
def run_backup_job(jid: int, request: Request = None):
    """立即执行一次（带 X-Task-Id 时走任务终端实时日志）。"""
    from app.services import host_backup_service
    r = host_backup_service.run_job(jid, request)
    if not r.get("ok"):
        raise HTTPException(status_code=502, detail=r.get("error", "执行失败"))
    return r
