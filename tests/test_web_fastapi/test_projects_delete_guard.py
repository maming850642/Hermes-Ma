"""F6：DELETE /api/projects/{slug} 会话守卫 + ?force=true 级联删除测试。

不启动完整 create_app（那会 fork worker 子进程），仿 test_projects_layer.py：
仅挂 projects router 的精简 FastAPI app + 注入组合根替身（storage/workspace/
sessions）。快照目录与 SessionLog 库均指向 tmp。

覆盖：
- 项目下有会话、无 force → 409 带会话计数，项目与会话原样保留
- ?force=true → 会话全清（快照 unlink + 事件库 purge）且项目删除
- 级联只清本项目会话（其他项目/inbox 的会话不动）
- 无会话项目直接删（向后兼容，sessions_deleted=0）
- 项目不存在 → 404（先于守卫，即使有孤儿会话挂着该 slug）
- 内建收件箱 → 400（不进会话守卫，语义不变）
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.session_store as ss
from src.agent.session_log import USER_MSG, SessionLog
from src.constants import LOCAL_USER
from src.storage.projects_store import INBOX_SLUG, ProjectStore
from src.storage.sqlite_provider import SQLiteProvider
from src.workspace.service import WorkspaceService
from web_fastapi.dependencies import get_current_user_id
from web_fastapi.routers import projects as proj_router


class _StubCtx:
    def __init__(self, mapping):
        self._mapping = mapping

    def try_get(self, name):
        return self._mapping.get(name)


@pytest.fixture(autouse=True)
def _force_persist(monkeypatch):
    from config import get_settings
    monkeypatch.setattr(get_settings(), "session_persist", True, raising=False)


@pytest.fixture
def env(tmp_path, monkeypatch, request):
    """精简 app + tmp 化的快照目录/SQLite 库。"""
    from types import SimpleNamespace

    from src.storage import paths
    # P3 起 save/级联删除还会经 session_state_store 读写默认库 kv——数据根一并改道
    paths.set_data_root(tmp_path)
    request.addfinalizer(lambda: paths.set_data_root(None))
    monkeypatch.setattr(ss, "SESSIONS_DIR", tmp_path / "sessions")
    storage = SQLiteProvider(db_path=tmp_path / "proj.db")
    events_provider = SQLiteProvider(db_path=tmp_path / "events.db")
    log = SessionLog(provider=events_provider)

    app = FastAPI()
    app.state.cordis_ctx = _StubCtx({
        "storage": storage,
        "workspace": WorkspaceService(storage),
        "sessions": log,
    })
    app.include_router(proj_router.router, prefix="/api/projects")
    app.dependency_overrides[get_current_user_id] = lambda: LOCAL_USER
    client = TestClient(app)
    try:
        yield SimpleNamespace(app=app, client=client,
                              store=ProjectStore(storage), log=log)
    finally:
        storage.close()
        events_provider.close()


def _mk_project(env, name="Demo 项目") -> str:
    r = env.client.post("/api/projects", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["project"]["slug"]


def _mk_session(env, sid: str, project: str):
    ss.save_session(LOCAL_USER, [{"role": "user", "content": f"会话 {sid}"}], sid,
                    project=project)
    env.log.append(sid, USER_MSG, {"content": f"会话 {sid}"})


# ============================================
# 守卫：有会话无 force → 409
# ============================================
def test_delete_with_sessions_no_force_409(env):
    slug = _mk_project(env)
    _mk_session(env, "sess-a", slug)
    _mk_session(env, "sess-b", slug)

    r = env.client.delete(f"/api/projects/{slug}")
    assert r.status_code == 409, r.text
    assert "2" in r.json()["detail"]
    assert "会话" in r.json()["detail"]
    # 拒绝后项目与会话原样保留（不制造孤儿）
    assert env.store.get(slug) is not None
    assert len(ss.list_sessions(LOCAL_USER, project=slug)) == 2


# ============================================
# force 级联：会话全清 + 项目删除
# ============================================
def test_delete_force_cascades_sessions(env):
    slug = _mk_project(env)
    _mk_session(env, "sess-a", slug)
    _mk_session(env, "sess-b", slug)

    r = env.client.delete(f"/api/projects/{slug}?force=true")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "sessions_deleted": 2}

    # 项目没了，会话三连堵全清：快照 unlink + 事件库 purge + 侧栏不可见
    assert env.store.get(slug) is None
    assert ss.list_sessions(LOCAL_USER, project=slug) == []
    assert not ss._get_session_file(LOCAL_USER, "sess-a").exists()
    assert not ss._get_session_file(LOCAL_USER, "sess-b").exists()
    assert env.log.events("sess-a") == []
    assert env.log.events("sess-b") == []


def test_force_cascade_scoped_to_project(env):
    """级联只清本项目会话：其他项目与未绑定（inbox）会话不动。"""
    slug1 = _mk_project(env, "甲项目")
    slug2 = _mk_project(env, "乙项目")
    _mk_session(env, "sess-p1", slug1)
    _mk_session(env, "sess-p2", slug2)
    _mk_session(env, "sess-inbox", "")          # 未绑定 → inbox

    r = env.client.delete(f"/api/projects/{slug1}?force=true")
    assert r.status_code == 200
    assert r.json()["sessions_deleted"] == 1
    assert ss._get_session_file(LOCAL_USER, "sess-p2").exists()
    assert ss._get_session_file(LOCAL_USER, "sess-inbox").exists()
    assert env.log.events("sess-p2")
    assert env.store.get(slug2) is not None


# ============================================
# 向后兼容与契约保持
# ============================================
def test_delete_without_sessions_still_ok(env):
    """无会话项目直接删，不需要 force（既有行为），sessions_deleted=0。"""
    slug = _mk_project(env)
    r = env.client.delete(f"/api/projects/{slug}")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "sessions_deleted": 0}
    assert env.store.get(slug) is None


def test_delete_unknown_project_404_even_with_orphan_sessions(env):
    """项目不存在 → 404 先于守卫（孤儿会话挂着该 slug 也不改判 409）。"""
    _mk_session(env, "orphan-1", "ghost-slug")
    r = env.client.delete("/api/projects/ghost-slug")
    assert r.status_code == 404


def test_delete_inbox_still_400(env):
    """内建收件箱不可删（语义不变，不进会话守卫——即使 inbox 下有会话）。"""
    _mk_session(env, "sess-inbox", "")
    r = env.client.delete(f"/api/projects/{INBOX_SLUG}")
    assert r.status_code == 400
