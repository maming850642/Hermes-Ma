"""
============================================
ToolExecutor —— 执行器抽象基类 + 共享辅助
============================================
3 种内置执行器的共同抽象。每种执行器接收 args + ctx，返回 ToolResult。

执行器与权限决策的关系：
    permissions.py 的三层叠加在 executor.execute() 之前完成。
    executor 被调用时，权限已经全通过——executor 只管"怎么执行"，
    不管"该不该执行"。

共享辅助函数 resolve_workspace_root()：
    ShellExecutor 的 cwd 锚点（文件工具 2026-09 退役后 bash 是唯一
    文件通道）。抽到本模块供多执行器/测试复用。

"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.types import ToolResult

if TYPE_CHECKING:
    from src.tools.context import ToolContext


# ════════════════════════════════════════════════════════════════
# ToolExecutor 抽象基类
# ════════════════════════════════════════════════════════════════

class ToolExecutor(ABC):
    """执行器抽象基类。

    子类必须实现 execute()，接收已强转的参数字典和调用上下文，
    返回 ToolResult。

    执行器实例是「配置对象」（YAML 加载时构造一次，多次调用 execute），
    不是「每次调用新建」。所以执行器的 __init__ 参数来自 YAML 的 runtime 段。
    """

    @abstractmethod
    def execute(
        self,
        args: dict[str, Any],
        ctx: "ToolContext",
    ) -> ToolResult:
        """执行工具。

        Args:
            args: 已强转的工具参数（Registry 在调用前做 _coerce）。
            ctx: 调用级上下文（含 permission_mode / caller_context / progress_cb）。

        Returns:
            ToolResult: 含 content（返回给 LLM）和可选的 state_updates。
        """
        ...


# ════════════════════════════════════════════════════════════════
# 共享辅助：workspace 锚点解析
# ════════════════════════════════════════════════════════════════

def resolve_workspace_root() -> Path | None:
    """统一的 workspace 锚点（ShellExecutor 的 cwd）。

    T5：优先用挂载目录（workspace_state.current_root()——挂载 local/upload
    时= 挂载根；未挂载/未配置时回退 paths.agent_home()）。

    Returns:
        Path: 当前工作根目录（首次访问自动创建）。
        None: 仅测试 patch 时出现（ShellExecutor 应在权限层拒绝
              执行——无锚点的 shell 危险）。

    双保险说明：未配置（none）模式下 shell 工具根本不会出现在
    resolve_tools 的结果里（chat-only 过滤），此回退只为独立进程/CLI 兼容。
    """
    from src.storage import paths
    from src.workspace import state as workspace_state

    root = workspace_state.current_root()
    if root is None:
        root = paths.agent_home()
    root.mkdir(parents=True, exist_ok=True)
    return root
