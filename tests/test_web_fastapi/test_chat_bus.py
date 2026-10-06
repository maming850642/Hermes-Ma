"""可恢复 SSE 续流：事件总线单元 + 订阅端点 + POST 消费解耦回归。

覆盖契约：
- 总线：seq 单调 / 环形滚动 / 补差游标 / 多订阅者独立队列与游标 /
  慢消费者丢最旧 / turn 终态哨兵 / TTL 清理与 begin_turn 撤销 /
  会话删除清理 / 跨线程投递桥 / clear 撤销定时器。
- GET /api/chat/stream/{sid}：补差+实时推+done 三态、Last-Event-ID 头、
  query 优先、无在途立即 done、越界游标、非法 sid 400、探测在途的
  心跳复核收流。
- POST 解耦：客户端中途断开后总线仍收满全流（假 worker send_stream）、
  POST 帧带总线 id、busy 不进总线且不误取消、approve 共享 seq 空间。
"""
import asyncio
import json
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.constants import LOCAL_USER
from web_fastapi.chat_bus import ChatBus, STREAM_END
from web_fastapi.dependencies import get_current_user_id
from web_fastapi.routers import chat as chat_router

# ════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════


def _drain(queue):
    """非阻塞排空队列（单元测试用）。"""
    items = []
    while True:
        try:
            items.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return items


def _parse_sse(raw: bytes):
    """SSE 字节流 → [(id|None, event, data)]；忽略 comment（心跳）块。"""
    frames = []
    for block in raw.decode("utf-8").split("\n\n"):
        lines = block.splitlines()
        if not any(ln.startswith(("id: ", "event: ", "data: ")) for ln in lines):
            continue                                   # comment-only 块（keepalive）
        fid, event, data = None, "message", {}
        for ln in lines:
            if ln.startswith(":"):
                continue
            if ln.startswith("id: "):
                fid = int(ln[4:])
            elif ln.startswith("event: "):
                event = ln[7:]
            elif ln.startswith("data: "):
                data = json.loads(ln[6:])
        frames.append((fid, event, data))
    return frames


# ════════════════════════════════════════════════════════════════
# 一、ChatBus 单元（纯 asyncio，无 HTTP）
# ════════════════════════════════════════════════════════════════


def test_publish_assigns_monotonic_seq_and_replay_after_cursor():
    async def main():
        bus = ChatBus()
        bus.begin_turn("s")
        f1 = bus.publish("s", "token", {"content": "a"})
        f2 = bus.publish("s", "token", {"content": "b"})
        f3 = bus.publish("s", "complete", {})
        bus.end_turn("s")
        assert (f1.seq, f2.seq, f3.seq) == (1, 2, 3)
        assert [f.seq for f in bus.subscribe("s", after=0).replay] == [1, 2, 3]
        assert [f.seq for f in bus.subscribe("s", after=1).replay] == [2, 3]
        assert [f.seq for f in bus.subscribe("s", after=3).replay] == []
        # turn 已结束：不在途
        assert bus.subscribe("s", after=0).live is False
    asyncio.run(main())


def test_ring_buffer_rolls_and_replay_reflects_it():
    async def main():
        bus = ChatBus(buffer_max=3)
        bus.begin_turn("s")
        for i in range(6):
            bus.publish("s", "token", {"i": i})
        assert bus.buffer_len("s") == 3
        # 全量游标也只见滚动窗口内的最新 3 帧
        assert [f.seq for f in bus.subscribe("s", after=0).replay] == [4, 5, 6]
        # 窗口内游标：精确补差
        assert [f.seq for f in bus.subscribe("s", after=4).replay] == [5, 6]
    asyncio.run(main())


