"""会话生命周期三连缺陷回归（同根因：多槽架构下非 chat 路径对冷槽不水合）。

P0 冷槽空桶首轮失忆+覆写快照：
    应用重启后 /api/sessions/current?sid=X 只在 main 槽做了展示用水合；
    用户发首条消息 → acquire_chat_slot(X) spawn 全新槽 → _op_chat 的
    get_bucket(X) 新建空桶无水合 → agent 收 messages=[] 失忆开局，轮末
    _save_bucket 用 [user, assistant] 覆写 X.json（历史蒸发）。
    修法：_op_chat 经 _get_or_hydrate_bucket 取桶——桶不存在或为空先
    _hydrate_bucket；水合失败（无快照无事件）保持空桶开新会话。

P1 /api/compact 不写 compact/applied：
    旧实现对齐缺失——事件投影重载时复活压缩前历史、fork 复制全量。
    修法：_op_compact 压缩成功后经 agent._session_log 写同型事件
    （summary/original_count/compacted_count/kept_messages + lc_to_dict）。

P2 冷会话 compact/save 空桶误导：
    刚 spawn 的槽里 compact 对着盘上有历史的会话回"无对话历史"、save 回
    message_count=0。水合后 compact/save 拿到真实桶；真无会话时 save 仍
    空桶不落盘 + ok=True message_count=0（不撒谎）。

P3 管理端点 busy 语义 + reset 槽回收：
    delete/rename/save worker 锁忙时 503（旧实现 500，delete 还可能误回收
    正在跑 chat 的槽）；reset 带 sid 时回收旧 sid 专属槽（live 数回落）。
"""
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.session_store as ss_mod
from config import get_settings
from src.agent.session_log import ASSISTANT_MSG, SessionLog, TURN_END, TURN_START, USER_MSG
from src.constants import LOCAL_USER
from src.session_store import load_session, save_session
from src.storage.sqlite_provider import SQLiteProvider
from web_fastapi import worker_process as wp
from web_fastapi.routers import sessions as sessions_router
from web_fastapi.worker_manager import DEFAULT_SLOT


# ════════════════════════════════════════════════════════════════
# 公共夹具
# ════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _capture_send(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(wp, "_send", lambda msg, **k: sent.append(msg))
    yield sent


@pytest.fixture(autouse=True)
def _no_project_lookup(monkeypatch):
    """桶创建/保存时的项目归属读取不碰真实存储（测试隔离）。"""
    monkeypatch.setattr(wp.WorkerState, "_get_active_project", lambda self: "")


@pytest.fixture(autouse=True)
def force_persist(monkeypatch):
    monkeypatch.setattr(get_settings(), "session_persist", True, raising=False)


@pytest.fixture
def sessions_root(tmp_path, monkeypatch, request):
    from src.storage import paths
    # P3 起 save 还会经 session_state_store 读写默认库 kv——数据根一并改道
    paths.set_data_root(tmp_path)
    request.addfinalizer(lambda: paths.set_data_root(None))
    root = tmp_path / "sessions"
    monkeypatch.setattr(ss_mod, "SESSIONS_DIR", root)
    return root


@pytest.fixture
def session_log(tmp_path):
    """隔离的 SessionLog（SQLiteProvider 指 tmp），经桩 agent._session_log 注入。"""
    provider = SQLiteProvider(db_path=tmp_path / "db" / "events.db")
    log = SessionLog(provider=provider)
    yield log
    provider.close()


class _StubAgent:
    """替身 agent：记录 stream_invoke 收到的历史，回放一轮增量事件。"""

    def __init__(self, session_log=None):
        self._session_log = session_log
        self.seen_histories: list[list] = []
        self.llm_param_calls: list[dict] = []

    def set_llm_params(self, **kwargs):
        self.llm_param_calls.append(kwargs)

    def stream_invoke(self, user_id, message, messages, **kwargs):
        self.seen_histories.append(list(messages))

        def _gen():
            yield {"type": "turn_messages",
                   "messages": [{"role": "user", "content": message},
                                 {"role": "assistant", "content": "收到"}]}
            yield {"type": "complete", "content": "收到"}

        return _gen()


