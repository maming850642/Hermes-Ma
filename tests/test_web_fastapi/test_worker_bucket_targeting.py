"""worker 侧会话桶语义回归：compact / session_summary / waker_set 作用于正确会话。

- P2-14：_op_compact 按 cmd.session_id 定位桶（/api/compact 亲和路由后
  压缩的是目标会话，而非 main 槽的空/旧桶）；waker_set（内联/非内联）
  带 session_id 时落到该会话的桶（槽 worker 的 current 桶不保证是该会话）。
- P3-3：session_summary 先水合目标会话再总结——旧实现拿"当前桶的消息"
  配 cmd 里的 session_id，A 会话的消息会被总结记到 B 名下（跨会话污染）。
"""
import pytest

from web_fastapi import worker_process as wp


@pytest.fixture(autouse=True)
def _capture_send(monkeypatch):
    sent = []
    monkeypatch.setattr(wp, "_send", lambda msg, **k: sent.append(msg))
    yield sent


@pytest.fixture(autouse=True)
def _no_project_lookup(monkeypatch):
    """桶创建时的项目归属读取不碰真实存储（测试隔离）。"""
    monkeypatch.setattr(wp.WorkerState, "_get_active_project", lambda self: "")


@pytest.fixture
def state():
    s = wp.WorkerState("local", slot="main")
    s.set_current("curAAAAA")
    return s


# ── P2-14：_op_compact 桶定位 ──

class _CompactResult:
    compacted_count = 3


class _FakeCtx:
    def compact_messages(self, msgs):
        msgs[:] = [{"role": "user", "content": "压缩后"}]
        return _CompactResult()


def test_op_compact_targets_cmd_session(state, monkeypatch, _capture_send):
    state.get_bucket("curAAAAA").messages = [{"role": "user", "content": "当前桶"}]
    state.get_bucket("sessBBBB").messages = [
        {"role": "user", "content": "甲"},
        {"role": "assistant", "content": "乙"},
        {"role": "user", "content": "丙"},
    ]
    saved = []
    monkeypatch.setattr(wp.WorkerState, "_save_bucket",
                        lambda self, b: saved.append(b.session_id))
    monkeypatch.setattr("src.agent.context.ContextManager", _FakeCtx)

    wp._op_compact(state, "r1", {"session_id": "sessBBBB"})

    # 压缩作用于目标会话桶，当前桶不受影响
    assert state.get_bucket("sessBBBB").messages == [
        {"role": "user", "content": "压缩后"}]
    assert state.get_bucket("curAAAAA").messages == [
        {"role": "user", "content": "当前桶"}]
    assert saved == ["sessBBBB"]
    assert _capture_send[-1]["type"] == "result"
    assert _capture_send[-1]["data"]["compacted_count"] == 3


def test_op_compact_empty_sid_uses_current_bucket(state, monkeypatch):
    state.get_bucket("curAAAAA").messages = [{"role": "user", "content": "旧消息"}]
    monkeypatch.setattr(wp.WorkerState, "_save_bucket", lambda self, b: None)
    monkeypatch.setattr("src.agent.context.ContextManager", _FakeCtx)

    wp._op_compact(state, "r1", {"session_id": ""})

    assert state.get_bucket("curAAAAA").messages == [
        {"role": "user", "content": "压缩后"}]


# ── P3-3：session_summary 先水合目标会话 ──

def test_session_summary_hydrates_target_bucket(monkeypatch, _capture_send):
    summarized = []

    def fake_on_end(manager, user_id, messages, session_id, **kw):
        summarized.append((list(messages), session_id))
        return {"summary_stored": False, "facts_count": 0, "markdown_path": None}

    def fake_hydrate(st, sid):
        st.get_bucket(sid).messages = [{"role": "user", "content": f"{sid} 的历史"}]
        return True

    monkeypatch.setattr("src.agent.session_lifecycle.on_session_end", fake_on_end)
    monkeypatch.setattr(wp, "_hydrate_bucket", fake_hydrate)

    st = wp.WorkerState("local", slot="main")
    st.set_current("curAAAAA")
    st.get_bucket("curAAAAA").messages = [
        {"role": "user", "content": "当前会话消息"}]

    wp.handle_command(st, {"id": "r1", "op": "session_summary",
                           "session_id": "othBBBBB"})

    assert len(summarized) == 1
    messages, sid = summarized[0]
    assert sid == "othBBBBB"
    # 总结的是水合后的目标会话消息，不再拿当前桶张冠李戴
    assert messages == [{"role": "user", "content": "othBBBBB 的历史"}]
    assert _capture_send[-1]["type"] == "result"


