"""独立单容器面板 API（/panel）：
一个面板令牌绑定一个实例，持有令牌即可登录面板操作该容器。
不依赖主控、无用户体系，可被任何开通系统对接（开通时下发 panel_token 即可）。
REST 走 Bearer JWT；WS 走 ?ptoken=（浏览器无法带 header）。
"""
import asyncio
import re
import threading
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.config import settings
from app.database import get_db, SessionLocal
from app.docker_client import get_docker
from app.models import AgentMetric, AgentOpLog, Instance
from app.services import caddy_service as caddy
from app.services import file_service as fs
from app.services import instance_service as svc
from app.services import mysql_service as mysql_svc

router = APIRouter()


# ---------- 凭证 ----------

def _issue_jwt(iid: int) -> str:
    payload = {
        "sub": f"panel:{iid}",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def _iid_from_token(token: str) -> int | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        sub = str(payload.get("sub", ""))
        if sub.startswith("panel:"):
            return int(sub.split(":", 1)[1])
    except (jwt.PyJWTError, TypeError, ValueError):
        pass
    return None


def _iid_of(request: Request) -> int:
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not token:
        # 浏览器 <a> 下载无法带 header，允许 ?t= 兜底
        token = request.query_params.get("t", "")
    iid = _iid_from_token(token)
    if iid is None:
        raise HTTPException(status_code=401, detail="面板未登录或凭证已失效")
    return iid


def _inst_of(iid: int, db: Session) -> Instance:
    """实例存在且令牌未吊销（panel_token 被清空即全部面板凭证失效）。"""
    inst = db.get(Instance, iid)
    if not inst or not inst.panel_token:
        raise HTTPException(status_code=401, detail="面板凭证已失效")
    return inst


def _dep(request: Request, db: Session = Depends(get_db)) -> Instance:
    inst = _inst_of(_iid_of(request), db)
    _check_expire(request.method, inst)
    return inst


def _expired(inst: Instance) -> bool:
    expire = getattr(inst, "expire_at", None)
    if not expire:
        return False
    return datetime.utcnow() >= expire


def _check_expire(method: str, inst: Instance) -> None:
    """到期后的面板行为：readonly 只放行 GET（可看状态/下载备份），block 全锁。"""
    if not _expired(inst):
        return
    if settings.expired_panel_mode != "block" and method == "GET":
        return
    raise HTTPException(status_code=403, detail="实例已到期，请联系商家续期")


def _out(inst: Instance) -> dict:
    expire = getattr(inst, "expire_at", None)
    return {
        "id": inst.id,
        "name": inst.name,
        "image": inst.image,
        "status": inst.status,
        "ext_port": inst.ext_port,
        "cpu_limit": inst.cpu_limit,
        "mem_limit": inst.mem_limit,
        "disk_quota": inst.disk_quota,
        "note": inst.note,
        "created_at": inst.created_at.isoformat() if inst.created_at else None,
        "expire_at": expire.isoformat() if expire else None,
        "owner_ref": getattr(inst, "owner_ref", ""),
        "traffic_gb": getattr(inst, "traffic_gb", None),
        "traffic_used_mb": round(getattr(inst, "traffic_used_mb", 0.0) or 0.0, 1),
    }


class PanelLoginIn(BaseModel):
    token: str = Field(min_length=8, max_length=128)


@router.post("/panel/login")
def panel_login(body: PanelLoginIn, db: Session = Depends(get_db)):
    inst = db.query(Instance).filter(Instance.panel_token == body.token.strip()).first()
    if not inst:
        raise HTTPException(status_code=401, detail="面板令牌无效")
    svc.sync_status(inst)
    db.commit()
    return {"token": _issue_jwt(inst.id), "instance": _out(inst)}


# ---------- 实例操作 ----------

@router.get("/panel/instance")
def panel_instance(dep: Instance = Depends(_dep), db: Session = Depends(get_db)):
    svc.sync_status(dep)
    db.commit()
    return _out(dep)


# 注意：必须注册在 /panel/instance/{action} 通配路由之前，否则 POST runtime 会被捕获成 action
@router.get("/panel/instance/runtime")
def panel_runtime(dep: Instance = Depends(_dep)):
    """运行环境卡：当前镜像 + 节点本地已拉取的可切换版本。"""
    return svc.runtime_versions(dep)


@router.post("/panel/instance/runtime")
def panel_switch_runtime(body: dict, dep: Instance = Depends(_dep),
                         db: Session = Depends(get_db)):
    _check_expire("POST", dep)  # 到期只读模式禁止切换
    return svc.switch_runtime(dep, (body or {}).get("image", ""), db)


# ---------- 共享 MySQL 数据库（一实例一库） ----------
# 注意：必须注册在 /panel/instance/{action} 通配路由之前

@router.get("/panel/instance/database")
def panel_database(dep: Instance = Depends(_dep), db: Session = Depends(get_db)):
    return {"db": mysql_svc.get_instance_db(db, dep),
            "versions": mysql_svc.enabled_versions(db)}


@router.post("/panel/instance/database")
def panel_db_create(body: dict, dep: Instance = Depends(_dep), db: Session = Depends(get_db)):
    _check_expire("POST", dep)  # 到期只读模式禁止建库
    info = mysql_svc.create_instance_db(db, dep, (body or {}).get("version", ""))
    svc.log_op(db, dep.id, "db_create", detail=f"MySQL {info['version']} {info['db_name']}")
    db.commit()
    return info


@router.post("/panel/instance/database/reset-password")
def panel_db_reset_password(dep: Instance = Depends(_dep), db: Session = Depends(get_db)):
    _check_expire("POST", dep)
    info = mysql_svc.reset_db_password(db, dep)
    svc.log_op(db, dep.id, "db_reset_password", detail=f"MySQL {info['version']} {info['db_name']}")
    db.commit()
    return info


@router.post("/panel/instance/database/switch")
def panel_db_switch(body: dict, dep: Instance = Depends(_dep), db: Session = Depends(get_db)):
    """切换数据库版本（带数据迁移）：失败保留原库可重试；到期只读模式禁止。"""
    _check_expire("POST", dep)
    info = mysql_svc.switch_instance_db(db, dep, (body or {}).get("version", ""))
    svc.log_op(db, dep.id, "db_switch", detail=f"MySQL → {info['version']} {info['db_name']}")
    db.commit()
    return info


@router.delete("/panel/instance/database")
def panel_db_drop(dep: Instance = Depends(_dep), db: Session = Depends(get_db)):
    _check_expire("DELETE", dep)  # 到期只读模式禁止删库
    info = mysql_svc.get_instance_db(db, dep)
    mysql_svc.drop_instance_db(db, dep)
    svc.log_op(db, dep.id, "db_drop", detail=f"MySQL {info['version']} {info['db_name']}" if info else "")
    db.commit()
    return {"detail": "数据库已删除"}


@router.post("/panel/instance/{action}")
def panel_action(action: str, dep: Instance = Depends(_dep), db: Session = Depends(get_db)):
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=404, detail="未知操作")
    if action == "start":
        svc.start_instance(dep)
        db.commit()
        svc.diagnose_startup(dep)
        svc.log_op(db, dep.id, "start")
        db.commit()
        return {"status": "running"}
    if action == "stop":
        svc.stop_instance(dep)
        svc.log_op(db, dep.id, "stop")
    else:
        svc.restart_instance(dep)
        svc.log_op(db, dep.id, "restart")
    db.commit()
    return {"status": dep.status}