HIST5 = [
    {"role": "user", "content": "问一"},
    {"role": "assistant", "content": "答一"},
    {"role": "user", "content": "问二"},
    {"role": "assistant", "content": "答二"},
    {"role": "user", "content": "问三"},
]


def _cold_state(slot: str, agent) -> wp.WorkerState:
    """冷槽 state：刚 spawn 的会话槽 worker，目标会话桶尚未物化。"""
    state = wp.WorkerState(LOCAL_USER, slot=slot)
    state.agent = agent
    assert slot not in state._buckets
    return state


# ════════════════════════════════════════════════════════════════
# P0：_op_chat 冷槽水合
# ════════════════════════════════════════════════════════════════

def test_op_chat_cold_slot_hydrates_history_before_first_turn(
        sessions_root, session_log, _capture_send):
    """假盘快照 5 条 → 冷 state 直接 _op_chat：桩 agent 收到 5 条历史，
    轮末保存是 append 语义（5+2 条），不再被 [user, assistant] 覆写。"""
    save_session(LOCAL_USER, [dict(m) for m in HIST5], "cold0001")

    state = _cold_state("cold0001", _StubAgent(session_log=session_log))
    wp._op_chat(state, "r1", {"session_id": "cold0001", "message": "新消息"})

    # agent 收到水合后的 5 条历史（旧实现：[] 失忆开局）
    assert state.agent.seen_histories[0] == HIST5
    # 桶 = 历史 5 + 本轮 2（增量 append，不是整体替换）
    bucket = state.get_bucket("cold0001")
    assert len(bucket.messages) == 7
    assert bucket.messages[-2:] == [{"role": "user", "content": "新消息"},
                                     {"role": "assistant", "content": "收到"}]
    assert bucket.messages[0] == {"role": "user", "content": "问一"}
    # 盘上快照未被 [user, assistant] 覆写：仍是完整 7 条
    msgs, _, _, _ = load_session(LOCAL_USER, "cold0001")
    assert len(msgs) == 7
    assert msgs[0] == {"role": "user", "content": "问一"}
    assert _capture_send[-1]["type"] == "done"


def test_op_chat_prelogged_user_not_duplicated_on_cold_hydrate(
        sessions_root, session_log, _capture_send):
    """prelog 已把本轮 user 写入事件流；冷槽水合后必须剥掉再交给 agent，
    否则 LLM 看到两条、drain 桶里也两条（「已收到两次」）。"""
    sid = "prelogdup"
    session_log.append(sid, TURN_START, {"input": "测试subagent"})
    session_log.append(sid, USER_MSG, {"content": "测试subagent"})

    state = _cold_state(sid, _StubAgent(session_log=session_log))
    wp._op_chat(state, "r1", {
        "session_id": sid, "message": "测试subagent", "prelogged": True,
    })

    assert state.agent.seen_histories[0] == []
    bucket = state.get_bucket(sid)
    assert bucket.messages == [
        {"role": "user", "content": "测试subagent"},
        {"role": "assistant", "content": "收到"},
    ]


def test_op_chat_prelogged_strips_only_current_user_keeps_history(
        sessions_root, session_log, _capture_send):
    """完整历史 + 预写的本轮 user：只剥末条预写，旧轮次保留。"""
    sid = "preloghist"
    session_log.append(sid, TURN_START, {"input": "问一"})
    session_log.append(sid, USER_MSG, {"content": "问一"})
    session_log.append(sid, ASSISTANT_MSG, {"content": "答一"})
    session_log.append(sid, TURN_END, {})
    session_log.append(sid, TURN_START, {"input": "新消息"})
    session_log.append(sid, USER_MSG, {"content": "新消息"})

    state = _cold_state(sid, _StubAgent(session_log=session_log))
    wp._op_chat(state, "r1", {
        "session_id": sid, "message": "新消息", "prelogged": True,
    })

    assert state.agent.seen_histories[0] == [
        {"role": "user", "content": "问一"},
        {"role": "assistant", "content": "答一"},
    ]
    bucket = state.get_bucket(sid)
    assert bucket.messages == [
        {"role": "user", "content": "问一"},
        {"role": "assistant", "content": "答一"},
        {"role": "user", "content": "新消息"},
        {"role": "assistant", "content": "收到"},
    ]


