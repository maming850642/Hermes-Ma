"""
============================================
InterruptSignal —— 异常式 HITL 的暂停信号
============================================
权限决策要求审批时抛出（V3 的暂停信号）。

抛出时机：Registry 的 Layer 2/3 求值得出 requireApproval 时。
捕获者：ReAct 循环（stream_invoke）——捕获后存快照、yield 审批事件、暂停。

resume 时重新求值权限（不是无脑恢复快照）：
如果用户趁暂停切换了 mode 到 full_access，重新求值后 action=allow，
直接放行 executor —— 用户切模式就是为了放行。

配套的快照/存储（InterruptSnapshot / InterruptStore）仍在 src/agent/hitl.py
——它们依赖 session_log 等 agent 运行时设施，不是跨包公共类型。

P3-2 公共类型下沉：本定义原居 src/agent/hitl.py，现下沉至
src/types/（agent 与 tools 共同向下依赖），原模块保留 re-export。
"""

from __future__ import annotations

from typing import Any

# ── InterruptSignal.payload 约定键 ──
# 批量工具执行被审批中断时，已完成的前序/兄弟工具结果经此键挂在 signal
# payload 上带出（P1-3，只收敛键定义不改行为）：串行路径由
# registry_v3._execute_serial 写入，并发路径由 _execute_concurrent 写入；
# agent_v3._repair_interrupt_pairing 读取，用于补齐 assistant(tool_calls)
# 与 tool 消息的一一配对（悬空 tool_calls 会被严格 OpenAI 兼容端点 400）。
COMPLETED_TOOL_MESSAGES_KEY = "completed_tool_messages"


class InterruptSignal(Exception):
    """权限决策要求审批时抛出（V3 的暂停信号）。

    抛出时机：Registry 的 Layer 2/3 求值得出 requireApproval 时。
    捕获者：ReAct 循环（stream_invoke）——捕获后存快照、yield 审批事件、暂停。

    resume 时重新求值权限（不是无脑恢复快照）：
    如果用户趁暂停切换了 mode 到 full_access，重新求值后 action=allow，
    直接放行 executor —— 用户切模式就是为了放行。
    """

    def __init__(self, payload: dict[str, Any]):
        # payload 约定：{"action": str, "details": str, "tool_call_id": str}
        self.payload = payload
        super().__init__(f"InterruptSignal: {payload.get('action', '')}")


__all__ = ["InterruptSignal", "COMPLETED_TOOL_MESSAGES_KEY"]
