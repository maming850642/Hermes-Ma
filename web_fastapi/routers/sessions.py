"""会话管理 API。通过 IPC 发给 worker 子进程处理。

例外（T9 事件流 API，主进程直接读 ctx.sessions，不经 worker）：
  - GET  /{session_id}/events：会话事件流（冷归档合并视图，见
         SessionLog.load_events_with_archive）
  - POST /{session_id}/fork：事件流复制成新会话（Trajectory 数据基础）

经 worker IPC 的 handler 一律声明为同步 def（P2-11，memory.py:8-10 同一
规则）：worker.send 同步抢 per-worker 锁、_worker_for 兜底可能触发
get_or_create（spawn 最长 60s，P2-13）——async def 里调它们会把整个
uvicorn 事件循环卡住（主槽 chat 流式持锁期间全站停摆）；同步 def 由
FastAPI 丢进线程池执行，事件循环不再被阻塞。
"""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.constants import LOCAL_USER
from web_fastapi.dependencies import get_worker, get_current_user_id
from web_fastapi.routers.chat import _worker_for as chat_gate_worker
from web_fastapi.worker_manager import DEFAULT_SLOT, SlotsFullError, WorkerProcess
from web_fastapi.models import RenameRequest
from web_fastapi.security import validate_id

logger = logging.getLogger("hermes.web.sessions")
router = APIRouter()


def _worker_for(request: Request, session_id: str) -> WorkerProcess:
    """管理类端点按会话取 worker：已有存活槽 → 亲和复用；没有 → main。

    绝不在管理路径创建会话槽（会话槽的唯一创建者是 chat 闸门
    acquire_chat_slot）——对话页每次加载都会 /current 恢复会话，若在
    这里 spawn，浏览几个历史会话就把并发名额（默认 3）占满，新会话
    直接 429。轻量盘读/内存操作在 main 槽完全够用。

    注意：本函数兜底的 wm.get_or_create(LOCAL_USER)（main 槽）在首次
    调用时会触发 spawn——调用方必须是同步 def 路由（线程池内执行），
    不得在 async handler 里直接调（P2-13：新会话首条消息冻结全站）。
    """
    wm = request.app.state.worker_manager
    sid = (session_id or "").strip()
    if sid:
        wp = wm.lookup(LOCAL_USER, sid)
        if wp is not None:
            return wp
    return wm.get_or_create(LOCAL_USER)


def _busy_if_timeout(op: str, exc: Exception):
    """worker 锁占用超时（chat 进行中）→ 返回 503 让前端友好提示，而非 500。"""
    if isinstance(exc, TimeoutError):
        raise HTTPException(
            status_code=503,
            detail=f"AI 正在思考，请稍候再试（{op} 等待 worker 锁超时）",
        )



@router.get("")
async def list_user_sessions(project: str | None = None):
    """M5：主进程直读磁盘快照（不经 worker 锁）——AI 回复期间侧栏/会话页
    依旧可用，消灭高频 503。"""
    if project is not None:
        validate_id(project, "项目标识")
    from src.session_store import list_sessions
    sessions = list_sessions(LOCAL_USER, project=project)
    return {"sessions": sessions}


@router.get("/current")
def current_session(request: Request, session_id: str = ""):
    """M5：标签页可带 session_id 恢复自己的会话（含懒加载水合）。"""
    worker = _worker_for(request, session_id)
    kwargs = {"session_id": session_id} if session_id else {}
    try:
        events = worker.send("current_session", **kwargs)
    except TimeoutError as e:
        # 对齐同文件其他端点的 busy 语义（chat 进行中持 worker 锁）——旧实现
        # 直接 500；503 让前端按瞬时失败走恢复轮询并给用户友好提示
        _busy_if_timeout("current_session", e)
        raise   # pragma: no cover —— _busy_if_timeout 对 TimeoutError 恒抛，防御性收口
    return events[0]["data"] if events else {}


