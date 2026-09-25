"""PPanel 被控 Agent 独立入口：
    uv run uvicorn agent_main:app --host 0.0.0.0 --port 9100

无用户体系，全部接口走 X-Node-Token（REST）/ ?node_token=（WS），
只接受主控调用。与面板（main.py）共用 services/models/config。
另有独立单容器面板 /panel（panel_token 鉴权）与域名反代。
"""
import asyncio
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from app.agent_auth import require_node
from app.auth import hash_password
from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import Instance, InstanceCron, InstanceSite, User  # noqa: F401
from app.routers import (agent, agent_ws, backup, host_files, host_tasks,
                         open_api, panel, panel_ops, security)
from app.services import instance_service as svc
from app.services import cron_service


async def _metrics_loop():
    """每 60s 采样运行中实例的用量，供独立面板画历史曲线（被控自持数据）；
    顺带检查实例状态变化与到期，触发商城 webhook 通知；
    并对账清理孤儿容器（docker 有、登记无）。"""
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(_sample_once)
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.to_thread(_watch_once)
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.to_thread(svc.reconcile_orphans)
        except Exception:  # noqa: BLE001
            pass


def _sample_once():
    db = SessionLocal()
    try:
        svc.sample_all_metrics(db)
    finally:
        db.close()


# ---------- 商城 webhook：崩溃通知 + 到期提醒（内存去重，重启后最多重发一次） ----------

_sent: dict[str, float] = {}  # event_key -> 上次发送时间戳


def _webhook(event: dict) -> None:
    import httpx
    if not settings.webhook_url:
        return
    try:
        httpx.post(settings.webhook_url, json=event, timeout=5.0)
    except Exception as e:  # noqa: BLE001 通知失败不影响主流程
        print(f"[agent] webhook 发送失败：{e}")


def _watch_once():
    from app.models import utcnow

    now = utcnow()
    db = SessionLocal()
    try:
        for inst in db.query(Instance).all():
            svc.sync_status(inst)
            db.commit()
            # 崩溃：running → exited，至少间隔 1h 才重复提醒
            if inst.status == "exited":
                key = f"exited:{inst.id}"
                if now.timestamp() - _sent.get(key, 0) > 3600:
                    _sent[key] = now.timestamp()
                    _webhook({"event": "instance_exited", "iid": inst.id,
                              "name": inst.name, "owner_ref": inst.owner_ref, "ts": now.isoformat()})
            # 到期提醒：≤3天 / ≤1天 / 已过期 三档，各档每天最多发一次
            expire = getattr(inst, "expire_at", None)
            if not expire:
                continue
            left = (expire - now).total_seconds() / 86400
            tier = ("expired" if left <= 0 else
                    "d1" if left <= 1 else
                    f"d{settings.webhook_warn_days}" if left <= settings.webhook_warn_days else "")
            if not tier:
                continue
            key = f"expire:{inst.id}:{tier}"
            if now.timestamp() - _sent.get(key, 0) > 86400:
                _sent[key] = now.timestamp()
                _webhook({"event": "instance_expiring", "iid": inst.id, "name": inst.name,
                          "owner_ref": inst.owner_ref, "tier": tier,
                          "expire_at": expire.isoformat(),
                          "days_left": round(left, 1), "ts": now.isoformat()})
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_app):
    os.makedirs(settings.data_root, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    # 轻量迁移：老库补列（create_all 不会给已有表加列）
    with engine.begin() as conn:
        for ddl in ("ALTER TABLE instances ADD COLUMN panel_token VARCHAR(64)",
                "ALTER TABLE instances ADD COLUMN domain VARCHAR(255)",
                "ALTER TABLE instances ADD COLUMN expire_at DATETIME",
                "ALTER TABLE instances ADD COLUMN owner_ref VARCHAR(128) DEFAULT ''",
                "ALTER TABLE instances ADD COLUMN disabled_funcs VARCHAR(512) DEFAULT ''",
                "ALTER TABLE instances ADD COLUMN traffic_gb REAL",
                "ALTER TABLE instances ADD COLUMN traffic_used_mb REAL DEFAULT 0",
                "ALTER TABLE instances ADD COLUMN traffic_last_mb REAL DEFAULT 0",
                "ALTER TABLE instances ADD COLUMN traffic_month VARCHAR(7) DEFAULT ''",
                "ALTER TABLE host_backup_jobs ADD COLUMN dest VARCHAR(8) DEFAULT 'local'"):
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
    if settings.open_api_key:
        print("[agent] 开通接口 /open/* 已启用（X-API-Key）")
    else:
        print("[agent] 开通接口未启用（未配置 OPEN_API_KEY）")
    task = asyncio.create_task(_metrics_loop())
    threading.Thread(target=cron_service.cron_loop, daemon=True,
                     name="ppanel-cron").start()
    yield
    task.cancel()


app = FastAPI(title="PPanel Agent", version="0.1.0", lifespan=lifespan)
app.include_router(agent.router, prefix="/agent",
                   dependencies=[Depends(require_node)])
app.include_router(agent_ws.router, prefix="/agent")
app.include_router(host_files.router, prefix="/agent")  # 宿主机文件管理（管理员节点运维）
app.include_router(security.router, prefix="/agent")  # 宿主机安全防护：X-Node-Token 或 X-API-Key
app.include_router(backup.router, prefix="/agent")  # 节点备份：容器导出 / 数据库导出，X-Node-Token 或 X-API-Key
app.include_router(host_tasks.router, prefix="/agent")  # 任务日志增量拉取（配合 X-Task-Id）
app.include_router(panel_ops.router)  # 面板扩展：自助备份/定时任务/站点防护/phpMyAdmin
# 注意：panel_ops 必须先于 panel 挂载——panel 里有 POST /panel/instance/{action} 通配路由，
# 后注册的多段路由会被单段通配截胡（如 POST /panel/instance/crons）
app.include_router(panel.router)  # 独立单容器面板：自带令牌鉴权
app.include_router(open_api.router)  # 开放开通面：X-API-Key，供第三方商城直连


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


def main() -> None:
    """快捷入口：uv run python agent_main.py（等价 uvicorn agent_main:app --host 0.0.0.0 --port 9100）"""
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9100)


if __name__ == "__main__":
    main()
