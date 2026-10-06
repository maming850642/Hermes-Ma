"""projects 数据层（ADR-0005 D1/D2，M2）行为锁定。

覆盖：schema v1→v2 迁移、ProjectStore CRUD/slug 规则/inbox 保护/激活指针、
会话归属一次性绑定 stamping、list_sessions 项目过滤、activate API 集成。
"""
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.session_store as ss_mod
from src.agent.session_log import SessionLog
from src.constants import LOCAL_USER
from src.session_store import (
    list_sessions,
    load_session,
    read_session_meta,
    save_session,
)
from src.storage.projects_store import (
    INBOX_SLUG,
    ProjectError,
    ProjectStore,
    default_slug_for,
    get_active_project,
    validate_slug,
)
from src.storage.sqlite_provider import SQLiteProvider
from src.workspace.service import WorkspaceService


# ── fixtures ──

@pytest.fixture
def provider(tmp_path):
    p = SQLiteProvider(tmp_path / "proj.db")
    yield p
    p.close()


@pytest.fixture
def sessions_root(tmp_path, monkeypatch, request):
    from src.storage import paths
    # P3 起 save/load 还会经 session_state_store 读写默认库 kv——数据根一并改道
    paths.set_data_root(tmp_path)
    request.addfinalizer(lambda: paths.set_data_root(None))
    root = tmp_path / "sessions"
    monkeypatch.setattr(ss_mod, "SESSIONS_DIR", root)
    return root


@pytest.fixture(autouse=True)
def force_persist(monkeypatch):
    from config import get_settings
    monkeypatch.setattr(get_settings(), "session_persist", True, raising=False)


# ── schema 迁移 ──

_V1_DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS memories(
    id TEXT PRIMARY KEY, content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'legacy',
    created_at REAL NOT NULL, updated_at REAL);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL, payload TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS kv(
    scope TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    updated_at REAL NOT NULL, PRIMARY KEY(scope, key));