@router.post("/save")
def save_current_session(request: Request, session_id: str = ""):
    """保存会话。

    P2-14 同款修法（对齐 POST /compact）：带 session_id 时经 chat 闸门
    亲和路由到该会话的 worker 槽并把 sid 传给 worker（worker 侧
    session_save 按桶定位，保存的是那个会话而非槽的当前桶）。此前恒走
    Depends(get_worker) 的 main 槽——M5 下标签页会话在专属槽，/save 存
    的是 main 槽当前会话且返回误导性成功。无 session_id 维持 main 槽
    旧行为。
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
        events = worker.send("session_save", session_id=sid)
    except TimeoutError as e:
        # P3：对齐 busy 语义（chat 进行中持 worker 锁）——旧实现直接 500
        _busy_if_timeout("session_save", e)
    return events[0]["data"] if events else {"ok": False}


@router.post("/reset")
def reset_session(request: Request,
                  body: "ResetBody | None" = None):
    """新建会话。M5：新 sid 由主进程统一生成下发，保证"生成者"与
    "后续 chat 路由者"是同一实例（修跨槽错位）。"""
    old_sid = (body.session_id if body else "") or ""
    worker = _worker_for(request, old_sid)
    log = _sessions_log(request)
    new_sid = _generate_new_sid(log)
    try:
        events = worker.send("session_reset", new_sid=new_sid)
    except TimeoutError as e:
        _busy_if_timeout("session_reset", e)
        raise   # pragma: no cover —— _busy_if_timeout 对 TimeoutError 恒抛，防御性收口
    # P3 槽回收：reset 后前端持新 sid 聊天，旧会话的专属槽只剩内存态
    # （worker 侧 reset 已把旧桶落盘），不回收则僵尸槽永久占一个并发名额
    # （SlotsFullError 计数含它）。remove 会 shutdown worker——旧槽若仍有
    # 活跃流被一并终止，属可接受的 reset 语义（用户已显式放弃旧会话）；
    # main 槽是共享杂项槽，永不回收。
    if old_sid and old_sid != DEFAULT_SLOT:
        request.app.state.worker_manager.remove(LOCAL_USER, slot=old_sid)
    data = events[0]["data"] if events else {"ok": False}
    data["session_id"] = new_sid
    return data


@router.post("/{session_id}/release-worker")
def release_session_worker(request: Request, session_id: str):
    """只回收专属槽，不删会话文件（草稿化/切走时腾出名额）。

    会话快照仍在磁盘，下次点开会冷启动水合。main 槽永不回收。
    """
    session_id = validate_id(session_id, "会话 ID")
    if session_id != DEFAULT_SLOT:
        request.app.state.worker_manager.remove(LOCAL_USER, slot=session_id)
    return {"ok": True, "session_id": session_id}


@router.patch("/{session_id}/rename")
def rename_session(request: Request, session_id: str, body: RenameRequest):
    session_id = validate_id(session_id, "会话 ID")
    worker = _worker_for(request, session_id)
    try:
        events = worker.send("session_rename", session_id=session_id, name=body.name)
    except TimeoutError as e:
        # P3：对齐 busy 语义（chat 进行中持 worker 锁）——旧实现直接 500
        _busy_if_timeout("session_rename", e)
    return events[0]["data"] if events else {"ok": False}


@router.delete("/{session_id}")
def delete_session(request: Request, session_id: str):
    session_id = validate_id(session_id, "会话 ID")
    worker = _worker_for(request, session_id)
    try:
        events = worker.send("session_delete", session_id=session_id)
    except TimeoutError as e:
        # P3：对齐 busy 语义——chat 进行中持 worker 锁时旧实现直接 500；
        # 且不得走到下面的槽回收（忙 = 该槽有活跃流，删槽会误杀）
        _busy_if_timeout("session_delete", e)
    except RuntimeError as e:
        # worker.send 对 error 事件直接抛 RuntimeError（会话文件不存在等），
        # 归一成 error 事件继续走下面的 404 + 槽回收路径。
        events = [{"type": "error", "message": str(e)}]
    # 会话没了（删除成功或本就不存在）→ 专属槽（若 chat 创建过）一并回收，
    # 否则僵尸槽永久占一个并发名额（SlotsFullError 计数含它），内存也白养。
    # main 槽是共享杂项槽，永不回收。
    if session_id != DEFAULT_SLOT:
        request.app.state.worker_manager.remove(LOCAL_USER, slot=session_id)
    # 会话删除：事件总线的该通道一并清（订阅者收哨兵、TTL 定时器撤销）。
    # 本路由是同步 def（线程池执行），经 call_soon_threadsafe 转回事件循环
    # （drop_session 须在循环线程调用）；循环已关（关服竞态）则跳过。
    bus = getattr(request.app.state, "chat_bus", None)
    if bus is not None:
        # bound_loop 是方法（chat_bus.py）——漏括号会把方法当循环对象，
        # .is_closed() AttributeError → 删会话恒 500（2026-09-10 修复）
        loop = bus.bound_loop()
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(bus.drop_session, session_id)
    if events and events[0].get("type") == "error":
        raise HTTPException(status_code=404, detail=events[0].get("message", "删除失败"))
    return events[0]["data"] if events else {"ok": False}


@router.post("/{session_id}/load")
def load_user_session(request: Request, session_id: str):
    session_id = validate_id(session_id, "会话 ID")
    worker = _worker_for(request, session_id)
    # 生成中切会话：不要 503 把人卡死。worker 忙则跳过 IPC 水合，
    # 前端 reload 后走事件库快路径 / 开轮冷启动。
    if getattr(worker, "streaming", False):
        return {"ok": True, "session_id": session_id, "deferred": True}
    try:
        events = worker.send("session_load", session_id=session_id)
    except TimeoutError:
        return {"ok": True, "session_id": session_id, "deferred": True}
    if events and events[0].get("type") == "error":
        raise HTTPException(status_code=404, detail=events[0].get("message", "会话不存在"))
    return events[0]["data"] if events else {"ok": False}


@router.post("/{session_id}/summary")
def summarize_session(session_id: str,
                      worker: WorkerProcess = Depends(get_worker)):
    session_id = validate_id(session_id, "会话 ID")
    events = worker.send("session_summary", session_id=session_id)
    return events[0]["data"] if events else {}


# ════════════════════════════════════════════════════════════════
# T9 事件流 API：主进程直接读 ctx.sessions（不经 worker IPC）
# ════════════════════════════════════════════════════════════════

def _sessions_log(request: Request):
    """取 SessionLog。

    优先 app.state.cordis_ctx.try_get("sessions")（lifespan 里 boot_context()
    的组合根，与 worker 子进程同一 SQLite 库）；ctx 未 boot 的场景（如部分
    测试）回退进程内构造 SessionLog()（默认库 data/hermes.db，降级安全）。
    """
    ctx = getattr(request.app.state, "cordis_ctx", None)
    if ctx is not None:
        try:
            log = ctx.try_get("sessions")
        except Exception:
            log = None
        if log is not None:
            return log
    from src.agent.session_log import SessionLog
    return SessionLog()


@router.get("/{session_id}/events")
async def get_session_events(
    request: Request,
    session_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """会话事件流（Trajectory 数据基础）。

    冷归档合并视图（P3）：SessionLog.load_events_with_archive(session_id)，
    冷区（最后 COMPACT_APPLIED 之前的已归档行）+ 热表按 id 合并去重、
    升序返回——与归档前的 events() 逐条等价；未归档时即 events() 原样。
    无事件返回空列表。仅 chat 会话：sid 含 ":"（waker:/wakerflow: 无人
    值守任务流）被 validate_id 拒绝（400）。
    """
    session_id = validate_id(session_id, "会话 ID")
    log = _sessions_log(request)
    return {"session_id": session_id,
            "events": log.load_events_with_archive(session_id)}


class ResetBody(BaseModel):
    """新建会话的可选定位：标签页当前所在会话（用于槽路由）。"""
    session_id: str = ""


class ForkBody(BaseModel):
    """fork 请求体。

    截断点三选一（都不给 = 全量复制）：
    - up_to_user_ordinal: 消息级寻址（2026-09-19「从此 fork」入口）——保留
      到第 N 条（0-based）user 消息所在轮结束，即丢弃第 N+1 条 user/message
      事件及其后全部（tool/assistant/turn/* 都挂在其所属轮的 user 之后）。
      序数越界 → 404；其为最后一条 user 消息时等价全量复制。
    - up_to_event_id: **含端点**（复制 id <= up_to_event_id 的事件，推荐）
    - after_event_id: **排他**（复制 id < after_event_id 的事件；旧名兼容，
      R3-19 起语义由含端点改为排他——调用方传"此事件之后"的语义更直观，
      旧行为等价于 up_to_event_id=after_event_id）
    """
    after_event_id: int | None = None
    up_to_event_id: int | None = None
    up_to_user_ordinal: int | None = None
    name: str | None = None


class TruncateBody(BaseModel):
    """编辑重发前半程的截断请求：截到「第 user_ordinal 条 user 消息」之前
    （0-based；该消息及其后全部丢弃，新文本随后走普通 chat 重发）。

    expect_text 可选乐观校验：该消息的文本与前端持有值不一致（多标签页
    并发修改/陈旧视图）时拒绝，防误删。多模态消息取 text parts 拼接比对。
    """
    user_ordinal: int
    expect_text: str = ""


def _generate_new_sid(log) -> str:
    """生成未占用的新会话 ID（events 非空视为占用，最多重试 5 次）。

    8 位 uuid 短串理论上可能撞上已有会话（fork-of-fork 多代后概率上升，
    R3-19）——撞上就重生成；5 次仍撞（病态场景）抛 409。
    """
    for _ in range(5):
        candidate = str(uuid.uuid4())[:8]
        try:
            occupied = bool(log.events(candidate))
        except Exception:
            occupied = False  # 查询失败按未占用处理（写入失败另有兜底）
        if not occupied:
            return candidate
    raise HTTPException(
        status_code=409,
        detail="新会话 ID 连续冲突，请重试 fork",
    )


@router.post("/{session_id}/fork")
async def fork_session(
    request: Request,
    session_id: str,
    body: ForkBody | None = None,
    user_id: str = Depends(get_current_user_id),
):
    """把会话事件流复制成新会话（缺省全量复制，可按事件 id 截断）。

    语义：生成 new_sid（占用则重生成，最多 5 次）→ 旧 sid 事件按截断点
    过滤后逐条 append 到 new_sid（保序、payload 原样）→ 写一个 JSON 快照
    stub 让会话列表可见 → 返回 {session_id, event_count, up_to_event_id}。

    截断点（详见 ForkBody 文档字符串）：up_to_event_id 含端点 /
    after_event_id 排他（旧名兼容，R3-19 语义修正）。两者同给时
    up_to_event_id 优先。响应的 up_to_event_id 是**实际复制到的最后
    一条源事件 id**（复制进新流的事件会重新编号；空复制为 0）。

    仅 chat 会话可 fork：sid 含 ":"（waker:/wakerflow:）被 validate_id
    拒绝（400）。
    """
    session_id = validate_id(session_id, "会话 ID")
    log = _sessions_log(request)

    name = (body.name if body and body.name else None) or f"fork:{session_id[:8]}"

    # 冷归档合并视图（P3）：截断点落冷区也能切——合并视图与归档前的
    # events() 逐条等价，复制进新流的事件仍完整（payload 原样保序）。
    src_events = log.load_events_with_archive(session_id)
    if body is not None and body.up_to_event_id is not None:
        cutoff = max(0, int(body.up_to_event_id))
        src_events = [e for e in src_events if e["id"] <= cutoff]
    elif body is not None and body.after_event_id is not None:
        cutoff = max(0, int(body.after_event_id))
        src_events = [e for e in src_events if e["id"] < cutoff]
    elif body is not None and body.up_to_user_ordinal is not None:
        # 消息级寻址：保留到第 N 条 user 消息所在轮结束。丢弃段从第 N+1 条
        # user 消息所属轮的 turn/start 起（含）——只切到 user 事件的话，
        # 分支尾部会挂着无 turn/end 的 turn/start，前端把 fork 出的会话
        # 误判成"生成中"。
        from src.agent.session_log import TURN_START, USER_MSG
        user_positions = [i for i, ev in enumerate(src_events)
                          if ev.get("type") == USER_MSG]
        ordinal = int(body.up_to_user_ordinal)
        if ordinal < 0 or ordinal >= len(user_positions):
            raise HTTPException(
                status_code=404,
                detail=f"会话里没有第 {ordinal + 1} 条用户消息")
        if ordinal + 1 < len(user_positions):
            cut_idx = user_positions[ordinal + 1]
            for j in range(cut_idx - 1, -1, -1):
                if src_events[j].get("type") == TURN_START:
                    cut_idx = j
                    break
            src_events = src_events[:cut_idx]

    new_sid = _generate_new_sid(log)
    for ev in src_events:
        log.append(new_sid, ev.get("type", ""), ev.get("payload") or {})
    actual_up_to = max((e["id"] for e in src_events), default=0)

    # JSON 快照 stub：会话列表可见 + 承载 todos/vfs/waker 等非消息状态。
    # F7：这些字段此前没传——stub 只带 messages/name/project，fork 出的分支
    # 加载即回默认人格/空 todos/空 vfs。现从源会话快照一并透传（源快照
    # 缺失或旧 schema 无字段时给默认值，与 load_session 的回退一致）。
    # ADR-0004-D2：仅当快照缺失时创建、永不覆盖已有文件——worker 是权威
    # 写者，主进程旁路只做「从无到有」。
    # 归属（ADR-0005 D2）：fork 继承源会话的 project 绑定。
    from src.session_store import ensure_session_stub, read_session_meta
    src_meta = read_session_meta(LOCAL_USER, session_id) or {}
    ensure_session_stub(
        LOCAL_USER, log.derive_messages(new_sid), new_sid,
        todos=src_meta.get("todos") or [],
        virtual_fs=src_meta.get("virtual_fs") or {},
        waker=src_meta.get("waker") or "",
        name=name, project=src_meta.get("project", ""),
    )
    # P3 kv 权威对齐：源会话的状态 kv 行复制到新 sid（stub 落盘时已把
    # 透传值写进 kv，这里是"源 kv 行直拷"——源 JSON 缓存陈旧/缺失时仍
    # 保真；源是旧会话无 kv 行时本调用 no-op，由上面的 stub 透传兜底）。
    try:
        from src.storage.session_state_store import copy_state
        copy_state(session_id, new_sid)
    except Exception:
        logger.warning(f"fork 复制会话状态 kv 行失败（stub 透传已兜底）: "
                       f"{session_id} → {new_sid}", exc_info=True)

    logger.info(
        f"会话 fork: {session_id} → {new_sid} "
        f"(源事件 {len(log.load_events_with_archive(session_id))} 条，复制 {len(src_events)} 条, name={name!r})"
    )
    return {
        "session_id": new_sid,
        "event_count": len(src_events),
        "up_to_event_id": actual_up_to,
    }


@router.post("/{session_id}/truncate")
def truncate_session(request: Request, session_id: str, body: TruncateBody):
    """编辑重发前半程：把会话截断到第 N 条 user 消息之前（原地改写语义）。

    照 /api/compact 的闸门模式：chat 亲和路由 + 流式中 503 busy。必须经
    worker op 落地——主进程直写事件的话，活跃 worker 的内存桶不会更新，
    下一轮会把被截掉的旧尾巴喂 LLM 并在轮末覆盖存盘。
    """
    session_id = validate_id(session_id, "会话 ID")
    try:
        worker = chat_gate_worker(request, session_id)
    except SlotsFullError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    try:
        events = worker.send(
            "session_truncate",
            session_id=session_id,
            user_ordinal=int(body.user_ordinal),
            expect_text=body.expect_text or "",
        )
    except TimeoutError as e:
        _busy_if_timeout("session_truncate", e)
    data = events[0]["data"] if events else {"ok": False}
    if not data.get("ok", False):
        # 定位失败/乐观校验失败：400 让前端恢复气泡原状并提示
        raise HTTPException(status_code=400, detail=data.get("message") or "截断失败")
    return data