def test_two_live_subscribers_independent_queues_and_cursors():
    async def main():
        bus = ChatBus()
        bus.begin_turn("s")
        a = bus.subscribe("s", after=0)
        bus.attach("s", a)
        bus.publish("s", "token", {"i": 1})           # seq 1 → 只进 A 队列
        b = bus.subscribe("s", after=0)               # B 补差含 seq1
        assert [f.seq for f in b.replay] == [1]
        assert b.live is True
        bus.attach("s", b)
        bus.publish("s", "token", {"i": 2})           # seq 2 → A、B 队列
        bus.end_turn("s")
        a_items = _drain(a.queue)
        b_items = _drain(b.queue)
        # A：实时收 seq1/seq2，终态后 STREAM_END 哨兵
        assert [f.seq for f in a_items[:2]] == [1, 2]
        assert a_items[2] is STREAM_END
        # B：补差走 replay（seq1），队列只有 seq2 + 哨兵——独立游标
        assert [f.seq for f in b_items[:1]] == [2]
        assert b_items[1] is STREAM_END
    asyncio.run(main())


def test_slow_subscriber_drops_oldest_but_buffer_keeps_all():
    async def main():
        bus = ChatBus(buffer_max=100, subscriber_max=2)
        bus.begin_turn("s")
        sub = bus.subscribe("s", after=0)
        bus.attach("s", sub)
        for i in range(5):                     # 订阅队列容量 2 → 只留最新 2 帧
            bus.publish("s", "token", {"i": i})
        items = _drain(sub.queue)
        assert [f.seq for f in items] == [4, 5]
        # 丢的只是实时性：缓冲仍完整，可经 GET after=游标 补差找回
        assert bus.buffer_len("s") == 5
    asyncio.run(main())


def test_end_turn_sends_stream_end_and_ttl_cleans_channel():
    async def main():
        bus = ChatBus(ttl_seconds=0.05)
        bus.begin_turn("s")
        bus.publish("s", "complete", {})
        sub = bus.subscribe("s", after=0)
        bus.attach("s", sub)
        bus.end_turn("s")
        assert _drain(sub.queue) == [STREAM_END]     # 终态后唤醒订阅者收流
        assert bus.turn_active("s") is False
        await asyncio.sleep(0.15)
        assert bus.has_channel("s") is False         # TTL 清理
    asyncio.run(main())


def test_begin_turn_cancels_pending_ttl():
    async def main():
        bus = ChatBus(ttl_seconds=0.05)
        bus.begin_turn("s")
        bus.publish("s", "token", {})
        bus.end_turn("s")
        await asyncio.sleep(0.02)
        bus.begin_turn("s")                          # 新一轮接上：撤销 TTL
        await asyncio.sleep(0.15)
        assert bus.has_channel("s") is True
        assert bus.turn_active("s") is True
        bus.end_turn("s")
    asyncio.run(main())


def test_drop_session_closes_subscribers_and_idempotent():
    async def main():
        bus = ChatBus(ttl_seconds=60)
        bus.begin_turn("s")
        sub = bus.subscribe("s", after=0)
        bus.attach("s", sub)
        bus.drop_session("s")
        assert bus.has_channel("s") is False
        assert _drain(sub.queue) == [STREAM_END]
        bus.drop_session("s")                        # 幂等
    asyncio.run(main())


def test_clear_empties_all_and_cancels_timers():
    async def main():
        bus = ChatBus(ttl_seconds=0.05)
        for sid in ("a", "b"):
            bus.begin_turn(sid)
            bus.publish(sid, "complete", {})
            bus.end_turn(sid)
        assert bus.session_count == 2
        bus.clear()
        assert bus.session_count == 0
        await asyncio.sleep(0.15)                    # 定时器已撤销，无迟爆
        assert bus.session_count == 0
    asyncio.run(main())


def test_publish_threadsafe_bridge_from_raw_thread():
    """跨线程投递桥：裸线程生产者经 call_soon_threadsafe 转回循环线程。"""
    async def main():
        bus = ChatBus()
        bus.begin_turn("s")                          # 先在循环线程绑定
        sub = bus.subscribe("s", after=0)
        bus.attach("s", sub)

        def produce():
            for i in range(3):
                bus.publish_threadsafe("s", "token", {"i": i})

        t = threading.Thread(target=produce)
        t.start()
        await asyncio.sleep(0.2)
        t.join(5)
        assert bus.buffer_len("s") == 3
        assert [f.seq for f in _drain(sub.queue)] == [1, 2, 3]   # 单线程生产保序
    asyncio.run(main())


