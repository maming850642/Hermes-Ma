"""可恢复 SSE 事件总线（per-session 环形缓冲 + 订阅者扇出）。

架构位置（"可恢复 SSE 续流"的服务端核心）：
- POST /api/chat/stream 的后台泵把 worker 流的每条事件 publish 到本总线
  （总线是会话帧序号 seq 的**唯一**分配源，POST 响应与 GET 订阅共享同一
  编号空间，前端 Last-Event-ID 才有确定语义）。
- GET /api/chat/stream/{sid} 订阅端点用 after / Last-Event-ID 游标补差
  （seq > after 的缓冲帧）+ 实时续推，实现"切页回来续流"。

线程模型：uvicorn 单事件循环。总线全部状态只在事件循环线程读写；
worker 事件来自同步生成器（send_stream，阻塞在 stdout 泵队列上），由
chat 路由的后台 asyncio 任务经 run_in_threadpool(next, it) 逐条拉取——
publish 因此总发生在事件循环线程上，asyncio.Queue 扇出天然安全（这是
所选的桥接方式）。为兼容其它线程生产者（直接驱动同步 send_stream 的
裸线程），publish_threadsafe 提供 loop.call_soon_threadsafe 桥
（call_later/call_soon 的 ready 队列 FIFO，同一生产者线程内保序）。

内存上界：会话数 × buffer_max 帧有界；turn 终态后 TTL 清理（默认 5
分钟），buffer 超限自然滚动（deque maxlen 丢最旧）；会话删除走
drop_session 立即清；app shutdown 走 clear() 全清。
"""
import asyncio
from collections import deque
from typing import NamedTuple

# 默认环形缓冲深度（帧）：一轮长对话 token 数量级在数百~数千，
# 2000 帧足够覆盖"断开期间漏掉的一轮"的绝大多数场景。
DEFAULT_BUFFER_MAX = 2000

# turn 终态后的通道保留时长（秒）：给"切页回来"留补差窗口。
DEFAULT_TTL_SECONDS = 300.0

# SSE 事件名中的 turn 终态（收到后本轮生成完结，GET 订阅转发完即可 done）
TERMINAL_EVENTS = frozenset({"complete", "error"})


class Frame(NamedTuple):
    """总线帧：seq（会话内从 1 起单调递增；busy 等请求私有帧为 None）。"""
    seq: int | None
    event: str
    data: dict


# 订阅队列哨兵：本轮已到终态 / 通道被清理——订阅方据此发 done 收流。
# 与帧（NamedTuple）类型不同，永不相混淆。
STREAM_END = object()


def put_drop_oldest(queue: asyncio.Queue, item) -> None:
    """投递一帧；队列满则丢最旧（慢消费者不反压生产者）。

    丢掉的帧仍在环形缓冲里，客户端可经 GET 订阅 with after=游标 补差
    找回——所以"丢最旧"只损失实时性，不损失数据可达性。
    """
    while True:
        try:
            queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            try:
                queue.get_nowait()   # 丢最旧，腾位重试
            except asyncio.QueueEmpty:
                pass


class Subscription:
    """subscribe() 的原子快照结果。

    - replay：seq > after 的补差帧快照（列表脱离 deque，后续滚动不影响）；
    - live：订阅瞬间总线上是否有活动 turn；
    - queue：实时续推队列（attach() 注册进通道扇出后才开始收帧）。

    subscribe → (调用方判断 live) → attach 的全过程在事件循环线程上
    无 await 连续执行，与 publish/end_turn 天然互斥——不存在
    "补差快照与订阅注册之间漏帧"的窗口。
    """

    __slots__ = ("replay", "live", "queue")

    def __init__(self, replay: list, live: bool, queue: asyncio.Queue):
        self.replay = replay
        self.live = live
        self.queue = queue


class _Channel:
    """单会话通道：环形缓冲 + 订阅者队列列表 + turn 计数 + TTL 定时器。"""

    __slots__ = ("buffer", "next_seq", "subscribers", "active_turns", "gc_handle")

    def __init__(self, buffer_max: int):
        self.buffer = deque(maxlen=buffer_max)
        self.next_seq = 1
        self.subscribers: list[asyncio.Queue] = []
        self.active_turns = 0
        self.gc_handle = None   # loop.TimerHandle | None