def test_op_chat_without_any_data_starts_fresh(
        sessions_root, session_log, _capture_send):
    """无任何数据（无快照无事件）：水合无所获，空桶正常开新会话。"""
    state = _cold_state("fresh0001", _StubAgent(session_log=session_log))
    wp._op_chat(state, "r1", {"session_id": "fresh0001", "message": "你好"})

    assert state.agent.seen_histories[0] == []          # 真新会话：空历史开局
    bucket = state.get_bucket("fresh0001")
    assert bucket.messages == [{"role": "user", "content": "你好"},
                               {"role": "assistant", "content": "收到"}]
    assert _capture_send[-1]["type"] == "done"


def test_op_chat_failed_hydration_keeps_preset_waker(
        sessions_root, session_log, monkeypatch):
    """空水合不冲掉开聊前经 waker_set 预设的会话级状态。"""
    monkeypatch.setattr("src.waker.store.WakerStore", lambda *a, **kw: MagicMock())
    monkeypatch.setattr("src.waker.persona.load_persona_prompt",
                        lambda *a, **kw: None)

    state = _cold_state("waker0001", _StubAgent(session_log=session_log))
    wp.handle_command(state, {"id": "w1", "op": "waker_set",
                              "name": "阿黄", "session_id": "waker0001"})
    wp._op_chat(state, "r1", {"session_id": "waker0001", "message": "在吗"})

    assert state.get_bucket("waker0001").waker == "阿黄"


def test_hydrate_restores_bucket_project_and_chat_stamps_it(
        sessions_root, session_log, _capture_send):
    """打开旧项目 A 会话而顶栏在别处：桶归属以快照为准回填 A，
    本轮记忆上下文（remember 写入/检索范围）stamp A，不受顶栏影响。"""
    from src.tools import remember as remember_mod
    from src.tools.remember import get_current_project
    save_session(LOCAL_USER, [dict(m) for m in HIST5], "projA001", project="proj-a")

    captured: dict = {}

    class _CapturingAgent(_StubAgent):
        def stream_invoke(self, user_id, message, messages, **kwargs):
            captured["project"] = get_current_project()
            return super().stream_invoke(user_id, message, messages, **kwargs)

    state = _cold_state("projA001", _CapturingAgent(session_log=session_log))
    try:
        wp._op_chat(state, "r1", {"session_id": "projA001", "message": "继续"})
    finally:
        remember_mod._current_project.set(None)

    assert state.get_bucket("projA001").project == "proj-a"  # 快照归属回填
    assert captured["project"] == "proj-a"                    # 本轮记忆上下文跟会话


# ════════════════════════════════════════════════════════════════
# P1：_op_compact 写 compact/applied 事件
# ════════════════════════════════════════════════════════════════

class _CompactResultStub:
    def __init__(self, summary, original_count, compacted_count):
        self.summary = summary
        self.original_count = original_count
        self.compacted_count = compacted_count


class _CtxMgrStub:
    """替身压缩器：原位把消息替换为 [system 摘要, 保留区 2 条]。"""

    summary = "压缩摘要"

    def compact_messages(self, msgs):
        original = len(msgs)
        msgs[:] = [
            {"role": "system",
             "content": "以下是对话历史的压缩摘要，请基于此背景继续对话：\n压缩摘要"},
            {"role": "user", "content": "尾问"},
            {"role": "assistant", "content": "尾答"},
        ]
        return _CompactResultStub(self.summary, original, original - 2)


