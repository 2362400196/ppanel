"""任务日志查询（主控侧任务）：GET /api/tasks/lines?task_id=&since=。

必须登录：任务日志含实例名、端口等编排细节，原先匿名可读等于信息泄露。
任务创建时记录了发起人，非发起人（且非管理员）只能拿到空结果。
"""
from fastapi import APIRouter, Depends

from app import tasks
from app.auth import Principal, get_principal

router = APIRouter(tags=["tasks"])


@router.get("/tasks/lines")
def task_lines(task_id: str = "", since: int = 0,
               p: Principal = Depends(get_principal)):
    return tasks.lines(task_id, since, owner=p.user_id, is_admin=p.is_admin)
