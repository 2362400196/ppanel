"""Docker 管理中心（仅 admin / X-API-Key）：
容器管理、镜像管理（含拉取/删除）、镜像加速设置、宿主机信息。
"""
import threading
import time
from datetime import datetime

import docker
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app import tasks
from app.auth import require_admin
from app.docker_client import get_docker, reset_docker
from app.services import host_ops
from app.services.instance_service import container_prefix

router = APIRouter(prefix="/docker", tags=["docker"], dependencies=[Depends(require_admin)])


# ---------- 宿主机与 docker 概览 ----------

@router.get("/info")
def docker_info():
    client = get_docker()
    try:
        version = client.version()
        info = client.info()
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")
    return {
        "host_kind": host_ops.host_kind(),
        "server_version": version.get("Version"),
        "api_version": version.get("ApiVersion"),
        "os": f'{info.get("OperatingSystem", "")} {info.get("OSType", "")}'.strip(),
        "arch": version.get("Arch"),
        "kernel": info.get("KernelVersion"),
        "storage_driver": info.get("Driver"),
        "cpus": info.get("NCPU"),
        "mem_total_gb": round((info.get("MemTotal") or 0) / 1024 ** 3, 1),
        "containers_running": info.get("ContainersRunning", 0),
        "containers_paused": info.get("ContainersPaused", 0),
        "containers_stopped": info.get("ContainersStopped", 0),
        "images": info.get("Images", 0),
        "daemon_json_path": host_ops.DAEMON_JSON,
        "registry_mirrors": host_ops.read_daemon_json().get("registry-mirrors", []),
    }


# ---------- 容器管理 ----------

def _fmt_ports(ports) -> str:
    """兼容两种结构：列表式（/containers/json）与 inspect 的 dict 式。"""
    parts = []
    if isinstance(ports, list):
        for p in ports or []:
            pub = p.get("PublicPort")
            if pub:
                parts.append(f'{pub}->{p.get("PrivatePort")}/{p.get("Type", "tcp")}')
    elif isinstance(ports, dict):
        for inner, binds in (ports or {}).items():
            for b in binds or []:
                parts.append(f'{b.get("HostPort")}->{inner}')
    out = ", ".join(dict.fromkeys(parts))  # 去重（IPv4/IPv6 重复绑定）
    return out if out else "-"


@router.get("/containers")
def list_containers():
    client = get_docker()
    try:
        containers = client.containers.list(all=True)
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")
    rows = []
    for c in containers:
        a = c.attrs
        state = a.get("State")
        if isinstance(state, dict):
            state = state.get("Status", "")
        name = c.name or a.get("Name", "").lstrip("/") or (a.get("Names") or [""])[0].lstrip("/")
        ports = a.get("Ports")
        if not ports:
            ports = a.get("NetworkSettings", {}).get("Ports")
        # 同一宿主机可能并存旧面板（ppanel-）与被控（CONTAINER_PREFIX，如 pagent-）的实例容器
        known_prefixes = {container_prefix(), "ppanel-"}
        rows.append({
            "id": c.id[:12],
            "name": name,
            "image": a.get("Config", {}).get("Image", ""),
            "state": state or "",
            "status": c.status or a.get("Status", "") or state or "",
            "ports": _fmt_ports(ports),
            "created": a.get("Created", ""),
            "is_panel": any(name.startswith(p) for p in known_prefixes),
        })
    return rows


def _get_container(client, cid: str):
    try:
        return client.containers.get(cid)
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="容器不存在")
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")


@router.post("/containers/{cid}/start")
def container_start(cid: str):
    c = _get_container(get_docker(), cid)
    try:
        c.start()
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"启动失败：{e}")
    return {"ok": True, "detail": "已启动"}


@router.post("/containers/{cid}/stop")
def container_stop(cid: str):
    c = _get_container(get_docker(), cid)
    try:
        c.stop(timeout=10)
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"停止失败：{e}")
    return {"ok": True, "detail": "已停止"}


@router.post("/containers/{cid}/restart")
def container_restart(cid: str):
    c = _get_container(get_docker(), cid)
    try:
        c.restart(timeout=10)
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"重启失败：{e}")
    return {"ok": True, "detail": "已重启"}


