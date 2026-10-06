"""
============================================
子智能体工具
============================================
启动一个独立的子 Agent 执行隔离任务，返回结果后销毁。

改进记录：
- 初始实现
- 新增 StreamingSubAgent 类，支持流式 token 输出 + 并发执行
- 改进：添加超时控制、max_tokens 参数、工具继承、结构化错误返回
- T7：消息/工具全面 dict 化（OpenAI dict + ToolSpec.executor 执行）
"""

import logging
import threading
import contextvars
from dataclasses import dataclass
from typing import Callable, Generator, Sequence

from src.llm.client import LLMClient, get_current_llm_overrides
from src.tools.context import ToolContext
from src.types import InterruptSignal

from config import get_settings

logger = logging.getLogger("hermes.tools.sub_agent")

# 子智能体嵌套深度：用 contextvars 而非 threading.local。
# 原因：task 工具可能在 Registry 的 ThreadPoolExecutor 并发路径里执行（见
# registry_v3 的 _submit → copy_context）。threading.local 在每个 worker
# 线程各自独立、主线程的深度修改不传播，会导致并发子智能体各自看到 depth=0，
# 突破 sub_agent_max_depth 限制。contextvars 会随 copy_context() 正确传播到
# worker 且各自独立可写，与项目其它隔离（remember 的 user_id）范式统一。
_current_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "hermes_subagent_depth", default=0
)


def _get_current_depth() -> int:
    return _current_depth.get()


def _set_current_depth(depth: int):
    _current_depth.set(depth)


# ============================================
# 结构化错误返回
# ============================================


@dataclass
class SubAgentResult:
    """
    子智能体执行结果的结构化封装。

    Attributes:
        success: 是否执行成功
        result: 执行结果文本（成功时）
        error_type: 失败类型（失败时），如 timeout / depth_exceeded / llm_error / tool_error
        error_message: 失败详细描述
        suggestion: 对用户的建议（如重试、简化任务等）
    """

    success: bool
    result: str = ""
    error_type: str = ""
    error_message: str = ""
    suggestion: str = ""

    def to_text(self) -> str:
        """返回给 LLM 的纯文本格式"""
        if self.success:
            return self.result
        return (
            f"子智能体执行失败 [{self.error_type}]: {self.error_message}\n"
            f"建议: {self.suggestion}"
        )


# ============================================
# 流式子智能体
# ============================================


