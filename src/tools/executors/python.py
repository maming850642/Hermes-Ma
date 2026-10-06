"""
============================================
PythonExecutor —— 调用 Python 函数的执行器
============================================
服务 8 个复杂工具（web_fetch / web_search / task / compact_conversation /
remember / request_human_approval / use_skill / write_todos / dispatch）。

YAML 的 runtime 段配置：
    type: python
    module: src.tools.web_fetch
    function: _execute_web_fetch

函数签名约定：
    def fn(*, arg1, arg2, ..., ctx: ToolContext) -> str | dict | ToolResult

PythonExecutor 自动：
- importlib 动态加载 module.function
- 把 args 解包为关键字参数 + 注入 ctx 尾参
- 返回值统一为 ToolResult（str/dict 自动包装）

ctx 注入让函数能拿到调用级上下文（permission_mode / progress_cb）。
@make_tool / PythonExecutor 把 ctx 参数自动剔除出 LLM schema。

"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any

from src.tools.executor_base import ToolExecutor
from src.types import InterruptSignal, ToolResult

if TYPE_CHECKING:
    from src.tools.context import ToolContext

logger = logging.getLogger("hermes.tools.executors.python")


class PythonExecutor(ToolExecutor):
    """调用 Python 函数的执行器。

    执行器实例是配置对象（YAML 加载时构造一次）。
    module / function 来自 YAML 的 runtime 段。
    """

    def __init__(self, module: str, function: str):
        self.module = module
        self.function = function
        # 延迟加载：首次 execute 时 import（避免循环依赖 + 启动加速）
        self._fn: Any = None

    def _resolve(self):
        """延迟 import 目标函数。"""
        if self._fn is None:
            mod = importlib.import_module(self.module)
            self._fn = getattr(mod, self.function)
        return self._fn

    def execute(self, args: dict[str, Any], ctx: "ToolContext") -> ToolResult:
        fn = self._resolve()
        try:
            # 注入 ctx 作为关键字参数（函数签名需声明 ctx: ToolContext）
            result = fn(**args, ctx=ctx)
        except TypeError:
            # 函数签名可能不接受 ctx（简单工具），退回无 ctx 调用
            try:
                result = fn(**args)
            except InterruptSignal:
                raise  # HITL 审批信号必须向上冒泡，不能被吞
            except Exception as e:
                logger.error(f"PythonExecutor 调用 {self.module}.{self.function} 失败: {e}",
                             exc_info=True)
                return ToolResult(content=f"错误：工具执行失败 - {e}")
        except InterruptSignal:
            raise  # HITL 审批信号必须向上冒泡，不能被吞
        except Exception as e:
            logger.error(f"PythonExecutor 调用 {self.module}.{self.function} 失败: {e}",
                         exc_info=True)
            return ToolResult(content=f"错误：工具执行失败 - {e}")

        # 返回值统一为 ToolResult
        if isinstance(result, ToolResult):
            return result
        if isinstance(result, dict):
            # dict 且不是 ToolResult —— 包装为 content（write_todos 这类
            # 返回 {"todos":..., "summary":...} 的工具应自己返回 ToolResult）
            return ToolResult(content=str(result))
        return ToolResult(content=str(result) if result is not None else "")
