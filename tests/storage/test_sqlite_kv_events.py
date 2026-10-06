"""
SQLiteProvider KV + 事件日志协议测试。

- KV：任意 JSON 值增删改查、scope 隔离
- 事件：append 自增 id、iter_events 的 scope/session_id/after_id 过滤与
  升序、payload 解析回 dict（含中文 round-trip）
所有用例显式传 tmp_path 下的 db 路径。
"""
from __future__ import annotations

import pytest

from src.storage.sqlite_provider import SQLiteProvider


@pytest.fixture()
def store(tmp_path):
    p = SQLiteProvider(db_path=tmp_path / "kv_events.db")
    yield p
    p.close()


# ============================================
# KV
# ============================================


def test_kv_roundtrip_various_types(store):
    values = {
        "str": "hello",
        "cjk": "中文值-北京",
        "int": 42,
        "float": 3.14,
        "list": [1, "二", {"三": 3}],
        "dict": {"a": 1, "nested": {"b": [True, None]}},
        "bool": True,
        "none": None,
    }
    for k, v in values.items():
        store.kv_put("cfg", k, v)
    for k, v in values.items():
        assert store.kv_get("cfg", k) == v


def test_kv_get_default_when_missing(store):
    assert store.kv_get("cfg", "missing") is None
    assert store.kv_get("cfg", "missing", default={"d": 1}) == {"d": 1}


def test_kv_put_overwrites(store):
    store.kv_put("cfg", "k", "v1")
    store.kv_put("cfg", "k", {"v": 2})
    assert store.kv_get("cfg", "k") == {"v": 2}


def test_kv_delete(store):
    store.kv_put("cfg", "k", "v")
    assert store.kv_delete("cfg", "k") is True
    assert store.kv_get("cfg", "k") is None
    assert store.kv_delete("cfg", "k") is False  # 再删不谎报成功


def test_kv_list_and_scope_isolation(store):
    store.kv_put("cfg", "shared", "from-cfg")
    store.kv_put("cfg", "only-cfg", 1)
    store.kv_put("state", "shared", "from-state")

    assert store.kv_list("cfg") == {"shared": "from-cfg", "only-cfg": 1}
    assert store.kv_list("state") == {"shared": "from-state"}
    assert store.kv_list("empty-scope") == {}
    # 同 key 不同 scope 互不串
    assert store.kv_get("cfg", "shared") == "from-cfg"
    assert store.kv_get("state", "shared") == "from-state"


# ============================================
# 事件日志
# ============================================


def test_append_event_returns_increasing_ids(store):
    ids = [store.append_event("memory", "s1", "add", {"i": i}) for i in range(3)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 3
    assert all(isinstance(i, int) for i in ids)


def test_iter_events_scope_filter(store):
    store.append_event("memory", "s1", "add", {"a": 1})
    store.append_event("waker", "s1", "tick", {"b": 2})
    store.append_event("memory", "s2", "delete", {"c": 3})

    events = store.iter_events("memory")
    assert [e["type"] for e in events] == ["add", "delete"]
    assert all(e["scope"] == "memory" for e in events)
    assert store.iter_events("no-such-scope") == []


def test_iter_events_session_filter(store):
    store.append_event("memory", "s1", "e1", {})
    store.append_event("memory", "s2", "e2", {})
    store.append_event("memory", "s1", "e3", {})

    only_s1 = store.iter_events("memory", session_id="s1")
    assert [e["type"] for e in only_s1] == ["e1", "e3"]
    assert all(e["session_id"] == "s1" for e in only_s1)

    # session_id=None → 该 scope 下全部会话
    all_events = store.iter_events("memory")
    assert [e["type"] for e in all_events] == ["e1", "e2", "e3"]


def test_iter_events_after_id(store):
    first = store.append_event("memory", "s1", "e1", {})
    store.append_event("memory", "s1", "e2", {})
    store.append_event("memory", "s1", "e3", {})

    tail = store.iter_events("memory", after_id=first)
    assert [e["type"] for e in tail] == ["e2", "e3"]
    assert all(e["id"] > first for e in tail)
    # after_id=0 → 全部
    assert len(store.iter_events("memory", after_id=0)) == 3


def test_iter_events_ascending_and_payload_parsed(store):
    """iter_events 按 id 升序；payload 解析回 dict（含中文）。"""
    store.append_event("memory", "s1", "add", {"用户": "张三", "tags": ["新", "老"]})
    store.append_event("memory", "s1", "add", {"用户": "李四"})
    events = store.iter_events("memory")
    assert [e["id"] for e in events] == sorted(e["id"] for e in events)
    assert events[0]["payload"] == {"用户": "张三", "tags": ["新", "老"]}
    # 行契约字段齐全
    row = events[0]
    assert set(row.keys()) == {"id", "scope", "session_id", "type", "payload", "ts"}
    assert isinstance(row["ts"], float)


def test_last_event_id(store):
    assert store.last_event_id("memory") == 0  # 空
    store.append_event("memory", "s1", "e1", {})
    second = store.append_event("memory", "s2", "e2", {})
    store.append_event("other", "s1", "e3", {})

    assert store.last_event_id("memory") == second
    assert store.last_event_id("memory", session_id="s1") == second - 1
    assert store.last_event_id("memory", session_id="s2") == second
    assert store.last_event_id("no-such") == 0
