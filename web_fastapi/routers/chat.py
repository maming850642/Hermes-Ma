"""对话核心 API：SSE 流式 + HITL 审批 + 中断 + 可恢复续流。

方向 B（多进程）：前端只做 IPC→SSE 转发。所有 agent 逻辑（stream_invoke、
contextvar、线程模型）都在 worker 子进程里，前端零线程复杂性。

可恢复续流（消费解耦）：
- POST /stream 与 /approve：handler 启动**后台泵任务**（asyncio task），
  经 run_in_threadpool 逐条拉取同步 send_stream 的全部事件，每条
  （a）publish 到 chat_bus（seq 唯一编号源）（b）投给本请求 SSE 队列。
  SSE 响应生成器只从本请求队列取帧——客户端断开（GeneratorExit）只
  丢弃本队列订阅，泵继续把 worker 流消费到自然结束（断开≠取消：worker
  跑完落盘 + 总线拿到完整流，切页回来经 GET 补差找回）。
- GET /stream/{sid}：EventSource 兼容订阅端点（见端点 docstring 契约）。
"""
import asyncio
import logging
import time
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from src.constants import LOCAL_USER
from web_fastapi.chat_bus import (
    ChatBus, Frame, STREAM_END, TERMINAL_EVENTS, put_drop_oldest,
)
from web_fastapi.security import validate_id
from web_fastapi.sse import encode_sse_event
from web_fastapi.worker_manager import SlotsFullError
from web_fastapi.models import ChatRequest, ApprovalRequest, StopRequest

logger = logging.getLogger("hermes.web.chat")
router = APIRouter()

# GET 订阅流心跳间隔：每 15s 一条 SSE comment（: keepalive）防代理断连
KEEPALIVE_SECONDS = 15.0

# 单请求 SSE 队列上限：慢客户端丢最旧（帧已在总线缓冲里，可经 GET
# after=游标 补差找回，丢的只是实时性不是可达性）
REQUEST_QUEUE_MAX = 500

# 后台泵任务强引用（asyncio 对 task 只持弱引用，不持会被 GC 中途夭折）
_PUMP_TASKS: set = set()

# send_stream 迭代尽哨兵：StopIteration 不能跨线程池/协程边界传递，
# 先在池线程里转成普通返回值。
_STREAM_EXHAUSTED = object()


def _worker_for(request: Request, session_id: str):
    """M5：按会话亲和取 worker（超限抛 SlotsFullError → 429）。

    P2-13：无实例时 get_or_create 会同步 spawn + 等 ready（Windows 下
    十秒级）——async handler 必须经 run_in_threadpool 调本函数，否则
    新会话首条消息会冻结整个事件循环（全站停摆）。
    """
    wm = request.app.state.worker_manager
    slot = wm.acquire_chat_slot(session_id)
    return wm.get_or_create(LOCAL_USER, slot=slot)


def _get_bus(app) -> ChatBus:
    """取（或懒建/换新）chat 事件总线。

    生产 app 在 lifespan 创建（app.py），循环活到进程退出。测试常挂裸
    router 的 FastAPI 且经 TestClient 逐请求独立 portal（每请求一个全新
    事件循环、用完即关）——绑定的循环已关闭时丢弃旧总线换新（语义 =
    服务重启：缓冲历史不延续），否则旧循环上的 asyncio 原语会让后续
    请求全数 500/挂死。单事件循环上 check-then-set 之间无 await，原子。
    """
    bus = getattr(app.state, "chat_bus", None)
    loop = bus.bound_loop() if bus is not None else None
    if bus is None or (loop is not None and loop.is_closed()):
        bus = ChatBus()
        app.state.chat_bus = bus
    return bus


def _map_worker_event(data: dict):
    """单条 worker 事件 → (SSE 事件名, payload)；未知类型返回 None。

    relay_stream（同步兼容路径）与后台泵共用本映射，保证两条路径
    帧格式完全一致（前端契约锚点）。
    """
    mtype = data.get("type")
    if mtype == "event":
        return data.get("event", "message"), data.get("data", {})
    if mtype == "result":
        # chat 的 done 也可能带 result（如 stop）
        return "complete", data.get("data", {})
    if mtype == "error":
        # busy 透传给前端：停止后立即再发会撞上取消收尾期的槽锁，
        # 前端据此自动重试（只认结构化标志，不猜文案）
        return "error", {"message": data.get("message", ""),
                         "busy": bool(data.get("busy"))}
    return None


