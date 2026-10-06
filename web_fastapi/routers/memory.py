"""记忆管理 API。

列表/清空/手动新增/单条编辑与聚合都走主进程（不抢 worker 锁）。
列表可见性跟对话检索一致：顶栏当前项目 ∪ 全局。
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from config import get_settings
from web_fastapi.dependencies import get_current_user_id
from src.memory import config_store

router = APIRouter()


def _workspace_root() -> str:
    """取 settings.workspace_root，空串回退（store 内再解析）。"""
    return getattr(get_settings(), "workspace_root", "") or ""


def _scheduler(request):
    """取主进程聚合调度器。"""
    sched = getattr(request.app.state, "memory_consolidation_scheduler", None)
    if sched is None:
        raise HTTPException(status_code=503, detail="聚合调度器未启用")
    return sched


def _memory_manager(request: Request):
    """取 MemoryManager（T9 收敛：消除主进程/worker 双实例）。

    优先 app.state.cordis_ctx.try_get("memory")——与 worker 子进程同源的
    组合根服务（同一 data/hermes.db 连接）；ctx 未 boot 的场景（如部分
    测试）回退进程内直接构造（降级安全，行为与旧实现一致）。
    """
    ctx = getattr(request.app.state, "cordis_ctx", None)
    if ctx is not None:
        try:
            mgr = ctx.try_get("memory")
        except Exception:
            mgr = None
        if mgr is not None:
            return mgr
    from src.memory.manager import MemoryManager
    return MemoryManager()


# ════════════════════════════════════════════════════════════════
# 列表 / 清空 / 手动新增：主进程直连存储（不抢 worker 锁）
# ════════════════════════════════════════════════════════════════

@router.get("")
def get_memory(request: Request, user_id: str = Depends(get_current_user_id)):
    mgr = _memory_manager(request)
    memories = mgr.get_all(user_id)
    return {"memories": memories, "count": len(memories)}


@router.delete("")
def clear_memory(request: Request, user_id: str = Depends(get_current_user_id)):
    mgr = _memory_manager(request)
    return {"ok": bool(mgr.delete_all(user_id))}


class MemoryCreateBody(BaseModel):
    content: str


@router.post("")
def create_memory(body: MemoryCreateBody, request: Request,
                  user_id: str = Depends(get_current_user_id)):
    """记忆页手动新增一条：打当前顶栏项目，不走 LLM decide。"""
    mgr = _memory_manager(request)
    mid = mgr.add_memory(user_id, body.content)
    if not mid:
        raise HTTPException(status_code=400, detail="内容为空或写入失败")
    return {"ok": True, "id": mid}


# ════════════════════════════════════════════════════════════════
# 单条操作：主进程直连存储（理由同聚合旁路——不抢 worker 锁，
# chat 流式期间也能即时编辑/删除；SQLite WAL 支持跨进程并发写）
# ════════════════════════════════════════════════════════════════

class MemoryEditBody(BaseModel):
    content: str


@router.put("/{memory_id}")
def edit_memory(memory_id: str,
                body: MemoryEditBody,
                request: Request,
                user_id: str = Depends(get_current_user_id)):
    """人工修正单条记忆文本（保留 source/created_at，刷新 updated_at）。"""
    mgr = _memory_manager(request)
    if not mgr.edit_memory(user_id, memory_id, body.content):
        raise HTTPException(status_code=404, detail="记忆不存在或内容为空")
    return {"ok": True}


@router.delete("/{memory_id}")
def delete_single_memory(memory_id: str,
                         request: Request,
                         user_id: str = Depends(get_current_user_id)):
    """删除单条记忆。"""
    mgr = _memory_manager(request)
    if not mgr.delete_memory(user_id, memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"ok": True}


# ════════════════════════════════════════════════════════════════
# 聚合：手动触发 + 轮询（主进程直跑）
# ════════════════════════════════════════════════════════════════

@router.post("/consolidate")
async def consolidate(request: Request, user_id: str = Depends(get_current_user_id)):
    """手动触发聚合，立即返回 run_id（submit-and-poll）。"""
    sched = _scheduler(request)
    run_id = sched.submit_now(user_id)
    return {"run_id": run_id}


@router.get("/consolidate/status")
async def consolidate_status(
    request: Request,
    run_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """轮询聚合状态。"""
    sched = _scheduler(request)
    st = sched.get_status(run_id)
    if st.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="聚合任务不存在")
    return st


# ════════════════════════════════════════════════════════════════
# 聚合配置：读写 memory_config.yaml
# ════════════════════════════════════════════════════════════════

@router.get("/consolidate/config")
async def get_consolidate_config(user_id: str = Depends(get_current_user_id)):
    ws = _workspace_root()
    return config_store.load(ws, user_id)


class ConsolidateConfigBody(BaseModel):
    auto_consolidate: bool | None = None
    interval_hours: int | None = None
    threshold: int | None = None


@router.put("/consolidate/config")
async def put_consolidate_config(
    body: ConsolidateConfigBody,
    user_id: str = Depends(get_current_user_id),
):
    ws = _workspace_root()
    # 合并：先读现有（保留 last_run_at），再用 body 覆盖传入字段
    cur = config_store.load(ws, user_id)
    if body.auto_consolidate is not None:
        cur["auto_consolidate"] = body.auto_consolidate
    if body.interval_hours is not None:
        cur["interval_hours"] = max(1, int(body.interval_hours))
    if body.threshold is not None:
        cur["threshold"] = max(1, int(body.threshold))
    config_store.save(ws, user_id, cur)
    return {"ok": True, "config": config_store.load(ws, user_id)}


# ════════════════════════════════════════════════════════════════
# 备份：枚举 + 恢复
# ════════════════════════════════════════════════════════════════

@router.get("/backups")
async def list_backups(request: Request,
                       user_id: str = Depends(get_current_user_id)):
    """列出该用户的聚合备份（时间倒序）。"""
    mgr = _memory_manager(request)
    return {"backups": mgr.list_backups(user_id)}


class RestoreBody(BaseModel):
    name: str


@router.post("/backups/restore")
async def restore_backup(
    body: RestoreBody,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    """把指定备份恢复为 profile.md。"""
    mgr = _memory_manager(request)
    ok = mgr.restore_backup(user_id, body.name)
    if not ok:
        raise HTTPException(status_code=404, detail="备份不存在或恢复失败")
    return {"ok": True}
