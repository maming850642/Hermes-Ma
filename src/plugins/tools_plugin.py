"""
tools 插件 —— ToolsService 注册为 "tools"，工具权限决策 + 执行事件化。

事件契约（模块 import 时声明；registry_v3 侧同样声明，幂等）：
    "tools/pre-execute"  waterfall —— payload 是 registry_v3.ToolExecRequest
    "tools/execute"      waterfall —— payload 是 registry_v3.ToolRunRequest
    "tools/post-execute" emit      —— 纯观察 (spec, args, tool_ctx, result)

等价改造（行为不变）：
    旧路径：ToolRegistryV3.execute 内直调 permissions.decide + executor.execute
    新路径：kernel_ctx 给定时 execute 构造 ToolExecRequest / ToolRunRequest
            经 waterfall 分发；本插件注册的默认监听器——
            - _permission_listener 调 permissions.decide 填 req.decision
            - _execute_listener 调 spec.executor.execute 填 req.result
            ——输入与输出与旧路径完全一致。deny 监听器可不调 next 短路
            （后续监听器不执行）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.cordis import events
from src.cordis.context import Context
from src.cordis.service import Service

if TYPE_CHECKING:
    from src.agent.registry_v3 import ToolRegistryV3
    from src.tools.context import ToolContext
    from src.tools.permissions import PermissionDecision
    from src.tools.schema import ToolSpec

logger = logging.getLogger("hermes.plugins.tools")

# 事件契约：模块 import 时声明（declare 幂等，同模式重复声明无害；
# registry_v3.py 作为分发方也声明，保证单独使用 kernel_ctx 不缺声明）
events.declare("tools/pre-execute", "waterfall")
events.declare("tools/execute", "waterfall")
events.declare("tools/post-execute", "emit")


class ToolsService(Service):
    """工具服务：resolve（组装）+ bind（注册）+ execute（事件化权限/执行）。

    内持 ToolRegistryV3（start 时以插件 ctx 为 kernel_ctx 构造），
    execute 的权限段经 "tools/pre-execute"、执行段经 "tools/execute" 分发。
    """

    name = "tools"

    def __init__(self) -> None:
        self.registry: "ToolRegistryV3 | None" = None
        self._ctx: Context | None = None

    # ─── 生命周期 ───

    def start(self, ctx: Context) -> None:
        from src.agent.registry_v3 import ToolRegistryV3

        self._ctx = ctx
        self.registry = ToolRegistryV3(kernel_ctx=ctx)
        # 默认监听器：三层权限求值 + executor 执行（先于外层观察者执行）
        ctx.on("tools/pre-execute", self._permission_listener)
        ctx.on("tools/execute", self._execute_listener)

    def stop(self) -> None:
        self._ctx = None

    # ─── 默认监听器（permissions.decide 填 req.decision）───

    @staticmethod
    def _permission_listener(req, next) -> Any:
        from src.tools.permissions import decide

        req.decision = decide(req.spec, req.args, req.tool_ctx)
        if req.decision.is_deny:
            # 短路：deny 是硬结论，不调 next（后续监听器不执行）
            return req
        return next()

    # ─── 默认监听器（spec.executor.execute 填 req.result）───

    @staticmethod
    def _execute_listener(req, next) -> Any:
        from src.types import InterruptSignal, ToolResult

        try:
            req.result = req.spec.executor.execute(req.args, req.tool_ctx)
        except InterruptSignal:
            raise  # HITL 异常向上冒泡（与旧直调路径一致）
        except Exception as e:
            logger.error(f"工具 {req.spec.name} 执行异常: {e}", exc_info=True)
            req.result = ToolResult(content=f"错误：工具执行失败 - {e}")
        return next()

    # ─── 公开 API ───

    def resolve(
        self,
        tool_ctx: "ToolContext",
        settings: Any = None,
        include_mcp: bool = True,
    ) -> "list[ToolSpec]":
        """组装当前上下文的可用工具列表（包 resolve_tools）。

        settings 缺省时优先用 ctx.config（inject [config] 的消费点），
        仍无则由 resolve_tools 回落 get_settings()。
        """
        from src.tools.resolve import resolve_tools

        if settings is None and self._ctx is not None:
            settings = self._ctx.try_get("config")
        return resolve_tools(tool_ctx, settings, include_mcp=include_mcp)

    def bind(self, specs: "list[ToolSpec]") -> None:
        """注册工具列表（转发 registry.bind_tools）。"""
        self.registry.bind_tools(specs)

    def execute(
        self,
        spec: "ToolSpec",
        args: dict[str, Any],
        tool_ctx: "ToolContext",
        tool_call_id: str = "",
    ):
        """执行单个工具（转发 registry.execute，权限段已事件化）。"""
        return self.registry.execute(spec, args, tool_ctx, tool_call_id)


def apply(ctx: Context, config: dict) -> None:
    ctx.register("tools", ToolsService())
