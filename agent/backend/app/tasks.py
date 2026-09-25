"""任务日志器：耗时操作（备份/恢复/镜像拉取/MySQL 启停/清理/SSH 安装等）的步骤日志。

约定：前端发起耗时请求时带 X-Task-Id 头（uuid），handler 内用 task_log() 打步骤；
另开 GET /host/tasks/lines 增量拉取（since 游标），终端浮层 2s 轮询实时呈现。
内存保存最近 TASK_KEEP 个任务，每任务最多 TASK_MAX_LINES 行（超出丢最旧）。
"""
import threading
import time

_lock = threading.Lock()
_tasks: dict[str, dict] = {}
_order: list[str] = []
TASK_KEEP = 30
TASK_MAX_LINES = 400


def start(task_id: str, title: str) -> None:
    with _lock:
        _tasks.pop(task_id, None)
        while _order and len(_order) >= TASK_KEEP:
            _tasks.pop(_order.pop(0), None)
        _tasks[task_id] = {"id": task_id, "title": title, "status": "running",
                           "lines": [], "ts": time.time()}
        _order.append(task_id)


def log(task_id: str, line: str) -> None:
    if not task_id or not line:
        return
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return  # 未注册的任务（如直接 curl）静默忽略
        t["lines"].append(f"[{time.strftime('%H:%M:%S')}] {line}")
        if len(t["lines"]) > TASK_MAX_LINES:
            del t["lines"][:len(t["lines"]) - TASK_MAX_LINES]


def finish(task_id: str, ok: bool, msg: str = "") -> None:
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        t["status"] = "done" if ok else "error"
        if msg:
            t["lines"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")


def lines(task_id: str, since: int = 0) -> dict:
    """增量拉取：返回 since 之后的行与游标。"""
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return {"lines": [], "total": 0, "status": ""}
        ls = t["lines"]
        return {"lines": ls[since:], "total": len(ls), "status": t["status"]}
