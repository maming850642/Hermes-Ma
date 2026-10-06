"""
============================================
HermesAgentV3 —— 手写 ReAct 循环 + 异常式 HITL
============================================
Hermes 多智能体系统的唯一 Agent 引擎（旧图引擎版 HermesAgent 已随
框架依赖退役删除）。核心设计：

    - 手写 ReAct 循环（参考 employee_worker._react_loop + sub_agent._stream_with_tools）
    - InterruptStore 内存级状态（等价旧 checkpointer，重启即丢）
    - yield 事件 generator（直接驱动，无双模式 stream）
    - InterruptSignal 异常冒泡 → 存快照 → yield human_approval_request
    - resume 时从快照恢复 + 重新求值权限（mode 切换自动放行）

T6 循环事件化（行为不变）：
    - __init__ 可注入 kernel_ctx（组合根 Context）：agent 在其 scope("agent-v3")
      上注册全部默认监听器，外部监听器经事件冒泡可见 agent 环节；未注入时
      建 standalone Context——两条路径注册同一批监听器、循环永远走事件分发
    - 事件契约（模块 import 时声明）：
        "agent/pre-step"      waterfall —— payload 是 StepRequest（见下）
        "agent/request"       waterfall —— payload {messages, tools}（发 LLM 前）
        "agent/turn-stopping" serial   —— 循环结束、complete 前（无默认监听器）
        "llm/stream"          waterfall —— payload 是 llm_plugin.StreamRequest
        （tools/pre-execute / tools/execute / tools/post-execute 见 registry_v3）
    - durable 会话事件（T3 SessionLog）由循环本体写入：turn/start、
      user/message、assistant/message、tool/call、tool/result、compact/applied、
      turn/end；session_id 为空串时不写

业务逻辑复用：
    - ContextManager.build_llm_messages（构建 LLM 消息，OpenAI dict）
    - ContextManager.compact_messages（压缩，OpenAI dict）
    - MemoryOrchestrator.retrieve_with_detail（记忆检索）
    - ToolRegistryV3（工具执行 + 三层权限）

消息格式（T7 dict 迁移）：内部 state["messages"] 直接存 OpenAI dict
（system/user/assistant/tool；assistant 可带 tool_calls，tool 带 tool_call_id）。
LLM 返回的 AIMsg 内联转 dict 入 state；推理内容存消息附带的 "reasoning" 键
（不进 LLM payload，供前端恢复推理面板）。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Generator, TYPE_CHECKING

from src.agent.context import ContextManager
from src.agent.hitl import (
    COMPLETED_TOOL_MESSAGES_KEY,
    InterruptSignal,
    InterruptSnapshot,
    InterruptStore,
)
from src.agent.listeners import register_default_listeners
from src.agent.llm_stream import stream_with_hard_timeout
from src.agent.memory_orch import MemoryOrchestrator
from src.agent.mode_guidance import build_mode_prompt_section
from src.agent.registry_v3 import ToolRegistryV3
from src.agent.session_log import (
    ASSISTANT_MSG,
    COMPACT_APPLIED,
    LLM_ERROR,
    TOOL_CALL,
    TOOL_RESULT,
    TURN_END,
    TURN_START,
    USER_MSG,
    build_compact_applied_payload,
    sid_scope,
)
from src.agent.stream_consumer import StreamConsumption, consume_llm_stream
from src.cordis import events
from src.cordis.context import Context
from src.llm.client import LLMClient
from src.plugins.llm_plugin import StreamRequest
from src.tools.context import (
    DEFAULT_PERMISSION_MODE,
    ToolContext,
)
from src.tools.resolve import resolve_tools

if TYPE_CHECKING:  # 仅类型标注（避免运行期多余依赖）
    from src.agent.session_log import SessionLog

logger = logging.getLogger("hermes.agent.v3")

# ── R3-15 增长治理：durable tool/result 体积上限 ──
# 事件日志是 append-only 单表，超大工具结果（如全量文件读取）会让
# events 表与 derive 投影无限膨胀。durable 侧截断为前 64KB + 标记；
# UI 的 tool_end 事件（前端展示）不受影响。
TOOL_RESULT_MAX_BYTES = 64 * 1024
TOOL_RESULT_TRUNCATION_MARK = "\n...(已截断，原长 {n} 字节)"

# ── P2-9：LLM 调用失败的旁路事件 ──
# 错误文本不再作为 assistant 消息写入持久历史（不进桶、不进
# build_llm_messages / derive_messages 投影——session_log 对未知事件类型
# 忽略），只落 LLM_ERROR 事件（常量定义在 session_log，与其他事件类型
# 同一清单）供审计与 UI 读取；用户经 complete 事件仍能看到错误


def _truncate_for_durable(content: Any) -> Any:
    """tool/result content 超 64KB（UTF-8 字节）→ 前 64KB + 截断标记。

    非 str 原样返回（防御异常 payload 形状）；多字节字符在字节边界
    切断时按 errors="ignore" 丢弃残缺尾字符。
    """
    if not isinstance(content, str):
        return content
    raw = content.encode("utf-8", errors="replace")
    if len(raw) <= TOOL_RESULT_MAX_BYTES:
        return content
    kept = raw[:TOOL_RESULT_MAX_BYTES].decode("utf-8", errors="ignore")
    return kept + TOOL_RESULT_TRUNCATION_MARK.format(n=len(raw))


# ── 事件契约：模块 import 时声明（declare 幂等，同模式重复声明无害）──
# pre-step：每次迭代构建 LLM 消息前（默认监听器：记忆注入/压缩阈值/模式指导）
events.declare("agent/pre-step", "waterfall")
# request：发 LLM 前（payload {messages, tools}，返回值用于实际请求）
events.declare("agent/request", "waterfall")
# turn-stopping：循环结束、complete 前（纯观察，无默认监听器）
events.declare("agent/turn-stopping", "serial")


@dataclass
class StepRequest:
    """"agent/pre-step" waterfall 事件的 payload。

    监听器约定：
    - 可改写 messages（循环用它构建 LLM 消息，只影响本步，不改内部 state）
    - 可置 reject=True（循环直接收 turn：不发 LLM 请求）
    - 可读写 notes（与默认监听器交换结果）
    - 调 next() 委托后续监听器（不调即短路）

    notes 由循环预填的键（监听器只读这些、写自己的键）：
    - user_id / session_id：本次 stream_invoke 的标识
    - permission_mode：本次 ToolContext 的权限模式
    - first_step：是否循环首步（记忆检索与压缩阈值检查的频次闸门）
    - memory_pending：仅 fresh 轮首步为 True（记忆检索频次与旧实现一致：
      每 stream_invoke 一次、resume 不检索）
    - precheck_pct：仅首步出现（fresh 轮 = compact_threshold_pct 入参；
      resume 轮 = None 即配置默认）。键存在才做 token 阈值检查

    默认监听器产出的键：
    - memories / memory_event（a 记忆注入）
    - compact_needed（b 压缩阈值超限）
    - mode_guidance（c 模式指导文本）
    """

    messages: list
    user_input: str
    reject: bool = False
    notes: dict = field(default_factory=dict)


class HermesAgentV3:
    """V3 Agent：手写 ReAct 循环 + 异常式 HITL。

    用法（stream_invoke 签名与历史调用方约定一致）：
        agent = HermesAgentV3(memory_manager)
        for event in agent.stream_invoke(user_id=..., user_input=..., ...):
            handle(event)
    """

    def __init__(
        self,
        memory_manager,
        tool_callback=None,
        registry: ToolRegistryV3 | None = None,
        interrupt_store: InterruptStore | None = None,
        kernel_ctx: "Context | None" = None,
        session_log: "SessionLog | None" = None,
    ):
        from config import get_settings

        self.settings = get_settings()

        # 业务模块（复用现有，不动）
        self.context_manager = ContextManager()
        self.memory_orchestrator = MemoryOrchestrator(memory_manager)
        self.memory = memory_manager  # 向后兼容

        # V3 工具注册中心（T4：组合根可注入 ctx.tools.registry——
        # 带 kernel_ctx 的实例，权限段走 tools/pre-execute 事件分发）
        self.registry = registry if registry is not None else ToolRegistryV3()

        # HITL 存储（T4：组合根可注入 InterruptStore(session_log=ctx.sessions)）
        self.interrupt_store = interrupt_store if interrupt_store is not None else InterruptStore()

        # T6 事件化作用域：kernel_ctx 给定 → 子作用域（外部监听器在祖先 ctx
        # 注册、经冒泡参与 agent 各环节分发）；未给定 → standalone Context。
        # 两条路径行为完全一致：默认监听器同一批、循环永远走事件分发
        if kernel_ctx is not None:
            self.scope = kernel_ctx.scope("agent-v3")
        else:
            self.scope = Context("agent-v3-standalone")
        self._register_default_listeners()

        # T3/T6 durable 事件日志：显式注入优先；未注入时从 kernel_ctx 的
        # sessions 服务自动接线（worker 经 boot_context 即此路径）
        self._session_log = session_log
        if self._session_log is None and kernel_ctx is not None:
            self._session_log = kernel_ctx.try_get("sessions")
        # 本次 stream_invoke 的 durable 会话 ID（空串 = 不写事件）
        self._durable_sid = ""

        # 会话级状态
        self._permission_mode = DEFAULT_PERMISSION_MODE

        # 注入 memory_manager 给 remember 工具（与现有一致）
        from src.tools.remember import set_memory_manager
        set_memory_manager(memory_manager)

        # LLM 客户端（延迟初始化，避免启动时连 LLM）
        self._llm_client: LLMClient | None = None
        # LLM 参数覆盖（worker prefs 经 set_llm_params 注入；None = 用默认）
        self._llm_temperature: float | None = None
        self._llm_max_tokens: int | None = None
        # 模型热切换覆盖（Web 系统配置保存 / CLI /model 经 set_llm_params 注入；
        # 键 = model/base_url/api_key/context_window，非 None 才更新）
        self._llm_overrides: dict = {}
        # 上次构造 LLMClient 的参数签名（_get_llm_client 失效判断用）
        self._llm_client_build_sig: tuple | None = None
        # 主思考开关（Web「主思考」chip / CLI --thinking；翻转时重建 LLM 客户端）
        self._thinking: bool = False

        # 工具回调（向后兼容，event_sink 用）
        self._tool_callback = tool_callback

    # ════════════════════════════════════════════════════════════════
    # 默认监听器注册（kernel / standalone 两路径同一批；注册逻辑在
    # src/agent/listeners.py，监听器本体因读写 self 状态留在本类）
    # ════════════════════════════════════════════════════════════════

    def _register_default_listeners(self) -> None:
        """在 scope 上注册 agent 循环的全部默认监听器。

        注册顺序即洋葱外→内顺序（本 scope 先于祖先链）：
        pre-step: 记忆注入 → 压缩阈值 → 模式指导；llm/stream: 真流式实现。
        """
        register_default_listeners(
            self.scope,
            prestep_memory=self._prestep_memory_listener,
            prestep_compact=self._prestep_compact_listener,
            prestep_mode=self._prestep_mode_listener,
            llm_stream_default=self._llm_stream_default,
        )

    # ── agent/pre-step 默认监听器（a/b/c）──

    def _prestep_memory_listener(self, req: StepRequest, next):
        """a) 记忆注入（原 _retrieve_memory 内核逻辑，频次闸门见 notes）。"""
        if req.notes.get("memory_pending"):
            try:
                # 会话绑定项目（contextvar，worker 开轮注入）；None = CLI 等
                # 未设置路径，回落全局激活指针。多项目并发时检索范围跟会话
                # 归属走，不跟"顶栏此刻停在哪"走。
                from src.tools.remember import get_current_project
                retrieve_detail = self.memory_orchestrator.retrieve_with_detail(
                    user_id=req.notes.get("user_id", ""),
                    current_input=req.user_input,
                    messages=req.messages,
                    session_id=req.notes.get("session_id", ""),
                    project=get_current_project(),
                )
                req.notes["memories"] = retrieve_detail.get("memories", [])
                req.notes["memory_event"] = {
                    "type": "memory_search",
                    "query": retrieve_detail.get("query", ""),
                    "raw_count": retrieve_detail.get("raw_count", 0),
                    "hit_count": retrieve_detail.get("hit_count", 0),
                    "hits": retrieve_detail.get("hits", []),
                }
            except Exception as e:
                logger.warning(f"记忆检索失败（忽略，继续对话）: {e}")
        return next()

    def _prestep_compact_listener(self, req: StepRequest, next):
        """b) 压缩阈值判断（原 _precheck 语义：仅首步检查一次）。"""
        if "precheck_pct" in req.notes and self._compact_threshold_hit(
            req.messages, req.notes.get("precheck_pct"),
        ):
            req.notes["compact_needed"] = True
        return next()

    def _prestep_mode_listener(self, req: StepRequest, next):
        """c) 模式指导文本（循环注入 system prompt 用）。"""
        req.notes["mode_guidance"] = build_mode_prompt_section(
            req.notes.get("permission_mode") or DEFAULT_PERMISSION_MODE,
        )
        return next()

    def _compact_threshold_hit(self, messages: list, compact_pct_override: int | None) -> bool:
        """token 阈值判断（原 _precheck 主体，语义零变化：达到阈值才压缩）。"""
        if not messages:
            return False

        pct = compact_pct_override
        if pct is None:
            pct = int(self.settings.get("compact_threshold_pct", 80) or 80)
        if pct <= 0:
            return False

        try:
            from src.agent.token_counter import count_tokens
            from src.agent.context_window import get_context_window
            current_tokens = count_tokens(messages)
            # 窗口覆盖优先（模型热切换注入的 context_window，置于 settings
            # 值之上）；无覆盖时回落原有三级解析（配置 > 探测 > 保守默认）
            overrides = getattr(self, "_llm_overrides", None) or {}
            context_window = overrides.get("context_window") or get_context_window()
            reserve = int(getattr(self.settings, "max_tokens", 0) or 0)
            threshold = int(context_window * (pct / 100.0)) - reserve
            if threshold <= 0:
                logger.error(
                    f"压缩阈值={threshold}（window={context_window}×{pct}%-max_tokens={reserve}≤0），"
                    f"配置不合理，跳过自动压缩"
                )
                return False
            return current_tokens >= threshold
        except Exception:
            return False  # token 计数失败不阻塞

    # ── llm/stream 默认监听器 ──

    def _llm_stream_default(self, req: StreamRequest, next):
        """真流式实现（stream_with_hard_timeout）入 req.chunks 结果槽。

        客户端优先用 req.client（调用方自备），与插件默认监听器语义一致。
        cancel_event 取 req.cancel_event——ReAct 循环构造 StreamRequest 时
        已把本轮预置的 _stream_cancel_event 放进请求（二轮审查第 12 项接线，
        kernel 路径与 standalone 路径防线 C 语义一致：消费方超时/中途停止
        置位 → worker 线程断流退出）。
        """
        if req.chunks is None:
            client = req.client if req.client is not None else self._get_llm_client()
            req.chunks = stream_with_hard_timeout(
                client, req.messages,
                tools=req.tools, timeout_s=req.timeout_s,
                temperature=req.temperature, max_tokens=req.max_tokens,
                cancel_event=req.cancel_event
                or getattr(self, "_stream_cancel_event", None),
            )
        return next()

    # ════════════════════════════════════════════════════════════════
    # durable 事件（T3 SessionLog 写入，尽力而为：失败只告警不阻塞）
    # ════════════════════════════════════════════════════════════════

    def _durable_append(self, type_: str, payload: dict) -> None:
        """追加一条 durable 会话事件（无日志/无会话 ID 时 no-op）。

        R3-15：tool/result 的 content 超 64KB 时截断（事件日志防膨胀；
        UI 事件不经过这里，保持全量）。
        """
        log = self._session_log
        if log is None or not self._durable_sid:
            return
        if type_ == TOOL_RESULT and isinstance(payload, dict):
            payload = dict(payload)
            payload["content"] = _truncate_for_durable(payload.get("content"))
        try:
            log.append(self._durable_sid, type_, payload)
        except Exception:
            logger.warning(
                f"事件日志写入失败: {type_} sid={self._durable_sid}", exc_info=True,
            )

    def _durable_ui_event(self, event: dict) -> None:
        """UI 事件 → durable 事件映射（tool_start→tool/call；tool_end→tool/result）。"""
        etype = event.get("type")
        if etype == "tool_start":
            self._durable_append(TOOL_CALL, {
                "tool_call_id": event.get("tool_id", ""),
                "name": event.get("tool_name", ""),
                "args": event.get("tool_args") or {},
            })
        elif etype == "tool_end":
            tool_result_payload = {
                "tool_call_id": event.get("tool_id", ""),
                "name": event.get("tool_name", ""),
                "content": event.get("result", ""),
            }
            if "ok" in event:
                tool_result_payload["ok"] = event.get("ok")
            self._durable_append(TOOL_RESULT, tool_result_payload)

    def _durable_tool_result(self, tool_call_id: str, tool_name: str, content: str) -> None:
        """补写一条 tool/result（R2 悬空 tool_calls 修复）。

        中断/审批拒绝/force_deny 拦截/工具消失等路径不执行工具，但中断轮
        已写 assistant(tool_calls)+tool/call——这里补上配对的 tool/result，
        保证事件流自洽（derive_messages 不依赖占位合成即可得到合法序列）。
        """
        self._durable_append(TOOL_RESULT, {
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": content,
        })

    def _workspace_mode(self) -> "str | None":
        """当前工作区模式（turn/start 记录用；异常按未配置 None）。"""
        try:
            from src.workspace import state as workspace_state
            st = workspace_state.current_status()
            return st.mode if st is not None else None
        except Exception:
            return None

    def _durable_turn_start(self, user_input: str) -> None:
        """fresh 轮开轮：turn/start（input + 本轮工作区模式）。"""
        self._durable_append(TURN_START, {
            "input": user_input,
            "workspace_mode": self._workspace_mode(),
        })

    def _durable_turn_start_resume(self, decision: str) -> None:
        """resume 轮开轮：turn/start（审批恢复不是新 user 消息，不记 user/message）。"""
        self._durable_append(TURN_START, {"input": "(resume)", "resume": decision})

    def _durable_turn_end(self, message_count: int, cancelled: bool = False) -> None:
        """收轮。必须在 yield complete / human_approval_request 之前写——
        消费方收到这两个事件即 break + close()，generator 挂起点之后的
        代码不再执行。cancelled=True 时带标记（回放/审计可见：这轮是
        被用户主动停止的，非自然完结）。"""
        payload: dict = {"message_count": message_count}
        if cancelled:
            payload["cancelled"] = True
        self._durable_append(TURN_END, payload)

    def _durable_close_turn(self, content: str, message_count: int,
                            reasoning: str = "", cancelled: bool = False) -> None:
        """complete 前的 durable 收尾：最终答复 + 收轮（对齐旧 worker shim）。

        reasoning 非空时一并落盘——事件优先加载（T9）投影据此回放推理面板。
        """
        payload: dict = {"content": content}
        if reasoning:
            payload["reasoning"] = reasoning
        self._durable_append(ASSISTANT_MSG, payload)
        self._durable_turn_end(message_count, cancelled=cancelled)

    # ════════════════════════════════════════════════════════════════
    # LLM 客户端 + 工具绑定
    # ════════════════════════════════════════════════════════════════

    def set_llm_params(self, temperature: float | None = None, max_tokens: int | None = None,
                       model: str | None = None, base_url: str | None = None,
                       api_key: str | None = None, context_window: int | None = None,
                       clear_model_overrides: bool = False) -> None:
        """覆盖 LLM 参数（worker prefs / 模型热切换注入点，取代旧 llm_with_tools.bind 路径）。

        temperature/max_tokens：None = 该项用默认（temperature=0.7、
        max_tokens=settings）——不受 clear_model_overrides 影响。
        模型切换四项（model/base_url/api_key/context_window）语义按
        clear_model_overrides 分两态：
        - False（默认，prefs 注入形状）：None = 保持现值不变——每轮 chat
          的 prefs 注入只传温度/上限，不得把已热切换的模型冲掉；
        - True（模型切换专用）：四键全量覆盖——非 None 值写入 override，
          None/空串 = 显式清除该项 override（回退 settings/config）。解决
          覆盖残留：P 档案（有 key+ctx）切到 Q 档案（无 key）或回默认后，
          P 的 api_key/context_window 不得残留在 override 里继续生效。
        已构造的客户端一律作废，下次使用时按新参数重建。
        """
        self._llm_temperature = temperature
        self._llm_max_tokens = max_tokens
        overrides = getattr(self, "_llm_overrides", None)
        if overrides is None:
            overrides = {}
            self._llm_overrides = overrides
        for name, value in (("model", model), ("base_url", base_url),
                            ("api_key", api_key), ("context_window", context_window)):
            if value is not None and value != "":
                overrides[name] = value
            elif clear_model_overrides:
                # 清除通道：None/空串 = 清掉该项 override（仅 clear 模式；
                # 默认模式下 None = 保持现值，空串历史上不会出现——
                # 切换发送方约定空串表清除）
                overrides.pop(name, None)
        self._llm_client = None

    def _get_llm_client(self) -> LLMClient:
        """延迟初始化 LLM 客户端（尊重 set_llm_params 的覆盖值）。

        thinking 偏好或模型覆盖项（model/base_url/api_key）变化时重建
        缓存客户端——这些参数在构造期固化，用构建签名比对检测变化。
        """
        desired_extra_body = (
            {"chat_template_kwargs": {"enable_thinking": True}}
            if getattr(self, "_thinking", False) else {}
        )
        overrides = getattr(self, "_llm_overrides", None) or {}
        build_sig = (
            desired_extra_body,
            overrides.get("model"), overrides.get("base_url"), overrides.get("api_key"),
        )
        # 仅当缓存的是真实 LLMClient 且构造参数变化时才重建；
        # 测试注入的 fake 客户端（无对应属性）原样保留。
        if (isinstance(self._llm_client, LLMClient)
                and getattr(self, "_llm_client_build_sig", None) != build_sig):
            self._llm_client = None  # 思考开关/模型覆盖变化 → 按新参数重建
        if self._llm_client is None:
            # max_tokens：显式配置（settings/prefs）优先；均未设置则不传字段，
            # 由服务端默认决定（旧版隐式 2000 已随模型上下文扩容移除）
            default_max_tokens = getattr(self.settings, "max_tokens", None)
            self._llm_client = LLMClient(
                api_key=overrides.get("api_key") or self.settings.openai_api_key,
                base_url=overrides.get("base_url") or self.settings.openai_base_url,
                model=overrides.get("model") or self.settings.llm_model_name,
                temperature=self._llm_temperature if self._llm_temperature is not None else 0.7,
                max_tokens=int(self._llm_max_tokens) if self._llm_max_tokens is not None
                else (int(default_max_tokens) if default_max_tokens else None),
                request_timeout=int(getattr(self.settings, "llm_timeout", 120)),
                max_retries=2,
                extra_body=desired_extra_body,
            )
            self._llm_client_build_sig = build_sig
        return self._llm_client

    def get_llm_client(self) -> LLMClient:
        """公开访问当前缓存客户端（记忆模块复用同一 client，见
        MemoryManager.set_llm_provider）；思考开关翻转时返回重建后的新实例。"""
        return self._get_llm_client()

    def _resolve_and_bind_tools(self, ctx: ToolContext) -> list[dict]:
        """动态组装工具列表 + 绑定到 Registry + 返回 OpenAI tools 参数。"""
        specs = resolve_tools(ctx, self.settings, include_mcp=True)
        self.registry.bind_tools(specs)
        return [s.to_openai() for s in specs]

    # ════════════════════════════════════════════════════════════════
    # 权限模式
    # ════════════════════════════════════════════════════════════════

    def set_permission_mode(self, mode: str) -> None:
        """切换权限模式（CLI /mode 命令、Web 按钮调用）。"""
        self._permission_mode = mode
        logger.info(f"权限模式切换为: {mode}")

    def get_permission_mode(self) -> str:
        return self._permission_mode

    # ════════════════════════════════════════════════════════════════
    # stream_invoke —— 主入口
    # ════════════════════════════════════════════════════════════════

    def stream_invoke(
        self,
        user_id: str,
        user_input: str,
        session_messages: list | None = None,
        session_id: str = "",
        todos: list | None = None,
        virtual_fs: dict | None = None,
        thread_id: str | None = None,
        resume_payload: Any = None,
        llm_override: Any = None,
        role: str | None = None,
        thinking: bool = False,
        images: list[str] | None = None,
        compact_threshold_pct: int | None = None,
        waker_persona: str | None = None,
        allowed_tools: set[str] | None = None,
        caller_context: str = "main",
        should_cancel: Callable[[], bool] | None = None,
        prelogged_turn: bool = False,
    ) -> Generator[dict, None, None]:
        """流式执行一轮 agent 对话。

        签名与历史调用方（CLI/Web worker）约定一致。
        yield 事件类型与现有兼容（token/tool_start/tool_end/...）。

        Args：与历史调用方（CLI/Web worker）约定一致。

        should_cancel: 协作式取消探针（worker 传 `lambda: _cancel_event.is_set()`）。
            在 ReAct 轮间、LLM chunk 流、工具批量执行之间轮询；置位即干净
            收尾（部分内容 + ⏹️标记 + cancelled 轮跳过记忆沉淀）。缺省 None
            = 永不取消（CLI 等调用方行为不变）。

        llm_override：兼容形参，未使用（V3 用 LLMClient 直连 +
        set_llm_params 覆盖参数）。

        thinking：主思考开关。True 时请求携带 enable_thinking 提示；
        False/缺省 = 不发 kwarg，由后端默认决定。翻转即时生效
        （缓存客户端按需重建）；reasoning 经 reasoning_token 事件流出，
        并在 durable assistant 消息中持久化（刷新/回放不丢）。

        waker_persona: 数字员工人格段（由 src.waker.load_persona_prompt 组装）。
            非空时透传给 ContextManager.build_llm_messages → build_system_prompt，
            作为独立 section 注入 system prompt。runner 跑 waker 任务时使用。

        allowed_tools: 调用级工具白名单（T8a）。None = 不过滤；非 None 时
            resolve_tools 仅保留名字在集合内的工具。waker/flow runner 把
            cfg.tools 传进来，作用域天然覆盖整个 generator 消费期
            （取代旧的 resolve_tools monkey-patch）。

        session_id 非空且接了 SessionLog 时，本轮 durable 事件由本方法写入
        （turn/start、user/message、assistant/message、tool/call、tool/result、
        compact/applied、turn/end；见 _durable_* 系列）。
        """
        # 主思考开关：在首次 _get_llm_client() 前生效；开关翻转时客户端按需重建。
        self._thinking = bool(thinking)
        # 协作式取消探针（本轮有效；_cancelled() 容错读取）
        self._should_cancel_fn = should_cancel

        # 设置 contextvars（user_id / vfs / depth / 热切换模型覆盖 / 用量记账归属）
        from src.tools.remember import set_current_user_id
        from src.tools.virtual_fs import set_current_vfs
        from src.tools.sub_agent import _set_current_depth
        from src.llm.client import set_current_llm_overrides, set_current_usage_ctx

        set_current_user_id(user_id)
        if virtual_fs is not None:
            set_current_vfs(virtual_fs)
        _set_current_depth(0)
        # 子代理 LLM 与本轮主 agent 热切换对齐（copy_context 会传到 task 线程）
        set_current_llm_overrides(getattr(self, "_llm_overrides", None) or {})
        # 用量记账归属（随 copy_context 传入流式 worker 线程与子代理）：
        # client 层每次 LLM 调用记账时读取 session_id/scope/caller。
        set_current_usage_ctx(
            session_id=session_id or "",
            scope=sid_scope(session_id or ""),
            caller=caller_context,
        )

        effective_thread_id = thread_id or session_id or str(user_id)
        ctx = ToolContext(
            permission_mode=self._permission_mode,
            caller_context=caller_context,
            progress_cb=None,
            allowed_tools=allowed_tools,
            should_cancel=self._should_cancel_fn,
        )

        # 工具组装（每次调用都重新组装，支持 MCP 热刷新 + config_guard 动态过滤）
        tools_openai = self._resolve_and_bind_tools(ctx)

        # durable 会话 ID（空串 = 不写事件）
        self._durable_sid = session_id or ""

        # ── HITL resume 路径 ──
        if resume_payload is not None:
            decision, reason = self._resume_decision_reason(resume_payload)
            self._durable_turn_start_resume(decision)
            # 增量基线（F2）：resume 时调用方传入的 session_messages 就是
            # 消费方桶的当前内容；快照恢复出来的 turn_messages 增量必须
            # 从这个基线起算（桶在中断轮从未收到本轮消息）
            resume_baseline = len(list(session_messages or []))
            # P2-5：todos / compact_threshold_pct 透传进 resume 轮——此前
            # _build_state 硬编码 []，审批恢复轮系统提示丢待办、压缩偏好
            # 回落配置默认。实际传参方：worker 两个都传（prefs 取值）；
            # CLI chat() 只传 todos（无压缩阈值偏好入口，resume 轮该值
            # 为 None → 按配置默认，与 fresh 轮行为一致）
            yield from self._handle_resume(
                effective_thread_id, decision, reason,
                ctx, tools_openai, role, user_id, waker_persona,
                turn_baseline=resume_baseline,
                todos=todos,
                compact_threshold_pct=compact_threshold_pct,
            )
            return

        # ── 正常路径：初始化 state ──

        session_messages = list(session_messages or [])
        # P2（二轮审查）：Web 附图链路——images 非空时经 multimodal.
        # build_user_content 构造 OpenAI Vision 多模态 content（base64
        # data URI 由 worker 子进程解析上传文件得出；无图/全部解析失败时
        # 返回纯文本 str，行为与原来完全一致）。此前形参声明了 images 但
        # 函数体零引用，Web 附图被静默丢弃。
        if images:
            from src.agent.multimodal import build_user_content
            user_content = build_user_content(user_input, user_id, images)
        else:
            user_content = user_input
        # 冷槽水合会把 prelog 的本轮 user 灌进历史。content 全等才跳过
        # append（字符串对字符串，正是预写路径）；附图时 user_content 是
        # list，与预写的 str 不全等，仍 append——由 worker 先剥预写 str。
        last = session_messages[-1] if session_messages else None
        already_present = (
            isinstance(last, dict)
            and last.get("role") == "user"
            and last.get("content") == user_content
        )
        if not already_present:
            session_messages.append({"role": "user", "content": user_content})
        # 增量基线（F2）：turn_messages 只回报本轮新增。当前 user 已是
        # 末条（刚 append 或水合预写），基线指向它；消费方桶若仍含这条
        # 预写 user，须在调用前剥掉，否则 drain 会二次叠加。
        turn_baseline = max(0, len(session_messages) - 1)

        state = {
            "user_id": user_id,
            "current_input": user_input,
            "session_id": session_id,
            "messages": session_messages,
            "retrieved_memories": [],
            "todos": todos or [],
            "iteration_count": 0,
            "current_role": role,
            "waker_persona": waker_persona,
            "compact_pct_override": compact_threshold_pct,
            "turn_baseline": turn_baseline,
        }

        # durable：开轮（含本轮工作区模式）+ 新输入（resume 不写 user/message）。
        # prelogged_turn：主进程（Web POST handler）已在 spawn 前预写这两条
        # （spawn 窗口可见性），跳过防重复——仅 fresh 轮可达，resume 不预写。
        if not prelogged_turn:
            self._durable_turn_start(user_input)
            self._durable_append(USER_MSG, {"content": user_input})

        # 记忆检索 / precheck 压缩 / ReAct 循环 / 收尾 —— 全部在循环本体
        # （agent/pre-step 默认监听器 + _finish_turn）
        yield from self._finish_turn(
            state, ctx, tools_openai, effective_thread_id, memory_pending=True,
        )

    # ════════════════════════════════════════════════════════════════
    # 收尾公共路径（主路径 + HITL resume 两分支共用）
    # ════════════════════════════════════════════════════════════════

    def _cancelled(self) -> bool:
        """轮询协作式取消探针（should_cancel）。探针异常一律视为未取消
        （取消是尽力而为的优化，绝不能反过来打断正常对话）。"""
        fn = getattr(self, "_should_cancel_fn", None)
        if fn is None:
            return False
        try:
            return bool(fn())
        except Exception:
            return False

    def _finish_turn(
        self,
        state: dict,
        ctx: ToolContext,
        tools_openai: list[dict],
        thread_id: str,
        memory_pending: bool = False,
    ) -> Generator[dict, None, None]:
        """ReAct 循环 → turn-stopping 分发 → turn_messages + complete。

        原 stream_invoke 主路径与 _handle_resume approve/reject 两条收尾
        重复块的公共抽取（行为一致）。

        F2 语义：turn_messages 一律增量（state["messages"][turn_baseline:]，
        基线 = 本 stream 传入的历史长度；压缩后基线重置 0——快照通道承担
        全量职责）。messages_snapshot 一律全量。
        """
        try:
            yield from self._react_loop(state, ctx, tools_openai, memory_pending=memory_pending)
        except InterruptSignal as sig:
            # 工具需要审批 → 存快照 + yield 审批事件
            yield from self._handle_interrupt(sig, thread_id, state, ctx)
            return

        # 用户中途停止的轮：跳过 turn-stopping/记忆沉淀（省一次 LLM，也避免
        # 取消后 worker 锁被提取调用再占住——那会让用户的下一条消息撞上
        # "worker 忙"）；照常写 turn/end + 增量落桶，会话历史保持一致。
        if state.get("cancelled"):
            full_response = self._extract_final_response(state["messages"])
            final_reasoning = ""
            for _msg in reversed(state["messages"]):
                if isinstance(_msg, dict) and _msg.get("role") == "assistant":
                    final_reasoning = _msg.get("reasoning") or ""
                    break
            self._durable_close_turn(full_response, len(state["messages"]),
                                     final_reasoning, cancelled=True)
            yield {
                "type": "turn_messages",
                "messages": self._turn_increment(state),
                "partial": True,
            }
            yield {"type": "complete", "content": full_response, "cancelled": True}
            return

        # P2-9：LLM 调用失败轮——错误文本带 llm_error 标记进 messages
        # （随 turn_messages 落桶，UI 历史可回看；模型侧由 build_llm_messages
        # 过滤，不进 LLM 投影），durable 写 llm/error 事件（LLM 视图投影
        # 忽略、UI 视图投影还原）；complete 照常带错误文本给 UI，记忆沉淀跳过
        if state.get("llm_error"):
            err = state["llm_error"]
            self._durable_append(LLM_ERROR, {
                "content": err["content"],
                "error": err["error"],
            })
            self._durable_turn_end(len(state["messages"]))
            yield {
                "type": "turn_messages",
                "messages": self._turn_increment(state),
                "partial": True,
            }
            yield {"type": "complete", "content": err["content"]}
            return

        # agent/turn-stopping（serial，无默认监听器；观察者异常不影响收尾）
        try:
            self.scope.serial("agent/turn-stopping", state)
        except Exception:
            logger.warning("agent/turn-stopping 监听器异常（忽略）", exc_info=True)

        full_response = self._extract_final_response(state["messages"])
        # 最终答复的 reasoning（若本轮模型产出）随 durable 落盘
        final_reasoning = ""
        for _msg in reversed(state["messages"]):
            if isinstance(_msg, dict) and _msg.get("role") == "assistant":
                final_reasoning = _msg.get("reasoning") or ""
                break
        # durable：最终答复 + 收轮（先写后 yield，防消费方 break+close 丢事件）
        self._durable_close_turn(full_response, len(state["messages"]), final_reasoning)

        yield {
            "type": "turn_messages",
            "messages": self._turn_increment(state),
            "partial": False,
        }
        yield {
            "type": "complete",
            "content": full_response,
        }

    @staticmethod
    def _turn_increment(state: dict) -> list:
        """本轮增量消息（F2：turn_messages 的唯一切片来源）。

        turn_baseline 缺失按 0（全量，防御旧调用形状）；压缩把 messages
        整体替换时同样重置基线 0（messages_snapshot 通道已承担全量）。
        """
        return list(state["messages"][state.get("turn_baseline", 0):])

    # ════════════════════════════════════════════════════════════════
    # 压缩执行（唯一执行点；阈值判断见 _prestep_compact_listener）
    # ════════════════════════════════════════════════════════════════

    def _compact_messages(self, state: dict, auto: bool = False) -> Generator[dict, None, None]:
        """执行上下文压缩。"""
        messages = state.get("messages", [])
        if not messages:
            return

        try:
            compact_result = self.context_manager.compact_messages(list(messages))
            if not compact_result:
                return
        except Exception as e:
            logger.warning(f"压缩失败（忽略）: {e}")
            return

        new_messages = compact_result.compressed_messages

        # 可见性（2026-09-06）：无论阈值自动触发还是 compact_conversation
        # 工具触发，压缩落定都必须让用户知情——此前 auto=False 路径不发
        # 事件，用户只看到工具面板"已触发"，感知不到上下文已被替换。
        # auto=False 补 trigger:"tool" 标注来源；CLI 渲染只读
        # compacted_count/original_count（cli.py auto_compact 分支），
        # 多余字段无副作用；worker 对未知事件按原样转发，同样不受影响。
        compact_event = {
            "type": "auto_compact",
            "compacted_count": compact_result.compacted_count,
            "original_count": compact_result.original_count,
        }
        if not auto:
            compact_event["trigger"] = "tool"
        yield compact_event
        yield {"type": "messages_snapshot", "messages": new_messages}

        # durable：compact/applied（summary + 精确计数 + 保留区；derive_messages
        # 据此丢弃此前投影、以 [system:summary] + kept_messages 重启——真实压缩
        # 保留最后 N 条，事件流必须记录保留区，否则重载后丢失（R2-9））
        # payload 由 build_compact_applied_payload 构造：保留区 = 压缩后消息
        # 去掉头部的本轮摘要 system 消息（lc_to_dict 规范化，剔除 compact_id
        # 等附带键）
        self._durable_append(COMPACT_APPLIED,
                             build_compact_applied_payload(compact_result, new_messages))

        # 应用压缩到 state（dict 列表整体替换：摘要 + 近期消息）
        state["messages"] = list(new_messages)
        # F2：快照通道承担全量职责，增量基线重置 0——此后的 turn_messages
        # 从压缩后状态起算（消费方以 snapshot 整体替换桶后按此重放）
        state["turn_baseline"] = 0

    # ════════════════════════════════════════════════════════════════
    # ReAct 循环
    # ════════════════════════════════════════════════════════════════

    def _react_loop(
        self,
        state: dict,
        ctx: ToolContext,
        tools_openai: list[dict],
        memory_pending: bool = False,
    ) -> Generator[dict, None, None]:
        """ReAct 循环：LLM 流式 → 有 tool_calls → 执行 → 继续循环。

        每次迭代：agent/pre-step 分发（记忆/压缩/模式默认监听器 + 外部
        监听器可改 messages/reject）→ 构建消息 → agent/request 分发 →
        llm/stream 分发取 chunk 生成器 → 流式消费。
        """
        # 预热客户端（保持旧时序：构造失败在循环体 try 之外抛出、直传调用方）
        client = self._get_llm_client()  # noqa: F841 （实际请求经 llm/stream 默认监听器取同一实例）

        max_iter = int(getattr(self.settings, "max_agent_iterations", 15) or 15)
        timeout_s = int(getattr(self.settings, "llm_timeout", 120) or 120)

        first_step = True

        while state["iteration_count"] < max_iter:
            # 协作式取消（用户点停止）：worker 侧 drain 循环只在事件间隙
            # 轮询，工具执行期/首 token 前 agent 零事件——这里主动检查，
            # 干净返回走 _finish_turn 的 cancelled 收尾。无部分内容时也
            # 落一条标记消息，保证会话历史/回放有可见的停止痕迹。
            if self._cancelled():
                state["cancelled"] = True
                state["messages"].append({"role": "assistant", "content": "⏹️（已停止）"})
                return

            # 权限模式轮间热刷新：chat 进行中内联切换（worker 改
            # _permission_mode）从下一个 LLM 轮起生效，不必等下一条消息
            ctx.permission_mode = self._permission_mode

            # 循环检测（stateful：签名历史存 state，跨压缩存活——修复前
            # 从 messages 反向收集，压缩截窗后交替型规则结构性不可达）
            if self._detect_tool_loop(state["messages"], state):
                yield {"type": "token", "content": "⚠️ 检测到重复的工具调用，已终止循环。"}
                state["messages"].append({
                    "role": "assistant",
                    "content": "⚠️ 检测到重复的工具调用，已终止循环。",
                })
                return

            # ── agent/pre-step：构建 LLM 消息前分发 ──
            notes: dict = {
                "user_id": state.get("user_id", ""),
                "session_id": state.get("session_id", ""),
                "permission_mode": ctx.permission_mode,
                "first_step": first_step,
            }
            if first_step:
                # 记忆检索仅 fresh 轮首步（resume 不检索，与旧实现一致）；
                # 压缩阈值首步检查（fresh 轮用入参 pct，resume 轮用配置默认）
                if memory_pending:
                    notes["memory_pending"] = True
                notes["precheck_pct"] = state.get("compact_pct_override")

            step_req = StepRequest(
                messages=state["messages"],
                user_input=state.get("current_input", ""),
                notes=notes,
            )
            self.scope.waterfall("agent/pre-step", step_req)

            # a) 记忆：yield memory_search 事件 + 存入 state（后续迭代复用）
            memory_event = step_req.notes.get("memory_event")
            if memory_event is not None:
                yield memory_event
                state["retrieved_memories"] = step_req.notes.get("memories", [])
            # b) 压缩：阈值超限 → 与旧 _precheck 相同的 auto 压缩
            if step_req.notes.get("compact_needed"):
                yield from self._compact_messages(state, auto=True)
            # reject：直接收 turn（不发 LLM 请求；turn/end 由 _finish_turn 写）
            if step_req.reject:
                return
            first_step = False

            # 构建 LLM 消息（监听器可改写 messages——只影响本步请求）
            llm_messages = self.context_manager.build_llm_messages(
                user_id=state["user_id"],
                current_input=state["current_input"],
                messages=step_req.messages,
                memories=state.get("retrieved_memories", []),
                todos=state.get("todos", []),
                role=state.get("current_role"),
                waker_persona=state.get("waker_persona"),
            )

            # 注入 mode guidance 到 system prompt（默认监听器产出，外部可改写）
            mode_section = step_req.notes.get("mode_guidance") or ""
            if mode_section and llm_messages and llm_messages[0].get("role") == "system":
                llm_messages[0] = {
                    "role": "system",
                    "content": llm_messages[0].get("content", "") + "\n\n" + mode_section,
                }

            # 运行时工具面权威清单：系统提示的静态工具段按"完整模式"撰写，
            # 当工具面被作用域收窄（收件箱 / waker 白名单）时模型并不知情，
            # 会反复尝试不存在的工具（实测退化成交替刷工具 14 轮才被循环
            # 检测终止）。把本请求真实工具名追加进 system prompt 收口。
            if llm_messages and llm_messages[0].get("role") == "system" and tools_openai:
                tool_names = [
                    (t.get("function") or {}).get("name", "") for t in tools_openai
                ]
                tool_names = [n for n in tool_names if n]
                if tool_names:
                    llm_messages[0]["content"] += (
                        "\n\n## 本轮实际可用的工具（权威清单）\n"
                        + "、".join(tool_names)
                        + "\n不在此清单中的工具对你不存在：不要尝试调用、不要提及其输出；"
                        "若用户请求超出上述能力面，如实说明限制并给出你能做到的替代方案。"
                    )

            # ContextManager 已返回 OpenAI dict（clean 投影，无 reasoning 等附带键）
            llm_msgs_dict = llm_messages

            # ── agent/request：发 LLM 前分发（返回值用于实际请求）──
            request_payload = {"messages": llm_msgs_dict, "tools": tools_openai}
            out = self.scope.waterfall("agent/request", request_payload)
            if isinstance(out, dict):
                request_messages = out.get("messages", llm_msgs_dict)
                request_tools = out.get("tools", tools_openai)
            else:
                request_messages, request_tools = llm_msgs_dict, tools_openai

            # LLM 流式调用（llm/stream waterfall 取 chunk 生成器，含双 deadline 超时）
            chunks = []
            full_content = ""
            full_reasoning = ""

            try:
                # P2-8 防线 C：本轮流式的取消事件。消费方（下方 chunk 循环）
                # 超时/中途停止时置位，llm_stream 的 worker 线程在 chunk
                # 边界 close() 底层 openai 流并退出——不再把流读到生成完
                stream_cancel = threading.Event()
                self._stream_cancel_event = stream_cancel
                stream_req = StreamRequest(
                    messages=request_messages,
                    tools=request_tools,
                    timeout_s=timeout_s,
                    # R2-13：参数与客户端随请求透传——booted kernel 路径由 llm
                    # 插件的默认监听器执行流式，set_llm_params 的覆盖值与测试
                    # 注入的 mock 客户端经此生效（客户端优先用 req.client）
                    temperature=self._llm_temperature,
                    max_tokens=self._llm_max_tokens,
                    client=self._get_llm_client(),
                    # P2-8 防线 C + 二轮审查第 12 项：本轮流式取消事件随请求
                    # 透传——kernel 路径（llm_plugin 默认监听器）经
                    # StreamRequest.cancel_event 接进 stream_with_hard_timeout，
                    # 消费方超时/中途停止置位后 worker 线程同样断流退出
                    cancel_event=stream_cancel,
                )
                self.scope.waterfall("llm/stream", stream_req)
                if stream_req.chunks is None:
                    if stream_req.handled:
                        # R2-13：监听器短路接管但不给 chunks → 空流（尊重
                        # 短路，不 fallback 直连真 LLM）
                        stream_req.chunks = iter(())
                    else:
                        # 链上无人处理（无默认监听器）→ 直连兜底
                        stream_req.chunks = stream_with_hard_timeout(
                            self._get_llm_client(), request_messages,
                            tools=request_tools, timeout_s=timeout_s,
                            cancel_event=stream_cancel,
                        )
                cancelled = False
                # 流式消费（P3 轻拆：chunk 循环体外提 src/agent/stream_consumer，
                # 事件即时透传 + chunk 粒度取消/防线 C set() 语义逐字等价，
                # 迭代异常沿委派链冒泡、仍由本函数 except 收口）。token /
                # reasoning_token 事件镜像回局部累积量——迭代中途抛
                # TimeoutError 时局部量仍持有已收部分（原循环局部变量语义，
                # 下方 except 分支零改动的前提）；chunks/cancelled 仅在正常
                # 完成路径读取，从聚合结果取回
                consumption = StreamConsumption()
                for _ev in consume_llm_stream(
                    stream_req.chunks,
                    cancel_probe=self._cancelled,
                    cancel_event=stream_cancel,
                    out=consumption,
                ):
                    if _ev["type"] == "token":
                        full_content += _ev["content"]
                    elif _ev["type"] == "reasoning_token":
                        full_reasoning += _ev["content"]
                    yield _ev
                chunks = consumption.chunks
                cancelled = consumption.cancelled
            except TimeoutError as e:
                logger.warning(f"LLM 流式超时: {e}")
                if full_content:
                    state["messages"].append({"role": "assistant", "content": full_content})
                state["messages"].append({
                    "role": "assistant",
                    "content": f"\n\n[LLM 响应超时，已返回已有内容]",
                })
                state["iteration_count"] += 1
                return
            except Exception as e:
                logger.error(f"LLM 调用异常: {e}", exc_info=True)
                # P2-9：错误文本存为带标记的旁路记录（state.llm_error）；
                # durable 侧由 _finish_turn 写 llm/error 事件。
                # 2026-09-19：错误同时以 llm_error 标记消息进 messages——随
                # turn_messages 落桶/落盘，切页或重启后 UI 历史仍能看到失败
                # 信息（此前只随 complete 事件带给 UI 一次，切页即丢，只剩
                # 用户发出的请求）。模型不可见性不变：build_llm_messages
                # 投影过滤 llm_error 标记，derive_messages 的 LLM 视图同样
                # 忽略 llm/error 事件。complete 照常带给 UI，当场可见。
                err_text = f"⚠️ LLM 调用失败: {e}"
                state["llm_error"] = {"content": err_text, "error": str(e)}
                state["messages"].append(
                    {"role": "assistant", "content": err_text, "llm_error": True})
                state["iteration_count"] += 1
                return

            # 累积成 AIMsg
            ai_msg = LLMClient.accumulate(chunks)
            state["iteration_count"] += 1

            # 用户中途停止：已有内容作为部分答复收尾（带 ⏹️ 标记），不再
            # 执行工具调用。cancelled 轮走 _finish_turn 的专用收尾（跳过
            # 记忆沉淀，正常写 turn/end 落桶）。
            if cancelled or self._cancelled():
                from src.think import strip_think_tags
                partial = strip_think_tags(ai_msg.content)
                partial = (partial + " ⏹️（已停止）") if partial else "⏹️（已停止）"
                assistant_dict: dict = {"role": "assistant", "content": partial}
                if ai_msg.reasoning:
                    assistant_dict["reasoning"] = ai_msg.reasoning
                state["messages"].append(assistant_dict)
                state["cancelled"] = True
                yield {"type": "token", "content": " ⏹️（已停止）"}
                return

            # 无 tool_calls → 最终答复，结束循环
            if not ai_msg.has_tool_calls:
                # 剥离 <think> 标签（防止下轮 LLM 看到残留标记），
                # reasoning 存附带键供前端页面刷新时恢复推理面板（不进 LLM payload）
                from src.think import strip_think_tags
                clean_content = strip_think_tags(ai_msg.content)
                assistant_dict: dict = {"role": "assistant", "content": clean_content}
                if ai_msg.reasoning:
                    assistant_dict["reasoning"] = ai_msg.reasoning
                state["messages"].append(assistant_dict)
                return

            # 有 tool_calls → 转 assistant dict（OpenAI tool_calls 格式）+ 执行
            # 用 safe_parse_tool_args 容错解析 arguments
            # 剥离 <think> 标签 + reasoning 存附带键（同上）
            from src.llm.json_fix import safe_parse_tool_args
            from src.think import strip_think_tags
            clean_content = strip_think_tags(ai_msg.content)
            tool_calls_openai = [
                {
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"].get("arguments", ""),
                    },
                }
                for tc in ai_msg.tool_calls
            ]
            assistant_dict = {"role": "assistant", "content": clean_content,
                              "tool_calls": tool_calls_openai}
            if ai_msg.reasoning:
                assistant_dict["reasoning"] = ai_msg.reasoning
            state["messages"].append(assistant_dict)

            # durable：本轮带 tool_calls 的 assistant 消息（OpenAI 格式透传，
            # derive_messages 据此重建 "assistant(tool_calls) → tool" 配对）
            assistant_payload: dict = {"content": assistant_dict.get("content", "")}
            if assistant_dict.get("tool_calls"):
                assistant_payload["tool_calls"] = assistant_dict["tool_calls"]
            if ai_msg.reasoning:
                assistant_payload["reasoning"] = ai_msg.reasoning
            self._durable_append(ASSISTANT_MSG, assistant_payload)

            # 执行工具（args 容错解析为 dict）
            tool_calls_for_registry = [
                {
                    "id": tc.get("id", ""),
                    "name": tc["function"]["name"],
                    "args": safe_parse_tool_args(tc["function"].get("arguments", "")),
                }
                for tc in ai_msg.tool_calls
            ]

            # 执行前再刷一次：本轮 LLM 流式期间的内联切换也吃到
            ctx.permission_mode = self._permission_mode

            # event_sink：把 Registry 的事件 yield 出去
            def event_sink(event: dict):
                # generator 不能嵌套 yield，用 hack：存到列表，循环后 yield
                # 但更简洁的方式是 Registry 同步调用 sink，sink 内部不能 yield。
                # 这里改用「收集 + 循环后 yield」。
                _pending_events.append(event)
                # durable：tool_start → tool/call；tool_end → tool/result
                self._durable_ui_event(event)

            _pending_events: list[dict] = []
            tool_msgs, state_updates = self.registry.process_tool_calls(
                tool_calls_for_registry, ctx, event_sink=event_sink,
            )
            # yield 收集的事件
            for ev in _pending_events:
                yield ev

            # compact_requested 每轮一次闸门（2026-09-06 死循环修复）：
            # 本轮已执行过压缩（compacted_this_turn；state 每轮 stream_invoke
            # 重建 → 天然按用户轮重置）则不再压缩，并在 append 前把
            # compact_conversation 的工具结果改写为"已压缩"提示——模型读到
            # 完成时态 + 明示勿重调，二次触发被就地拆解，不再喂一句
            # "已触发"让它下一轮重新发起。
            if state_updates.get("compact_requested") and state.get("compacted_this_turn"):
                compact_ids = {
                    tc["id"] for tc in tool_calls_for_registry
                    if tc["name"] == "compact_conversation"
                }
                for tm in tool_msgs:
                    if tm.tool_call_id in compact_ids:
                        tm.content = (
                            "上下文本轮已压缩过（早期对话已替换为摘要）。"
                            "请直接继续当前任务，不要再次调用本工具。"
                        )

            # 工具结果以 tool 消息 append 进 state
            for tm in tool_msgs:
                state["messages"].append({
                    "role": "tool",
                    "content": tm.content,
                    "tool_call_id": tm.tool_call_id,
                })

            # 合并 state_updates（todos 等）
            if "todos" in state_updates:
                state["todos"] = state_updates["todos"]

            # compact_requested 信号（compact_conversation 工具返回）：
            # 闸门内同步执行压缩（本轮未压缩过才执行）；无论成败都标记
            # 本轮已尝试（防失败重试也成环，下一用户轮 state 重建自动
            # 恢复可压缩）
            if state_updates.get("compact_requested") and not state.get("compacted_this_turn"):
                yield from self._compact_messages(state, auto=False)
                state["compacted_this_turn"] = True

            # 继续 ReAct 循环

        # 达到最大迭代
        yield {"type": "token", "content": f"\n⚠️ 已达最大迭代次数 {max_iter}"}
        state["messages"].append({
            "role": "assistant",
            "content": f"⚠️ 已达最大迭代次数 {max_iter}",
        })

    # ════════════════════════════════════════════════════════════════
    # HITL 处理
    # ════════════════════════════════════════════════════════════════

    @staticmethod
    def _resume_decision_reason(resume_payload: Any) -> tuple[str, str]:
        """resume_payload → (decision, reason)。

        'approve' → ('approve', '')；'reject:原因' → ('reject', '原因')；
        其他任意非 approve 文本 → ('reject', 全文)——这正是 CLI 审批提示
        的契约（"其他任何输入 = 拒绝（内容作为原因）"）。decision 恒为
        approve/reject 二值：durable turn/start{resume} 与 interrupt/resolved
        都是审计事件，自由文本混进 decision 字段会污染轨迹语义。
        """
        if isinstance(resume_payload, str) and resume_payload.strip().lower() == "approve":
            return "approve", ""
        text = resume_payload if isinstance(resume_payload, str) else ""
        decision, _, reason = text.partition(":")
        if decision.strip().lower() == "reject":
            return "reject", reason
        return "reject", text

    # 批量中断占位文本：审批暂停时未执行到的调用合成此占位，保持
    # assistant tool_calls 与 tool 消息一一配对（与串行取消路径的
    # "（用户已停止，本工具未执行）"同风格）
    INTERRUPT_SKIPPED_MARK = "（等待人工审批被中断，本工具未执行）"

    def _repair_interrupt_pairing(self, state: dict, payload: dict, pending_call_id: str) -> None:
        """P1-3：中断前补齐批量工具调用的消息配对。

        串行/并发批量在 InterruptSignal 冒泡时把已完成的 tool 结果挂在
        sig.payload[COMPLETED_TOOL_MESSAGES_KEY] 带出；最后一条带 tool_calls
        的 assistant 消息里，除 pending 之外的每个 call_id 都必须有配对
        tool 消息：已完成用真实结果，从未执行的合成占位。否则 resume 后
        消息序列非法（悬空 tool_calls 被严格 OpenAI 兼容端点 400），且
        随 turn_messages 落桶持久化后该会话每轮请求都失败。
        """
        last_tc_idx = None
        for i in range(len(state["messages"]) - 1, -1, -1):
            msg = state["messages"][i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                last_tc_idx = i
                break
        if last_tc_idx is None:
            return
        last_tc = state["messages"][last_tc_idx]
        # 已有配对的（幂等防御）+ signal 带出的已完成结果
        paired = {
            m.get("tool_call_id")
            for m in state["messages"][last_tc_idx + 1:]
            if m.get("role") == "tool"
        }
        completed = {
            m.get("tool_call_id", ""): m.get("content", "")
            for m in (payload.get(COMPLETED_TOOL_MESSAGES_KEY) or [])
        }
        for tc in last_tc.get("tool_calls") or []:
            cid = tc.get("id", "") if isinstance(tc, dict) else ""
            if not cid or cid == pending_call_id or cid in paired:
                continue
            if cid in completed:
                content = completed[cid]
            else:
                # 从未执行到的调用：合成占位；durable 侧同步补 tool/result
                # 保持 in-memory state 与事件投影一致（已完成的在中断轮
                # event_sink 已写过 tool/result，不重复写）
                content = self.INTERRUPT_SKIPPED_MARK
                self._durable_tool_result(
                    cid, (tc.get("function") or {}).get("name", ""), content,
                )
            state["messages"].append({
                "role": "tool",
                "content": content,
                "tool_call_id": cid,
            })

    def _handle_interrupt(
        self,
        sig: InterruptSignal,
        thread_id: str,
        state: dict,
        ctx: ToolContext,
    ) -> Generator[dict, None, None]:
        """工具需审批 → 存快照 + yield human_approval_request。"""
        payload = sig.payload
        tool_call_id = payload.get("tool_call_id", "")
        tool_name = payload.get("tool_name", "")

        # P3（二轮审查）：并发批量中断时，已完成兄弟工具（如 write_todos）
        # 的 state_updates 经 payload 带出——合并进本轮 state 并补发
        # todos_update 事件（对齐正常路径语义，待办不随中断丢失）
        sibling_updates = payload.get("state_updates") or {}
        if sibling_updates.get("todos"):
            state["todos"] = sibling_updates["todos"]
            yield {"type": "todos_update", "todos": sibling_updates["todos"]}

        # P1-3：先补齐配对再存快照（快照深拷贝 state["messages"]，修复后
        # 的合法序列随快照进 resume 路径）
        self._repair_interrupt_pairing(state, payload, tool_call_id)

        # 深拷贝 messages 存快照
        snapshot = InterruptSnapshot.create(
            thread_id=thread_id,
            messages=state["messages"],
            pending_args=payload.get("tool_args", {}),
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            payload={
                "action": payload.get("action", ""),
                "details": payload.get("details", ""),
            },
            permission_mode=ctx.permission_mode,
        )
        self.interrupt_store.save(snapshot)

        # durable：收轮（turn 暂停在审批上，等 resume 续；先写后 yield，
        # 防消费方 break + close 后丢事件）
        self._durable_turn_end(len(state["messages"]))

        yield {
            "type": "human_approval_request",
            "action": payload.get("action", ""),
            "details": payload.get("details", ""),
            "thread_id": thread_id,
        }
        # generator 结束，控制权回调用方

    def _handle_resume(
        self,
        thread_id: str,
        decision: str,
        reason: str,
        ctx: ToolContext,
        tools_openai: list[dict],
        role: str | None,
        user_id: str,
        waker_persona: str | None = None,
        turn_baseline: int = 0,
        todos: list | None = None,
        compact_threshold_pct: int | None = None,
    ) -> Generator[dict, None, None]:
        """resume 路径：从快照恢复 + 重新求值权限 + 继续 ReAct。

        turn_baseline（F2）：调用方桶的当前长度——快照恢复的增量从这里
        起算（中断轮的 user/assistant(tool_calls) 消息桶里还没有）。

        todos / compact_threshold_pct（P2-5）：调用方（worker / CLI）在
        stream_invoke 传入的会话上下文，透传进 _build_state——此前 resume
        轮硬编码丢弃，系统提示丢待办、压缩偏好回落配置默认。
        """
        # pop 时把 decision/reason 传全 → interrupt/resolved 事件完整记录审批结果
        snapshot = self.interrupt_store.pop(thread_id, decision, reason)
        if snapshot is None:
            # 无快照（可能 race）→ 当作新对话
            yield {"type": "token", "content": "⚠️ 无待处理的审批（可能已过期）"}
            self._durable_close_turn("无待处理的审批", 0)
            yield {"type": "complete", "content": "无待处理的审批"}
            return

        # 检测 mode 是否切换（用户切到 full_access 后自动放行）
        mode_changed = snapshot.permission_mode_at_interrupt != ctx.permission_mode

        # 重新求值权限（mode 可能已切换）
        spec = self.registry.get_spec(snapshot.pending_tool_name)
        if spec is None:
            # 工具已不存在（MCP 断开等）→ 补 tool 消息拒绝
            state = {"messages": list(snapshot.messages), "turn_baseline": turn_baseline}
            state["messages"].append({
                "role": "tool",
                "content": f"工具 {snapshot.pending_tool_name} 已不可用",
                "tool_call_id": snapshot.pending_tool_call_id,
            })
            # durable：补 tool/result 闭环中断轮的悬空 tool/call（R2-8）
            self._durable_tool_result(
                snapshot.pending_tool_call_id, snapshot.pending_tool_name,
                f"工具 {snapshot.pending_tool_name} 已不可用",
            )
            yield {"type": "turn_messages", "messages": self._turn_increment(state), "partial": True}
            self._durable_close_turn("", len(state["messages"]))
            yield {"type": "complete", "content": ""}
            return

        from src.tools.permissions import decide
        permission_decision = decide(spec, snapshot.pending_args, ctx)
        # 用户显式 reject 优先于权限放行——非 destructive 工具（如
        # request_human_approval）decide 恒 allow，此前 is_allow 直接翻成
        # is_approved，会把用户的拒绝静默改成"批准并执行"。权限放行自动
        # 通过（mode 切到 full_access）只在用户未显式拒绝时生效。
        is_approved = (
            decision != "reject"
            and (permission_decision.is_allow or decision == "approve")
        )

        # P1（二轮审查）：request_human_approval 是"暂停等审批"的信号工具
        # （destructive=false → decide 恒 allow，resume 的 approve 必然重入
        # executor，而 executor 拿到控制权只会再抛 InterruptSignal）。重入
        # 的 InterruptSignal 被下方 except 误报成"force_deny 硬拦截"，且
        # yield 的 turn_messages 快照里 pending 调用没有 tool 结果（悬空进
        # 活桶）。resume 时识别该工具名，不再重入 executor，直接以用户决策
        # 文本闭合 pending tool 消息。
        is_approval_signal_tool = snapshot.pending_tool_name == "request_human_approval"

        # ─── 公共状态组装 ───
        def _build_state(content: str) -> dict:
            """把工具结果作为 tool 消息追加到快照消息列表，返回可继续 ReAct 的 state。"""
            msgs = list(snapshot.messages)
            msgs.append({
                "role": "tool",
                "content": content,
                "tool_call_id": snapshot.pending_tool_call_id,
            })
            return {
                "messages": msgs,
                "user_id": user_id,
                "current_input": "",
                "session_id": self._durable_sid,
                "retrieved_memories": [],
                # P2-5：透传调用方 todos（此前硬编码 []，恢复轮丢待办）
                "todos": todos or [],
                "iteration_count": 0,
                "current_role": role,
                "waker_persona": waker_persona,
                # P2-5：透传压缩阈值偏好（此前缺失，恢复轮回落配置默认）
                "compact_pct_override": compact_threshold_pct,
                "turn_baseline": turn_baseline,
            }

        if is_approved:
            # ─── 审批通过：通知前端 + 执行工具 + 发事件 + 继续 ReAct ───
            yield {
                "type": "approval_result",
                "decision": "approve",
                "tool_name": spec.name,
            }
            tool_start_event = {
                "type": "tool_start",
                "tool_name": spec.name,
                "tool_args": snapshot.pending_args,
                "tool_id": snapshot.pending_tool_call_id,
            }
            # R3-18：UI 的 tool_start 照常 yield，但不再写 durable tool/call——
            # 中断轮 event_sink 在 InterruptSignal 抛出之前已写过同
            # tool_call_id 的 tool/call（registry._execute_serial 先 sink
            # 后 execute），重复写会污染事件流；resume 轮只补 tool/result
            # （下方 tool_end 的映射，与 _durable_tool_result 同型）。
            yield tool_start_event

            if is_approval_signal_tool:
                # P1（二轮审查）：审批信号工具不重入 executor（重入必然再抛
                # InterruptSignal：被误报 force_deny + pending 消息悬空），
                # 直接以"用户已批准：<action>"闭合。action 优先取工具调用
                # 参数（权限层抛出时带 tool_args），executor 直抛时回落到
                # 信号 payload 里的 action。
                action = (
                    (snapshot.pending_args or {}).get("action")
                    or snapshot.pending_payload.get("action", "")
                )
                content = f"用户已批准：{action}"
            elif permission_decision.is_allow:
                # mode 切到 full_access → 走 registry.execute（统一路径）
                try:
                    result = self.registry.execute(
                        spec, snapshot.pending_args, ctx, snapshot.pending_tool_call_id,
                    )
                    content = result.content
                except InterruptSignal:
                    # durable：force_deny 硬拦截也补 tool/result，闭环悬空 tool/call（R2-8）
                    self._durable_tool_result(
                        snapshot.pending_tool_call_id, spec.name,
                        "已拦截：force_deny 硬底线（审批通过仍被安全规则拒绝）",
                    )
                    yield {"type": "token", "content": "⚠️ 该操作被安全规则硬拦截（force_deny）"}
                    yield {"type": "turn_messages", "messages": list(snapshot.messages)[turn_baseline:], "partial": True}
                    self._durable_close_turn("", len(snapshot.messages))
                    yield {"type": "complete", "content": ""}
                    return
                except Exception as e:
                    content = f"工具执行失败: {e}"
            else:
                # 用户显式 approve，mode 未变 → 绕过权限直接执行（用户已拍板）
                from src.agent.registry_v3 import coerce_args
                try:
                    coerced = coerce_args(snapshot.pending_args, spec.parameters)
                    result = spec.executor.execute(coerced, ctx)
                    content = result.content
                except InterruptSignal:
                    # durable：force_deny 硬拦截也补 tool/result，闭环悬空 tool/call（R2-8）
                    self._durable_tool_result(
                        snapshot.pending_tool_call_id, spec.name,
                        "已拦截：force_deny 硬底线（审批通过仍被安全规则拒绝）",
                    )
                    yield {"type": "token", "content": "⚠️ 该操作被安全规则硬拦截（force_deny）"}
                    yield {"type": "turn_messages", "messages": list(snapshot.messages)[turn_baseline:], "partial": True}
                    self._durable_close_turn("", len(snapshot.messages))
                    yield {"type": "complete", "content": ""}
                    return
                except Exception as e:
                    content = f"工具执行失败: {e}"

            tool_end_event = {
                "type": "tool_end",
                "tool_name": spec.name,
                "tool_id": snapshot.pending_tool_call_id,
                "result": content,
            }
            self._durable_ui_event(tool_end_event)
            yield tool_end_event

            state = _build_state(content)
            yield from self._finish_turn(state, ctx, tools_openai, thread_id)
            return

        # ─── 用户拒绝 ───
        # reason 来自 _resume_decision_reason（剥 'reject:' 前缀或自由文本
        # 全文），直接用——此前拿原始 resume_payload，指令前缀会泄漏进
        # UI 回显、interrupt/resolved 事件与会话历史三处。
        reject_msg = reason
        yield {
            "type": "approval_result",
            "decision": "reject",
            "tool_name": spec.name,
            "reason": reject_msg,
        }
        # durable：拒绝路径补 tool/result（content 与 state 的 tool 消息一致），
        # 闭环中断轮的悬空 tool/call（R2-8）。审批信号工具以"用户已拒绝"闭合
        # （P1 二轮审查：其语义就是审批反馈，不走"命令未获批准"措辞），
        # 拒绝原因拼进闭合文本——此前裸"用户已拒绝"把 reason 丢弃，模型
        # 只知道被拒、不知道为什么，下一轮只能盲猜重试；其余工具裸 reject
        # （CLI 默认值）无原因，不留尾冒号
        if is_approval_signal_tool:
            denial = f"用户已拒绝：{reject_msg}" if reject_msg else "用户已拒绝"
        else:
            denial = f"命令未获批准：{reject_msg}" if reject_msg else "命令未获批准"
        self._durable_tool_result(snapshot.pending_tool_call_id, spec.name, denial)
        state = _build_state(denial)

        yield from self._finish_turn(state, ctx, tools_openai, thread_id)

    # ════════════════════════════════════════════════════════════════
    # 循环检测
    # ════════════════════════════════════════════════════════════════

    def _detect_tool_loop(self, messages: list, state: dict | None = None) -> bool:
        """检测工具调用死循环：连发同调用 + 交替型循环，双规则。

        规则 1（原有）：最近 threshold 条签名完全相同。
        规则 2（新增，交替型）：最近 threshold*3 条签名唯一数 ≤ 2——
        A/B/A/B… 交替永远凑不齐"连续全同"，实测收件箱 compact/write_todos
        交替烧掉 14 轮 LLM 调用才被旧规则逮住。args 完全相同的两类调用
        反复交替基本必然是循环（正常任务的 args 会随进度变化）。

        签名窗口来源（2026-09-06 死循环修复）：
        - 传入 state（ReAct 主循环路径）：签名滚动历史存
          state["_tool_sig_hist"]，每次检测追加"最近一个带 tool_calls 的
          assistant 消息"的签名；最近 assistant 为纯文本 → 链条断裂，
          清空历史（对齐 messages 扫描的窗口重置语义）。历史只裁剪尾窗
          （threshold*3 条），**跨压缩存活**——compact 只整体替换
          messages、不动 state 其他键，旧的 messages 反向收集在压缩后
          窗口骤缩（keep 条数 < wide），交替型规则结构性不可达。
        - 未传 state（兼容旧调用方 / 测试桩）：退回 messages 反向收集，
          行为与修复前一致。

        消息容器为 OpenAI dict（tool_calls 为 OpenAI 格式，arguments 是
        JSON 字符串，解析失败时退回原文比较）。
        """
        # 阈值可配置：tool_loop_threshold（config.example.yaml 出厂值 5）。
        # 0 = 关闭检测。getattr 兜底默认值有意保留更严格的 3：仅在旧
        # config.yaml 缺该键时触发的最后防线（出厂模板 / 本注释 / 测试桩
        # 三方一致由 tests/test_tool_loop_threshold.py 锁定）。
        threshold = int(getattr(self.settings, "tool_loop_threshold", 3))
        if threshold <= 0:
            return False

        from src.llm.json_fix import safe_parse_tool_args

        def _sig_of(tool_calls: list) -> tuple:
            parts = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                raw_args = fn.get("arguments", "")
                try:
                    args_key = json.dumps(
                        safe_parse_tool_args(raw_args), sort_keys=True, ensure_ascii=False,
                    )
                except Exception:
                    args_key = raw_args
                parts.append((fn.get("name", ""), args_key))
            return tuple(sorted(parts))

        wide = threshold * 3

        if state is not None:
            # 有状态模式：每步 ReAct 迭代恰好经过本检测一次（循环顶部），
            # 两次检测之间恰有一个 LLM 步——要么产出 tool_calls（工具轮），
            # 要么纯文本收尾（轮结束）。因此"最近一个 assistant 消息"的
            # 签名就是尚未入史的最新一步，逐次追加不重不漏；压缩替换
            # messages 后，尾部保留区必然含最新工具步，取到的仍是同一签名。
            hist = state.setdefault("_tool_sig_hist", [])
            for msg in reversed(messages):
                if msg.get("role", "") != "assistant":
                    continue
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    hist.append(_sig_of(tool_calls))
                else:
                    # 纯文本 assistant = 模型跳出工具循环表达结论 → 链断裂
                    hist.clear()
                break
            # 滚动窗口裁剪：规则至多看最近 wide 条，防长轮无界增长
            recent = hist[-wide:]
            state["_tool_sig_hist"] = recent
        else:
            # 无 state 回退：从 messages 反向收集签名（修复前行为）。
            # 注意压缩会把窗口截到 keep 条数——wide 不可达时规则 2 失效，
            # 这正是主循环改走 state 历史的原因。
            recent = []
            for msg in reversed(messages):
                role = msg.get("role", "")
                tool_calls = msg.get("tool_calls") if role == "assistant" else None
                if tool_calls:
                    recent.append(_sig_of(tool_calls))
                    if len(recent) >= wide:
                        break
                elif role == "assistant":
                    break

        if len(recent) >= threshold and len(set(recent)) == 1:
            logger.warning(f"⚠️ 检测到工具调用死循环：{recent[0]}")
            return True
        if len(recent) >= wide and len(set(recent)) <= 2:
            logger.warning(
                f"⚠️ 检测到交替型工具循环：{len(recent)} 轮仅 {len(set(recent))} 种调用"
            )
            return True
        return False

    # ════════════════════════════════════════════════════════════════
    # 辅助
    # ════════════════════════════════════════════════════════════════

    @staticmethod
    def _extract_final_response(messages: list) -> str:
        """从 messages 里找最后一条无 tool_calls 的 assistant 消息 content。

        llm_error 标记消息（LLM 调用失败留底）跳过——它是上一轮的旁路
        记录，不能被当成任何后续轮次的"最终答复"落 durable。
        """
        for msg in reversed(messages):
            if (msg.get("role") == "assistant" and not msg.get("tool_calls")
                    and not msg.get("llm_error")):
                return msg.get("content") or ""
        return ""

    # ════════════════════════════════════════════════════════════════
    # 兼容性方法（供 worker_process.py / cli.py 调用）
    # ════════════════════════════════════════════════════════════════

    def get_todos(self) -> list:
        """获取当前 todos。

        兼容性保留（恒 []，R2 起不再被调用方消费）：todos 实际经
        todos_update 事件 + turn_messages 传递，worker/CLI 从事件流捕获。
        """
        return []

    def rebind_tools(self) -> None:
        """重新拉取工具 + 绑定（MCP 热刷新时调用）。

        V3 的工具在每次 stream_invoke 时通过
        _resolve_and_bind_tools 动态组装，所以这里不需要预先 bind。
        但为了兼容 worker 的调用（它期望此方法存在），空实现即可。
        """
        logger.debug("V3 rebind_tools（no-op，工具在 stream_invoke 时动态组装）")

    def shutdown_mcp(self) -> None:
        """关闭 MCP 连接。"""
        try:
            from src.mcp.client import get_client_manager
            get_client_manager().shutdown()
        except Exception as e:
            logger.debug(f"shutdown_mcp 失败（忽略）: {e}")
