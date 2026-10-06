"""
============================================
ToolRegistryV3 —— V3 三层架构的工具执行注册中心
============================================
V3 工具执行注册中心。与已退役旧 Registry 的核心区别：

  旧 Registry：
    - 消费消息类工具对象
    - tool.invoke(args, config=config)
    - _INTERRUPT_TOOL_NAMES 硬编码集合判 interrupt
    - if tc["name"] == "task" 字符串拦截
    - ResultHandler 注册表
    - StreamWriter 推事件
    - 图引擎异常透传

  ToolRegistryV3：
    - 消费 ToolSpec（V3 schema）
    - spec.executor.execute(args, ctx)
    - needs_approval 由 permissions.decide() 三层叠加决定
    - intercepted / writes_state 等全部走 schema flag
    - Python 函数直接返回 ToolResult（含 state_updates）
    - EventSink（callable）推事件
    - InterruptSignal 异常冒泡

批量执行策略（对齐现有语义）：
    单工具 或 含 needs_approval 工具 → 串行同步执行（InterruptSignal 必须在
                                         调用线程冒泡，不进线程池）
    多工具（全非 needs_approval）    → 并发执行（ThreadPoolExecutor + copy_context）

"""

from __future__ import annotations

import contextvars
import inspect
import logging
import queue as _queue
import threading
import time as _t
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING

from src.agent.hitl import COMPLETED_TOOL_MESSAGES_KEY, InterruptSignal
from src.agent.tool_result import ToolResult
from src.cordis import events
from src.llm.messages import ToolMsg
from src.tools.context import ToolContext
from src.tools.permissions import decide
from src.types import PermissionDecision, ToolSpec

if TYPE_CHECKING:
    from src.cordis.context import Context

logger = logging.getLogger("hermes.agent.registry_v3")


# 事件回调类型：Registry 推 tool_start/tool_end/todos_update 事件
EventSink = Callable[[dict], None]


def _cancel_takes_token(cancel: Callable) -> bool:
    """cancel 钩子是否接受 call_token 实参（按调用粒度取消）。

    bound method 的 signature 已不含 self：无参 cancel() → 0 个位置参数；
    cancel(call_token=None)（如 ShellExecutor）→ 1 个。取不到 signature
    （C 实现/异常形态）一律按无参处理，回退旧全杀语义。
    """
    try:
        return bool(inspect.signature(cancel).parameters)
    except (TypeError, ValueError):
        return False


# ════════════════════════════════════════════════════════════════
# 参数轻量强转（取代 pydantic 校验）
# ════════════════════════════════════════════════════════════════

