"""
============================================
LLM 双 deadline 硬超时流式 —— 从 graph.py 整体搬出
============================================
防止自托管 vLLM 端点的"流式半开连接"导致进程永久挂死。

双 deadline 设计：
    1. gap deadline（间隙超时）：interval 秒内无任何 chunk → 触发
    2. total deadline（总时长上限）：从 start() 起累计 max_total 秒必触发，
       reset() 不重置它（专防 trickle-hang）

三道防线（搬自 graph.py 的踩坑修复）：
    A. 流式半开（gap timer）—— 无 chunk 时超时
    B. contextvars 传播 —— worker 线程 copy_context()，否则 pollutes callback state
    C. 连接/线程泄漏 —— cancel_event（消费方超时/中途退出置位）→ worker
       线程在下一个 chunk 边界 close() 底层 openai 流断开 socket 并退出；
       不再把流读到生成完（vLLM 槽位被白占、token 白烧）

底层用 LLMClient.stream_chat（openai SDK 直接暴露 delta.reasoning）

"""

from __future__ import annotations

import contextvars
import logging
import queue as _queue
import threading
import time as _t
from typing import TYPE_CHECKING, Generator

if TYPE_CHECKING:
    from src.llm.client import LLMClient
    from src.llm.messages import Chunk

logger = logging.getLogger("hermes.agent.llm_stream")

#: worker 线程名（测试轮询线程退出用）
WORKER_THREAD_NAME = "llm-stream-worker"


def stream_with_hard_timeout(
    llm_client: "LLMClient",
    messages: list,
    tools: list[dict] | None = None,
    timeout_s: float = 120.0,
    temperature: float | None = None,
    max_tokens: int | None = None,
    cancel_event: "threading.Event | None" = None,
) -> Generator["Chunk", None, None]:
    """带双 deadline 硬超时的 LLM 流式调用。

    Args:
        llm_client: LLMClient 实例（封装 openai SDK）。
        messages: 消息列表（SystemMsg/HumanMsg/AIMsg/ToolMsg 或 dict）。
        tools: OpenAI tools 参数（可选）。
        timeout_s: 间隙超时秒数（total = timeout_s * 3）。
        temperature: 覆盖温度。
        max_tokens: 覆盖 max_tokens。
        cancel_event: 防线 C 的取消事件（缺省内部自建）。消费方超时/中途
            退出置位（本生成器的 finally 自动覆盖 close()/GeneratorExit/
            耗尽/超时各路径），worker 线程在下一个 chunk 边界收到后
            close() 底层 openai 流并退出；需要外部强制停止的调用方也可
            传入自备 Event 直接 set()。

    Yields:
        Chunk: 流式 chunk（含 content_delta / reasoning_delta / tool_call_deltas）。

    Raises:
        TimeoutError: 间隙超时或总时长超时。
    """
    _SENTINEL_END = object()
    _chunks_q: _queue.Queue = _queue.Queue()
    _worker_exc: list = []

    if cancel_event is None:
        cancel_event = threading.Event()

    # 防线 B：worker 线程必须 copy_context()，否则 contextvars（user_id/vfs/depth
    # + openai SDK 内部的 callback state）不会传播，导致下一轮工具节点状态污染。
    ctx = contextvars.copy_context()

    def _worker():
        stream = None
        try:
            stream = llm_client.stream_chat(
                messages, tools=tools, temperature=temperature, max_tokens=max_tokens
            )
            for chunk in stream:
                if cancel_event.is_set():
                    # 防线 C：消费方已超时/停止——立即退出，不再把流读到生成完
                    break
                _chunks_q.put(chunk)
        except Exception as e:
            _worker_exc.append(e)
        finally:
            # 防线 C：close() 底层流（generator close 释放 openai/httpx 连接
            # 断开 socket）——超时/停止后 vLLM 槽位不再被白占
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            _chunks_q.put(_SENTINEL_END)

    t = threading.Thread(target=ctx.run, args=(_worker,), daemon=True,
                         name=WORKER_THREAD_NAME)
    t.start()

    # 双 deadline 消费循环
    deadline = _t.time() + timeout_s          # 间隙 deadline（每 chunk reset）
    total_deadline = _t.time() + timeout_s * 3  # 总时长上限（不 reset）

    last_chunk_time = _t.time()
    try:
        while True:
            now = _t.time()
            wait = min(timeout_s, deadline - now, total_deadline - now)
            if wait <= 0:
                # 总时长超限
                if _t.time() >= total_deadline:
                    raise TimeoutError(
                        f"LLM 流式总时长超过 {timeout_s * 3:.0f}s（硬上限）"
                    )
                # 间隙超限
                raise TimeoutError(
                    f"LLM 流式 {timeout_s:.0f}s 内无新 chunk（疑似半开连接）"
                )
            try:
                chunk = _chunks_q.get(timeout=wait)
            except _queue.Empty:
                # 再次检查是哪种超时
                now = _t.time()
                if now >= total_deadline:
                    raise TimeoutError(
                        f"LLM 流式总时长超过 {timeout_s * 3:.0f}s（硬上限）"
                    )
                raise TimeoutError(
                    f"LLM 流式 {timeout_s:.0f}s 内无新 chunk（疑似半开连接）"
                )

            if chunk is _SENTINEL_END:
                # worker 正常结束 —— 若有异常，re-raise
                if _worker_exc:
                    raise _worker_exc[0]
                return

            # 防线 C（消费侧）：cancel 已置位 → worker 置位前排队的积压
            # chunk 一律丢弃，立即收尾
            if cancel_event.is_set():
                return

            yield chunk
            last_chunk_time = _t.time()
            deadline = last_chunk_time + timeout_s  # 重置间隙，不动 total
    except TimeoutError:
        logger.warning(f"LLM 流式超时，即将置位 cancel_event，worker 将断流退出")
        raise
    finally:
        # 防线 C：消费方超时 / close() / GeneratorExit / 耗尽一律置位——
        # worker 在下一个 chunk 边界 close() 底层流并退出（置位发生在
        # 本 finally，晚于上方 except 的日志——日志措辞为"即将置位"）
        cancel_event.set()
