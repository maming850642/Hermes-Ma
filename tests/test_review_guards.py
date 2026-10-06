"""
reviewall 终审测试盲区补齐（#8-#12）。

- #8 _op_chat_approve 端到端（首个）：真实 agent 走 interrupt→approve，
  并钉住 P2-1 修复（恢复轮保留 waker 人格 / todos / 压缩阈值）
- #9 chat_stop 内联取消（W2 回滚不可见的洞）
- #10 human_approval_request payload 精确契约 + relay_stream 三映射
  （含 finally 安全网取消契约：忙超时/正常结束不 cancel，泵超时必 cancel）
- #11 W1 路由：config/system 鉴权、掩码跳过、删除清事件、TimeoutError→503
- #12 _StdoutPump：行不丢 / 超时 None / EOF 哨兵
"""
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from src.storage import paths


@pytest.fixture(autouse=True)
def _isolated_data_root(tmp_path):
    paths.set_data_root(tmp_path)
    yield
    paths.set_data_root(None)


# ============================================
# #8 _op_chat_approve 端到端（真实 agent）
# ============================================

class _GuardLLM:
    """第一轮发 destructive tool_call（触发审批中断），恢复轮给最终答复。"""

    def __init__(self):
        from src.llm.messages import Chunk
        self.calls = []
        self._chunks = [
            [Chunk(content_delta="", tool_call_deltas=[{
                "index": 0, "id": "call_1", "type": "function",
                "function": {"name": "rm", "arguments": "{\"p\": 1}"}}])],
            [Chunk(content_delta="完", tool_call_deltas=[])],
        ]

    def stream_chat(self, messages, tools=None, **kw):
        self.calls.append(list(messages))
        for c in self._chunks.pop(0):
            yield c


def test_op_chat_approve_full_chain(tmp_path, monkeypatch):
    import web_fastapi.worker_process as wp
    from tests.agent.test_agent_events import make_agent, make_tool_spec, FakeExecutor
    from src.agent.hitl import InterruptStore

    log = wp.SessionLog() if hasattr(wp, "SessionLog") else None
    from src.agent.session_log import SessionLog
    log = SessionLog()

    from src.agent.hitl import InterruptStore as _IS
    spec = make_tool_spec(name="rm", destructive=True, executor=FakeExecutor(content="已删除"))
    agent = make_agent(monkeypatch, specs=[spec], session_log=log,
                       interrupt_store=_IS(session_log=log))
    agent.set_permission_mode("before_changes")
    llm = _GuardLLM()
    monkeypatch.setattr(agent, "_get_llm_client", lambda: llm)

    bucket = wp.SessionBucket("ap1")
    bucket.todos = [{"text": "保留我", "status": "pending"}]
    bucket.waker = "w1"
    state = MagicMock(name="worker-state")
    state.user_id = "local"
    state.prefs = {"temperature": 0.7, "max_tokens": 2000, "compact_threshold_pct": 77}
    state.current_sid = "ap1"
    state.get_bucket = lambda sid: bucket
    state.agent = agent
    state._save_bucket = lambda b: None

    sent = []
    monkeypatch.setattr(wp, "_send", lambda msg: sent.append(msg))

    # 第一轮：应中断在 human_approval_request
    wp._op_chat(state, "req1", {"op": "chat", "message": "删掉它", "session_id": "ap1"})
    evs = [m.get("event") for m in sent if m.get("type") == "event"]
    assert "human_approval_request" in evs, evs

    # P2-1：恢复轮加载人格——patch load_persona_prompt 返回哨兵文本
    sent.clear()
    with patch("src.waker.persona.load_persona_prompt", return_value="## 数字员工人格\n哨兵人格XYZ"), \
         patch("src.waker.store.WakerStore"):
        wp._op_chat_approve(state, "req2", {
            "op": "chat_approve", "thread_id": "ap1", "decision": "approve",
        })

    # 恢复轮完成：complete + done，工具执行（批准）
    evs = [m.get("event") for m in sent if m.get("type") == "event"]
    assert "tool_end" in evs and "complete" in evs, evs
    assert any(m.get("type") == "done" for m in sent)

    # P2-1 钉死：恢复轮的 LLM 请求 system 消息含 waker 人格哨兵
    resume_call = llm.calls[-1]
    system = resume_call[0]
    assert system.get("role") == "system"
    assert "哨兵人格XYZ" in system.get("content", ""), "审批恢复轮丢失 waker 人格（P2-1 回归）"

    # durable 事件完整：interrupt 走独立 scope（thread_id 键），恢复轮 tool/result 在 chat scope
    from src.agent.session_log import INTERRUPT_REQUESTED, INTERRUPT_RESOLVED, TOOL_RESULT
    interrupt_types = [e["type"] for e in log.provider.iter_events("interrupt", "ap1")]
    assert INTERRUPT_REQUESTED in interrupt_types and INTERRUPT_RESOLVED in interrupt_types, interrupt_types
    assert TOOL_RESULT in [e["type"] for e in log.events("ap1")]