@router.delete("/containers/{cid}")
def container_remove(cid: str, force: bool = False):
    client = get_docker()
    c = _get_container(client, cid)
    if c.status == "running" and not force:
        raise HTTPException(status_code=409, detail="容器运行中，请先停止或使用强制删除")
    try:
        c.remove(force=force)
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"删除失败：{e}")
    return {"ok": True, "detail": "容器已删除"}


@router.get("/containers/{cid}/logs")
def container_logs(cid: str, tail: int = 200):
    c = _get_container(get_docker(), cid)
    try:
        raw = c.logs(tail=max(1, min(tail, 5000)), stdout=True, stderr=True)
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"读取日志失败：{e}")
    if isinstance(raw, bytes):
        return {"logs": raw.decode("utf-8", "replace")}
    return {"logs": b"".join(raw).decode("utf-8", "replace")}


# ---------- 镜像管理 ----------

def _fmt_size(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    return f"{n / 1024 ** 2:.1f} MB"


def _image_row(img):
    a = img.attrs
    tags = a.get("RepoTags") or ["<none>:<none>"]
    created = a.get("Created", 0) or 0       # epoch 秒 → ISO（前端直接 new Date 解析）
    try:
        created_iso = datetime.fromtimestamp(int(created)).isoformat(timespec="seconds")
    except (ValueError, OSError, TypeError):
        created_iso = ""
    return {
        "id": a.get("Id", "").replace("sha256:", "")[:12],
        "repo": tags[0].rsplit(":", 1)[0] if ":" in tags[0] else tags[0],
        "tag": tags[0].rsplit(":", 1)[1] if ":" in tags[0] else "latest",
        "full_name": tags[0],
        "tags": tags,
        "size_bytes": a.get("Size", 0),
        "size": _fmt_size(a.get("Size", 0)),
        "created": created_iso,
    }


@router.get("/images")
def list_images():
    client = get_docker()
    try:
        images = client.images.list()
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")
    rows = [_image_row(i) for i in images]
    rows.sort(key=lambda r: r["repo"])
    return rows


@router.delete("/images/{ref:path}")
def remove_image(ref: str, force: bool = False):
    client = get_docker()
    try:
        client.images.remove(ref, force=force)
    except docker.errors.ImageNotFound:
        raise HTTPException(status_code=404, detail="镜像不存在")
    except docker.errors.APIError as e:
        msg = str(e)
        if "conflict" in msg.lower() or "in use" in msg.lower():
            raise HTTPException(status_code=409, detail="镜像正被容器使用，请先删除相关容器或使用强制删除")
        raise HTTPException(status_code=502, detail=f"删除失败：{msg}")
    return {"ok": True, "detail": f"镜像 {ref} 已删除"}


@router.post("/images/prune")
def prune_images():
    client = get_docker()
    try:
        res = client.images.prune(filters={"dangling": True})
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"清理失败：{e}")
    n = len(res.get("ImagesDeleted") or [])
    mb = round((res.get("SpaceReclaimed") or 0) / 1024 / 1024, 1)
    return {"ok": True, "detail": f"已清理 {n} 个悬空镜像，释放 {mb} MB"}


class PullReq(BaseModel):
    image: str = Field(min_length=1, max_length=200)


# 拉取进度由 /ws/docker/pull 流式输出；此接口保留给 API 调用方做阻塞式拉取
@router.post("/images/pull")
def pull_image(body: PullReq, request: Request = None):
    tid = (request.headers.get("x-task-id") if request else "") or ""
    image = body.image.strip()
    if tid:
        tasks.start(tid, f"拉取镜像 {image}")
        tasks.log(tid, "向仓库请求镜像层…")
    try:
        client = get_docker()
        try:
            img = client.images.pull(image)
        except docker.errors.ImageNotFound:
            raise HTTPException(status_code=404, detail=f"镜像 {image} 不存在于仓库")
        except docker.errors.APIError as e:
            raise HTTPException(status_code=502, detail=f"拉取失败：{e}")
        if tid:
            row = _image_row(img)
            # row["size"] 已是格式化文本（如 "123.4 MB"），直接使用；
            # 不能再传回 _fmt_size()（str >= int 会崩），数值用 row["size_bytes"]
            tasks.log(tid, f"镜像层全部就位（{row.get('size') or ''}）")
            tasks.finish(tid, True, "✔ 拉取完成")
        return {"ok": True, "detail": f"拉取完成", "image": _image_row(img)}
    except Exception as e:  # noqa: BLE001
        if tid:
            tasks.finish(tid, False, f"✘ 拉取失败：{e}")
        raise


