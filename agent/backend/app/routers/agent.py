"""被控 Agent REST API（前缀 /agent，节点级鉴权）：
health 心跳、实例环境生命周期、实例文件、Docker 运维（复用 docker_admin handlers）。
"""
import os
import re as _re
import shutil
import subprocess as _sp
import time

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.config import settings
from app.database import get_db
from app.models import Instance
from app.routers import docker_admin
from app.schemas import (CopyIn, InstanceCreate, InstanceOut, InstanceUpdate,
                         MkdirIn, RenameIn, RuntimeIn, SaveIn, UnzipIn,
                         UploadExistsIn, ZipIn)
from app.services import file_service as fs
from app.services import instance_service as svc

router = APIRouter()


def _inst(instance_id: int, db: Session) -> Instance:
    inst = db.get(Instance, instance_id)
    if not inst:
        raise HTTPException(status_code=404, detail="实例不存在")
    return inst


# ---------- 心跳 ----------

@router.get("/health")
def agent_health(db: Session = Depends(get_db)):
    client = svc.try_get_docker()
    info = {"docker_ok": client is not None}
    if client is not None:
        try:
            d = client.info()
            v = client.version()
            info.update({
                "server_version": v.get("Version"),
                "os": d.get("OperatingSystem"),
                "cpus": d.get("NCPU"),
                "mem_total_gb": round((d.get("MemTotal") or 0) / 1024 ** 3, 1),
                "containers_running": d.get("ContainersRunning", 0),
                "images": d.get("Images", 0),
            })
        except Exception:  # noqa: BLE001
            info["docker_ok"] = False
    info["instances"] = db.query(Instance.id).count()
    info["port_start"] = settings.port_start
    info["port_end"] = settings.port_end
    info["python_images"] = settings.python_images
    info["php_images"] = settings.php_images
    return info


# ---------- 实例环境 ----------

@router.post("/instances")
def agent_create(body: InstanceCreate, db: Session = Depends(get_db)):
    if body.image not in svc.allowed_images():
        raise HTTPException(status_code=400, detail="不支持的镜像版本")

    client = svc.get_docker()
    svc.ensure_image(client, body.image)

    port = svc.alloc_port(db)
    # user_id=1：Agent 本地库的占位账号（Agent 无用户概念）
    inst = Instance(
        user_id=1,
        name=body.name.strip(),
        image=body.image,
        start_cmd=body.start_cmd.strip() or svc.default_start_cmd(body.image, body.mem_limit),
        ext_port=port,
        cpu_limit=body.cpu_limit,
        mem_limit=body.mem_limit,
        disk_quota=body.disk_quota,
        status="creating",
        expire_at=body.expire_at,
        traffic_gb=body.traffic_gb,
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)

    inst.host_dir = svc.host_dir_for(inst.id)
    try:
        os.makedirs(inst.host_dir, exist_ok=True)
        svc.ensure_entry_file(inst.host_dir, body.image)
        inst.container_id = svc.create_container(inst)
        inst.status = "created"
        db.commit()
    except HTTPException:
        _rollback(db, inst)
        raise
    except Exception as e:  # noqa: BLE001
        _rollback(db, inst)
        raise HTTPException(status_code=502, detail=f"创建实例失败：{e}")
    return InstanceOut.model_validate(inst).model_dump()


def _rollback(db: Session, inst: Instance) -> None:
    db.delete(inst)
    db.commit()
    if inst.host_dir:
        shutil.rmtree(inst.host_dir, ignore_errors=True)


@router.get("/instances")
def agent_list(db: Session = Depends(get_db)):
    instances = db.query(Instance).order_by(Instance.id.desc()).all()
    out = []
    for inst in instances:
        started_at = svc.sync_status(inst)
        db.commit()
        data = InstanceOut.model_validate(inst).model_dump()
        data["started_at"] = started_at  # 容器本次启动时间（running 时有值）
        out.append(data)
    return out