def test_op_compact_writes_compact_applied_event(
        sessions_root, session_log, monkeypatch, _capture_send):
    """压缩后事件库有 compact/applied，且 derive 结果为压缩后形态
    （不再复活压缩前历史）。"""
    sid = "evnt0001"
    for m in HIST5[:4]:  # 2 轮问答 → 4 条
        session_log.append(
            sid, USER_MSG if m["role"] == "user" else ASSISTANT_MSG,
            {"content": m["content"]})
    assert len(session_log.derive_messages(sid)) == 4

    state = _cold_state(sid, _StubAgent(session_log=session_log))
    monkeypatch.setattr("src.agent.context.ContextManager", _CtxMgrStub)

    wp._op_compact(state, "r1", {"session_id": sid})

    compact_events = [e for e in session_log.events(sid)
                      if e["type"] == "compact/applied"]
    assert len(compact_events) == 1
    payload = compact_events[0]["payload"]
    assert payload["summary"] == "压缩摘要"
    assert payload["original_count"] == 4
    assert payload["compacted_count"] == 2
    # kept_messages = 压缩后消息去掉头部摘要 system（lc_to_dict 规范化）
    assert payload["kept_messages"] == [
        {"role": "user", "content": "尾问"},
        {"role": "assistant", "content": "尾答"},
    ]
    # derive = 压缩后形态（system 摘要 + 保留区），不是压缩前 4 条
    assert session_log.derive_messages(sid) == [
        {"role": "system", "content": "压缩摘要"},
        {"role": "user", "content": "尾问"},
        {"role": "assistant", "content": "尾答"},
    ]
    assert _capture_send[-1]["type"] == "result"
    assert _capture_send[-1]["data"]["ok"] is True
    assert _capture_send[-1]["data"]["compacted_count"] == 2


# ════════════════════════════════════════════════════════════════
# P2：冷槽 compact/save 水合语义
# ════════════════════════════════════════════════════════════════

def test_op_compact_cold_slot_hydrates_then_compacts(
        sessions_root, session_log, monkeypatch, _capture_send):
    """冷槽 + 盘上快照有历史 → 压缩真实历史，不再回"无对话历史"。"""
    save_session(LOCAL_USER, [dict(m) for m in HIST5], "snapBBBB")
    monkeypatch.setattr("src.agent.context.ContextManager", _CtxMgrStub)

    state = _cold_state("snapBBBB", _StubAgent(session_log=session_log))
    wp._op_compact(state, "r1", {"session_id": "snapBBBB"})

    data = _capture_send[-1]["data"]
    assert data["ok"] is True
    assert data["compacted_count"] == 3          # 5 条历史 → 摘要 + 保留 2
    assert [m["content"] for m in state.get_bucket("snapBBBB").messages] == [
        "以下是对话历史的压缩摘要，请基于此背景继续对话：\n压缩摘要", "尾问", "尾答"]


def test_op_compact_cold_slot_truly_empty_reports_no_history(
        sessions_root, session_log, _capture_send):
    """真无会话：保持"无对话历史"的诚实回复。"""
    state = _cold_state("ghost0002", _StubAgent(session_log=session_log))
    wp._op_compact(state, "r1", {"session_id": "ghost0002"})
    data = _capture_send[-1]["data"]
    assert data == {"ok": False, "message": "无对话历史"}


def test_session_save_cold_slot_hydrates_then_reports_real_count(
        sessions_root, session_log, _capture_send):
    """冷槽 save：水合后回报真实 message_count（旧实现恒 0，误导）。"""
    save_session(LOCAL_USER, [dict(m) for m in HIST5], "coldBBBB")

    state = _cold_state("coldBBBB", _StubAgent(session_log=session_log))
    wp.handle_command(state, {"id": "r1", "op": "session_save",
                              "session_id": "coldBBBB"})

    assert _capture_send[-1]["data"] == {"ok": True, "session_id": "coldBBBB",
                                         "message_count": 5}


