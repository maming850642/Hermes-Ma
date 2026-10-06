"""
============================================
resolve_tools —— 动态工具列表组装
============================================
「静态为主，动态为辅」架构的动态层。

工具定义是静态的（tools/*.yaml 或 McpToolFactory 动态生成），
但「当前哪些工具对 LLM 可见」是运行时按上下文组装的。

三层过滤：
    1. config_guard：requires_config 不满足 → 隐藏（LLM 看不到未启用的工具）
    2. caller_context：blocked_in 含当前上下文 → 隐藏（employee 禁用 dispatch）
    3. MCP 动态拉取：连上的 server 的工具动态加入
尾部追加两层（T8a）：
    4. workspace 模式门禁（仅对话时只留 chat-only）
    5. blocked_in / allowed_tools 作用域过滤：
       - spec.blocked_in 命中 ctx.caller_context → 隐藏
       - ctx.allowed_tools 非 None → 仅保留白名单内的（waker/flow 的 cfg.tools）

调用方只需调一次 resolve_tools(ctx, settings) 拿到当前可用工具列表。
bind_tools 永远绑 resolve_tools() 的返回值，不绑「全部工具」。

"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.tools.loader import load_builtin_tools

if TYPE_CHECKING:
    from src.tools.context import ToolContext
    from src.tools.schema import ToolSpec

logger = logging.getLogger("hermes.tools.resolve")


def resolve_tools(
    ctx: "ToolContext",
    settings=None,
    include_mcp: bool = True,
) -> list["ToolSpec"]:
    """组装当前上下文的可用工具列表。

    Args:
        ctx: 调用级上下文（含 caller_context / permission_mode）。
        settings: config.Settings 对象（None 则 get_settings()）。
        include_mcp: 是否拉取 MCP 工具（CLI / Web 主 agent=True，
                     employee/subagent 可选 False）。

    Returns:
        当前可用的 ToolSpec 列表。已按 config_guard + workspace 模式 +
        blocked_in（caller_context）+ allowed_tools（调用级白名单）过滤。
    """
    if settings is None:
        from config import get_settings
        settings = get_settings()

    builtin = load_builtin_tools()
    visible: list[ToolSpec] = []

    # ── 内置工具过滤 ──
    for spec in builtin.values():
        # ① config_guard：配置门控（如 shell_enabled=False → run_shell 隐藏）
        if spec.config_guard:
            if not getattr(settings, spec.config_guard, False):
                continue

        visible.append(spec)

    # ── MCP 动态拉取 ──
    if include_mcp:
        mcp_tools = _fetch_mcp_tools()
        visible.extend(mcp_tools)

    # ── ④ Workspace 模式过滤（T5）──
    visible = _apply_workspace_filter(visible, settings)

    # ── ⑤ caller_context 过滤（T8a：落地 blocked_in，此前只有 docstring 声称）──
    # spec.blocked_in 声明"本工具禁止出现的调用方上下文"（如 dispatch 禁入
    # employee，防递归分派）。MCP 工具默认 blocked_in=[] 不受影响。
    before = len(visible)
    visible = [
        s for s in visible
        if not (s.blocked_in and ctx.caller_context in s.blocked_in)
    ]
    if len(visible) != before:
        logger.info(
            f"blocked_in 过滤: caller_context={ctx.caller_context!r}, "
            f"可见工具 {before} → {len(visible)}"
        )

    # ── ⑥ 调用级白名单（T8a：waker/flow 的 cfg.tools，取代 monkey-patch）──
    if ctx.allowed_tools is not None:
        before = len(visible)
        visible = [s for s in visible if s.name in ctx.allowed_tools]
        logger.info(
            f"allowed_tools 白名单过滤: 可见工具 {before} → {len(visible)} "
            f"(whitelist={sorted(ctx.allowed_tools)})"
        )

    return visible


def _apply_workspace_filter(
    visible: list["ToolSpec"], settings=None
) -> list["ToolSpec"]:
    """T5 双模式门禁：仅对话（或从未配置）时只保留 chat-only 工具。

    判定优先级：
        1. 进程内无 WorkspaceService（独立进程/CLI/未 boot）→ 安全回退：
           视为未配置，按 settings 的 chat-only 集合过滤（MCP 工具一并消失）
        2. 有 service 且 status 为 None（从未配置）或 mode="none"（仅对话）
           → 只保留 chat_only_tools() 内的 spec
        3. mode local/upload（已挂载）→ 全量放行

    生效链路：worker/waker/flow 的 agent 每 turn 都调
    _resolve_and_bind_tools → resolve_tools，故挂载切换下一 turn 即生效。
    """
    from src.workspace import state as workspace_state

    svc = workspace_state.get_service()
    if svc is not None:
        st = workspace_state.current_status()
        if st is not None and st.mode not in ("none",):
            return visible  # 已挂载（local/upload）→ 全量
        allowed = svc.chat_only_tools()
    else:
        # 未 boot workspace 插件：独立进程默认安全（chat-only 集合）
        from src.workspace.service import chat_only_tools_from_settings
        allowed = chat_only_tools_from_settings(settings)

    filtered = [s for s in visible if _name_in_allowlist(s.name, allowed)]
    logger.info(
        "workspace 模式过滤：chat-only（未配置/仅对话），"
        f"可见工具 {len(visible)} → {len(filtered)}"
    )
    return filtered


def _name_in_allowlist(name: str, allowed: set[str]) -> bool:
    """chat-only 白名单：精确匹配，或 `mcp__*` 这类前缀通配。

    前缀必须非空（裸 `*` 不匹配全部），避免配置笔误把工具面打穿。
    """
    if name in allowed:
        return True
    for pat in allowed:
        if pat.endswith("*") and len(pat) > 1:
            prefix = pat[:-1]
            if prefix and name.startswith(prefix):
                return True
    return False


def _fetch_mcp_tools() -> list["ToolSpec"]:
    """拉取已连接的 MCP server 的工具（动态生成 ToolSpec）。

    延迟 import 避免循环依赖。fastmcp 未安装时静默降级为空列表。
    """
    try:
        from src.mcp.tool_factory import generate_mcp_tool_specs
        return generate_mcp_tool_specs()
    except ImportError:
        # fastmcp 未安装
        return []
    except Exception as e:
        logger.warning(f"加载 MCP 工具失败，仅使用内置工具: {e}")
        return []
