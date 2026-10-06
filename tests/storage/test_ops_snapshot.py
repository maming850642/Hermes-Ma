"""P2-6：collect_ops_snapshot（启动 ops 心跳采集）单测。

契约（src/storage/housekeeping.collect_ops_snapshot）：
- 正常路径：返回注入 provider 的 WAL 字节数 + events/memories 行数；
- 单项失败降级：provider 缺 query/wal_size_bytes 入口 → 该键 None，绝不抛异常；
- storage=None：自建默认库连接（随 isolated_data_env 改道 tmp，用完即关）。
"""
from src.storage.housekeeping import collect_ops_snapshot
from src.storage.sqlite_provider import SQLiteProvider


class _DeafProvider:
    """无 query / wal_size_bytes 的最小 provider 替身（模拟自定义降级面）。"""


class _BoomProvider:
    """全入口抛错的 provider 替身（查询失败必须吞掉，返回 None 项）。"""

    def wal_size_bytes(self):
        raise RuntimeError("boom")

    def query(self, sql, params=()):
        raise RuntimeError("boom")


def test_snapshot_counts_events_and_memories(tmp_path):
    provider = SQLiteProvider(tmp_path / "hb.db")
    try:
        provider.append_event("chat", "s1", "user/message", {"content": "hi"})
        provider.append_event("chat", "s1", "turn/start", {})
        provider.execute(
            "INSERT INTO memories(id, content, source, created_at) "
            "VALUES('m1', '内容', 'test', 123.0)")
        snap = collect_ops_snapshot(provider)
        assert snap["events_rows"] == 2
        assert snap["memories_rows"] == 1
        assert isinstance(snap["wal_bytes"], int) and snap["wal_bytes"] >= 0
    finally:
        provider.close()


def test_snapshot_degrades_itemwise_without_raising():
    snap = collect_ops_snapshot(_DeafProvider())
    assert snap == {"wal_bytes": None, "events_rows": None, "memories_rows": None}
    snap = collect_ops_snapshot(_BoomProvider())
    assert snap == {"wal_bytes": None, "events_rows": None, "memories_rows": None}


def test_snapshot_self_constructs_default_db(isolated_data_env):
    """storage=None 时自建默认库（数据根已改道 tmp）：空库计数为 0 而非 None。"""
    snap = collect_ops_snapshot(None)
    assert snap["events_rows"] == 0
    assert snap["memories_rows"] == 0
    # 建库 DDL 本身会写 -wal（字节数不定），但查询必须成功（非 None）
    assert isinstance(snap["wal_bytes"], int) and snap["wal_bytes"] >= 0