# JSON Schema type → Python type
_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _clamp_numeric(value: Any, prop_schema: dict[str, Any]) -> Any:
    """对声明了 minimum/maximum 的数值参数做 clamp（P2-4：JSON Schema 约束兜底）。

    LLM 幻觉出的越界值（如 bash timeout=99999）直接钳到声明区间，而不是
    让各 executor 自行防御。bool 是 int 的子类，显式排除；非数值原样返回。
    注意"哨兵 0"语义（bash timeout 的 minimum: 0，0=不限）：负值被 clamp
    到 0 而非剔除，与 executor 的 `timeout > 0 else None` 判定组合后语义
    不变（非正数都不限时）。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    minimum = prop_schema.get("minimum")
    maximum = prop_schema.get("maximum")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
        value = max(value, minimum)
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
        value = min(value, maximum)
    return value


def coerce_args(args: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    """基于 JSON Schema 的轻量类型强转。

    LLM tool_call 的 args 可能传字符串数字（如 "60" 而非 60）。
    按 parameters 的 type 声明做强转，对齐现有"防御性强转"风格
    （sub_agent.py、旧 Registry 的防御性强转）。

    缺必填项 → 不抛异常，让 executor 自己处理（返回友好错误）。
    未知字段 → 保留（不剔除，宽松）。
    """
    if not isinstance(args, dict):
        return {}

    properties = parameters.get("properties", {})
    coerced: dict[str, Any] = {}

    for key, value in args.items():
        prop_schema = properties.get(key)
        if prop_schema is None:
            # 未知字段：原样保留
            coerced[key] = value
            continue

        json_type = prop_schema.get("type", "")
        py_type = _JSON_TYPE_MAP.get(json_type)

        if py_type is None:
            coerced[key] = value
        elif py_type is bool:
            # bool 的强转：字符串 "true"/"false" → bool
            if isinstance(value, str):
                coerced[key] = value.strip().lower() in ("true", "1", "yes")
            else:
                coerced[key] = bool(value)
        elif py_type is int and isinstance(value, str):
            try:
                coerced[key] = int(value)
            except ValueError:
                try:
                    coerced[key] = int(float(value))  # "3.0" → 3
                except ValueError:
                    coerced[key] = value  # 转不了保留原值
        elif py_type is float and isinstance(value, str):
            try:
                coerced[key] = float(value)
            except ValueError:
                coerced[key] = value
        elif py_type is int and isinstance(value, float):
            coerced[key] = int(value)  # 3.0 → 3
        else:
            coerced[key] = value

        # P2-4：声明了 minimum/maximum 的数值参数按 schema 区间 clamp
        coerced[key] = _clamp_numeric(coerced[key], prop_schema)

    return coerced


# ════════════════════════════════════════════════════════════════
# tools/pre-execute 事件契约 + payload
# ════════════════════════════════════════════════════════════════

#: 权限求值事件（waterfall）。tools_plugin 也声明同模式（declare 幂等）；
#: 此处作为分发方声明，保证单独使用 kernel_ctx 时不缺声明
events.declare("tools/pre-execute", "waterfall")


@dataclass
class ToolExecRequest:
    """"tools/pre-execute" waterfall 事件的 payload。

    execute() 在强转后构造本对象分发；监听器（默认实现见
    src/plugins/tools_plugin.py 的 _permission_listener）调
    permissions.decide 填 decision。deny 监听器可不调 next 短路。
    decision 保持 None 时视为放行（等价 allow）。
    """

    spec: ToolSpec
    args: dict[str, Any]
    tool_ctx: ToolContext
    decision: "PermissionDecision | None" = None


# T6 补充事件契约：执行段事件化（kernel_ctx 给定时分发，None 时旧直调）
# tools/execute waterfall：默认监听器（tools_plugin._execute_listener）调
#   spec.executor.execute 把 ToolResult 写进 req.result 槽
events.declare("tools/execute", "waterfall")
# tools/post-execute emit：执行完成后的纯观察（spec, args, tool_ctx, result）
events.declare("tools/post-execute", "emit")


@dataclass
class ToolRunRequest:
    """"tools/execute" waterfall 事件的 payload。

    execute() 权限放行后构造本对象分发；默认监听器执行工具并把 ToolResult
    写进 result 槽。result 保持 None 时（链上无人处理）execute 兜底直连执行
    （等价旧路径）。外层监听器可观察或在 next() 之后替换 result。

    短路语义（R2-13）：监听器不调 next 即短路。置 handled=True 表示
    "本监听器已接管本次执行"——execute 尊重短路：填了 result 槽则用之，
    未填则返回错误 ToolResult，不再 fallback 直连 executor（旧语义拦截
    不住）；未置 handled 的短路维持 fallback 兼容。
    """

    spec: ToolSpec
    args: dict[str, Any]
    tool_ctx: ToolContext
    result: "ToolResult | None" = None
    handled: bool = False
    """短路接管标志：True = 监听器已处理（调用方不得 fallback 直连）。"""


# ════════════════════════════════════════════════════════════════
# ToolRegistryV3
# ════════════════════════════════════════════════════════════════

class ToolRegistryV3:
    """V3 工具执行注册中心。

    职责：
    1. execute(spec, args, ctx) — 单个工具的三层权限 + 强转 + 执行
    2. process_tool_calls(tool_calls, ctx, event_sink) — 批量执行（串行/并发）
    3. _coerce(args, parameters) — 基于 JSON Schema 的轻量强转

    不负责工具列表组装（那是 resolve_tools 的职责）。

    kernel_ctx（T4/T6，可选）：给了它，execute 的权限段经 "tools/pre-execute"
    waterfall 事件分发（默认监听器=permissions.decide，行为等价，见
    tests/plugins/test_tools_event.py），执行段经 "tools/execute" waterfall
    （默认监听器=executor.execute）+ "tools/post-execute" emit（纯观察）；
    None 时全直调（旧路径，现有调用方/测试零改动）。
    """

    def __init__(self, kernel_ctx: "Context | None" = None):
        # 工具缓存：name → ToolSpec（由 bind_tools 注册）
        self._specs: dict[str, ToolSpec] = {}
        # Cordis 内核上下文：权限段事件分发的目标（None = 旧直调路径）
        self._kernel = kernel_ctx
        # R2-12：decision 回退告警只发一次（防刷屏）
        self._warned_decision_fallback = False

    # ─── 工具注册 ───

    def bind_tools(self, specs: list[ToolSpec]) -> None:
        """注册工具列表（供 process_tool_calls 按 name 查找）。"""
        self._specs = {s.name: s for s in specs}
        logger.debug(f"RegistryV3 bind {len(self._specs)} tools")

    def get_spec(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def bound_specs(self) -> list[ToolSpec]:
        """当前绑定的生效工具集（resolve_tools 组装后的结果，按注册序）。"""
        return list(self._specs.values())

    # ─── 单工具执行 ───

    def execute(
        self,
        spec: ToolSpec,
        args: dict[str, Any],
        ctx: ToolContext,
        tool_call_id: str = "",
    ) -> ToolResult:
        """执行单个工具。三层权限求值 + 强转 + executor.execute()。

        Args:
            tool_call_id: LLM tool_call ID，透传到 HITL 快照用于回填。

        Raises:
            InterruptSignal: 权限决策为 requireApproval 时抛出（上层捕获做 HITL）。
        """
        # 1. 轻量强转（时机不变：事件/权限求值之前）
        coerced = coerce_args(args, spec.parameters)

        # 2. 三层权限求值（旧路径直调 decide；kernel_ctx 给定时事件分发，
        #    默认监听器等价调 decide 填 req.decision）
        decision = self._resolve_decision(spec, coerced, ctx)

        # 3. 权限决策分支
        if decision is not None and decision.is_deny:
            logger.info(f"工具 {spec.name} 被拒绝: {decision.reason}")
            return ToolResult(content=f"错误：{decision.reason}")

        if decision is not None and decision.needs_approval:
            # 抛 InterruptSignal，由上层（process_tool_calls → stream_invoke）捕获
            payload = {
                "action": f"执行工具 {spec.name}",
                "details": f"参数: {coerced}",
                "tool_name": spec.name,
                "tool_args": coerced,
                "tool_call_id": tool_call_id,
            }
            logger.info(f"工具 {spec.name} 需审批: {decision.reason}")
            raise InterruptSignal(payload)

        # 4. 放行（allow 或事件链无人填 decision）→ executor 执行
        #    kernel_ctx 给定时经 tools/execute waterfall 分发（默认监听器=
        #    executor.execute，见 tools_plugin）+ tools/post-execute emit 观察；
        #    None 时旧直调（现有调用方/测试零改动）
        if self._kernel is not None:
            run_req = ToolRunRequest(spec=spec, args=coerced, tool_ctx=ctx)
            self._kernel.waterfall("tools/execute", run_req)
            if run_req.result is None:
                if run_req.handled:
                    # R2-13：监听器短路接管但未填结果槽 → 错误 ToolResult
                    # （尊重短路，不 fallback 直连 executor）
                    run_req.result = ToolResult(
                        content=f"错误：工具 {spec.name} 执行被监听器拦截（无结果）",
                    )
                else:
                    run_req.result = self._invoke_executor(spec, coerced, ctx)
            result = run_req.result
            try:
                self._kernel.emit("tools/post-execute", spec, coerced, ctx, result)
            except Exception:
                logger.warning("tools/post-execute 观察监听器异常（忽略）", exc_info=True)
            return result

        return self._invoke_executor(spec, coerced, ctx)

    def _invoke_executor(self, spec: ToolSpec, coerced: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行 spec.executor（InterruptSignal 冒泡；其余异常转错误 ToolResult）。"""
        try:
            return spec.executor.execute(coerced, ctx)
        except InterruptSignal:
            raise  # HITL 异常向上冒泡
        except Exception as e:
            logger.error(f"工具 {spec.name} 执行异常: {e}", exc_info=True)
            return ToolResult(content=f"错误：工具执行失败 - {e}")

    def _resolve_decision(self, spec: ToolSpec, coerced: dict[str, Any], ctx: ToolContext):
        """权限求值：kernel_ctx 给定时经 tools/pre-execute waterfall 分发，
        否则直调 permissions.decide（旧路径）。

        fail-safe（R2-12 内核 H3）：kernel_ctx 存在而链上无人求值出 decision
        （组合根 teardown 后 / 未挂 tools 插件默认监听器）时，不得按 allow
        放行（fail-open）——回退直调 permissions.decide，告警一次（防刷屏）。
        类型防御（R2-14 内核 M3）：decision 不是 PermissionDecision 实例
        （恶意/劣化监听器）→ 按 None 处理进同一回退 + 告警。
        """
        if self._kernel is None:
            return decide(spec, coerced, ctx)
        req = ToolExecRequest(spec=spec, args=coerced, tool_ctx=ctx)
        self._kernel.waterfall("tools/pre-execute", req)
        decision = req.decision
        if decision is not None and not isinstance(decision, PermissionDecision):
            logger.warning(
                f"tools/pre-execute 监听器填了非法 decision 类型 "
                f"{type(decision).__name__}（tool={spec.name}），按未求值处理"
            )
            decision = None
        if decision is None:
            if not self._warned_decision_fallback:
                logger.warning(
                    "tools/pre-execute 链上无人求值权限（kernel ctx teardown 后或"
                    "未挂默认监听器），回退直调 permissions.decide（fail-safe）"
                )
                self._warned_decision_fallback = True
            return decide(spec, coerced, ctx)
        return decision

    # ─── 批量执行 ───

    def process_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        ctx: ToolContext,
        event_sink: EventSink | None = None,
    ) -> tuple[list[ToolMsg], dict[str, Any]]:
        """批量执行工具调用。

        Args:
            tool_calls: LLM 输出的 tool_call 列表。
                [{"id", "name", "args": {...}}, ...]
            ctx: 调用级上下文（含 permission_mode / caller_context）。
            event_sink: 事件回调（可选）。推 tool_start/tool_end/todos_update 等。

        Returns:
            (tool_messages, state_updates)
            - tool_messages: 每个 tool_call 对应的 ToolMsg
            - state_updates: 合并后的 state 更新（如 todos）
        """
        # 过滤无效调用（未知工具名）。
        # P2-3：未知工具不再静默剔除——合成错误 ToolMsg 保持 assistant
        # tool_calls 与 tool 消息一一配对（悬空 tool_call 会被严格
        # OpenAI 兼容端点 400，会话从此每轮卡死）。模型幻觉工具名或
        # MCP 热刷新后都能拿到明确反馈。
        known_calls: list[dict[str, Any]] = []
        unknown_msgs: dict[str, ToolMsg] = {}
        for tc in tool_calls:
            name = tc.get("name", "")
            if name in self._specs:
                known_calls.append(tc)
            else:
                logger.warning(f"未知工具: {name}")
                unknown_msgs[tc.get("id", "")] = ToolMsg(
                    content=f"错误：未知工具 {name}，本调用未执行",
                    tool_call_id=tc.get("id", ""),
                )

        if not known_calls:
            return list(unknown_msgs.values()), {}

        # 预判：含 needs_approval 工具 → 强制串行（InterruptSignal 必须在调用线程冒泡）
        contains_approval = self._contains_approval_tool(known_calls)

        # 单工具 或 含审批工具 → 串行同步
        if len(known_calls) == 1 or contains_approval:
            executed, state_updates = self._execute_serial(known_calls, ctx, event_sink)
        else:
            # 多工具（全非审批）→ 并发
            executed, state_updates = self._execute_concurrent(known_calls, ctx, event_sink)

        if not unknown_msgs:
            return executed, state_updates

        # 按原始 tool_call 顺序合并：未知位置填错误消息，其余按执行序回填
        executed_iter = iter(executed)
        merged = [
            unknown_msgs[tc.get("id", "")]
            if tc.get("id", "") in unknown_msgs
            else next(executed_iter)
            for tc in tool_calls
        ]
        return merged, state_updates

    # ─── 串行执行 ───

    def _execute_with_timeout(
        self,
        spec: ToolSpec,
        args: dict[str, Any],
        ctx: ToolContext,
        timeout_s: float,
        tool_call_id: str = "",
    ) -> ToolResult:
        """带超时保护的单工具执行。

        InterruptSignal 不被超时拦截——它是同步、即时抛出的（权限决策在
        executor.execute 之前），所以不会进入长时间阻塞。future.result()
        重新抛出 InterruptSignal，上层 stream_invoke 正常捕获做 HITL。

        超时后先调 executor 的 cancel 钩子（P2-4：ShellExecutor 借此杀掉
        命令进程树），再放弃等待继续主循环——工作线程随即因进程树被杀而
        自然退出，不再遗弃 bash 孤儿进程；无 cancel 钩子的 executor 维持
        旧语义（线程自行退出），与 _execute_concurrent 的
        shutdown(wait=False) 一致。
        """
        from config import settings

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            cvars = contextvars.copy_context()
            future = executor.submit(cvars.run, self.execute, spec, args, ctx, tool_call_id)
            try:
                return future.result(timeout=timeout_s)
            except (TimeoutError, FuturesTimeoutError):
                # P2-7：future.result() 在 py3.10 抛的是
                # concurrent.futures.TimeoutError（3.11 才与 builtin 合一），
                # 只捕 builtin 会漏——双兼容捕获（同仓 session_lifecycle.py 同法）。
                # P2-4：给 executor 一次终止机会（如 ShellExecutor.cancel 杀
                # 进程树），不遗弃工作线程下的命令进程。
                # P2-4b：执行器实例经 loader 进程级缓存共享，同实例可能同时
                # 服务多个会话/子代理的 execute——cancel 支持可选 call_token
                # （= 本次调用 ToolContext 的 id）时按调用粒度取消，只杀本次
                # 超时调用的进程树，不牵连兄弟执行；无参签名的自定义执行器
                # 回退旧全杀语义。
                cancel = getattr(spec.executor, "cancel", None)
                if callable(cancel):
                    try:
                        if _cancel_takes_token(cancel):
                            cancel(id(ctx))
                        else:
                            cancel()
                    except Exception:
                        logger.warning(
                            f"工具 {spec.name} 超时终止钩子异常", exc_info=True,
                        )
                logger.warning(
                    f"工具 {spec.name} 执行超时({timeout_s}s)，已请求终止，跳过继续"
                )
                return ToolResult(content=f"工具执行超时（{timeout_s}s），请重试或简化任务")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _execute_serial(
        self,
        tool_calls: list[dict[str, Any]],
        ctx: ToolContext,
        event_sink: EventSink | None,
    ) -> tuple[list[ToolMsg], dict[str, Any]]:
        """串行同步执行（单工具或含审批工具时走此路径）。"""
        from config import settings

        tool_timeout_s = float(getattr(settings, "tool_timeout", 180))
        tool_messages: list[ToolMsg] = []
        state_updates: dict[str, Any] = {}

        for tc in tool_calls:
            spec = self._specs[tc["name"]]
            tool_id = tc["id"]

            # 协作式取消：置位即不再启动后续工具（已跑完的保留结果），
            # 剩余调用合成占位结果保持 assistant tool_calls 配对完整
            should_cancel = getattr(ctx, "should_cancel", None)
            if should_cancel is not None:
                try:
                    if should_cancel():
                        logger.info(f"取消已置位：跳过剩余 {len(tool_calls) - len(tool_messages)} 个工具")
                        for rest in tool_calls[len(tool_messages):]:
                            tool_messages.append(ToolMsg(
                                content="（用户已停止，本工具未执行）",
                                tool_call_id=rest["id"],
                            ))
                        return tool_messages, state_updates
                except Exception:
                    pass

            if event_sink:
                event_sink({
                    "type": "tool_start",
                    "tool_name": spec.name,
                    "tool_args": tc["args"],
                    "tool_id": tool_id,
                })

            # execute 可能抛 InterruptSignal —— 让它冒泡到 stream_invoke。
            # P1-3：把已完成的 tool_messages 挂到 signal 上带出（局部列表
            # 随栈帧丢弃，会让快照里的 assistant(tool_calls) 永久悬空——
            # resume 后消息序列非法，且随 turn_messages 落桶持久化，
            # 该会话从此每轮请求都被严格 OpenAI 兼容端点 400）
            try:
                result = self._execute_with_timeout(spec, tc["args"], ctx, tool_timeout_s, tool_id)
            except InterruptSignal as sig:
                sig.payload[COMPLETED_TOOL_MESSAGES_KEY] = [
                    {"tool_call_id": m.tool_call_id, "content": m.content}
                    for m in tool_messages
                ]
                # 串行批量被中断时，已执行工具（如 write_todos）的
                # state_updates 只能经 payload 带出（异常冒泡，无正常返回
                # 通道）——对齐并发腿 interrupt 分支的同名挂载，agent 侧
                # _handle_interrupt 消费 payload["state_updates"] 补发
                # todos_update。此前串行腿只挂 tool_messages，兄弟待办随
                # 中断静默丢失。
                if state_updates:
                    sig.payload["state_updates"] = state_updates
                # P1（二轮审查）：executor 直抛的 InterruptSignal（如
                # request_human_approval，静态 destructive=false，单发调用
                # 出厂必走串行）payload 只有 action/details——不补身份的话
                # 快照存出 pending_tool_call_id=""，resume 时 get_spec("")
                # 落 None-spec 分支：审批决策被静默丢弃 + 空 id 孤儿 tool
                # 消息进活桶（严格端点 400）。对齐并发腿的 setdefault 注入
                # （_execute_concurrent 入队前）：权限层 raise 点 payload
                # 已带全时不重复覆盖。
                sig.payload.setdefault("tool_call_id", tool_id)
                sig.payload.setdefault("tool_name", spec.name)
                raise

            content = result.content
            # 合并 state_updates
            for k, v in result.state_updates.items():
                state_updates[k] = v
                # todos 更新推结构化事件
                if k == "todos" and event_sink:
                    event_sink({"type": "todos_update", "todos": v})

            if event_sink:
                event_sink({
                    "type": "tool_end",
                    "tool_name": spec.name,
                    "tool_id": tool_id,
                    "result": content,
                })

            tool_messages.append(ToolMsg(content=content, tool_call_id=tool_id))

        return tool_messages, state_updates

    # ─── 并发执行 ───

    def _execute_concurrent(
        self,
        tool_calls: list[dict[str, Any]],
        ctx: ToolContext,
        event_sink: EventSink | None,
    ) -> tuple[list[ToolMsg], dict[str, Any]]:
        """并发执行（多工具，全非审批）。

        并发执行逻辑（自旧 Registry 沿用）：
        - ThreadPoolExecutor + event_queue
        - 每个 worker 各自 copy_context（避免 Context.run 重入死锁）
        - 总超时 120s（避免单个卡死工具拖垮整轮）
        - shutdown(wait=False, cancel_futures=True) 不阻塞等待
        """
        event_queue: _queue.Queue = _queue.Queue()

        def _run_tool(tc_item: dict[str, Any]) -> None:
            spec = self._specs[tc_item["name"]]
            tool_id = tc_item["id"]

            event_queue.put({
                "type": "tool_start",
                "tool_name": spec.name,
                "tool_args": tc_item["args"],
                "tool_id": tool_id,
            })

            try:
                # 与串行路径一致传 tool_call_id：权限层 requireApproval 抛出的
                # InterruptSignal payload 才带得上本调用的真实 id
                result = self.execute(spec, tc_item["args"], ctx, tool_id)
                event_queue.put({
                    "type": "tool_end",
                    "tool_name": spec.name,
                    "tool_id": tool_id,
                    "result": result.content,
                    "state_updates": result.state_updates,
                })
            except InterruptSignal as sig:
                # P1-4：HITL 异常穿透——request_human_approval 静态
                # destructive=false 且无 evaluator，批量会落进并发路径，
                # 此前被下面的 except Exception 吞成"工具执行错误"文本，
                # 审批面板永不弹出。经队列转交主循环原样冒泡。
                # P1-4 半程：executor 直抛的 InterruptSignal（如
                # request_human_approval）payload 只有 action/details，缺
                # id/name——不入队补齐的话，快照存出 pending_tool_call_id=""，
                # resume 时 get_spec("") 落 None-spec 分支：审批决策被静默
                # 丢弃 + 产生空 id 孤儿 tool 消息（严格端点 400 卡死会话）。
                # setdefault：权限层 raise 点 payload 已带全时不重复覆盖。
                sig.payload.setdefault("tool_call_id", tool_id)
                sig.payload.setdefault("tool_name", spec.name)
                event_queue.put({"type": "interrupt", "signal": sig})
            except Exception as e:
                event_queue.put({
                    "type": "tool_end",
                    "tool_name": spec.name,
                    "tool_id": tool_id,
                    "result": f"工具执行错误: {e}",
                    "state_updates": {},
                })

        executor = ThreadPoolExecutor(max_workers=min(len(tool_calls), 4))
        try:
            # 每个 worker 各自 copy_context（修复 Context.run 重入死锁）
            def _submit(tc_item: dict[str, Any]):
                cvars = contextvars.copy_context()
                return executor.submit(cvars.run, _run_tool, tc_item)

            futures = {_submit(tc): tc["id"] for tc in tool_calls}
            tool_name_by_id = {tc["id"]: tc["name"] for tc in tool_calls}
            results_map: dict[str, str] = {}
            state_updates: dict[str, Any] = {}
            completed_count = 0
            _TOTAL_TIMEOUT = 120.0
            _batch_deadline = _t.time() + _TOTAL_TIMEOUT

            while completed_count < len(tool_calls):
                if _t.time() >= _batch_deadline:
                    # 总超时：未完成的工具标记超时
                    for fut, tid in futures.items():
                        if tid not in results_map:
                            results_map[tid] = "工具执行超时（整批 120s 上限）"
                            completed_count += 1
                            # P3（二轮审查）：对齐串行路径 _execute_with_timeout
                            # 的超时语义——超时后先调 executor 的 cancel 钩子
                            # （如 ShellExecutor 杀掉命令进程树），不再遗弃
                            # bash 孤儿进程；无 cancel 钩子的 executor 维持旧
                            # 语义。cancel 分桶签名探测（_cancel_takes_token）
                            # 与串行路径共用，按调用粒度传 id(ctx)。
                            timeout_spec = self._specs.get(tool_name_by_id.get(tid, ""))
                            cancel = getattr(timeout_spec.executor, "cancel", None) if timeout_spec else None
                            if callable(cancel):
                                try:
                                    if _cancel_takes_token(cancel):
                                        cancel(id(ctx))
                                    else:
                                        cancel()
                                except Exception:
                                    logger.warning(
                                        f"工具 {timeout_spec.name} 整批超时终止钩子异常",
                                        exc_info=True,
                                    )
                    logger.warning(
                        f"并发工具执行整批超时({_TOTAL_TIMEOUT}s)，剩余标记超时"
                    )
                    break

                try:
                    event = event_queue.get(timeout=0.1)
                except _queue.Empty:
                    # 检查是否有 worker 异常（兜底，防永久 busy-spin）
                    for fut, tid in futures.items():
                        if fut.done() and tid not in results_map:
                            exc = fut.exception()
                            results_map[tid] = (
                                f"工具执行错误: {exc}" if exc else "工具执行失败"
                            )
                            completed_count += 1
                    continue

                event_type = event.get("type")

                if event_type == "tool_start" and event_sink:
                    event_sink({
                        "type": "tool_start",
                        "tool_name": event["tool_name"],
                        "tool_args": event["tool_args"],
                        "tool_id": event["tool_id"],
                    })

                elif event_type == "interrupt":
                    # P1-4：InterruptSignal 冒泡到 stream_invoke 做审批；
                    # 已完成的兄弟结果挂 payload（P1-3 同格式），resume 后
                    # 由 agent 侧补齐配对
                    sig: InterruptSignal = event["signal"]
                    # 兄弟结果竞态：兄弟的 tool_end 可能已入队但排在
                    # interrupt 之后（主循环按 FIFO 先消费到 interrupt 就
                    # raise）——先非阻塞 drain 队列里已入队的 tool_end 再
                    # 收集，避免已跑完的兄弟被 resume 侧误合成"未执行"占位
                    while True:
                        try:
                            queued = event_queue.get_nowait()
                        except _queue.Empty:
                            break
                        if queued.get("type") == "tool_end":
                            results_map[queued["tool_id"]] = queued["result"]
                            # P3（二轮审查）：drain 只取 result 会丢兄弟的
                            # state_updates（如 write_todos 的 todos）——合并
                            # 进本次批量的 state_updates，随 payload 带出由
                            # agent 补发 todos_update（对齐正常路径语义）
                            for k, v in (queued.get("state_updates") or {}).items():
                                state_updates[k] = v
                        # 滞留事件照常过 sink：durable 日志与模型可见消息
                        # 保持一致（agent 侧 repair 只对"未完成"调用补写
                        # tool/result，已完成的不得缺 tool/call、tool/result）
                        if event_sink and queued.get("type") in ("tool_start", "tool_end"):
                            event_sink(queued)
                    completed = [
                        {"tool_call_id": tid, "content": content}
                        for tid, content in results_map.items()
                    ]
                    if completed:
                        sig.payload[COMPLETED_TOOL_MESSAGES_KEY] = completed
                    if state_updates:
                        # P3（二轮审查）：并发批量被中断时整批已收集的
                        # state_updates（含中断前正常消费的兄弟结果）只能
                        # 经 payload 带出（异常冒泡，无正常返回通道）
                        sig.payload["state_updates"] = state_updates
                    raise sig

                elif event_type == "tool_end":
                    if event_sink:
                        event_sink({
                            "type": "tool_end",
                            "tool_name": event["tool_name"],
                            "tool_id": event["tool_id"],
                            "result": event["result"],
                        })
                    results_map[event["tool_id"]] = event["result"]
                    for k, v in event.get("state_updates", {}).items():
                        state_updates[k] = v
                        if k == "todos" and event_sink:
                            event_sink({"type": "todos_update", "todos": v})
                    completed_count += 1
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        # 按原始顺序构造 ToolMsg 列表
        tool_messages = [
            ToolMsg(
                content=results_map.get(tc["id"], "工具执行超时"),
                tool_call_id=tc["id"],
            )
            for tc in tool_calls
        ]
        return tool_messages, state_updates

    # ─── 辅助 ───

    def _contains_approval_tool(self, tool_calls: list[dict[str, Any]]) -> bool:
        """预判是否含 needs_approval 工具。

        注意：needs_approval 不是静态 flag，而是由 permissions.decide() 动态求值。
        这里做"保守预判"——只要工具的 side_effects.destructive=True 或有 se_evaluator，
        就视为可能需要审批，走串行路径。

        真正的审批决策在 execute() 里由 decide() 做。
        """
        for tc in tool_calls:
            spec = self._specs.get(tc["name"])
            if spec is None:
                continue
            if spec.side_effects.destructive or spec.se_evaluator is not None:
                return True
        return False
