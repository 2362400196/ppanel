"""节点级共享 MySQL 服务：每版本一个 MySQL 容器，多实例共享进程、各持独立库。

启用 = 跑 mysql:{ver} 容器（镜像必须已本地拉取，绝不自动 pull）；
实例容器 join ppanel-net 后用容器名直连（内网），外网直连走映射的宿主端口
（所有库共用一个端口，按账号鉴权）。
实例回收时联动删库（drop_instance_db），到期只读模式由 panel 层统一拦截。
"""
import socket
import subprocess
import time

import docker
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.docker_client import get_docker
from app.models import Instance, InstanceDb, MySqlService
from app.services.instance_service import container_prefix

# 版本列表以节点已拉取的 mysql 镜像为准（动态发现，不再硬编码候选）；
# 未拉取镜像时启用会被拦（绝不自动 pull）
_MEM_LIMIT = 768 * 1024 * 1024  # MySQL 服务固定 768M（swap 同值）


def _pulled_versions(client: docker.DockerClient) -> set[str]:
    """本地已拉取的 mysql 镜像 tag 集合（如 5.7 / 8.0 / 8.4 / latest）。"""
    vers: set[str] = set()
    try:
        for img in client.images.list(name="mysql"):
            for tag in (img.tags or []):
                if not tag.startswith("mysql:"):
                    continue
                v = tag.split(":", 1)[1]
                if v and v != "<none>":
                    vers.add(v)
    except docker.errors.APIError:
        pass
    return vers


def _ver_sort_key(v: str):
    try:
        return (0, float(v), v)   # 纯数字版本按数值排
    except ValueError:
        return (1, 0.0, v)        # latest 等非数字 tag 排在后面


def svc_name(ver: str) -> str:
    return f"{container_prefix()}mysql-{ver}"


def vol_name(ver: str) -> str:
    return f"ppanel-mysql-{ver}-data"


# ---------- 内部工具 ----------

def _get_svc_row(db: Session, ver: str) -> MySqlService:
    row = db.get(MySqlService, ver)
    if not row:
        raise HTTPException(status_code=404, detail=f"MySQL {ver} 服务未启用")
    return row


def _svc_container(ver: str, client: docker.DockerClient | None = None):
    client = client or get_docker()
    try:
        return client.containers.get(svc_name(ver))
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="MySQL 容器不存在，请重新启用服务")
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")


def _sql(ver: str, root_password: str, sql: str) -> str:
    """在 MySQL 容器内执行 SQL（列表参数不过 shell，无注入面）。返回输出文本。"""
    c = _svc_container(ver)
    try:
        res = c.exec_run(["mysql", "-uroot", f"--password={root_password}", "-e", sql])
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"容器执行失败：{e}")
    out = res.output.decode("utf-8", "replace") if isinstance(res.output, bytes) else str(res.output)
    if res.exit_code != 0:
        # 去掉 mysql 密码的固定 Warning 行，保留真实错误
        lines = [l for l in out.splitlines() if "Using a password on the command line" not in l]
        raise HTTPException(status_code=502, detail=f"MySQL 执行失败：{''.join(lines).strip() or f'exit={res.exit_code}'}")
    return out


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def _pick_port() -> int:
    """外网直连端口：优先 3306，被占用则在实例端口段内找空闲口。"""
    if _port_free(3306):
        return 3306
    for p in range(settings.port_start, settings.port_end + 1):
        if _port_free(p):
            return p
    raise HTTPException(status_code=503, detail="无可用端口：3306 与实例端口段均被占用")


def _fw_allow(port: int) -> str | None:
    """防火墙放行外网直连端口（ufw / firewalld 自动探测，列表参数防注入）。
    未安装防火墙工具或执行失败均跳过——不阻塞启用流程。"""
    def _which(binname: str) -> str | None:
        import shutil
        p = shutil.which(binname)
        if p:
            return p
        for pre in ("/usr/sbin", "/sbin", "/usr/local/sbin"):
            import os
            cand = f"{pre}/{binname}"
            if os.path.exists(cand):
                return cand
        return None

    ufw = _which("ufw")
    if ufw:
        try:
            subprocess.run([ufw, "allow", f"{port}/tcp"], capture_output=True, timeout=15)
        except Exception:  # noqa: BLE001
            pass
        return None
    fw = _which("firewall-cmd")
    if fw:
        try:
            subprocess.run([fw, "--permanent", f"--add-port={port}/tcp"], capture_output=True, timeout=15)
            subprocess.run([fw, "--reload"], capture_output=True, timeout=30)
        except Exception:  # noqa: BLE001
            pass
    return None  # 未安装防火墙工具：跳过


