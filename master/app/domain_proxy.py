"""Host 头域名反代：请求 Host 命中已绑定域名时，转发到对应实例的对外端口。

MVP 约定：域名解析（或本机 hosts / *.localhost）指向主控，主控按 Host 分流。
"""
from urllib.parse import urlparse

import httpx
from starlette.requests import Request
from starlette.responses import Response

from app.database import SessionLocal
from app.models import Domain, Instance, Node

_HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}


def _resolve_target(host: str) -> str | None:
    """查绑定关系，返回实例目标 base url；无绑定返回 None。"""
    db = SessionLocal()
    try:
        d = db.query(Domain).filter(Domain.hostname == host).first()
        if not d:
            return None
        inst = db.get(Instance, d.instance_uuid)
        if not inst or not inst.ext_port:
            return None
        node = db.get(Node, inst.node_id)
        if not node:
            return None
        # 与 agent_client 同样的归一化：localhost → 127.0.0.1
        node_host = urlparse(node.base_url.replace("localhost", "127.0.0.1")).hostname \
            or "127.0.0.1"
        return f"http://{node_host}:{inst.ext_port}"
    finally:
        db.close()


async def try_domain_proxy(request: Request) -> Response | None:
    host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    if not host:
        return None
    base = _resolve_target(host)
    if base is None:
        return None

    url = base + request.url.path
    if request.url.query:
        url += "?" + str(request.url.query)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
    body = await request.body()

    client = httpx.AsyncClient(timeout=30, follow_redirects=False)
    try:
        resp = await client.request(request.method, url, headers=headers, content=body)
    except httpx.HTTPError:
        return Response("实例服务不可达", status_code=502,
                        media_type="text/plain; charset=utf-8")
    finally:
        await client.aclose()

    out_headers = {k: v for k, v in resp.headers.items()
                   if k.lower() not in _HOP_HEADERS}
    return Response(content=resp.content, status_code=resp.status_code,
                    headers=out_headers)
