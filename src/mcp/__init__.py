"""
============================================
Hermes MCP 模块 - Model Context Protocol 客户端集成
============================================
让 Hermes Agent 能连接外部 MCP Server，复用生态工具。

仅实现 Client 端（消费外部 server），不实现 Server 端。
基于 fastmcp 库，支持 stdio / sse / streamable_http 三种传输。

配置目录: mcp_servers/（每个 server 一个 mcp_<name>.json 文件）

工具暴露：已连接 server 的工具经 tool_factory 动态生成 ToolSpec
（供 resolve_tools 组装），不经过本包的 __init__ 导出。
"""

from src.mcp.config import McpServerConfig, McpConfigManager
from src.mcp.client import McpClientManager, McpConnectionState

__all__ = [
    "McpServerConfig",
    "McpConfigManager",
    "McpClientManager",
    "McpConnectionState",
]