CREATE TABLE IF NOT EXISTS snapshots(
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
    label TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
"""


def _make_v1_db(path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(_V1_DDL)
    conn.execute("INSERT INTO meta VALUES('schema_version','1')")
    conn.execute(
        "INSERT INTO memories(id,content,source,created_at) "
        "VALUES('m1','旧记忆','user',1.0)")
    conn.commit()
    conn.close()


def test_fresh_db_is_current_with_projects_table(tmp_path):
    p = SQLiteProvider(tmp_path / "fresh.db")
    try:
        ver = p.query("SELECT value FROM meta WHERE key='schema_version'")
        # v6：v2(projects) + v3(memories.project) + v4(llm_usage) 
        #     + v5(详情三列) + v6(tools 列)
        assert ver[0]["value"] == "6"
        # projects 表可写可查
        p.execute(
            "INSERT INTO projects(slug,name,type,created_at,last_opened_at) "
            "VALUES('a','A','hosted',1.0,1.0)")
        assert p.query("SELECT COUNT(*) AS c FROM projects")[0]["c"] == 1
    finally:
        p.close()


def test_legacy_v1_db_upgrades_to_current(tmp_path):
    db = tmp_path / "legacy.db"
    _make_v1_db(db)
    p = SQLiteProvider(db)
    try:
        assert p.query("SELECT value FROM meta WHERE key='schema_version'")[0]["value"] == "6"
        mem = p.query("SELECT content FROM memories WHERE id='m1'")
        assert mem[0]["content"] == "旧记忆"          # 数据无损
        p.query("SELECT 1 FROM projects LIMIT 1")      # 新表存在
    finally:
        p.close()


def test_unknown_future_version_rejected(tmp_path):
    db = tmp_path / "future.db"
    _make_v1_db(db)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError):
        SQLiteProvider(db)


# ── slug 与 CRUD ──

@pytest.mark.parametrize("ok", ["abc", "a", "p-1a2b3c", "blog.pro", "x_y-z9"])
def test_validate_slug_accepts(ok):
    assert validate_slug(ok) == ok


@pytest.mark.parametrize("bad", ["", "-lead", ".dot", "Big", "has space",
                                 "co:lon", "sl/ash", "x" * 65])
def test_validate_slug_rejects(bad):
    with pytest.raises(ProjectError):
        validate_slug(bad)


def test_default_slug_for_names():
    assert default_slug_for("My Cool-App.py") == "my-cool-app.py"
    generated_cn = default_slug_for("博客空间")
    assert generated_cn.startswith("p-") and len(generated_cn) == len("p-") + 6
    assert default_slug_for("")  # 非空即可（随机 slug）


def test_create_hosted_auto_dedupe_and_reserved_inbox(provider):
    store = ProjectStore(provider)
    store.ensure_default()
    store.ensure_default()                              # 幂等
    assert store.get(INBOX_SLUG)["type"] == "inbox"

    a = store.create("我的项目", slug=None)
    b = store.create("我的项目", slug=None)             # 中文名两次独立随机 slug
    assert a["slug"].startswith("p-")
    assert a["type"] == "hosted" and a["path"]          # hosted 自动托管路径
    assert b["slug"] != a["slug"]

    first = store.create("Dup 项目", slug="dupcase")
    assert first["slug"] == "dupcase"
    with pytest.raises(ProjectError):
        store.create("再来一个", slug="dupcase")
    # unique_slug 路径产出 -2 后缀
    next_free = store.unique_slug("dupcase")
    assert next_free.startswith("dupcase-")

    with pytest.raises(ProjectError):
        store.delete(INBOX_SLUG)                        # 内建收件箱受保护


def test_active_pointer_and_touch_ordering(provider):
    store = ProjectStore(provider)
    store.ensure_default()
    assert store.active_slug() == ""
    x = store.create("老项目")
    y = store.create("新项目")
    # 显式时间戳排序（Windows 墙钟精度下 time.time()+sleep 可能同值，不可依赖）
    provider.execute("UPDATE projects SET last_opened_at=? WHERE slug=?", (2_000_000_000.0, x["slug"]))
    provider.execute("UPDATE projects SET last_opened_at=? WHERE slug=?", (3_000_000_000.0, y["slug"]))
    provider.execute("UPDATE projects SET last_opened_at=? WHERE slug=?", (1_000_000_000.0, INBOX_SLUG))
    assert [r["slug"] for r in store.list()] == [y["slug"], x["slug"], INBOX_SLUG]

    store.set_active(y["slug"])
    assert store.active_slug() == y["slug"]
    store.delete(y["slug"])
    assert store.active_slug() == ""                    # 删除激活项清空指针


# ── 会话归属 stamping / 过滤 ──

def test_project_binding_stamped_once(sessions_root, provider):
    store = ProjectStore(provider)
    store.ensure_default()
    store.set_active("blog")

    assert get_active_project(provider) == "blog"
    save_session(LOCAL_USER, [{"role": "user", "content": "第一条"}], "sid1",
                 project=get_active_project(provider))
    assert read_session_meta(LOCAL_USER, "sid1")["project"] == "blog"

    # 用户切走后继续聊旧会话：归属保持不漂移（快照是全量保存，传累积消息）
    store.set_active("other")
    save_session(LOCAL_USER, [
        {"role": "user", "content": "第一条"},
        {"role": "user", "content": "第二条"},
    ], "sid1", project=get_active_project(provider))
    assert read_session_meta(LOCAL_USER, "sid1")["project"] == "blog"

    # 未绑定的旧式保存：字段为空串＝收件箱语义
    save_session(LOCAL_USER, [{"role": "user", "content": "随手问"}], "sid2")
    assert read_session_meta(LOCAL_USER, "sid2")["project"] == ""

    msgs, todos, vfs, waker = load_session(LOCAL_USER, "sid1")
    assert [m["content"] for m in msgs] == ["第一条", "第二条"]


def test_list_sessions_project_filter(sessions_root, provider):
    store = ProjectStore(provider)
    save_session(LOCAL_USER, [{"role": "user", "content": "blog 的"}], "b1",
                 name="b", waker="", project="blog")
    save_session(LOCAL_USER, [{"role": "user", "content": "未归类的"}], "i1",
                 name="i")
    save_session(LOCAL_USER, [], "ghost-blog", project="blog")   # 空快照被过滤

    blog_ids = [s["session_id"] for s in list_sessions(LOCAL_USER, project="blog")]
    assert blog_ids == ["b1"]
    inbox_ids = [s["session_id"] for s in list_sessions(LOCAL_USER, project=INBOX_SLUG)]
    assert inbox_ids == ["i1"]
    unfiltered = list_sessions(LOCAL_USER)
    assert {s["session_id"] for s in unfiltered} == {"b1", "i1"}
    assert all("project" in s for s in unfiltered)


def test_read_session_meta_missing_or_invalid(sessions_root):
    assert read_session_meta(LOCAL_USER, "nope") is None
    target = sessions_root / LOCAL_USER
    target.mkdir(parents=True)
    (target / "broken.json").write_text("{oops", encoding="utf-8")
    assert read_session_meta(LOCAL_USER, "broken") is None


# ── activate/delete API 集成 ──

class _StubCtx:
    def __init__(self, mapping):
        self._mapping = mapping

    def try_get(self, name):
        return self._mapping.get(name)


@pytest.fixture
def api_client(tmp_path, provider, monkeypatch):
    """组装最小 FastAPI：覆盖鉴权依赖 + 注入组合根替身。"""
    from web_fastapi.dependencies import get_current_user_id
    from web_fastapi.routers import projects as proj_router

    storage = provider
    ws = WorkspaceService(storage)

    app = FastAPI()
    app.state.cordis_ctx = _StubCtx({"storage": storage, "workspace": ws})
    app.include_router(proj_router.router, prefix="/api/projects")

    app.dependency_overrides[get_current_user_id] = lambda: LOCAL_USER
    return TestClient(app), ws, store_from(app)


def store_from(app: FastAPI) -> ProjectStore:
    return ProjectStore(app.state.cordis_ctx.try_get("storage"))


def test_projects_api_lifecycle(api_client, tmp_path):
    client, ws, store = api_client
    r = client.get("/api/projects")
    assert r.status_code == 200
    body = r.json()
    assert any(p["slug"] == INBOX_SLUG for p in body["projects"])
    assert body["active"] == ""

    # 创建 hosted 项目并激活 → 挂载流水线生效
    r = client.post("/api/projects", json={"name": "Demo 项目"})
    assert r.status_code == 200, r.text
    slug = r.json()["project"]["slug"]
    record = store.get(slug)
    from pathlib import Path
    Path(record["path"]).mkdir(parents=True, exist_ok=True)

    r = client.post(f"/api/projects/{slug}/activate")
    assert r.status_code == 200, r.text
    st = ws.status()
    assert st.is_mounted() and st.path == record["path"]
    assert store.active_slug() == slug

    r = client.post("/api/projects/inbox/activate")
    assert r.status_code == 200
    assert not ws.status().is_mounted()                  # inbox = 仅对话态
    assert store.active_slug() == INBOX_SLUG

    # 删除未激活的旧项目：激活指针不受影响（仍是收件箱）
    r = client.delete(f"/api/projects/{slug}")
    assert r.status_code == 200
    assert store.get(slug) is None
    assert store.active_slug() == INBOX_SLUG

    r = client.delete("/api/projects/inbox")
    assert r.status_code == 400                          # 收件箱不可删