def _wait_ready(ver: str, root_password: str, timeout: int = 90, tid: str = "") -> None:
    """循环 mysqladmin ping 直到 mysqld 就绪（首次启动需初始化数据目录）。"""
    from app import tasks
    c = _svc_container(ver)
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if c.status != "running":
            logs = c.logs(tail=20).decode("utf-8", "replace")
            raise HTTPException(status_code=502, detail=f"MySQL 容器启动失败：{logs.strip()[-400:]}")
        try:
            res = c.exec_run(["mysqladmin", "ping", "-uroot", f"-p{root_password}", "--silent"])
            last = res.output.decode("utf-8", "replace") if isinstance(res.output, bytes) else ""
            if res.exit_code == 0:
                tasks.log(tid, f"mysqld 就绪（等待 {int(timeout - (deadline - time.time()))}s）")
                return
        except docker.errors.APIError:
            pass
        if tid and int(deadline - time.time()) % 10 == 0:
            tasks.log(tid, f"等待 mysqld 初始化…（剩余 {int(deadline - time.time())}s 内）")
        time.sleep(2)
    raise HTTPException(status_code=504, detail=f"MySQL 在 {timeout}s 内未就绪，请稍后在服务列表查看状态")


# ---------- 服务生命周期（管理员，/agent/docker/mysql/*） ----------

def enable_mysql(db: Session, ver: str, tid: str = "") -> dict:
    from app import tasks
    if db.get(MySqlService, ver):
        raise HTTPException(status_code=409, detail=f"MySQL {ver} 已启用")

    client = get_docker()
    image = f"mysql:{ver}"
    try:
        client.images.get(image)  # 绝不自动 pull，防占磁盘
    except docker.errors.ImageNotFound:
        raise HTTPException(status_code=404, detail=f"镜像 {image} 未拉取，请先在镜像页拉取")
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")
    tasks.log(tid, f"镜像 {image} 就绪，选取外网端口…")

    import secrets
    root_password = secrets.token_urlsafe(24)
    host_port = _pick_port()

    try:
        client.volumes.get(vol_name(ver))
    except docker.errors.NotFound:
        client.volumes.create(vol_name(ver))
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")
    tasks.log(tid, f"数据卷 {vol_name(ver)} 就绪，端口 {host_port}")

    # 同名容器残留（如 DB 记录丢失）：先删旧容器再建（数据在卷里，不丢）
    try:
        client.containers.get(svc_name(ver)).remove(force=True)
    except docker.errors.NotFound:
        pass

    try:
        container = client.containers.run(
            image=image,
            name=svc_name(ver),
            environment={"MYSQL_ROOT_PASSWORD": root_password},
            volumes={vol_name(ver): {"bind": "/var/lib/mysql", "mode": "rw"}},
            ports={"3306/tcp": host_port},
            network=settings.docker_network,
            nano_cpus=int(1.0 * 1e9),
            mem_limit=_MEM_LIMIT,
            memswap_limit=_MEM_LIMIT,
            pids_limit=settings.pids_limit,
            restart_policy={"Name": "unless-stopped"},
            privileged=False,
            detach=True,
        )
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"创建 MySQL 容器失败：{e}")
    tasks.log(tid, f"容器 {svc_name(ver)} 已创建，等待 mysqld 就绪（首次初始化较慢）…")

    _wait_ready(ver, root_password, tid=tid)

    # 数据卷复用（停用未删卷→重新启用）时 MYSQL_ROOT_PASSWORD 不生效（它只在卷为空的
    # 首次初始化生效），卷内旧 root 密码会与登记密码失配——这里校验并自动重置
    if not _sql_ok(ver, root_password):
        tasks.log(tid, "检测到数据卷已有旧数据（root 密码失配），自动重置 root 密码…")
        _reset_root_password(ver, root_password, tid=tid)

    _fw_allow(host_port)

    row = MySqlService(version=ver, container_id=container.id,
                       root_password=root_password, host_port=host_port)
    db.add(row)
    db.commit()
    return {"ok": True, "version": ver, "host_port": host_port, "root_password": root_password}


def _sql_ok(ver: str, password: str) -> bool:
    """真实 SQL 校验 root 密码（mysqladmin ping 对 access denied 也算存活，不可靠）。"""
    try:
        _sql(ver, password, "SELECT 1")
        return True
    except HTTPException:
        return False


