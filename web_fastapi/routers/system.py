"""系统信息 API：工具/技能/压缩/健康自检。通过 IPC 发给 worker。

worker.send 会同步抢 per-worker 锁（最多等 lock_wait=5s），凡直接调用它的
handler 一律声明为同步 def——FastAPI 把同步路由丢进线程池执行；async def
里调它会把整个 uvicorn 事件循环卡住（主槽 chat 流式持锁期间，全站停摆，
memory.py:8-10 为同一规则，P2-11）。
"""
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from src.constants import LOCAL_USER
from src.version import __version__
from src.waker.store import WakerStore
from web_fastapi.dependencies import get_worker
from web_fastapi.routers.chat import _worker_for as chat_gate_worker
from web_fastapi.worker_manager import SlotsFullError, WorkerProcess

logger = logging.getLogger("hermes.web.system")
router = APIRouter()

# 项目根（web_fastapi/routers/system.py → parents[2]）：/api/health 的
# code_dir 字段与启动日志的 code= 同源，运维侧好对账。
CODE_DIR = Path(__file__).resolve().parents[2]


@router.get("/tools")
def get_tools(worker: WorkerProcess = Depends(get_worker)):
    events = worker.send("tools_list")
    return events[0]["data"] if events else {"tools": [], "count": 0}


@router.get("/skills")
def get_skills(worker: WorkerProcess = Depends(get_worker)):
    events = worker.send("skills_list")
    return events[0]["data"] if events else {"skills": [], "count": 0}


@router.post("/compact")
def compact(request: Request, session_id: str = ""):
    """压缩对话历史。

    P2-14：带 session_id 时按 chat 闸门亲和路由到该会话的 worker 槽并让
    worker 侧压缩对应桶（此前恒打 main 槽——用户对当前会话点"压缩历史"
    压缩的是 main 的空/旧桶）；无 session_id 维持 main 槽旧行为。
    """
    sid = (session_id or "").strip()
    try:
        if sid:
            worker = chat_gate_worker(request, sid)
        else:
            worker = request.app.state.worker_manager.get_or_create(LOCAL_USER)
    except SlotsFullError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    try:
        events = worker.send("compact", session_id=sid)
    except TimeoutError as e:
        # P3：对齐 busy 语义（sessions.py 同款）——目标会话正在 chat 持锁时
        # 旧实现直接 500；压缩可稍后再试
        from web_fastapi.routers.sessions import _busy_if_timeout
        _busy_if_timeout("compact", e)
    return events[0]["data"] if events else {"ok": False}


@router.get("/health")
def health(worker: WorkerProcess = Depends(get_worker)):
    """健康自检：worker IPC 结果（ok 语义 = worker 侧检查结论，保持不变）
    之上在 router 层叠加版本可见性三元组——name/version/code_dir 全在
    主进程本地可得，不过 IPC（worker 不必知道部署路径）。"""
    events = worker.send("health")
    out = dict(events[0]["data"]) if events else {"ok": False}
    out.setdefault("name", "hermes-ma")
    out["version"] = __version__
    out["code_dir"] = str(CODE_DIR)
    return out


# ============================================
# 运行统计（GET /api/stats）：主进程本地聚合，不走 worker IPC
# ============================================

def _stats_storage(request: Request):
    """取只读存储：姿势与 runs.py:_tasks_registry 同款——cordis_ctx.try_get
    ("storage") 失败/缺失回退自建 SQLiteProvider()（默认 data/hermes.db）。
    返回 (provider, created)：created=True 表示自建，调用方负责 close。"""
    ctx = getattr(request.app.state, "cordis_ctx", None)
    provider = None
    if ctx is not None:
        try:
            provider = ctx.try_get("storage")
        except Exception:
            provider = None
    if provider is None:
        from src.storage.sqlite_provider import SQLiteProvider
        provider = SQLiteProvider()
        return provider, True
    return provider, False


def _count_storage(provider) -> dict:
    """events/memories 行数 + 会话/LLM 错误聚合（SQL 口径见各注释）。

    单项查询失败（全新环境无表等）按 0 计，不拖垮整个端点——
    collect_ops_snapshot（housekeeping.py）同款降级纪律。
    """
    sessions = {"total": 0, "active_24h": 0}
    llm_errors = {"total": 0, "last_24h": 0}
    storage = {"events_rows": 0, "memories_rows": 0}
    since = time.time() - 86400

    def _one(sink, key, sql, params=()):
        try:
            sink[key] = int(provider.query(sql, params)[0][0])
        except Exception:
            logger.warning(f"[stats] 查询失败按 0 计: {key}", exc_info=True)

    # 有对话活动的会话总数（scope=chat 且 session_id 非空）
    _one(sessions, "total",
         "SELECT COUNT(DISTINCT session_id) FROM events "
         "WHERE scope='chat' AND session_id<>''")
    # 近 24h 有 turn/start 的会话数（活跃口径）
    _one(sessions, "active_24h",
         "SELECT COUNT(DISTINCT session_id) FROM events "
         "WHERE scope='chat' AND session_id<>'' AND type='turn/start' AND ts > ?",
         (since,))
    # LLM 错误事件计数
    _one(llm_errors, "total", "SELECT COUNT(*) FROM events WHERE type='llm/error'")
    _one(llm_errors, "last_24h",
         "SELECT COUNT(*) FROM events WHERE type='llm/error' AND ts > ?", (since,))
    # 表行数（与 collect_ops_snapshot 同款）
    _one(storage, "events_rows", "SELECT COUNT(*) FROM events")
    _one(storage, "memories_rows", "SELECT COUNT(*) FROM memories")
    return {"sessions": sessions, "llm_errors": llm_errors, "storage": storage}


def _count_wakers(request: Request) -> dict:
    """waker 概览：enabled 数 + last_status 分布。姿势照 routers/waker.py
    的 _store（scheduler 挂 workspace_root 优先，其次 app.state，末位回退）。"""
    out = {"total": 0, "enabled": 0, "status": {}}
    scheduler = getattr(request.app.state, "waker_scheduler", None)
    ws = getattr(scheduler, "_workspace_root", "") if scheduler else ""
    if not ws:
        ws = getattr(request.app.state, "workspace_root", "") or ""
    store = WakerStore(LOCAL_USER, workspace_root=ws)
    for cfg in store.list():
        out["total"] += 1
        if cfg.enabled:
            out["enabled"] += 1
        status = cfg.last_status or "none"
        out["status"][status] = out["status"].get(status, 0) + 1
    return out


@router.get("/stats")
def stats(request: Request):
    """运行统计（只读）。存储聚合 + waker 概览全部主进程本地完成，不过
    worker IPC。任何一步失败：log warning + 该块零值兜底，绝不 500。"""
    out = {"sessions": {"total": 0, "active_24h": 0},
           "llm_errors": {"total": 0, "last_24h": 0},
           "storage": {"events_rows": 0, "memories_rows": 0},
           "wakers": {"total": 0, "enabled": 0, "status": {}}}
    provider, created = None, False
    try:
        provider, created = _stats_storage(request)
        agg = _count_storage(provider)
        out["sessions"] = agg["sessions"]
        out["llm_errors"] = agg["llm_errors"]
        out["storage"] = agg["storage"]
    except Exception:
        logger.warning("[stats] 存储聚合失败（零值兜底）", exc_info=True)
    finally:
        if created and provider is not None:
            try:
                provider.close()
            except Exception:
                pass
    try:
        out["wakers"] = _count_wakers(request)
    except Exception:
        logger.warning("[stats] waker 概览失败（零值兜底）", exc_info=True)
    return out