class PanelUpdateIn(BaseModel):
    start_cmd: str | None = Field(default=None, max_length=512)
    note: str | None = Field(default=None, max_length=255)


@router.patch("/panel/instance")
def panel_update(body: PanelUpdateIn, dep: Instance = Depends(_dep), db: Session = Depends(get_db)):
    changed = []
    if body.note is not None and body.note != dep.note:
        dep.note = body.note
        changed.append("note")
    if body.start_cmd is not None and body.start_cmd.strip() != dep.start_cmd:
        if dep.status == "running":
            raise HTTPException(status_code=409, detail="实例运行中，请先停止后再修改启动命令")
        dep.start_cmd = body.start_cmd.strip()
        svc.recreate_container(dep)  # 命令烧进容器，需重建
        changed.append("start_cmd")
    if changed:
        svc.log_op(db, dep.id, "update", detail=",".join(changed))
        db.commit()
    return _out(dep)


@router.get("/panel/instance/metrics")
def panel_metrics(dep: Instance = Depends(_dep), db: Session = Depends(get_db),
                  hours: float = 6):
    """被控自持的历史用量采样（最多 24h）。"""
    from datetime import datetime, timedelta, timezone
    hours = max(0.5, min(hours, 24))
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    rows = (db.query(AgentMetric)
            .filter(AgentMetric.instance_id == dep.id, AgentMetric.ts >= since)
            .order_by(AgentMetric.ts).all())
    return {
        "points": [{
            "ts": r.ts.isoformat(),
            "cpu": round(r.cpu_percent, 2),
            "mem": round(r.mem_used_mb, 1),
            "mem_limit": round(r.mem_limit_mb, 1),
            "rx": round(r.net_rx_mb, 3),
            "tx": round(r.net_tx_mb, 3),
        } for r in rows],
    }


_DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


@router.get("/panel/instance/domain")
def panel_get_domain(dep: Instance = Depends(_dep)):
    """域名与 SSL 状态：域名、Caddy 状态、DNS 解析检测、HTTPS 地址。"""
    domain = (dep.domain or "").strip()
    info = caddy.caddy_info()
    ip = caddy.public_ip()
    out = {
        "domain": domain,
        "caddy": info,
        "https_url": f"https://{domain}" if domain else "",
        "http_url": f"http://{ip}:{dep.ext_port}" if dep.ext_port and ip else "",
        "ssl_active": bool(domain and info.get("active")),
    }
    if domain:
        ok, msg = caddy.check_dns(domain)
        out["dns_ok"] = ok
        out["dns_msg"] = msg
    return out


@router.put("/panel/instance/domain")
def panel_set_domain(body: dict, dep: Instance = Depends(_dep), db: Session = Depends(get_db)):
    """绑定/解绑域名：校验解析 → 写 Caddy 反代 → reload（证书由 Caddy 自动申请续期）。

    body: {"domain": "app.example.com"} 或 {"domain": ""}（解绑）。"""
    raw = (body or {}).get("domain", "").strip().lower()
    if not raw:
        if dep.domain:
            try:
                caddy.remove_site(dep.id)
            except RuntimeError as e:
                raise HTTPException(status_code=500, detail=str(e))
            svc.log_op(db, dep.id, "domain_unbind", detail=dep.domain)
            dep.domain = None
            db.commit()
        return {"domain": ""}
    if not _DOMAIN_RE.match(raw):
        raise HTTPException(status_code=400, detail="域名格式不正确，如 app.example.com")
    other = (db.query(Instance)
             .filter(Instance.domain == raw, Instance.id != dep.id).first())
    if other:
        raise HTTPException(status_code=409, detail="该域名已被其他实例绑定")
    if not caddy.caddy_installed():
        raise HTTPException(status_code=400, detail="节点未安装 Caddy，无法自动签发证书（联系管理员安装）")
    if not dep.ext_port:
        raise HTTPException(status_code=400, detail="实例没有外部端口，无法反代")
    ok, msg = caddy.check_dns(raw)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    try:
        if dep.domain and dep.domain != raw:
            caddy.remove_site(dep.id)  # 换域名：先清旧配置
        caddy.write_site(dep.id, raw, dep.ext_port)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    dep.domain = raw
    svc.log_op(db, dep.id, "domain_bind", detail=f"{raw}（{msg}）")
    db.commit()
    return {"domain": raw, "dns_msg": msg, "https_url": f"https://{raw}"}


@router.get("/panel/instance/ops")
def panel_ops(dep: Instance = Depends(_dep), db: Session = Depends(get_db),
              limit: int = 20):
    rows = (db.query(AgentOpLog)
            .filter(AgentOpLog.instance_id == dep.id)
            .order_by(AgentOpLog.id.desc())
            .limit(max(1, min(limit, 500))).all())
    return [{"action": r.action, "detail": r.detail,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows]


@router.get("/panel/instance/stats")
def panel_stats(dep: Instance = Depends(_dep)):
    return svc.container_stats(dep)


@router.get("/panel/instance/logs")
def panel_logs(dep: Instance = Depends(_dep), tail: int = 200):
    return {"logs": svc.container_logs(dep, tail)}


@router.post("/panel/instance/deps/install")
def panel_install(dep: Instance = Depends(_dep), db: Session = Depends(get_db),
                  file: str = "requirements.txt"):
    if "/" in file or "\\" in file or ".." in file or not file:
        raise HTTPException(status_code=400, detail="非法文件名")
    if svc.has_running_job(dep.id):
        raise HTTPException(status_code=409, detail="已有任务正在执行，请等待完成后再试")
    job = svc.start_install(dep, file)
    svc.log_op(db, dep.id, "deps_install", detail=file)
    db.commit()
    return {"job_id": job.id}


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\[\]=<>!~+,\-]*$")


def _check_names(raw: str) -> list:
    names = [n for n in (raw or "").replace(",", " ").split() if n]
    if not names:
        raise HTTPException(status_code=400, detail="请输入依赖名称")
    for n in names:
        if not _NAME_RE.match(n):
            raise HTTPException(status_code=400, detail=f"非法的依赖名称：{n}")
    return names


@router.post("/panel/instance/deps/install-pkgs")
def panel_install_pkgs(dep: Instance = Depends(_dep), db: Session = Depends(get_db),
                       body: dict = None):
    """手动安装依赖（支持多个，空格/逗号分隔）。"""
    if svc.has_running_job(dep.id):
        raise HTTPException(status_code=409, detail="已有任务正在执行，请等待完成后再试")
    names = _check_names((body or {}).get("names", ""))
    job = svc.start_pip(dep, ["install", *names])
    svc.log_op(db, dep.id, "deps_install", detail=" ".join(names))
    db.commit()
    return {"job_id": job.id}


