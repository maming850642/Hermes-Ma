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
    Layer 1  工具静态定义（本文件）
             side_effects 是行为描述（destructive/writes_state/...），
             不是权限决策。
    Layer 2  权限模式（会话级，用户随时切换）
             full_access / before_changes / plan
             唯一决策依据是 destructive 一列。
    Layer 3  细粒度规则修正器（工具可选）
             按本次参数动态修正 destructive 标签 / force_deny 硬底线。

P3-2 公共类型下沉：本定义原居 src/tools/schema.py，现下沉至
src/types/（agent 与 tools 共同向下依赖），原模块保留 re-export。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from src.tools.context import ToolContext
    from src.tools.executor_base import ToolExecutor


# ════════════════════════════════════════════════════════════════
# Layer 1：行为描述
# ════════════════════════════════════════════════════════════════

@dataclass
class SideEffects:
    """工具的行为画像。

    重要：这是「行为描述」，不是「权限决策」。destructive 表示"我会改数据"，
    不表示"我要审批"——审不审批由 Layer 2 的权限模式基于这些标签决定。

    只有 ``destructive`` 参与 Layer 2 的 mode 决策；
    writes_state / network_access / spawns_process 不参与，仅供日志/审计/UI。
    """

    destructive: bool = False
    """会修改/删除用户数据（写文件、删文件、shell 写操作）。
    唯一参与 mode 决策的字段：before_changes 审批、plan 拒绝。"""

    writes_state: bool = False
    """会改 agent 内部 state（todos / vfs / 记忆）。
    不参与 mode 决策——agent 的内部认知活动不该让用户审批。"""

    network_access: bool = False
    """访问外部网络（web_fetch / web_search / MCP 远程）。
    不参与 mode 决策（plan 允许搜索/抓取）。"""

    spawns_process: bool = False
    """spawn 子进程/子 agent（run_shell / task / dispatch）。
    不参与 mode 决策（plan 允许规划辅助）。"""


# ════════════════════════════════════════════════════════════════
# Layer 3：动态修正器
# ════════════════════════════════════════════════════════════════

@dataclass
class SideEffectsOverride:
    """Layer 3 evaluator 的返回值，修正 Layer 1 的静态标签。

    使用场景：run_shell 的 ``destructive`` 静态标签是 True（能力上限——能跑 rm），
    但本次调用可能是 ``ls``（实际无破坏）。Layer 3 evaluator 按 args 动态修正。
    """

    destructive: bool | None = None
    """覆盖 Layer 1 的 destructive 标签。None = 不覆盖，沿用静态值。"""

    force_deny: bool = False
    """硬底线：任何模式（含 full_access）都拒绝。
    用于 fork bomb、SSRF 内网、黑名单命令等不可降级的安全拦截。"""

    force_approval: bool = False
    """强制审批：任何模式（含 full_access）都 requireApproval。
    自进化护栏（2026-09）：写自己仓库的命令命中路径检测时置位——
    full_access 不再是"agent 静默改写自身源码"的通道。
    蕴含 destructive=True（before_changes 审批 / plan 拒绝语义不变）。"""


# evaluator 协议：纯函数，接收 (args, ctx)，返回 override
SideEffectsEvaluator = Callable[[dict[str, Any], "ToolContext"], SideEffectsOverride]


@dataclass
class SideEffectsRegexOverride:
    """Layer 3 的 regex 版（轻量级，不需写 Python 函数）。

    match_field 指向参数名，match_patterns 是 regex 列表。
    任一 pattern 命中 → 应用 override。
    """
    field: str
    """要检查的参数名（如 "command"、"url"）。"""

    patterns: list[str]
    """regex 模式列表（任一命中即触发）。"""

    override: SideEffectsOverride
    """命中后应用的覆盖。"""

    reason: str = ""
    """人类可读原因（用于日志/审批面板）。"""


# ════════════════════════════════════════════════════════════════
# Layer 1：ToolSpec —— 唯一的工具描述对象
# ════════════════════════════════════════════════════════════════

ToolSource = str  # "yaml" | "mcp"（宽松用 str，避免 Literal 锁死扩展）


@dataclass
class ToolSpec:
    """工具规格。一个 dataclass 承载 schema + 执行元信息。

    全部 flag 都是中性行为描述；权限决策在 permissions.py 的三层叠加里完成。
    """

    # ─── 面向 LLM（to_openai() 直接吐 OpenAI function JSON）───
    name: str
    """工具唯一名。MCP 工具用 mcp__<server>__<tool> 前缀避免冲突。"""

    description: str
    """注入 LLM 的 function description。"""

    parameters: dict[str, Any]
    """JSON Schema dict（OpenAI function parameters 格式）。
    由 YAML 加载器填入，或由 MCP inputSchema 直接填入。不经 Pydantic 转换。"""

    # ─── 面向运行时：执行 ───
    executor: "ToolExecutor"
    """执行器实例（ShellExecutor / PythonExecutor / McpExecutor）。"""

    # ─── Layer 1：行为描述 ───
    side_effects: SideEffects = field(default_factory=SideEffects)

    # ─── Layer 3：动态修正器（可选，大多数工具为空）───
    se_evaluator: SideEffectsEvaluator | None = None
    """Python 函数版修正器。签名 (args, ctx) -> SideEffectsOverride。
    典型用法：run_shell 的 classify_command 包装。"""

    se_overrides: list[SideEffectsRegexOverride] = field(default_factory=list)
    """regex 版修正器列表。典型用法：web_fetch 的 SSRF 内网检测。"""

    # ─── 元信息 ───
    source: ToolSource = "yaml"
    """工具来源：yaml（内置）| mcp（动态发现）。"""

    config_guard: str | None = None
    """配置门控：需 getattr(settings, this_key, False) 为 True 才启用。
    None = 无门槛。Registry 在 bind_tools 前过滤。
    取代工具函数内部 if not getattr(settings, "shell_enabled") 检查。"""

    blocked_in: list[str] = field(default_factory=list)
    """禁止出现的 caller_context 列表（如 ["employee"]——employee 禁用
    dispatch 这类递归分派工具）。resolve_tools 尾部过滤：
    ctx.caller_context 命中列表 → 该工具对本上下文隐藏。默认 [] 不限制。"""

    # ─── OpenAI 适配 ───
    def to_openai(self) -> dict[str, Any]:
        """转成 OpenAI chat completion 的 tools 参数项。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


__all__ = [
    "SideEffects",
    "SideEffectsEvaluator",
    "SideEffectsOverride",
    "SideEffectsRegexOverride",
    "ToolSource",
    "ToolSpec",
]
