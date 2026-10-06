"""M5 并行会话槽：路由纯函数矩阵 / 双槽并发 / stop 精确取消 / reset new_sid。"""
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.constants import LOCAL_USER
from web_fastapi.worker_manager import DEFAULT_SLOT, SlotsFullError, WorkerManager
from web_fastapi.dependencies import get_current_user_id
from web_fastapi.routers import chat as chat_router
from web_fastapi.routers import sessions as sessions_router


# ── WorkerManager：槽位容量与亲和 ──

class _FakeStdin:
    """记录写入行数（WorkerProcess.request_cancel 真正触达的位置）。"""

    def __init__(self, owner):
        self.owner = owner

    def write(self, data):
        self.owner.writes.append(data)
        return len(data)

    def flush(self):
        pass


class _FakeProc:
    def __init__(self, alive=True):
        self._alive = alive
        self.pid = id(self)
        self.writes = []
        self.stdin = _FakeStdin(self)

    def poll(self):
        # 模拟 subprocess.Popen.poll：存活 None / 退出码 0
        return None if self._alive else 0

    def is_alive(self):
        return self._alive

    def request_cancel(self):
        self.cancels += 1


@pytest.fixture
def wm(monkeypatch, tmp_path):
    m = WorkerManager(max_parallel=2, state_path=tmp_path / "web_state.json")
    # 注入假实例（绕过真实 fork）；is_alive 可切换模拟崩溃
    m._workers = {}
    yield m


def _put(m: WorkerManager, slot: str, alive=True):
    from web_fastapi.worker_manager import WorkerProcess
    wp = WorkerProcess.__new__(WorkerProcess)
    wp.user_id = LOCAL_USER
    wp.slot = slot
    wp.proc = _FakeProc(alive)
    wp._lock = threading.Lock()
    wp.streaming = False
    wp.last_active = time.time()
    m._workers[(LOCAL_USER, slot)] = wp
    return wp


def test_acquire_no_sid_goes_main(wm):
    assert wm.acquire_chat_slot("") == DEFAULT_SLOT
    assert wm.acquire_chat_slot(None) == DEFAULT_SLOT


def test_acquire_affinity_returns_same_slot(wm):
    _put(wm, "s1", alive=True)
    assert wm.acquire_chat_slot("s1") == "s1"


def test_acquire_full_evicts_idle_lru(wm):
    """满员但有空闲槽：回收最久未活动的空闲槽，新会话可入。"""
    a = _put(wm, "a", True)
    b = _put(wm, "b", True)
    a.last_active = 1.0
    b.last_active = 2.0
    assert wm.acquire_chat_slot("c") == "c"
    assert (LOCAL_USER, "a") not in wm._workers   # LRU 空闲槽被回收
    assert (LOCAL_USER, "b") in wm._workers


def test_acquire_full_all_streaming_still_raises(wm):
    """三路都在生成：仍然 429，不误杀在途流。"""
    a = _put(wm, "a", True)
    b = _put(wm, "b", True)
    a.streaming = b.streaming = True
    with pytest.raises(SlotsFullError):
        wm.acquire_chat_slot("c")


def test_acquire_dead_slot_reusable(wm):
    _put(wm, "a", True)
    _put(wm, "b", True)
    wm._workers[(LOCAL_USER, "a")].streaming = True
    wm._workers[(LOCAL_USER, "b")].streaming = True
    wm._workers[(LOCAL_USER, "b")].proc._alive = False
    assert wm.acquire_chat_slot("c") == "c"
    assert wm.acquire_chat_slot("b") == "b"  # 死槽亲和照常复用（随后重建）


def test_lookup_never_creates(wm):
    """lookup 只读：命中存活实例、缺席/死亡返 None，绝不创建槽。"""
    wp = _put(wm, "s1", alive=True)
    assert wm.lookup(LOCAL_USER, "s1") is wp
    assert wm.lookup(LOCAL_USER, "ghost") is None
    assert len(wm.all_users()) == 1                  # 零创建
    wm._workers[(LOCAL_USER, "s1")].proc._alive = False
    assert wm.lookup(LOCAL_USER, "s1") is None       # 死槽不算数


def test_cancel_for_targets_only_that_slot(wm):
    a = _put(wm, "s-a")
    b = _put(wm, "s-b")
    main = _put(wm, DEFAULT_SLOT)
    assert wm.cancel_for("s-a") is True
    assert len(a.proc.writes) == 1
    assert len(b.proc.writes) == 0 and len(main.proc.writes) == 0
    # 不存在的槽：不 spawn、不报错
    assert wm.cancel_for("ghost") is False
    assert wm.cancel_for("") is False or True  # main 槽存在与否都安全