@router.post("/panel/instance/deps/uninstall")
def panel_uninstall_pkg(dep: Instance = Depends(_dep), db: Session = Depends(get_db),
                        body: dict = None):
    """手动卸载依赖。"""
    if svc.has_running_job(dep.id):
        raise HTTPException(status_code=409, detail="已有任务正在执行，请等待完成后再试")
    names = _check_names((body or {}).get("names", ""))
    job = svc.start_pip(dep, ["uninstall", "-y", *names])
    svc.log_op(db, dep.id, "deps_uninstall", detail=" ".join(names))
    db.commit()
    return {"job_id": job.id}


@router.get("/panel/instance/deps/installed")
def panel_deps_installed(dep: Instance = Depends(_dep)):
    """检测已安装依赖（容器内 pip list）。"""
    return {"packages": svc.pip_list(dep)}


def _require_python(inst: Instance) -> None:
    if svc.is_php(inst.image):
        raise HTTPException(status_code=400, detail="该功能仅支持 Python 环境")


# Python 常用依赖目录（面板一键安装；pip 在线装，容器重建会丢——持久依赖请写 requirements.txt）
PY_PKG_CATALOG = [
    {"name": "flask", "type": "Web 框架", "desc": "轻量级 Web 框架"},
    {"name": "django", "type": "Web 框架", "desc": "全功能 Web 框架（含 ORM/后台）"},
    {"name": "fastapi", "type": "Web 框架", "desc": "高性能异步 API 框架"},
    {"name": "uvicorn", "type": "服务器", "desc": "ASGI 异步服务器（FastAPI 配套）"},
    {"name": "gunicorn", "type": "服务器", "desc": "WSGI 生产服务器"},
    {"name": "requests", "type": "HTTP", "desc": "最常用的同步 HTTP 客户端"},
    {"name": "httpx", "type": "HTTP", "desc": "支持异步的现代化 HTTP 客户端"},
    {"name": "aiohttp", "type": "HTTP", "desc": "异步 HTTP 客户端/服务器"},
    {"name": "sqlalchemy", "type": "数据库", "desc": "Python 最流行的 ORM"},
    {"name": "pymysql", "type": "数据库", "desc": "纯 Python 的 MySQL 驱动（配合共享 MySQL）"},
    {"name": "psycopg2-binary", "type": "数据库", "desc": "PostgreSQL 驱动"},
    {"name": "pymongo", "type": "数据库", "desc": "MongoDB 驱动"},
    {"name": "redis", "type": "缓存", "desc": "Redis 客户端（配合共享 MySQL/Redis）"},
    {"name": "celery", "type": "任务队列", "desc": "分布式异步任务队列"},
    {"name": "numpy", "type": "数据处理", "desc": "数值计算基础库"},
    {"name": "pandas", "type": "数据处理", "desc": "数据分析与表格处理"},
    {"name": "pillow", "type": "图像处理", "desc": "图片处理库（PIL）"},
    {"name": "pydantic", "type": "工具", "desc": "数据校验与类型解析"},
    {"name": "python-dotenv", "type": "工具", "desc": "读取 .env 环境变量文件"},
    {"name": "loguru", "type": "工具", "desc": "开箱即用的日志库"},
]


@router.get("/panel/instance/py/packages")
def panel_py_packages(dep: Instance = Depends(_dep)):
    """依赖安装列表：候选目录 + pip list 检测（三态），仅 Python 实例。"""
    _require_python(dep)
    client = svc.get_docker()
    try:
        c = client.containers.get(svc.container_name(dep.id))
        running = c.status == "running"
        cid = c.id
    except Exception:  # noqa: BLE001
        running = False
        cid = None
    installed: set = set()
    if running:
        out = _php_exec(client, cid, ["pip", "list", "--format=freeze"])
        installed = {ln.split("==")[0].strip().lower()
                     for ln in out.splitlines() if "==" in ln}
    catalog = [{**it, "installed": (it["name"].lower() in installed) if running else None}
               for it in PY_PKG_CATALOG]
    return {"running": running, "catalog": catalog,
            "total_installed": len(installed) if running else None}


# ---------- pip 镜像加速（内置国内源，写容器 /etc/pip.conf 即时生效） ----------

PY_MIRRORS = [
    {"name": "清华大学", "url": "https://pypi.tuna.tsinghua.edu.cn/simple"},
    {"name": "阿里云", "url": "https://mirrors.aliyun.com/pypi/simple/"},
    {"name": "中科大", "url": "https://mirrors.ustc.edu.cn/pypi/simple"},
    {"name": "腾讯云", "url": "https://mirrors.cloud.tencent.com/pypi/simple"},
    {"name": "华为云", "url": "https://mirrors.huaweicloud.com/repository/pypi/simple"},
    {"name": "官方源", "url": "https://pypi.org/simple"},
]

_URL_RE = re.compile(r"^https://[A-Za-z0-9.\-_]+(:\d{2,5})?/[A-Za-z0-9.\-_/]*$")


def _pip_mirror_of(client, cid: str) -> str:
    out = _php_exec(client, cid, ["sh", "-c", "cat /etc/pip.conf 2>/dev/null || true"])
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.lower().startswith("index-url"):
            return ln.split("=", 1)[1].strip()
    return "https://pypi.org/simple"  # 未配置 = 官方


@router.get("/panel/instance/pip/mirror")
def panel_pip_mirror_get(dep: Instance = Depends(_dep)):
    _require_python(dep)
    client = svc.get_docker()
    try:
        c = client.containers.get(svc.container_name(dep.id))
        if c.status != "running":
            return {"running": False, "current": None, "mirrors": PY_MIRRORS}
        return {"running": True, "current": _pip_mirror_of(client, c.id), "mirrors": PY_MIRRORS}
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        return {"running": False, "current": None, "mirrors": PY_MIRRORS}


@router.put("/panel/instance/pip/mirror")
def panel_pip_mirror_put(body: dict, dep: Instance = Depends(_dep), db: Session = Depends(get_db)):
    """设置 pip 镜像源：写 /etc/pip.conf（容器重建后恢复官方默认，可重设）。"""
    _require_python(dep)
    url = str((body or {}).get("url", "")).strip()
    if not _URL_RE.match(url):
        raise HTTPException(status_code=400, detail="镜像地址格式不正确（需 https 链接）")
    client = svc.get_docker()
    try:
        c = client.containers.get(svc.container_name(dep.id))
        if c.status != "running":
            raise HTTPException(status_code=409, detail="实例未运行，请先启动实例")
        host = url.split("//", 1)[1].split("/", 1)[0]
        conf = f"[global]\nindex-url = {url}\ntrusted-host = {host}\n"
        _php_exec(client, c.id, ["sh", "-c",
                                 "printf '" + conf.replace("'", "") + "' > /etc/pip.conf"])
        named = next((m["name"] for m in PY_MIRRORS if m["url"] == url), url)
        svc.log_op(db, dep.id, "pip_mirror", detail=named)
        db.commit()
        return {"current": url}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"设置镜像失败：{e}")


# ---------- PHP 扩展（php -m 列表 + pecl 在线安装，仅 PHP 实例） ----------

def _require_php(inst: Instance) -> None:
    if not svc.is_php(inst.image):
        raise HTTPException(status_code=400, detail="该功能仅支持 PHP 环境")