def _reset_root_password(ver: str, new_password: str, tid: str = "") -> None:
    """复用旧数据卷导致 root 密码失配时：停主容器 → skip-grant 临时实例重置 → 复原。"""
    from app import tasks
    client = get_docker()
    main = client.containers.get(svc_name(ver))
    main.stop(timeout=10)
    tmp_name = f"{svc_name(ver)}-resetpw"
    try:
        client.containers.get(tmp_name).remove(force=True)
    except docker.errors.NotFound:
        pass
    tasks.log(tid, "启动 skip-grant 临时实例…")
    tmp = client.containers.run(
        image=f"mysql:{ver}",
        name=tmp_name,
        command=["--skip-grant-tables", "--skip-networking"],
        volumes={vol_name(ver): {"bind": "/var/lib/mysql", "mode": "rw"}},
        network=settings.docker_network,
        detach=True,
    )
    try:
        for _ in range(60):  # 等 skip-grant 实例就绪
            time.sleep(1)
            r = tmp.exec_run(["sh", "-c", "mysql -uroot -N -e 'SELECT 1'"])
            if r.exit_code == 0:
                break
        else:
            raise HTTPException(status_code=502, detail="root 密码重置超时：临时实例未就绪")
        # skip-grant 模式下必须先 FLUSH PRIVILEGES 才能执行 ALTER USER
        r = tmp.exec_run(["sh", "-c",
            "mysql -uroot -e \"FLUSH PRIVILEGES; "
            f"ALTER USER 'root'@'%' IDENTIFIED BY '{new_password}'; "
            f"ALTER USER 'root'@'localhost' IDENTIFIED BY '{new_password}'; "
            "FLUSH PRIVILEGES;\""])
        if r.exit_code != 0:
            raise HTTPException(status_code=502, detail="root 密码重置失败："
                                + (r.output or b"").decode("utf-8", "replace")[-200:])
    finally:
        try:
            tmp.remove(force=True)
        except docker.errors.APIError:
            pass
    tasks.log(tid, "重启 MySQL 服务…")
    main.start()
    _wait_ready(ver, new_password, tid=tid)  # 重启后 mysqld 需数秒就绪，直接查必失败
    if not _sql_ok(ver, new_password):
        raise HTTPException(status_code=502, detail="root 密码重置后仍无法连接，请查看容器日志")


def disable_mysql(db: Session, ver: str, purge: bool = False, tid: str = "") -> dict:
    from app import tasks
    _get_svc_row(db, ver)
    n = db.query(InstanceDb).filter(InstanceDb.version == ver).count()
    if n:
        raise HTTPException(status_code=400, detail=f"该版本下还有 {n} 个实例数据库，请先删除后再停用")

    client = get_docker()
    tasks.log(tid, "删除 MySQL 容器…")
    try:
        client.containers.get(svc_name(ver)).remove(force=True)
    except docker.errors.NotFound:
        pass
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"删除容器失败：{e}")

    if purge:
        tasks.log(tid, "删除数据卷（不可恢复）…")
        try:
            client.volumes.get(vol_name(ver)).remove()
        except docker.errors.NotFound:
            pass
        except docker.errors.APIError as e:
            raise HTTPException(status_code=502, detail=f"删除数据卷失败：{e}")

    row = db.get(MySqlService, ver)
    db.delete(row)
    db.commit()
    return {"ok": True, "detail": f"MySQL {ver} 已停用" + ("，数据卷已删除" if purge else "（数据卷保留）")}


def enabled_versions(db: Session) -> list[str]:
    """已启用的 MySQL 版本（面板建库下拉用）。"""
    return [r.version for r in db.query(MySqlService.version).all()]


def list_services(db: Session) -> list[dict]:
    """版本 = 节点已拉取的 mysql 镜像 tag ∪ 已启用版本（管理员视图）。
    enabled / running / host_port / 库数量全量列出。"""
    client = None
    try:
        client = get_docker()
    except HTTPException:
        pass
    pulled = _pulled_versions(client) if client is not None else set()
    enabled = {r.version for r in db.query(MySqlService.version).all()}
    versions = sorted(pulled | enabled, key=_ver_sort_key)
    counts: dict[str, int] = {}
    for r in db.query(InstanceDb.version, InstanceDb.id).all():
        counts[r.version] = counts.get(r.version, 0) + 1
    rows = []
    for ver in versions:
        svc_row = db.get(MySqlService, ver)
        item = {
            "version": ver,
            "image": f"mysql:{ver}",
            "enabled": svc_row is not None,
            "running": False,
            "host_port": svc_row.host_port if svc_row else None,
            "db_count": counts.get(ver, 0),
        }
        if client is not None and svc_row:
            try:
                item["running"] = client.containers.get(svc_name(ver)).status == "running"
            except docker.errors.NotFound:
                item["running"] = False
            except docker.errors.APIError:
                item["running"] = False
        rows.append(item)
    return rows