# ---------- 磁盘占用与一键清理 ----------

@router.get("/df")
def docker_df():
    """docker system df：镜像/容器/卷/构建缓存占用。"""
    client = get_docker()
    try:
        df = client.df()
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")

    def _sum(items, key="Size"):
        items = items or []
        total = sum((i.get(key) or 0) for i in items)
        return {"count": len(items), "size": total, "size_text": _fmt_size(total)}

    vol_items = df.get("Volumes") or []
    vol_total = sum(((v.get("UsageData") or {}).get("Size") or 0) for v in vol_items)
    return {
        "images": _sum(df.get("Images")),
        "containers": _sum(df.get("Containers")),
        "volumes": {"count": len(vol_items), "size": vol_total, "size_text": _fmt_size(vol_total)},
        "build_cache": _sum(df.get("BuildCache")),
    }


class PruneReq(BaseModel):
    builder: bool = False  # 同时清理构建缓存


@router.post("/system/prune")
def system_prune(body: PruneReq, request: Request = None):
    """清理停止的容器 / 悬空镜像 / 无用网络（可选构建缓存）。不动数据卷。"""
    tid = (request.headers.get("x-task-id") if request else "") or ""
    if tid:
        tasks.start(tid, "一键清理（停止容器/悬空镜像/无用网络）")
    try:
        client = get_docker()
        freed = {}
        total = 0
        try:
            if tid:
                tasks.log(tid, "清理停止的容器…")
            c = client.containers.prune()
            freed["containers"] = len(c.get("ContainersDeleted") or [])
            total += c.get("SpaceReclaimed") or 0
            if tid:
                tasks.log(tid, "清理悬空镜像…")
            i = client.images.prune(filters={"dangling": True})
            freed["images"] = len(i.get("ImagesDeleted") or [])
            total += i.get("SpaceReclaimed") or 0
            if tid:
                tasks.log(tid, "清理无用网络…")
            n = client.networks.prune()
            freed["networks"] = len(n.get("NetworksDeleted") or [])
            if body.builder:
                if tid:
                    tasks.log(tid, "清理构建缓存…")
                b = client.api.prune_builds()
                freed["build_cache"] = len(b.get("CachesDeleted") or [])
                total += b.get("SpaceReclaimed") or 0
        except docker.errors.APIError as e:
            raise HTTPException(status_code=502, detail=f"清理失败：{e}")
        parts = "、".join(f"{k} {v} 项" for k, v in freed.items())
        if tid:
            tasks.finish(tid, True, f"✔ 已清理 {parts}，释放 {_fmt_size(total)}")
        return {"ok": True, "detail": f"已清理 {parts}，释放 {_fmt_size(total)}"}
    except Exception as e:  # noqa: BLE001
        if tid:
            tasks.finish(tid, False, f"✘ 清理失败：{e}")
        raise


class MirrorSettings(BaseModel):
    registry_mirrors: list[str] = Field(default_factory=list, max_length=20)
    restart: bool = True


@router.get("/settings")
def get_settings():
    data = host_ops.read_daemon_json()
    return {
        "host_kind": host_ops.host_kind(),
        "daemon_json_path": host_ops.DAEMON_JSON,
        "registry_mirrors": data.get("registry-mirrors", []),
        "raw_keys": sorted(data.keys()),
    }


