"""任务账本查询（GET /api/runs）+ 关掉死信（DELETE /api/runs/{id}）。

RunRegistry("tasks") 目前承载 git clone 等主进程短命任务；waker/flow/
memory 聚合各有自己的 scope（runs/waker_async/flow/memory_consolidation），
不在此聚合，避免把高频自动任务灌进 UI 列表。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from web_fastapi.security import validate_id

from web_fastapi.dependencies import get_current_user_id
from fastapi import Depends

logger = logging.getLogger("hermes.web.runs")
router = APIRouter()


def _tasks_registry(request: Request):
    ctx = getattr(request.app.state, "cordis_ctx", None)
    storage = None
    if ctx is not None:
        try:
            storage = ctx.try_get("storage")
        except Exception:
            storage = None
    if storage is None:
        from src.storage.sqlite_provider import SQLiteProvider
        storage = SQLiteProvider()
    from src.storage.run_registry import RunRegistry
    return RunRegistry("tasks", storage)


@router.get("")
async def list_runs(request: Request,
                    user_id: str = Depends(get_current_user_id)):
    reg = _tasks_registry(request)
    items = list(reg.list())
    # 最新在前：finished_at/started_at 皆缺省的 queued 排最后
    def _key(r):
        return r.get("finished_at") or r.get("started_at") or 0
    items.sort(key=_key, reverse=True)
    return {"runs": items}


@router.delete("/{run_id}")
async def delete_run(request: Request, run_id: str,
                     user_id: str = Depends(get_current_user_id)):
    """关掉一条终态任务记录（失败/完成/中断）。进行中的拒绝删除。"""
    run_id = validate_id(run_id, "任务 ID")
    reg = _tasks_registry(request)
    rec = reg.get(run_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="任务记录不存在")
    if rec.get("status") in ("running", "pending", "queued"):
        raise HTTPException(status_code=409, detail="任务仍在进行，不能关掉")
    if not reg.delete(run_id):
        raise HTTPException(status_code=409, detail="无法删除该任务记录")
    return {"ok": True, "run_id": run_id}