# ---------- 实例数据库（面板 / 实例回收联动） ----------

def _conn_info(db: Session, row: InstanceDb) -> dict:
    """连接信息：内网 host=容器名（实例容器 join 同一网络直连）；外网端口由前端拼 hostname。"""
    return {
        "version": row.version,
        "db_name": row.db_name,
        "db_user": row.db_user,
        "db_password": row.db_password,
        "internal_host": svc_name(row.version),
        "internal_port": 3306,
        "host_port": _host_port_of(db, row.version),
    }


def _host_port_of(db: Session, ver: str) -> int | None:
    row = db.get(MySqlService, ver)
    return row.host_port if row else None


def create_instance_db(db: Session, inst: Instance, ver: str) -> dict:
    """一实例一库：db=ppanel_{iid}，user=ppanel_u{iid}，密码随机（无引号注入风险）。
    版本必须已启用 MySQL 服务（未启用由 _get_svc_row 拦截）。"""
    _get_svc_row(db, ver)
    if db.query(InstanceDb).filter(InstanceDb.instance_id == inst.id).first():
        raise HTTPException(status_code=409, detail="该实例已有数据库，如需更换请先删除数据库")

    import secrets
    db_name = f"ppanel_{inst.id}"
    db_user = f"ppanel_u{inst.id}"
    db_password = secrets.token_urlsafe(18)
    _sql(ver, _get_svc_row(db, ver).root_password,
         f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; "
         f"CREATE USER IF NOT EXISTS '{db_user}'@'%' IDENTIFIED BY '{db_password}'; "
         # 用户可能因"停用服务保留数据卷→重新启用"而残留，密码必须与登记一致
         f"ALTER USER '{db_user}'@'%' IDENTIFIED BY '{db_password}'; "
         f"GRANT ALL PRIVILEGES ON `{db_name}`.* TO '{db_user}'@'%'; FLUSH PRIVILEGES;")

    row = InstanceDb(instance_id=inst.id, version=ver, db_name=db_name,
                     db_user=db_user, db_password=db_password)
    db.add(row)
    db.commit()
    return _conn_info(db, row)


def get_instance_db(db: Session, inst: Instance) -> dict | None:
    row = db.query(InstanceDb).filter(InstanceDb.instance_id == inst.id).first()
    if not row:
        return None
    info = _conn_info(db, row)
    info["service_running"] = False
    try:
        info["service_running"] = _svc_container(row.version).status == "running"
    except HTTPException:
        pass
    return info


def reset_db_password(db: Session, inst: Instance) -> dict:
    row = db.query(InstanceDb).filter(InstanceDb.instance_id == inst.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="该实例尚未开通数据库")
    svc_row = _get_svc_row(db, row.version)
    import secrets
    new_pwd = secrets.token_urlsafe(18)
    _sql(row.version, svc_row.root_password,
         f"ALTER USER '{row.db_user}'@'%' IDENTIFIED BY '{new_pwd}'; FLUSH PRIVILEGES;")
    row.db_password = new_pwd
    db.commit()
    return _conn_info(db, row)


def _exec(c, cmd: list) -> tuple[int, str]:
    try:
        r = c.exec_run(cmd)
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"容器执行失败：{e}")
    out = r.output.decode("utf-8", "replace") if isinstance(r.output, bytes) else str(r.output)
    return r.exit_code, out


def _table_count(ver: str, root_password: str, db_name: str) -> int:
    code, out = _exec(_svc_container(ver), [
        "mysql", "-uroot", f"--password={root_password}", "-N", "-e",
        f"SELECT COUNT(*) FROM information_schema.tables "
        f"WHERE table_schema='{db_name}' AND table_type='BASE TABLE'"])
    if code != 0:
        raise HTTPException(status_code=502, detail=f"查询 {ver} 表数量失败")
    lines = [l.strip() for l in out.splitlines() if l.strip() and "Warning" not in l]
    return int(lines[-1]) if lines else 0


