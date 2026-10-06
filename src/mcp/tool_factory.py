"""
============================================
MCP ToolSpec 工厂 —— 动态生成 ToolSpec（取代 adapters.py 的 BaseTool 子类化）
============================================
MCP 工具连上 server 时运行时动态发现，不经 YAML 文件。
本模块把 MCP server 返回的 raw_tool（含 name/description/inputSchema）
转成 ToolSpec，executor 指向 McpExecutor。

side_effects 策略（信息不对称处理）：
    MCP 协议只暴露 name/description/inputSchema，工具内部行为（删文件？
    发网络？）我们看不到。保守标 + 让用户在 server 配置里声明信任级别（trust）。

    trust=full      → side_effects 仅标 network_access（用户已表态信任）
    trust=approval  → side_effects 标 destructive + network_access（保守全标，
                      在 before_changes/plan 下审批/拒绝）

trust 字段加在 mcp_servers/*.json 的 server 配置里：
    {"name": "filesystem", "trust": "approval", ...}
    未配置 trust 时默认 "approval"（最安全）。

"""

from __future__ import annotations

import logging
from typing import Any

from src.tools.executors.mcp import McpExecutor
from src.tools.schema import SideEffects, ToolSpec

logger = logging.getLogger("hermes.mcp.tool_factory")


def _get_server_trust(server_config: Any) -> str:
    """读 server 配置的 trust 字段，默认 'approval'（最安全）。"""
    if hasattr(server_config, "trust"):
        return server_config.trust or "approval"
    if isinstance(server_config, dict):
        return server_config.get("trust", "approval")
    return "approval"


def mcp_tool_to_spec(
    raw_tool: Any,
    mcp_client: Any,
    server_name: str,
    server_config: Any = None,
) -> ToolSpec:
    """单个 MCP 工具 → ToolSpec。

    Args:
        raw_tool: MCP server 返回的工具对象（含 name/description/inputSchema）。
        mcp_client: 已连接的 MCP client（McpExecutor 调用时用）。
        server_name: server 名（用于命名前缀 mcp__<server>__<tool>）。
        server_config: server 配置对象/dict（读 trust 字段）。
    """
    name = f"mcp__{server_name}__{raw_tool.name}"
    raw_desc = (getattr(raw_tool, "description", "") or "").strip()
    desc = raw_desc or f"MCP tool {raw_tool.name}"
    description = f"[MCP/{server_name}] {desc}"

    # MCP inputSchema 本身就是 JSON Schema，直接用，不经 Pydantic
    parameters = getattr(raw_tool, "inputSchema", None) or {
        "type": "object",
        "properties": {},
    }

    # side_effects：按 trust 决定保守程度
    trust = _get_server_trust(server_config)
    if trust == "full":
        # 用户已表态信任 → 仅标 network_access
        side_effects = SideEffects(network_access=True)
    else:
        # trust=approval（默认）→ 保守标 destructive + network_access
        side_effects = SideEffects(destructive=True, network_access=True)

    # McpExecutor 闭包捕获 server / tool 名
    # 注意：mcp_client 不直接存进 executor（延迟从 client_manager 取），
    # 避免 client 重连后 executor 持有过期引用。
    executor = McpExecutor(server=server_name, tool=raw_tool.name)

    return ToolSpec(
        name=name,
        description=description,
        parameters=parameters,
        executor=executor,
        side_effects=side_effects,
        source="mcp",
    )


def generate_mcp_tool_specs() -> list[ToolSpec]:
    """拉取所有已连接 MCP server 的工具，批量生成 ToolSpec。

    被 resolve_tools() 调用。fastmcp 未安装或无已连接 server 时返回空列表。

    信任级别来自 mcp_servers/*.json 的 trust 字段（默认 approval）。
    """
    try:
        from src.mcp.client import get_client_manager
    except ImportError:
        return []

    try:
        manager = get_client_manager()
        result: list[ToolSpec] = []
        for server_name, state in manager._servers.items():
            if not getattr(state, "connected", False) or getattr(state, "_client", None) is None:
                continue
            raw_tools = getattr(state, "_raw_tools", []) or []
            client = state._client

            # trust 配置直接读连接状态自带的 config（P1-9：McpClientManager
            # reload_config 时已把 mcp_servers/*.json 加载进
            # McpConnectionState.config；此前对不存在符号 get_config_manager
            # 的 import 恒抛 ImportError 被吞，trust 全链路失效）
            sconfig = getattr(state, "config", None)
            for raw_tool in raw_tools:
                try:
                    spec = mcp_tool_to_spec(
                        raw_tool, client, server_name, sconfig
                    )
                    result.append(spec)
                except Exception as e:
                    logger.error(
                        f"生成 MCP ToolSpec 失败 {server_name}/{raw_tool.name}: {e}"
                    )
        return result
    except Exception as e:
        logger.warning(f"加载 MCP 工具失败，仅使用内置工具: {e}")
        return []