def test_session_save_truly_empty_returns_honest_zero(
        sessions_root, session_log, _capture_send):
    """真无会话：ok=True message_count=0 且不落盘空快照（幽灵治理）。"""
    state = _cold_state("ghostCCCC", _StubAgent(session_log=session_log))
    wp.handle_command(state, {"id": "r1", "op": "session_save",
                              "session_id": "ghostCCCC"})

    assert _capture_send[-1]["data"] == {"ok": True, "session_id": "ghostCCCC",
                                         "message_count": 0}
    assert not (sessions_root / LOCAL_USER / "ghostCCCC.json").exists()


# ════════════════════════════════════════════════════════════════
# P3：管理端点 busy 503 + reset 槽回收（路由层）
# ════════════════════════════════════════════════════════════════

class _RouterStubWorker:
    def __init__(self, slot: str, busy: bool = False):
        self.slot = slot
        self.busy = busy

    def send(self, op, **kwargs):
        if self.busy:
            raise TimeoutError(
                f"worker {LOCAL_USER} 忙（chat 进行中？），op={op} 超过 5s 未拿到锁")
        return [{"type": "result", "data": {"ok": True, "slot": self.slot}}]


class _RouterStubManager:
    """替身 WorkerManager：槽表 + remove 记录（reset 回收断言用）。"""

    def __init__(self):
        self.slots: dict[str, _RouterStubWorker] = {}
        self.removed: list[str] = []

    def lookup(self, user_id, sid):
        return self.slots.get(sid)

    def get_or_create(self, user_id, slot=DEFAULT_SLOT):
        return self.slots.setdefault(slot, _RouterStubWorker(slot))

    def acquire_chat_slot(self, session_id):
        return (session_id or "").strip() or DEFAULT_SLOT

    def remove(self, user_id, slot=DEFAULT_SLOT):
        self.removed.append(slot)
        self.slots.pop(slot, None)


class _FakeCtx:
    def __init__(self, log):
        self._log = log

    def try_get(self, key):
        return self._log if key == "sessions" else None


def _router_app(manager, log) -> FastAPI:
    a = FastAPI()
    a.include_router(sessions_router.router, prefix="/api/sessions")
    a.state.worker_manager = manager
    a.state.cordis_ctx = _FakeCtx(log)
    return a


def test_session_mgmt_busy_returns_503_and_keeps_slot(tmp_path):
    """worker 锁忙（chat 进行中）：delete/rename/save → 503；delete 不回收
    正在跑 chat 的槽。"""
    manager = _RouterStubManager()
    manager.slots["sessBBBB"] = _RouterStubWorker("sessBBBB", busy=True)
    provider = SQLiteProvider(db_path=tmp_path / "db" / "hermes.db")
    client = TestClient(_router_app(manager, SessionLog(provider=provider)))
    try:
        r = client.delete("/api/sessions/sessBBBB")
        assert r.status_code == 503, r.text
        assert manager.removed == []                 # 活跃流不能被删槽误杀
        assert "sessBBBB" in manager.slots

        r = client.patch("/api/sessions/sessBBBB/rename", json={"name": "新名"})
        assert r.status_code == 503, r.text

        r = client.post("/api/sessions/save", params={"session_id": "sessBBBB"})
        assert r.status_code == 503, r.text
    finally:
        provider.close()