# 扩展目录（宝塔同款思路）：
#   builtin=官方镜像默认已装；core=PHP 核心扩展（pecl 无包，docker-php-ext-install 编译，可带
#           deps=apt 系统库 / configure=编译前配置参数）；
#   pecl=pecl 在线编译（可带 deps / pre=前置依赖扩展 / interactive=安装问句回车取默认）；
#   manual=运行时依赖复杂或私有 loader（SG/oracle 等），需管理员构建进镜像。
PHP_EXT_CATALOG = [
    # ---- 核心扩展（pecl 上无包，镜像内源码编译） ----
    {"name": "mysqli", "type": "数据库", "desc": "MySQL 改进版扩展", "core": True},
    {"name": "pdo_mysql", "type": "数据库", "desc": "PDO MySQL 驱动", "core": True},
    {"name": "gd", "type": "图形库", "desc": "图片处理（含 freetype/jpeg 字体）", "core": True,
     "deps": "libpng-dev libjpeg62-turbo-dev libfreetype-dev", "configure": "--with-freetype --with-jpeg"},
    {"name": "zip", "type": "通用扩展", "desc": "zip 压缩包读写", "core": True, "deps": "libzip-dev"},
    {"name": "exif", "type": "通用扩展", "desc": "读取图片 EXIF 信息", "core": True},
    {"name": "intl", "type": "通用扩展", "desc": "国际化支持", "core": True, "deps": "libicu-dev"},
    {"name": "xsl", "type": "通用扩展", "desc": "XSL 解析", "core": True, "deps": "libxslt1-dev"},
    {"name": "soap", "type": "通用扩展", "desc": "SOAP 协议（官方扩展）", "core": True},
    {"name": "sockets", "type": "通用扩展", "desc": "socket 通信（官方扩展）", "core": True},
    {"name": "pcntl", "type": "通用扩展", "desc": "多进程控制（官方扩展）", "core": True},
    {"name": "shmop", "type": "通用扩展", "desc": "共享内存（官方扩展）", "core": True},
    {"name": "sysvmsg", "type": "通用扩展", "desc": "System V 消息队列（官方扩展）", "core": True},
    {"name": "sysvshm", "type": "通用扩展", "desc": "System V 共享内存（官方扩展）", "core": True},
    {"name": "gettext", "type": "通用扩展", "desc": "多语言翻译（官方扩展）", "core": True},
    {"name": "bz2", "type": "通用扩展", "desc": "bzip2 压缩（官方扩展）", "core": True, "deps": "libbz2-dev"},
    {"name": "gmp", "type": "通用扩展", "desc": "大数运算（官方扩展）", "core": True, "deps": "libgmp-dev"},
    {"name": "snmp", "type": "通用扩展", "desc": "SNMP 网络管理（官方扩展）", "core": True, "deps": "libsnmp-dev"},
    {"name": "ldap", "type": "通用扩展", "desc": "LDAP 目录服务（官方扩展）", "core": True, "deps": "libldap-dev"},
    {"name": "pspell", "type": "通用扩展", "desc": "拼写检查（官方扩展）", "core": True, "deps": "libpspell-dev"},
    {"name": "enchant", "type": "通用扩展", "desc": "拼写库抽象（官方扩展）", "core": True, "deps": "libenchant-2-dev"},
    {"name": "pgsql", "type": "数据库", "desc": "PostgreSQL 连接（官方扩展）", "core": True, "deps": "libpq-dev"},
    {"name": "pdo_pgsql", "type": "数据库", "desc": "PDO PostgreSQL 驱动（官方扩展）", "core": True, "deps": "libpq-dev"},
    # ---- 官方镜像默认已装 ----
    {"name": "opcache", "type": "缓存器", "desc": "用于加速 PHP 脚本", "builtin": True},
    {"name": "fileinfo", "type": "通用扩展", "desc": "文件类型识别", "builtin": True},
    {"name": "mbstring", "type": "通用扩展", "desc": "多字节字符串处理", "builtin": True},
    {"name": "calendar", "type": "通用扩展", "desc": "历法转换（官方扩展）", "builtin": True},
    {"name": "readline", "type": "通用扩展", "desc": "交互式输入（官方扩展）", "builtin": True},
    # ---- pecl 在线编译 ----
    {"name": "redis", "type": "缓存器", "desc": "基于内存的可持久化的 Key-Value 数据库", "pecl": "redis"},
    {"name": "memcache", "type": "缓存器", "desc": "强大的内容缓存器", "pecl": "memcache"},
    {"name": "memcached", "type": "缓存器", "desc": "比 memcache 支持更多高级功能", "pecl": "memcached",
     "deps": "libz-dev libmemcached-dev"},
    {"name": "igbinary", "type": "缓存器", "desc": "二进制序列化（redis/memcached/yac 的优化前置）", "pecl": "igbinary"},
    {"name": "yac", "type": "缓存器", "desc": "高性能无锁共享内存 Cache（自动先装 igbinary）", "pecl": "yac",
     "pre": "igbinary"},
    {"name": "apcu", "type": "缓存器", "desc": "脚本缓存器", "pecl": "apcu"},
    {"name": "imagick", "type": "通用扩展", "desc": "Imagick 高性能图形库", "pecl": "imagick",
     "deps": "libmagickwand-dev"},
    {"name": "mongodb", "type": "数据库", "desc": "MongoDB 数据库连接驱动", "pecl": "mongodb"},
    {"name": "sqlsrv", "type": "数据库", "desc": "SQL Server 扩展（连接需运行时 msodbcsql）", "pecl": "sqlsrv",
     "deps": "unixodbc-dev"},
    {"name": "pdo_sqlsrv", "type": "数据库", "desc": "SQL Server PDO 驱动", "pecl": "pdo_sqlsrv",
     "deps": "unixodbc-dev"},
    {"name": "rdkafka", "type": "通用扩展", "desc": "Kafka 消息队列客户端", "pecl": "rdkafka",
     "deps": "librdkafka-dev"},
    {"name": "zmq", "type": "通用扩展", "desc": "ZeroMQ 通用消息库", "pecl": "zmq", "deps": "libzmq3-dev"},
    {"name": "swoole", "type": "通用扩展", "desc": "高性能协程网络引擎（最新版，具体版本以 phpinfo 为准）",
     "pecl": "swoole", "interactive": True},
    {"name": "swoole_loader", "type": "脚本加密", "desc": "SourceGuardian 解密 loader（SG11/SG14-17，按 PHP 版本自动装载）",
     "loader": "sg"},
    {"name": "xlswriter", "type": "通用扩展", "desc": "Excel xlsx 高性能读写", "pecl": "xlswriter"},
    {"name": "yaf", "type": "框架", "desc": "C 语言编写的 PHP 框架", "pecl": "yaf"},
    {"name": "phalcon", "type": "框架", "desc": "C 语言编写的 PHP 框架（自动先装 psr 依赖）", "pecl": "phalcon",
     "pre": "psr"},
    {"name": "grpc", "type": "通用扩展", "desc": "gRPC 客户端", "pecl": "grpc", "deps": "zlib1g-dev"},
    {"name": "protobuf", "type": "通用扩展", "desc": "protobuf 编解码", "pecl": "protobuf"},
    {"name": "xhprof", "type": "调试器", "desc": "PHP 性能分析", "pecl": "xhprof"},
    {"name": "xdebug", "type": "调试器", "desc": "开源的 PHP 程序调试器", "pecl": "xdebug"},
    {"name": "event", "type": "通用扩展", "desc": "libevent 库接口", "pecl": "event", "deps": "libevent-dev",
     "interactive": True},
    {"name": "mailparse", "type": "邮件服务", "desc": "邮件消息处理", "pecl": "mailparse"},
    {"name": "yaml", "type": "通用扩展", "desc": "YAML 解析", "pecl": "yaml", "deps": "libyaml-dev"},
    {"name": "ssh2", "type": "通用扩展", "desc": "SSH2 协议（libssh2）", "pecl": "ssh2", "deps": "libssh2-1-dev"},
    {"name": "zstd", "type": "通用扩展", "desc": "Zstandard 压缩解压", "pecl": "zstd", "deps": "libzstd-dev"},
    {"name": "mcrypt", "type": "通用扩展", "desc": "mcrypt 加密/解密", "pecl": "mcrypt",
     "deps": "libmcrypt-dev"},
    {"name": "imap", "type": "邮件服务", "desc": "邮件服务器必备（依赖较多，可能安装失败）", "pecl": "imap",
     "deps": "libkrb5-dev", "interactive": True},
    {"name": "smbclient", "type": "通用扩展", "desc": "Samba 相关功能与 smb 流", "pecl": "smbclient",
     "deps": "libsmbclient-dev"},
    # ---- 解密 loader（运行时下载对应 PHP 版本的预编译 .so，无需编译/构建镜像） ----
    {"name": "ioncube", "type": "脚本解密", "desc": "用于解密 ionCube Encoder 加密脚本", "loader": "ioncube"},
]


