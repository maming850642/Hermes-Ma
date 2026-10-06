"""
T9 会话事件流 API（/api/sessions/{sid}/events + /{sid}/fork）测试。

不启动完整 create_app（那会 boot 主进程组合根 + fork worker），仿
test_workspace_api.py：仅挂 sessions router 的精简 FastAPI app +
TestClient。SessionLog 用真实实例（SQLiteProvider 指向 tmp 库），经
app.state.cordis_ctx（_FakeCtx）注入；JSON 快照目录重定向到 tmp。

覆盖：
- events 端点：未登录 401 / 空事件 / 有事件原样返回 / 非法 sid 400
- fork 端点：未登录 401 / 全量复制计数 / after_event_id 截断 /
  fork 后 events 可读且保序 / 快照 stub 出现在 list_sessions / 自定义 name
- 降级安全：app.state.cordis_ctx 缺失时回退进程内 SessionLog()（默认库，
  测试经 set_data_root 指向 tmp）
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.agent.session_log import ASSISTANT_MSG, TURN_START, USER_MSG, SessionLog
from src.constants import LOCAL_USER
from src.storage import paths
from src.storage.sqlite_provider import SQLiteProvider
from web_fastapi.routers import sessions as sessions_router


class _FakeCtx:
    """替身组合根：try_get("sessions") 返回注入的 SessionLog。"""

    def __init__(self, log):
        self._log = log

    def try_get(self, key):
        return self._log if key == "sessions" else None


def _make_app(tmp_path, monkeypatch, with_ctx=True) -> tuple[FastAPI, SQLiteProvider]:
    """精简 app：sessions router +（可选）cordis_ctx 注入 SessionLog。

    JSON 快照目录与 SQLite 库都重定向到 tmp，不碰真实 data/。
    """
    import src.session_store as ss
    monkeypatch.setattr(ss, "SESSIONS_DIR", tmp_path / "sessions")
    provider = SQLiteProvider(db_path=tmp_path / "db" / "hermes.db")

    a = FastAPI()
    a.include_router(sessions_router.router, prefix="/api/sessions", tags=["sessions"])
    if with_ctx:
        a.state.cordis_ctx = _FakeCtx(SessionLog(provider=provider))
    return a, provider


@pytest.fixture
def app(tmp_path, monkeypatch, request):
    from src.storage import paths
    # P3 起 fork/stub/save 还会经 session_state_store 读写默认库 kv——数据根一并改道
    paths.set_data_root(tmp_path)
    request.addfinalizer(lambda: paths.set_data_root(None))
    a, provider = _make_app(tmp_path, monkeypatch)
    yield a
    provider.close()


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def auth_headers(app):
    return {}


def _append_sample_events(log, sid="src1") -> None:
    """写 4 条样例事件（turn/user/assistant/turn），返回后可断言。"""
    log.append(sid, TURN_START, {"input": "hi"})
    log.append(sid, USER_MSG, {"content": "hi"})
    log.append(sid, ASSISTANT_MSG, {"content": "在的"})
    log.append(sid, "turn/end", {"message_count": 2})


# ============================================
# events 端点
# ============================================

def test_events_public_access(client):
    """免认证：匿名也可读事件流（ADR-0005 D5）。"""
    r = client.get("/api/sessions/src1/events")
    assert r.status_code == 200
    assert r.json()["session_id"] == "src1"


def test_fork_public_access(client):
    """免认证：匿名 fork 可用，三元组契约不变。"""
    r = client.post("/api/sessions/src1/fork")
    assert r.status_code == 200
    j = r.json()
    assert set(j) == {"session_id", "event_count", "up_to_event_id"}


def test_events_empty_for_unknown_session(client, auth_headers):
    r = client.get("/api/sessions/ghost/events", headers=auth_headers)
    assert r.status_code == 200
    j = r.json()
    assert j["session_id"] == "ghost"
    assert j["events"] == []


def test_events_returns_session_log_events_as_is(client, auth_headers, app):
    log = app.state.cordis_ctx.try_get("sessions")
    _append_sample_events(log)
    r = client.get("/api/sessions/src1/events", headers=auth_headers)
    assert r.status_code == 200
    j = r.json()
    assert j["session_id"] == "src1"
    assert j["events"] == log.events("src1")  # SessionLog.events 原样
    ids = [e["id"] for e in j["events"]]
    assert ids == sorted(ids)  # id 升序
    assert [e["type"] for e in j["events"]] == [
        "turn/start", "user/message", "assistant/message", "turn/end",
    ]
    assert j["events"][1]["payload"] == {"content": "hi"}


def test_events_rejects_traversal_session_id(client, auth_headers):
    r = client.get("/api/sessions/..escape/events", headers=auth_headers)
    assert r.status_code == 400


# ============================================
# fork 端点
# ============================================

def test_fork_full_copy_count(client, auth_headers, app):
    log = app.state.cordis_ctx.try_get("sessions")
    _append_sample_events(log)
    r = client.post("/api/sessions/src1/fork", headers=auth_headers)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["event_count"] == 4
    assert j["session_id"] != "src1"


def test_fork_after_event_id_exclusive(client, auth_headers, app):
    """R3-19：after_event_id 改排他语义（id < after_event_id）。"""
    log = app.state.cordis_ctx.try_get("sessions")
    _append_sample_events(log)
    second_id = log.events("src1")[1]["id"]  # user/message 的 id
    r = client.post(
        "/api/sessions/src1/fork",
        headers=auth_headers,
        json={"after_event_id": second_id},
    )
    assert r.status_code == 200
    j = r.json()
    assert j["event_count"] == 1  # 仅 turn/start（user/message 被排他截掉）
    new_events = log.events(j["session_id"])
    assert [e["type"] for e in new_events] == ["turn/start"]
    # up_to_event_id = 实际复制到的最后一条源事件 id（复制到新流会重编号）
    assert j["up_to_event_id"] == log.events("src1")[0]["id"]


def test_fork_up_to_event_id_inclusive(client, auth_headers, app):
    """R3-19：up_to_event_id 含端点语义（id <= up_to_event_id）。"""
    log = app.state.cordis_ctx.try_get("sessions")
    _append_sample_events(log)
    second_id = log.events("src1")[1]["id"]
    r = client.post(
        "/api/sessions/src1/fork",
        headers=auth_headers,
        json={"up_to_event_id": second_id},
    )
    assert r.status_code == 200
    j = r.json()
    assert j["event_count"] == 2  # turn/start + user/message（含端点）
    new_events = log.events(j["session_id"])
    assert [e["type"] for e in new_events] == ["turn/start", "user/message"]
    assert new_events[1]["payload"] == {"content": "hi"}  # payload 原样
    assert j["up_to_event_id"] == second_id


def test_fork_events_readable_and_order_preserved(client, auth_headers, app):
    log = app.state.cordis_ctx.try_get("sessions")
    _append_sample_events(log)
    r = client.post("/api/sessions/src1/fork", headers=auth_headers)
    new_sid = r.json()["session_id"]

    # fork 后 events 端点可读，且与源事件逐条同型（type/payload 保序复制）
    j = client.get(f"/api/sessions/{new_sid}/events", headers=auth_headers).json()
    src = log.events("src1")
    assert len(j["events"]) == len(src)
    for copied, origin in zip(j["events"], src):
        assert copied["type"] == origin["type"]
        assert copied["payload"] == origin["payload"]
        assert copied["session_id"] == new_sid  # 归属新 sid
    ids = [e["id"] for e in j["events"]]
    assert ids == sorted(ids)


def test_fork_snapshot_stub_in_list(client, auth_headers, app, tmp_path):
    import src.session_store as ss
    log = app.state.cordis_ctx.try_get("sessions")
    _append_sample_events(log)
    r = client.post("/api/sessions/src1/fork", headers=auth_headers)
    new_sid = r.json()["session_id"]

    entries = {s["session_id"]: s for s in ss.list_sessions(LOCAL_USER)}
    assert new_sid in entries
    assert entries[new_sid]["name"] == "fork:src1"  # 缺省名 fork:{old[:8]}
    # stub 的 messages 是事件投影（user/assistant 两条，turn/* 不投影）
    msgs, _, _, _ = ss.load_session(LOCAL_USER, new_sid)
    assert msgs == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "在的"},
    ]


def test_fork_custom_name_in_body(client, auth_headers, app):
    import src.session_store as ss
    log = app.state.cordis_ctx.try_get("sessions")
    _append_sample_events(log)
    r = client.post(
        "/api/sessions/src1/fork",
        headers=auth_headers,
        json={"name": "我的分支"},
    )
    assert r.status_code == 200
    new_sid = r.json()["session_id"]
    entries = {s["session_id"]: s for s in ss.list_sessions(LOCAL_USER)}
    assert entries[new_sid]["name"] == "我的分支"


def test_fork_rejects_traversal_session_id(client, auth_headers):
    r = client.post("/api/sessions/..escape/fork", headers=auth_headers)
    assert r.status_code == 400


# ============================================
# 降级安全：无 cordis_ctx 时回退进程内 SessionLog()（默认库）
# ============================================

def test_fallback_without_cordis_ctx(tmp_path, monkeypatch):
    """app.state.cordis_ctx 缺失 → 进程内 SessionLog()（默认库，测试重定向 tmp）。"""
    monkeypatch.setattr(paths, "_data_root_override", tmp_path)
    a, provider = _make_app(tmp_path, monkeypatch, with_ctx=False)
    try:
        c = TestClient(a)
        headers = {}
        # 空事件（默认库 tmp 下无该会话）
        j = c.get("/api/sessions/nosid/events", headers=headers).json()
        assert j == {"session_id": "nosid", "events": []}
        # 回退实例也接默认库：append 后可读
        SessionLog().append("nosid", USER_MSG, {"content": "x"})
        j = c.get("/api/sessions/nosid/events", headers=headers).json()
        assert [e["type"] for e in j["events"]] == ["user/message"]
    finally:
        provider.close()
        monkeypatch.setattr(paths, "_data_root_override", None)
