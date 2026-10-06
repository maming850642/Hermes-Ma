"""
全局常量 —— 跨模块共享的恒定值。

与 config.py（可配置项，来自 config.yaml/环境变量）相区分：本文件只放
真正全局恒定、不随部署变化的值。 Cordis 重构引入（T2a）。
"""
from __future__ import annotations

# 单用户架构下的恒定用户身份（哨兵值）。
# 存储接缝（src/storage/）的协议无 user 维度；本地运行时所有记忆/事件/kv
# 都归属此身份。从旧 FileMemoryStore（users/<uid>/profile.md）迁移数据时，
# 也以它为默认 user_id。
LOCAL_USER: str = "local"
