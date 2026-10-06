"""
M0-3 验收：证明 agent / worker 在无 Qdrant 下能启动。

这是 M0 的核心承诺——移除 Qdrant 运行时依赖后，HermesAgent 和 worker
都能在断网/无 Qdrant 环境下实例化。

T2b-②：默认后端切 SQLiteProvider（data/hermes.db）；测试用
set_data_root(tmp_path) 隔离，不碰真实 data/。
P3-5：workspace_root 文件兜底分支已删除（退役到
scripts/legacy_memory_backend.py），传入 workspace_root 走默认 SQLite。
"""
import os

import pytest

from src.memory import MemoryManager
from src.storage import paths
from src.storage.sqlite_provider import SQLiteProvider


@pytest.fixture(autouse=True)
def _isolated_data_root(tmp_path):
    """数据根指向 tmp：默认 SQLiteProvider 建在 tmp 下。"""
    paths.set_data_root(tmp_path)
    yield
    paths.set_data_root(None)


def test_memory_manager_default_is_sqlite():
    """MemoryManager 默认实例化后 store 是 SQLiteProvider（非 Qdrant）。"""
    mgr = MemoryManager()
    assert isinstance(mgr.store, SQLiteProvider)


def test_memory_manager_ignores_workspace_root(tmp_path):
    """workspace_root 已废弃（P3-5 退役）：传入后不再落文件后端，走默认 SQLite。"""
    mgr = MemoryManager(workspace_root=str(tmp_path))
    assert isinstance(mgr.store, SQLiteProvider)
    assert not (tmp_path / "profile.md").exists()  # 不再产生文件后端痕迹


def test_memory_manager_no_qdrant_env():
    """qdrant 端口指向不可达地址，MemoryManager 仍能实例化（不连 Qdrant）。"""
    os.environ["QDRANT_PORT"] = "59999"  # 不可达
    try:
        mgr = MemoryManager()  # 不应抛连接异常
        assert isinstance(mgr.store, SQLiteProvider)
    finally:
        del os.environ["QDRANT_PORT"]


def test_hermes_agent_init_no_qdrant():
    """HermesAgentV3(MemoryManager()) 能实例化（不连 Qdrant）。"""
    from src.agent import HermesAgentV3
    mgr = MemoryManager()
    agent = HermesAgentV3(mgr, tool_callback=None)
    assert agent is not None
    assert agent.memory_orchestrator is not None