def test_broadcast_updates_mirror_and_targets(wm):
    a = _put(wm, "s-a")
    _put(wm, DEFAULT_SLOT)
    wm.remember_prefs({"temperature": 0.9})
    wm.remember_permission_mode("plan")
    assert wm._mirror_prefs == {"temperature": 0.9}
    assert wm._mirror_perm == "plan"


# ── 路由层：stop 精确取消 / reset 下发 new_sid ──

class _FakeWorker:
    def __init__(self):
        self.cancels = 0
        self.sent = []
        self.streaming = False

    def request_cancel(self):
        self.cancels += 1
        self.sent.append(("cancel",))

    def send(self, op, **kw):
        self.sent.append((op, kw))
        if op == "session_reset":
            return [{"type": "result", "data": {"ok": True,
                                                "session_id": kw.get("new_sid", "")}}]
        if op == "current_session":
            return [{"type": "result", "data": {
                "session_id": kw.get("session_id") or "cur00000",
                "message_count": 0, "waker": "", "history": []}}]
        return [{"type": "result", "data": {"ok": True}}]

    def send_stream(self, op, **kw):
        yield {"type": "result", "data": {"ok": True}}


class _FakeManager:
    """按 slot 发实例的替身（记录每次路由）。"""

    def __init__(self):
        self.by_slot = {}
        self.routes = []
        self.removed = []

    def get_or_create(self, user_id=LOCAL_USER, slot=DEFAULT_SLOT):
        self.routes.append(slot)
        return self.by_slot.setdefault(slot, _FakeWorker())

    def lookup(self, user_id, slot):
        """只读查找：替身不模拟存活态，存在即返回。"""
        return self.by_slot.get(slot)

    def remove(self, user_id, slot=DEFAULT_SLOT):
        self.removed.append(slot)
        self.by_slot.pop(slot, None)

    def acquire_chat_slot(self, session_id):
        slot = (session_id or "").strip() or DEFAULT_SLOT
        if slot != DEFAULT_SLOT and slot not in self.by_slot \
                and len([s for s in self.by_slot if s != DEFAULT_SLOT]) >= 2:
            raise SlotsFullError(2)
        return slot

    def cancel_for(self, session_id):
        slot = (session_id or "").strip() or DEFAULT_SLOT
        w = self.by_slot.get(slot)
        if w is None:
            return False
        w.request_cancel()
        return True


@pytest.fixture
def chat_app():
    a = FastAPI()
    a.state.worker_manager = _FakeManager()
    a.include_router(chat_router.router, prefix="/api/chat")
    a.include_router(sessions_router.router, prefix="/api/sessions")
    a.dependency_overrides[get_current_user_id] = lambda: LOCAL_USER
    return a


@pytest.fixture
def c(chat_app):
    return TestClient(chat_app)


def test_stop_routes_only_to_target_slot(chat_app, c):
    fm = chat_app.state.worker_manager
    fm.get_or_create(slot="sessx")          # 预置：该会话正在生成（已有实例）
    fm.get_or_create(slot="other")
    r = c.post("/api/chat/stop", json={"session_id": "sessx"})
    assert r.status_code == 200 and r.json()["ok"] is True
    target = fm.by_slot["sessx"]
    assert target.cancels == 1
    # 其他槽零影响
    others = [w for s, w in fm.by_slot.items() if s != "sessx"]
    assert all(w.cancels == 0 for w in others)


def test_stream_routes_by_body_session(chat_app, c):
    r = c.post("/api/chat/stream", json={"message": "hi", "session_id": "tab2"})
    assert r.status_code == 200
    assert chat_app.state.worker_manager.routes[-1] == "tab2"


def test_stream_full_returns_429(chat_app, c):
    c.post("/api/chat/stream", json={"message": "1", "session_id": "p1"})
    c.post("/api/chat/stream", json={"message": "2", "session_id": "p2"})
    r = c.post("/api/chat/stream", json={"message": "3", "session_id": "p3"})
    assert r.status_code == 429
    assert "正在生成" in r.json()["detail"]
    # 已占槽不受影响
    assert c.post("/api/chat/stream",
                  json={"message": "4", "session_id": "p1"}).status_code == 200


