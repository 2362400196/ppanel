"""被控 Agent WebSocket（节点级鉴权 ?node_token=）：
实例日志跟踪 / 依赖安装回显 / 镜像拉取进度 / 宿主机终端。
"""
import asyncio
import threading

import docker
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.agent_auth import ws_node_ok
from app.database import SessionLocal
from app.models import Instance
from app.services import host_ops
from app.services.instance_service import EXEC_JOBS, container_name, try_get_docker

router = APIRouter()


def _inst_or_close(ws: WebSocket, db: Session, instance_id: int):
    inst = db.get(Instance, instance_id)
    if not inst:
        ws.accept()
        ws.send_text("[agent] 实例不存在")
        ws.close(code=4404)
        return None
    return inst


@router.websocket("/ws/instances/{instance_id}/logs")
async def agent_ws_logs(ws: WebSocket, instance_id: int):
    db: Session = SessionLocal()
    try:
        if not ws_node_ok(ws):
            await ws.close(code=4401)
            return
        inst = _inst_or_close(ws, db, instance_id)
        if inst is None:
            return

        client = try_get_docker()
        if client is None:
            await ws.accept()
            await ws.send_text("[agent] Docker 服务不可用")
            await ws.close(code=4503)
            return
        try:
            container = client.containers.get(container_name(inst.id))
        except Exception:
            await ws.accept()
            await ws.send_text("[agent] 容器不存在")
            await ws.close(code=4404)
            return

        await ws.accept()
        tail = int(ws.query_params.get("tail", "200") or 200)
        aq: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        done_flag = threading.Event()

        def worker():
            try:
                for chunk in container.logs(stream=True, follow=True,
                                            tail=max(1, min(tail, 5000)),
                                            stdout=True, stderr=True):
                    text = chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else str(chunk)
                    loop.call_soon_threadsafe(aq.put_nowait, text)
            except Exception:
                pass
            finally:
                done_flag.set()
                loop.call_soon_threadsafe(aq.put_nowait, None)

        threading.Thread(target=worker, daemon=True).start()

        while True:
            item = await aq.get()
            if item is None:
                break
            try:
                await ws.send_text(item)
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await ws.close(code=1011)
        except Exception:
            pass
    finally:
        db.close()


@router.websocket("/ws/instances/{instance_id}/exec")
async def agent_ws_exec(ws: WebSocket, instance_id: int):
    db: Session = SessionLocal()
    try:
        if not ws_node_ok(ws):
            await ws.close(code=4401)
            return
        inst = _inst_or_close(ws, db, instance_id)
        if inst is None:
            return
        await ws.accept()

        job_id = ws.query_params.get("job_id", "")
        job = EXEC_JOBS.get(job_id)
        if job is None or job.instance_id != inst.id:
            await ws.send_text("[agent] 安装任务不存在或已结束")
            await ws.close()
            return

        idx = 0
        while True:
            lines = list(job.lines)
            while idx < len(lines):
                try:
                    await ws.send_text(lines[idx])
                except Exception:
                    return
                idx += 1
            if job.done and idx >= len(list(job.lines)):
                await ws.send_text(f"[agent] 任务完成 exit={job.exit_code}")
                break
            await asyncio.sleep(0.3)
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await ws.close(code=1011)
        except Exception:
            pass
    finally:
        db.close()


@router.websocket("/ws/docker/pull")
async def agent_ws_pull(ws: WebSocket):
    db: Session = SessionLocal()
    try:
        if not ws_node_ok(ws):
            await ws.close(code=4401)
            return
        image = (ws.query_params.get("image") or "").strip()
        if not image or len(image) > 200:
            await ws.close(code=4400)
            return

        client = try_get_docker()
        if client is None:
            await ws.accept()
            await ws.send_text("[agent] Docker 服务不可用")
            await ws.close(code=4503)
            return

        await ws.accept()
        await ws.send_text(f"[agent] 开始拉取镜像：{image}")
        aq: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def pull_worker():
            try:
                stream = client.api.pull(image, stream=True, decode=True)
                for ev in stream:
                    status = ev.get("status", "")
                    pid = ev.get("id", "")
                    prog = ev.get("progress") or ""
                    line = f"{pid + ': ' if pid else ''}{status}" + (f" {prog}" if prog else "")
                    if line.strip():
                        loop.call_soon_threadsafe(aq.put_nowait, line)
                loop.call_soon_threadsafe(aq.put_nowait, f"[agent] 拉取完成：{image}")
            except docker.errors.ImageNotFound:
                loop.call_soon_threadsafe(aq.put_nowait, f"[错误] 仓库中不存在镜像 {image}")
            except docker.errors.APIError as e:
                loop.call_soon_threadsafe(aq.put_nowait, f"[错误] 拉取失败：{e}")
            except Exception as e:  # noqa: BLE001
                loop.call_soon_threadsafe(aq.put_nowait, f"[错误] {e}")
            finally:
                loop.call_soon_threadsafe(aq.put_nowait, None)

        threading.Thread(target=pull_worker, daemon=True).start()

        while True:
            item = await aq.get()
            if item is None:
                break
            try:
                await ws.send_text(item)
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await ws.close(code=1011)
        except Exception:
            pass
    finally:
        db.close()


@router.websocket("/ws/docker/term")
async def agent_ws_term(ws: WebSocket):
    db: Session = SessionLocal()
    try:
        if not ws_node_ok(ws):
            await ws.close(code=4401)
            return

        client = try_get_docker()
        hostname = "node"
        if client is not None:
            try:
                hostname = client.info().get("Name") or hostname
            except docker.errors.APIError:
                pass

        await ws.accept()
        cwd = ""
        await ws.send_json({"type": "hello", "user": "master", "host": hostname, "cwd": "~"})

        while True:
            msg = await ws.receive_json()
            cmd = str(msg.get("cmd", "")).strip()
            if not cmd:
                continue
            try:
                out, cwd = host_ops.run_host_command(cmd, cwd)
            except Exception as e:  # noqa: BLE001
                out, cwd = f"[终端] 执行出错：{e}", cwd
            prompt = f"master@{hostname}:{cwd or '~'}$"
            try:
                await ws.send_json({"type": "out", "out": out, "cwd": cwd, "prompt": prompt})
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await ws.close(code=1011)
        except Exception:
            pass
    finally:
        db.close()
