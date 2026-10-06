"""生成中会话可发现性（in-flight discoverability）回归。

主链缺陷：首条消息生成中切页再回来，旧实现看不见该会话——
1. 快照列表以 worker 轮末 _save_bucket 为准，生成中的新会话不在
   /api/sessions 里；前端零锁快路径对不上 saved sid（旧实现把 sid 劫持
   成项目最新旧会话，currentSessionId 被改写 → 恢复轮询被守卫永久阻断）。
2. /active 一次性探测 false（worker spawn 窗口）即放弃（前端 chat.js
   有界重试，无 pytest 覆盖面）。
3. /current 生成中撞 worker 锁 5s 超时直接 500（旧实现）。

本文件锁定修复后的 worker/路由侧行为：
- _op_chat 开轮（agent 写 turn/start 前）经 ensure_session_stub 建最小
  快照（既有历史 + 本条 user 消息，ADR-0004-D2「从无到有」语义）→
  生成中的会话立即出现在 /api/sessions 列表与侧栏；
- 轮末 _save_bucket 权威整文件覆盖，与 stub 不冲突（message_count 正常、
  不覆盖已有快照）；
- GET /api/sessions/{sid}/events 主进程直读 SQLite：开轮即有
  turn/start + user/message（前端直读投影的数据基础）；
- GET /api/sessions/current 忙时 503（对齐同文件其他端点）。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.session_store as ss_mod
from config import get_settings
from src.agent.session_log import ASSISTANT_MSG, TURN_START, USER_MSG, SessionLog
from src.constants import LOCAL_USER
from src.session_store import list_sessions, load_session, save_session
from src.storage.sqlite_provider import SQLiteProvider
from web_fastapi import worker_process as wp
from web_fastapi.routers import sessions as sessions_router


# ════════════════════════════════════════════════════════════════
# 公共夹具（与 test_cold_slot_hydration 同款）
# ════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _capture_send(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(wp, "_send", lambda msg, **k: sent.append(msg))
    yield sent


@pytest.fixture(autouse=True)
def _no_project_lookup(monkeypatch):
    """桶创建/stub 保存时的项目归属读取不碰真实存储（测试隔离，绑 inbox）。"""
    monkeypatch.setattr(wp.WorkerState, "_get_active_project", lambda self: "")


@pytest.fixture(autouse=True)
def force_persist(monkeypatch):
    monkeypatch.setattr(get_settings(), "session_persist", True, raising=False)


@pytest.fixture
def sessions_root(tmp_path, monkeypatch, request):
    from src.storage import paths
    # P3 起 save/stub 还会经 session_state_store 读写默认库 kv——数据根一并改道
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


class _TurnStartAgent:
    """替身 agent：开轮即写 durable 事件（对齐 agent_v3 开轮时序：
    turn/start → user/message 先落事件库，assistant 稍后），回放一轮增量。"""

    def __init__(self, session_log):
        self._session_log = session_log
        self.seen_histories: list[list] = []

    def set_llm_params(self, **kwargs):
        pass

    def stream_invoke(self, user_id, message, messages, session_id="", **kwargs):
        self.seen_histories.append(list(messages))
        log = self._session_log

        def _gen():
            # 开轮 durable 事件（真实 agent 由循环本体在轮起点写）
            log.append(session_id, TURN_START, {"input": message})
            log.append(session_id, USER_MSG, {"content": message})
            yield {"type": "turn_messages",
                   "messages": [{"role": "user", "content": message},
                                {"role": "assistant", "content": "收到"}]}
            yield {"type": "complete", "content": "收到"}

        return _gen()


def _inflight_state(sid: str, agent) -> wp.WorkerState:
    state = wp.WorkerState(LOCAL_USER, slot=sid)
    state.agent = agent
    return state


def _sessions_app(log=None) -> FastAPI:
    a = FastAPI()
    a.include_router(sessions_router.router, prefix="/api/sessions")
    if log is not None:
        a.state.cordis_ctx = type("_Ctx", (), {"try_get": staticmethod(
            lambda key: log if key == "sessions" else None)})()
    return a


# ════════════════════════════════════════════════════════════════
# (a) 开轮（未轮末保存）后 /api/sessions 列表含会话
# ════════════════════════════════════════════════════════════════

def test_inflight_session_listed_after_turn_start(
        sessions_root, session_log, monkeypatch, _capture_send):
    """开轮即 stub：_op_chat 起跑（轮末保存被冻结模拟生成中）后，列表里
    立即可见本会话（旧实现要等轮末 _save_bucket 才出现）。"""
    sid = "infl0001"
    saved_buckets: list[str] = []
    monkeypatch.setattr(wp.WorkerState, "_save_bucket",
                        lambda self, b: saved_buckets.append(b.session_id))

    state = _inflight_state(sid, _TurnStartAgent(session_log))
    wp._op_chat(state, "r1", {"session_id": sid, "message": "首条消息"})

    # 轮末保存已被冻结（仍在生成中的模拟），stub 已先行落盘
    assert saved_buckets == [sid]
    client = TestClient(_sessions_app())
    r = client.get("/api/sessions")
    assert r.status_code == 200, r.text
    rows = {s["session_id"]: s for s in r.json()["sessions"]}
    assert sid in rows
    # stub 消息 = 既有历史（空）+ 本条 user 消息；空消息 stub 会被
    # list_sessions 的幽灵过滤吃掉，故必须带真实 user 消息
    assert rows[sid]["message_count"] == 1
    assert rows[sid]["preview"] == "首条消息"
    # 侧栏语义：绑 inbox（桶创建时 stamp ""，与 inbox 同义可匹配）
    r2 = client.get("/api/sessions", params={"project": "inbox"})
    assert sid in {s["session_id"] for s in r2.json()["sessions"]}


def test_stub_never_overwrites_existing_snapshot(
        sessions_root, session_log, monkeypatch, _capture_send):
    """ADR-0004-D2：已有快照的会话开轮，stub 是 no-op——盘上历史不被
    「历史 + 本条 user」的最小快照覆盖（生成中崩溃也不丢历史）。"""
    sid = "keep0001"
    save_session(LOCAL_USER, [
        {"role": "user", "content": "旧问"},
        {"role": "assistant", "content": "旧答"},
    ], sid)
    monkeypatch.setattr(wp.WorkerState, "_save_bucket", lambda self, b: None)

    state = _inflight_state(sid, _TurnStartAgent(session_log))
    wp._op_chat(state, "r1", {"session_id": sid, "message": "新消息"})

    msgs, _, _, _ = load_session(LOCAL_USER, sid)
    assert msgs == [{"role": "user", "content": "旧问"},
                    {"role": "assistant", "content": "旧答"}]


# ════════════════════════════════════════════════════════════════
# (b) 事件直读返回 turn/start + user/message
# ════════════════════════════════════════════════════════════════

def test_inflight_events_direct_read_shows_turn_start_and_user_message(
        sessions_root, session_log, monkeypatch, _capture_send):
    """前端零锁直读的数据基础：开轮后 GET /{sid}/events 主进程直读
    SQLite（不经 worker 锁），立即有 turn/start + user/message。"""
    sid = "evts0001"
    monkeypatch.setattr(wp.WorkerState, "_save_bucket", lambda self, b: None)

    state = _inflight_state(sid, _TurnStartAgent(session_log))
    wp._op_chat(state, "r1", {"session_id": sid, "message": "在吗"})

    client = TestClient(_sessions_app(session_log))
    r = client.get(f"/api/sessions/{sid}/events")
    assert r.status_code == 200, r.text
    events = r.json()["events"]
    types = [e["type"] for e in events]
    assert "turn/start" in types
    assert "user/message" in types
    user_ev = next(e for e in events if e["type"] == "user/message")
    assert user_ev["payload"]["content"] == "在吗"
    # 顺序契约：turn/start 先于 user/message（开轮时序）
    assert types.index("turn/start") < types.index("user/message")


# ════════════════════════════════════════════════════════════════
# 轮末权威覆盖：stub 与 _save_bucket 不冲突
# ════════════════════════════════════════════════════════════════

def test_turn_end_snapshot_overwrites_stub_with_full_history(
        sessions_root, session_log, _capture_send):
    """正常收轮：轮末 _save_bucket 整文件覆盖 stub，message_count 回真实
    值（user + assistant），列表不再是开轮态的 1。"""
    sid = "done0001"
    state = _inflight_state(sid, _TurnStartAgent(session_log))
    wp._op_chat(state, "r1", {"session_id": sid, "message": "你好"})

    client = TestClient(_sessions_app())
    rows = {s["session_id"]: s
            for s in client.get("/api/sessions").json()["sessions"]}
    assert rows[sid]["message_count"] == 2
    msgs, _, _, _ = load_session(LOCAL_USER, sid)
    assert msgs == [{"role": "user", "content": "你好"},
                    {"role": "assistant", "content": "收到"}]


# ════════════════════════════════════════════════════════════════
# (c) /current busy 503
# ════════════════════════════════════════════════════════════════

class _BusyWorker:
    def send(self, op, **kwargs):
        raise TimeoutError(
            f"worker {LOCAL_USER} 忙（chat 进行中？），op={op} 超过 5s 未拿到锁")


class _BusyManager:
    def __init__(self):
        self.slots = {"busyAAAA": _BusyWorker()}

    def lookup(self, user_id, sid):
        return self.slots.get(sid)

    def get_or_create(self, user_id, slot=None):
        raise AssertionError("管理端点不得 spawn（_worker_for 契约）")


def test_current_session_busy_returns_503(sessions_root):
    """/current 生成中撞 worker 锁 5s 超时 → 503（旧实现 500），前端按
    瞬时失败走恢复轮询而非当成致命错误。"""
    client = TestClient(_sessions_app())
    client.app.state.worker_manager = _BusyManager()
    r = client.get("/api/sessions/current", params={"session_id": "busyAAAA"})
    assert r.status_code == 503, r.text
    assert "current_session" in r.json()["detail"]