def _php_exec(client, container_id: str, cmd: list) -> str:
    """容器内执行命令并返回 stdout（退出码非 0 抛 500）。"""
    try:
        exec_id = client.api.exec_create(container_id, cmd)["Id"]
        out = client.api.exec_start(exec_id).decode("utf-8", "replace")
        code = client.api.exec_inspect(exec_id).get("ExitCode")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Docker 调用失败：{e}")
    if code != 0:
        raise HTTPException(status_code=500, detail="容器内命令执行失败")
    return out


def _running_php_container(dep: Instance):
    client = svc.get_docker()
    try:
        c = client.containers.get(svc.container_name(dep.id))
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=409, detail="容器未运行，请先启动实例")
    if c.status != "running":
        raise HTTPException(status_code=409, detail="容器未运行，请先启动实例")
    return client, c.id


@router.get("/panel/instance/php/extensions")
def panel_php_extensions(dep: Instance = Depends(_dep)):
    """扩展检测：目录 + 已启用（php -m）+ 禁用函数（disable_functions）。
    容器停止时目录仍可见，enabled=None 表示未知。"""
    _require_php(dep)
    client = svc.get_docker()
    try:
        c = client.containers.get(svc.container_name(dep.id))
        running = c.status == "running"
        cid = c.id
    except Exception:  # noqa: BLE001
        running = False
        cid = None
    modules: list = []
    zend: list = []
    disabled: list = []
    if running:
        out = _php_exec(client, cid, ["php", "-m"])
        cur = modules
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("[Zend Modules]"):
                cur = zend
                continue
            cur.append(line)
        # 禁用函数以站点入口命令注入的为准（实例可在「禁用函数」页自定义，与节点默认不同）
        disabled = svc.parse_disabled_from_cmd(dep.start_cmd)
        if disabled is None:  # 启动命令里没有该段（非常旧的实例）→ 按节点默认展示
            disabled = [f for f in (x.strip() for x in settings.php_disable_functions.split(",")) if f]
    # 名字归一：php -m 的 Zend 段显示 "Zend OPcache"，目录名是 opcache
    enabled = {m.lower() for m in modules} | {z.lower().removeprefix("zend ") for z in zend}
    catalog = [{**item, "enabled": (item["name"].lower() in enabled) if running else None}
               for item in PHP_EXT_CATALOG]
    return {"running": running, "modules": modules, "zend": zend,
            "catalog": catalog, "disabled": disabled}


_PECL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*$")


# ---------- 禁用函数自选（实例级，写进启动命令重建生效） ----------

# 面板可勾选的候选函数（常见危险/高频禁用项）
PHP_FUNC_CATALOG = [
    ("exec", "执行外部程序"),
    ("shell_exec", "Shell 执行（反引号 equivalent）"),
    ("system", "执行系统命令并显示输出"),
    ("passthru", "执行命令并输出原始结果"),
    ("proc_open", "打开进程句柄"),
    ("popen", "打开进程文件指针"),
    ("pcntl_exec", "执行指定程序（进程控制）"),
    ("pcntl_fork", "派生子进程"),
    ("putenv", "设置环境变量"),
    ("dl", "运行时加载 PHP 扩展"),
    ("eval", "执行字符串代码（语言构造）"),
    ("assert", "执行断言内代码"),
    ("curl_multi_exec", "批量 curl 执行"),
    ("chown", "修改文件属主"),
    ("chmod", "修改文件权限"),
    ("chgrp", "修改文件属组"),
    ("link", "创建硬链接"),
    ("symlink", "创建符号链接"),
    ("disk_free_space", "查看磁盘剩余空间"),
    ("disk_total_space", "查看磁盘总空间"),
    ("php_uname", "获取系统信息"),
    ("getenv", "读取环境变量"),
    ("show_source", "显示源码（highlight_file 别名）"),
    ("pfsockopen", "持久 socket 连接"),
]

_FUNC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _effective_disabled(dep: Instance) -> list[str]:
    seg = svc.parse_disabled_from_cmd(dep.start_cmd)
    if seg is None:  # 命令里没有该段 → 节点默认
        seg = [f for f in (x.strip() for x in settings.php_disable_functions.split(",")) if f]
    return seg


@router.get("/panel/instance/php/disabled")
def panel_php_disabled_get(dep: Instance = Depends(_dep)):
    """禁用函数自选页：候选目录 + 当前生效列表 + 是否自定义过。"""
    _require_php(dep)
    return {
        "catalog": [{"name": n, "desc": d} for n, d in PHP_FUNC_CATALOG],
        "disabled": _effective_disabled(dep),
        "is_custom": bool(dep.disabled_funcs),
    }


@router.put("/panel/instance/php/disabled")
def panel_php_disabled_put(body: dict, dep: Instance = Depends(_dep), db: Session = Depends(get_db)):
    """保存实例禁用列表：写进启动命令并重建容器（立即生效）。
    body: {"functions": ["exec", ...]}；空数组=全开放。"""
    _require_php(dep)
    if svc.has_running_job(dep.id):
        raise HTTPException(status_code=409, detail="已有任务正在执行，请稍后再试")
    funcs = (body or {}).get("functions")
    if not isinstance(funcs, list):
        raise HTTPException(status_code=400, detail="参数格式错误")
    clean: list[str] = []
    for f in funcs:
        f = str(f).strip()
        if not f:
            continue
        if not _FUNC_NAME_RE.match(f):
            raise HTTPException(status_code=400, detail=f"非法函数名：{f}")
        if f not in clean:
            clean.append(f)
    if len(clean) > 60:
        raise HTTPException(status_code=400, detail="禁用函数数量过多")

    dep.start_cmd = svc.apply_disabled_to_cmd(dep.start_cmd, clean)
    dep.disabled_funcs = ",".join(clean)
    svc.log_op(db, dep.id, "disabled_funcs", detail=f"{len(clean)} 个")
    was_running = dep.status == "running"
    if was_running:
        svc.stop_instance(dep)
    svc.recreate_container(dep)
    if was_running:
        svc.start_instance(dep)
        svc.diagnose_startup(dep)
    db.commit()
    return {"disabled": clean, "restarted": was_running, "status": dep.status}


