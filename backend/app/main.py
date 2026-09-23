import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.auth import hash_password
from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import Instance, OpLog, User  # noqa: F401 确保模型注册
from app.routers import admin, auth, docker_admin, files, instances, ws


@asynccontextmanager
async def lifespan(_app: FastAPI):
    os.makedirs(settings.data_root, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    _seed_admin()
    yield


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


app = FastAPI(title="PPanel - Python 环境容器托管面板", version="0.1.0", lifespan=lifespan)

app.include_router(auth.router, prefix="/api")
app.include_router(instances.router, prefix="/api")
app.include_router(files.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(docker_admin.router, prefix="/api")
app.include_router(ws.router)


@app.get("/api/health", include_in_schema=False)
def health():
    return {"ok": True}


# 生产环境可直接托管前端构建产物（frontend/dist 存在时启用 SPA 兜底）
_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _dist.exists():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        candidate = _dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_dist / "index.html")
