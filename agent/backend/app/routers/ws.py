"""WebSocket：容器日志跟踪 + 依赖安装输出回显 + 镜像拉取进度 + 宿主机终端。

鉴权通过 ?token=<JWT>（浏览器 WS 无法自定义 header）。
"""
import asyncio
import subprocess
import threading

import docker
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.auth import ws_user
from app.database import SessionLocal
from app.models import Instance
from app.services import host_ops
from app.services.instance_service import (EXEC_JOBS, container_name,
                                           try_get_docker)

router = APIRouter(tags=["ws"])


@router.websocket("/ws/instances/{instance_id}/logs")
async def ws_logs(ws: WebSocket, instance_id: int):
    db: Session = SessionLocal()
    try:
        user = ws_user(ws, db)
        if user is None:
            await ws.close(code=4401)
            return
        inst = db.get(Instance, instance_id)
        if not inst or (user.role != "admin" and inst.user_id != user.id):
            await ws.close(code=4404)
            return

        client = try_get_docker()
        if client is None:
            await ws.accept()
            await ws.send_text("[面板] Docker 服务不可用")
            await ws.close(code=4503)
            return
        try:
            container = client.containers.get(container_name(inst.id))
        except Exception:
            await ws.accept()
            await ws.send_text("[面板] 容器不存在")
            await ws.close(code=4404)
            return

        await ws.accept()
        tail = int(ws.query_params.get("tail", "200") or 200)

        aq: asyncio.Queue = asyncio.Queue()
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
async def ws_exec(ws: WebSocket, instance_id: int):
    db: Session = SessionLocal()
    try:
        user = ws_user(ws, db)
        if user is None:
            await ws.close(code=4401)
            return
        inst = db.get(Instance, instance_id)
        if not inst or (user.role != "admin" and inst.user_id != user.id):
            await ws.close(code=4404)
            return
        await ws.accept()

        job_id = ws.query_params.get("job_id", "")
        job = EXEC_JOBS.get(job_id)
        if job is None or job.instance_id != inst.id:
            await ws.send_text("[面板] 安装任务不存在或已结束")
            await ws.close()
            return

        # 轮询推送：先补发历史输出，再持续跟随直到任务结束
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
                await ws.send_text(f"[面板] 任务完成 exit={job.exit_code}")
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
async def ws_docker_pull(ws: WebSocket):
    """镜像拉取进度流：?token=&image=repo:tag，逐条发送状态文本。"""
    db: Session = SessionLocal()
    try:
        user = ws_user(ws, db)
        if user is None:
            await ws.close(code=4401)
            return
        if user.role != "admin":
            await ws.close(code=4403)
            return
        image = (ws.query_params.get("image") or "").strip()
        if not image or len(image) > 200:
            await ws.close(code=4400)
            return

        client = try_get_docker()
        if client is None:
            await ws.accept()
            await ws.send_text("[面板] Docker 服务不可用")
            await ws.close(code=4503)
            return

        await ws.accept()
        await ws.send_text(f"[面板] 开始拉取镜像：{image}")

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
                loop.call_soon_threadsafe(aq.put_nowait, f"[面板] 拉取完成：{image}")
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


@router.websocket("/ws/host/term")
async def ws_host_term(ws: WebSocket):
    """宿主机终端（行模式，仅 admin）：收 {cmd}，回 {out, cwd}。"""
    db: Session = SessionLocal()
    try:
        user = ws_user(ws, db)
        if user is None:
            await ws.close(code=4401)
            return
        if user.role != "admin":
            await ws.close(code=4403)
            return

        client = try_get_docker()
        hostname = "docker-host"
        if client is not None:
            try:
                hostname = client.info().get("Name") or hostname
            except docker.errors.APIError:
                pass

        await ws.accept()
        cwd = ""
        await ws.send_json({"type": "hello", "user": user.username, "host": hostname, "cwd": "~"})

        while True:
            msg = await ws.receive_json()
            cmd = str(msg.get("cmd", "")).strip()
            if not cmd:
                continue
            try:
                out, cwd = host_ops.run_host_command(cmd, cwd)
            except Exception as e:  # noqa: BLE001
                out, cwd = f"[终端] 执行出错：{e}", cwd
            prompt = f"{user.username}@{hostname}:{cwd or '~'}$"
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
