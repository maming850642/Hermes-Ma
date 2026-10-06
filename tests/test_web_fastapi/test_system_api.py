"""系统信息 API（/api/health 版本字段 + /api/stats 统计端点）测试。

不启动完整 create_app（那会 boot 组合根 + fork worker），仿
test_waker_api.py / test_session_events_api.py：仅挂 system router 的
精简 FastAPI app + TestClient。

- /api/health：worker 替身（记 send 调用，不真跑 IPC），校验 router 层
  叠加的 name/version/code_dir 三字段且 ok 语义不被篡改。
- /api/stats：SQLiteProvider 指向 tmp 库经 _FakeCtx 注入；waker 用真实
  WakerStore（workspace_root 指向 tmp）；降级路径不碰真实 data/。
"""
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.storage import paths
from src.storage.sqlite_provider import SQLiteProvider
from src.version import __version__
from src.waker.models import WakerConfig
from src.waker.store import WakerStore
from web_fastapi.routers import system as system_router

PROJECT_ROOT = Path(system_router.__file__).resolve().parents[2]


# ============================================
# 替身
# ============================================
class FakeWorker:
    """替身 WorkerProcess：只记录 send 的 op，health 返回固定数据。"""

    def __init__(self, health_data):
        self.health_data = health_data
        self.ops = []

    def send(self, op, **kwargs):
        self.ops.append(op)
        if op == "health":
            return [{"type": "health", "data": dict(self.health_data)}]
        raise AssertionError(f"未预期的 IPC op: {op}")


class _FakeCtx:
    """替身组合根：try_get("storage") 返回注入的 provider。"""

    def __init__(self, provider):
        self._provider = provider

    def try_get(self, key):
        return self._provider if key == "storage" else None


class BrokenProvider:
    """query 一律抛异常的存储替身：验证 /api/stats 零值兜底不 500。"""

    def query(self, sql, params=()):
        raise RuntimeError("boom")

    def close(self):
        pass


def _make_app(worker_manager=None, cordis_ctx=None, workspace_root=""):
    a = FastAPI()
    a.include_router(system_router.router, prefix="/api", tags=["system"])
    if worker_manager is not None:
        a.state.worker_manager = worker_manager
    if cordis_ctx is not None:
        a.state.cordis_ctx = cordis_ctx
    if workspace_root:
        a.state.workspace_root = workspace_root
    return a


# ============================================
# /api/health：版本可见性三元组
# ============================================
def test_health_injects_name_version_code_dir():
    worker = FakeWorker({"ok": True})
    client = TestClient(_make_app(worker_manager=type("WM", (), {
        "get_or_create": staticmethod(lambda uid: worker),
    })()))
    r = client.get("/api/health")
    assert r.status_code == 200
    j = r.json()
    # worker 结果原样保留（ok 语义不变）
    assert j["ok"] is True
    # router 层叠加的版本可见性字段
    assert j["name"] == "hermes-ma"
    assert j["version"] == __version__
    assert j["code_dir"] == str(PROJECT_ROOT)
    # 只发了一次 health IPC，版本三元组不经 worker
    assert worker.ops == ["health"]


def test_health_worker_unhealthy_keeps_ok_false():
    worker = FakeWorker({"ok": False})
    client = TestClient(_make_app(worker_manager=type("WM", (), {
        "get_or_create": staticmethod(lambda uid: worker),
    })()))
    j = client.get("/api/health").json()
    assert j["ok"] is False
    assert j["version"] == __version__


# ============================================
# /api/stats：聚合 + waker 概览
# ============================================
def _seed_events(provider: SQLiteProvider) -> None:
    """写样例 events：2 会话（1 活跃 1 过期）+ 3 条 llm/error（2 新 1 旧）。"""
    now = time.time()
    rows = [
        ("chat", "s-live", "turn/start", now),
        ("chat", "s-live", "user", now),
        ("chat", "s-old", "turn/start", now - 2 * 86400),
        ("chat", "s-old", "llm/error", now),
        ("chat", "s-live", "llm/error", now - 100),
        ("", "s-old", "llm/error", now - 2 * 86400),  # 旧错误，24h 外
    ]
    for scope, sid, typ, ts in rows:
        provider.execute(
            "INSERT INTO events(scope, session_id, type, payload, ts) "
            "VALUES(?,?,?,?,?)",
            (scope, sid, typ, "{}", ts),
        )
    provider.execute(
        "INSERT INTO memories(id, content, source, created_at) VALUES(?,?,?,?)",
        ("m1", "一条记忆", "legacy", now),
    )


@pytest.fixture
def seeded_app(tmp_path):
    provider = SQLiteProvider(db_path=tmp_path / "db" / "hermes.db")
    _seed_events(provider)
    a = _make_app(cordis_ctx=_FakeCtx(provider), workspace_root=str(tmp_path))
    yield a, provider, tmp_path
    provider.close()


def test_stats_aggregates_storage_and_wakers(seeded_app):
    a, provider, tmp_path = seeded_app
    # 两个 waker：一个启用 + ok 状态，一个默认禁用（last_status 空 → none）
    store = WakerStore("local", workspace_root=str(tmp_path))
    w1 = store.create(WakerConfig(name="w1"))
    w1.last_status = "ok"
    store.save_state(w1)
    store.set_enabled("w1", True)
    store.create(WakerConfig(name="w2"))

    client = TestClient(a)
    r = client.get("/api/stats")
    assert r.status_code == 200
    j = r.json()
    # s-live + s-old（scope=chat 且 sid 非空去重）
    assert j["sessions"]["total"] == 2
    # 只有 s-live 在 24h 内有 turn/start
    assert j["sessions"]["active_24h"] == 1
    assert j["llm_errors"]["total"] == 3
    assert j["llm_errors"]["last_24h"] == 2
    assert j["storage"]["events_rows"] == 6
    assert j["storage"]["memories_rows"] == 1
    assert j["wakers"]["total"] == 2
    assert j["wakers"]["enabled"] == 1
    assert j["wakers"]["status"] == {"ok": 1, "none": 1}


def test_stats_zero_fallback_when_storage_broken(tmp_path):
    """query 全炸 → 存储块零值兜底仍 200；waker 块独立照常计算。"""
    a = _make_app(cordis_ctx=_FakeCtx(BrokenProvider()),
                  workspace_root=str(tmp_path))
    r = TestClient(a).get("/api/stats")
    assert r.status_code == 200
    j = r.json()
    assert j["sessions"] == {"total": 0, "active_24h": 0}
    assert j["llm_errors"] == {"total": 0, "last_24h": 0}
    assert j["storage"] == {"events_rows": 0, "memories_rows": 0}
    assert j["wakers"] == {"total": 0, "enabled": 0, "status": {}}


def test_stats_fallback_default_db_redirects_to_tmp(tmp_path, monkeypatch):
    """无 cordis_ctx → 回退 SQLiteProvider()（默认路径）。数据根改道 tmp，
    不碰真实 data/hermes.db；全新库应全零。"""
    monkeypatch.setattr(paths, "_data_root_override", tmp_path)
    a = _make_app(workspace_root=str(tmp_path))
    r = TestClient(a).get("/api/stats")
    assert r.status_code == 200
    j = r.json()
    assert j["sessions"]["total"] == 0
    assert j["storage"]["events_rows"] == 0
    assert j["wakers"]["total"] == 0