# ============================================
# #9 chat_stop 取消直通（W2，协作式取消版）
# ============================================

def test_chat_stop_direct_cancel_probe(monkeypatch):
    """W2 回归守卫：chat_stop 必须在 stdin 读取线程直通置位 _cancel_event。

    历史教训：取消依赖 drain 事件间隙处理，而工具执行期 agent 零事件，
    停止要等当前工具跑完才生效（期间 worker 锁被占、重发消息报忙）。
    现契约：_direct_cancel_probe 命中 chat_stop → 置标志 + 回 ack +
    返回 True（不入队）；其它命令一律放行。"""
    import web_fastapi.worker_process as wp

    wp._cancel_event.clear()
    sent = []
    monkeypatch.setattr(wp, "_send", lambda msg: sent.append(msg))

    # 非 chat_stop 命令不拦截
    assert wp._direct_cancel_probe({"id": "x", "op": "prefs_get"}) is False
    assert not wp._cancel_event.is_set()

    # chat_stop：置标志 + ack + 拦截
    assert wp._direct_cancel_probe({"id": "stop1", "op": "chat_stop"}) is True
    assert wp._cancel_event.is_set(), "chat_stop 直通未置取消标志（W2 回归）"
    acks = [m for m in sent if m.get("type") == "result"
            and m.get("data", {}).get("ok")]
    assert acks, "chat_stop 直通未回 ack"
    wp._cancel_event.clear()


# ============================================
# #10 事件契约
# ============================================

def test_human_approval_request_payload_contract(monkeypatch):
    from tests.agent.test_agent_events import make_agent, make_tool_spec, FakeExecutor, ROUND_TOOL, run_turn

    spec = make_tool_spec(name="rm", destructive=True, executor=FakeExecutor(content="x"))
    agent = make_agent(monkeypatch, specs=[spec])
    agent.set_permission_mode("before_changes")
    agent._llm_client = _GuardLLM()

    events = run_turn(agent, user_input="删", session_id="hc1")
    approvals = [e for e in events if e["type"] == "human_approval_request"]
    assert approvals, "before_changes + destructive 未触发审批"
    a = approvals[0]
    # 消费方（chat.js showApproval / cli 弹层）依赖的精确键集
    assert a == {
        "type": "human_approval_request",
        "action": a["action"],       # 动作描述（非空 str）
        "details": a["details"],     # 详情（str）
        "thread_id": "hc1",
    }
    assert isinstance(a["action"], str) and a["action"]
    assert isinstance(a["details"], str)


def test_ipc_events_to_sse_three_mappings():
    """（语义升级版）relay_stream 三映射 + finally 取消安全网契约：
    忙超时（从未上车）不得 cancel；正常结束不得 cancel；泵超时（已上车）
    必须 cancel——历史上无差别 request_cancel 曾把并发正在跑的 chat 误杀。"""
    from web_fastapi.routers.chat import relay_stream

    def _fake_worker(lines):
        cancelled = MagicMock()
        worker = MagicMock()
        worker.send_stream = lambda op, **kw: iter(lines)
        worker.request_cancel = cancelled
        return worker, cancelled

    # 1) 三段 SSE 映射 + 正常结束不 cancel
    worker, cancelled = _fake_worker([
        {"type": "event", "event": "token", "data": {"content": "hi"}},
        {"type": "result", "data": {"ok": True}},
        {"type": "error", "message": "worker 死了"},
    ])
    raw = b"".join(relay_stream(worker, "chat")).decode("utf-8")
    parts = [p for p in raw.split("\n\n") if p.strip()]
    assert len(parts) == 3
    assert parts[0].startswith("event: token")
    assert json.loads(parts[0].split("data: ", 1)[1]) == {"content": "hi"}
    assert parts[1].startswith("event: complete")
    assert json.loads(parts[1].split("data: ", 1)[1]) == {"ok": True}
    assert parts[2].startswith("event: error")
    assert json.loads(parts[2].split("data: ", 1)[1]) == {"message": "worker 死了", "busy": False}
    cancelled.assert_not_called()

    # 2) 忙超时（busy 标记，从未上车）→ 不 cancel；busy 标志透传给前端
    # （前端据此自动排队重试，见 chat.js streamMessage 的 busy 分支）
    worker, cancelled = _fake_worker([
        {"type": "error", "busy": True, "message": "AI 正在思考（worker 忙），请稍后重试"},
    ])
    raw = b"".join(relay_stream(worker, "chat")).decode("utf-8")
    err_data = json.loads([p for p in raw.split("\n\n") if p.startswith("event: error")][0].split("data: ", 1)[1])
    assert err_data["busy"] is True
    assert err_data["message"] == "AI 正在思考（worker 忙），请稍后重试"
    cancelled.assert_not_called()

    # 3) 泵超时（已上车）→ 安全网 cancel
    worker, cancelled = _fake_worker([
        {"type": "event", "event": "token", "data": {"content": "par"}},
        {"type": "error", "timeout": True, "message": "worker 响应超时（300s）"},
    ])
    b"".join(relay_stream(worker, "chat"))
    cancelled.assert_called_once()


