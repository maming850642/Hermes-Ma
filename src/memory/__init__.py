"""
============================================
Hermes 自研记忆模块
============================================
模块构成：
  - MemoryManager: 门面，组合存储 + Extractor + Decider（对外保持旧接口兼容）
  - 存储：SQLiteProvider 默认（data/hermes.db）；旧文件后端已退役
    （scripts/legacy_memory_backend.py，仅迁移脚本用）
  - MemoryExtractor: LLM 提取原子事实
  - MemoryDecider: LLM 决策 ADD/UPDATE/DELETE/NOOP
  - Summarizer: 会话总结

T2b-②：单用户拍平（paths.agent_home），存储协议无 user 维度。
由 `from src.memory import MemoryManager` 导入。
"""
from src.memory.manager import MemoryManager

__all__ = ["MemoryManager"]