@router.post("/panel/instance/php/disabled/reset")
def panel_php_disabled_reset(dep: Instance = Depends(_dep), db: Session = Depends(get_db)):
    """恢复节点默认：用 PHP_DISABLE_FUNCTIONS 配置重写启动命令段。"""
    _require_php(dep)
    if svc.has_running_job(dep.id):
        raise HTTPException(status_code=409, detail="已有任务正在执行，请稍后再试")
    default = [f for f in (x.strip() for x in settings.php_disable_functions.split(",")) if f]
    dep.start_cmd = svc.apply_disabled_to_cmd(dep.start_cmd, default)
    dep.disabled_funcs = ""
    svc.log_op(db, dep.id, "disabled_funcs", detail="恢复节点默认")
    was_running = dep.status == "running"
    if was_running:
        svc.stop_instance(dep)
    svc.recreate_container(dep)
    if was_running:
        svc.start_instance(dep)
        svc.diagnose_startup(dep)
    db.commit()
    return {"disabled": default, "restarted": was_running, "status": dep.status}


def _ext_install_cmd(pecl: str, deps: str = "", interactive: bool = False,
                     core: bool = False, configure: str = "", pre: str = "") -> list:
    parts = []
    if deps:
        parts.append(f"apt-get update -qq && apt-get install -y -qq {deps}")
    if core:
        # PHP 核心扩展（mysqli/gd 等）pecl 上无包：官方镜像自带源码，直接 docker-php-ext-install
        if configure:
            parts.append(f"docker-php-ext-configure {pecl} {configure}")
        parts.append(f"docker-php-ext-install -j$(nproc) {pecl}")
        return ["sh", "-c", " && ".join(parts)]
    if pre:  # 前置依赖扩展（如 phalcon→psr、yac→igbinary），失败不阻断主扩展安装
        parts.append(f"(pecl install {pre} && docker-php-ext-enable {pre}) || true")
    # test 文件重定向到 /tmp：绕开 PEAR 向 /usr/local/lib/php/test 写入的权限问题
    parts.append("mkdir -p /tmp/pear-tests && pear config-set test_dir /tmp/pear-tests")
    # 已安装（上次编译成功但未启用/被卸载）时跳过编译，直接走到启用步骤；
    # pecl 无此包（如手动安装 mysqli 等核心扩展）时自动回退 docker-php-ext-install
    pecl_cmd = (f"(pecl install {pecl} || {{ pecl list | grep -q {pecl} && "
                f"echo '[面板] 扩展已编译过，跳过重新编译' || exit 1; }}) "
                f"|| docker-php-ext-install -j$(nproc) {pecl}")
    if interactive:  # 交互询问项一路回车取默认值（swoole 等问项较多，多给几个回车）
        pecl_cmd = f"printf '\\n\\n\\n\\n\\n\\n' | {pecl_cmd}"
    parts.append(pecl_cmd)
    parts.append(f"docker-php-ext-enable {pecl} 2>/dev/null || true")
    return ["sh", "-c", " && ".join(parts)]


def _loader_install_cmd(kind: str) -> list:
    """解密 loader（ionCube / SourceGuardian）：下载官方预编译包，按 PHP 版本取 .so 放置并写 ini。"""
    if kind == "ioncube":
        url = "https://downloads.ioncube.com/loader_downloads/ioncube_loaders_lin_x86-64.tar.gz"
        script = (
            "cd /tmp && rm -rf ioncube loaders.tgz && "
            f"curl -fsSL {url} -o loaders.tgz && tar xzf loaders.tgz && "
            "V=$(php -r 'echo PHP_MAJOR_VERSION.\".\".PHP_MINOR_VERSION;') && "
            "SO=$(ls ioncube/ioncube_loader_lin_$V.so) && "
            "D=$(php-config --extension-dir) && cp \"$SO\" \"$D/\" && "
            "printf 'zend_extension=%s/ioncube_loader_lin_%s.so\\n' \"$D\" \"$V\" "
            "> /usr/local/etc/php/conf.d/01-ioncube.ini && "
            "php -v | grep -qi ioncube && echo '[面板] ionCube Loader 已就位（重启实例后生效）'"
        )
    elif kind == "sg":
        url = "https://www.sourceguardian.com/loaders/download/loaders.linux-x86_64.tar.gz"
        script = (
            "cd /tmp && rm -rf sgloaders loaders.tgz && "
            f"curl -fsSL {url} -o loaders.tgz && mkdir sgloaders && tar xzf loaders.tgz -C sgloaders && "
            "V=$(php -r 'echo PHP_MAJOR_VERSION.\".\".PHP_MINOR_VERSION;') && "
            "SO=$(ls sgloaders/ixed.$V.lin) && "
            "D=$(php-config --extension-dir) && cp \"$SO\" \"$D/\" && "
            "printf 'zend_extension=%s/ixed.%s.lin\\n' \"$D\" \"$V\" "
            "> /usr/local/etc/php/conf.d/01-swoole_loader.ini && "
            "php -v | grep -qi sourceguardian && echo '[面板] SourceGuardian Loader 已就位（重启实例后生效）'"
        )
    else:
        raise ValueError(f"未知 loader: {kind}")
    return ["sh", "-c", script]


@router.post("/panel/instance/php/extensions/install")
def panel_php_ext_install(body: dict, dep: Instance = Depends(_dep), db: Session = Depends(get_db)):
    """安装扩展（pecl 编译 1-3 分钟），完成后需重启实例生效。"""
    _require_php(dep)
    if svc.has_running_job(dep.id):
        raise HTTPException(status_code=409, detail="已有任务正在执行，请等日志弹窗显示完成后重试")
    name = ((body or {}).get("name") or "").strip()
    if not _PECL_RE.match(name):
        raise HTTPException(status_code=400, detail="非法扩展名（示例：redis、xdebug、imagick）")
    item = next((x for x in PHP_EXT_CATALOG if x["name"] == name), None)
    if item and item.get("builtin"):
        raise HTTPException(status_code=400, detail=f"{name} 为官方内置扩展，无需安装")
    if item and item.get("manual"):
        raise HTTPException(status_code=400, detail=f"{name} 需管理员构建进镜像后使用，请联系管理员")
    if item and item.get("loader"):
        job = svc.start_exec(dep, f"安装扩展 {name}", _loader_install_cmd(item["loader"]))
        svc.log_op(db, dep.id, "deps_install", detail=f"loader:{item['loader']}")
        db.commit()
        return {"job_id": job.id}
    pecl = (item or {}).get("pecl", name)
    job = svc.start_exec(
        dep, f"安装扩展 {name}",
        _ext_install_cmd(pecl, (item or {}).get("deps", ""), (item or {}).get("interactive", False),
                         core=bool((item or {}).get("core")),
                         configure=(item or {}).get("configure", ""),
                         pre=(item or {}).get("pre", "")))
    svc.log_op(db, dep.id, "deps_install", detail=f"{'core' if (item or {}).get('core') else 'pecl'}:{pecl}")
    db.commit()
    return {"job_id": job.id}


