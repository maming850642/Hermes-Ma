"""P2-11 / P2-13 / P2-14 / P2-15 回归：事件循环冻结与槽路由。

- P2-11：worker.send 同步抢 per-worker 锁（lock_wait 最长 5s），直接调它的
  handler 必须是同步 def（FastAPI 丢线程池执行）；async handler 里调 =
  冻结整个 uvicorn 事件循环（主槽 chat 流式持锁期间全站停摆）。
- P2-13：get_or_create 首次触发 spawn + ready 等待（Windows 十秒级），
  必须在线程池执行（同步 def 路由或 run_in_threadpool）。
- P2-14：/api/compact 与 PUT /api/config/waker 带 session_id 时按 chat
  闸门亲和路由到该会话的槽，不再错打 main。
- P2-15：approve 槽满 → 429（对齐 chat_stream）。

判定手法：worker.send / get_or_create 执行时若当前线程存在运行中的事件
循环（asyncio.get_running_loop 成功），说明调用发生在 async handler 内
（冻结事件循环）；线程池内执行时必无运行循环。
"""
import asyncio
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.constants import LOCAL_USER
from web_fastapi.dependencies import get_current_user_id
from web_fastapi.routers import chat as chat_router
from web_fastapi.routers import config_router
from web_fastapi.routers import sessions as sessions_router
from web_fastapi.routers import system as system_router
from web_fastapi.worker_manager import DEFAULT_SLOT, SlotsFullError


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class _FakeWorker:
    def __init__(self):
        self.sent = []
        self.fired = []
        self.streaming = False
        self.send_on_loop = None

    def send(self, op, **kw):
        self.sent.append((op, kw))
        self.send_on_loop = _on_event_loop()
        return [{"type": "result", "data": {"ok": True}}]

    def send_fire_and_forget(self, op, **kw):
        self.fired.append((op, kw))
        return True

    def send_stream(self, op, **kw):
        yield {"type": "result", "data": {"ok": True}}


class _FakeManager:
    def __init__(self, cap=2):
        self.by_slot = {}
        self.routes = []
        self.cap = cap
        self.spawn_on_loop = None

    def acquire_chat_slot(self, session_id):
        slot = (session_id or "").strip() or DEFAULT_SLOT
        if slot != DEFAULT_SLOT and slot not in self.by_slot \
                and len([s for s in self.by_slot if s != DEFAULT_SLOT]) >= self.cap:
            raise SlotsFullError(self.cap)
        return slot

    def get_or_create(self, user_id=LOCAL_USER, slot=DEFAULT_SLOT):
        self.routes.append(slot)
        if self.spawn_on_loop is None:
            self.spawn_on_loop = _on_event_loop()
        return self.by_slot.setdefault(slot, _FakeWorker())

    def lookup(self, user_id, slot):
        return self.by_slot.get(slot)

    def remove(self, user_id, slot=DEFAULT_SLOT):
        self.by_slot.pop(slot, None)

    def cancel_for(self, session_id):
        return False


def _make_app(*router_prefix_pairs, cap=2):
    a = FastAPI()
    a.state.worker_manager = _FakeManager(cap=cap)
    for router, prefix in router_prefix_pairs:
        a.include_router(router, prefix=prefix)
    a.dependency_overrides[get_current_user_id] = lambda: LOCAL_USER
    return a


# ── P2-11：worker.send 不在事件循环内执行 ──

def test_system_handlers_send_off_event_loop():
    app = _make_app((system_router.router, "/api"))
    c = TestClient(app)
    assert c.get("/api/tools").status_code == 200
    assert c.get("/api/skills").status_code == 200
    assert c.get("/api/health").status_code == 200
    m = app.state.worker_manager
    assert m.routes == [DEFAULT_SLOT, DEFAULT_SLOT, DEFAULT_SLOT]
    for w in m.by_slot.values():
        assert w.send_on_loop is False


def test_sessions_handlers_send_off_event_loop():
    app = _make_app((sessions_router.router, "/api/sessions"))
    c = TestClient(app)
    assert c.get("/api/sessions/current").status_code == 200
    assert c.post("/api/sessions/save").status_code == 200
    assert c.post("/api/sessions/reset", json={"session_id": ""}).status_code == 200
    assert c.patch("/api/sessions/renm0001/rename",
                   json={"name": "新名"}).status_code == 200
    assert c.post("/api/sessions/loadd001/load").status_code == 200
    assert c.post("/api/sessions/summ0001/summary").status_code == 200
    for w in app.state.worker_manager.by_slot.values():
        assert w.send_on_loop is False


