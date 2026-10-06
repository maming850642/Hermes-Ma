"""
mcp 插件 —— McpClientManager 单例注册为 "mcp"。

MCP 是可选依赖（fastmcp 未安装时 src.mcp.client 不可 import）：
import 失败注册 None 并告警，消费方用 ctx.try_get("mcp") 判空降级。
"""

from __future__ import annotations

import logging

from src.cordis.context import Context

logger = logging.getLogger("hermes.plugins.mcp")


def apply(ctx: Context, config: dict) -> None:
    try:
        from src.mcp.client import get_client_manager

        manager = get_client_manager()
    except Exception as e:  # ImportError 为主；可选依赖缺失不阻塞 boot
        logger.warning(f"MCP 不可用（可选依赖），ctx.mcp 注册为 None: {e}")
        manager = None
    ctx.register("mcp", manager)
