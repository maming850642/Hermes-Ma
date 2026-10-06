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

P3-2 公共类型下沉：定义已搬至 src/types/tool_result.py（本模块原是
定义家）。此处仅 re-export，兼容既有 import 路径
（from src.agent.tool_result import ToolResult）零改动可用。
"""

from src.types.tool_result import ResultHandler as ResultHandler
from src.types.tool_result import ToolResult as ToolResult

__all__ = ["ToolResult", "ResultHandler"]
