"""实例服务：容器创建/启停/删除、端口分配、依赖安装任务、状态同步。"""
import json
import os
import queue
import random
import socket
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone

import docker
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.docker_client import get_docker, try_get_docker
from app.models import AgentMetric, AgentOpLog, Instance

CONTAINER_PREFIX = "ppanel-"

# 依赖安装任务注册表：job_id -> ExecJob（内存态，重启面板即清空）
EXEC_JOBS: dict[str, "ExecJob"] = {}


def container_prefix() -> str:
    """本进程管理的容器名前缀：被控可通过 CONTAINER_PREFIX 环境变量覆盖（如 pagent-）。"""
    import os
    return os.environ.get("CONTAINER_PREFIX", "ppanel-")


def container_name(instance_id: int) -> str:
    # 被控进程可通过 CONTAINER_PREFIX 环境变量换前缀（如 pagent-），
    # 避免与同宿主机上旧版面板的容器撞名
    return f"{container_prefix()}{instance_id}"


def host_dir_for(instance_id: int) -> str:
    return os.path.join(settings.data_root, str(instance_id))


def volume_src_for(inst: Instance) -> str:
    """容器挂载源路径：跨系统部署时可与面板本机路径不同根。"""
    if settings.docker_volume_root:
        return f"{settings.docker_volume_root.rstrip('/')}/{inst.id}"
    return inst.host_dir


def ensure_network(client: docker.DockerClient) -> None:
    """安全红线 3：所有实例容器挂在独立 network 内，与默认 bridge 隔离。"""
    try:
        client.networks.get(settings.docker_network)
    except docker.errors.NotFound:
        try:
            client.networks.create(settings.docker_network, driver="bridge")
        except docker.errors.APIError:
            pass  # 并发创建等场景，忽略


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def alloc_port(db: Session) -> int:
    """在 PORT_START-PORT_END 内随机起点扫描空闲端口，避开已分配的。"""
    used = {row[0] for row in db.query(Instance.ext_port).all()}
    total = settings.port_end - settings.port_start + 1
    offset = random.randrange(total)
    for i in range(total):
        port = settings.port_start + (offset + i) % total
        if port in used:
            continue
        if _port_free(port):
            return port
    raise HTTPException(status_code=503, detail="外部端口已耗尽，无可用端口")


def ensure_image(client: docker.DockerClient, image: str) -> None:
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        try:
            client.images.pull(image)
        except docker.errors.APIError as e:
            raise HTTPException(status_code=502, detail=f"拉取镜像失败：{e}")


def _entry_cmd(inst: Instance) -> list[str]:
    """入口命令：有 main.py 跑用户命令；没有则空闲模式常驻（环境可用，随传随跑）。"""
    if os.path.exists(os.path.join(inst.host_dir, "main.py")):
        return ["/bin/sh", "-c", inst.start_cmd or "python main.py"]
    return ["/bin/sh", "-c",
            "echo '[空闲模式] 未检测到 main.py：环境已就绪，上传代码后点重启即可运行' && sleep infinity"]


def create_container(inst: Instance) -> str:
    """安全红线 2：CPU/内存/memswap/pids 全部硬限制，绝不 privileged。"""
    client = get_docker()
    ensure_network(client)
    try:
        container = client.containers.create(
            image=inst.image,
            name=container_name(inst.id),
            command=_entry_cmd(inst),
            working_dir="/app",
            volumes={volume_src_for(inst): {"bind": "/app", "mode": "rw"}},
            ports={f"{settings.inner_app_port}/tcp": inst.ext_port},
            nano_cpus=int(inst.cpu_limit * 1e9),
            mem_limit=inst.mem_limit * 1024 * 1024,
            memswap_limit=inst.mem_limit * 1024 * 1024,  # swap=mem，杜绝内存超额
            pids_limit=settings.pids_limit,
            restart_policy={"Name": "unless-stopped"},
            privileged=False,
            network=settings.docker_network,
            environment={"PYTHONUNBUFFERED": "1"},  # 用户代码 print 实时进容器日志
            detach=True,
            stdin_open=False,
            tty=False,
        )
        return container.id
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"创建容器失败：{e}")


