"""
============================================
LLM chunk 流纯消费 —— 自 agent_v3._react_loop 外提（P3 轻拆）
============================================
输入：chunk 生成器（llm/stream waterfall 的产物）+ 回调集（取消探针、
取消事件）；输出：StreamConsumption 聚合结果 + token/reasoning_token
事件流（yield 透传）。

零行为变化约定（agent_v3 调用处）：
- 事件即时透传（token 随到随 yield，时序不变）；chunk 粒度取消 +
  防线 C 置位 cancel_event 语义不变
- 聚合就地写调用方持有的 out：即便迭代中途抛 TimeoutError / LLM 异常，
  已收到的部分聚合仍在 out 里可见（对齐原循环局部变量在异常路径的
  可见性——except TimeoutError 把已收内容落 messages 依赖这一点）
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Generator

if TYPE_CHECKING:
    from src.llm.messages import Chunk

__all__ = ["StreamConsumption", "consume_llm_stream"]


@dataclass
class StreamConsumption:
    """一次 LLM 流消费的聚合结果（原 _react_loop 局部变量集合的落点）。

    调用方创建并传入（out=），本函数逐 chunk 就地累积——异常中断时
    部分结果仍在调用方手里，不会随生成器帧丢失。
    """

    chunks: list["Chunk"] = field(default_factory=list)
    full_content: str = ""
    full_reasoning: str = ""
    cancelled: bool = False


def consume_llm_stream(
    chunks_iter,
    *,
    cancel_probe: Callable[[], bool],
    cancel_event: threading.Event,
    out: StreamConsumption,
) -> Generator[dict, None, None]:
    """逐 chunk 消费 LLM 流：yield token / reasoning_token，就地聚合进 out。

    - 每个 chunk 边界先轮询 cancel_probe（agent 内的第一道取消，比
      worker drain 的事件间隙轮询更快）：命中 → out.cancelled=True +
      置位 cancel_event（防线 C：worker 线程立即断流退出，不等生成完，
      vLLM 槽位不被白占）并停止消费——触发取消的那个 chunk 不消费
    - content_delta / reasoning_delta 分别聚合并即时 yield 对应事件
    """
    for chunk in chunks_iter:
        # chunk 粒度取消：中断长输出（worker drain 循环同样能
        # 在事件间隙 break，这里是 agent 内的第一道、更快）
        if cancel_probe():
            out.cancelled = True
            # 防线 C：置位让 worker 立即断流退出（不等生成完，
            # vLLM 槽位不被白占）
            cancel_event.set()
            break
        out.chunks.append(chunk)
        if chunk.content_delta:
            out.full_content += chunk.content_delta
            yield {"type": "token", "content": chunk.content_delta}
        if chunk.reasoning_delta:
            out.full_reasoning += chunk.reasoning_delta
            yield {"type": "reasoning_token", "content": chunk.reasoning_delta}