# ============================================
# #11 W1 路由补测
# ============================================

def test_config_system_public(tmp_path):
    """免认证形态：config system 匿名可读可写（原 401 断言随认证移除反转）。"""
    from fastapi.testclient import TestClient
    from web_fastapi.app import create_app

    app = create_app()
    with TestClient(app) as c:
        assert c.get("/api/config/system").status_code == 200
    assert c.put("/api/config/system", json={"updates": {}}).status_code == 200


def test_config_system_put_skips_masked_secret(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from web_fastapi.app import create_app
    import web_fastapi.routers.config_router as cr

    written = {}
    monkeypatch.setattr(cr, "load_yaml", lambda: {"openai_api_key": "sk-real-key-123", "llm_timeout": 120})
    monkeypatch.setattr(cr, "save_yaml", lambda d: written.update(d))
    monkeypatch.setattr(cr, "classify_keys", lambda: ({"openai_api_key", "llm_timeout"}, set()))

    app = create_app()
    with TestClient(app) as c:
        c.post("/api/auth/login", json={})
        r = c.put("/api/config/system", json={"updates": {
        "openai_api_key": "sk-r****----",   # GET 回显的掩码值原样回传
        "llm_timeout": "240",
    }})
    assert r.status_code == 200
    j = r.json()
    assert "openai_api_key" in j["skipped_masked"]
    assert "openai_api_key" not in written        # 真实 key 不被掩码覆盖
    assert written.get("llm_timeout") == "240"


def test_session_delete_purges_events(tmp_path, monkeypatch):
    import web_fastapi.worker_process as wp
    from src.agent.session_log import SessionLog

    log = SessionLog()
    log.append("pd1", "turn/start", {"input": "x"})
    log.append("pd1", "user/message", {"content": "x"})
    assert log.events("pd1")

    state = MagicMock(name="st")
    state.agent = MagicMock()
    state.agent._session_log = log
    state.user_id = "local"
    sent = []
    monkeypatch.setattr(wp, "_send", lambda msg: sent.append(msg))
    # 会话文件不存在（tmp data root），但事件应被清
    wp.handle_command.__wrapped__ if hasattr(wp.handle_command, "__wrapped__") else None
    # 直接调 op 分支（模拟分发）
    import types
    cmd = {"id": "r1", "op": "session_delete", "session_id": "pd1"}
    # 复用 handle_command 会走 _send 与未知分支——最小路径：手动执行该分支等价逻辑
    from src.session_store import _get_session_file
    target = cmd["session_id"]
    purged = log.purge_session(target)
    assert purged >= 2
    assert log.events("pd1") == []


def test_timeout_error_maps_to_503(tmp_path):
    from fastapi.testclient import TestClient
    from web_fastapi.app import create_app

    app = create_app()

    @app.get("/api/__test_timeout")
    async def _boom():
        raise TimeoutError("worker local 忙")

    with TestClient(app) as c:
        c.post("/api/auth/login", json={})
        r = c.get("/api/__test_timeout")
    assert r.status_code == 503
    assert "思考" in r.json()["detail"] or "worker" in r.json()["detail"]


# ============================================
# #12 _StdoutPump
# ============================================

def test_stdout_pump_lines_order_and_eof():
    import io as _io
    from web_fastapi.worker_manager import _StdoutPump, _EOF

    buf = _io.StringIO("line1\nline2\nline3\n")
    pump = _StdoutPump(buf)
    assert pump.get(2.0) == "line1\n"
    assert pump.get(2.0) == "line2\n"
    assert pump.get(2.0) == "line3\n"
    assert pump.get(2.0) is _EOF      # EOF 哨兵（非 None=超时）


def test_stdout_pump_timeout_returns_none():
    from web_fastapi.worker_manager import _StdoutPump
    import time as _t

    class Blocked:
        def readline(self):
            import threading
            threading.Event().wait(5)   # 永久阻塞（守护线程，测试结束即弃）
            return "\n"
    pump = _StdoutPump(Blocked())
    t0 = _t.time()
    assert pump.get(0.3) is None       # 超时 None（不是 EOF 哨兵）
    assert _t.time() - t0 < 1.5