def recreate_container(inst: Instance) -> None:
    """启动命令变更后重建容器（保留目录与端口）。"""
    client = get_docker()
    try:
        c = client.containers.get(container_name(inst.id))
        if c.status == "running":
            raise HTTPException(status_code=409, detail="实例运行中，请先停止再修改启动命令")
        c.remove(force=True)
    except docker.errors.NotFound:
        pass
    inst.container_id = create_container(inst)


def get_container(inst: Instance) -> docker.models.containers.Container:
    client = get_docker()
    try:
        return client.containers.get(container_name(inst.id))
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="容器不存在，请尝试重新启动实例以自动重建")
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")


def sync_status(inst: Instance) -> None:
    """用 Docker 真实状态校正 DB 里的 status（Docker 不可用时保持原值）。"""
    client = try_get_docker()
    if client is None:
        return
    try:
        c = client.containers.get(container_name(inst.id))
    except docker.errors.NotFound:
        return
    except docker.errors.APIError:
        return
    new_status = "running" if c.status == "running" else "exited"
    if inst.status != new_status:
        inst.status = new_status
    if inst.container_id != c.id:
        inst.container_id = c.id


def container_stats(inst: Instance) -> dict:
    """单次采样 CPU/内存占用（docker stats no-stream 语义）。"""
    empty = {"running": False, "cpu_percent": 0.0, "mem_usage_mb": 0.0,
             "mem_limit_mb": inst.mem_limit, "mem_percent": 0.0,
             "net_rx_mb": 0.0, "net_tx_mb": 0.0}
    client = try_get_docker()
    if client is None:
        return empty
    try:
        c = client.containers.get(container_name(inst.id))
    except docker.errors.NotFound:
        return empty
    except docker.errors.APIError:
        return empty
    if c.status != "running":
        return empty
    try:
        s = c.stats(stream=False)
    except docker.errors.APIError:
        return empty

    cpu = s.get("cpu_stats", {})
    pre = s.get("precpu_stats", {})
    cpu_delta = cpu.get("cpu_total_usage", 0) - pre.get("cpu_total_usage", 0)
    sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
    online = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage", []) or []) or 1
    cpu_percent = (cpu_delta / sys_delta * online * 100) if sys_delta > 0 else 0.0

    mem = s.get("memory_stats", {})
    usage = max(mem.get("usage", 0) - mem.get("stats", {}).get("cache", 0), 0)
    limit = mem.get("limit") or inst.mem_limit * 1024 * 1024
    mem_mb = round(usage / 1024 / 1024, 1)
    limit_mb = round(limit / 1024 / 1024, 1)
    net = s.get("networks") or {}
    rx = sum((v.get("rx_bytes") or 0) for v in net.values())
    tx = sum((v.get("tx_bytes") or 0) for v in net.values())
    return {
        "running": True,
        "cpu_percent": round(cpu_percent, 1),
        "mem_usage_mb": mem_mb,
        "mem_limit_mb": limit_mb,
        "mem_percent": round(usage / limit * 100, 1) if limit else 0.0,
        "net_rx_mb": round(rx / 1024 / 1024, 2),
        "net_tx_mb": round(tx / 1024 / 1024, 2),
    }


def container_logs(inst: Instance, tail: int = 200) -> str:
    c = get_container(inst)
    try:
        raw = c.logs(tail=max(1, min(tail, 5000)), stdout=True, stderr=True, timestamps=False)
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"读取日志失败：{e}")
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return b"".join(raw).decode("utf-8", "replace")


def diagnose_startup(inst: Instance, wait: float = 2.0, interval: float = 0.5) -> None:
    """启动后短暂观察：容器若在观察期内未能保持 running，则直接把原因带回给用户，
    避免“点了启动却看不到任何反应”。缺文件、语法错误等会立刻暴露。"""
    import time as _t

    client = try_get_docker()
    if client is None:
        return
    t0 = int(_t.time())
    end = t0 + wait
    c = None
    while _t.time() < end:
        _t.sleep(interval)
        try:
            c = client.containers.get(container_name(inst.id))
        except docker.errors.NotFound:
            return
        except docker.errors.APIError:
            return
        if c.status == "running":
            return
    if c is not None and c.status in ("exited", "restarting"):
        try:
            # 只取本次启动之后的新日志，避免历史错误刷屏
            raw = c.logs(stdout=True, stderr=True, since=max(t0 - 2, 0), tail=15)
            text = (raw.decode("utf-8", "replace") if isinstance(raw, bytes)
                    else b"".join(raw).decode("utf-8", "replace"))
            tail = text.strip() or "(本次启动无新日志)"
        except Exception:  # noqa: BLE001
            tail = "(日志读取失败)"
        raise HTTPException(
            status_code=400,
            detail=f"容器未能保持运行（状态：{c.status}）。本次启动日志：\n{tail}",
        )


