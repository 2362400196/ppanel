"""宿主机任务日志查询：配合 X-Task-Id 头使用（见 app/tasks.py）。"""
from fastapi import APIRouter, Depends

from app.agent_auth import require_node_or_api
from app import tasks

router = APIRouter(dependencies=[Depends(require_node_or_api)])


@router.get("/host/tasks/lines")
def task_lines(task_id: str = "", since: int = 0):
    return tasks.lines(task_id, since)
