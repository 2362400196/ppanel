import asyncio
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.auth import hash_password
from app.config import settings
from app.database import Base, SessionLocal, engine
from app.domain_proxy import try_domain_proxy
from app.metrics_sampler import metrics_loop
from app.models import Domain, Instance, Metric, Node, OpLog, User  # noqa: F401 确保模型注册
from app.routers import (auth_router, docker_proxy, instances, nodes_admin,
                         open_api, users_admin, ws_proxy)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _seed_admin()
    sampler = asyncio.create_task(metrics_loop())
    yield
    sampler.cancel()
    with suppress(asyncio.CancelledError):
        await sampler


def _seed_admin() -> None:
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            db.add(User(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
                role="admin",
            ))
            db.commit()
    finally:
        db.close()


app = FastAPI(title="PPanel Master - 容器实例主控", version="0.1.0", lifespan=lifespan)

app.include_router(auth_router.router, prefix="/api")
app.include_router(instances.router, prefix="/api")
app.include_router(users_admin.router, prefix="/api")
app.include_router(nodes_admin.router, prefix="/api")
app.include_router(docker_proxy.router, prefix="/api")
app.include_router(open_api.router, prefix="/api")
app.include_router(ws_proxy.router)  # WS 与面板一致，不带 /api 前缀


@app.get("/api/health", include_in_schema=False)
def health():
    return {"ok": True, "service": "ppanel-master"}


# 托管主控前端构建产物 + 域名反代兜底（注册在所有 /api 路由之后）
_dist = Path(__file__).resolve().parents[0] / "frontend" / "dist"


@app.api_route("/{full_path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
               include_in_schema=False)
async def catch_all(full_path: str, request: Request):
    # 1) Host 命中已绑定域名 → 反代到对应实例
    proxied = await try_domain_proxy(request)
    if proxied is not None:
        return proxied
    # 2) 其余请求走主控前端 SPA
    if request.method not in ("GET", "HEAD"):
        return Response(status_code=404)
    if _dist.exists():
        candidate = _dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_dist / "index.html")
    return Response("Not Found", status_code=404)


if _dist.exists():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")