@router.post("/panel/instance/php/extensions/uninstall")
def panel_php_ext_uninstall(body: dict, dep: Instance = Depends(_dep), db: Session = Depends(get_db)):
    """卸载扩展 = 移除 ini 停用（保留 .so，重装无需再编译），重启实例后生效。"""
    _require_php(dep)
    name = ((body or {}).get("name") or "").strip()
    if not _PECL_RE.match(name):
        raise HTTPException(status_code=400, detail="非法扩展名")
    item = next((x for x in PHP_EXT_CATALOG if x["name"] == name), None)
    if item and (item.get("builtin") or item.get("manual")):
        raise HTTPException(status_code=400, detail=f"{name} 不支持在线卸载")
    pecl = (item or {}).get("pecl", name)
    client, cid = _running_php_container(dep)
    _php_exec(client, cid, ["sh", "-c",
             f"rm -f /usr/local/etc/php/conf.d/docker-php-ext-{pecl}.ini /usr/local/etc/php/conf.d/*{pecl}*.ini && echo OK"])
    svc.log_op(db, dep.id, "deps_uninstall", detail=f"pecl:{pecl}")
    db.commit()
    return {"detail": f"{name} 已停用，重启实例后生效"}


# ---------- 文件操作 ----------

def _root(inst: Instance) -> str:
    fs.ensure_root(inst.host_dir)
    return inst.host_dir


@router.get("/panel/instance/files")
def panel_files(dep: Instance = Depends(_dep), path: str = "/"):
    return {"path": path, "entries": fs.list_dir(_root(dep), path)}


@router.get("/panel/instance/files/content")
def panel_file_content(dep: Instance = Depends(_dep), path: str = "/"):
    data = fs.read_text(_root(dep), path)
    data["path"] = path
    return data


@router.put("/panel/instance/files/upload")
def panel_upload(dep: Instance = Depends(_dep), path: str = "/", file: UploadFile = None):
    if file is None:
        raise HTTPException(status_code=400, detail="未收到文件")
    limit = settings.upload_limit_mb * 1024 * 1024
    return fs.save_upload(_root(dep), path, file.filename, file.file, limit)


@router.post("/panel/instance/files/save")
def panel_save(dep: Instance = Depends(_dep), body: dict = None):
    if not body or not body.get("path"):
        raise HTTPException(status_code=400, detail="参数缺失")
    fs.write_text(_root(dep), body["path"], body.get("content", ""))
    return {"detail": "已保存"}


@router.post("/panel/instance/files/mkdir")
def panel_mkdir(dep: Instance = Depends(_dep), body: dict = None):
    if not body or not body.get("path"):
        raise HTTPException(status_code=400, detail="参数缺失")
    fs.mkdir(_root(dep), body["path"])
    return {"detail": "已创建"}


@router.post("/panel/instance/files/rename")
def panel_rename(dep: Instance = Depends(_dep), body: dict = None):
    if not body or not body.get("src") or not body.get("dst"):
        raise HTTPException(status_code=400, detail="参数缺失")
    fs.rename(_root(dep), body["src"], body["dst"])
    return {"detail": "已重命名"}


@router.delete("/panel/instance/files")
def panel_delete_path(dep: Instance = Depends(_dep), path: str = ""):
    fs.delete(_root(dep), path)
    return {"detail": "已删除"}


@router.get("/panel/instance/files/download")
def panel_download(dep: Instance = Depends(_dep), path: str = ""):
    import os
    from fastapi.responses import FileResponse
    target = fs.resolve_path(_root(dep), path)
    if not os.path.isfile(target):
        raise HTTPException(status_code=400, detail="仅支持下载文件")
    return FileResponse(target, filename=os.path.basename(target))


@router.get("/panel/instance/files/backup")
def panel_backup(dep: Instance = Depends(_dep)):
    import time
    from fastapi.responses import FileResponse
    tmp_path = fs.create_backup(_root(dep))
    fname = f"instance_{dep.id}_backup_{time.strftime('%Y%m%d_%H%M%S')}.tar.gz"
    return FileResponse(
        tmp_path, filename=fname, media_type="application/gzip",
        background=BackgroundTask(_cleanup, tmp_path))


def _cleanup(path: str) -> None:
    import os
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@router.post("/panel/instance/files/restore")
def panel_restore(dep: Instance = Depends(_dep), file: UploadFile = None):
    if file is None:
        raise HTTPException(status_code=400, detail="未收到文件")
    svc.sync_status(dep)
    if dep.status == "running":
        raise HTTPException(status_code=409, detail="实例运行中，请先停止后再恢复")
    out = fs.restore_backup(_root(dep), file.file, dep.disk_quota * 1024 * 1024)
    svc.log_op(db, dep.id, "restore", detail=file.filename or "")
    db.commit()
    return out


@router.post("/panel/instance/files/copy")
def panel_copy(dep: Instance = Depends(_dep), body: dict = None):
    if not body or not body.get("src") or not body.get("dst"):
        raise HTTPException(status_code=400, detail="参数缺失")
    fs.copy_path(_root(dep), body["src"], body["dst"])
    return {"detail": "已复制"}


@router.post("/panel/instance/files/zip")
def panel_zip(dep: Instance = Depends(_dep), body: dict = None):
    if not body or not body.get("paths") or not body.get("name"):
        raise HTTPException(status_code=400, detail="参数缺失")
    fs.zip_entries(_root(dep), body["paths"], body["name"], body.get("z_type", "tar.gz"))
    return {"detail": "压缩完成"}


@router.post("/panel/instance/files/unzip")
def panel_unzip(dep: Instance = Depends(_dep), body: dict = None):
    if not body or not body.get("src"):
        raise HTTPException(status_code=400, detail="参数缺失")
    dest = fs.unzip_entry(_root(dep), body["src"])
    return {"detail": "解压完成", "dest": dest}


@router.get("/panel/instance/files/path-size")
def panel_path_size(dep: Instance = Depends(_dep), path: str = ""):
    return {"size": fs.dir_size(_root(dep), path)}


@router.get("/panel/instance/files/preview")
def panel_preview(dep: Instance = Depends(_dep), path: str = ""):
    return fs.image_preview(_root(dep), path)


@router.post("/panel/instance/files/upload-exists")
def panel_upload_exists(dep: Instance = Depends(_dep), body: dict = None):
    """分片上传断点查询：返回续传起点。"""
    if not body or not body.get("file_name"):
        raise HTTPException(status_code=400, detail="参数缺失")
    return fs.upload_exists(_root(dep), body.get("path", "/"),
                            body["file_name"], int(body.get("total_size", 0)))


