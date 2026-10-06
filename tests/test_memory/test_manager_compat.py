"""
MemoryManager 接口兼容性测试（M0-2：文件后端时代的安全网）。

验证 MemoryManager 仍满足 cli.py/web.py/graph.py 的接口契约：
- remember_fact 返回 {success, events, item_count}，events 元素是 ADD/UPDATE/NOOP/DELETE
- search_with_detail 返回 {raw_results, filtered_results, raw_count, hit_count}
- get_all 返回 list[dict]，元素含 memory/id
- delete_all 返回 bool

store 用旧文件后端（P3-5 已退役到 scripts/legacy_memory_backend.py，
此处兼作退役位置回归保护；manager 与 store 只走协议面），
extractor/decider mock。
"""
import time

import pytest
from unittest.mock import MagicMock

from src.memory import MemoryManager
from src.memory.models import Decision
from scripts.legacy_memory_backend import FileMemoryProvider, FileMemoryStore


def _make_manager_with_filestore(tmp_path):
    """构造 MemoryManager：注入旧文件后端（scripts/legacy_memory_backend），
    extractor/decider mock。

    返回 manager。不再需要 collection 清理（文件随 tmp_path 自动销毁）。
    """
    from config import get_settings
    s = get_settings()

    mgr = MemoryManager.__new__(MemoryManager)
    mgr.store = FileMemoryProvider(FileMemoryStore(workspace_root=str(tmp_path)))
    mgr._settings = s
    mgr.extractor = MagicMock()
    mgr.decider = MagicMock()
    return mgr


def test_remember_fact_returns_compat_shape(tmp_path):
    """remember_fact 返回 {success, events, item_count}。"""
    mgr = _make_manager_with_filestore(tmp_path)
    mgr.decider.decide = MagicMock(return_value=[Decision(action="ADD", content="用户叫小明")])
    result = mgr.remember_fact("alice", "用户叫小明")
    assert set(result.keys()) >= {"success", "events", "item_count"}
    assert result["success"] is True
    assert "ADD" in result["events"]
    assert isinstance(result["item_count"], int)


def test_remember_fact_writes_profile_md(tmp_path):
    """remember_fact 后，profile.md 出现该内容（M0 核心验证）。"""
    mgr = _make_manager_with_filestore(tmp_path)
    mgr.decider.decide = MagicMock(return_value=[Decision(action="ADD", content="用户叫小明")])
    mgr.remember_fact("alice", "用户叫小明")

    profile = tmp_path / "profile.md"
    assert profile.exists()
    assert "用户叫小明" in profile.read_text(encoding="utf-8")


def test_remember_fact_noop_event_filtered_from_item_count(tmp_path):
    """NOOP 不计入 item_count（与旧 Mem0 语义接近）。"""
    mgr = _make_manager_with_filestore(tmp_path)
    # 先存一条
    mgr.decider.decide = MagicMock(return_value=[Decision(action="ADD", content="事实A")])
    mgr.remember_fact("alice", "事实A")
    # 再存相同事实，decider 返回 NOOP
    mgr.decider.decide = MagicMock(return_value=[Decision(action="NOOP")])
    result = mgr.remember_fact("alice", "事实A")
    assert result["events"] == ["NOOP"]
    assert result["item_count"] == 0


def test_search_with_detail_returns_compat_shape(tmp_path):
    """search_with_detail 返回四件套。"""
    mgr = _make_manager_with_filestore(tmp_path)
    from src.memory.models import Memory
    mgr.store.upsert(Memory(user_id="alice", content="用户喜欢火锅"))
    result = mgr.search_with_detail("alice", "饮食偏好", limit=5, min_score=0.0)
    assert set(result.keys()) == {"raw_results", "filtered_results", "raw_count", "hit_count"}
    assert isinstance(result["raw_count"], int)
    assert isinstance(result["hit_count"], int)
    # filtered_results 元素含 memory/score
    if result["filtered_results"]:
        assert "memory" in result["filtered_results"][0]
        assert "score" in result["filtered_results"][0]


def test_get_all_returns_list_of_dict_with_memory_and_id(tmp_path):
    """get_all 返回 list[dict]，元素含 memory/id。"""
    mgr = _make_manager_with_filestore(tmp_path)
    from src.memory.models import Memory
    mgr.store.upsert(Memory(user_id="alice", content="测试"))
    all_mems = mgr.get_all("alice")
    assert isinstance(all_mems, list)
    assert len(all_mems) == 1
    assert "memory" in all_mems[0]
    assert "id" in all_mems[0]
    assert all_mems[0]["memory"] == "测试"


