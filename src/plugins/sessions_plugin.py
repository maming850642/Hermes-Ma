"""
sessions 插件 —— SessionLog（T3 事件溯源会话日志）注册为 "sessions"。

inject: [storage] —— 事件流写进 storage 插件提供的 SQLite events 表，
与 memory 共享同一 provider 实例（同一 data/hermes.db）。
"""

from __future__ import annotations

from src.cordis.context import Context


def apply(ctx: Context, config: dict) -> None:
    from src.agent.session_log import SessionLog

    ctx.register("sessions", SessionLog(provider=ctx.get("storage")))