def test_missing_channel_subscribe_is_empty_not_live():
    async def main():
        bus = ChatBus()
        sub = bus.subscribe("ghost", after=0)
        assert sub.replay == [] and sub.live is False
        assert bus.turn_active("ghost") is False
    asyncio.run(main())


# ════════════════════════════════════════════════════════════════
# 二、HTTP 端点（TestClient，持久 portal——后台泵跨请求存活）
# ════════════════════════════════════════════════════════════════


class _FakeWorker:
    def __init__(self):
        self.cancels = 0
        self.streaming = False

    def request_cancel(self):
        self.cancels += 1


class _GatedWorker(_FakeWorker):
    """send_stream 可中途闸门阻塞：模拟长推理 + 断开后继续跑完。"""

    def __init__(self):
        super().__init__()
        self.gate = threading.Event()
        self.consumed_all = threading.Event()
        self.head = [
            {"type": "event", "event": "token", "data": {"content": "a"}},
            {"type": "event", "event": "token", "data": {"content": "b"}},
        ]
        self.tail = [
            {"type": "event", "event": "token", "data": {"content": "c"}},
            {"type": "result", "data": {"ok": True}},
        ]

    def send_stream(self, op, **kw):
        for e in self.head:
            yield e
        self.gate.wait(10)
        for e in self.tail:
            yield e
        self.consumed_all.set()


class _BusyWorker(_FakeWorker):
    def send_stream(self, op, **kw):
        yield {"type": "error", "busy": True,
               "message": "AI 正在思考（worker 忙），请稍后重试"}


class _FakeManager:
    def __init__(self):
        self.by_slot = {}
        self.routes = []

    def acquire_chat_slot(self, session_id):
        return (session_id or "").strip() or "main"

    def get_or_create(self, user_id=LOCAL_USER, slot="main"):
        self.routes.append(slot)
        return self.by_slot.setdefault(slot, _GatedWorker())

    def lookup(self, user_id, slot):
        return self.by_slot.get(slot)

    def cancel_for(self, session_id):
        return False


@pytest.fixture
def bus_app(tmp_path):
    # POST handler 现带"发送即持久化"预写（写会话 stub + 事件库）——
    # 必须隔离 data_root，否则测试 sid 污染真实 data/hermes.db
    # （GET 兜底会读到未闭合孤儿事件造成永假在途）
    from src.storage import paths
    paths.set_data_root(tmp_path)
    try:
        a = FastAPI()
        a.state.worker_manager = _FakeManager()
        a.include_router(chat_router.router, prefix="/api/chat")
        a.dependency_overrides[get_current_user_id] = lambda: LOCAL_USER
        yield a
    finally:
        paths.set_data_root(None)


def test_post_frames_carry_bus_ids_and_get_replays_then_done(bus_app):
    """POST 帧带总线 seq（id 行）；无在途时 GET 补差全量 + done 关流。"""
    with TestClient(bus_app) as c:
        w = bus_app.state.worker_manager.get_or_create(slot="rsm00001")
        w.gate.set()                                # 快速完成（无长推理）
        r = c.post("/api/chat/stream",
                   json={"message": "hi", "session_id": "rsm00001"})
        assert r.status_code == 200
        post_frames = _parse_sse(r.content)
        assert [(f[0], f[1]) for f in post_frames] == [
            (1, "token"), (2, "token"), (3, "token"), (4, "complete")]
        assert post_frames[-1][2] == {"ok": True}

        r2 = c.get("/api/chat/stream/rsm00001?after=0")
        assert r2.status_code == 200
        assert r2.headers["content-type"].startswith("text/event-stream")
        frames = _parse_sse(r2.content)
        # GET 帧（含 id）与 POST 完全同源同序，末尾 done（data: {}）
        assert [(f[0], f[1]) for f in frames] == [
            (1, "token"), (2, "token"), (3, "token"), (4, "complete"),
            (None, "done")]
        assert frames[-1][2] == {}


