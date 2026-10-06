"""
============================================
ToolResult - 工具统一返回协议
============================================
定义工具执行结果的标准格式，消除工具与 Agent 之间的隐式契约。

设计动机（改进建议 #7）：
    之前 write_todos 返回 dict、compact_conversation 返回特殊字符串，
    ToolRegistry 中用 if-elif-else 链逐个硬编码处理，存在三个问题：
    1. 隐式契约：返回格式只在 tools.py 中硬编码，无类型约束
    2. 静默失败：格式变化不报错但功能失效
    3. 扩展性差：新工具需添加新的 if-elif 分支

    改进后：
    - ToolResult 是所有工具的统一返回协议
    - 每个"需要更新 state 的工具"注册一个 ResultHandler
    - execute_tool() 不再包含任何硬编码的工具名判断

用法：
    # 内置处理器已在 ToolRegistry 中自动注册
    # 如需为新工具添加 state 更新能力：
    registry.register_handler("my_tool", my_handler)

P3-2 公共类型下沉：本定义原居 src/agent/tool_result.py，现下沉至
src/types/（agent 与 tools 共同向下依赖），原模块保留 re-export。
"""

from dataclasses import dataclass, field
from typing import Any, Callable

# 类型别名：工具结果处理器函数
# 接收原始结果和工具调用信息，返回标准化的 ToolResult
ResultHandler = Callable[[Any, str, dict], "ToolResult"]


@dataclass
class ToolResult:
    """
    工具执行的统一返回协议。

    所有工具经过 ResultHandler 处理后都应产出此对象。
    未注册处理器的工具走默认逻辑：content=str(result), state_updates={}。

    Attributes:
        content: 返回给 LLM 的文本内容（必填）
        state_updates: 需要合并到 AgentState 的更新（可选）
            - 键为 AgentState 的字段名（如 "todos", "compact_requested"）
            - 值为该字段的新值（会覆盖或合并到 state 中）
    """

    content: str
    """返回给 LLM 的文本内容"""

    state_updates: dict[str, Any] = field(default_factory=dict)
    """需要合并到 AgentState 的状态更新"""

    @property
    def has_state_updates(self) -> bool:
        """是否有状态更新"""
        return bool(self.state_updates)


__all__ = ["ToolResult", "ResultHandler"]