@router.put("/settings")
def put_settings(body: MirrorSettings, request: Request = None):
    tid = (request.headers.get("x-task-id") if request else "") or ""
    if tid:
        tasks.start(tid, "保存镜像加速配置")
    try:
        mirrors = [m.strip().rstrip("/") for m in body.registry_mirrors if m.strip()]
        if tid:
            tasks.log(tid, "写入 daemon.json…")
        data = host_ops.read_daemon_json()
        if mirrors:
            data["registry-mirrors"] = mirrors
        else:
            data.pop("registry-mirrors", None)
        host_ops.write_daemon_json(data)

        restart_err = None
        if body.restart:
            if tid:
                tasks.log(tid, "重启 Docker 守护进程（最长 30s）…")
            reset_docker()

            def _restart():
                nonlocal restart_err
                try:
                    host_ops.restart_dockerd()
                except Exception as e:  # noqa: BLE001
                    restart_err = str(e)

            threading.Thread(target=_restart, daemon=True).start()
            # 等待 dockerd 回归，最多 30s
            ok = False
            for _ in range(60):
                time.sleep(0.5)
                reset_docker()
                try:
                    get_docker().ping()
                    ok = True
                    break
                except HTTPException:
                    continue
            if tid:
                tasks.log(tid, "Docker 已重启恢复响应" if ok else "Docker 30s 内未恢复响应")
            if restart_err:
                raise HTTPException(status_code=502, detail=f"daemon.json 已写入，但重启 Docker 失败：{restart_err}")
            if not ok:
                raise HTTPException(status_code=502, detail="daemon.json 已写入，但 Docker 重启后未在 30s 内恢复，请到宿主机检查")
        if tid:
            tasks.finish(tid, True, "✔ 镜像加速配置已保存" + ("，Docker 已重启生效" if body.restart else ""))
        return {"ok": True, "detail": "镜像加速配置已保存，Docker 已重启生效", "registry_mirrors": mirrors}
    except Exception as e:  # noqa: BLE001
        if tid:
            tasks.finish(tid, False, f"✘ 保存失败：{e}")
        raise


# ---------- PHP 增强镜像构建 ----------
# 官方 php:*-cli 镜像精简，缺 mysqli/pdo_mysql/gd/zip 等常用扩展；且扩展必须烧进镜像层
# （容器重建会丢运行时安装的扩展）。本功能在节点上预构建增强镜像，完成后自动进运行环境白名单。

import os as _os
import re as _re
import subprocess as _sp

from app.config import settings as _settings
from app.services.instance_service import EXEC_JOBS as _EXEC_JOBS
from app.services.instance_service import ExecJob as _ExecJob

# 构建产物命名规则（instance_service 按此动态发现并入白名单）
PHP_BUILT_RE = _re.compile(r"^ppanel-php:\d+\.\d+-full$")

_PHP_DOCKERFILE = """ARG BASE
FROM ${BASE}
RUN apt-get update && apt-get install -y --no-install-recommends \\
        libpng-dev libjpeg-dev libfreetype6-dev libzip-dev libicu-dev \\
    && docker-php-ext-configure gd --with-jpeg --with-freetype \\
    && docker-php-ext-install -j"$(nproc)" mysqli pdo_mysql gd zip bcmath intl sockets exif opcache \\
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
"""


class PhpBuildReq(BaseModel):
    base: str = Field(pattern=r"^php:\d+\.\d+-cli$")  # 官方 CLI 镜像作为构建基底


def _php_build_worker(job, base: str, target: str) -> None:
    try:
        job.emit(f"[构建] {base} → {target}")
        job.emit("[构建] 编译扩展：mysqli pdo_mysql gd zip bcmath intl sockets exif opcache")
        proc = _sp.Popen(["docker", "build", "--network=host", "-t", target,
                          "--build-arg", f"BASE={base}", "-"],
                         stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.STDOUT,
                         text=True, env={**_os.environ, "DOCKER_BUILDKIT": "0"})
        proc.stdin.write(_PHP_DOCKERFILE)
        proc.stdin.close()
        for line in proc.stdout:
            job.emit(line.rstrip())
        proc.wait()
        job.exit_code = proc.returncode
        job.emit(f"[构建] {'完成！镜像已加入运行环境白名单：' + target if proc.returncode == 0 else '失败（exit=%d）' % proc.returncode}")
    except Exception as e:  # noqa: BLE001
        job.emit(f"[构建] 异常：{e}")
        job.exit_code = -1
    finally:
        job.done = True