def _env_ok(c: docker.models.containers.Container) -> bool:
    return "PYTHONUNBUFFERED=1" in (c.attrs.get("Config", {}).get("Env") or [])


def _ensure_entry_cmd(client: docker.DockerClient, inst: Instance,
                      c: docker.models.containers.Container):
    """入口命令/环境随目录内容变化：非运行中时按需重建容器（目录/端口/配置保留）。"""
    stale = (c.attrs.get("Config", {}).get("Cmd") != _entry_cmd(inst) or not _env_ok(c))
    if stale and c.status != "running":
        c.remove(force=True)
        inst.container_id = create_container(inst)
        c = client.containers.get(container_name(inst.id))
    return c


def start_instance(inst: Instance) -> None:
    client = get_docker()
    name = container_name(inst.id)
    try:
        c = client.containers.get(name)
    except docker.errors.NotFound:
        # 容器被手动删除但目录还在：按当前配置自动重建
        inst.container_id = create_container(inst)
        c = client.containers.get(name)
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")
    c = _ensure_entry_cmd(client, inst, c)
    try:
        if c.status == "restarting":
            # 失败退避循环中：先停再起，打断 backoff 立即重跑（否则新上传的代码要等退避窗口）
            c.stop(timeout=5)
        c.start()
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"启动失败：{e}")
    inst.status = "running"


def stop_instance(inst: Instance) -> None:
    c = get_container(inst)
    try:
        c.stop(timeout=10)
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"停止失败：{e}")
    inst.status = "exited"


def restart_instance(inst: Instance) -> None:
    """重启 = 停止后按当前目录内容重建入口（main.py 增删/代码更新都生效）再启动。"""
    client = get_docker()
    c = get_container(inst)
    try:
        c.stop(timeout=10)
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"重启失败：{e}")
    c = _ensure_entry_cmd(client, inst, client.containers.get(container_name(inst.id)))
    try:
        c.start()
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"重启失败：{e}")
    inst.status = "running"


def remove_container(inst: Instance) -> None:
    client = try_get_docker()
    if client is None:
        return
    try:
        c = client.containers.get(container_name(inst.id))
        c.stop(timeout=5)
    except docker.errors.NotFound:
        return
    except docker.errors.APIError:
        pass  # 停不掉也要尝试删除
    try:
        client.containers.get(container_name(inst.id)).remove(force=True)
    except (docker.errors.NotFound, docker.errors.APIError):
        pass


# ---------- 依赖安装 ----------

class ExecJob:
    def __init__(self, instance_id: int, file: str, cmd: list | None = None):
        self.id = uuid.uuid4().hex
        self.instance_id = instance_id
        self.file = file
        self.cmd = cmd or ["pip", "install", "-r", file]
        self.lines: deque[str] = deque(maxlen=8000)
        self.done = False
        self.exit_code: int | None = None

    def emit(self, text: str) -> None:
        self.lines.append(text)


def _pip_container(inst: Instance):
    """依赖操作前置：容器存在且运行中。"""
    client = get_docker()
    try:
        c = client.containers.get(container_name(inst.id))
    except docker.errors.NotFound:
        raise HTTPException(status_code=409, detail="容器未运行，请先启动实例")
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")
    if c.status != "running":
        raise HTTPException(status_code=409, detail="容器未运行，请先启动实例")
    return c


def start_install(inst: Instance, file: str) -> ExecJob:
    """容器必须处于运行状态才能安装依赖。"""
    c = _pip_container(inst)

    # 清理同实例已完成的历史任务，保持注册表精简
    for jid in [j for j, job in EXEC_JOBS.items() if job.instance_id == inst.id and job.done]:
        EXEC_JOBS.pop(jid, None)

    job = ExecJob(inst.id, file)
    EXEC_JOBS[job.id] = job
    threading.Thread(target=_exec_worker, args=(job,), daemon=True).start()
    return job


