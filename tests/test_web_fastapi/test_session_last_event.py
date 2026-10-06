"""
会话最后事件轻量读（GET /api/sessions/{sid}/last_event + SessionLog.last_event）
与单事务批量写（SessionLog.append_events_bulk）测试。

仿 test_session_events_api.py：不启动完整 create_app，仅挂 sessions router
的精简 FastAPI app + TestClient。SessionLog 用真实实例（SQLiteProvider 指向
tmp 库），经 app.state.cordis_ctx（_FakeCtx）注入；JSON 快照目录重定向到 tmp。

覆盖：
- last_event 方法：空会话 None / 热表末条只含 {type, ts} / 热表空冷归档
  非空取归档末条 / 热表优先于冷归档 / 非 SQLite provider 降级 None
- append_events_bulk：批量写入保序可读、计数正确 / 空列表 0 /
  非 SQLite provider 逐条退化
- last_event 端点：免认证 / 空会话 null / 返回末条 / 非法 sid 400 /
  waker: 冒号拒绝语义与 events 端点一致
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.agent.session_log import ASSISTANT_MSG, TURN_START, USER_MSG, SessionLog
from src.storage import paths
from src.storage.sqlite_provider import SQLiteProvider
from web_fastapi.routers import sessions as sessions_router


class _FakeCtx:
    """替身组合根：try_get("sessions") 返回注入的 SessionLog。"""

    def __init__(self, log):
        self._log = log

    def try_get(self, key):
        return self._log if key == "sessions" else None


def _make_app(tmp_path, monkeypatch) -> tuple[FastAPI, SQLiteProvider]:
    """精简 app：sessions router + cordis_ctx 注入 SessionLog。

    JSON 快照目录与 SQLite 库都重定向到 tmp，不碰真实 data/。
    """
    import src.session_store as ss
    monkeypatch.setattr(ss, "SESSIONS_DIR", tmp_path / "sessions")
    provider = SQLiteProvider(db_path=tmp_path / "db" / "hermes.db")

    a = FastAPI()
    a.include_router(sessions_router.router, prefix="/api/sessions", tags=["sessions"])
    a.state.cordis_ctx = _FakeCtx(SessionLog(provider=provider))
    return a, provider


@pytest.fixture
def app(tmp_path, monkeypatch, request):
    # last_event 端点路由链不触 kv/stub，数据根一并改道保持隔离一致性
    paths.set_data_root(tmp_path)
    request.addfinalizer(lambda: paths.set_data_root(None))
    a, provider = _make_app(tmp_path, monkeypatch)
    yield a
    provider.close()


@pytest.fixture
def client(app):
    return TestClient(app)


def _write_archive(log: SessionLog, sid: str, rows: list[dict]) -> None:
    """手写冷归档 JSONL（与写侧同格式：逐行事件 + 换行，id 升序）。"""
    path = log.archive_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8")


# ============================================
# SessionLog.last_event 方法
# ============================================

def test_last_event_none_for_empty_session(app):
    log = app.state.cordis_ctx.try_get("sessions")
    assert log.last_event("ghost") is None


def test_last_event_returns_hot_last_type_ts_only(app):
    log = app.state.cordis_ctx.try_get("sessions")
    log.append("s1", TURN_START, {"input": "hi"})
    log.append("s1", USER_MSG, {"content": "hi"})
    log.append("s1", ASSISTANT_MSG, {"content": "在的"})   # payload 不应外泄
    last = log.last_event("s1")
    assert last is not None
    assert set(last) == {"type", "ts"}   # 轻量视图：绝不携带 payload/id
    assert last["type"] == ASSISTANT_MSG
    assert last["ts"] > 0


def test_last_event_falls_back_to_archive_last(app):
    """热表空但冷归档非空 → 取归档末条（不整读文件也能拿到最后一条）。"""
    log = app.state.cordis_ctx.try_get("sessions")
    _write_archive(log, "arc1", [
        {"id": 1, "scope": "chat", "session_id": "arc1",
         "type": TURN_START, "payload": {"input": "旧"}, "ts": 100.0},
        {"id": 2, "scope": "chat", "session_id": "arc1",
         "type": "turn/end", "payload": {}, "ts": 200.0},
    ])
    assert log.last_event("arc1") == {"type": "turn/end", "ts": 200.0}


def test_last_event_hot_wins_over_archive(app):
    """热表非空 → 热表末条即全局末条（归档行必然更旧），无视归档内容。"""
    log = app.state.cordis_ctx.try_get("sessions")
    _write_archive(log, "s2", [
        {"id": 999, "scope": "chat", "session_id": "s2",
         "type": USER_MSG, "payload": {"content": "冷区"}, "ts": 1.0},
    ])
    log.append("s2", TURN_START, {"input": "新"})
    last = log.last_event("s2")
    assert last is not None
    assert last["type"] == TURN_START   # 不是冷区的 user/message


def test_last_event_degrades_to_none_on_fake_provider(tmp_path, monkeypatch):
    """非 SQLite provider（无 _conn 内部结构）→ 热查降级 None，保守收敛。"""
    import src.session_store as ss
    monkeypatch.setattr(ss, "SESSIONS_DIR", tmp_path / "sessions")

    class _FakeProvider:
        """最小事件日志替身：只有协议原语，没有 SQLite 连接内部。"""

        def __init__(self):
            self.rows = []

        def append_event(self, scope, session_id, type_, payload):
            self.rows.append((scope, session_id, type_, payload))
            return len(self.rows)

        def iter_events(self, scope, session_id=None, after_id=0):
            return [
                {"id": i + 1, "scope": s, "session_id": sid,
                 "type": t, "payload": p, "ts": 0.0}
                for i, (s, sid, t, p) in enumerate(self.rows)
                if s == scope and (session_id is None or sid == session_id)
                and i + 1 > after_id
            ]

        def last_event_id(self, scope, session_id=None):
            return max((e["id"] for e in self.iter_events(scope, session_id)),
                       default=0)

    log = SessionLog(provider=_FakeProvider())
    log.append("fx", USER_MSG, {"content": "x"})
    assert log.last_event("fx") is None   # 降级：不加载全量、不猜测


# ============================================
# SessionLog.append_events_bulk（fork 批量写）
# ============================================

def test_append_events_bulk_order_payload_and_count(app):
    log = app.state.cordis_ctx.try_get("sessions")
    items = [
        (TURN_START, {"input": "hi"}),
        (USER_MSG, {"content": "hi"}),
        (ASSISTANT_MSG, {"content": "在的", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
        ]}),
    ]
    n = log.append_events_bulk("b1", items)
    assert n == 3
    evs = log.events("b1")
    assert [e["type"] for e in evs] == [TURN_START, USER_MSG, ASSISTANT_MSG]
    assert evs[1]["payload"] == {"content": "hi"}   # payload 原样
    assert evs[2]["payload"]["tool_calls"][0]["id"] == "c1"
    ids = [e["id"] for e in evs]
    assert ids == sorted(ids) and len(set(ids)) == 3   # id 唯一且升序


def test_append_events_bulk_empty_returns_zero(app):
    log = app.state.cordis_ctx.try_get("sessions")
    assert log.append_events_bulk("b2", []) == 0
    assert log.events("b2") == []


def test_append_events_bulk_falls_back_on_fake_provider(tmp_path, monkeypatch):
    """非 SQLite provider → 逐条退化 append，语义等价。"""
    import src.session_store as ss
    monkeypatch.setattr(ss, "SESSIONS_DIR", tmp_path / "sessions")

    class _FakeProvider:
        def __init__(self):
            self.rows = []

        def append_event(self, scope, session_id, type_, payload):
            self.rows.append((scope, session_id, type_, payload))
            return len(self.rows)

        def iter_events(self, scope, session_id=None, after_id=0):
            return [
                {"id": i + 1, "scope": s, "session_id": sid,
                 "type": t, "payload": p, "ts": 0.0}
                for i, (s, sid, t, p) in enumerate(self.rows)
                if s == scope and (session_id is None or sid == session_id)
                and i + 1 > after_id
            ]

        def last_event_id(self, scope, session_id=None):
            return max((e["id"] for e in self.iter_events(scope, session_id)),
                       default=0)

    log = SessionLog(provider=_FakeProvider())
    n = log.append_events_bulk("fx", [(USER_MSG, {"content": "a"}),
                                      (ASSISTANT_MSG, {"content": "b"})])
    assert n == 2
    assert [e["type"] for e in log.events("fx")] == [USER_MSG, ASSISTANT_MSG]


# ============================================
# last_event 端点
# ============================================

def test_last_event_endpoint_public_access(client):
    """免认证：匿名可读（ADR-0005 D5，与 events 端点一致）。"""
    r = client.get("/api/sessions/src1/last_event")
    assert r.status_code == 200
    j = r.json()
    assert j["session_id"] == "src1"
    assert j["last_event"] is None   # 空会话 → null（不可判定 → 前端不重试）


def test_last_event_endpoint_returns_last_after_appends(client, app):
    log = app.state.cordis_ctx.try_get("sessions")
    log.append("src1", TURN_START, {"input": "hi"})
    log.append("src1", USER_MSG, {"content": "hi"})
    r = client.get("/api/sessions/src1/last_event")
    assert r.status_code == 200
    j = r.json()
    assert j["session_id"] == "src1"
    assert j["last_event"]["type"] == USER_MSG
    assert set(j["last_event"]) == {"type", "ts"}


def test_last_event_endpoint_rejects_traversal_session_id(client):
    r = client.get("/api/sessions/..escape/last_event")
    assert r.status_code == 400


def test_last_event_endpoint_rejects_waker_sid(client):
    """sid 含 ":"（waker:/wakerflow:）→ 400，与 events 端点同一拒绝语义。"""
    r = client.get("/api/sessions/waker:x1/last_event")
    assert r.status_code == 400
