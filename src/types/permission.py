"""
============================================
PermissionDecision —— 权限求值的决策结果
============================================
permissions.py 三层叠加（Layer 3 evaluator → Layer 2 mode → 放行）的
最终决策对象。action 值域三值：allow / deny / requireApproval。

P3-2 公共类型下沉：本定义原居 src/tools/permissions.py，现下沉至
src/types/（agent 与 tools 共同向下依赖），原模块保留 re-export。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# action 值域
Action = Literal["allow", "deny", "requireApproval"]


@dataclass
class PermissionDecision:
    """权限求值的最终决策。"""

    action: Action
    """allow / deny / requireApproval。"""

    reason: str = ""
    """人类可读原因（deny/requireApproval 时用于日志/审批面板）。"""

    @property
    def is_allow(self) -> bool:
        return self.action == "allow"

    @property
    def is_deny(self) -> bool:
        return self.action == "deny"

    @property
    def needs_approval(self) -> bool:
        return self.action == "requireApproval"


__all__ = ["PermissionDecision", "Action"]