@router.post("/php-images/build")
def php_image_build(body: PhpBuildReq):
    base = body.base.strip()
    if base not in _settings.php_images:
        raise HTTPException(status_code=400, detail="仅支持基于官方 PHP 白名单镜像构建")
    ver = base.split(":")[1].replace("-cli", "")
    target = f"ppanel-php:{ver}-full"

    client = get_docker()
    try:
        client.images.get(base)  # 基底必须已拉取（绝不自动 pull，防占磁盘）
    except docker.errors.ImageNotFound:
        raise HTTPException(status_code=404, detail=f"基础镜像 {base} 未拉取，请先拉取镜像")
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")

    for j in list(_EXEC_JOBS.values()):  # 同版本防并发重复构建
        if getattr(j, "image_target", None) == target and not j.done:
            raise HTTPException(status_code=409, detail=f"{target} 正在构建中，请稍候")

    for jid in [j for j, job in _EXEC_JOBS.items() if job.done]:  # 清理历史任务
        _EXEC_JOBS.pop(jid, None)

    job = _ExecJob(0, f"php-build {target}")
    job.image_target = target
    _EXEC_JOBS[job.id] = job
    threading.Thread(target=_php_build_worker, args=(job, base, target), daemon=True).start()
    return {"ok": True, "job_id": job.id, "image": target}


@router.get("/php-images/build/{job_id}")
def php_image_build_status(job_id: str):
    job = _EXEC_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="构建任务不存在或已被清理")
    return {"lines": list(job.lines), "done": job.done, "exit_code": job.exit_code,
            "image": getattr(job, "image_target", None)}


# ---------- 节点级共享 MySQL 服务 ----------
# 每版本一个 MySQL 容器，多实例共享进程、各持独立库；外网直连映射宿主端口（按账号鉴权）。

from pydantic import BaseModel as _BaseModel  # noqa: E402

from app.database import get_db as _get_db  # noqa: E402
from app.services import mysql_service as _mysql  # noqa: E402


@router.get("/mysql")
def mysql_list(db=Depends(_get_db)):
    return _mysql.list_services(db)


@router.post("/mysql/{ver}/enable")
def mysql_enable(ver: str, db=Depends(_get_db), request: Request = None):
    """启用（镜像必须已本地拉取；阻塞等待 mysqld 就绪，最长 ~90s）。"""
    tid = (request.headers.get("x-task-id") if request else "") or ""
    if tid:
        tasks.start(tid, f"启用 MySQL {ver} 服务")
        tasks.log(tid, "检查本地镜像与端口（绝不自动拉取）…")
    try:
        result = _mysql.enable_mysql(db, ver, tid=tid)
        if tid:
            tasks.finish(tid, True, f"✔ MySQL {ver} 已启用，外网端口 {result['host_port']}")
        return result
    except Exception as e:  # noqa: BLE001
        if tid:
            tasks.finish(tid, False, f"✘ 启用失败：{e}")
        raise


class _MysqlDisableReq(_BaseModel):
    purge: bool = False  # 同时删除数据卷（所有库数据不可恢复）


@router.post("/mysql/{ver}/disable")
def mysql_disable(ver: str, body: _MysqlDisableReq | None = None, db=Depends(_get_db),
                  request: Request = None):
    tid = (request.headers.get("x-task-id") if request else "") or ""
    if tid:
        tasks.start(tid, f"停用 MySQL {ver} 服务" + ("（同时删除数据卷）" if body and body.purge else ""))
    try:
        result = _mysql.disable_mysql(db, ver, purge=bool(body and body.purge), tid=tid)
        if tid:
            tasks.finish(tid, True, f"✔ {result.get('detail', '已停用')}")
        return result
    except Exception as e:  # noqa: BLE001
        if tid:
            tasks.finish(tid, False, f"✘ 停用失败：{e}")
        raise


@router.get("/mysql/{ver}/root-password")
def mysql_root_password(ver: str, db=Depends(_get_db)):
    row = _mysql._get_svc_row(db, ver)
    return {"version": ver, "root_password": row.root_password, "host_port": row.host_port}
