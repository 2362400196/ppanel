"""任务日志查询（主控侧任务）：GET /api/tasks/lines?task_id=&since=。"""
from fastapi import APIRouter

from app import tasks

router = APIRouter(tags=["tasks"])


@router.get("/tasks/lines")
def task_lines(task_id: str = "", since: int = 0):
    return tasks.lines(task_id, since)