def test_session_summary_current_sid_uses_current_bucket(monkeypatch, _capture_send):
    summarized = []

    def fake_on_end(manager, user_id, messages, session_id, **kw):
        summarized.append((list(messages), session_id))
        return {"summary_stored": False, "facts_count": 0, "markdown_path": None}

    hydrate_calls = []
    monkeypatch.setattr("src.agent.session_lifecycle.on_session_end", fake_on_end)
    monkeypatch.setattr(wp, "_hydrate_bucket",
                        lambda st, sid: hydrate_calls.append(sid) or True)

    st = wp.WorkerState("local", slot="main")
    st.set_current("curAAAAA")
    st.get_bucket("curAAAAA").messages = [
        {"role": "user", "content": "当前会话消息"}]

    wp.handle_command(st, {"id": "r1", "op": "session_summary",
                           "session_id": "curAAAAA"})

    assert hydrate_calls == []          # 目标即当前桶，无需水合
    assert summarized == [([{"role": "user", "content": "当前会话消息"}],
                           "curAAAAA")]


# ── P2-14：waker_set 桶定位（非内联 + 内联） ──

def test_waker_set_targets_session_bucket(state):
    wp.handle_command(state, {"id": "r1", "op": "waker_set",
                              "name": "阿黄", "session_id": "sessBBBB"})
    assert state.get_bucket("sessBBBB").waker == "阿黄"
    assert state.current_bucket.waker == ""     # 当前桶不受影响


def test_waker_set_without_session_targets_current(state):
    wp.handle_command(state, {"id": "r1", "op": "waker_set", "name": "阿黑"})
    assert state.current_bucket.waker == "阿黑"


def test_waker_set_inline_targets_session_bucket(state):
    wp._handle_inline_cmd(state, "waker_set", "r2",
                          {"id": "r2", "op": "waker_set",
                           "name": "阿花", "session_id": "sessCCCC"})
    assert state.get_bucket("sessCCCC").waker == "阿花"
    assert state.current_bucket.waker == ""


# ── GET waker 的 session_id 透传：worker 按桶取值 ──
# （路由已亲和到会话槽，但槽的 current 桶不保证是该会话——旧实现
#  waker_get 不带 session_id 恒读 current 桶，刚 spawn 的专槽读到空值。）

def test_waker_get_targets_session_bucket(state, _capture_send):
    state.get_bucket("curAAAAA").waker = "主槽人格"
    state.get_bucket("sessBBBB").waker = "专属人格"

    wp.handle_command(state, {"id": "r3", "op": "waker_get",
                              "session_id": "sessBBBB"})

    assert _capture_send[-1]["type"] == "result"
    assert _capture_send[-1]["data"]["waker"] == "专属人格"


def test_waker_get_without_session_keeps_current_bucket(state, _capture_send):
    """无参（main 槽旧行为）：读 current 桶，不落到别的会话桶。"""
    state.get_bucket("curAAAAA").waker = "主槽人格"
    state.get_bucket("sessBBBB").waker = "专属人格"

    wp.handle_command(state, {"id": "r4", "op": "waker_get"})

    assert _capture_send[-1]["type"] == "result"
    assert _capture_send[-1]["data"]["waker"] == "主槽人格"


def test_waker_get_inline_targets_session_bucket(state, _capture_send):
    """内联路径（chat 进行中）同样按桶取值。"""
    state.get_bucket("curAAAAA").waker = "主槽人格"
    state.get_bucket("sessCCCC").waker = "内联专属"

    wp._handle_inline_cmd(state, "waker_get", "r5",
                          {"id": "r5", "op": "waker_get",
                           "session_id": "sessCCCC"})

    assert _capture_send[-1]["type"] == "result"
    assert _capture_send[-1]["data"]["waker"] == "内联专属"
