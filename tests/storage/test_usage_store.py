"""llm_usage 用量记账数据层（D1）测试。

覆盖：
- 迁移：全新库 v5 + llm_usage 表/索引/详情三列就位；手改 version=3 的
  旧库重开 provider → 沿链迁移到 5 且既有数据（memories / llm_usage 行）
  不丢
- UsageStore：record（含 tokens NULL 行、详情三列截断）、summary 按日×
  scope 聚合、records 按 ts 倒序分页、detail 单条全文、purge_before
  保留期删除
- record_usage 失败隔离：存储层抛错只 log.error 绝不冒出（记账不打断业务）
- error 摘要截断 ~200 字
- StorageHousekeeping.run_once 的 180 天保留期清理（注入 provider）
"""
from __future__ import annotations

import logging
import sqlite3
import time

import pytest

from src.storage import paths, usage_store
from src.storage.housekeeping import StorageHousekeeping
from src.storage.sqlite_provider import SQLiteProvider
from src.storage.usage_store import UsageStore


@pytest.fixture
def db(tmp_path, monkeypatch):
    """数据根改道 tmp + usage_store 默认连接缓存清零（前后各一次）。"""
    paths.set_data_root(tmp_path)
    usage_store.reset_default_provider()
    yield tmp_path
    usage_store.reset_default_provider()
    paths.set_data_root(None)


# ============================================
# 表与迁移（schema v3 → v6）
# ============================================