def test_get_all_includes_metadata_keys(tmp_path):
    """get_all 元素含 source/created_at/updated_at（web 记忆页逐条展示）。"""
    mgr = _make_manager_with_filestore(tmp_path)
    from src.memory.models import Memory
    mgr.store.upsert(Memory(user_id="alice", content="测试", source="session_summary"))
    row = mgr.get_all("alice")[0]
    assert row["source"] == "session_summary"
    assert isinstance(row["created_at"], float)
    assert row["updated_at"] is None


def test_delete_memory_single(tmp_path):
    """delete_memory：删除指定条目；未知 id 返回 False。"""
    mgr = _make_manager_with_filestore(tmp_path)
    from src.memory.models import Memory
    mgr.store.upsert(Memory(user_id="alice", content="A"))
    mgr.store.upsert(Memory(user_id="alice", content="B"))
    mid = mgr.get_all("alice")[0]["id"]

    assert mgr.delete_memory("alice", mid) is True
    assert mgr.store.get_by_id(mid) is None
    assert len(mgr.get_all("alice")) == 1
    assert mgr.delete_memory("alice", mid) is False  # 已删，不谎报成功


def test_edit_memory_preserves_fields_and_refreshes_updated_at(tmp_path):
    """edit_memory：改文本；id/source/created_at 保留、updated_at 刷新。"""
    mgr = _make_manager_with_filestore(tmp_path)
    from src.memory.models import Memory
    mgr.store.upsert(Memory(user_id="alice", content="原始", created_at=1000.0))
    mid = mgr.get_all("alice")[0]["id"]

    assert mgr.edit_memory("alice", mid, "  人工修订版 ") is True  # 前后空白被剥掉
    row = mgr.store.get_by_id(mid)
    assert row.content == "人工修订版"
    assert row.created_at == 1000.0          # 原时间线保留
    assert row.updated_at is not None and row.updated_at > 1000.0

    # 异常路径：未知 id / 空文本 → False 不落盘
    assert mgr.edit_memory("alice", "missing-id", "x") is False
    assert mgr.edit_memory("alice", mid, "   ") is False
    assert mgr.store.get_by_id(mid).content == "人工修订版"


def test_delete_all_returns_bool(tmp_path):
    """delete_all 返回 bool。"""
    mgr = _make_manager_with_filestore(tmp_path)
    from src.memory.models import Memory
    mgr.store.upsert(Memory(user_id="alice", content="A"))
    result = mgr.delete_all("alice")
    assert isinstance(result, bool)
    assert result is True


def test_user_id_validation():
    """空 user_id 抛 ValueError（保持旧契约）。"""
    mgr = MemoryManager.__new__(MemoryManager)
    mgr._settings = MagicMock()
    mgr.store = MagicMock()
    with pytest.raises(ValueError):
        mgr.search_with_detail("", "q")
    with pytest.raises(ValueError):
        mgr.get_all("")
    with pytest.raises(ValueError):
        mgr.delete_all("")
    with pytest.raises(ValueError):
        mgr.remember_fact("", "content")


# ============================================
# T2b-②：存储注入
# ============================================


def test_store_injection_semantics(tmp_path):
    """store 注入两级语义：显式 store > 默认 SQLite。

    P3-5 退役：workspace_root 文件兜底分支已删除——传 workspace_root
    不再落到文件后端，而是走默认 SQLite（保参忽略，不静默切换后端）。
    """
    from src.storage import paths
    from src.storage.sqlite_provider import SQLiteProvider

    # 1) 显式注入
    fake = MagicMock()
    mgr = MemoryManager(store=fake)
    assert mgr.store is fake

    # 2) workspace_root → 被忽略（文件兜底已退役），走默认 SQLite
    #    （set_data_root 隔离到 tmp，不碰真实 data/）
    paths.set_data_root(tmp_path)
    try:
        mgr = MemoryManager(workspace_root=str(tmp_path))
        assert isinstance(mgr.store, SQLiteProvider)
        # 3) 都不给 → SQLiteProvider（默认）
        mgr = MemoryManager()
        assert isinstance(mgr.store, SQLiteProvider)
    finally:
        paths.set_data_root(None)
