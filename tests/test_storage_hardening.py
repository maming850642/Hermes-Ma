"""存储硬骨架（ADR-0004 / M1）行为锁定。

覆盖：save_session 原子写、幽灵治理三件套（懒建桶/空桶跳存/列表过滤）、
fork stub 从无到有永不覆盖、删档弹桶、interrupt resolved TTL 清理、
storage housekeeping 单次执行。
"""
import json
import sqlite3

import pytest

import src.session_store as ss_mod
from src.agent.hitl import InterruptSnapshot
from src.agent.session_log import SessionLog
from src.session_store import (
    ensure_session_stub,
    list_sessions,
    save_session,
)
from src.storage.housekeeping import StorageHousekeeping
from src.storage.sqlite_provider import SQLiteProvider


# ── fixtures ──

@pytest.fixture
def sessions_root(tmp_path, monkeypatch, request):
    """把 SESSIONS_DIR 指到临时目录，避免污染真实 data/。

    P3 起 save 还会经 session_state_store 读写默认库 kv——数据根一并改道。
    """
    from src.storage import paths
    paths.set_data_root(tmp_path)
    request.addfinalizer(lambda: paths.set_data_root(None))
    root = tmp_path / "sessions"
    monkeypatch.setattr(ss_mod, "SESSIONS_DIR", root)
    return root


@pytest.fixture(autouse=True)
def force_persist(monkeypatch):
    """持久化开关强制打开（防宿主配置关闭导致测试静默空转）。"""
    from config import get_settings
    monkeypatch.setattr(get_settings(), "session_persist", True, raising=False)


# ── 原子写（D1）──

def test_atomic_write_keeps_old_on_dump_failure(sessions_root, monkeypatch):
    """json.dump 中途抛错（模拟断电）时：旧文件完好、无 .tmp 残留。"""
    save_session("u1", [{"role": "user", "content": "旧消息"}], "abc")
    path = sessions_root / "u1" / "abc.json"

    real_dump = ss_mod.json.dump

    def flaky(obj, *args, **kwargs):
        msgs = obj.get("messages") or []
        if msgs and msgs[0].get("content") == "新消息":
            raise RuntimeError("模拟断电")
        return real_dump(obj, *args, **kwargs)

    monkeypatch.setattr(ss_mod.json, "dump", flaky)
    with pytest.raises(RuntimeError):
        save_session("u1", [{"role": "user", "content": "新消息"}], "abc")

    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["messages"][0]["content"] == "旧消息"
    assert list((sessions_root / "u1").glob("*.tmp")) == []


# ── fork stub（D2）──

def test_ensure_stub_creates_only_when_absent(sessions_root):
    assert ensure_session_stub(
        "u1", [{"role": "user", "content": "hi"}], "f1", name="fork:x") is True
    saved = json.loads((sessions_root / "u1" / "f1.json").read_text(encoding="utf-8"))
    assert saved["name"] == "fork:x"

    # 权威文件已存在 → 返回 False 且内容不被旁路覆盖
    assert ensure_session_stub("u1", [], "f1", name="evil-stub") is False
    again = json.loads((sessions_root / "u1" / "f1.json").read_text(encoding="utf-8"))
    assert again["name"] == "fork:x"


# ── 幽灵治理（D4）──

def test_list_sessions_skips_empty_by_default(sessions_root):
    save_session("u1", [{"role": "user", "content": "真实会话"}], "s1")
    save_session("u1", [], "ghost")  # 存量空快照

    assert {s["session_id"] for s in list_sessions("u1")} == {"s1"}
    both = list_sessions("u1", include_empty=True)
    assert {s["session_id"] for s in both} == {"s1", "ghost"}


def test_workerstate_lazy_bucket_no_startup_materialization():
    from web_fastapi.worker_process import WorkerState
    st = WorkerState("t-user")
    assert st.current_sid not in st._buckets          # 启动不预建内存桶
    bucket = st.get_bucket(st.current_sid)             # 首次访问懒创建
    assert st._buckets[st.current_sid] is bucket


def test_startup_shutdown_cycle_leaves_no_ghost(sessions_root):
    from web_fastapi.worker_process import WorkerState
    for _ in range(5):
        st = WorkerState("local")
        st.save_all_buckets()  # 模拟统一收尾
    assert not (sessions_root / "local").exists() or \
        not list((sessions_root / "local").glob("*.json"))