def _next_or_end(iterator):
    """next() 的 StopIteration 哨兵化（见 _STREAM_EXHAUSTED）。"""
    try:
        return next(iterator)
    except StopIteration:
        return _STREAM_EXHAUSTED


async def _pump_worker_stream(app, worker, sid: str, queue: asyncio.Queue,
                              op: str, **kwargs):
    """后台泵：把 worker.send_stream 消费到自然结束，逐帧写总线 + 投请求队列。

    客户端断开不影响本任务（请求队列只是投递目标之一，满则丢最旧）——
    保证总线拿到完整流（GET 续流补差的真相源）。

    线程桥：send_stream 是同步生成器（阻塞在 worker stdout 泵队列上），
    逐条经 run_in_threadpool(next) 拉取；publish 因此发生在事件循环线程，
    总线的 asyncio.Queue 扇出安全（uvicorn 单循环假设的落点）。

    busy 语义保持：send_stream 抢锁逻辑不动（本任务里抢一次）；busy 是
    "本请求从未上车"的私有错误，只投请求队列、不进总线（不是会话
    规范流的组成部分，GET 补差不应看到陈旧的 busy）。

    取消安全网（与旧 relay_stream 同契约）：本请求已上车（收到过
    event/result）且死于泵超时 → request_cancel；busy/正常结束/中途
    error/EOF 一律不取消。
    """
    bus = _get_bus(app)
    saw_active = False
    timed_out = False
    try:
        bus.begin_turn(sid)
        stream = worker.send_stream(op, **kwargs)
        while True:
            data = await run_in_threadpool(_next_or_end, stream)
            if data is _STREAM_EXHAUSTED:
                break
            mapped = _map_worker_event(data)
            if mapped is None:
                continue
            name, payload = mapped
            mtype = data.get("type")
            if mtype in ("event", "result"):
                saw_active = True
            elif mtype == "error" and data.get("timeout"):
                timed_out = True
            if mtype == "error" and data.get("busy"):
                # 请求私有帧：无 seq、不进总线
                put_drop_oldest(queue, Frame(None, name, payload))
            else:
                put_drop_oldest(queue, bus.publish(sid, name, payload))
    except Exception as e:
        # send_stream 本体抛异常（worker 已退出/管道断等）：转终态 error 帧
        # （对齐旧 StreamingResponse 中途断流，但补上结构化 error 更友好）。
        logger.warning("chat 后台泵异常: sid=%s op=%s err=%s", sid, op, e)
        try:
            put_drop_oldest(queue, bus.publish(
                sid, "error", {"message": str(e), "busy": False}))
        except Exception:
            logger.exception("异常路径发布 error 帧失败: sid=%s", sid)
    finally:
        # end_turn 的任何失败都不得吞掉 STREAM_END——否则请求响应生成器
        # 会永远等不到收流哨兵（挂死连接）。
        try:
            bus.end_turn(sid)
        except Exception:
            logger.exception("end_turn 失败: sid=%s", sid)
        if saw_active and timed_out:
            worker.request_cancel()
        put_drop_oldest(queue, STREAM_END)


async def _request_queue_sse(queue: asyncio.Queue):
    """POST 主路径响应生成器：只从本请求队列取帧。

    客户端断开 → Starlette 关闭本生成器，仅丢弃本队列；后台泵不受影响，
    继续消费 worker 流到自然结束（断开≠取消）。

    心跳（2026-09-09）：对齐 GET 订阅流的 15s keepalive——长静默轮
    （长工具执行 / 长思考首 token 前）期间连接完全无字节，代理/浏览器
    可能掐成半开：前端 reader.read() 永久挂起、流锁不释放（实测
    "输出中途停住、页面卡死" 的载体）。注释行前端 parseSSE 自然忽略。
    """
    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
        except asyncio.TimeoutError:
            yield b": keepalive\n\n"
            continue
        if item is STREAM_END:
            return
        yield encode_sse_event(item.event, item.data, seq=item.seq).encode("utf-8")


