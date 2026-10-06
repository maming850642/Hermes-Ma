"""
SQLiteProvider 并发测试 —— 双实例同库交替写。

两个 SQLiteProvider（各自连接）指向同一 db 文件，两线程以 barrier 对齐
交替写 memories/events 各 50 次。验证 WAL + busy_timeout + 模块级 RLock
组合下无 "database is locked"，且数据齐全不丢。
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from src.constants import LOCAL_USER
from src.memory.models import Memory
from src.storage.sqlite_provider import SQLiteProvider

N = 50  # 每线程写入次数


def test_wal_mode_enabled(tmp_path):
    """连接确实运行在 WAL 模式（并发能力的前提）。"""
    store = SQLiteProvider(db_path=tmp_path / "wal.db")
    try:
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()["journal_mode"]
        assert mode.lower() == "wal"
    finally:
        store.close()


def test_two_instances_alternating_writes_same_db(tmp_path):
    """双实例同库：交替写 memories/events 各 50 次，无锁异常、数据齐全。"""
    db = tmp_path / "concurrent.db"
    a = SQLiteProvider(db_path=db)
    b = SQLiteProvider(db_path=db)
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def worker(store: SQLiteProvider, tag: str) -> None:
        try:
            for i in range(N):
                barrier.wait(timeout=30)  # 两线程交替对齐，制造写写重叠窗口
                store.upsert(Memory(user_id=LOCAL_USER, id=f"{tag}-{i}", content=f"{tag} 的第 {i} 条记忆"))
                store.append_event("test", f"session-{tag}", "write", {"tag": tag, "i": i})
        except Exception as e:  # noqa: BLE001 —— 记录任意异常（含锁错误）供断言
            errors.append(f"{tag}: {e!r}")

    t1 = threading.Thread(target=worker, args=(a, "a"))
    t2 = threading.Thread(target=worker, args=(b, "b"))
    t1.start()
    t2.start()
    t1.join(timeout=120)
    t2.join(timeout=120)

    assert not t1.is_alive() and not t2.is_alive()
    assert errors == [], f"并发写出现异常: {errors}"

    # 数据齐全：记忆 2*N 条、事件 2*N 条
    mems = a.get_all()
    assert len(mems) == 2 * N
    ids = {m.id for m in mems}
    assert f"a-{N - 1}" in ids and f"b-{N - 1}" in ids

    events = b.iter_events("test")
    assert len(events) == 2 * N
    # id 唯一且升序，payload 完整
    event_ids = [e["id"] for e in events]
    assert event_ids == sorted(event_ids)
    assert len(set(event_ids)) == 2 * N
    payloads = {(e["payload"]["tag"], e["payload"]["i"]) for e in events}
    assert ("a", N - 1) in payloads and ("b", N - 1) in payloads

    a.close()
    b.close()


def test_reopen_persistence(tmp_path):
    """关闭后重开同一 db：数据与 schema 均在（幂等 init）。"""
    db = tmp_path / "persist.db"
    a = SQLiteProvider(db_path=db)
    a.upsert(Memory(user_id=LOCAL_USER, id="m1", content="持久化记忆"))
    a.append_event("test", "s1", "e", {"k": "中文"})
    a.kv_put("cfg", "k", "v")
    a.close()

    b = SQLiteProvider(db_path=db)
    try:
        assert [m.id for m in b.get_all()] == ["m1"]
        assert len(b.iter_events("test")) == 1
        assert b.kv_get("cfg", "k") == "v"
        assert b.last_event_id("test") > 0
    finally:
        b.close()


def test_close_is_idempotent_enough(tmp_path):
    """close 后再 close 不抛异常（sqlite3.close 重复调用自身容忍）。"""
    store = SQLiteProvider(db_path=tmp_path / "close.db")
    store.close()
    store.close()