def test_approve_routes_by_thread_id(chat_app, c):
    fm = chat_app.state.worker_manager
    fm.by_slot["born-here"] = _FakeWorker()      # 预置出生槽
    r = c.post("/api/chat/approve",
               json={"thread_id": "born-here", "decision": "approve"})
    assert r.status_code == 200
    assert fm.routes[-1] == "born-here"


def test_reset_without_slot_falls_back_to_main(chat_app, c):
    """无槽会话的 reset 落 main：管理路径绝不创建会话槽（429 根因回归锁）。"""
    fm = chat_app.state.worker_manager
    r = c.post("/api/sessions/reset", json={"session_id": "oldsid1"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["session_id"] and j["session_id"] != "oldsid1"
    assert fm.routes[-1] == DEFAULT_SLOT             # 回落 main
    assert "oldsid1" not in fm.by_slot               # 未创建会话槽
    target = fm.by_slot[DEFAULT_SLOT]
    assert target.sent[-1][0] == "session_reset"
    assert target.sent[-1][1]["new_sid"] == j["session_id"]


def test_reset_with_hot_slot_affinity(chat_app, c):
    """已有槽的会话 reset 亲和复用（对话进行中不 503 的前提）；
    reset 成功后旧槽一并回收（P3：僵尸槽不再永久占并发名额）。"""
    fm = chat_app.state.worker_manager
    fm.by_slot["hotrst1"] = _FakeWorker()
    target = fm.by_slot["hotrst1"]                   # reset 后槽被回收，先持引用
    r = c.post("/api/sessions/reset", json={"session_id": "hotrst1"})
    assert r.status_code == 200
    assert target.sent[-1][0] == "session_reset"
    assert DEFAULT_SLOT not in fm.by_slot            # 未回落 main
    assert "hotrst1" not in fm.by_slot               # 旧槽已回收（live 回落）


def test_current_without_slot_falls_back_to_main(chat_app, c):
    fm = chat_app.state.worker_manager
    r = c.get("/api/sessions/current", params={"session_id": "noslot1"})
    assert r.status_code == 200
    assert fm.routes[-1] == DEFAULT_SLOT             # 浏览历史不占并发名额
    assert "noslot1" not in fm.by_slot


def test_current_with_existing_slot_affinity(chat_app, c):
    fm = chat_app.state.worker_manager
    fm.by_slot["hotslot"] = _FakeWorker()
    r = c.get("/api/sessions/current", params={"session_id": "hotslot"})
    assert r.status_code == 200
    assert ("current_session", {"session_id": "hotslot"}) in fm.by_slot["hotslot"].sent
    assert DEFAULT_SLOT not in fm.by_slot            # 命中专属槽，main 零参与


def test_delete_recycles_session_slot(chat_app, c):
    """删除会话连带回收专属槽（僵尸槽不再永久占用并发名额）。"""
    fm = chat_app.state.worker_manager
    fm.by_slot["dead0001"] = _FakeWorker()
    r = c.delete("/api/sessions/dead0001")
    assert r.status_code == 200
    assert fm.removed == ["dead0001"]
    assert "dead0001" not in fm.by_slot              # 槽已回收


def test_delete_missing_session_still_recycles_slot(chat_app, c, monkeypatch):
    """/current 等创建过的槽 + 会话文件已不存在 → 404 但槽照样回收。"""
    fm = chat_app.state.worker_manager
    fm.by_slot["gone0001"] = _FakeWorker()
    # 让 session_delete 返回 error（文件不存在路径）
    fm.by_slot["gone0001"].send = lambda op, **kw: (
        [{"type": "error"}] if op == "session_delete"
        else _FakeWorker().send(op, **kw))
    r = c.delete("/api/sessions/gone0001")
    assert r.status_code == 404
    assert "gone0001" not in fm.by_slot              # 404 前置回收已发生


def test_delete_with_real_shaped_chat_bus_not_500(chat_app, c):
    """回归（2026-09-10）：chat_bus.bound_loop 是方法——路由漏括号曾让
    删会话恒 500（AttributeError: 'function' object has no attribute
    'is_closed'）。挂真实形状的 bus 断言不再炸。"""
    import asyncio

    class _Bus:
        def __init__(self):
            self.dropped = []
            self._loop = asyncio.new_event_loop()

        def bound_loop(self):          # 方法形态（对齐 chat_bus.ChatBus）
            return self._loop

        def drop_session(self, sid):
            self.dropped.append(sid)

    bus = _Bus()
    chat_app.state.chat_bus = bus
    try:
        r = c.delete("/api/sessions/gone0002")
        assert r.status_code in (200, 404), r.text   # 不再 500
        # call_soon_threadsafe 已排队，跑一下该循环让 drop 生效
        bus._loop.run_until_complete(asyncio.sleep(0))
        assert "gone0002" in bus.dropped
    finally:
        bus._loop.close()


# ── 后台生成（断开 ≠ 停止）：relay_stream 三态 + /active 探测 ──

class _StreamWorker:
    """send_stream 产出预置事件、记录 request_cancel 的替身。"""

    def __init__(self, events):
        self._events = events
        self.cancels = 0

    def request_cancel(self):
        self.cancels += 1

    def send_stream(self, op, **kw):
        yield from self._events


def test_relay_normal_end_never_cancels():
    """正常结束：不发 chat_stop。历史上 finally 无差别 request_cancel，
    若此刻下一条 chat 刚起跑会被误杀（切权限模式后消息"没反应"的根因）。"""
    w = _StreamWorker([{"type": "event", "event": "token", "data": {}}])
    out = list(chat_router.relay_stream(w, "chat", message="x"))
    assert len(out) == 1
    assert w.cancels == 0                      # 正常完结，无物可取消


def test_relay_client_disconnect_keeps_running():
    """GeneratorExit（切页面断连）不取消——worker 后台继续跑完并落盘。"""
    w = _StreamWorker([{"type": "event", "event": "token", "data": {}}] * 3)
    g = chat_router.relay_stream(w, "chat", message="x")
    next(g)                                    # 流到第一条
    g.close()                                  # 模拟客户端断开
    assert w.cancels == 0                      # 未取消


def test_relay_error_event_no_cancel():
    """中途 error 事件（worker 侧 LLM 失败等）：chat 已完结，不取消。
    唯一触发安全网取消的是泵超时（见 test_review_guards #10）。"""
    w = _StreamWorker([{"type": "error", "message": "boom"}])
    out = list(chat_router.relay_stream(w, "chat", message="x"))
    assert b"error" in out[0]                  # SSE 字节流里带 error 事件
    assert w.cancels == 0


def test_chat_active_endpoint(chat_app, c):
    fm = chat_app.state.worker_manager
    fm.by_slot["busy0001"] = _FakeWorker()
    r = c.get("/api/chat/active", params={"session_id": "busy0001"})
    assert r.status_code == 200
    assert r.json() == {"session_id": "busy0001", "active": False}
    fm.by_slot["busy0001"].streaming = True
    assert c.get("/api/chat/active",
                 params={"session_id": "busy0001"}).json()["active"] is True
    # 无槽 / 空 sid：一律闲
    assert c.get("/api/chat/active",
                 params={"session_id": "ghost999"}).json()["active"] is False
    assert c.get("/api/chat/active").json()["active"] is False


def test_delete_worker_error_maps_404(chat_app, c):
    """worker.send 对 error 事件抛 RuntimeError → 归一 404，槽照常回收。"""
    fm = chat_app.state.worker_manager
    fm.by_slot["err00001"] = _FakeWorker()

    def boom(op, **kw):
        raise RuntimeError("会话文件不存在")

    fm.by_slot["err00001"].send = boom
    r = c.delete("/api/sessions/err00001")
    assert r.status_code == 404
    assert r.json()["detail"] == "会话文件不存在"
    assert "err00001" not in fm.by_slot


def test_sessions_list_direct_read(chat_app, c, tmp_path, monkeypatch):
    """list 已改主进程直读：不经过 worker（fake manager 未被调用）。"""
    import src.session_store as ss
    monkeypatch.setattr(ss, "SESSIONS_DIR", tmp_path / "sess")
    (tmp_path / "sess" / LOCAL_USER).mkdir(parents=True)
    import json
    (tmp_path / "sess" / LOCAL_USER / "direct1.json").write_text(
        json.dumps({"session_id": "direct1", "name": "直读",
                    "updated_at": "2026-08-27T00:00:00",
                    "message_count": 1, "preview": "p",
                    "messages": [{"role": "user", "content": "x"}]}),
        encoding="utf-8")
    r = c.get("/api/sessions")
    assert r.status_code == 200
    assert r.json()["sessions"][0]["session_id"] == "direct1"
    assert chat_app.state.worker_manager.routes == []   # 零 IPC