def test_save_bucket_skips_empty_but_saves_real(sessions_root):
    from web_fastapi.worker_process import SessionBucket, WorkerState
    st = WorkerState("local")

    empty = SessionBucket("e1")
    st._buckets["e1"] = empty
    st._save_bucket(empty)
    assert not (sessions_root / "local" / "e1.json").exists()

    filled = SessionBucket("r1")
    filled.messages = [{"role": "user", "content": "hello"}]
    st._buckets["r1"] = filled
    st._save_bucket(filled)
    assert (sessions_root / "local" / "r1.json").exists()


def test_drop_bucket_removes_memory_state():
    from web_fastapi.worker_process import SessionBucket, WorkerState
    st = WorkerState("local")
    st._buckets["dead"] = SessionBucket("dead")
    st.drop_bucket("dead")
    st.drop_bucket("never-existed")  # 幂等
    assert "dead" not in st._buckets


# ── interrupt resolved TTL（D3）──

def _snapshot(tid: str) -> InterruptSnapshot:
    return InterruptSnapshot.create(
        thread_id=tid, messages=[], pending_args={},
        tool_call_id="", tool_name="bash", payload={},
        permission_mode="before_changes",
    )


def _age_interrupt_thread(provider: SQLiteProvider, tid: str, days: int) -> None:
    conn = sqlite3.connect(str(provider.db_path))
    try:
        conn.execute(
            "UPDATE events SET ts = ts - ? WHERE scope='interrupt' AND session_id=?",
            (days * 86400, tid),
        )
        conn.commit()
    finally:
        conn.close()


def _count_interrupt_rows(provider: SQLiteProvider) -> int:
    conn = sqlite3.connect(str(provider.db_path))
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM events WHERE scope='interrupt'").fetchone()[0])
    finally:
        conn.close()


def test_purge_resolved_only_removes_stale_completed_threads(tmp_path):
    provider = SQLiteProvider(tmp_path / "t.db")
    try:
        log = SessionLog(provider)
        log.save_interrupt(_snapshot("done-old"))
        log.resolve_interrupt("done-old", "approve")     # 陈旧已完结 → 该删
        log.save_interrupt(_snapshot("pending-old"))      # 陈旧但仍 pending → 保
        log.save_interrupt(_snapshot("fresh-done"))
        log.resolve_interrupt("fresh-done", "reject")     # 未过期已完结 → 保
        _age_interrupt_thread(provider, "done-old", days=30)
        _age_interrupt_thread(provider, "pending-old", days=90)

        removed = provider.purge_resolved_interrupts(max_age_days=7)

        assert removed == 2  # 只删 done-old 的 requested+resolved 两行
        remaining = {
            r for r in ("done-old", "pending-old", "fresh-done")
        }
        conn = sqlite3.connect(str(provider.db_path))
        try:
            left = {row[0] for row in conn.execute(
                "SELECT DISTINCT session_id FROM events WHERE scope='interrupt'")}
        finally:
            conn.close()
        assert "done-old" not in left
        assert left == remaining - {"done-old"}
    finally:
        provider.close()


def test_checkpoint_wal_smoke(tmp_path):
    provider = SQLiteProvider(tmp_path / "c.db")
    try:
        provider.append_event("chat", "x", "user/message", {"content": "hi"})
        assert provider.wal_size_bytes() >= 0
        provider.checkpoint_wal()            # 默认 TRUNCATE
        provider.checkpoint_wal("PASSIVE")
    finally:
        provider.close()


class _FakeSchedule:
    """SchedulerService 最小替身：记录注册，不真正调度。"""

    def __init__(self):
        self.registered = {}

    def register(self, registrant_id, tick_seconds, fn, *, max_workers=1):
        self.registered[registrant_id] = fn
        return lambda: self.registered.pop(registrant_id, None)

    def stop(self, timeout: float = 5.0) -> None:
        self.registered.clear()


def test_housekeeping_run_once(tmp_path):
    provider = SQLiteProvider(tmp_path / "h.db")
    sched = _FakeSchedule()
    hk = StorageHousekeeping(storage=provider, schedule=sched,
                             interrupt_ttl_days=7)
    hk.start()
    assert "storage_housekeeping" in sched.registered

    log = SessionLog(provider)
    log.save_interrupt(_snapshot("old-done"))
    log.resolve_interrupt("old-done", "approve")
    _age_interrupt_thread(provider, "old-done", days=30)

    summary = hk.run_once()

    assert summary["purged_events"] == 2
    assert summary["checkpointed"] in (True, False)  # 小库 wal 可能为 0 字节
    hk.stop()
    assert "storage_housekeeping" not in sched.registered
    provider.close()