def _start_relay(request: Request, worker, op: str, sid: str,
                 **kwargs) -> StreamingResponse:
    """POST 流式端点公共装配：起后台泵 + 返回队列驱动的 SSE 响应。"""
    queue = asyncio.Queue(maxsize=REQUEST_QUEUE_MAX)
    task = asyncio.create_task(
        _pump_worker_stream(request.app, worker, sid, queue, op, **kwargs))
    _PUMP_TASKS.add(task)
    task.add_done_callback(_PUMP_TASKS.discard)
    return StreamingResponse(_request_queue_sse(queue),
                             media_type="text/event-stream")


def relay_stream(worker, op: str, **kwargs):
    """同步 SSE 转发生成器（兼容保留：relay 语义的测试契约锚点）。

    生产路径已改走后台泵（消费解耦，见 _pump_worker_stream）；本函数
    保持旧语义供测试与无总线场景直用——两条路径共用 _map_worker_event，
    帧格式恒一致。

    finally 的安全网取消只在一种情况触发：**本请求已上车**（收到过
    worker 的 event/result）且死于泵超时（worker 疑似卡死）。其余一切
    结束方式都不得发 chat_stop：

    - 忙超时（锁 5s 未拿到）：本请求从未上车，此时发 chat_stop 会把
      正在跑的另一条 chat 误杀（实测：切权限模式后发的消息被静默取消，
      用户视角 = "没反应"）；
    - 正常结束/中途 error/EOF：chat 已完结，无物可取消；若此时恰有
      下一条 chat 刚起跑，cancel 反而会误杀它；
    - GeneratorExit（用户切页面/关标签导致连接断开）：**不取消**——
      worker 继续把本轮跑完并落盘。显式停止走 /api/chat/stop。
    """
    client_gone = False
    saw_active = False   # 本请求已上车（收到过 worker 的 event/result）
    timed_out = False    # 死于泵超时（唯一需要安全网取消的情形）
    try:
        for data in worker.send_stream(op, **kwargs):
            mapped = _map_worker_event(data)
            if mapped is None:
                continue
            name, payload = mapped
            mtype = data.get("type")
            if mtype in ("event", "result"):
                saw_active = True
            elif mtype == "error":
                if data.get("timeout"):
                    timed_out = True
                elif data.get("busy"):
                    pass  # 从未上车，不置 saw_active
            yield encode_sse_event(name, payload).encode("utf-8")
    except GeneratorExit:
        client_gone = True
        raise
    finally:
        if not client_gone and saw_active and timed_out:
            worker.request_cancel()


@router.get("/active")
async def chat_active(request: Request, session_id: str = ""):
    """探测会话是否正在生成（后台生成提示条轮询用，主进程内存读零 IPC）。"""
    wm = request.app.state.worker_manager
    wp = wm.lookup(LOCAL_USER, session_id) if session_id.strip() else None
    return {"session_id": session_id, "active": bool(wp is not None and wp.streaming)}


def _prelog_turn(app, sid: str, message: str, project: str = "") -> bool:
    """发送即持久化（spawn 窗口可见性根治）：

    Windows 下 worker spawn 十秒级——此前 turn/start+user/message 事件
    要等 agent 开轮才写、stub 在 _op_chat 里建，切页回来落在 spawn 窗口
    时三条数据通道（事件库/会话列表/active 探测）全空，恢复要 18s+。
    这里在 spawn 之前由主进程把"用户已发送"这个事实落盘：

    - ensure_session_stub：会话列表/侧栏立即可见（worker 侧保留幂等双保险）
    - 事件库预写 turn/start + user/message：切回时零锁直读立即可渲染
      user 消息 + 在途占位（worker 侧 agent 收到 prelogged 标记跳过重写）

    调用方须先短路 busy（同会话在途时不预写，防重复轮事件）。
    任何失败只告警降级为现行为（等 worker 写），不阻断发送。
    返回是否预写成功（成功才给 cmd 附 prelogged 标记）。
    """
    ok = False
    try:
        from src.session_store import ensure_session_stub
        ensure_session_stub(
            "local", [{"role": "user", "content": message}], sid,
            project=project or None)
        log = _sessions_log_from_app(app)
        log.append(sid, "turn/start", {"input": message, "workspace_mode": "local"})
        log.append(sid, "user/message", {"content": message})
        ok = True
    except Exception:
        logger.warning("开轮预写失败（降级为 worker 写）: sid=%s", sid, exc_info=True)
    return ok


