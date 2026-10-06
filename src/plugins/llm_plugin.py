"""
llm 插件 —— LlmService 注册为 "llm"，LLM 流式调用事件化。

事件契约（模块 import 时声明）：
    "llm/stream"  waterfall —— payload 是 StreamRequest dataclass

洋葱链语义：
    - 默认监听器（本插件在服务 start 时经 ctx.on 注册，位于最内层）
      用 stream_with_hard_timeout 生成 chunk 迭代器，放进 req.chunks
      结果槽，再 next() 放行外层
    - 外层监听器（审计/录制/mock 等在更外层 ctx 注册）可观察请求、
      或把 req.chunks 包一层再放行
    - 无任何监听器时 waterfall 原样返回请求（passthrough），
      LlmService.stream 兜底直接生成——服务单独可用
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator

from src.cordis import events
from src.cordis.context import Context
from src.cordis.service import Service

if TYPE_CHECKING:
    from src.llm.client import LLMClient
    from src.llm.messages import Chunk

logger = logging.getLogger("hermes.plugins.llm")

# 事件契约：模块 import 时声明（declare 幂等，同模式重复声明无害）
events.declare("llm/stream", "waterfall")

#: "llm/stream" 的默认超时（对齐 agent_v3 的 llm_timeout 缺省）
_DEFAULT_TIMEOUT_S = 120.0


@dataclass
class StreamRequest:
    """"llm/stream" waterfall 事件的 payload。

    监听器约定：可读 messages/tools/timeout_s/temperature/max_tokens，
    可把最终 chunk 迭代器写进 chunks 结果槽。service.stream 分发完后
    返回 req.chunks。

    短路语义（R2-13）：监听器不调 next 即短路。置 handled=True 表示
    "本监听器已接管本次调用"——调用方尊重短路（不给 chunks 时返回空流），
    不再 fallback 直连真 LLM；未置 handled 的短路维持旧行为（chunks 已
    填则用之，否则 passthrough 兜底）。
    """

    messages: list
    tools: list[dict] | None = None
    timeout_s: float = _DEFAULT_TIMEOUT_S
    temperature: float | None = None
    max_tokens: int | None = None
    chunks: "Iterator[Chunk] | None" = None
    """结果槽：监听器写入的 chunk 迭代器（None = 尚无监听器处理）。"""
    handled: bool = False
    """短路接管标志：True = 监听器已处理（调用方不得 fallback 直连）。"""
    client: "LLMClient | None" = None
    """调用方自备客户端（agent 的 set_llm_params 覆盖 / 测试注入 mock）。
    默认监听器优先用它，其次才经 service.client() 按 config 构造。"""
    cancel_event: "threading.Event | None" = None
    """流式取消事件（P2-8 防线 C）：调用方（agent ReAct 循环）预置、
    消费方超时/中途停止置位 → worker 线程在 chunk 边界断流退出。
    kernel 路径下默认监听器把它接进 stream_with_hard_timeout，不再丢失。"""


class LlmService(Service):
    """LLM 服务：client 工厂 + 事件化 stream。

    用法：
        ctx = boot_context()
        client = ctx.llm.client()                  # 按 config 默认值构造
        for chunk in ctx.llm.stream(msgs, tools):  # waterfall 分发
            ...
    """

    name = "llm"

    def __init__(self) -> None:
        self._ctx: Context | None = None

    # ─── 生命周期 ───

    def start(self, ctx: Context) -> None:
        self._ctx = ctx
        # 默认监听器：真正的流式实现（stream_with_hard_timeout）
        ctx.on("llm/stream", self._default_stream_listener)

    def stop(self) -> None:
        self._ctx = None

    # ─── 默认监听器（最内层：生成 chunk 迭代器入槽）───

    def _default_stream_listener(self, req: StreamRequest, next) -> Any:
        """真流式实现入 req.chunks 结果槽。

        R2-13：只在 chunks is None 时填——链上更早的监听器已写入的 chunks
        （外部 mock / set_llm_params 覆盖产物）不被丢弃。客户端优先用
        req.client（调用方自备：agent 的参数覆盖/测试注入），否则按 config 构造。
        """
        if req.chunks is None:
            client = req.client if req.client is not None else self.client()
            req.chunks = self._raw_stream(client, req)
        return next()

    @staticmethod
    def _raw_stream(client: "LLMClient", req: StreamRequest) -> "Iterator[Chunk]":
        from src.agent.llm_stream import stream_with_hard_timeout

        return stream_with_hard_timeout(
            client,
            req.messages,
            tools=req.tools,
            timeout_s=req.timeout_s,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            cancel_event=req.cancel_event,
        )

    # ─── 公开 API ───

    def client(self, **overrides: Any) -> "LLMClient":
        """按 config 默认值构造 LLMClient（overrides 覆盖任意构造参数）。

        默认值从 ctx.config 读 api/base/model（含 max_tokens/llm_timeout），
        缺项交由 LLMClient 自身回落 get_settings()。
        """
        from src.llm.client import LLMClient

        cfg = self._ctx.try_get("config") if self._ctx is not None else None
        kwargs: dict[str, Any] = {}
        if cfg is not None:
            kwargs["api_key"] = getattr(cfg, "openai_api_key", None) or None
            kwargs["base_url"] = getattr(cfg, "openai_base_url", None) or None
            kwargs["model"] = getattr(cfg, "llm_model_name", "") or ""
            max_tokens = getattr(cfg, "max_tokens", None)
            if max_tokens:
                kwargs["max_tokens"] = int(max_tokens)
            llm_timeout = getattr(cfg, "llm_timeout", None)
            if llm_timeout:
                kwargs["request_timeout"] = int(llm_timeout)
        kwargs.update(overrides)
        return LLMClient(**kwargs)

    def stream(
        self,
        messages: list,
        tools: list[dict] | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        cancel_event: "threading.Event | None" = None,
        **kw: Any,
    ) -> "Iterator[Chunk]":
        """流式调用 LLM（事件化）：构造 StreamRequest 经 waterfall 分发。

        无监听器（服务未 start / 独立使用）时 passthrough——直接用
        默认实现兜底生成 chunk 迭代器。

        cancel_event：调用方预置的取消事件（agent 每轮流式预置，超时/
        中途停止置位），随请求透传给默认监听器 → stream_with_hard_timeout。
        """
        req = StreamRequest(
            messages=messages,
            tools=tools,
            timeout_s=timeout_s,
            temperature=kw.get("temperature"),
            max_tokens=kw.get("max_tokens"),
            cancel_event=cancel_event,
        )
        if self._ctx is not None:
            result = self._ctx.waterfall("llm/stream", req)
            if isinstance(result, StreamRequest):
                req = result
        if req.chunks is None:
            if req.handled:
                # R2-13：监听器短路且声明已处理但不给 chunks → 空流（尊重短路）
                return iter(())
            # passthrough：链上无人处理，直接生成
            client = req.client if req.client is not None else self.client()
            req.chunks = self._raw_stream(client, req)
        return req.chunks


def apply(ctx: Context, config: dict) -> None:
    ctx.register("llm", LlmService())