def test_get_after_cursor_and_last_event_id_header(bus_app):
    """after query 优先；缺省读 Last-Event-ID 头（EventSource 重连自动带）。"""
    with TestClient(bus_app) as c:
        w = bus_app.state.worker_manager.get_or_create(slot="rsm00002")
        w.gate.set()
        c.post("/api/chat/stream",
               json={"message": "hi", "session_id": "rsm00002"})
        # query after
        assert [f[0] for f in _parse_sse(
            c.get("/api/chat/stream/rsm00002?after=2").content)] == [3, 4, None]
        # Last-Event-ID 头
        assert [f[0] for f in _parse_sse(
            c.get("/api/chat/stream/rsm00002",
                  headers={"Last-Event-ID": "3"}).content)] == [4, None]
        # 两者同给：query 优先
        assert [f[0] for f in _parse_sse(
            c.get("/api/chat/stream/rsm00002?after=1",
                  headers={"Last-Event-ID": "3"}).content)] == [2, 3, 4, None]
        # 游标越过最新 seq：无补差，立即 done
        assert _parse_sse(
            c.get("/api/chat/stream/rsm00002?after=999").content) == [
            (None, "done", {})]


def test_get_without_any_activity_immediate_done(bus_app):
    """会话从未有过流：无补差帧，直接 done 收流。"""
    with TestClient(bus_app) as c:
        r = c.get("/api/chat/stream/ghost0001")
        assert r.status_code == 200
        assert _parse_sse(r.content) == [(None, "done", {})]


def test_get_rejects_unsafe_sid(bus_app):
    """sid 经 validate_id：含冒号（NTFS ADS / waker 前缀防穿越）→ 400。"""
    with TestClient(bus_app) as c:
        assert c.get("/api/chat/stream/a%3Ab").status_code == 400
        assert c.get("/api/chat/stream/a%2E%2Eb").status_code == 400


# ── 原生 ASGI 驱动 ──
# 本环境 starlette TestClient 会把整个响应在 portal.call 里物化完才返回
# （无增量流、无中途断连路径），流式三态/断开续收只能直连 ASGI 层驱动：
# 自带 receive/send，可逐块收集、可随时投 http.disconnect 模拟断开。


class _ASGIResponse:
    """一次 ASGI 请求的收集句柄（chunks 逐 body 块追加）。"""

    def __init__(self):
        self.status = None
        self.headers = {}
        self.chunks = []
        self.done = threading.Event()

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)


