"""
============================================
Hermes 工具模块
============================================
为 Agent 提供所有可用工具的统一入口。

T7 起工具统一走声明式 ToolSpec（tools/*.yaml → loader → resolve_tools），
不再维护 Python 侧的消息类工具注册表。本包入口只做：
- get_builtin_tools(): 内置 ToolSpec 列表（YAML 加载）
- get_all_tools(): 内置 + 已连接 MCP 的 ToolSpec 列表
"""

from src.tools.loader import load_builtin_tools


def get_builtin_tools(force: bool = False):
    """返回内置工具的 ToolSpec 列表（不含 MCP 工具；结果走 loader 缓存）。

    Args:
        force: True 时清缓存重扫（MCP 热刷新场景）。
    """
    return list(load_builtin_tools(force=force).values())


def get_all_tools():
    """
    返回所有可用工具 = 内置 ToolSpec + 已连接的 MCP ToolSpec。

    MCP 工具经 tool_factory 从 McpClientManager 动态生成，命名格式
    mcp__<server>__<tool>。无 MCP 配置或无已连接 server 时，返回纯内置
    工具（零行为变化）。

    Returns:
        list[ToolSpec]: 所有可用工具
    """
    tools = get_builtin_tools()
    try:
        from src.tools.resolve import _fetch_mcp_tools
        mcp_tools = _fetch_mcp_tools()
        if mcp_tools:
            tools.extend(mcp_tools)
            import logging
            logging.getLogger("hermes.tools").info(
                f"工具加载完成: {len(tools) - len(mcp_tools)} 内置 + {len(mcp_tools)} MCP = {len(tools)} 总计"
            )
    except ImportError:
        # fastmcp 未安装时静默降级
        pass
    except Exception as e:
        import logging
        logging.getLogger("hermes.tools").warning(f"加载 MCP 工具失败，仅使用内置工具: {e}")

    return tools
