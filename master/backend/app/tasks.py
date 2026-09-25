"""任务日志器（主控侧）：耗时操作的步骤日志，供前端终端浮层增量轮询。

约定：前端发起耗时请求时带 X-Task-Id 头；主控自有 handler（实例开通/删除等）
与经 host_proxy 转发的节点操作都会写入这里。内存保存最近 TASK_KEEP 个任务。
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
            return
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
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return {"lines": [], "total": 0, "status": ""}
        ls = t["lines"]
        return {"lines": ls[since:], "total": len(ls), "status": t["status"]}
