import threading

import docker
from fastapi import HTTPException

_lock = threading.Lock()
_client: docker.DockerClient | None = None
_long: docker.DockerClient | None = None


def get_docker() -> docker.DockerClient:
    """获取 Docker 客户端（连接失败抛 503）。默认 60s 读超时。"""
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


def get_docker_long(timeout: int = 1800) -> docker.DockerClient:
    """长任务（目录 tar 打包 / 容器 export 等）专用客户端：读超时 30 分钟。

    备份打包万级文件时单次 exec 远超默认 60s，共用客户端会 ReadTimeout。"""
    global _long
    with _lock:
        if _long is None:
            try:
                _long = docker.from_env(timeout=timeout)
                _long.ping()
            except Exception:
                _long = None
                raise HTTPException(status_code=503, detail="Docker 服务不可用")
        return _long


def try_get_docker() -> docker.DockerClient | None:
    try:
        return get_docker()
    except HTTPException:
        return None


def reset_docker() -> None:
    """清空缓存客户端（dockerd 重启后强制重连）。"""
    global _client, _long
    with _lock:
        _client = None
        _long = None