def _sessions_log_from_app(app):
    """从 app.state 取组合根 SessionLog（与 sessions._sessions_log 同源，
    这里不依赖 Request 以便在泵/预写路径复用）。"""
    ctx = getattr(app.state, "cordis_ctx", None)
    if ctx is not None:
        try:
            log = ctx.try_get("sessions")
            if log is not None:
                return log
        except Exception:
            pass
    from src.agent.session_log import SessionLog
    return SessionLog()


@router.post("/stream")
async def chat_stream(body: ChatRequest, request: Request):
    sid = (body.session_id or "").strip()
    prelogged = False
    if sid:
        # busy 短路：同会话已在途（另一连接流式中）不预写——重写
        # turn/start+user/message 会造成重复轮事件；让泵的 busy 错误
        # 自然产生（前端既有重试语义）
        wm = request.app.state.worker_manager
        wp = wm.lookup(LOCAL_USER, sid)
        if not (wp is not None and getattr(wp, "streaming", False)):
            def _prelog_on_pool() -> bool:
                # P2-11：get_active_project（SQLite 读）+ _prelog_turn（JSON
                # 快照原子写 + 2 次 SQLite INSERT）都是阻塞调用，包进线程池
                # （对齐下方 _worker_for 的用法）。busy 短路判定留在本协程
                # （内存读），预写语义与返回值不变。
                try:
                    from src.storage.projects_store import get_active_project
                    project = get_active_project()
                except Exception:
                    project = ""
                return _prelog_turn(request.app, sid, body.message, project)
            prelogged = await run_in_threadpool(_prelog_on_pool)
    try:
        worker = await run_in_threadpool(_worker_for, request, body.session_id)
    except SlotsFullError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    return _start_relay(request, worker, "chat", sid,
                        message=body.message,
                        thinking=body.thinking,
                        images=body.images,
                        session_id=body.session_id,
                        waker=body.waker,
                        prelogged=prelogged)


