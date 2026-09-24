"""被控 Agent WebSocket（节点级鉴权 ?node_token=）：
实例日志跟踪 / 依赖安装回显 / 镜像拉取进度 / 宿主机终端。
"""
import asyncio
import threading
import time

import docker
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.agent_auth import ws_node_ok
from app.database import SessionLocal
from app.models import Instance
from app.services.instance_service import EXEC_JOBS, container_name, try_get_docker


def _fmt_size(n: float) -> str:
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f}{unit}" if unit != "KB" else f"{n / div:.0f}KB"
    return f"{n}B"


class _PullRender:
    """docker pull 事件流 → docker CLI 风格的多行进度快照。

    快照文本带 \x01 前缀，前端收到后清空重绘（进度条覆写，不滚屏）；
    已完成层固化在上方历史区，进行中层在下方刷新进度条。
    """

    def __init__(self):
        self.done: list[str] = []            # 已固化行（Pull complete / Already exists 等）
        self.active: dict[str, list] = {}    # 层 id -> [状态, current, total]
        self._last = 0.0                     # 节流时间戳

    def _snapshot(self) -> str:
        lines = list(self.done)
        for lid, (st, cur, total) in self.active.items():
            if total and cur:
                cur = min(cur, total)
                w = 26
                filled = int(cur * w / total)
                bar = "=" * filled + ">" + " " * (w - filled - 1) if filled < w else "=" * w
                lines.append(f"{lid}: {st}  [{bar}]  {_fmt_size(cur)}/{_fmt_size(total)}")
            else:
                lines.append(f"{lid}: {st}")
        return "\x01" + "\n".join(lines)

    def feed(self, ev: dict) -> str | None:
        """消费一个 docker pull 事件；返回要发送的快照（节流后）或 None。"""
        status = (ev.get("status") or "").strip()
        if "errorDetail" in ev:
            raise RuntimeError((ev.get("errorDetail") or {}).get("message") or status or "拉取失败")
        if not status:
            return None
        pid = (ev.get("id") or "").strip()
        now = time.monotonic()

        # 头部/汇总行（Pulling from / Digest / Status）→ 固化
        if not pid or status.startswith(("Pulling from", "Digest", "Status")):
            self.done.append(f"{pid}: {status}" if pid and not status.startswith(("Digest", "Status")) else status)
            return self._snapshot()

        if status in ("Pull complete", "Already exists"):
            self.active.pop(pid, None)
            self.done.append(f"{pid}: {status}")
            return self._snapshot()          # 状态变化强制重绘

        detail = ev.get("progressDetail") or {}
        cur, total = detail.get("current") or 0, detail.get("total") or 0
        slot = self.active.setdefault(pid, [status, 0, 0])
        slot[0] = status
        if total:
            slot[1], slot[2] = cur, total

        if now - self._last >= 0.3:          # 进行中事件 300ms 节流重绘
            self._last = now
            return self._snapshot()
        return None

    def final(self) -> str:
        """流结束后的最终快照（含 Status 汇总行）。"""
        return self._snapshot()

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
                loop.call_soon_threadsafe(
                    aq.put_nowait,
                    "[agent] 正在向 Docker 仓库查询镜像（镜像名不存在或网络慢时此步较久，请耐心等待）...")
                stream = client.api.pull(image, stream=True, decode=True)
                render = _PullRender()
                for ev in stream:
                    msg = render.feed(ev)
                    if msg:
                        loop.call_soon_threadsafe(aq.put_nowait, msg)
                loop.call_soon_threadsafe(aq.put_nowait, render.final())
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
    """宿主机交互式终端：本机 PTY shell 或 paramiko SSH 登录远程主机（xterm.js 前端）。

    协议：收 {"type":"input","data"} / {"type":"resize","cols","rows"} /
    {"type":"connect","host","port","user","password"}（切换到 SSH 会话）；
    发 {"type":"data","data"} / {"type":"closed"} / {"type":"error","msg"} /
    {"type":"mode","mode":"local"|"ssh","label"}。
    """
    import os
    import platform
    import socket
    import struct
    import subprocess

    db: Session = SessionLocal()
    local = None      # _LocalSession（本机 PTY）或 None
    ssh = None        # _SSHSession
    out_task = None
    loop = asyncio.get_running_loop()

    cur: list = []    # 当前会话（单元素列表，read_loop 动态读取）

    class _LocalSession:
        """本机交互 shell（pty + bash）。"""

        def __init__(self):
            import fcntl
            import pty
            import termios
            self._fcntl, self._termios = fcntl, termios
            home = os.path.expanduser("~")
            hostname = socket.gethostname()
            shell = os.environ.get("SHELL") or "/bin/bash"
            self.fd, slave = pty.openpty()
            self.proc = subprocess.Popen(
                [shell, "-i"],
                stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True, cwd=home,
                env={**os.environ, "TERM": "xterm-256color",
                     "PS1": f"\\u@{hostname}:\\w$ ", "HOME": home},
            )
            os.close(slave)

        def read(self, n=4096):
            return os.read(self.fd, n)

        def write(self, data: bytes):
            os.write(self.fd, data)

        def resize(self, cols: int, rows: int):
            self._fcntl.ioctl(self.fd, self._termios.TIOCSWINSZ,
                              struct.pack("HHHH", rows, cols, 0, 0))

        def close(self):
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            try:
                os.close(self.fd)
            except Exception:  # noqa: BLE001
                pass

        @property
        def label(self):
            return f"{os.environ.get('USER', 'root')}@{socket.gethostname()}"

    class _SSHSession:
        """paramiko SSH 交互 shell（登录任意可达主机）。"""

        def __init__(self, host: str, port: int, user: str, password: str, cols: int, rows: int):
            import paramiko
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.client.connect(
                host, port=port, username=user, password=password,
                timeout=12, allow_agent=False, look_for_keys=False,
            )
            self.chan = self.client.invoke_shell(
                term="xterm-256color", width=cols, height=rows)

        def read(self, n=4096):
            return self.chan.recv(n)

        def write(self, data: bytes):
            self.chan.send(data)

        def resize(self, cols: int, rows: int):
            self.chan.resize_pty(width=cols, height=rows)

        def close(self):
            try:
                self.chan.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.client.close()
            except Exception:  # noqa: BLE001
                pass

        @property
        def label(self):
            c = self.client.get_transport()
            return f"ssh://{c.getpeername()[0]}:{c.getpeername()[1]}" if c else "ssh"

    try:
        if not ws_node_ok(ws):
            await ws.close(code=4401)
            return
        await ws.accept()
        if platform.system() != "Windows":
            local = _LocalSession()
            cur.append(local)
            await ws.send_json({"type": "mode", "mode": "local", "label": local.label})
        else:
            await ws.send_json({"type": "mode", "mode": "none",
                                "label": "本机终端仅 Linux 可用，可使用 SSH 登录远程节点"})
    except Exception as e:  # noqa: BLE001
        try:
            await ws.send_json({"type": "error", "msg": f"无法创建终端会话：{e}"})
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
        return

    out_q: asyncio.Queue = asyncio.Queue(maxsize=2000)

    def _read_loop():
        while True:
            s = cur[0] if cur else None
            if s is None:
                import time as _t
                _t.sleep(0.05)
                continue
            try:
                data = s.read(4096)
            except Exception:  # noqa: BLE001
                if cur and cur[0] is s:
                    break  # 当前会话读取失败，终端结束
                continue    # 旧会话被切换关闭，改读新会话
            if not data:
                if cur and cur[0] is s:
                    break
                continue
            loop.call_soon_threadsafe(
                out_q.put_nowait, data.decode("utf-8", "replace"))
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
                s = cur[0] if cur else None
                if s is not None:
                    try:
                        s.resize(cols, rows)
                    except Exception:  # noqa: BLE001
                        pass
            elif mtype == "input":
                data = msg.get("data") or ""
                s = cur[0] if cur else None
                if data and s is not None:
                    try:
                        s.write(data.encode("utf-8"))
                    except Exception:  # noqa: BLE001
                        break
            elif mtype == "connect":
                host = str(msg.get("host") or "").strip()
                user = str(msg.get("user") or "root").strip() or "root"
                password = str(msg.get("password") or "")
                try:
                    port = int(msg.get("port") or 22)
                except (TypeError, ValueError):
                    port = 22
                if not host:
                    await ws.send_json({"type": "error", "msg": "SSH 主机不能为空"})
                    continue
                cols = max(20, min(int(msg.get("cols") or 120), 500))
                rows = max(5, min(int(msg.get("rows") or 30), 200))
                try:
                    new_sess = _SSHSession(host, port, user, password, cols, rows)
                except Exception as e:  # noqa: BLE001
                    await ws.send_json({"type": "error", "msg": f"SSH 连接失败：{e}"})
                    continue
                old = cur[0] if cur else None
                if cur:
                    cur[0] = new_sess
                else:
                    cur.append(new_sess)
                if old is not None:
                    old.close()  # 触发旧会话读取异常，read_loop 自动切到新会话
                ssh = new_sess
                await ws.send_json({"type": "mode", "mode": "ssh",
                                    "label": f"{user}@{host}:{port}"})
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        if out_task:
            out_task.cancel()
        if local is not None:
            local.close()
        if ssh is not None:
            ssh.close()
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
        db.close()
