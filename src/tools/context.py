"""
============================================
ToolContext —— 调用级上下文（双通道的「调用级」那半）
============================================
每次工具调用时由 Registry 构造，通过约定尾参 ctx 传给 Python executor 的函数。

双通道原则：
    会话级（per-session 固定值）→ contextvars
        user_id / vfs / depth —— 已在现有代码验证可靠，零改动。
    调用级（per-call 变化值）  → ToolContext 尾参（本文件）
        permission_mode / caller_context / progress_cb

为何不用 contextvars 承载调用级：
    ctx 是 per-call 的（每次调用可能不同的 progress_cb、不同 caller_context），
    生命周期短。contextvar 更适合 per-session 固定值，且 contextvar 的
    set/reset 在高频 per-call 场景下繁琐且易遗漏 reset。

"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


# 权限模式值域（宽松用 str，避免 Literal 锁死，方便未来扩展自定义模式）
PERMISSION_MODE_FULL = "full_access"
PERMISSION_MODE_BEFORE = "before_changes"
PERMISSION_MODE_PLAN = "plan"

DEFAULT_PERMISSION_MODE = PERMISSION_MODE_BEFORE


@dataclass
class ToolContext:
    """调用级上下文，Registry 每次工具调用时构造并注入。

    通过约定尾参 ``ctx: ToolContext`` 传给 PythonExecutor 指向的函数。
    @make_tool / PythonExecutor 自动把 ctx 参数剔除出 LLM schema。

    ShellExecutor 不接收 ctx 参数（它的执行逻辑不依赖它），
    但 Registry 在调用 executor 前用 ctx 做权限决策。
    """

    permission_mode: str = DEFAULT_PERMISSION_MODE
    """当前会话的权限模式。决定 Layer 2 对 destructive 工具的决策。
    full_access / before_changes / plan。默认 before_changes（最安全）。

    子 agent / employee 继承父会话的 mode：
    task 工具的 _execute_task 把 ctx.permission_mode 传给 StreamingSubAgent；
    dispatch 的 employee_worker 启动时把 mode 作为命令行参数传入。"""

    caller_context: str = "main"
    """当前调用方上下文。main / employee / subagent。
    用于 Layer 3 的 when.context_in 规则过滤（如 dispatch 在 employee 禁用）。"""

    allowed_tools: "set[str] | None" = None
    """调用级工具白名单（T8a 作用域化）。None = 不过滤；非 None 时
    resolve_tools 仅保留名字在集合内的工具（按名匹配，含 MCP 工具）。
    waker / wakerflow 的 cfg.tools 白名单落地处——走 ToolContext 天然覆盖
    整个 generator 消费期，取代旧 resolve_tools monkey-patch。"""

    progress_cb: Callable[[str], None] | None = None
    """进度回调。dispatch 工具用它推进度事件给 Registry → writer → 前端。
    工具进度回调（供长任务工具推送进度事件）。"""

    should_cancel: "Callable[[], bool] | None" = None
    """协作式取消探针（worker chat_stop → _cancel_event）。Registry 在
    多工具批量执行的工具之间轮询：置位即跳过剩余调用（合成"已取消"
    结果）；单个阻塞中的工具内部不可中断（shell 有自身超时兜底）。"""