class StreamingSubAgent:
    """
    流式子智能体：在线程中运行，通过回调或生成器实时输出 token。

    用法：
        agent = StreamingSubAgent(name="researcher")
        # 生成器模式
        for token in agent.stream("分析这段文本"):
            print(token, end="", flush=True)
        # 回调模式
        agent.run("分析这段文本", callback=lambda t: print(t, end=""))
    """

    def __init__(
        self,
        name: str = "sub_agent",
        max_tokens: int | None = None,
        timeout: int | None = None,
        tools: Sequence = None,
        permission_mode: str | None = None,
    ):
        """
        初始化子智能体。

        Args:
            name: 子智能体名称
            max_tokens: 最大输出 token 数（None 则使用配置默认值）
            timeout: 超时时间秒数（None 则使用配置默认值，0 表示不限）
            tools: 可选的 ToolSpec 列表，传入后子智能体可调用这些工具
            permission_mode: 权限模式（None 时默认 before_changes；
                继承父会话 mode 由 _execute_task 传入）
        """
        self.name = name
        settings = get_settings()
        # 2026-06-23: 防御性强转，防止 LLM tool_call 传字符串数字（如 "60"）
        # 导致后续 self._timeout > 0 抛 TypeError。即使上层解析漏了也能兜住。
        if max_tokens is not None:
            max_tokens = int(max_tokens)
        if timeout is not None:
            timeout = int(timeout)
        self._max_tokens = max_tokens or settings.sub_agent_default_max_tokens
        self._timeout = timeout if timeout is not None else settings.sub_agent_default_timeout
        self._tools = list(tools) if tools else None
        self._cancelled = False
        self._timed_out = False

        # 工具执行上下文 + Registry（S1 修复：子代理工具执行必须过
        # ToolRegistryV3 的三层权限求值，不再直连 executor——否则
        # waker 白名单 / workspace chat-only / plan 模式全部被绕过）。
        # 非交互场景语义：
        #   - decide=deny            → 返回错误 ToolResult（LLM 可见失败原因）
        #   - decide=requireApproval → 按工具拒绝并回写 tool 消息，循环继续
        #     （无 HITL 通道，整段失败会让只读子任务也废掉）。
        from src.agent.registry_v3 import ToolRegistryV3
        self._registry = ToolRegistryV3()
        self._tool_ctx = ToolContext(
            permission_mode=permission_mode or "before_changes",
            caller_context="subagent",
        )

        # extra_body={}：不发 enable_thinking（对齐主 agent thinking=False，
        # 尊重 llama.cpp / Ollama / vLLM 默认）。模型四项吃主 agent 热切换。
        overrides = get_current_llm_overrides()
        self.llm = LLMClient(
            api_key=overrides.get("api_key") or settings.openai_api_key,
            base_url=overrides.get("base_url") or settings.openai_base_url,
            model=overrides.get("model") or settings.llm_model_name,
            temperature=0.3,
            max_tokens=self._max_tokens,
            request_timeout=int(getattr(settings, "llm_timeout", 120)),
            max_retries=2,
            extra_body={},
        )

        # 工具列表保存为 OpenAI tools 参数（ToolSpec.to_openai()）
        if self._tools:
            self._tools_openai = []
            for t in self._tools:
                try:
                    self._tools_openai.append(t.to_openai())
                except Exception:
                    pass
            if not self._tools_openai:
                self._tools_openai = None
                self._tools = None
            else:
                # Registry 绑定（execute 时按 name 查找 + 三层权限求值）
                self._registry.bind_tools(self._tools)
        else:
            self._tools_openai = None

    def cancel(self):
        """请求取消生成"""
        self._cancelled = True

    def _build_system_prompt(self) -> str:
        # 对齐 task 工具契约(sub_agent.py 的 task docstring):
        #   - 隔离执行、可并行、不依赖其他任务
        #   - 只关心最终结果,不关心中间步骤
        #   - instruction 应含期望的输出格式
        base = (
            f"你是一个名为「{self.name}」的子智能体,由主智能体通过 task 工具派出,隔离执行单一任务。\n\n"
            "## 输出契约\n"
            "- 直接输出最终结果,不复述任务、不加过程旁白、不加寒暄。\n"
            "- 如果指令指定了输出格式(JSON/表格/字数/结构),严格遵循。\n"
            "- 你是隔离执行的——指令之外的上下文一律不假设存在,不要反问(反问无人应答,只会浪费这一轮)。\n"
            "- 指令缺信息时,基于现有信息给出最佳结果,并标注你的假设。\n"
        )
        if self._tools:
            tool_names = ", ".join(t.name for t in self._tools)
            base += (
                "\n## 工具使用\n"
                f"可用工具: {tool_names}。\n"
                "- 工具调用要有明确目的;纯推理能解决的不调工具。\n"
                "- 工具失败时说明原因,并给出基于现有信息的最佳判断,而非静默放弃。\n"
            )
        return base

    def stream(self, instruction: str) -> Generator[str, None, None]:
        """
        流式生成子智能体的回复（支持超时控制）。

        2026-06-19: 修复 Bug 1 —— 把 run() 的 timer 逻辑下沉到 stream()，
        让流式路径也受超时保护。timer 在 finally 中取消，_stream_plain /
        _stream_with_tools 循环内检查 self._timed_out。

        Args:
            instruction: 任务指令

        Yields:
            str: 逐 token 输出
        """
        # 超时控制：启动 timer，超时触发 cancel() + 设置 _timed_out
        timer = None
        if self._timeout and self._timeout > 0:
            def on_timeout():
                self._timed_out = True
                self.cancel()
                logger.warning(f"子智能体 {self.name} 超时（{self._timeout}s）")

            timer = threading.Timer(self._timeout, on_timeout)
            timer.daemon = True
            timer.start()

        # 用量记账归属打上子代理标记（2026-09-19）：直接流式路径继承主
        # agent 的 usage ctx（caller=main/employee），用量明细里这些行与
        # 主 agent 的调用无法区分（req_messages 还是子代理自己的巨型
        # system 提示，观感很怪）。这里只覆盖 caller 为 task:{name}，
        # session/scope 原样继承——明细场景列显示 🤖 名字。
        from src.llm.client import (
            get_current_usage_ctx,
            reset_current_usage_ctx,
            set_current_usage_ctx,
        )
        _outer = get_current_usage_ctx()
        _ctx_token = set_current_usage_ctx(
            _outer["session_id"], _outer["scope"], caller=f"task:{self.name}")

        try:
            system_prompt = self._build_system_prompt()
            messages: list[dict] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": instruction},
            ]

            # 如果有工具绑定，使用带工具的 LLM 并处理工具调用循环
            if self._tools_openai is not None:
                yield from self._stream_with_tools(messages)
            else:
                yield from self._stream_plain(messages)

            # 流结束后检查是否超时，若是则 yield 超时提示
            if self._timed_out:
                yield (
                    f"\n\n[子智能体执行超过 {self._timeout} 秒被终止。"
                    f"建议：简化任务，或增加 timeout 参数值]"
                )
        finally:
            if timer is not None:
                timer.cancel()
            reset_current_usage_ctx(_ctx_token)

    def _stream_plain(self, messages: list) -> Generator[str, None, None]:
        """纯 LLM 流式输出（无工具）。

        LLMClient.stream_chat + stream_with_hard_timeout 保护，
        防止半开连接永久阻塞（主 agent 有此保护，子 agent 也必须有）。
        """
        from src.agent.llm_stream import stream_with_hard_timeout
        from config import settings

        timeout_s = float(getattr(settings, "llm_timeout", 120))
        try:
            for chunk in stream_with_hard_timeout(self.llm, messages, timeout_s=timeout_s):
                if self._cancelled:
                    logger.info(f"子智能体 {self.name} 已被取消")
                    break
                if chunk.content_delta:
                    yield chunk.content_delta
        except TimeoutError as e:
            logger.warning(f"子智能体 {self.name} LLM 流式超时: {e}")
            yield f"\n[子智能体 LLM 流式超时: {e}]"

    def _stream_with_tools(self, messages: list) -> Generator[str, None, None]:
        """带工具的流式输出。

        LLMClient.stream_chat + accumulate 拼接；工具经 ToolSpec.executor
        执行（args 容错解析），结果以 tool 消息回填。
        """
        from src.agent.llm_stream import stream_with_hard_timeout
        from src.llm.json_fix import safe_parse_tool_args as _safe_parse_args
        from config import settings

        timeout_s = float(getattr(settings, "llm_timeout", 120))
        tool_map = {t.name: t for t in self._tools} if self._tools else {}

        for _ in range(10):  # 防止无限循环
            if self._cancelled:
                break

            chunks = []
            try:
                for chunk in stream_with_hard_timeout(
                    self.llm, messages, tools=self._tools_openai, timeout_s=timeout_s
                ):
                    if self._cancelled:
                        break
                    chunks.append(chunk)
                    if chunk.content_delta:
                        yield chunk.content_delta
            except TimeoutError as e:
                logger.warning(f"子智能体 {self.name} LLM 流式超时: {e}")
                yield f"\n[子智能体 LLM 流式超时: {e}]"
                break

            if self._cancelled:
                break

            if not chunks:
                break

            ai_msg = LLMClient.accumulate(chunks)

            if not ai_msg.has_tool_calls:
                break

            # 把 AI 消息追加到 messages（OpenAI tool_calls 格式透传）
            messages.append({"role": "assistant", "content": ai_msg.content,
                             "tool_calls": ai_msg.tool_calls})

            # 执行工具调用（S1：经 ToolRegistryV3，三层权限生效）。
            # requireApproval：子代理无 HITL 通道——按工具拒绝并继续
            # （回写 tool 消息保证配对，不把整次 task 打成失败）。
            for tc in ai_msg.tool_calls:
                tool_name = tc["function"]["name"]
                tool_args = _safe_parse_args(tc["function"].get("arguments", ""))
                spec = tool_map.get(tool_name)
                if spec is not None:
                    try:
                        result = self._registry.execute(
                            spec, tool_args, self._tool_ctx,
                            tool_call_id=tc.get("id", ""),
                        )
                        content = result.content if hasattr(result, "content") else str(result)
                        yield f"\n[工具 {tool_name} 执行结果]: {content}\n"
                        messages.append({
                            "role": "tool", "content": content,
                            "tool_call_id": tc.get("id", ""),
                        })
                    except InterruptSignal as sig:
                        action = sig.payload.get("action", f"执行工具 {tool_name}")
                        content = (
                            f"错误：子智能体无人工审批通道，已跳过需审批的操作"
                            f"（{action}）。请改用只读工具（web_fetch/web_search/"
                            f"bash 只读命令）基于现有信息给出结果；"
                            f"写文件/破坏性操作留给主会话。"
                        )
                        logger.info(
                            f"子智能体 {self.name} 跳过需审批工具: "
                            f"tool={tool_name}, action={action}"
                        )
                        yield f"\n[工具 {tool_name} 已跳过]: {content}\n"
                        messages.append({
                            "role": "tool", "content": content,
                            "tool_call_id": tc.get("id", ""),
                        })
                    except Exception as e:
                        yield f"\n[工具 {tool_name} 执行失败]: {e}\n"
                        messages.append({
                            "role": "tool", "content": f"工具执行失败: {e}",
                            "tool_call_id": tc.get("id", ""),
                        })
                else:
                    yield f"\n[工具 {tool_name} 未找到]\n"
                    messages.append({
                        "role": "tool", "content": f"工具 {tool_name} 未找到",
                        "tool_call_id": tc.get("id", ""),
                    })

    def run(self, instruction: str, callback: Callable[[str], None] | None = None) -> SubAgentResult:
        """
        运行子智能体，可选通过回调实时输出 token。支持超时控制。

        2026-06-19: 超时控制已下沉到 stream()，本方法不再启动 timer，
        仅消费 stream() 的输出并检查 self._timed_out 标志。

        Args:
            instruction: 任务指令
            callback: token 回调函数（可选）

        Returns:
            SubAgentResult: 结构化的执行结果
        """
        result_text = ""

        try:
            for token in self.stream(instruction):
                result_text += token
                if callback:
                    callback(token)

            if self._timed_out:
                return SubAgentResult(
                    success=False,
                    result=result_text,
                    error_type="timeout",
                    error_message=f"子智能体执行超过 {self._timeout} 秒被终止",
                    suggestion="尝试简化任务，或增加 timeout 参数值",
                )

            return SubAgentResult(success=True, result=result_text)

        except InterruptSignal as sig:
            # 防御：工具循环已按条拒绝并回写 tool 消息，正常路径不再冒泡。
            # 若仍漏到这里，保持结构化失败，避免把审批信号吞成 llm_error。
            action = sig.payload.get("action", "工具执行")
            tool_name = sig.payload.get("tool_name", "")
            logger.info(
                f"子智能体 {self.name} 工具需审批（漏出循环，转结构化失败）: "
                f"tool={tool_name}, action={action}"
            )
            return SubAgentResult(
                success=False,
                result=result_text,
                error_type="approval_required",
                error_message=f"子智能体工具需人工审批（{action}），非交互场景无法完成审批",
                suggestion="在主会话直接执行该操作，或按需放宽 permission_mode 后重试",
            )
        except Exception as e:
            logger.error(f"子智能体 {self.name} 执行失败: {e}")
            return SubAgentResult(
                success=False,
                error_type="llm_error",
                error_message=str(e),
                suggestion="检查 LLM 配置和网络连接，然后重试",
            )


