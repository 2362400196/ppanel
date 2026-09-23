"""Docker 管理中心（仅 admin / X-API-Key）：
容器管理、镜像管理（含拉取/删除）、镜像加速设置、宿主机信息。
"""
import threading
import time

import docker
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

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
    return {
        "id": a.get("Id", "").replace("sha256:", "")[:12],
        "repo": tags[0].rsplit(":", 1)[0] if ":" in tags[0] else tags[0],
        "tag": tags[0].rsplit(":", 1)[1] if ":" in tags[0] else "latest",
        "full_name": tags[0],
        "tags": tags,
        "size_bytes": a.get("Size", 0),
        "size": _fmt_size(a.get("Size", 0)),
        "created": a.get("Created", ""),
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
def pull_image(body: PullReq):
    client = get_docker()
    image = body.image.strip()
    try:
        img = client.images.pull(image)
    except docker.errors.ImageNotFound:
        raise HTTPException(status_code=404, detail=f"镜像 {image} 不存在于仓库")
    except docker.errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"拉取失败：{e}")
    return {"ok": True, "detail": f"拉取完成", "image": _image_row(img)}


# ---------- 镜像加速设置 ----------

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
def put_settings(body: MirrorSettings):
    mirrors = [m.strip().rstrip("/") for m in body.registry_mirrors if m.strip()]
    data = host_ops.read_daemon_json()
    if mirrors:
        data["registry-mirrors"] = mirrors
    else:
        data.pop("registry-mirrors", None)
    host_ops.write_daemon_json(data)

    restart_err = None
    if body.restart:
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
        if restart_err:
            raise HTTPException(status_code=502, detail=f"daemon.json 已写入，但重启 Docker 失败：{restart_err}")
        if not ok:
            raise HTTPException(status_code=502, detail="daemon.json 已写入，但 Docker 重启后未在 30s 内恢复，请到宿主机检查")
    return {"ok": True, "detail": "镜像加速配置已保存，Docker 已重启生效", "registry_mirrors": mirrors}
