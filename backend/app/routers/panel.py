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
from app.services import file_service as fs
from app.services import instance_service as svc

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
    return _inst_of(_iid_of(request), db)


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
    return {"domain": dep.domain or ""}


@router.put("/panel/instance/domain")
def panel_set_domain(body: dict, dep: Instance = Depends(_dep), db: Session = Depends(get_db)):
    raw = (body or {}).get("domain", "").strip().lower()
    if not raw:
        if dep.domain:
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
    dep.domain = raw
    svc.log_op(db, dep.id, "domain_bind", detail=raw)
    db.commit()
    return {"domain": raw}


@router.get("/panel/instance/ops")
def panel_ops(dep: Instance = Depends(_dep), db: Session = Depends(get_db),
              limit: int = 20):
    rows = (db.query(AgentOpLog)
            .filter(AgentOpLog.instance_id == dep.id)
            .order_by(AgentOpLog.id.desc())
            .limit(max(1, min(limit, 100))).all())
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
    names = _check_names((body or {}).get("names", ""))
    job = svc.start_pip(dep, ["install", *names])
    svc.log_op(db, dep.id, "deps_install", detail=" ".join(names))
    db.commit()
    return {"job_id": job.id}


@router.post("/panel/instance/deps/uninstall")
def panel_uninstall_pkg(dep: Instance = Depends(_dep), db: Session = Depends(get_db),
                        body: dict = None):
    """手动卸载依赖。"""
    names = _check_names((body or {}).get("names", ""))
    job = svc.start_pip(dep, ["uninstall", "-y", *names])
    svc.log_op(db, dep.id, "deps_uninstall", detail=" ".join(names))
    db.commit()
    return {"job_id": job.id}


@router.get("/panel/instance/deps/installed")
def panel_deps_installed(dep: Instance = Depends(_dep)):
    """检测已安装依赖（容器内 pip list）。"""
    return {"packages": svc.pip_list(dep)}


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