@router.post("/panel/instance/files/upload-chunk")
def panel_upload_chunk(dep: Instance = Depends(_dep), file: UploadFile = None,
                       path: str = Form("/"), file_name: str = Form(...),
                       total_size: int = Form(0), start: int = Form(0)):
    """分片上传：按 start 偏移追加写入（start=0 截断新建）。"""
    if file is None:
        raise HTTPException(status_code=400, detail="未收到分片数据")
    return fs.upload_chunk(_root(dep), path, file_name, total_size, start, file.file)


# ---------- WebSocket（?ptoken= 鉴权） ----------

def _iid_ws(ws: WebSocket, db: Session) -> Instance | None:
    iid = _iid_from_token(ws.query_params.get("ptoken", ""))
    if iid is None:
        return None
    try:
        return _inst_of(iid, db)
    except HTTPException:
        return None


@router.websocket("/panel/ws/term")
async def panel_ws_term(ws: WebSocket):
    """容器内交互式终端：docker exec PTY 流（xterm.js 前端，qnbt 同款）。

    协议：收 {"type":"input","data"} / {"type":"resize","cols","rows"}；
    发 {"type":"data","data"} / {"type":"closed"}。
    """
    db: Session = SessionLocal()
    sock = None
    client = None
    exec_id = ""
    out_task = None
    loop = asyncio.get_running_loop()
    try:
        inst = _iid_ws(ws, db)
        if inst is None:
            await ws.close(code=4401)
            return
        if _expired(inst):  # 终端属写操作，到期即拒（日志 WS 只读可继续用）
            await ws.close(code=4403)
            return
        c = svc._pip_container(inst)
        client = get_docker()
        # 提示符随 cd 实时变化（dash/busybox ash/bash 均支持 PS1 参数展开）
        inner = f"export TERM=xterm-256color PS1='app@{inst.name}:$PWD\\$ '; exec /bin/sh"
        exec_id = client.api.exec_create(
            c.id, ["/bin/sh", "-c", inner],
            stdin=True, stdout=True, stderr=True, tty=True, workdir="/app",
        )["Id"]
        sock = client.api.exec_start(exec_id, socket=True)
        # docker-py 7.x 返回只读 SocketIO，取底层原生 socket 才能写 stdin
        if hasattr(sock, "_sock"):
            sock = sock._sock
        await ws.accept()
    except HTTPException as e:
        try:
            await ws.accept()
            await ws.send_json({"type": "error", "msg": e.detail})
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
        return

    out_q: asyncio.Queue = asyncio.Queue(maxsize=2000)

    def _sock_read(s, n=4096):
        """docker-py exec_start(socket=True) 返回 socket.SocketIO（read/write），
        旧版返回原生 socket（recv/sendall），两者兼容。"""
        if hasattr(s, "recv"):
            return s.recv(n)
        return s.read(n)

    def _sock_write(s, data: bytes):
        if hasattr(s, "sendall"):
            s.sendall(data)
        else:
            s.write(data)
            try:
                s.flush()
            except Exception:  # noqa: BLE001
                pass

    def _read_exact(s, n):
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def _read_loop():
        """exec 流读取线程。实测部分引擎 tty 模式下 exec 流仍为帧协议
        [1B流类型|3B保留|4B大端长度|payload]，按首 8 字节探测：合法帧头则逐帧
        demux，否则按 raw 流直读。EOF/异常后放哨兵结束。"""
        def push(text):
            loop.call_soon_threadsafe(out_q.put_nowait, text)

        try:
            while True:
                header = _read_exact(sock, 8)
                if not header:
                    break
                stream_type = header[0]
                size = int.from_bytes(header[4:8], "big")
                if (stream_type not in (0, 1, 2) or header[1:4] != b"\x00\x00\x00"
                        or size > 1048576):
                    # raw 流：首块原样输出，后续直读直到 EOF
                    push(header.decode("utf-8", "replace"))
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk:
                            return
                        push(chunk.decode("utf-8", "replace"))
                if size:
                    data = _read_exact(sock, size)
                    if data:
                        push(data.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            pass
        loop.call_soon_threadsafe(out_q.put_nowait, None)

    threading.Thread(target=_read_loop, daemon=True).start()

    async def _pump_out():
        while True:
            data = await out_q.get()
            if data is None:
                try:
                    await ws.send_json({"type": "closed"})
                except Exception:  # noqa: BLE001
                    pass
                return
            await ws.send_json({"type": "data", "data": data})

    out_task = asyncio.create_task(_pump_out())
    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")
            if mtype == "resize":
                cols = max(20, min(int(msg.get("cols") or 120), 500))
                rows = max(5, min(int(msg.get("rows") or 30), 200))
                try:
                    client.api.exec_resize(exec_id, width=cols, height=rows)
                except Exception:  # noqa: BLE001
                    pass
            elif mtype == "input":
                data = msg.get("data") or ""
                if data:
                    _sock_write(sock, data.encode("utf-8"))
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        if out_task:
            out_task.cancel()
        if sock:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
        db.close()


@router.websocket("/panel/ws/logs")
async def panel_ws_logs(ws: WebSocket):
    import asyncio
    import threading

    from app.services.instance_service import container_name, try_get_docker

    db: Session = SessionLocal()
    try:
        inst = _iid_ws(ws, db)
        if inst is None:
            await ws.close(code=4401)
            return
        client = try_get_docker()
        container = None
        if client is not None:
            try:
                container = client.containers.get(container_name(inst.id))
            except Exception:  # noqa: BLE001
                container = None
        if container is None:
            await ws.accept()
            await ws.send_text("[panel] 容器不存在或 Docker 不可用")
            await ws.close(code=4404)
            return

        await ws.accept()
        tail = int(ws.query_params.get("tail", "200") or 200)
        aq: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        done_flag = asyncio.Event()

        def worker():
            try:
                for chunk in container.logs(stream=True, follow=True,
                                            tail=max(1, min(tail, 5000)),
                                            stdout=True, stderr=True):
                    text = chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else str(chunk)
                    loop.call_soon_threadsafe(aq.put_nowait, text)
            except Exception:  # noqa: BLE001
                pass
            finally:
                loop.call_soon_threadsafe(aq.put_nowait, None)

        threading.Thread(target=worker, daemon=True).start()
        while True:
            item = await aq.get()
            if item is None:
                break
            try:
                await ws.send_text(item)
            except Exception:  # noqa: BLE001
                break
    except Exception:  # noqa: BLE001
        try:
            await ws.close(code=1011)
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


@router.websocket("/panel/ws/install")
async def panel_ws_install(ws: WebSocket):
    import asyncio

    from app.services.instance_service import EXEC_JOBS

    db: Session = SessionLocal()
    try:
        inst = _iid_ws(ws, db)
        if inst is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        job = EXEC_JOBS.get(ws.query_params.get("job_id", ""))
        if job is None or job.instance_id != inst.id:
            await ws.send_text("[panel] 安装任务不存在或已结束")
            await ws.close()
            return
        idx = 0
        while True:
            lines = list(job.lines)
            while idx < len(lines):
                try:
                    await ws.send_text(lines[idx])
                except Exception:  # noqa: BLE001
                    return
                idx += 1
            if job.done and idx >= len(list(job.lines)):
                await ws.send_text(f"[panel] 任务完成 exit={job.exit_code}")
                break
            await asyncio.sleep(0.3)
    except Exception:  # noqa: BLE001
        try:
            await ws.close(code=1011)
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()
