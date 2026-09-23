import threading

import docker
from fastapi import HTTPException

_lock = threading.Lock()
_client: docker.DockerClient | None = None


def get_docker() -> docker.DockerClient:
    """获取 Docker 客户端（连接失败抛 503）。"""
    global _client
    with _lock:
        if _client is None:
            try:
                _client = docker.from_env()
                _client.ping()
            except Exception:
                _client = None
                raise HTTPException(status_code=503, detail="Docker 服务不可用")
        return _client


def try_get_docker() -> docker.DockerClient | None:
    try:
        return get_docker()
    except HTTPException:
        return None


def reset_docker() -> None:
    """清空缓存客户端（dockerd 重启后强制重连）。"""
    global _client
    with _lock:
        _client = None
