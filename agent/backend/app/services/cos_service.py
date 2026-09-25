"""腾讯云 COS 对象存储备份上传（免 SDK：手写 XML API 签名，纯 httpx）。

配置存 CosConfig（单行）。上传使用简单 PUT（单对象上限 5GB，备份文件足够），
流式读文件不占大内存；凭证校验用 GET bucket（?max-keys=1）。
"""
import hashlib
import hmac
import os
import time

import httpx


def get_config(db=None):
    """读取 COS 配置行（无则建空行）；返回模型对象或 None 参数化调用时直接返回。"""
    from app.database import SessionLocal
    from app.models import CosConfig

    own = db is None
    s = db or SessionLocal()
    try:
        row = s.get(CosConfig, 1)
        if not row:
            row = CosConfig(id=1)
            s.add(row)
            s.commit()
        return row
    finally:
        if own:
            s.close()


def _sign(secret_key: str, key_time: str, method: str, path: str) -> str:
    """COS XML API 请求签名（Authorization 的 q-signature）。"""
    sign_key = hmac.new(secret_key.encode(), key_time.encode(), hashlib.sha1).hexdigest()
    # StringToSign: HttpMethod\nUriPathname\nHttpParams\nHttpHeaders\n（后两者此处为空）
    string_to_sign = f"{method.lower()}\n{path}\n\n\n"
    return hmac.new(sign_key.encode(), string_to_sign.encode(), hashlib.sha1).hexdigest()


def _auth_header(cfg, method: str, path: str) -> str:
    now = int(time.time())
    key_time = f"{now - 60};{now + 600}"
    sig = _sign(cfg.secret_key, key_time, method, path)
    return (f"q-sign-algorithm=sha1&q-ak={cfg.secret_id}&q-sign-time={key_time}"
            f"&q-key-time={key_time}&q-header-list=&q-url-param-list=&q-signature={sig}")


def _base_url(cfg) -> str:
    return f"https://{cfg.bucket}.cos.{cfg.region}.myqcloud.com"


def test_conn(cfg) -> dict:
    """校验凭证与桶可访问性（GET bucket ?max-keys=1）。"""
    if not (cfg.secret_id and cfg.secret_key and cfg.bucket and cfg.region):
        raise ValueError("请先填写 SecretId / SecretKey / 存储桶 / 地域")
    url = _base_url(cfg) + "/?max-keys=1"
    r = httpx.get(url, headers={"Authorization": _auth_header(cfg, "get", "/")}, timeout=10)
    if r.status_code == 200:
        return {"ok": True, "message": "连接成功，桶可访问"}
    if r.status_code == 403:
        raise ValueError("连接被拒（403）：SecretId/SecretKey 无该桶权限或防盗链限制")
    if r.status_code == 404:
        raise ValueError("存储桶不存在（404）：检查桶名与地域是否匹配")
    raise ValueError(f"COS 返回 {r.status_code}：{r.text[:160]}")


def upload(cfg, file_path: str, remote_name: str | None = None) -> dict:
    """上传备份文件到 COS，返回对象 key 与大小。失败抛异常（httpx.HTTPError/ValueError）。"""
    size = os.path.getsize(file_path)
    key = f"{(cfg.prefix or 'ppanel-backups').strip('/')}/{remote_name or os.path.basename(file_path)}"
    path = f"/{key}"
    url = _base_url(cfg) + path
    headers = {"Authorization": _auth_header(cfg, "put", path),
               "Content-Length": str(size)}
    with open(file_path, "rb") as f:
        r = httpx.put(url, content=f, headers=headers, timeout=600)
    if r.status_code not in (200,):
        raise ValueError(f"COS 上传失败（{r.status_code}）：{r.text[:160]}")
    return {"key": key, "size": size}


def remove(cfg, remote_name: str) -> None:
    """删除 COS 对象（静默容忍 404）。"""
    key = f"{(cfg.prefix or 'ppanel-backups').strip('/')}/{remote_name}"
    path = f"/{key}"
    url = _base_url(cfg) + path
    r = httpx.delete(url, headers={"Authorization": _auth_header(cfg, "delete", path)}, timeout=30)
    if r.status_code not in (200, 204, 404):
        raise ValueError(f"COS 删除失败（{r.status_code}）：{r.text[:160]}")
