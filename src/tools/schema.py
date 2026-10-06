"""
============================================
ToolSpec —— 声明式工具 schema（V3 三层架构）
============================================
声明式工具规格（一个 dataclass 承载 schema + 执行元信息）：

    ┌─ 面向 LLM（to_openai() 直接吐 OpenAI function JSON）─────┐
    │  name / description / parameters                          │
    └───────────────────────────────────────────────────────────┘
    ┌─ 面向运行时（Registry 看 side_effects + executor 做决策）─┐
    │  executor / side_effects / se_evaluator / se_overrides    │
    └───────────────────────────────────────────────────────────┘

三层权限架构：
    Layer 1  工具静态定义（src/types/tool_spec.py）
             side_effects 是行为描述（destructive/writes_state/...），
             不是权限决策。
    Layer 2  权限模式（会话级，用户随时切换）
             full_access / before_changes / plan
             唯一决策依据是 destructive 一列。
    Layer 3  细粒度规则修正器（工具可选）
             按本次参数动态修正 destructive 标签 / force_deny 硬底线。

P3-2 公共类型下沉：定义（ToolSpec + SideEffects 家族）已搬至
src/types/tool_spec.py（本模块原是定义家）。此处仅 re-export，
兼容既有 import 路径（from src.tools.schema import ToolSpec）零改动可用。
"""

from src.types.tool_spec import SideEffects as SideEffects
from src.types.tool_spec import SideEffectsEvaluator as SideEffectsEvaluator
from src.types.tool_spec import SideEffectsOverride as SideEffectsOverride
from src.types.tool_spec import SideEffectsRegexOverride as SideEffectsRegexOverride
from src.types.tool_spec import ToolSource as ToolSource
from src.types.tool_spec import ToolSpec as ToolSpec

__all__ = [
    "SideEffects",
    "SideEffectsEvaluator",
    "SideEffectsOverride",
    "SideEffectsRegexOverride",
    "ToolSource",
    "ToolSpec",
]