def switch_instance_db(db: Session, inst: Instance, target_ver: str) -> dict:
    """切换数据库版本（带数据迁移）：导出旧库 → 目标容器建库导入 → 表数校验 → 删旧库。
    任何一步失败都保留原库原密码，可重试。"""
    row = db.query(InstanceDb).filter(InstanceDb.instance_id == inst.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="该实例尚未开通数据库")
    if target_ver == row.version:
        raise HTTPException(status_code=400, detail="已在该版本上，请选择其他版本")

    src_row = _get_svc_row(db, row.version)
    dst_row = _get_svc_row(db, target_ver)
    src_c = _svc_container(row.version)
    dst_c = _svc_container(target_ver)
    if src_c.status != "running":
        raise HTTPException(status_code=502, detail="当前版本的 MySQL 服务未运行，无法导出")
    if dst_c.status != "running":
        raise HTTPException(status_code=502, detail=f"MySQL {target_ver} 服务未运行")

    import secrets
    new_pwd = secrets.token_urlsafe(18)

    # 1) 旧容器内导出到临时文件（mysqldump，exit_code 可校验）
    code, out = _exec(src_c, ["sh", "-c",
        f"mysqldump -uroot --password='{src_row.root_password}' --single-transaction "
        f"--routines --triggers {row.db_name} > /tmp/ppanel_switch_dump.sql"])
    if code != 0:
        lines = [l for l in out.splitlines() if "command line interface" not in l.lower()]
        raise HTTPException(status_code=502, detail=f"导出旧库失败：{''.join(lines).strip()[-300:]}")

    # 2) 目标容器建库 + 建同名账号（沿用 db_name/db_user，应用只改 host）
    try:
        _sql(target_ver, dst_row.root_password,
             f"CREATE DATABASE IF NOT EXISTS `{row.db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; "
             f"CREATE USER IF NOT EXISTS '{row.db_user}'@'%' IDENTIFIED BY '{new_pwd}'; "
             f"GRANT ALL PRIVILEGES ON `{row.db_name}`.* TO '{row.db_user}'@'%'; FLUSH PRIVILEGES;")
    except HTTPException:
        _exec(src_c, ["sh", "-c", "rm -f /tmp/ppanel_switch_dump.sql"])
        raise

    # 3) 旧容器经内网把数据泵入目标容器（两容器同在 ppanel 网络，容器名可解析）
    code, out = _exec(src_c, ["sh", "-c",
        f"mysql -h{svc_name(target_ver)} -uroot --password='{dst_row.root_password}' "
        f"{row.db_name} < /tmp/ppanel_switch_dump.sql"])
    if code != 0:
        # 清理半成品目标库，保留原库
        try:
            _sql(target_ver, dst_row.root_password,
                 f"DROP DATABASE IF EXISTS `{row.db_name}`; DROP USER IF EXISTS '{row.db_user}'@'%';")
        except HTTPException:
            pass
        _exec(src_c, ["sh", "-c", "rm -f /tmp/ppanel_switch_dump.sql"])
        lines = [l for l in out.splitlines() if "command line interface" not in l.lower()]
        raise HTTPException(status_code=502,
                            detail=f"导入 MySQL {target_ver} 失败（原库未动）：{''.join(lines).strip()[-300:]}")

    # 4) 表数一致性校验（空库也算通过）
    old_n = _table_count(row.version, src_row.root_password, row.db_name)
    new_n = _table_count(target_ver, dst_row.root_password, row.db_name)
    _exec(src_c, ["sh", "-c", "rm -f /tmp/ppanel_switch_dump.sql"])
    if new_n != old_n:
        try:
            _sql(target_ver, dst_row.root_password,
                 f"DROP DATABASE IF EXISTS `{row.db_name}`; DROP USER IF EXISTS '{row.db_user}'@'%';")
        except HTTPException:
            pass
        raise HTTPException(status_code=502,
                            detail=f"迁移校验失败：旧库 {old_n} 张表 / 新库 {new_n} 张表，已保留原库，请重试")

    # 5) 删旧库旧账号（保留 dump 清理的 best-effort 已完成）
    try:
        _sql(row.version, src_row.root_password,
             f"DROP DATABASE IF EXISTS `{row.db_name}`; DROP USER IF EXISTS '{row.db_user}'@'%';")
    except HTTPException:
        pass  # 原库删除失败不阻塞切换（新库已就绪且校验通过），残留由停用服务时兜底

    # 6) 更新发放记录（新密码）
    row.version = target_ver
    row.db_password = new_pwd
    db.commit()
    return _conn_info(db, row)


def drop_instance_db(db: Session, inst: Instance) -> None:
    """删除实例的数据库（实例删除/商城回收联动）。尽力而为：MySQL 不可用不阻塞实例回收。"""
    row = db.query(InstanceDb).filter(InstanceDb.instance_id == inst.id).first()
    if not row:
        return
    try:
        svc_row = db.get(MySqlService, row.version)
        if svc_row:
            _sql(row.version, svc_row.root_password,
                 f"DROP DATABASE IF EXISTS `{row.db_name}`; DROP USER IF EXISTS '{row.db_user}'@'%';")
    except Exception:  # noqa: BLE001 服务不在/容器不在：仅回滚本地记录
        pass
    db.delete(row)
    db.commit()
