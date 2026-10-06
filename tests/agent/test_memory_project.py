"""记忆按项目隔离：本项目 ∪ 全局（空 project），跨项目不串。"""
from src.constants import LOCAL_USER
from src.memory.models import Memory, memory_visible_in
from src.storage.sqlite_provider import SQLiteProvider


def test_v2_db_migrates_project_column(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    c = sqlite3.connect(str(db))
    c.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE memories(
            id TEXT PRIMARY KEY, content TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'legacy',
            created_at REAL NOT NULL, updated_at REAL);
        INSERT INTO meta VALUES('schema_version', '2');
        INSERT INTO memories VALUES('a', '旧记忆', 'legacy', 1, NULL);
    """)
    c.commit()
    c.close()
    p = SQLiteProvider(db_path=db)
    m = p.get_by_id("a")
    assert m is not None
    assert m.content == "旧记忆"
    assert m.project == ""


def test_memory_visible_in():
    assert memory_visible_in("", "etf") is True
    assert memory_visible_in("etf", "etf") is True
    assert memory_visible_in("etf", "inbox") is False
    assert memory_visible_in("inbox", "etf") is False
    assert memory_visible_in("inbox", "inbox") is True


def test_search_scopes_project(tmp_path):
    p = SQLiteProvider(db_path=tmp_path / "mem.db")
    p.upsert(Memory(user_id=LOCAL_USER, content="全局喜欢 python", project=""))
    p.upsert(Memory(user_id=LOCAL_USER, content="etf 用 wind 终端", project="etf"))
    p.upsert(Memory(user_id=LOCAL_USER, content="收件箱随口备注", project="inbox"))

    etf = [h.memory.content for h in p.search("", limit=10, project="etf")]
    assert any("wind" in c for c in etf)
    assert any("python" in c for c in etf)
    assert not any("随口" in c for c in etf)

    inbox = [h.memory.content for h in p.search("", limit=10, project="inbox")]
    assert any("随口" in c for c in inbox)
    assert any("python" in c for c in inbox)
    assert not any("wind" in c for c in inbox)


def test_candidates_do_not_cross_project(tmp_path):
    p = SQLiteProvider(db_path=tmp_path / "mem.db")
    p.upsert(Memory(user_id=LOCAL_USER, content="只属于 etf 的事实", project="etf"))
    hits = p.search_candidates("事实", limit=5, project="inbox")
    assert hits == [] or all(h.memory.project in ("", "inbox") for h in hits)


def test_get_all_follows_active_project(tmp_path, monkeypatch):
    """管理列表跟顶栏项目走：本项目 ∪ 全局，看不到其他项目。"""
    from src.memory.manager import MemoryManager
    store = SQLiteProvider(db_path=tmp_path / "mem.db", embedder=None)
    mgr = MemoryManager(store=store)
    store.upsert(Memory(user_id=LOCAL_USER, content="全局喜欢 python", project=""))
    store.upsert(Memory(user_id=LOCAL_USER, content="etf 用 wind", project="etf"))
    store.upsert(Memory(user_id=LOCAL_USER, content="收件箱备注", project="inbox"))

    monkeypatch.setattr("src.storage.projects_store.get_active_project", lambda provider=None: "etf")
    texts = [m["memory"] for m in mgr.get_all(LOCAL_USER)]
    assert any("python" in t for t in texts)
    assert any("wind" in t for t in texts)
    assert not any("收件箱" in t for t in texts)

    monkeypatch.setattr("src.storage.projects_store.get_active_project", lambda provider=None: "inbox")
    texts = [m["memory"] for m in mgr.get_all(LOCAL_USER)]
    assert any("python" in t for t in texts)
    assert any("收件箱" in t for t in texts)
    assert not any("wind" in t for t in texts)


def test_delete_all_only_current_project_keeps_global(tmp_path, monkeypatch):
    """项目级清空只删本项目绑定记忆；全局/其他项目保留。"""
    from src.memory.manager import MemoryManager
    store = SQLiteProvider(db_path=tmp_path / "mem.db", embedder=None)
    mgr = MemoryManager(store=store)
    store.upsert(Memory(user_id=LOCAL_USER, content="全局", project=""))
    store.upsert(Memory(user_id=LOCAL_USER, content="etf 专用", project="etf"))
    store.upsert(Memory(user_id=LOCAL_USER, content="inbox 专用", project="inbox"))

    monkeypatch.setattr("src.storage.projects_store.get_active_project", lambda provider=None: "etf")
    assert mgr.delete_all(LOCAL_USER) is True
    left = {m.content: m.project for m in store.get_all()}
    assert "etf 专用" not in left
    assert left.get("全局") == ""
    assert left.get("inbox 专用") == "inbox"