def test_main_slot_send_does_not_freeze_sibling_request():
    """行为级回归：主槽 worker.send 阻塞期间，其余请求不被冻结。

    在共享 portal（单一事件循环，等价 uvicorn）内：sync def 路由进
    线程池，慢 send 只占一个池线程；若回归成 async handler 同步调
    worker.send，探针请求会被冻结到慢 send 结束。
    """
    app = _make_app((system_router.router, "/api"),
                    (chat_router.router, "/api/chat"))
    m = app.state.worker_manager

    slow = _FakeWorker()
    entered = threading.Event()

    def slow_send(op, **kw):
        entered.set()
        time.sleep(2.0)  # 模拟主槽持锁（chat 流式）
        return [{"type": "result", "data": {"ok": True}}]

    slow.send = slow_send
    m.by_slot[DEFAULT_SLOT] = slow

    with TestClient(app) as c:
        t = threading.Thread(target=lambda: c.get("/api/tools"))
        t.start()
        assert entered.wait(5.0)

        t0 = time.monotonic()
        r = c.get("/api/chat/active", params={"session_id": "zzz"})
        probe_elapsed = time.monotonic() - t0
        assert r.status_code == 200
        assert probe_elapsed < 1.0, (
            f"事件循环被 worker.send 冻结 {probe_elapsed:.2f}s（P2-11 回归）"
        )
    t.join(10.0)
    assert not t.is_alive()


# ── P2-13：spawn 不在事件循环内执行 ──

def test_chat_stream_spawn_in_threadpool():
    app = _make_app((chat_router.router, "/api/chat"))
    c = TestClient(app)
    r = c.post("/api/chat/stream", json={"message": "hi", "session_id": "spw0001"})
    assert r.status_code == 200
    m = app.state.worker_manager
    assert m.routes == ["spw0001"]
    assert m.spawn_on_loop is False, "get_or_create（spawn）在事件循环内执行"


def test_sessions_current_spawn_in_threadpool():
    """sessions._worker_for 兜底 main 槽 spawn 同样不占事件循环
    （sessions 路由整体为同步 def，handler 连同 spawn 都在线程池）。"""
    app = _make_app((sessions_router.router, "/api/sessions"))
    c = TestClient(app)
    r = c.get("/api/sessions/current", params={"session_id": ""})
    assert r.status_code == 200
    m = app.state.worker_manager
    assert m.routes == [DEFAULT_SLOT]
    assert m.spawn_on_loop is False


# ── P2-14：compact / waker 按会话亲和路由 ──

def test_compact_with_session_routes_to_slot():
    app = _make_app((system_router.router, "/api"))
    c = TestClient(app)
    r = c.post("/api/compact", params={"session_id": "cmpsess1"})
    assert r.status_code == 200, r.text
    m = app.state.worker_manager
    assert m.routes[-1] == "cmpsess1"          # chat 闸门亲和（acquire+get_or_create）
    assert m.by_slot["cmpsess1"].sent == [("compact", {"session_id": "cmpsess1"})]


def test_compact_without_session_falls_back_main():
    app = _make_app((system_router.router, "/api"))
    c = TestClient(app)
    r = c.post("/api/compact")
    assert r.status_code == 200
    m = app.state.worker_manager
    assert m.routes[-1] == DEFAULT_SLOT        # 无 sid 维持 main 旧行为
    assert m.by_slot[DEFAULT_SLOT].sent == [("compact", {"session_id": ""})]


def test_compact_slots_full_returns_429():
    app = _make_app((system_router.router, "/api"), cap=2)
    m = app.state.worker_manager
    m.by_slot["fullaa1"] = _FakeWorker()
    m.by_slot["fullbb2"] = _FakeWorker()
    c = TestClient(app)
    r = c.post("/api/compact", params={"session_id": "fullcc3"})
    assert r.status_code == 429
    assert "正在生成" in r.json()["detail"]