async def _drive_asgi(app, method: str, path: str, *, json_body=None,
                      headers=None, disconnect_after_chunks=None):
    """驱动一次 ASGI 请求，返回 (task, resp)。

    disconnect_after_chunks=N：收到第 N 个 body 块后向 app 投
    http.disconnect（模拟客户端中途断开，流式响应被取消收尾）。
    """
    from urllib.parse import unquote

    body = b""
    hdrs = {k.lower(): v for k, v in (headers or {}).items()}
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("content-type", "application/json")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": unquote(path.split("?", 1)[0]),
        "raw_path": path.split("?", 1)[0].encode("utf-8"),
        "query_string": (path.split("?", 1)[1].encode("utf-8")
                         if "?" in path else b""),
        "headers": [(k.encode(), v.encode()) for k, v in hdrs.items()],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
    }

    resp = _ASGIResponse()
    state = {"sent_request": False}
    disconnect = asyncio.Event()

    async def receive():
        if not state["sent_request"]:
            state["sent_request"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        if disconnect_after_chunks is not None:
            await disconnect.wait()
            return {"type": "http.disconnect"}
        await asyncio.Event().wait()   # 不主动断开：收流后由 app 侧 cancel
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message):
        if message["type"] == "http.response.start":
            resp.status = message["status"]
            resp.headers = {k.decode().lower(): v.decode()
                            for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            if message.get("body"):
                resp.chunks.append(message["body"])

    async def runner():
        try:
            await app(scope, receive, send)
        finally:
            resp.done.set()

    task = asyncio.create_task(runner())
    if disconnect_after_chunks is not None:
        async def cut():
            while len(resp.chunks) < disconnect_after_chunks:
                await asyncio.sleep(0.01)
            disconnect.set()
        asyncio.create_task(cut())
    return task, resp


async def _wait_chunks(resp: _ASGIResponse, n: int, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(resp.chunks) >= n:
            return True
        await asyncio.sleep(0.01)
    return False


def test_get_live_continuation_replay_then_realtime_then_done(bus_app):
    """三态一条流：补差（闸门前帧）→ 实时续推（闸门后帧）→ 终态后 done。"""

    async def scenario():
        fm = bus_app.state.worker_manager
        w = _GatedWorker()
        fm.by_slot["live0001"] = w

        post_task, post_resp = await _drive_asgi(
            bus_app, "POST", "/api/chat/stream",
            json_body={"message": "hi", "session_id": "live0001"})
        # 等泵把闸门前两帧写进总线（chat_bus 由首个请求懒建）
        bus = None
        deadline = time.time() + 5
        while time.time() < deadline:
            bus = getattr(bus_app.state, "chat_bus", None)
            if bus is not None and bus.buffer_len("live0001") >= 2:
                break
            await asyncio.sleep(0.01)
        assert bus is not None and bus.buffer_len("live0001") >= 2

        get_task, get_resp = await _drive_asgi(
            bus_app, "GET", "/api/chat/stream/live0001?after=0")
        # 补差帧已推到客户端，再放闸门（确保后续走的是实时续推通道）
        assert await _wait_chunks(get_resp, 2)
        w.gate.set()

        await asyncio.wait_for(post_task, 10)
        await asyncio.wait_for(get_task, 10)
        assert get_resp.headers.get("content-type", "").startswith(
            "text/event-stream")
        frames = _parse_sse(get_resp.body)
        assert [(f[0], f[1]) for f in frames] == [
            (1, "token"), (2, "token"),           # 补差
            (3, "token"), (4, "complete"),        # 实时续推 + 终态
            (None, "done")]                       # done 收流
        # POST 侧同源同序（共享 seq 空间；POST 无 done 收流帧，对应 GET 去掉 done）
        assert _parse_sse(post_resp.body) == frames[:-1]
        assert w.consumed_all.is_set()

    asyncio.run(scenario())


def test_post_client_disconnect_bus_still_receives_full_stream(bus_app):
    """消费解耦：客户端中途断开（真 http.disconnect）后，后台泵继续消费
    worker 流到自然结束，总线拿到完整流（断开 ≠ 取消：零 cancel）。"""

    async def scenario():
        fm = bus_app.state.worker_manager
        w = _GatedWorker()
        fm.by_slot["disc0001"] = w

        post_task, post_resp = await _drive_asgi(
            bus_app, "POST", "/api/chat/stream",
            json_body={"message": "hi", "session_id": "disc0001"},
            disconnect_after_chunks=2)             # 前两帧到手即断开
        assert await _wait_chunks(post_resp, 2)
        await asyncio.wait_for(post_task, 10)  # 流被取消收尾
        assert len(_parse_sse(post_resp.body)) == 2   # 只收到闸门前两帧

        w.gate.set()                               # 断开后放行
        assert await asyncio.to_thread(w.consumed_all.wait, 5)
        assert w.cancels == 0                      # 断开不触发安全网取消

        # 总线拿到完整流：GET 补差可见全部 4 帧 + done
        # （等待预算 30s：预写 turn/start 未闭合时订阅端点按在途等待，
        #  见 chat_stream_subscribe 的 spawn 窗口兜底——预算 30s 收流）
        get_task, get_resp = await _drive_asgi(
            bus_app, "GET", "/api/chat/stream/disc0001?after=0")
        await asyncio.wait_for(get_task, 35)
        frames = _parse_sse(get_resp.body)
        assert [(f[0], f[1]) for f in frames] == [
            (1, "token"), (2, "token"), (3, "token"), (4, "complete"),
            (None, "done")]

    asyncio.run(scenario())


def test_post_busy_error_private_not_on_bus_and_no_cancel(bus_app):
    """busy 回归：busy 是请求私有错误（无 id、不进总线、不误取消）；
    GET 补差看不到陈旧 busy。"""
    with TestClient(bus_app) as c:
        fm = bus_app.state.worker_manager
        w = _BusyWorker()
        fm.by_slot["busys001"] = w
        r = c.post("/api/chat/stream",
                   json={"message": "hi", "session_id": "busys001"})
        assert r.status_code == 200
        frames = _parse_sse(r.content)
        assert len(frames) == 1
        assert frames[0][1] == "error"
        assert frames[0][0] is None                 # busy 帧无总线 seq
        assert frames[0][2]["busy"] is True
        assert "请稍后重试" in frames[0][2]["message"]
        assert w.cancels == 0
        # 总线无 busy 帧：GET 无补差，立即 done
        assert _parse_sse(
            c.get("/api/chat/stream/busys001").content) == [(None, "done", {})]


def test_get_two_subscribers_independent_cursors(bus_app):
    """同一会话两个 GET 订阅者：各自游标各自的补差结果。"""
    with TestClient(bus_app) as c:
        fm = bus_app.state.worker_manager
        w = _GatedWorker()
        fm.by_slot["twos0001"] = w
        w.gate.set()
        c.post("/api/chat/stream",
               json={"message": "hi", "session_id": "twos0001"})
        results = {}

        def read(after, key):
            results[key] = _parse_sse(
                c.get(f"/api/chat/stream/twos0001?after={after}").content)

        t = threading.Thread(target=read, args=(2, "b"))
        t.start()
        read(0, "a")
        t.join(10)
        assert [f[0] for f in results["a"]] == [1, 2, 3, 4, None]
        assert [f[0] for f in results["b"]] == [3, 4, None]


def test_approve_shares_seq_space_with_chat(bus_app):
    """approve 走同一泵/总线：帧带 id，且与 chat 共享会话 seq 空间。"""
    with TestClient(bus_app) as c:
        fm = bus_app.state.worker_manager
        w = _GatedWorker()
        fm.by_slot["appr0001"] = w
        w.gate.set()
        r = c.post("/api/chat/approve",
                   json={"thread_id": "appr0001", "decision": "approve"})
        assert r.status_code == 200
        frames = _parse_sse(r.content)
        assert [f[1] for f in frames][-1] == "complete"
        assert all(f[0] is not None for f in frames)
        # GET 从中段游标续读同一 seq 空间
        assert [f[0] for f in _parse_sse(
            c.get("/api/chat/stream/appr0001?after=2").content)][0] == 3


def test_get_probe_live_then_reprobe_idle_closes(bus_app, monkeypatch):
    """在途信号来自 /active 探测（总线无记录）时也进实时态；
    心跳间隔后复核（总线+探测都转闲）→ done 收流（防订阅者永挂）。"""
    monkeypatch.setattr(chat_router, "KEEPALIVE_SECONDS", 0.2)
    with TestClient(bus_app) as c:
        fm = bus_app.state.worker_manager
        w = _GatedWorker()
        fm.by_slot["prob0001"] = w
        w.streaming = True                          # 幽灵在途：仅探测为真
        threading.Timer(0.5, setattr, args=(w, "streaming", False)).start()
        with c.stream("GET", "/api/chat/stream/prob0001") as r:
            buf = b"".join(r.iter_raw())
        assert b": keepalive" in buf                # 心跳 comment 到场
        assert _parse_sse(buf) == [(None, "done", {})]   # 复核转闲即收流