@router.get("/stream/{sid}")
async def chat_stream_subscribe(request: Request, sid: str, after: int | None = None):
    """可恢复续流订阅端点（EventSource 兼容，免认证与既有模型一致）。

    契约（前端并行开发依赖，写死后不得偏离）：
    - `GET /api/chat/stream/{sid}?after=<seq>` → text/event-stream；
    - 帧与 POST 流完全一致的事件类型（token/reasoning_token/tool_start/
      tool_end/approval_request/complete/error…），每帧带 `id: <seq>`
      （总线 per-session 单调序号，与 POST 共享同一编号空间）；
    - Last-Event-ID 请求头同效（EventSource 重连自动带；显式 after 优先）；
    - 无在途生成（总线无活动 turn 且 /active 探测 false）：补差帧后
      `event: done`（data: {}）即关流；
    - 在途：实时续推直到 complete/error 终态帧（或 turn 结束哨兵），
      随后 `event: done` 关流；
    - 心跳：每 15s 一条 SSE comment（: keepalive）防代理断连；
    - 不抢 per-worker 锁（不惊动 worker）；多订阅者各自独立游标。
    """
    sid = validate_id(sid, "会话 ID")
    # 游标解析：显式 after 优先；否则 Last-Event-ID 头；缺省 0（全量补差）
    if after is None:
        header_val = (request.headers.get("last-event-id") or "").strip()
        try:
            after = int(header_val) if header_val else 0
        except ValueError:
            after = 0
    after = max(0, after)

    bus = _get_bus(request.app)
    wp = request.app.state.worker_manager.lookup(LOCAL_USER, sid)
    probe_live = bool(wp is not None and getattr(wp, "streaming", False))
    # spawn 窗口兜底：总线/激活探测皆 false ≠ 无在途——主进程预写的
    # turn/start 尚无 turn/end 配对时，本轮正等 worker spawn/首帧。
    # 直读事件库（零锁）：从尾向前找最近的轮边界，turn/start 未被
    # turn/end 闭合且在时效窗内 → 按在途等待（gen 的 keepalive 循环
    # 会复核，真死轮由等待预算兜底收流）；超窗（spawn 失败/历史孤儿，
    # 如测试遗留）不等待，照旧立即 done。
    # P2-11：events() 全表扫描是阻塞调用，包进线程池——因此兜底判定
    # 挪到 subscribe **之前**（subscribe→attach 之间不得有 await 的原子
    # 两段式约定不变）：判定期间发布的帧落在通道缓冲里，随后的
    # subscribe 快照照常带出，无漏帧窗口。判定条件与旧实现等价
    # （probe_live 直通；bus.turn_active 即 subscribe 将返回的 live，
    # 免一次无谓扫库）。
    spawn_window = False   # 事件兜底判定的在途（区别于幽灵 probe_live）
    db_live = False
    if not probe_live and not bus.turn_active(sid):
        try:
            def _scan_events() -> list[dict]:
                # SessionLog 回退构造（sqlite3.connect + 建表）同为阻塞
                # 调用，一并放池内
                return _sessions_log_from_app(request.app).events(sid)
            events = await run_in_threadpool(_scan_events)
            for ev in reversed(events):
                t = ev.get("type")
                if t == "turn/end":
                    break
                if t == "turn/start":
                    ts = float(ev.get("ts") or 0)
                    if ts <= 0 or (time.time() - ts) <= 600.0:
                        db_live = True
                        spawn_window = True
                    break
        except Exception:
            pass
    # 原子两段式订阅：快照补差帧 + 在途标志（subscribe）→ 判定在途 →
    # 注册实时扇出（attach）。两步之间无 await，与 publish/end_turn
    # 互斥——不存在漏帧窗口。
    sub = bus.subscribe(sid, after)
    live = sub.live or probe_live or db_live
    if live:
        bus.attach(sid, sub, immediate_end=probe_live and not spawn_window)

    async def gen():
        # 1) 补差：seq > after 的缓冲帧（含滚动前已落缓冲的全部）
        for frame in sub.replay:
            yield encode_sse_event(frame.event, frame.data,
                                   seq=frame.seq).encode("utf-8")
        # 2) 无在途：补差即终态，done 收流
        if not live:
            yield encode_sse_event("done", {}).encode("utf-8")
            return
        # 3) 实时续推直到终态帧 / turn 结束哨兵
        try:
            waited = 0.0   # spawn 窗口兜底的等待预算（未闭合 turn/start 但
            #               总线始终无帧——worker spawn 失败/异常死轮）
            while True:
                try:
                    item = await asyncio.wait_for(sub.queue.get(),
                                                  timeout=KEEPALIVE_SECONDS)
                    waited = 0.0
                except TimeoutError:
                    yield b": keepalive\n\n"
                    # 每 15s 复核在途：总线与 /active 探测都转闲即收流
                    # （自愈"幽灵在途"，防订阅者永挂）
                    wp2 = request.app.state.worker_manager.lookup(LOCAL_USER, sid)
                    if not (bus.turn_active(sid) or
                            (wp2 is not None and getattr(wp2, "streaming", False))):
                        break
                    waited += KEEPALIVE_SECONDS
                    if waited >= 30.0:   # ~2 个 keepalive 无帧无在途 → 死轮收流
                        #（等待预算 30s：真实 EventSource 断线会自动重连，
                        # 端点挂更久无意义；也覆盖"worker spawn 失败"死轮）
                        break
                    continue
                if item is STREAM_END:
                    break
                yield encode_sse_event(item.event, item.data,
                                       seq=item.seq).encode("utf-8")
                if item.event in TERMINAL_EVENTS:
                    break
            yield encode_sse_event("done", {}).encode("utf-8")
        finally:
            if live:
                bus.unsubscribe(sid, sub.queue)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store"})


@router.post("/approve")
async def approve(body: ApprovalRequest, request: Request):
    # thread_id === session_id（恒等），据此路由到出生槽；冷槽经
    # recover_into 可自行恢复 pending 审批，路由任意性成立。
    # P2-15：槽满同样转 429（对齐 chat_stream），否则 3 槽全忙时提交
    # 审批 → 500 且审批卡死。
    try:
        worker = await run_in_threadpool(_worker_for, request, body.thread_id)
    except SlotsFullError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    return _start_relay(request, worker, "chat_approve",
                        (body.thread_id or "").strip(),
                        thread_id=body.thread_id,
                        decision=body.decision,
                        reason=body.reason)


@router.post("/stop")
async def stop_chat(body: StopRequest, request: Request):
    # M5：取消只作用于目标会话的槽（不为此新 spawn 实例）。
    # 不走 worker.send（推理期间锁被 send_stream 占用，send 会超时）。
    # request_cancel 直接写 stdin 插队 + worker 侧 _cancel_event 轮询生效。
    request.app.state.worker_manager.cancel_for(body.session_id)
    return {"ok": True}
