"""
============================================
权限求值 —— 三层叠加（V3 核心）
============================================
工具的权限决策由三层叠加完成，取代现有散落的硬编码
（_INTERRUPT_TOOL_NAMES / BLOCKED / if name=="task" / 工具内 if not shell_enabled）。

求值顺序（Layer 3 优先 → Layer 2 → 放行）：

    1. Layer 3 evaluator（如有）→ 产出 SideEffectsOverride
       force_deny=True     → 直接 deny（任何模式，硬底线）
       destructive 覆盖值  → 替换 Layer 1 的静态标签

    2. Layer 2 mode 基于修正后的 destructive：
       full_access          → 放行
       before_changes       → destructive 则 requireApproval，否则放行
       plan                 → destructive 则 deny，否则放行

    3. 两层都放行 → executor.execute()

决策表（destructive 是唯一参与 mode 决策的字段）：

    | 模式             | destructive=True  | destructive=False |
    |------------------|-------------------|-------------------|
    | full_access      | 放行              | 放行              |
    | before_changes   | requireApproval   | 放行              |
    | plan             | deny              | 放行              |

Layer 3 修正器可额外置 force_deny（任何模式拒绝）/ force_approval
（任何模式审批，只顶掉 full_access 的放行——自进化护栏：写自己仓库
的命令即使在 full_access 也要人工点一次）。

P3-2 公共类型下沉：PermissionDecision（决策结果类型）已搬至
src/types/permission.py，此处 re-export 兼容既有 import 路径；
三层求值逻辑（evaluate_layer3/evaluate_layer2/decide）仍在本模块。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from src.tools.context import (
    PERMISSION_MODE_BEFORE,
    PERMISSION_MODE_FULL,
    PERMISSION_MODE_PLAN,
)
from src.tools.schema import SideEffectsOverride, ToolSpec
from src.types.permission import Action as Action
from src.types.permission import PermissionDecision as PermissionDecision

if TYPE_CHECKING:
    from src.tools.context import ToolContext

__all__ = ["PermissionDecision", "Action", "evaluate_layer3", "evaluate_layer2", "decide"]


# ════════════════════════════════════════════════════════════════
# Layer 3：动态修正 destructive 标签
# ════════════════════════════════════════════════════════════════

def evaluate_layer3(
    spec: ToolSpec,
    args: dict[str, Any],
    ctx: "ToolContext",
) -> SideEffectsOverride:
    """运行 Layer 3 的动态修正器，返回 SideEffectsOverride。

    Layer 3 有两种形态（互斥，evaluator 优先）：
    1. se_evaluator（Python 函数）：如 run_shell 的 classify_command。
    2. se_overrides（regex 列表）：如 web_fetch 的 SSRF 内网检测。

    返回的 override 可能：
    - 修正 destructive 标签（如 ls 命令降级为 False）
    - force_deny=True（硬底线，如 fork bomb、SSRF 内网）

    没有 Layer 3 → 返回空 override（不改任何东西）。
    """
    # Python evaluator 优先
    if spec.se_evaluator is not None:
        return spec.se_evaluator(args, ctx)

    # regex 版
    for rule in spec.se_overrides:
        value = args.get(rule.field)
        if value is None or not isinstance(value, str):
            continue
        for pattern in rule.patterns:
            if re.search(pattern, value):
                return rule.override

    return SideEffectsOverride()


# ════════════════════════════════════════════════════════════════
# Layer 2：mode 决策
# ════════════════════════════════════════════════════════════════

def evaluate_layer2(
    mode: str,
    destructive: bool,
) -> PermissionDecision:
    """基于 mode + 修正后的 destructive 标签做决策。

    唯一参与决策的字段是 destructive。writes_state / network_access /
    spawns_process 不参与（plan 允许搜索和子 agent）。
    """
    if mode == PERMISSION_MODE_FULL:
        # 完全访问：全放行（force_deny 已在 Layer 3 处理）
        return PermissionDecision(action="allow", reason="完全访问模式")

    if mode == PERMISSION_MODE_BEFORE:
        if destructive:
            return PermissionDecision(
                action="requireApproval",
                reason="变更前访问模式：destructive 工具需审批",
            )
        return PermissionDecision(action="allow", reason="变更前访问模式：非破坏性工具放行")

    if mode == PERMISSION_MODE_PLAN:
        if destructive:
            return PermissionDecision(
                action="deny",
                reason="计划模式：destructive 工具被禁止",
            )
        return PermissionDecision(action="allow", reason="计划模式：非破坏性工具放行")

    # 未知 mode：保守视为 before_changes（最安全）
    if destructive:
        return PermissionDecision(
            action="requireApproval",
            reason=f"未知模式 '{mode}'，保守要求审批",
        )
    return PermissionDecision(action="allow", reason=f"未知模式 '{mode}'，非破坏性工具放行")


# ════════════════════════════════════════════════════════════════
# 三层叠加：最终决策入口
# ════════════════════════════════════════════════════════════════

def decide(
    spec: ToolSpec,
    args: dict[str, Any],
    ctx: "ToolContext",
) -> PermissionDecision:
    """三层叠加求值。Registry.execute() 的核心调用。

    求值顺序：
    1. Layer 3 → 修正 destructive / force_deny / force_approval
    2. force_deny=True → 直接 deny（硬底线）
    3. Layer 2 mode 基于修正后的 destructive 决策
    4. force_approval=True 且 Layer 2 判了 allow → 升级为 requireApproval
       （只顶掉 full_access 的放行；plan 的 deny / before_changes 的
       requireApproval 本就比它严，不动）
    """
    # Layer 3
    override = evaluate_layer3(spec, args, ctx)

    # 硬底线优先：force_deny 任何模式都拒绝
    if override.force_deny:
        return PermissionDecision(
            action="deny",
            reason="硬底线拦截（force_deny）",
        )

    # 修正后的 destructive（override.destructive 为 None 时沿用静态值；
    # force_approval 蕴含 destructive——否则 plan 会放行本该拦的调用）
    effective_destructive = (
        override.destructive
        if override.destructive is not None
        else spec.side_effects.destructive
    )
    if override.force_approval:
        effective_destructive = True

    # Layer 2
    decision = evaluate_layer2(ctx.permission_mode, effective_destructive)

    # 强制审批：仅升级 allow（full_access 的放行），不放宽任何更严决策
    if override.force_approval and decision.is_allow:
        return PermissionDecision(
            action="requireApproval",
            reason="强制审批（force_approval）：该操作影响 agent 自身，需人工确认",
        )
    return decision
