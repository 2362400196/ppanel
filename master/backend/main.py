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
from app.models import Domain, Instance, Metric, Node, OpLog, Plan, User  # noqa: F401 确保模型注册
from app.routers import (auth_router, docker_proxy, host_proxy, instances,
                         nodes_admin, open_api, plans_admin, users_admin,
                         ws_proxy, tasks_router)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _migrate()
    _seed_admin()
    _seed_plans()
    sampler = asyncio.create_task(metrics_loop())
    yield
    sampler.cancel()
    with suppress(asyncio.CancelledError):
        await sampler


def _migrate() -> None:
    """轻量补列：SQLite create_all 不会修改已存在的表。"""
    from sqlalchemy import text
    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(plans)"))}
        if "node_id" not in cols:
            conn.execute(text("ALTER TABLE plans ADD COLUMN node_id INTEGER"))
            conn.commit()
        if "image" not in cols:
            conn.execute(text("ALTER TABLE plans ADD COLUMN image VARCHAR(128) DEFAULT ''"))
            conn.commit()
        if "traffic_gb" not in cols:
            conn.execute(text("ALTER TABLE plans ADD COLUMN traffic_gb INTEGER DEFAULT 0"))
            conn.commit()
        icols = {r[1] for r in conn.execute(text("PRAGMA table_info(instances)"))}
        if "traffic_gb" not in icols:
            conn.execute(text("ALTER TABLE instances ADD COLUMN traffic_gb REAL"))
            conn.commit()


def _seed_plans() -> None:
    """商品表为空时写入默认三档套餐（对齐商城初版文案）。"""
    db = SessionLocal()
    try:
        if db.query(Plan).count() > 0:
            return
        for p in (
            dict(name="轻量型", desc="个人小站 / 学习练手", cpu=1, mem=512, disk=2048, price_cents=500, sort=1, image="python:3.11-slim"),
            dict(name="标准型", desc="常规 Web 应用 / 机器人", cpu=1, mem=1024, disk=5120, price_cents=1200, sort=2, image="python:3.11-slim"),
            dict(name="进阶型", desc="高并发服务 / 数据处理", cpu=2, mem=2048, disk=10240, price_cents=2500, sort=3, image="python:3.12-slim"),
        ):
            db.add(Plan(days=30, **p))
        db.commit()
    finally:
        db.close()


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
app.include_router(plans_admin.router, prefix="/api")
app.include_router(plans_admin.pub, prefix="/api")
app.include_router(docker_proxy.router, prefix="/api")
app.include_router(host_proxy.router, prefix="/api")
app.include_router(tasks_router.router, prefix="/api")  # 任务日志增量拉取（配合 X-Task-Id）
app.include_router(open_api.router, prefix="/api")
app.include_router(ws_proxy.router)  # WS 与面板一致，不带 /api 前缀


@app.get("/api/health", include_in_schema=False)
def health():
    return {"ok": True, "service": "ppanel-master"}


# 托管主控前端构建产物 + 域名反代兜底（注册在所有 /api 路由之后）
# 目录结构：master/backend/（后端）+ master/frontend/（前端），dist 在上一级
_dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"


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
        # index.html 禁止缓存：每次发版普通刷新即拿到新构建（assets 文件名带 hash 可长缓存）
        return FileResponse(_dist / "index.html", headers={"Cache-Control": "no-cache"})
    return Response("Not Found", status_code=404)


if _dist.exists():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")