class ChatBus:
    """可恢复 SSE 事件总线（单事件循环；见模块头线程模型说明）。"""

    def __init__(self, buffer_max: int = DEFAULT_BUFFER_MAX,
                 ttl_seconds: float = DEFAULT_TTL_SECONDS,
                 subscriber_max: int | None = None):
        self._channels: dict[str, _Channel] = {}
        self._buffer_max = max(1, int(buffer_max))
        self._ttl = float(ttl_seconds)
        self._subscriber_max = max(1, int(
            subscriber_max if subscriber_max is not None else buffer_max))
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- 内部：通道与循环绑定 ----

    def _loop_or_bind(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is None:
            self._loop = loop = asyncio.get_running_loop()
        return loop

    def _ensure_on_loop(self) -> None:
        """必须在事件循环线程调用（尽早暴露误用，而不是静默错乱）。"""
        running = asyncio.get_running_loop()
        if self._loop is not None and running is not self._loop:
            raise RuntimeError(
                "ChatBus 绑定的事件循环与当前运行循环不一致"
                "（总线生命周期应与单个事件循环一致）")

    def _channel(self, sid: str) -> _Channel:
        ch = self._channels.get(sid)
        if ch is None:
            ch = _Channel(self._buffer_max)
            self._channels[sid] = ch
            self._loop_or_bind()
        return ch

    # ---- 生产侧（事件循环线程）----

    def begin_turn(self, sid: str) -> None:
        """标记一轮生成开始（POST/approve 泵启动时调）。

        计数制而非布尔：busy 请求也会短暂 begin/end（它从未真正上车，
        但同一 worker 上另一轮在跑，计数保证不误熄在途标志）。
        """
        ch = self._channel(sid)
        ch.active_turns += 1
        if ch.gc_handle is not None:      # 新轮接上：撤销待执行的 TTL 清理
            ch.gc_handle.cancel()
            ch.gc_handle = None

    def publish(self, sid: str, event: str, data: dict) -> Frame:
        """发布一帧：分配 seq、入环形缓冲、扇出到全部订阅者。

        必须在事件循环线程调用（跨线程生产者走 publish_threadsafe）。
        返回带 seq 的帧（调用方据此给 SSE 帧 打 id: 行）。
        """
        self._ensure_on_loop()
        ch = self._channel(sid)
        frame = Frame(ch.next_seq, event, dict(data or {}))
        ch.next_seq += 1
        ch.buffer.append(frame)           # 超 maxlen 自然滚动丢最旧
        for q in list(ch.subscribers):
            put_drop_oldest(q, frame)
        return frame

    def publish_threadsafe(self, sid: str, event: str, data: dict) -> None:
        """跨线程投递桥：loop.call_soon_threadsafe 转回循环线程执行 publish。

        保序前提：单生产者线程（call_soon_threadsafe 的 ready 队列 FIFO）。
        无返回 seq——分配发生在循环线程上；需要 seq 的调用方应确保自身
        在循环线程上走 publish()。
        """
        loop = self._loop
        if loop is None:
            raise RuntimeError(
                "ChatBus 尚未绑定事件循环（需先在循环线程上触发任一操作）")
        loop.call_soon_threadsafe(self.publish, sid, event, data)

    def end_turn(self, sid: str) -> None:
        """标记一轮生成结束：唤醒存量订阅者收流，并排 TTL 清理。"""
        self._ensure_on_loop()
        ch = self._channels.get(sid)
        if ch is None:
            return
        if ch.active_turns > 0:
            ch.active_turns -= 1
        if ch.active_turns == 0:
            # 终态帧（complete/error）已先于本调用 publish 进队列；
            # 哨兵让"终态帧之后才 attach / 还在等待"的订阅者也能收流。
            for q in list(ch.subscribers):
                put_drop_oldest(q, STREAM_END)
            self._schedule_gc(sid, ch)

    # ---- 订阅侧（事件循环线程）----

    def subscribe(self, sid: str, after: int = 0) -> Subscription:
        """原子快照订阅：补差帧（seq > after）+ 在途标志 + 待注册队列。

        不注册扇出（attach 才注册）——无在途的订阅方用不到实时队列，
        不该在通道里留下悬空引用。
        """
        self._ensure_on_loop()
        ch = self._channels.get(sid)
        queue = asyncio.Queue(maxsize=self._subscriber_max)
        if ch is None:
            return Subscription(replay=[], live=False, queue=queue)
        replay = [f for f in ch.buffer if f.seq > after]
        return Subscription(replay=replay,
                            live=ch.active_turns > 0, queue=queue)

    def attach(self, sid: str, sub: Subscription, immediate_end: bool = False) -> None:
        """把订阅注册进通道扇出（subscribe 判定在途后调用；两步之间
        调用方不得 await → 原子，不漏帧）。

        immediate_end：end_turn 已发过 STREAM_END 哨兵的既有通道（迟到
        订阅者）——补一发让本次订阅立即走补差→done 收流。spawn 窗口的
        订阅（事件兜底判定，通道可能新建）不补——等待预算语义由端点的
        keepalive 复核管理；幽灵在途（仅 /active 真、总线无记录）同样
        不补（新通道无哨兵可补，端点 keepalive 复核会自愈收流）。
        """
        self._ensure_on_loop()
        ch = self._channel(sid)
        # 哨兵补发判据：通道真的"收过 turn/有帧"（end_turn 的哨兵早已投完
        # 的迟到订阅者）。注意 subscribe 会懒建通道——空通道（幽灵在途/
        # spawn 窗口）没有哨兵可补，端点 keepalive 复核自愈。
        has_activity = ch.active_turns > 0 or len(ch.buffer) > 0
        ch.subscribers.append(sub.queue)
        if immediate_end and has_activity and ch.active_turns == 0:
            put_drop_oldest(sub.queue, STREAM_END)
        # 幽灵在途（探测说在跑但总线无 turn 记录）也会走到这里：新通道
        # 没有 end_turn 排清理，补一个，保证通道最终可回收。
        if ch.active_turns == 0 and ch.gc_handle is None:
            self._schedule_gc(sid, ch)

    def unsubscribe(self, sid: str, queue: asyncio.Queue) -> None:
        """订阅方收流/断开时移除队列引用（幂等）。"""
        ch = self._channels.get(sid)
        if ch is None:
            return
        try:
            ch.subscribers.remove(queue)
        except ValueError:
            pass

    # ---- 查询 / 清理 ----

    def turn_active(self, sid: str) -> bool:
        """总线上是否存在活动 turn（GET 订阅的在途信号之一）。"""
        ch = self._channels.get(sid)
        return bool(ch is not None and ch.active_turns > 0)

    def has_channel(self, sid: str) -> bool:
        return sid in self._channels

    def buffer_len(self, sid: str) -> int:
        """调试/测试用：通道缓冲深度。"""
        ch = self._channels.get(sid)
        return len(ch.buffer) if ch is not None else 0

    def drop_session(self, sid: str) -> None:
        """会话删除：立即关闭订阅者并移除通道（幂等）。

        须在事件循环线程调用（会向订阅队列投哨兵）；同步路由里调用需经
        publish_threadsafe 同款桥（call_soon_threadsafe）转回循环线程。
        """
        self._ensure_on_loop()
        ch = self._channels.get(sid)
        if ch is not None:
            self._teardown(sid, ch)

    def clear(self) -> None:
        """app shutdown：清空全部通道（订阅者收 STREAM_END、TTL 定时器撤销）。"""
        self._ensure_on_loop()
        for sid in list(self._channels):
            self.drop_session(sid)

    @property
    def session_count(self) -> int:
        return len(self._channels)

    def bound_loop(self):
        """已绑定的事件循环（未绑定返回 None；调用方据此判断可否复用）。"""
        return self._loop

    # ---- TTL 清理 ----

    def _schedule_gc(self, sid: str, ch: _Channel) -> None:
        if ch.gc_handle is not None:
            ch.gc_handle.cancel()
        loop = self._loop_or_bind()
        ch.gc_handle = loop.call_later(self._ttl, self._gc, sid)

    def _gc(self, sid: str) -> None:
        ch = self._channels.get(sid)
        if ch is None:
            return
        if ch.active_turns > 0:
            return   # 新一轮已接上（begin_turn 本应已 cancel 定时器，双保险）
        self._teardown(sid, ch)

    def _teardown(self, sid: str, ch: _Channel) -> None:
        if ch.gc_handle is not None:
            ch.gc_handle.cancel()
            ch.gc_handle = None
        for q in list(ch.subscribers):
            put_drop_oldest(q, STREAM_END)
        ch.subscribers.clear()
        self._channels.pop(sid, None)
