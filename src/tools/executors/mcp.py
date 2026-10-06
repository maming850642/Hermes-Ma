"""
============================================
McpExecutor —— MCP 工具执行器
============================================
服务所有 MCP 工具（运行时动态发现）。

YAML 的 runtime 段配置：
    type: mcp
    server: <server_name>
    tool: <tool_name>

MCP 工具连上 server 时由 McpToolFactory 动态生成 ToolSpec（不经 YAML 文件）。
McpExecutor 通过 mcp_client.call_tool() 调用，结果格式化为字符串。

per-server 锁串行化同一 server 的工具调用（防 stdio 响应错位），
搬自 src/mcp/adapters.py:_get_server_lock。

"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from src.tools.executor_base import ToolExecutor
from src.types import ToolResult

if TYPE_CHECKING:
    from src.tools.context import ToolContext

logger = logging.getLogger("hermes.tools.executors.mcp")

# stdio 子进程挂掉时 host 侧常见文案（anyio / mcp sdk / 自制 server）
_CLOSED_MARKERS = (
    "connection closed",
    "closedresourceerror",
    "broken pipe",
    "连接已关闭",
)


def _looks_closed(exc: BaseException) -> bool:
    blob = f"{type(exc).__name__}: {exc}".lower()
    return any(m in blob for m in _CLOSED_MARKERS)


# ════════════════════════════════════════════════════════════════
# Per-server 锁注册表（搬自 src/mcp/adapters.py:29-38）
# ════════════════════════════════════════════════════════════════

_server_locks: dict[str, threading.Lock] = {}
_server_locks_guard = threading.Lock()


def _get_server_lock(server_name: str) -> threading.Lock:
    """获取（或创建）某个 server 的专用锁。

    同一 server 的所有 McpExecutor 实例共享一把锁。
    stdio transport 通过 stdin/stdout 顺序通信，多线程并发调用同一
    server 的多个工具会导致响应错位/JSON 解析错误。
    """
    with _server_locks_guard:
        if server_name not in _server_locks:
            _server_locks[server_name] = threading.Lock()
        return _server_locks[server_name]


class McpExecutor(ToolExecutor):
    """MCP 工具执行器。

    执行器实例是配置对象（McpToolFactory 动态构造一次）。
    server / tool 标识调用目标。
    """

    def __init__(self, server: str, tool: str):
        self.server = server
        self.tool = tool

    def execute(self, args: dict[str, Any], ctx: "ToolContext") -> ToolResult:
        """通过 mcp_client 调用 MCP 工具。

        延迟 import mcp.client 避免循环依赖。
        """
        try:
            from src.mcp.client import _get_loop_thread, get_client_manager
        except ImportError:
            return ToolResult(content="错误：MCP 客户端未安装（fastmcp）")

        try:
            manager = get_client_manager()
            client = manager.get_client(self.server)
            if client is None:
                return ToolResult(
                    content=f"错误：MCP server '{self.server}' 未连接"
                )

            loop = _get_loop_thread()
            server_lock = _get_server_lock(self.server)

            def _invoke(c):
                async def _call():
                    return await c.call_tool(self.tool, args)
                with server_lock:
                    return loop.run_coro(_call(), timeout=60.0)

            try:
                result = _invoke(client)
            except Exception as e:
                if not _looks_closed(e):
                    raise
                # stdio 子进程已死，状态却仍是 connected——重连一次再打。
                logger.warning(
                    f"MCP {self.server}/{self.tool} 连接断开，尝试重连: {e}"
                )
                ok, msg = manager.connect(self.server)
                client2 = manager.get_client(self.server) if ok else None
                if client2 is None:
                    raise ConnectionError(
                        f"{e}；重连失败: {msg}"
                    ) from e
                result = _invoke(client2)

            return ToolResult(content=self._format_result(result))

        except Exception as e:
            logger.error(f"MCP 工具调用失败 {self.server}/{self.tool}: {e}", exc_info=True)
            stderr = ""
            try:
                from src.mcp.client import _read_stderr_tail
                stderr = _read_stderr_tail(self.server)
            except Exception:
                pass
            extra = f"\n子进程 stderr:\n{stderr}" if stderr else ""
            return ToolResult(content=f"MCP 工具调用失败: {e}{extra}")

    @staticmethod
    def _format_result(result: Any) -> str:
        """将 fastmcp CallToolResult 格式化为字符串。

        搬自 src/mcp/adapters.py:_format_result。
        """
        if hasattr(result, "content") and result.content:
            parts = []
            for item in result.content:
                if hasattr(item, "text"):
                    parts.append(item.text)
                elif hasattr(item, "data"):
                    parts.append(f"[二进制数据 {getattr(item, 'mimeType', 'unknown')}]")
                else:
                    parts.append(str(item))
            text = "\n".join(parts)
            if getattr(result, "isError", False):
                return f"MCP 工具返回错误:\n{text}"
            return text

        if isinstance(result, str):
            return result
        return str(result)
