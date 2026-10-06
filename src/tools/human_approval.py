"""
============================================
人工介入工具
============================================
在执行高风险操作前暂停执行，等待人工批准或修改。

人工审批统一由权限层触发：
- 三层权限求值（permissions.decide）得出 requireApproval 时，
  Registry 抛 InterruptSignal 暂停循环
- CLI/Web 收到 human_approval_request 事件后弹窗确认
- resume_payload（"approve" / "reject:原因" / 自由反馈）恢复执行
- LLM 也可主动调用 request_human_approval 工具请求审批（同样走
  InterruptSignal）
"""

import logging

logger = logging.getLogger("hermes.tools.human_approval")


# ════════════════════════════════════════════════════════════════
# PythonExecutor 入口（tools/request_human_approval.yaml）
# ════════════════════════════════════════════════════════════════

def _execute_approval(action: str, details: str, *, ctx=None) -> str:
    """PythonExecutor 入口。

    根据当前权限模式决定行为：
    - full_access：用户已全局授权，直接放行，不弹窗
    - before_changes / plan：抛 InterruptSignal，暂停等审批
    """
    mode = getattr(ctx, "permission_mode", "before_changes") if ctx else "before_changes"

    if mode == "full_access":
        # P3-6：如实回执——full_access 下不弹窗、没有任何人工确认环节，
        # 不得谎称"已授权"误导模型以为走过了审批。
        return f"已直接放行，未经任何人工确认（full_access 模式不弹窗）：{action}"

    from src.types import InterruptSignal
    raise InterruptSignal({"action": action, "details": details})