def start_pip(inst: Instance, args: list) -> ExecJob:
    """手动 pip 操作（安装指定包 / 卸载），实时输出走 ExecJob。"""
    _pip_container(inst)
    for jid in [j for j, job in EXEC_JOBS.items() if job.instance_id == inst.id and job.done]:
        EXEC_JOBS.pop(jid, None)
    job = ExecJob(inst.id, " ".join(args), cmd=["pip", *args])
    EXEC_JOBS[job.id] = job
    threading.Thread(target=_exec_worker, args=(job,), daemon=True).start()
    return job


def pip_list(inst: Instance) -> list:
    """已安装依赖列表（pip list --format=json）。"""
    c = _pip_container(inst)
    client = get_docker()
    exec_id = client.api.exec_create(
        c.id, ["pip", "list", "--format=json"], workdir="/app")["Id"]
    out = client.api.exec_start(exec_id)
    inspect = client.api.exec_inspect(exec_id)
    if inspect.get("ExitCode") != 0:
        raise HTTPException(status_code=500, detail="获取依赖列表失败")
    raw = out.decode("utf-8", "replace")
    # demux 未开时 stderr（如 pip [notice]）会混入 stdout，用 raw_decode 取第一个合法 JSON 值
    start = raw.find("[")
    data = None
    if start != -1:
        try:
            data, _ = json.JSONDecoder().raw_decode(raw[start:])
        except ValueError:
            data = None
    if not isinstance(data, list):
        raise HTTPException(status_code=500, detail="解析依赖列表失败")
    return [{"name": d.get("name", ""), "version": d.get("version", "")} for d in data]


def _exec_worker(job: "ExecJob") -> None:
    try:
        client = get_docker()
        c = client.containers.get(container_name(job.instance_id))
        if c.status != "running":
            job.emit("[面板] 容器已停止，安装中止")
            job.exit_code = -1
            return
        exec_id = client.api.exec_create(
            c.id, job.cmd, workdir="/app"
        )["Id"]
        job.emit(f"[面板] 执行：pip {' '.join(job.cmd[1:])}")
        stream = client.api.exec_start(exec_id, stream=True, demux=True)
        for out, err in stream:
            if out:
                job.emit(out.decode("utf-8", "replace"))
            if err:
                job.emit(err.decode("utf-8", "replace"))
        inspect = client.api.exec_inspect(exec_id)
        job.exit_code = inspect.get("ExitCode")
        job.emit(f"[面板] 任务完成，退出码 {job.exit_code}"
                 + ("（成功）" if job.exit_code == 0 else "（失败）"))
    except HTTPException as e:
        job.emit(f"[面板] 执行出错：{e.detail}")
        job.exit_code = -1
    except Exception as e:  # noqa: BLE001
        job.emit(f"[面板] 执行出错：{e}")
        job.exit_code = -1
    finally:
        job.done = True


# ---------- 被控自持：操作记录 / 用量采样（供独立面板，脱离主控可用） ----------

def log_op(db: Session, instance_id: int, action: str, detail: str = "") -> None:
    db.add(AgentOpLog(instance_id=instance_id, action=action, detail=detail))


def sample_all_metrics(db: Session) -> None:
    """采样所有运行中实例的 CPU/内存/网络，存本地库；保留最近 24 小时。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for inst in db.query(Instance).filter(Instance.status == "running").all():
        try:
            s = container_stats(inst)
            db.add(AgentMetric(
                instance_id=inst.id,
                cpu_percent=s.get("cpu_percent", 0.0),
                mem_used_mb=s.get("mem_usage_mb", 0.0),
                mem_limit_mb=s.get("mem_limit_mb", inst.mem_limit),
                net_rx_mb=s.get("net_rx_mb", 0.0),
                net_tx_mb=s.get("net_tx_mb", 0.0),
            ))
        except Exception:  # noqa: BLE001 单实例失败不影响其他
            pass
    db.commit()
    db.query(AgentMetric).filter(
        AgentMetric.ts < now - timedelta(hours=24)).delete()
    db.commit()
