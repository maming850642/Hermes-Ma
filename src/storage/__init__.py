"""
存储接缝（storage seam）—— 可插拔存储服务。

Cordis 架构重构 T2a：定义三协议（记忆 / 事件日志 / KV）+ SQLite 默认实现
+ 路径约定。实现挂到插件内核 ctx.storage，调用方一律走协议面。

设计要点：
- 三协议均无 user 维度（单用户架构，身份恒为 src.constants.LOCAL_USER）
- SQLiteProvider 三协议全实现，是目标默认实现
- 旧文件记忆后端（FileMemoryProvider/FileMemoryStore/read_legacy_profile）
  已退役出生产包，见 scripts/legacy_memory_backend.py（迁移脚本用）
"""
from __future__ import annotations

from src.storage.base import EventLogProtocol, KVProtocol, MemoryStoreProtocol
from src.storage.paths import (
    PROJECT_ROOT,
    agent_home,
    data_dir,
    data_root,
    set_data_root,
)
from src.storage.sqlite_provider import SQLiteProvider

__all__ = [
    "EventLogProtocol",
    "KVProtocol",
    "MemoryStoreProtocol",
    "SQLiteProvider",
    "PROJECT_ROOT",
    "agent_home",
    "data_dir",
    "data_root",
    "set_data_root",
]