def test_waker_with_session_routes_to_slot(monkeypatch):
    # 已落盘会话走专属槽；本测试关注路由，打桩存在性
    monkeypatch.setattr(config_router, "_session_exists",
                        lambda request, sid: True)
    app = _make_app((config_router.router, "/api/config"))
    c = TestClient(app)
    r = c.put("/api/config/waker",
              json={"name": "阿黄", "session_id": "wkrsess1"})
    assert r.status_code == 200, r.text
    m = app.state.worker_manager
    assert m.routes[-1] == "wkrsess1"
    assert m.by_slot["wkrsess1"].fired == [
        ("waker_set", {"name": "阿黄", "session_id": "wkrsess1"})]


def test_waker_unknown_session_uses_main_no_404():
    """草稿/未知 sid：不 404、不 spawn 专属槽，落到 main 仍把 waker_set 打出去。"""
    app = _make_app((config_router.router, "/api/config"))
    c = TestClient(app)
    r = c.put("/api/config/waker",
              json={"name": "阿黄", "session_id": "ghost-draft"})
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True
    m = app.state.worker_manager
    assert m.routes[-1] == DEFAULT_SLOT
    assert "ghost-draft" not in m.by_slot
    assert m.by_slot[DEFAULT_SLOT].fired == [
        ("waker_set", {"name": "阿黄", "session_id": "ghost-draft"})]


def test_waker_without_session_keeps_main():
    app = _make_app((config_router.router, "/api/config"))
    c = TestClient(app)
    r = c.put("/api/config/waker", json={"name": ""})
    assert r.status_code == 200
    m = app.state.worker_manager
    assert m.routes[-1] == DEFAULT_SLOT
    assert m.by_slot[DEFAULT_SLOT].fired == [
        ("waker_set", {"name": "", "session_id": ""})]


def test_waker_slots_full_returns_429(monkeypatch):
    # 已落盘会话才走专属槽；打桩存在性，聚焦槽满 429 语义
    monkeypatch.setattr(config_router, "_session_exists",
                        lambda request, sid: True)
    app = _make_app((config_router.router, "/api/config"), cap=2)
    m = app.state.worker_manager
    m.by_slot["fullaa1"] = _FakeWorker()
    m.by_slot["fullbb2"] = _FakeWorker()
    c = TestClient(app)
    r = c.put("/api/config/waker",
              json={"name": "阿黄", "session_id": "fullcc3"})
    assert r.status_code == 429


# ── GET waker：session_id 透传进 waker_get op（按桶取值） ──

def test_waker_get_with_session_passes_session_id():
    """带 session_id：亲和路由 + op 携带 session_id（worker 侧按桶返回）。"""
    app = _make_app((config_router.router, "/api/config"))
    c = TestClient(app)
    r = c.get("/api/config/waker", params={"session_id": "wkrget01"})
    assert r.status_code == 200, r.text
    m = app.state.worker_manager
    assert m.routes[-1] == "wkrget01"
    assert m.by_slot["wkrget01"].sent == [("waker_get", {"session_id": "wkrget01"})]


def test_waker_get_without_session_keeps_main_op():
    """无参：main 槽旧行为，op 带空 session_id（worker 读 current 桶）。"""
    app = _make_app((config_router.router, "/api/config"))
    c = TestClient(app)
    r = c.get("/api/config/waker")
    assert r.status_code == 200
    m = app.state.worker_manager
    assert m.routes[-1] == DEFAULT_SLOT
    assert m.by_slot[DEFAULT_SLOT].sent == [("waker_get", {"session_id": ""})]


# ── P2-15：approve 槽满 → 429 ──

def test_approve_slots_full_returns_429():
    app = _make_app((chat_router.router, "/api/chat"), cap=2)
    m = app.state.worker_manager
    c = TestClient(app)
    c.post("/api/chat/stream", json={"message": "1", "session_id": "pa"})
    c.post("/api/chat/stream", json={"message": "2", "session_id": "pb"})
    r = c.post("/api/chat/approve",
               json={"thread_id": "pc", "decision": "approve"})
    assert r.status_code == 429
    assert "正在生成" in r.json()["detail"]
    # 已占槽的 approve 不受影响
    r2 = c.post("/api/chat/approve",
                json={"thread_id": "pa", "decision": "approve"})
    assert r2.status_code == 200
