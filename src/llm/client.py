"""
============================================
LLMClient —— openai SDK 封装（流式/非流式 chat + chunk 拼接）
============================================
薄封装 openai.OpenAI，提供同步 chat() 和 stream_chat()。

取代关系：
    ChatOpenAI(...)           → LLMClient(...)
    llm.invoke(messages)      → client.chat(messages)
    llm.stream(messages)      → client.stream_chat(messages)
    llm.bind_tools(tools)     → client.chat(messages, tools=[...])
    AIMessageChunk 拼接       → Chunk 累积（见 _accumulate_chunks）

关键优势：
    - openai SDK 直接暴露 delta.reasoning（删掉 think.py 的 monkeypatch）
    - 双 deadline 硬超时逻辑（搬自 graph.py:_stream_llm_with_hard_timeout）
      将在 Phase 4 接入。当前先用 openai SDK 原生 timeout。

"""

from __future__ import annotations

import contextvars
import json
import logging
import time
from typing import Any, Generator

from openai import OpenAI

from src.llm.messages import AIMsg, Chunk

logger = logging.getLogger("hermes.llm.client")

# 输出因 max_tokens 截断（finish_reason=length）时追加到记账 output 尾部
# 的标记——截断曾经完全静默（看板只能靠「恰好 2000 tokens」人工发现）。
_TRUNCATED_NOTE = "\n\n[⚠️ 输出因 max_tokens 上限被截断（finish_reason=length）]"


def _tool_calls_summary(tool_calls: list) -> str:
    """工具调用轮的 output 留底：content 为空时记录发起了哪些工具调用。

    否则这些「✓ 成功」的行在用量明细里看起来像模型什么都没说（实际是
    只回了 tool_calls——ReAct 中间轮/子代理轮的常态）。参数截 ~300 字。
    """
    if not tool_calls:
        return ""
    lines: list[str] = []
    for i, tc in enumerate(tool_calls, 1):
        fn = (tc or {}).get("function", {}) or {}
        name = fn.get("name", "") or "unknown"
        args = fn.get("arguments", "") or ""
        try:
            args = json.dumps(json.loads(args), ensure_ascii=False)
        except Exception:
            pass
        if len(args) > 300:
            args = args[:300] + "…"
        lines.append(f"⚙️ 工具调用 #{i}: {name}\n参数: {args}")
    lines.append("（本轮为工具调用：模型未产生文本输出）")
    return "\n".join(lines)


