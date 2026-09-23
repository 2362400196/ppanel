"""PPanel 被控 Agent 独立入口：
    uv run uvicorn agent_main:app --host 0.0.0.0 --port 9100

无用户体系，全部接口走 X-Node-Token（REST）/ ?node_token=（WS），
只接受主控调用。与面板（main.py）共用 services/models/config。
另有独立单容器面板 /panel（panel_token 鉴权）与域名反代。
"""
import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from app.agent_auth import require_node
from app.auth import hash_password
from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import Instance, User  # noqa: F401
from app.routers import agent, agent_ws, panel
from app.services import instance_service as svc


async def _metrics_loop():
    """每 60s 采样运行中实例的用量，供独立面板画历史曲线（被控自持数据）。"""
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(_sample_once)
        except Exception:  # noqa: BLE001
            pass


def _sample_once():
    db = SessionLocal()
    try:
        svc.sample_all_metrics(db)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_app):
    os.makedirs(settings.data_root, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    # 轻量迁移：老库补列（create_all 不会给已有表加列）
    with engine.begin() as conn:
        for ddl in ("ALTER TABLE instances ADD COLUMN panel_token VARCHAR(64)",
                    "ALTER TABLE instances ADD COLUMN domain VARCHAR(255)"):
            try:
                conn.exec_driver_sql(ddl)
            except Exception:  # noqa: BLE001 列已存在
                pass
    # Agent 本地库的占位账号（instances.user_id 外键需要）
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            db.add(User(username="__agent__",
                        password_hash=hash_password(os.urandom(16).hex()),
                        role="user"))
            db.commit()
    finally:
        db.close()
    if not settings.node_token:
        print("[agent] 警告：未设置 NODE_TOKEN，所有请求将被拒绝（401）")
    else:
        print(f"[agent] 节点鉴权已启用，监听 {settings.agent_host}:{settings.agent_port}")
    task = asyncio.create_task(_metrics_loop())
    yield
    task.cancel()


app = FastAPI(title="PPanel Agent", version="0.1.0", lifespan=lifespan)
app.include_router(agent.router, prefix="/agent",
                   dependencies=[Depends(require_node)])
app.include_router(agent_ws.router, prefix="/agent")
app.include_router(panel.router)  # 独立单容器面板：自带令牌鉴权


# ---------- 域名反代：绑定了域名的实例，按 Host 转发到实例端口 ----------

_DOM_TTL = 20.0
_dom_cache = {"at": 0.0, "map": {}}


def _domain_map() -> dict:
    now = time.time()
    if now - _dom_cache["at"] > _DOM_TTL:
        db = SessionLocal()
        try:
            rows = (db.query(Instance.domain, Instance.ext_port)
                    .filter(Instance.domain.isnot(None), Instance.domain != "",
                            Instance.ext_port > 0).all())
            _dom_cache["map"] = {r.domain.lower(): r.ext_port for r in rows}
            _dom_cache["at"] = now
        finally:
            db.close()
    return _dom_cache["map"]


async def _proxy_to(request: Request, port: int) -> Response:
    import httpx
    qs_raw = request.scope.get("query_string", b"")
    qs = f"?{qs_raw.decode('latin-1')}" if qs_raw else ""
    url = f"http://127.0.0.1:{port}{request.url.path}{qs}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length")}
    body = await request.body()
    client = httpx.AsyncClient(timeout=60.0)
    try:
        resp = await client.request(request.method, url, content=body, headers=headers)
        resp_headers = {k: v for k, v in resp.headers.items()
                        if k.lower() not in ("content-length", "transfer-encoding",
                                             "content-encoding")}
        return Response(content=resp.content, status_code=resp.status_code,
                        headers=resp_headers)
    except httpx.HTTPError:
        return JSONResponse({"detail": "实例服务不可达"}, status_code=502)
    finally:
        await client.aclose()


@app.middleware("http")
async def domain_proxy(request: Request, call_next):
    host = request.headers.get("host", "").split(":")[0].strip().lower()
    # 命中已绑定域名才转发；IP/localhost/面板路径不受影响
    if host and "." in host:
        port = _domain_map().get(host)
        if port:
            return await _proxy_to(request, port)
    return await call_next(request)


@app.get("/agent/ping", include_in_schema=False)
def ping():
    return {"ok": True, "service": "ppanel-agent"}


_PANEL_UI = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "app", "panel_ui", "index.html")


@app.get("/panel", include_in_schema=False)
def panel_page():
    """独立单容器面板入口（无构建单文件 UI）。"""
    return FileResponse(_PANEL_UI, media_type="text/html")


# Monaco Editor（VS Code 内核）静态资源：文件管理在线编辑器使用
from fastapi.staticfiles import StaticFiles  # noqa: E402

_VS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "panel_ui", "vs")
if os.path.isdir(_VS_DIR):
    app.mount("/panel/vs", StaticFiles(directory=_VS_DIR), name="panel-vs")

# xterm.js（终端内核，qnbt 同款）本地静态资源：离线可用
_VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "panel_ui", "vendor")
if os.path.isdir(_VENDOR_DIR):
    app.mount("/panel/vendor", StaticFiles(directory=_VENDOR_DIR), name="panel-vendor")

# 阿里 iconfont SVG Symbol（文件类型彩色图标，与 qnbt 面板一致）
_ICONFONT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "app", "panel_ui", "iconfont.js")
if os.path.isfile(_ICONFONT):
    from fastapi.responses import PlainTextResponse  # noqa: E402

    @app.get("/panel/iconfont.js", include_in_schema=False)
    def panel_iconfont():
        with open(_ICONFONT, "r", encoding="utf-8") as f:
            return PlainTextResponse(f.read(), media_type="application/javascript")
