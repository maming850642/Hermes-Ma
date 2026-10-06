"""发送即持久化（prelog）测试：

POST /api/chat/stream 在 spawn 前预写 turn/start + user/message + stub，
agent 侧 prelogged_turn 跳过重写；订阅端点对未闭合 turn/start 按在途等待。
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.storage import paths


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    paths.set_data_root(tmp_path)
    # SESSIONS_DIR 是模块级常量（不遵守 set_data_root），不打桩会读写本机
    # 真实 data/sessions/——sess01 恰好撞上真实会话时断言随开发者环境漂移。
    monkeypatch.setattr("src.session_store.SESSIONS_DIR", tmp_path / "sessions")
    yield tmp_path
    paths.set_data_root(None)


def _make_app_with_worker(monkeypatch, worker_events):
    """精简 app：挂 chat router + 假 worker manager（不 spawn 真进程）。"""
    from web_fastapi.routers import chat as chat_router

    app = FastAPI()
    app.include_router(chat_router.router, prefix="/api/chat", tags=["chat"])

    class FakeWP:
        streaming = False
        def send_stream(self, op, **kw):
            for e in worker_events:
                yield e
        def request_cancel(self):
            pass

    class FakeWM:
        def __init__(self):
            self.wp = FakeWP()
        def lookup(self, *a, **kw):
            # 在途探测：streaming=True 时 handler 应视作 busy 短路预写
            return self.wp if self.wp.streaming else None
        def acquire_chat_slot(self, session_id):
            return session_id or "main"
        def get_or_create(self, *a, **kw):
            return self.wp
    app.state.worker_manager = FakeWM()
    return app, app.state.worker_manager


def test_post_prelogs_turn_events_and_stub(tmp_path, monkeypatch):
    """POST（worker 空闲）→ spawn 前事件库已有 turn/start+user/message + stub。"""
    app, wm = _make_app_with_worker(monkeypatch, [{"type": "done"}])
    captured = {}

    # 拦截 _worker_for：在"spawn"时点检查事件库与 stub 是否已预写
    import web_fastapi.routers.chat as cr
    def fake_worker_for(request, session_id):
        from src.agent.session_log import SessionLog
        log = SessionLog()
        evs = log.events("sess01")
        captured["types_at_spawn"] = [e["type"] for e in evs]
        from src.session_store import read_session_meta
        captured["stub"] = read_session_meta("local", "sess01")
        return wm.get_or_create("local", slot="sess01")
    monkeypatch.setattr(cr, "_worker_for", fake_worker_for)

    with TestClient(app) as c:
        r = c.post("/api/chat/stream",
                   json={"message": "你好", "session_id": "sess01"})
        assert r.status_code == 200

    assert captured["types_at_spawn"] == ["turn/start", "user/message"]
    assert captured["stub"] is not None
    assert captured["stub"]["project"] in ("", "inbox")


def test_agent_prelogged_turn_skips_duplicate_writes():
    """stream_invoke(prelogged_turn=True)：fresh 轮不写 turn/start+user/message。"""
    from src.agent.agent_v3 import HermesAgentV3
    agent = HermesAgentV3.__new__(HermesAgentV3)
    agent._durable_sid = "s1"
    agent._thinking = False
    agent._should_cancel_fn = None
    agent.settings = type("S", (), {"max_agent_iterations": 1, "llm_timeout": 5})()
    writes = []
    agent._durable_append = lambda t, p: writes.append(t)
    agent._session_log = None  # _durable_append 被打桩，不会真走 log

    # _finish_turn 里会跑 ReAct——桩掉 _finish_turn 保持最小
    def fake_finish(*a, **k):
        yield {"type": "complete", "content": "ok"}
        return
    agent._finish_turn = fake_finish
    agent._permission_mode = "plan"

    import types
    ctx = types.SimpleNamespace()
    agent._resolve_and_bind_tools = lambda ctx: []

    list(agent.stream_invoke("u", "hi", session_id="s1",
                             prelogged_turn=True))
    assert "turn/start" not in writes
    assert "user/message" not in writes


def _stub_v3_capturing_state():
    """最小 stream_invoke 替身：桩掉 ReAct，捕获 state messages/baseline。"""
    from src.agent.agent_v3 import HermesAgentV3
    agent = HermesAgentV3.__new__(HermesAgentV3)
    agent._durable_sid = "s1"
    agent._thinking = False
    agent._should_cancel_fn = None
    agent.settings = type("S", (), {"max_agent_iterations": 1, "llm_timeout": 5})()
    agent._durable_append = lambda t, p: None
    agent._session_log = None
    captured = {}

    def fake_finish(state, *a, **k):
        captured["messages"] = list(state["messages"])
        captured["baseline"] = state["turn_baseline"]
        yield {"type": "complete", "content": "ok"}

    agent._finish_turn = fake_finish
    agent._permission_mode = "plan"
    agent._resolve_and_bind_tools = lambda ctx: []
    return agent, captured


def test_stream_invoke_does_not_duplicate_prelogged_user_in_messages():
    """水合历史末条已是本轮 user（content 全等）→ 不再 append，LLM 只见一条。"""
    agent, captured = _stub_v3_capturing_state()
    list(agent.stream_invoke(
        "u", "测试subagent",
        session_messages=[{"role": "user", "content": "测试subagent"}],
        session_id="s1", prelogged_turn=True,
    ))
    assert captured["messages"] == [{"role": "user", "content": "测试subagent"}]
    assert captured["baseline"] == 0


def test_stream_invoke_still_appends_when_history_has_no_current_user():
    """正常路径：历史不含本轮 user，append 一次，baseline 指向它。"""
    agent, captured = _stub_v3_capturing_state()
    hist = [
        {"role": "user", "content": "问一"},
        {"role": "assistant", "content": "答一"},
    ]
    list(agent.stream_invoke(
        "u", "问二", session_messages=hist, session_id="s1",
    ))
    assert captured["messages"] == hist + [{"role": "user", "content": "问二"}]
    assert captured["baseline"] == 2


def test_busy_session_skips_prelog(tmp_path, monkeypatch):
    """同会话在途（streaming=True）→ 不预写（防重复轮事件）。"""
    app, wm = _make_app_with_worker(monkeypatch, [{"type": "done"}])
    wm.wp.streaming = True   # 在途

    with TestClient(app) as c:
        r = c.post("/api/chat/stream",
                   json={"message": "x", "session_id": "sess02"})
        assert r.status_code == 200
    from src.agent.session_log import SessionLog
    assert SessionLog().events("sess02") == []


def test_subscribe_waits_on_open_turn_start(tmp_path, monkeypatch):
    """GET 订阅：事件尾是未闭合 turn/start 且无总线在途 → 按在途等待（挂流），
    补差后不立即 done。"""
    from src.agent.session_log import SessionLog
    log = SessionLog()
    log.append("sess03", "turn/start", {"input": "q"})
    log.append("sess03", "user/message", {"content": "q"})

    app, wm = _make_app_with_worker(monkeypatch, [])
    with TestClient(app) as c:
        # 自定义 ASGI 驱动太重：这里验证端点判定路径——请求挂起而非立即
        # done。TestClient 流式会整体物化，我们用 timeout 线程探测：
        import threading
        result = {}
        def hit():
            try:
                r = c.get("/api/chat/stream/sess03")
                result["body"] = r.text
            except Exception as e:
                result["err"] = str(e)[:80]
        t = threading.Thread(target=hit, daemon=True)
        t.start()
        t.join(timeout=3.0)
        # 挂起中（未返回）= 等待语义成立；立即返回则 body 里应有 done 帧头
        if t.is_alive():
            result["status"] = "pending（在途等待）"
        else:
            result["status"] = "returned: " + result.get("body", "")[:80]
        print(result["status"])
        assert "pending" in result["status"] or "token" in result.get("body", "")
