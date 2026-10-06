"""
============================================
src/types —— agent 与 tools 的公共类型包
============================================
P3-2 公共类型下沉：ToolResult / ToolSpec / PermissionDecision / InterruptSignal
原本分居 src/agent 与 src/tools 两个互依包，是 agent↔tools 双向依赖的根源。
下沉后两个包都向下依赖本包，原位置保留 re-export 兼容既有 import 路径。

模块划分（每类型一个模块）：
- tool_result.py: ToolResult + ResultHandler（原 src/agent/tool_result.py）
- tool_spec.py:   ToolSpec + SideEffects 家族（原 src/tools/schema.py）
- permission.py:  PermissionDecision + Action（原 src/tools/permissions.py）
- interrupt.py:   InterruptSignal + payload 约定键（原 src/agent/hitl.py）

本包不 import src.agent / src.tools 的任何运行期符号
（tool_spec.py 仅有 TYPE_CHECKING 级标注引用）。
"""

from src.types.interrupt import COMPLETED_TOOL_MESSAGES_KEY, InterruptSignal
from src.types.permission import Action, PermissionDecision
from src.types.tool_result import ResultHandler, ToolResult
from src.types.tool_spec import (
    SideEffects,
    SideEffectsEvaluator,
    SideEffectsOverride,
    SideEffectsRegexOverride,
    ToolSource,
    ToolSpec,
)

__all__ = [
    "Action",
    "COMPLETED_TOOL_MESSAGES_KEY",
    "InterruptSignal",
    "PermissionDecision",
    "ResultHandler",
    "SideEffects",
    "SideEffectsEvaluator",
    "SideEffectsOverride",
    "SideEffectsRegexOverride",
    "ToolResult",
    "ToolSource",
    "ToolSpec",
]