# ============================================
# 工具入口
# ============================================


def task(
    instruction: str,
    subagent_name: str = "sub_agent",
    timeout: int | None = None,
    max_tokens: int | None = None,
    inherit_tools: bool = True,
    permission_mode: str | None = None,
    allowed_tools: set[str] | None = None,
) -> str:
    """
    启动一个短期子智能体来处理独立的复杂任务。
    子智能体是临时的，完成后返回一个结果。

    何时使用：
    - 任务复杂、多步骤，且可以完全隔离执行
    - 任务不依赖其他任务，可并行运行
    - 需要大量 token 或专注推理的任务
    - 只关心最终结果，不关心中间步骤

    Args:
        instruction: 详细的任务描述，包括期望的输出格式
        subagent_name: 子智能体名称（可选，用于日志）
        timeout: 超时（秒），默认使用配置值（sub_agent_default_timeout，500s），0 表示不限
        max_tokens: 最大输出 token 数，默认使用配置值（sub_agent_default_max_tokens）
        inherit_tools: 是否继承当前会话可用工具，默认 True。纯推理传 False
        permission_mode: 权限模式（父会话 ctx 透传）
        allowed_tools: 父级调用级工具白名单（父会话 ctx.allowed_tools 透传）。
            None = 不过滤；非 None 时子代理工具集被收窄到白名单内
            （waker / wakerflow 的 cfg.tools 作用域借此覆盖子代理）。

    Returns:
        子智能体的执行结果（成功为文本，失败为结构化错误信息）
    """
    settings = get_settings()
    max_depth = settings.sub_agent_max_depth

    # 深度检查
    if _get_current_depth() >= max_depth:
        result = SubAgentResult(
            success=False,
            error_type="depth_exceeded",
            error_message=f"已达到最大子智能体嵌套深度 ({max_depth})",
            suggestion="避免在子智能体中再次调用 task 工具",
        )
        return result.to_text()

    _set_current_depth(_get_current_depth() + 1)

    try:
        logger.debug(f"启动子智能体: name={subagent_name}, depth={_get_current_depth()}")

        # 工具继承（S1 修复：必须经 resolve_tools 组装，天然继承一切门禁——
        # config_guard（shell_enabled 等）、workspace chat-only 模式过滤、
        # blocked_in（caller_context=subagent）、调用级 allowed_tools 白名单。
        # 旧实现 get_all_tools() 是未过滤全量，等于把主线程的所有门禁全部绕开）
        tools_to_inject = None
        if inherit_tools:
            from src.tools.resolve import resolve_tools
            resolve_ctx = ToolContext(
                permission_mode=permission_mode or "before_changes",
                caller_context="subagent",
                allowed_tools=allowed_tools,
            )
            tools_to_inject = resolve_tools(resolve_ctx, settings)

        sub_agent = StreamingSubAgent(
            name=subagent_name,
            max_tokens=max_tokens,
            timeout=timeout,
            tools=tools_to_inject,
            permission_mode=permission_mode,
        )
        result = sub_agent.run(instruction)

        logger.debug(f"子智能体完成: name={subagent_name}, success={result.success}, 结果长度={len(result.result)}")
        return result.to_text()

    except Exception as e:
        logger.error(f"子智能体执行失败: {e}")
        error_result = SubAgentResult(
            success=False,
            error_type="tool_error",
            error_message=str(e),
            suggestion="检查配置和参数后重试",
        )
        return error_result.to_text()

    finally:
        _set_current_depth(_get_current_depth() - 1)


# ════════════════════════════════════════════════════════════════
# PythonExecutor 入口（tools/task.yaml）
# ════════════════════════════════════════════════════════════════

def _execute_task(
    instruction: str,
    subagent_name: str = "sub_agent",
    timeout: int | None = None,
    max_tokens: int | None = None,
    inherit_tools: bool = True,
    *,
    ctx=None,
) -> str:
    """PythonExecutor 入口。

    包装 task 工具逻辑（深度检查 + StreamingSubAgent）。
    ctx.permission_mode / ctx.allowed_tools 透传给 task——子 agent 继承
    父会话 mode 与调用级工具白名单（waker/flow 的 cfg.tools 作用域）。
    """
    return task(
        instruction=instruction,
        subagent_name=subagent_name,
        timeout=timeout,
        max_tokens=max_tokens,
        inherit_tools=inherit_tools,
        permission_mode=getattr(ctx, "permission_mode", None) if ctx is not None else None,
        allowed_tools=getattr(ctx, "allowed_tools", None) if ctx is not None else None,
    )
