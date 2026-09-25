"""phpMyAdmin 一键开启/关闭（实例级）。

enable：生成仅能访问本实例库的临时账号 → 起 phpmyadmin 容器（自动登录 env，
label 标记 2 小时过期，由 cron_service._cleanup_pma 兜底回收）→ 返回访问 URL。
disable：删容器 + 删临时账号。
"""
import os
import secrets
import socket
import time

import docker
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.docker_client import get_docker
from app.models import Instance, InstanceDb, MySqlService
from app.services import mysql_service as mysql_svc

PMA_IMAGE = "phpmyadmin:5.2-apache"
PMA_TTL = 2 * 3600
PMA_PORT_BASE = 36100   # 36100-36999 探测空闲端口


def _free_port(prefer: int) -> int:
    used: set[int] = set()
    try:
        for c in get_docker().containers.list(all=True):
            for mp in (c.attrs.get("HostConfig", {}).get("PortBindings") or {}).values():
                for b in mp or []:
                    try:
                        used.add(int(b.get("HostPort")))
                    except (TypeError, ValueError):
                        pass
    except Exception:  # noqa: BLE001
        pass
    port = prefer
    for _ in range(64):
        if port not in used and not _loop_busy(port):
            return port
        port += 1 if port < PMA_PORT_BASE + 899 else 0
    raise HTTPException(status_code=502, detail="无法找到空闲端口（36100-36999 已满）")


def _loop_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _db_of(iid: int, db: Session) -> tuple[InstanceDb, MySqlService]:
    reg = db.query(InstanceDb).filter(InstanceDb.instance_id == iid).first()
    if not reg:
        raise HTTPException(status_code=400, detail="实例尚未开通数据库，请先在「数据库」页开通")
    svc_row = db.get(MySqlService, reg.version)
    if not svc_row:
        raise HTTPException(status_code=404, detail=f"MySQL {reg.version} 服务未启用")
    try:
        c = get_docker().containers.get(mysql_svc.svc_name(reg.version))
        if c.status != "running":
            raise HTTPException(status_code=502, detail="MySQL 服务未运行")
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="MySQL 容器不存在")
    return reg, svc_row


def _remove_old(iid: int, db: Session) -> None:
    """清理旧 pma 容器与账号（重复开启/关闭时）。"""
    try:
        c = get_docker().containers.get(f"ppanel-pma-{iid}")
        user = (c.labels or {}).get("ppanel.pma-user", "")
        c.remove(force=True)
        if user:
            _drop_user(db, user)
    except docker.errors.NotFound:
        pass
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"清理旧 phpMyAdmin 容器失败：{e}")


def _drop_user(db: Session, user: str) -> None:
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_]{1,64}", user or ""):
        return
    for svc_row in db.query(MySqlService).all():
        try:
            c = get_docker().containers.get(mysql_svc.svc_name(svc_row.version))
            if c.status != "running":
                continue
            c.exec_run(["sh", "-c",
                        f"mysql -uroot --password='{svc_row.root_password}' "
                        f"-e \"DROP USER IF EXISTS '{user}'@'%'\""])
        except (docker.errors.NotFound, docker.errors.APIError):
            continue


def enable(inst: Instance, request: Request, db: Session) -> dict:
    iid = inst.id
    reg, svc_row = _db_of(iid, db)
    _remove_old(iid, db)

    user = f"pma{iid}_{secrets.token_hex(2)}"
    password = secrets.token_urlsafe(16)
    mc = get_docker().containers.get(mysql_svc.svc_name(reg.version))
    # 注意：库名不加反引号（ppanel_1 已过 _DB_RE 白名单）——反引号在 sh -c 双引号里会被 shell 解释成命令替换
    res = mc.exec_run(["sh", "-c",
                       f"mysql -uroot --password='{svc_row.root_password}' "
                       f"-e \"CREATE USER IF NOT EXISTS '{user}'@'%' IDENTIFIED BY '{password}';"
                       f"GRANT ALL PRIVILEGES ON {reg.db_name}.* TO '{user}'@'%';"
                       f"FLUSH PRIVILEGES;\""])
    if res.exit_code != 0:
        out = (res.output or b"").decode("utf-8", "replace")[-200:]
        raise HTTPException(status_code=502, detail=f"创建临时账号失败：{out}")

    port = _free_port(PMA_PORT_BASE + iid)
    expire = time.time() + PMA_TTL
    try:
        get_docker().containers.run(
            PMA_IMAGE, name=f"ppanel-pma-{iid}", detach=True,
            ports={"80/tcp": port},
            network="ppanel-net",
            environment={"PMA_HOST": mysql_svc.svc_name(reg.version),
                         "PMA_USER": user, "PMA_PASSWORD": password},
            labels={"ppanel.managed": "true", "ppanel.pma-expire": str(expire),
                    "ppanel.pma-user": user, "ppanel.instance": str(iid)},
            restart_policy={"Name": "unless-stopped"})
    except docker.errors.ImageNotFound:
        raise HTTPException(status_code=502,
                            detail=f"镜像 {PMA_IMAGE} 未预拉取，请在节点面板拉取后重试")
    except docker.errors.APIError as e:
        _drop_user(db, user)
        raise HTTPException(status_code=502, detail=f"启动 phpMyAdmin 容器失败：{e}")

    host = request.url.hostname or ""
    return {"url": f"http://{host}:{port}/", "port": port,
            "expire_in": PMA_TTL, "db": reg.db_name,
            "detail": f"phpMyAdmin 已开启（{PMA_TTL // 3600} 小时后自动关闭）"}


def disable(iid: int, db: Session) -> dict:
    _remove_old(iid, db)
    return {"detail": "phpMyAdmin 已关闭（临时容器与账号已删除）"}