@router.get("/instances/{instance_id}")
def agent_detail(instance_id: int, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    started_at = svc.sync_status(inst)
    db.commit()
    data = InstanceOut.model_validate(inst).model_dump()
    data["host_dir"] = inst.host_dir
    data["started_at"] = started_at
    return data


@router.delete("/instances/{instance_id}")
def agent_delete(instance_id: int, purge: int = 0, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    host_dir = inst.host_dir
    from app.services import mysql_service as mysql_svc
    mysql_svc.drop_instance_db(db, inst)  # 联动回收实例数据库（与面板删除路径一致）
    svc.remove_container(inst)
    db.delete(inst)
    db.commit()
    if purge and host_dir:
        shutil.rmtree(host_dir, ignore_errors=True)
    return {"detail": "实例已回收", "host_dir": host_dir,
            "purged": bool(purge and host_dir)}


@router.post("/instances/{instance_id}/start")
def agent_start(instance_id: int, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    svc.start_instance(inst)
    db.commit()
    svc.diagnose_startup(inst)  # 秒退直接带回原因（缺文件/报错）
    return {"status": "running"}


@router.post("/instances/{instance_id}/stop")
def agent_stop(instance_id: int, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    svc.stop_instance(inst)
    db.commit()
    return {"status": inst.status}


@router.post("/instances/{instance_id}/restart")
def agent_restart(instance_id: int, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    svc.restart_instance(inst)
    db.commit()
    return {"status": inst.status}


@router.get("/instances/{instance_id}/runtime")
def agent_runtime(instance_id: int, db: Session = Depends(get_db)):
    """运行环境卡：当前镜像 + 节点本地已拉取的版本。"""
    inst = _inst(instance_id, db)
    return svc.runtime_versions(inst)


@router.post("/instances/{instance_id}/runtime")
def agent_switch_runtime(instance_id: int, body: RuntimeIn, db: Session = Depends(get_db)):
    """切换 Python 版本：换镜像重建容器。目录/端口/配额不变，容器内依赖需重装。"""
    inst = _inst(instance_id, db)
    return svc.switch_runtime(inst, body.image, db)


@router.patch("/instances/{instance_id}")
def agent_update(instance_id: int, body: InstanceUpdate, db: Session = Depends(get_db)):
    """主控转发来的实例编辑：start_cmd 变更需重建容器。"""
    inst = _inst(instance_id, db)
    if body.name is not None:
        inst.name = body.name.strip()
    if body.note is not None:
        inst.note = body.note
    if body.start_cmd is not None and body.start_cmd.strip() != inst.start_cmd:
        inst.start_cmd = body.start_cmd.strip()
        svc.recreate_container(inst)  # 命令烧进容器，需要重建
    db.commit()
    return InstanceOut.model_validate(inst).model_dump()


@router.get("/instances/{instance_id}/stats")
def agent_stats(instance_id: int, db: Session = Depends(get_db)):
    return svc.container_stats(_inst(instance_id, db))


@router.get("/instances/{instance_id}/logs")
def agent_logs(instance_id: int, tail: int = 200, db: Session = Depends(get_db)):
    return {"logs": svc.container_logs(_inst(instance_id, db), tail)}


@router.post("/instances/{instance_id}/deps/install")
def agent_install(instance_id: int, file: str = "requirements.txt",
                  db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    if "/" in file or "\\" in file or ".." in file or not file:
        raise HTTPException(status_code=400, detail="非法文件名")
    job = svc.start_install(inst, file)
    return {"job_id": job.id}


@router.post("/instances/{instance_id}/panel-token")
def agent_panel_token(instance_id: int, reset: int = 0, db: Session = Depends(get_db)):
    """生成/重置独立面板令牌：主控或开通系统据此下发单容器面板入口。"""
    import secrets as _secrets

    inst = _inst(instance_id, db)
    if reset or not inst.panel_token:
        inst.panel_token = _secrets.token_urlsafe(24)
        db.commit()
    return {"panel_token": inst.panel_token}


@router.delete("/instances/{instance_id}/panel-token")
def agent_panel_revoke(instance_id: int, db: Session = Depends(get_db)):
    """吊销面板令牌：清空后所有已发面板凭证立即失效。"""
    inst = _inst(instance_id, db)
    inst.panel_token = None
    db.commit()
    return {"detail": "面板令牌已吊销"}


# ---------- 实例文件（Agent 本机路径直接操作） ----------

def _root(inst: Instance) -> str:
    fs.ensure_root(inst.host_dir)
    return inst.host_dir


@router.get("/instances/{instance_id}/files")
def agent_files(instance_id: int, path: str = "/", db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    entries = fs.list_dir(_root(inst), path)
    return {"path": path, "entries": entries}


@router.get("/instances/{instance_id}/files/content")
def agent_file_content(instance_id: int, path: str, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    data = fs.read_text(_root(inst), path)
    data["path"] = path
    return data


@router.put("/instances/{instance_id}/files/upload")
def agent_upload(instance_id: int, path: str = "/",
                 file: UploadFile = None, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    if file is None:
        raise HTTPException(status_code=400, detail="未收到文件")
    limit = settings.upload_limit_mb * 1024 * 1024
    return fs.save_upload(_root(inst), path, file.filename, file.file, limit)


@router.post("/instances/{instance_id}/files/save")
def agent_save(instance_id: int, body: SaveIn, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    fs.write_text(_root(inst), body.path, body.content)
    return {"detail": "已保存"}


@router.post("/instances/{instance_id}/files/mkdir")
def agent_mkdir(instance_id: int, body: MkdirIn, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    fs.mkdir(_root(inst), body.path)
    return {"detail": "已创建"}


@router.post("/instances/{instance_id}/files/rename")
def agent_rename(instance_id: int, body: RenameIn, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    fs.rename(_root(inst), body.src, body.dst)
    return {"detail": "已重命名"}


@router.delete("/instances/{instance_id}/files")
def agent_delete_path(instance_id: int, path: str, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    fs.delete(_root(inst), path)
    return {"detail": "已删除"}


@router.get("/instances/{instance_id}/files/download")
def agent_download(instance_id: int, path: str, db: Session = Depends(get_db)):
    from fastapi.responses import FileResponse
    inst = _inst(instance_id, db)
    root = _root(inst)
    target = fs.resolve_path(root, path)
    if not os.path.isfile(target):
        raise HTTPException(status_code=400, detail="仅支持下载文件")
    return FileResponse(target, filename=os.path.basename(target))


@router.get("/instances/{instance_id}/files/backup")
def agent_backup(instance_id: int, db: Session = Depends(get_db)):
    """整目录打包下载：响应发送完毕后后台清理临时包。"""
    from fastapi.responses import FileResponse
    inst = _inst(instance_id, db)
    tmp_path = fs.create_backup(_root(inst))
    fname = f"instance_{inst.id}_backup_{time.strftime('%Y%m%d_%H%M%S')}.tar.gz"
    return FileResponse(
        tmp_path, filename=fname, media_type="application/gzip",
        background=BackgroundTask(_cleanup, tmp_path))


def _cleanup(path: str) -> None:
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@router.post("/instances/{instance_id}/files/restore")
def agent_restore(instance_id: int, file: UploadFile = None,
                  db: Session = Depends(get_db)):
    """从 tar.gz 恢复实例目录：运行中禁止（避免文件边写边换）。"""
    inst = _inst(instance_id, db)
    if file is None:
        raise HTTPException(status_code=400, detail="未收到文件")
    svc.sync_status(inst)
    if inst.status == "running":
        raise HTTPException(status_code=409, detail="实例运行中，请先停止后再恢复")
    return fs.restore_backup(_root(inst), file.file, inst.disk_quota * 1024 * 1024)


@router.post("/instances/{instance_id}/files/copy")
def agent_copy(instance_id: int, body: CopyIn, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    fs.copy_path(_root(inst), body.src, body.dst)
    return {"detail": "已复制"}


@router.post("/instances/{instance_id}/files/zip")
def agent_zip(instance_id: int, body: ZipIn, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    fs.zip_entries(_root(inst), body.paths, body.name, body.z_type)
    return {"detail": "压缩完成"}


@router.post("/instances/{instance_id}/files/unzip")
def agent_unzip(instance_id: int, body: UnzipIn, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    dest = fs.unzip_entry(_root(inst), body.src)
    return {"detail": "解压完成", "dest": dest}


@router.get("/instances/{instance_id}/files/path-size")
def agent_path_size(instance_id: int, path: str, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    return {"size": fs.dir_size(_root(inst), path)}


@router.get("/instances/{instance_id}/files/preview")
def agent_preview(instance_id: int, path: str, db: Session = Depends(get_db)):
    inst = _inst(instance_id, db)
    return fs.image_preview(_root(inst), path)


@router.post("/instances/{instance_id}/files/upload-exists")
def agent_upload_exists(instance_id: int, body: UploadExistsIn,
                        db: Session = Depends(get_db)):
    """分片上传断点查询：返回续传起点。"""
    inst = _inst(instance_id, db)
    return fs.upload_exists(_root(inst), body.path, body.file_name, body.total_size)


@router.post("/instances/{instance_id}/files/upload-chunk")
async def agent_upload_chunk(instance_id: int, file: UploadFile = None,
                             path: str = Form("/"), file_name: str = Form(...),
                             total_size: int = Form(0), start: int = Form(0),
                             db: Session = Depends(get_db)):
    """分片上传：按 start 偏移追加写入（start=0 截断新建）。"""
    inst = _inst(instance_id, db)
    if file is None:
        raise HTTPException(status_code=400, detail="未收到分片数据")
    return fs.upload_chunk(_root(inst), path, file_name, total_size, start, file.file)


# ---------- Docker 运维（复用 docker_admin 的 handler，鉴权由 router 级 require_node 承担） ----------

router.get("/docker/info")(docker_admin.docker_info)
router.get("/docker/containers")(docker_admin.list_containers)
router.post("/docker/containers/{cid}/start")(docker_admin.container_start)
router.post("/docker/containers/{cid}/stop")(docker_admin.container_stop)
router.post("/docker/containers/{cid}/restart")(docker_admin.container_restart)
router.delete("/docker/containers/{cid}")(docker_admin.container_remove)
router.get("/docker/containers/{cid}/logs")(docker_admin.container_logs)
router.get("/docker/images")(docker_admin.list_images)
router.delete("/docker/images/{ref:path}")(docker_admin.remove_image)
router.post("/docker/images/prune")(docker_admin.prune_images)
router.post("/docker/images/pull")(docker_admin.pull_image)
router.get("/docker/settings")(docker_admin.get_settings)
router.put("/docker/settings")(docker_admin.put_settings)
router.get("/docker/df")(docker_admin.docker_df)
router.post("/docker/system/prune")(docker_admin.system_prune)
router.post("/docker/php-images/build")(docker_admin.php_image_build)
router.get("/docker/php-images/build/{job_id}")(docker_admin.php_image_build_status)
router.get("/docker/mysql")(docker_admin.mysql_list)
router.post("/docker/mysql/{ver}/enable")(docker_admin.mysql_enable)
router.post("/docker/mysql/{ver}/disable")(docker_admin.mysql_disable)
router.get("/docker/mysql/{ver}/root-password")(docker_admin.mysql_root_password)


# ---------- 宿主机资源仪表盘（/proc 零依赖采集，仅 Linux） ----------

def _read_proc(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _cpu_percent_sample():
    """两次 /proc/stat 采样算 CPU 使用率（阻塞约 0.25s）。"""
    def times():
        first = _read_proc("/proc/stat").splitlines()
        if not first or not first[0].startswith("cpu "):
            return None
        vals = [int(x) for x in first[0].split()[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return idle, sum(vals)

    t1 = times()
    if t1 is None:
        return None, None
    time.sleep(0.25)
    t2 = times()
    if t2 is None:
        return None, None
    dt, di = t2[1] - t1[1], t2[0] - t1[0]
    if dt <= 0:
        return None, None
    cores = sum(1 for l in _read_proc("/proc/stat").splitlines()
                if l.startswith("cpu") and l[3:4].isdigit())
    return round((1 - di / dt) * 100, 1), (cores or None)


def host_stats():
    """宿主机资源快照：CPU / 内存 / 磁盘 / 负载 / 运行时长 / 容器概况。"""
    cpu, cores = _cpu_percent_sample()

    mem = None
    info = {}
    for line in _read_proc("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        parts = v.split()
        if parts:
            try:
                info[k.strip()] = int(parts[0]) * 1024
            except ValueError:
                pass
    total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    if total:
        mem = {"total": total, "used": total - avail,
               "percent": round((total - avail) / total * 100, 1)}

    disk = None
    try:
        u = shutil.disk_usage(settings.data_root)
        disk = {"total": u.total, "used": u.used,
                "percent": round(u.used / u.total * 100, 1)}
    except OSError:
        pass

    load = _read_proc("/proc/loadavg").split()[:3] or None

    uptime = None
    up = _read_proc("/proc/uptime").split()
    if up:
        try:
            uptime = int(float(up[0]))
        except ValueError:
            pass

    cont = None
    try:
        client = svc.try_get_docker()
        if client:
            i = client.info()
            cont = {"running": i.get("ContainersRunning", 0),
                    "paused": i.get("ContainersPaused", 0),
                    "stopped": i.get("ContainersStopped", 0),
                    "images": i.get("Images", 0),
                    "total": i.get("Containers", 0)}
    except Exception:  # noqa: BLE001
        pass

    return {"cpu": {"percent": cpu, "cores": cores}, "mem": mem, "disk": disk,
            "load": load, "uptime": uptime, "containers": cont}


router.get("/host/stats")(host_stats)


# ---------- 防火墙管理（ufw / firewalld 自动探测，subprocess 列表参数防注入） ----------

def _resolve(binname: str) -> str:
    """systemd 环境 PATH 可能缺 /usr/sbin，兜底常见 sbin 路径。"""
    p = shutil.which(binname)
    if p:
        return p
    for pre in ("/usr/sbin", "/sbin", "/usr/local/sbin"):
        cand = f"{pre}/{binname}"
        if os.path.exists(cand):
            return cand
    return binname


def _run(cmd: list, timeout: int = 15):
    try:
        p = _sp.run([_resolve(cmd[0])] + cmd[1:], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "command not found"
    except _sp.TimeoutExpired:
        return 124, "命令超时"


@router.get("/host/firewall")
def firewall_status():
    tool = None
    rc, _ = _run(["ufw", "version"])
    if rc == 0:
        tool = "ufw"
    else:
        rc, _ = _run(["firewall-cmd", "--version"])
        if rc == 0:
            tool = "firewalld"

    rules, active, err = [], False, None
    if tool == "ufw":
        rc, out = _run(["ufw", "status", "numbered"])
        active = rc == 0 and bool(out) and "active" in out.splitlines()[0]
        if rc == 0:
            rules = _parse_ufw(out)
        else:
            err = out.strip() or "ufw 执行失败"
    elif tool == "firewalld":
        rc, out = _run(["firewall-cmd", "--state"])
        active = rc == 0 and "running" in out
        if active:
            rules = _parse_firewalld()
        else:
            err = "firewalld 服务未运行"
    return {"tool": tool, "active": active, "rules": rules, "error": err}


def _parse_ufw(out: str):
    """v4/v6 镜像规则合并为一条，nums 保留全部编号（删除时一起删）。"""
    pat = _re.compile(r"\[\s*(\d+)\]\s+(\S+)(?:\s+\(v6\))?\s+(ALLOW|DENY|REJECT|LIMIT)(?:\s+IN)?\s+(.+?)\s*$")
    order, bykey = [], {}
    for line in out.splitlines():
        m = pat.match(line)
        if not m:
            continue
        num, port, action, frm = int(m.group(1)), m.group(2), m.group(3), m.group(4)
        key = (port, action)
        if key not in bykey:
            bykey[key] = {"port": port, "action": action, "from": frm.strip(), "nums": [num]}
            order.append(bykey[key])
        else:
            bykey[key]["nums"].append(num)
    return order


def _parse_firewalld():
    rules = []
    rc, out = _run(["firewall-cmd", "--permanent", "--list-ports"])
    if rc == 0:
        for item in out.split():
            rules.append({"num": 0, "port": item, "action": "ALLOW", "from": "Anywhere"})
    rc, out = _run(["firewall-cmd", "--permanent", "--list-rich-rules"])
    if rc == 0:
        for i, line in enumerate(x.strip() for x in out.splitlines() if x.strip()):
            rules.append({"num": 0, "port": line, "action": "RULE", "from": "-"})
    return rules


def _check_port(port: str):
    """端口或端口段（30000:39999）白名单校验。"""
    if not _re.fullmatch(r"\d{1,5}(:\d{1,5})?", port or ""):
        raise HTTPException(status_code=400, detail="端口格式无效")
    parts = [int(p) for p in port.split(":")] if ":" in port else [int(port)]
    if any(p < 1 or p > 65535 for p in parts) or (len(parts) == 2 and parts[0] > parts[1]):
        raise HTTPException(status_code=400, detail="端口范围无效")


def _check_proto(proto: str):
    if proto not in ("tcp", "udp"):
        raise HTTPException(status_code=400, detail="协议仅支持 tcp/udp")


class PortRule(BaseModel):
    port: str
    proto: str = "tcp"


class RuleDelete(BaseModel):
    nums: list = []    # ufw 规则号（v4+v6 一起删）
    num: int = 0       # 兼容单条
    port: str = ""     # firewalld 用
    proto: str = "tcp"


class FwToggle(BaseModel):
    enable: bool


@router.post("/host/firewall/allow")
def firewall_allow(body: PortRule):
    _check_port(body.port)
    _check_proto(body.proto)
    if _detect_tool() == "ufw":
        rc, out = _run(["ufw", "allow", f"{body.port}/{body.proto}"])
    else:
        rc, out = _run(["firewall-cmd", "--permanent", f"--add-port={body.port}/{body.proto}"])
        if rc == 0:
            rc, out = _run(["firewall-cmd", "--reload"])
    if rc != 0:
        raise HTTPException(status_code=502, detail=(out or "执行失败").strip()[:300])
    return {"ok": True, "detail": f"已放行 {body.port}/{body.proto}"}


@router.post("/host/firewall/deny")
def firewall_deny(body: PortRule):
    _check_port(body.port)
    _check_proto(body.proto)
    if _detect_tool() == "ufw":
        rc, out = _run(["ufw", "deny", f"{body.port}/{body.proto}"])
    else:
        rc, out = _run(["firewall-cmd", "--permanent", f"--remove-port={body.port}/{body.proto}"])
        if rc == 0:
            rc, out = _run(["firewall-cmd", "--reload"])
    if rc != 0:
        raise HTTPException(status_code=502, detail=(out or "执行失败").strip()[:300])
    return {"ok": True, "detail": f"已拒绝 {body.port}/{body.proto}"}


@router.post("/host/firewall/delete")
def firewall_delete(body: RuleDelete):
    if _detect_tool() == "ufw":
        nums = [n for n in (body.nums or []) if isinstance(n, int) and n > 0]
        if body.num > 0:
            nums.append(body.num)
        if not nums:
            raise HTTPException(status_code=400, detail="缺少规则号")
        rc, out = 0, ""
        for n in sorted(nums, reverse=True):   # 从大到小删，避免编号偏移
            rc, out = _run(["ufw", "--force", "delete", str(n)])
            if rc != 0:
                break
    else:
        _check_port(body.port)
        _check_proto(body.proto)
        rc, out = _run(["firewall-cmd", "--permanent", f"--remove-port={body.port}/{body.proto}"])
        if rc == 0:
            rc, out = _run(["firewall-cmd", "--reload"])
    if rc != 0:
        raise HTTPException(status_code=502, detail=(out or "执行失败").strip()[:300])
    return {"ok": True, "detail": "规则已删除"}


@router.post("/host/firewall/toggle")
def firewall_toggle(body: FwToggle):
    if _detect_tool() == "ufw":
        rc, out = _run(["ufw", "--force", "enable" if body.enable else "disable"])
    else:
        rc, out = _run(["systemctl", "start" if body.enable else "stop", "firewalld"])
    if rc != 0:
        raise HTTPException(status_code=502, detail=(out or "执行失败").strip()[:300])
    return {"ok": True, "detail": ("防火墙已启用" if body.enable else "防火墙已停用")}


def _detect_tool():
    if _run(["ufw", "version"])[0] == 0:
        return "ufw"
    if _run(["firewall-cmd", "--version"])[0] == 0:
        return "firewalld"
    raise HTTPException(status_code=400,
                        detail="节点未安装防火墙工具（ufw / firewalld），请先安装：apt install -y ufw 或 yum install -y firewalld")