def _content_to_text(content: Any) -> str:
    """消息 content → 纯文本留底。多模态 list 形状里的图片 data URI 只留
    占位标记（base64 动辄数百 KB，入库前必须剥掉）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                ptype = part.get("type", "unknown")
                if ptype == "text":
                    parts.append(str(part.get("text", "")))
                elif ptype == "image_url":
                    parts.append("[图片已省略]")
                else:
                    parts.append(f"[{ptype}]")
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content)


def _record_req_messages(msgs_dict: list) -> str:
    """请求 messages → 留底 JSON（[{role, content}]，供用量明细 👀 回看）。

    逐条内容先限长（单条 4000 字）再序列化。总量超 REQ_MESSAGES_MAX 时
    **丢最旧的非 system 消息**重序列化直至放得下（尾部加省略说明）——
    不能对 JSON 字符串硬截：拦腰截断产生非法 JSON，前端解析失败只能
    原样展示，且丢的恰是尾部最新的消息。序列化任何失败落空串（记账是
    旁路设施，绝不影响业务）。
    """
    try:
        from src.storage.usage_store import REQ_MESSAGES_MAX
        limit = REQ_MESSAGES_MAX - 250   # 留余量给省略说明头
    except Exception:
        limit = 11750
    try:
        slim = [
            {
                "role": str(m.get("role", "")),
                "content": _content_to_text(m.get("content"))[:4000],
            }
            for m in msgs_dict if isinstance(m, dict)
        ]
        s = json.dumps(slim, ensure_ascii=False)
        dropped = 0
        while len(s) > limit and len(slim) > 1:
            # 丢最旧的非 system 消息（system 是上下文骨架，最后才丢）
            for i, m in enumerate(slim):
                if m.get("role") != "system":
                    slim.pop(i)
                    dropped += 1
                    break
            else:
                slim.pop(0)
                dropped += 1
            s = json.dumps(slim, ensure_ascii=False)
        if dropped:
            s = json.dumps(
                [{"role": "system",
                  "content": f"[留底省略：最旧的 {dropped} 条消息未入库]"}] + slim,
                ensure_ascii=False)
        return s
    except Exception:
        return ""

# 主 agent 热切换的模型四项（model/base_url/api_key/context_window）。
# 会话级：stream_invoke 入口写入，task 子代理经 copy_context 读到同一覆盖，
# 避免子代理仍打 config.yaml 里的旧模型。None/{} = 无覆盖，回退 settings。
_current_llm_overrides: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "hermes_llm_overrides", default=None
)


def set_current_llm_overrides(overrides: dict | None) -> None:
    """写入当前会话的模型覆盖（主 agent stream_invoke 入口调用）。"""
    _current_llm_overrides.set(dict(overrides) if overrides else {})


def get_current_llm_overrides() -> dict:
    """读取当前会话的模型覆盖。无覆盖时返回空 dict。"""
    return dict(_current_llm_overrides.get() or {})


# LLM 用量记账的业务归属（session_id/scope/caller）。会话级：stream_invoke
# 入口写入，随 copy_context 自动传入流式 worker 线程与子代理；client 层
# 记账时读取。None = 无归属（直接调 chat()/stream_chat() 的辅助链路），
# 记账行 session_id/scope/caller 落空串。
_current_usage_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "hermes_llm_usage_ctx", default=None
)


def set_current_usage_ctx(session_id: str = "", scope: str = "", caller: str = "") -> contextvars.Token:
    """写入当前会话的用量记账归属（stream_invoke 入口调用）。

    scope 由调用方经 session_log.sid_scope(session_id) 推导（client 层不
    反向依赖 agent 层）。返回 Token 供 reset_current_usage_ctx 还原。
    """
    return _current_usage_ctx.set({
        "session_id": session_id or "",
        "scope": scope or "",
        "caller": caller or "",
    })


def reset_current_usage_ctx(token: contextvars.Token) -> None:
    """还原 set_current_usage_ctx 的写入（Token 须来自同一线程同一 context）。"""
    _current_usage_ctx.reset(token)


def get_current_usage_ctx() -> dict:
    """读取当前用量记账归属。无归属时返回空 session_id/scope/caller。"""
    ctx = _current_usage_ctx.get() or {}
    return {
        "session_id": ctx.get("session_id", ""),
        "scope": ctx.get("scope", ""),
        "caller": ctx.get("caller", ""),
    }


class LLMClient:
    """薄封装 openai.OpenAI。

    Phase 1 只提供基础 chat / stream_chat。
    Phase 4 会加入双 deadline 硬超时（搬自 graph.py）。
    Phase 5 会加入 think splitter 集成。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int | None = None,
        request_timeout: int = 120,
        max_retries: int = 0,
        extra_body: dict[str, Any] | None = None,
    ):
        # 延迟读 config：允许调用方覆盖，也允许不传（从 config 读）
        if api_key is None or base_url is None or not model:
            from config import get_settings
            s = get_settings()
            api_key = api_key or getattr(s, "openai_api_key", "")
            base_url = base_url or getattr(s, "openai_base_url", "")
            model = model or getattr(s, "llm_model_name", "")

        self.model = model
        self.temperature = temperature
        # None = 请求不带 max_tokens 字段，由服务端/模型默认决定（历史原因
        # 曾默认 2000——当年模型上下文普遍 ≤6w；现代后端无需客户端设限）。
        self.max_tokens = max_tokens
        # 思考开关由调用方经 extra_body 显式表达（如
        # {"chat_template_kwargs": {"enable_thinking": True}}）；不传 =
        # 不发任何 kwarg，尊重各后端（vLLM / Ollama / llama.cpp）默认。
        # 解析层与开关无关：reasoning / reasoning_content 字段与内联
        # <think> 标签两层吸收始终开启，模型给了就流式透出。
        # 调用方可经 chat()/invoke_simple() 的 extra_body 参数按次覆盖。
        self.extra_body = extra_body or {}

        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=request_timeout,
            max_retries=max_retries,
        )

    # ════════════════════════════════════════════════════════════════
    # 同步调用
    # ════════════════════════════════════════════════════════════════

    def _resolve_call_params(
        self,
        temperature: float | None,
        max_tokens: int | None,
        extra_body: dict[str, Any] | None,
    ) -> tuple[float, dict[str, Any]]:
        """解析调用级参数覆盖。

        Returns:
            (生效 temperature, 生效 extra_body)。extra_body 提供时（含空
            dict）整体替换实例值——记忆等辅助调用借此发出不带任何模型
            专属参数的纯标准请求。max_tokens 的解析在各方法内做（None =
            不发字段，与"用实例默认"区分）。
        """
        eff_temp = temperature if temperature is not None else self.temperature
        eff_extra = extra_body if extra_body is not None else self.extra_body
        return eff_temp, eff_extra

    def _record_usage(
        self,
        *,
        status: str,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        duration_ms: float | None = None,
        error: str = "",
        req_messages: str = "",
        reasoning: str = "",
        output: str = "",
        tools: str = "",
    ) -> None:
        """写一行 LLM 用量记录（chat / stream_chat 共用）。

        业务归属取当前 usage ctx（无归属时落空串）；存储侧 record_usage
        自身绝不抛，这里再兜一层（import 失败等也不打断业务）。
        """
        try:
            from src.storage import usage_store

            ctx = get_current_usage_ctx()
            usage_store.record_usage(
                session_id=ctx["session_id"],
                scope=ctx["scope"],
                caller=ctx["caller"],
                model=self.model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                duration_ms=duration_ms,
                status=status,
                error=(error or "")[:200],
                req_messages=req_messages,
                reasoning=reasoning,
                output=output,
                tools=tools,
            )
        except Exception:
            logger.warning("LLM 用量记账失败（忽略，不影响业务）", exc_info=True)

    @staticmethod
    def _tokens_of(usage: Any) -> tuple[int | None, int | None]:
        """openai usage 对象 → (prompt_tokens, completion_tokens)，缺则 None。"""
        if usage is None:
            return None, None
        tin = getattr(usage, "prompt_tokens", None)
        tout = getattr(usage, "completion_tokens", None)
        return (int(tin) if tin is not None else None,
                int(tout) if tout is not None else None)

    def chat(
        self,
        messages: list,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> AIMsg:
        """同步调用 LLM。

        Args:
            messages: 消息列表（SystemMsg/HumanMsg/AIMsg/ToolMsg 或 dict）。
            tools: OpenAI tools 参数（[{"type":"function","function":{...}}]）。
            temperature: 覆盖默认温度。
            max_tokens: 覆盖默认上限；与实例值均为 None 时请求**不带**该字段
                （服务端默认生效），不再回退任何隐式数值。
            extra_body: 按次覆盖实例 extra_body（None = 用实例值；{} = 不发
                任何模型专属 kwarg）。

        Returns:
            AIMsg: 含 content / tool_calls / reasoning。
        """
        msgs_dict = [self._msg_to_dict(m) for m in messages]
        eff_temp, eff_extra = self._resolve_call_params(temperature, max_tokens, extra_body)
        eff_mt = max_tokens if max_tokens is not None else self.max_tokens
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": msgs_dict,
            "temperature": eff_temp,
            "extra_body": eff_extra,
        }
        if eff_mt is not None:
            kwargs["max_tokens"] = eff_mt
        if tools:
            kwargs["tools"] = tools

        # 用量记账：create 前后计时；成功记 ok 行（tokens 取 resp.usage，
        # 服务端不给则 NULL），异常记 error 行后原样 re-raise（不吞）。
        # 两条路径都带请求留底；ok 行再带推理/最终输出（先 parse 再记账）。
        t0 = time.monotonic()
        req_record = _record_req_messages(msgs_dict)
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as e:
            self._record_usage(
                status="error",
                duration_ms=(time.monotonic() - t0) * 1000.0,
                error=str(e),
                req_messages=req_record,
            )
            raise
        tokens_in, tokens_out = self._tokens_of(getattr(resp, "usage", None))
        parsed = self._parse_response(resp)
        finish = getattr(getattr(resp, "choices", None)[0], "finish_reason", None) \
            if getattr(resp, "choices", None) else None
        if finish == "length":
            logger.warning("LLM 输出因 max_tokens 截断（finish_reason=length）: model=%s", self.model)
        # 工具调用轮（content 空但发起 tool_calls）：留底记工具摘要而非空串
        output_text = parsed.content or ""
        if not output_text.strip():
            output_text = _tool_calls_summary(parsed.tool_calls)
        tools_text = ", ".join(
            (tc or {}).get("function", {}).get("name", "") or "unknown"
            for tc in parsed.tool_calls)
        self._record_usage(
            status="ok",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_ms=(time.monotonic() - t0) * 1000.0,
            req_messages=req_record,
            reasoning=parsed.reasoning,
            output=output_text + (_TRUNCATED_NOTE if finish == "length" else ""),
            tools=tools_text,
        )
        return parsed

    def invoke_simple(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> str:
        """便捷方法：传 prompt 字符串，直接返回 content 字符串。

        简单文本补全入口（prompt 字符串直接作 user 消息）。
        记忆模块（decider/extractor/summarizer）和 compact 的摘要 LLM 用这个。

        Args:
            prompt: 用户 prompt 文本（会被包成 HumanMsg）。
            system: 可选的 system prompt（None 则不发 system）。
            temperature: 覆盖温度。
            max_tokens: 覆盖上限（None = 用实例值，实例也为 None 则不带字段）。
            extra_body: 按次覆盖实例 extra_body。

        Returns:
            LLM 回复的 content 字符串（空内容返回 "")。
        """
        messages: list = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = self.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body=extra_body,
        )
        return resp.content

    # ════════════════════════════════════════════════════════════════
    # 流式调用
    # ════════════════════════════════════════════════════════════════

    def _open_stream(self, kwargs: dict[str, Any]):
        """发出流式 create 请求，带 stream_options 兼容防御。

        openai 要求请求带 stream_options={"include_usage": True} 才会在流
        末尾发 usage-only 尾包；部分 OpenAI 兼容端点（旧 vLLM / llama.cpp
        等）不认该参数而报 BadRequest——错误信息含 stream_options 字样时
        去掉该参数重试一次并告警（这条流拿不到 usage，记账行 tokens 落
        NULL），其余异常原样抛出。
        """
        try:
            return self._client.chat.completions.create(**kwargs)
        except Exception as e:
            if "stream_options" not in str(e):
                raise
            kwargs.pop("stream_options", None)
            logger.warning(f"端点不支持 stream_options，去掉该参数重试（本流无 usage 记账）: {e}")
            return self._client.chat.completions.create(**kwargs)

    def stream_chat(
        self,
        messages: list,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> Generator[Chunk, None, None]:
        """流式调用 LLM，逐 chunk yield。

        内置 <think> 标签自动拆分：当模型把推理过程塞进 content（如 Ornith/
        DeepSeek-R1 等用 `</think>` 分隔），自动拆成 reasoning_delta +
        纯净 content_delta，与原生 delta.reasoning 字段统一。

        若模型经原生 delta.reasoning / delta.reasoning_content 字段返回推理，
        首个 reasoning chunk 即把拆分器锁定为纯透传——否则 PROBE 探测会把
        正文攒进缓冲（直到攒满或流结束），表现为正文"整段弹出"不流式；
        锁定后 content 偶发的内联 <think> 标签不再剥离（字段优先，取舍见
        ThinkSplitter.lock_content）。

        max_tokens 与实例值均为 None 时请求不带该字段；extra_body 提供时
        按次覆盖实例值。

        注意：双 deadline 硬超时（间隙超时 + 总时长上限）在 Phase 4 接入。
        当前先用 openai SDK 原生 timeout。
        """
        from src.think import ThinkSplitter

        msgs_dict = [self._msg_to_dict(m) for m in messages]
        eff_temp, eff_extra = self._resolve_call_params(temperature, max_tokens, extra_body)
        eff_mt = max_tokens if max_tokens is not None else self.max_tokens
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": msgs_dict,
            "temperature": eff_temp,
            "stream": True,
            "extra_body": eff_extra,
        }
        if eff_mt is not None:
            kwargs["max_tokens"] = eff_mt
        if tools:
            kwargs["tools"] = tools
        # 流式 usage 记账：请求带 include_usage，服务端才会在流末尾发
        # choices 为空的 usage-only 尾包（端点不认该参数时 _open_stream
        # 防御重试，这条流 tokens 落 NULL）。
        kwargs["stream_options"] = {"include_usage": True}

        # ThinkSplitter 的 emit 回调：把拆分后的 piece 包成 Chunk yield
        def _emit(kind: str, piece: str) -> None:
            if kind == "reasoning":
                _splitter_q.append(Chunk(reasoning_delta=piece))
            else:
                _splitter_q.append(Chunk(content_delta=piece))

        splitter = ThinkSplitter(_emit)
        _splitter_q: list[Chunk] = []

        # 用量记账：计时覆盖建流 + 整条流消耗；usage 尾包由本生成器在循环
        # 里直接捕获（生成器局部变量，天然随流隔离——并发多流互不串扰，
        # 不做实例级暂存）；正常耗尽记 ok 行，中途异常记 error 行后 re-raise。
        # out_content/out_reasoning 随 yield 同步累积最终文本（记账 ok 行
        # 留底用）；finish_reason 取流内最后一个非空值（"length"=截断）。
        t0 = time.monotonic()
        stream_usage: dict[str, int | None] = {"in": None, "out": None}
        req_record = _record_req_messages(msgs_dict)
        out_content: list[str] = []
        out_reasoning: list[str] = []
        # 工具调用留底（2026-09-19）：tool_call_deltas 按 index 增量合并，
        # content 为空的纯工具轮 output 记工具摘要（否则明细里像模型没说话）
        tc_acc: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        try:
            stream = self._open_stream(kwargs)
            native_reasoning = False  # 本流中出现过原生 reasoning 字段（透传锁定信号）
            for chunk in stream:
                # usage-only 尾包（choices 为空、只带 usage）：先捕获 usage
                # 再交 _parse_chunk（其对该类 chunk 返回 None 跳过）。兼容
                # 把 usage 附在末个内容 chunk 上的非标准端点——有则覆盖。
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    stream_usage["in"], stream_usage["out"] = self._tokens_of(usage)
                if getattr(chunk, "choices", None):
                    fr = getattr(chunk.choices[0], "finish_reason", None)
                    if fr:
                        finish_reason = fr
                parsed = self._parse_chunk(chunk)
                if parsed is None:
                    continue
                for tc_delta in parsed.tool_call_deltas:
                    idx = tc_delta.get("index", 0)
                    tc = tc_acc.setdefault(idx, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if tc_delta.get("id"):
                        tc["id"] = tc_delta["id"]
                    fn = tc_delta.get("function", {})
                    if fn.get("name"):
                        tc["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        tc["function"]["arguments"] += fn["arguments"]

                # 有原生 reasoning 字段 → 推理直接透出，并锁定拆分器为纯透传。
                # 原生路径 content 不含 <think> 标签，继续 PROBE 探测只会把正文
                # 攒进缓冲（不流式）；两路并存时字段优先，锁定后内联标签不再
                # 剥离（取舍见 ThinkSplitter.lock_content）。
                if parsed.reasoning_delta:
                    if not native_reasoning:
                        native_reasoning = True
                        _splitter_q.clear()  # 清掉上一轮已 yield 的残留引用，防重复下发
                        splitter.lock_content()
                        # 锁定可能吐出 PROBE 期已攒正文（content 先于 reasoning
                        # 到达的混流），须先于本 reasoning chunk 按序 yield，不能丢
                        for flushed in _splitter_q:
                            out_content.append(flushed.content_delta)
                            out_reasoning.append(flushed.reasoning_delta)
                            yield flushed
                    out_reasoning.append(parsed.reasoning_delta)
                    yield parsed
                    continue

                # content 可能有 <think> 标签 → 喂给 ThinkSplitter 拆分
                # （LOCKED 态下为纯透传 + 尾部安全扣留）
                if parsed.content_delta:
                    _splitter_q.clear()
                    splitter.feed(parsed.content_delta)
                    for split_chunk in _splitter_q:
                        # 保留原 chunk 的 tool_call_deltas（ThinkSplitter 不碰这些）
                        if parsed.tool_call_deltas:
                            split_chunk.tool_call_deltas = parsed.tool_call_deltas
                        out_content.append(split_chunk.content_delta)
                        out_reasoning.append(split_chunk.reasoning_delta)
                        yield split_chunk
                else:
                    # 纯 tool_call chunk（无 content），直接 yield
                    yield parsed

            # flush 拆分器（处理尾部半个标签 / PROBE 缓冲区残留 / LOCKED 扣留尾巴）
            _splitter_q.clear()
            splitter.flush()
            for split_chunk in _splitter_q:
                out_content.append(split_chunk.content_delta)
                out_reasoning.append(split_chunk.reasoning_delta)
                yield split_chunk
        except Exception as e:
            self._record_usage(
                status="error",
                duration_ms=(time.monotonic() - t0) * 1000.0,
                error=str(e),
                req_messages=req_record,
            )
            raise

        # 流正常耗尽：记 ok 行（tokens 可能 None——端点未发 usage 尾包）。
        # 消费方中途弃流（GeneratorExit）不记行：BaseException 不进上面的
        # except Exception，取消/放弃的流不产生 error 噪声。
        if finish_reason == "length":
            logger.warning("LLM 输出因 max_tokens 截断（finish_reason=length）: model=%s", self.model)
        content_text = "".join(out_content)
        if not content_text.strip():
            content_text = _tool_calls_summary(
                [tc_acc[i] for i in sorted(tc_acc)])
        tools_text = ", ".join(
            (tc_acc[i].get("function", {}).get("name", "") or "unknown")
            for i in sorted(tc_acc))
        self._record_usage(
            status="ok",
            tokens_in=stream_usage["in"],
            tokens_out=stream_usage["out"],
            duration_ms=(time.monotonic() - t0) * 1000.0,
            req_messages=req_record,
            reasoning="".join(out_reasoning),
            output=content_text + (_TRUNCATED_NOTE if finish_reason == "length" else ""),
            tools=tools_text,
        )

    # ════════════════════════════════════════════════════════════════
    # chunk 累积（流式结束后拼成完整 AIMsg）
    # ════════════════════════════════════════════════════════════════

    @staticmethod
    def accumulate(chunks: list[Chunk]) -> AIMsg:
        """把流式 chunks 累积成完整 AIMsg。

        chunk 拼接（等价增量对象相加）。
        累积 content / reasoning / tool_calls（按 index 合并）。
        """
        content = ""
        reasoning = ""
        tool_calls_by_index: dict[int, dict[str, Any]] = {}

        for chunk in chunks:
            content += chunk.content_delta
            reasoning += chunk.reasoning_delta
            for tc_delta in chunk.tool_call_deltas:
                idx = tc_delta.get("index", 0)
                if idx not in tool_calls_by_index:
                    tool_calls_by_index[idx] = {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                tc = tool_calls_by_index[idx]
                if tc_delta.get("id"):
                    tc["id"] = tc_delta["id"]
                fn = tc_delta.get("function", {})
                if fn.get("name"):
                    tc["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    tc["function"]["arguments"] += fn["arguments"]

        # 按 index 排序
        tool_calls = [tool_calls_by_index[i] for i in sorted(tool_calls_by_index)]

        return AIMsg(content=content, tool_calls=tool_calls, reasoning=reasoning)

    # ════════════════════════════════════════════════════════════════
    # 内部解析
    # ════════════════════════════════════════════════════════════════

    @staticmethod
    def _msg_to_dict(msg: Any) -> dict[str, Any]:
        """把 SystemMsg/HumanMsg/AIMsg/ToolMsg 或 dict 统一转成 dict。"""
        if isinstance(msg, dict):
            return msg
        if hasattr(msg, "to_dict"):
            return msg.to_dict()
        raise TypeError(f"未知消息类型: {type(msg)}")

    def _parse_response(self, resp: Any) -> AIMsg:
        """解析非流式响应。"""
        from src.think import extract_think_content

        choice = resp.choices[0]
        msg = choice.message
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                })
        # vLLM 的 reasoning 字段（可能叫 reasoning 或 reasoning_content）
        reasoning = ""
        if hasattr(msg, "reasoning") and msg.reasoning:
            reasoning = msg.reasoning
        elif hasattr(msg, "reasoning_content") and msg.reasoning_content:
            reasoning = msg.reasoning_content

        content = msg.content or ""
        # 若无独立 reasoning 字段，检查 content 里是否含 <think> 标签
        if not reasoning and content:
            extracted_reasoning, clean_content = extract_think_content(content)
            if extracted_reasoning:
                reasoning = extracted_reasoning
                content = clean_content

        return AIMsg(
            content=content,
            tool_calls=tool_calls,
            reasoning=reasoning,
        )

    def _parse_chunk(self, chunk: Any) -> Chunk | None:
        """解析流式 chunk → Chunk。"""
        if not chunk.choices:
            # usage-only 尾包（stream_options.include_usage 生效时服务端在
            # 流末尾发的 choices 为空、只带 usage 的 chunk）：usage 已由
            # stream_chat 循环先行捕获记账，这里返回 None 跳过，不再静默
            # 丢弃信息。
            return None
        delta = chunk.choices[0].delta

        content_delta = delta.content if hasattr(delta, "content") else ""
        if content_delta is None:
            content_delta = ""

        reasoning_delta = ""
        if hasattr(delta, "reasoning") and delta.reasoning:
            reasoning_delta = delta.reasoning
        elif hasattr(delta, "reasoning_content") and delta.reasoning_content:
            reasoning_delta = delta.reasoning_content

        tool_call_deltas = []
        if hasattr(delta, "tool_calls") and delta.tool_calls:
            for tc in delta.tool_calls:
                d: dict[str, Any] = {"index": tc.index}
                if tc.id:
                    d["id"] = tc.id
                fn: dict[str, str] = {}
                if tc.function and tc.function.name:
                    fn["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    fn["arguments"] = tc.function.arguments
                if fn:
                    d["function"] = fn
                tool_call_deltas.append(d)

        if not content_delta and not reasoning_delta and not tool_call_deltas:
            return None

        return Chunk(
            content_delta=content_delta,
            reasoning_delta=reasoning_delta,
            tool_call_deltas=tool_call_deltas,
        )