def test_fresh_db_creates_llm_usage_v6(db):
    p = SQLiteProvider()
    tables = {r["name"] for r in p.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "llm_usage" in tables
    indexes = {r["name"] for r in p.query(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_llm_usage_ts" in indexes
    cols = {r[1] for r in p.query("PRAGMA table_info(llm_usage)")}
    assert {"req_messages", "reasoning", "output", "tools"} <= cols
    row = p.query("SELECT value FROM meta WHERE key='schema_version'")[0]
    assert row["value"] == "6"


def test_v3_db_migrates_to_v6_without_data_loss(db):
    # 先建 v5 库并写入两类数据
    p1 = SQLiteProvider()
    from src.constants import LOCAL_USER
    from src.memory.models import Memory
    p1.upsert(Memory(id="m1", user_id=LOCAL_USER, content="旧记忆",
                     source="legacy", created_at=100.0, updated_at=100.0))
    p1.execute(
        "INSERT INTO llm_usage(ts, session_id, tokens_in, tokens_out, status) "
        "VALUES(?, 's1', 5, 6, 'ok')", (time.time(),))
    p1.close()

    # 手动把版本号改回 3（模拟旧版库），重开 provider 应沿链迁到 6
    conn = sqlite3.connect(str(paths.data_dir("hermes.db")))
    conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    conn.commit()
    conn.close()

    p2 = SQLiteProvider()
    row = p2.query("SELECT value FROM meta WHERE key='schema_version'")[0]
    assert row["value"] == "6"
    # 数据不丢；存量行详情三列落空串（历史调用没有留底）
    assert [m.id for m in p2.get_all()] == ["m1"]
    usage = p2.query("SELECT session_id, tokens_in, tokens_out, req_messages "
                     "FROM llm_usage")
    assert len(usage) == 1
    assert usage[0]["session_id"] == "s1"
    assert usage[0]["tokens_in"] == 5
    assert usage[0]["req_messages"] == ""
    p2.close()


def test_unknown_version_still_rejected(db):
    p = SQLiteProvider()  # 先建出带 meta 的库
    p.close()
    conn = sqlite3.connect(str(paths.data_dir("hermes.db")))
    conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="不支持的 schema_version"):
        SQLiteProvider()


# ============================================
# UsageStore CRUD
# ============================================

NOW = time.time()
DAY = 86400.0


def _seed(store: UsageStore):
    store.record(ts=NOW, session_id="s1", scope="chat", caller="main",
                 model="m1", tokens_in=10, tokens_out=20, duration_ms=100.0)
    # 拿不到 usage 的调用：tokens NULL，仍记一行
    store.record(ts=NOW - 60, session_id="s2", scope="waker", caller="employee",
                 model="m1", status="ok")
    # 旧行（保留期外）
    store.record(ts=NOW - 200 * DAY, session_id="s3", scope="chat",
                 caller="main", model="m0", tokens_in=1, tokens_out=1)


def test_record_summary_records_purge(db):
    store = UsageStore(SQLiteProvider())
    _seed(store)

    # summary：窗口内按 日×scope 聚合；NULL tokens 计次不计 token
    rows = store.summary(days=30)
    assert len(rows) == 2
    by_scope = {r["scope"]: r for r in rows}
    assert by_scope["chat"]["tokens_in"] == 10
    assert by_scope["chat"]["tokens_out"] == 20
    assert by_scope["chat"]["calls"] == 1
    assert by_scope["waker"]["tokens_in"] == 0
    assert by_scope["waker"]["calls"] == 1
    # 窗口外的 200 天前行不参与
    assert all(r["scope"] != "" or r["calls"] <= 1 for r in rows)

    # records：ts 倒序分页 + total
    page1 = store.records(page=1, page_size=2)
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    assert page1["items"][0]["session_id"] == "s1"  # 最新在前
    page2 = store.records(page=2, page_size=2)
    assert len(page2["items"]) == 1
    assert page2["items"][0]["session_id"] == "s3"
    # NULL 字段原样返回 None
    null_row = store.records(page=1, page_size=5)["items"][1]
    assert null_row["session_id"] == "s2"
    assert null_row["tokens_in"] is None and null_row["tokens_out"] is None

    # purge_before：只删截止前的旧行
    removed = store.purge_before(NOW - 100 * DAY)
    assert removed == 1
    assert store.records(page=1, page_size=10)["total"] == 2


def test_error_summary_truncated(db):
    store = UsageStore(SQLiteProvider())
    store.record(status="error", error="x" * 500)
    row = store.records(page=1, page_size=1)["items"][0]
    assert len(row["error"]) == 200
    assert row["status"] == "error"


def test_detail_full_row_and_field_clipping(db):
    """detail() 返回单条全文；详情三列超限截断并带省略标记。"""
    store = UsageStore(SQLiteProvider())
    from src.storage.usage_store import (
        OUTPUT_MAX, REQ_MESSAGES_MAX, REASONING_MAX,
    )
    req = '[{"role": "user", "content": "' + "问" * (REQ_MESSAGES_MAX + 100) + '"}]'
    store.record(
        session_id="s1", scope="chat", caller="main", model="m1",
        tokens_in=10, tokens_out=20,
        req_messages=req,
        reasoning="思" * (REASONING_MAX + 100),
        output="答" * (OUTPUT_MAX + 100),
    )
    row_id = store.records(page=1, page_size=1)["items"][0]["id"]
    d = store.detail(row_id)
    assert d["id"] == row_id
    assert d["session_id"] == "s1"
    assert d["req_messages"].endswith("…[已截断]")
    assert len(d["req_messages"]) == REQ_MESSAGES_MAX + len("…[已截断]")
    assert d["reasoning"].endswith("…[已截断]")
    assert len(d["reasoning"]) == REASONING_MAX + len("…[已截断]")
    assert d["output"].endswith("…[已截断]")
    assert len(d["output"]) == OUTPUT_MAX + len("…[已截断]")


def test_detail_defaults_empty_and_missing_none(db):
    store = UsageStore(SQLiteProvider())
    store.record(session_id="s1")   # 不传详情 → 三列空串
    d = store.detail(store.records(page=1, page_size=1)["items"][0]["id"])
    assert d["req_messages"] == "" and d["reasoning"] == "" and d["output"] == ""
    assert store.detail(99999) is None


# ============================================
# 模块级门面 + 失败隔离
# ============================================

def test_record_usage_uses_default_provider(db):
    """不传 provider 时懒加载默认库（数据根改道后落在 tmp hermes.db）。"""
    usage_store.record_usage(session_id="s9", scope="chat", caller="main",
                             model="m1", tokens_in=3, tokens_out=4)
    p = SQLiteProvider()
    rows = p.query("SELECT session_id, tokens_in, tokens_out FROM llm_usage")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s9"
    assert rows[0]["tokens_in"] == 3


def test_record_usage_never_raises(db, caplog):
    """存储层抛错 → 只 log.error，绝不冒出（记账不能打断业务）。"""

    class Boom:
        def execute(self, sql, params=()):
            raise RuntimeError("db gone")

    with caplog.at_level(logging.ERROR, logger="hermes.storage.usage"):
        usage_store.record_usage(provider=Boom(), session_id="s1")
    assert any("LLM 用量记账失败" in r.message for r in caplog.records)


def test_reset_default_provider_closes_and_clears(db):
    usage_store.record_usage(session_id="s1")
    old = usage_store._default_provider
    assert old is not None
    usage_store.reset_default_provider()
    assert usage_store._default_provider is None
    assert usage_store._default_provider_path is None


# ============================================
# StorageHousekeeping 保留期清理
# ============================================

def test_housekeeping_purges_old_usage(db):
    p = SQLiteProvider()
    store = UsageStore(p)
    _seed(store)
    hk = StorageHousekeeping(storage=p, usage_retention_days=180,
                             events_archive_enabled=False)
    summary = hk.run_once()
    assert summary["purged_usage"] == 1
    assert store.records(page=1, page_size=10)["total"] == 2


def test_housekeeping_usage_purge_failure_isolated(db):
    """purge 抛错不影响 run_once 其他步骤（只告警）。"""

    class BadProvider(SQLiteProvider):
        def execute(self, sql, params=()):
            if "DELETE FROM llm_usage" in sql:
                raise RuntimeError("boom")
            return super().execute(sql, params)

    p = BadProvider()
    hk = StorageHousekeeping(storage=p, events_archive_enabled=False)
    summary = hk.run_once()  # 不抛
    assert summary["purged_usage"] == 0
    assert "checkpointed" in summary


def test_summary_model_rows_and_records_model_filter(db):
    """summary 按日×scope×model 分行（模型维度，2026-09-19）；records 支持
    模型过滤且 COUNT 与列表同 WHERE。"""
    store = UsageStore(SQLiteProvider())
    store.record(ts=NOW, session_id="s1", scope="chat", caller="main",
                 model="glm", tokens_in=10, tokens_out=20)
    store.record(ts=NOW, session_id="s1", scope="chat", caller="main",
                 model="ornith", tokens_in=5, tokens_out=7)
    store.record(ts=NOW - DAY, session_id="s2", scope="waker", caller="employee",
                 model="glm", tokens_in=1, tokens_out=2)

    rows = store.summary(days=30)
    chat_rows = [r for r in rows if r["scope"] == "chat"]
    assert {(r["model"], r["tokens_in"]) for r in chat_rows} == {
        ("glm", 10), ("ornith", 5)}
    # 同日同 scope 不同模型是互斥分行——累加不重复计数
    assert sum(r["tokens_in"] for r in rows) == 16

    only_glm = store.records(page=1, page_size=10, model="glm")
    assert only_glm["total"] == 2
    assert all(r["model"] == "glm" for r in only_glm["items"])
    assert store.records(page=1, page_size=10)["total"] == 3
