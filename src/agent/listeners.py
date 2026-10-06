"""
============================================
HermesAgentV3 默认监听器注册 —— 自 agent_v3 外提（P3 轻拆）
============================================
只做注册/管理逻辑：把 agent 循环的默认监听器挂到 scope 上，并判断
祖先 ctx 链是否已提供某事件的监听器（llm/stream 恰好一个默认注册）。

监听器本体（_prestep_* / _llm_stream_default）仍留在 HermesAgentV3——
它们读写 agent 的 self 状态（记忆编排、压缩阈值、LLM 客户端、取消
事件）并被测试直接调用，此处只接收绑定方法。行为零变化：注册顺序
即洋葱外→内顺序（本 scope 先于祖先链冒泡）。
"""

from __future__ import annotations

from typing import Callable


def upstream_has_listener(scope, event: str) -> bool:
    """祖先 ctx 链上是否已有该事件的监听器（冒泡会命中它们）。"""
    ctx = scope.parent
    while ctx is not None:
        with ctx._lock:
            if ctx._listeners.get(event):
                return True
        ctx = ctx.parent
    return False


def register_default_listeners(
    scope,
    *,
    prestep_memory: Callable,
    prestep_compact: Callable,
    prestep_mode: Callable,
    llm_stream_default: Callable,
) -> None:
    """在 scope 上注册 agent 循环的全部默认监听器。

    注册顺序即洋葱外→内顺序（本 scope 先于祖先链）：
    pre-step: 记忆注入 → 压缩阈值 → 模式指导；llm/stream: 真流式实现。
    """
    scope.on("agent/pre-step", prestep_memory)
    scope.on("agent/pre-step", prestep_compact)
    scope.on("agent/pre-step", prestep_mode)
    # llm/stream 默认监听器：仅当祖先链上无人提供时注册（如某组合把
    # llm_plugin 的默认监听器挂在祖先 ctx），保证恰好一个默认监听器；
    # 循环侧另有"链上无人填 chunks"时的直连兜底
    if not upstream_has_listener(scope, "llm/stream"):
        scope.on("llm/stream", llm_stream_default)
