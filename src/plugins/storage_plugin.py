"""
storage 插件 —— SQLiteProvider（Memory/EventLog/KV 三协议）注册为 "storage"。

config:
    db_path: 库文件路径（缺省 data/hermes.db，见 paths.data_dir）

连接随插件子上下文生命周期：teardown 时 close（注册皆可逆）。
"""

from __future__ import annotations

import logging

from src.cordis.context import Context

logger = logging.getLogger("hermes.plugins.storage")


def apply(ctx: Context, config: dict) -> None:
    from src.storage.sqlite_provider import SQLiteProvider

    db_path = config.get("db_path") or None
    provider = SQLiteProvider(db_path=db_path) if db_path else SQLiteProvider()

    def _close() -> None:
        provider.close()

    ctx.register("storage", provider)
    # 注册皆可逆：teardown 时关连接（close 幂等）
    ctx.effect(lambda: _close)