def test_reset_recycles_old_session_slot(tmp_path):
    """占用 2 槽 + reset 一会话：旧 sid 槽被 remove，live 数回落 2→1。"""
    manager = _RouterStubManager()
    manager.slots["sessAAAA"] = _RouterStubWorker("sessAAAA")
    manager.slots["sessBBBB"] = _RouterStubWorker("sessBBBB")
    provider = SQLiteProvider(db_path=tmp_path / "db" / "hermes.db")
    client = TestClient(_router_app(manager, SessionLog(provider=provider)))
    try:
        r = client.post("/api/sessions/reset", json={"session_id": "sessAAAA"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["session_id"] and body["session_id"] != "sessAAAA"

        assert manager.removed == ["sessAAAA"]       # 旧槽已回收
        assert set(manager.slots) == {"sessBBBB"}    # live 2 → 1，他槽不受影响
    finally:
        provider.close()


def test_reset_without_sid_does_not_recycle_main(tmp_path):
    """无 sid 的 reset 维持旧行为：不回收（main 槽永不回收）。"""
    manager = _RouterStubManager()
    provider = SQLiteProvider(db_path=tmp_path / "db" / "hermes.db")
    client = TestClient(_router_app(manager, SessionLog(provider=provider)))
    try:
        r = client.post("/api/sessions/reset")
        assert r.status_code == 200, r.text
        assert manager.removed == []
    finally:
        provider.close()


# ════════════════════════════════════════════════════════════════
# 编辑重发：_op_session_truncate（2026-09-19）
# ════════════════════════════════════════════════════════════════

def _seed_events5(log, sid):
    """HIST5 逐条落成事件（user/assistant 交替 5 条，3 问 2 答）。"""
    for m in HIST5:
        log.append(sid, USER_MSG if m["role"] == "user" else ASSISTANT_MSG,
                   {"content": m["content"]})


def test_op_truncate_writes_truncated_event_and_shrinks_bucket(
        sessions_root, session_log, _capture_send):
    """截到第 1 条 user（0-based）之前：写 session/truncated、桶与返回
    history 同步收缩、derive（事件权威）不复活被截掉的尾巴。"""
    sid = "trnc0001"
    _seed_events5(session_log, sid)
    state = _cold_state(sid, _StubAgent(session_log=session_log))

    wp._op_session_truncate(state, "r1", {"session_id": sid, "user_ordinal": 1})

    data = _capture_send[-1]["data"]
    assert data["ok"] is True
    assert [m["content"] for m in state.get_bucket(sid).messages] == ["问一", "答一"]
    assert [m["content"] for m in data["history"]] == ["问一", "答一"]
    ev = [e for e in session_log.events(sid) if e["type"] == "session/truncated"]
    assert len(ev) == 1
    assert ev[0]["payload"]["original_count"] == 5
    assert ev[0]["payload"]["truncated_count"] == 3
    assert ev[0]["payload"]["kept_messages"] == [
        {"role": "user", "content": "问一"},
        {"role": "assistant", "content": "答一"},
    ]
    assert [m["content"] for m in session_log.derive_messages(sid)] == ["问一", "答一"]


def test_op_truncate_expect_text_mismatch_rejects(
        sessions_root, session_log, _capture_send):
    """expect_text 与桶内不符（多标签页/陈旧视图）→ 拒绝且零改动。"""
    sid = "trnc0002"
    _seed_events5(session_log, sid)
    state = _cold_state(sid, _StubAgent(session_log=session_log))

    wp._op_session_truncate(state, "r1", {
        "session_id": sid, "user_ordinal": 1, "expect_text": "被改过的问句"})

    data = _capture_send[-1]["data"]
    assert data["ok"] is False
    assert "已变化" in data["message"]
    assert len(session_log.derive_messages(sid)) == 5       # 原样未动
    assert not [e for e in session_log.events(sid)
                if e["type"] == "session/truncated"]


def test_op_truncate_expect_text_match_passes(
        sessions_root, session_log, _capture_send):
    sid = "trnc0003"
    _seed_events5(session_log, sid)
    state = _cold_state(sid, _StubAgent(session_log=session_log))

    wp._op_session_truncate(state, "r1", {
        "session_id": sid, "user_ordinal": 1, "expect_text": "问二"})

    assert _capture_send[-1]["data"]["ok"] is True


def test_op_truncate_ordinal_out_of_range_rejects(
        sessions_root, session_log, _capture_send):
    sid = "trnc0004"
    _seed_events5(session_log, sid)
    state = _cold_state(sid, _StubAgent(session_log=session_log))

    wp._op_session_truncate(state, "r1", {"session_id": sid, "user_ordinal": 9})

    data = _capture_send[-1]["data"]
    assert data["ok"] is False
    assert "第 10 条" in data["message"]
