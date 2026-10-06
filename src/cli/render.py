"""
============================================
Hermes Rich CLI —— 展示层（show_* / 面板构建器 / 历史渲染 / 会话选择器）
============================================
P2 拆包自 src/cli.py（函数体原样搬出，行为零变化）：
- 启动横幅 / 帮助 / 技能 / 记忆 / 工具等 Rich 渲染（show_*）
- 历史会话选择器（_show_and_pick_session）与历史消息面板（show_session_history）
- chat 流式渲染用的面板构建器（_build_memory_search_panel / _build_tool_panel）
- 行数估算与工具参数格式化（_estimate_lines / _format_tool_args）

拆包兼容层（零行为变化）：console 在此定义（单一真源），经
src/cli/__init__.py re-export 为包属性 src.cli.console。拆包前 console /
Prompt / list_sessions 等是 cli 单模块全局，测试可整体替换 `src.cli.X`；
因此各函数在使用处一律**调用期** `from src.cli import ...` 再绑定，保证
替换后的绑定对全部渲染路径生效（与拆包前单模块全局语义一致）。
"""

import logging
import sys
from typing import TYPE_CHECKING, Optional

from rich import box
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import get_settings
from src.agent.multimodal import extract_text
from src.memory import MemoryManager

if TYPE_CHECKING:
    # 仅供 show_tools 的字符串注解引用（运行期避免展示层拖起 agent 全链）
    from src.agent import HermesAgentV3

logger = logging.getLogger("hermes.cli")

# Rich Console 实例（统一输出到 stdout，避免与 Prompt.ask 顺序混乱）
console = Console(file=sys.stdout)


def _show_and_pick_session(user_id: str) -> Optional[str]:
    """
    显示用户的历史会话列表（分页），让用户输入编号选择恢复。

    交互（2026-06-23 分页改造）：
        - 输入数字：选择该页内对应编号的会话
        - 输入 n：下一页
        - 输入 p：上一页
        - 输入 q 或回车：退出

    Returns:
        str: 选中的 session_id，取消返回 None
    """
    from src.cli import Prompt, console, list_sessions  # 调用期再绑定（拆包兼容层，见模块 docstring）
    sessions = list_sessions(user_id)

    if not sessions:
        console.print(f"\n  [dim]用户 [cyan]{user_id}[/cyan] 暂无历史会话[/dim]\n")
        return None

    PAGE_SIZE = 20
    total = len(sessions)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = 0  # 0-indexed

    while True:
        start = page * PAGE_SIZE
        end = min(start + PAGE_SIZE, total)
        page_sessions = sessions[start:end]

        console.print(
            f"\n📋 [cyan]{user_id}[/cyan] 的历史会话 "
            f"（第 {page + 1}/{total_pages} 页，共 {total} 个）：\n"
        )

        table = Table(show_header=True, border_style="dim")
        table.add_column("#", style="dim", width=3)
        # 2026-06-16: 名称列优先显示自定义名，空时退到 session_id
        table.add_column("名称", style="cyan", width=20)
        table.add_column("会话 ID", style="dim", width=18)
        table.add_column("更新时间", style="white", width=19)
        table.add_column("消息数", style="white", width=6)
        table.add_column("预览", style="dim", width=35)

        for i, s in enumerate(page_sessions, 1):
            sid = s["session_id"]
            name = escape(s.get("name", "")[:20]) if s.get("name") else "—"
            updated = s["updated_at"][:19].replace("T", " ") if s["updated_at"] else "—"
            count = str(s["message_count"])
            preview = escape(s["preview"][:35]) if s["preview"] else "—"
            table.add_row(str(i), name, sid, updated, count, preview)

        console.print(table)

        # 翻页提示：只显示当前可用的操作
        hints = ["输入编号恢复会话"]
        if page < total_pages - 1:
            hints.append("[yellow]n[/yellow]=下一页")
        if page > 0:
            hints.append("[yellow]p[/yellow]=上一页")
        hints.append("[yellow]q[/yellow]/回车=退出")
        prompt_str = f"\n  {' · '.join(hints)}"

        try:
            choice = Prompt.ask(prompt_str, default="q")
        except EOFError:
            # P2-6：菜单输入流尽（管道喂入 /resume）→ 返回 None，让主循环
            # 的输入 EOF 分支干净退出（此前异常冲出 main，会话收尾被跳过）
            console.print("\n  [dim]（输入流结束）[/dim]\n")
            return None
        except KeyboardInterrupt:
            # P2-6：Ctrl+C = 取消选择，回主提示符
            console.print("\n  [dim]已取消[/dim]\n")
            return None
        choice = choice.strip().lower()

        if not choice or choice == "q":
            console.print("  [dim]已取消[/dim]\n")
            return None
        elif choice == "n" and page < total_pages - 1:
            page += 1
            continue
        elif choice == "p" and page > 0:
            page -= 1
            continue
        elif choice in ("n", "p"):
            # n/p 输入了但已到边界
            console.print(f"  [dim]已到{'最后' if choice == 'n' else '第一'}页[/dim]")
            continue
        else:
            try:
                idx = int(choice)
                if 1 <= idx <= len(page_sessions):
                    return page_sessions[idx - 1]["session_id"]
                else:
                    console.print(f"  [red]编号超出范围（1-{len(page_sessions)}）[/red]\n")
                    continue
            except ValueError:
                console.print(f"  [red]无效输入：{escape(choice)}（编号 / n / p / q）[/red]\n")
                continue


# ============================================
# CLI 界面组件
# ============================================
def show_logo():
    """显示 Hermes Logo"""
    from src.cli import console  # 调用期再绑定（拆包兼容层，见模块 docstring）
    logo = Text()
    logo.append("⚡ ", style="bold yellow")
    logo.append("Hermes—Ma", style="bold cyan")
    logo.append(" - 多用户 AI Agent 平台", style="dim")
    logo.append("\n   记忆隔离 · 长期记忆 · 工具调用", style="dim")

    console.print(Panel(logo, border_style="cyan", padding=(1, 2)))
    console.print()


def show_help():
    """显示帮助信息"""
    from src.cli import console  # 调用期再绑定（拆包兼容层，见模块 docstring）
    table = Table(title="📖 可用命令", show_header=False, border_style="dim")
    table.add_column("命令", style="cyan", width=20)
    table.add_column("说明", style="white")

    table.add_row("/project", "查看项目空间（列表 + 当前激活）")
    table.add_row("/project switch <slug>", "切换项目空间（与 Web 共享激活状态）")
    table.add_row("/project new <名称>", "新建托管项目空间并切换")
    table.add_row("/project off", "退出项目空间（回到收件箱免项目模式）")
    table.add_row("/events [条数]", "查看当前会话的事件流（事件溯源轨迹，默认 20 条）")
    table.add_row("/fork [事件ID]", "把当前会话复制成分支（可按事件 id 截断）并切换过去")
    table.add_row("/think [on|off]", "开/关模型思考模式（推理预览始终显示：流式滚动 · r 键折叠/展开）")
    table.add_row("/reasoning", "查看最近一轮完整推理（推理结束后折叠行的回看入口）")
    table.add_row("/waker [list|run <名>]", "数字员工：查看列表 / 立即执行一轮任务")
    table.add_row("/flow [list|run <名>]", "WakerFlow：查看列表 / 运行一次工作流")
    table.add_row("/mode", "查看/切换权限模式（full_access / before_changes / plan）")
    table.add_row("/model [id]", "查看/热切换模型（下一轮对话生效，无需重启）")
    table.add_row("/resume", "恢复历史会话（显示列表选择）")
    table.add_row("/resume <会话ID>", "直接恢复指定历史会话")
    table.add_row("/resume -a", "恢复会话并显示全部历史消息（不限 3 条）")
    table.add_row("/rename <名称>", "重命名当前会话")
    table.add_row("/skill", "列出所有可用技能（技能由 AI 自主调用 use_skill 加载）")
    table.add_row("/mcp", "管理 MCP 服务器（启用/禁用/删除，连接外部工具集）")
    table.add_row("/memory", "查看当前用户的所有长期记忆")
    table.add_row("/clear", "清除当前用户的所有长期记忆")
    table.add_row("/tools", "列出 Agent 可用的所有工具")
    table.add_row("/compact", "手动压缩对话历史（早期消息转为摘要）")
    table.add_row("/reset", "重置当前会话（清空对话历史，开启新会话）")
    table.add_row("/save", "手动保存当前会话")
    table.add_row("/help", "显示此帮助信息")
    table.add_row("/exit", "退出 Hermes（可选择是否生成本次会话总结）")

    console.print(table)
    console.print("[dim]💡 其他输入将作为消息发送给 AI 助手 · Ctrl+C 中断当前响应[/dim]")
    console.print()


def show_skills():
    """
    列出所有可用技能（只读）。

    技能由 LLM 自主调用 use_skill 工具加载，无需手动激活。
    展示：技能名、来源标记、描述、附属资源数。
    """
    from src.cli import console  # 调用期再绑定（拆包兼容层，见模块 docstring）
    from src.skills import get_registry

    registry = get_registry()
    skills = registry.list_all()

    console.print("\n🎯 [bold]可用技能列表[/bold]")

    if not skills:
        console.print("  [dim]暂无可用技能。在 ./skills/ 下为每个技能创建子文件夹，入口 md 命名为 SKILL.md 即可。[/dim]\n")
        return

    table = Table(show_header=True, border_style="dim")
    table.add_column("来源", width=4)
    table.add_column("技能名称", style="cyan", no_wrap=True)
    table.add_column("资源", style="dim", width=6, justify="right")
    table.add_column("描述", style="white")

    for s in skills:
        res_count = str(len(s.resources)) if s.resources else "—"
        table.add_row(s.source_icon, escape(s.name), res_count, escape(s.description[:80]))

    console.print(table)
    console.print(f"   [dim]共 {len(skills)} 个技能 · 📁项目 🏠用户 🔗外部 · 技能由 AI 自主调用 use_skill 加载[/dim]\n")


def show_memory(memory_manager: MemoryManager, user_id: str):
    """显示用户的所有长期记忆"""
    from src.cli import console  # 调用期再绑定（拆包兼容层，见模块 docstring）
    console.print(f"\n📋 [cyan]{user_id}[/cyan] 的长期记忆：")

    memories = memory_manager.get_all(user_id)

    if not memories:
        console.print("   [dim]暂无长期记忆[/dim]\n")
        return

    for i, mem in enumerate(memories):
        memory_text = escape(mem.get("memory", "（无内容）"))
        memory_id = escape(mem.get("id", "unknown"))
        console.print(f"   {i + 1}. {memory_text} [dim](id: {memory_id[:8]}...)[/dim]")

    console.print(f"\n   [dim]共 {len(memories)} 条记忆[/dim]\n")


def clear_memory(memory_manager: MemoryManager, user_id: str):
    """清除当前项目的长期记忆（带确认；全局共享记忆保留）"""
    from src.cli import Prompt, console  # 调用期再绑定（拆包兼容层，见模块 docstring）
    console.print(f"\n⚠️  [yellow]确定要清除当前项目的记忆吗？全局共享记忆将保留。此操作不可逆！[/yellow]")

    confirm = Prompt.ask("   请输入 yes 确认", default="no")

    if confirm.lower() == "yes":
        success = memory_manager.delete_all(user_id)
        if success:
            console.print("   ✅ [green]当前项目记忆已清除（全局记忆保留）[/green]\n")
        else:
            console.print("   ❌ [red]清除失败，请查看日志[/red]\n")
    else:
        console.print("   [dim]已取消[/dim]\n")


def describe_workspace() -> "tuple[str, str]":
    """当前工作区状态描述（启动横幅 / /tools 头部用）。

    Returns:
        (mode_desc, detail)：
        - 已挂载（local/upload）：("已挂载(local)", <挂载路径>)
        - 纯对话 / 未配置：("纯对话模式(chat-only)", "") / ("未配置(chat-only 回退)", "")

    F3 背景：CLI 不 boot workspace 服务时 resolve_tools 安全回退 chat-only，
    横幅必须如实展示，避免"看得到全量 /tools、实际只有 chat-only"的误导。
    """
    try:
        from src.workspace import state as workspace_state
        svc = workspace_state.get_service()
        if svc is None:
            return ("未配置（chat-only 回退）", "")
        st = workspace_state.current_status()
        if st is None:
            return ("未配置（chat-only）", "")
        if st.mode == "none":
            return ("纯对话模式（chat-only）", "")
        path = getattr(st, "path", "") or ""
        return (f"已挂载（{st.mode}）", path)
    except Exception:
        return ("未配置（chat-only 回退）", "")


def show_tools(agent: "HermesAgentV3 | None" = None):
    """显示 Agent 当前生效的工具列表。

    F3：打印 resolve 后的生效集（agent 已绑定进 registry 的工具），
    而非全量内置清单——两者在 chat-only 回退 / shell 未启用 / 白名单
    作用域下并不相同，展示全量会误导。
    """
    from src.cli import console  # 调用期再绑定（拆包兼容层，见模块 docstring）
    console.print("\n🔧 [bold]Agent 生效工具列表[/bold]")

    specs = None
    if agent is not None and getattr(agent, "registry", None) is not None:
        try:
            specs = agent.registry.bound_specs()
        except Exception:
            specs = None
    if not specs:
        # agent 未绑定（会话未开始）：按当前配置 resolve 一份
        from src.tools.context import ToolContext
        from src.tools.resolve import resolve_tools
        specs = resolve_tools(
            ToolContext(caller_context="main"), get_settings(), include_mcp=True,
        )

    mode_desc, detail = describe_workspace()
    console.print(f"   [dim]工作区: {mode_desc}" + (f" | {detail}" if detail else "") + "[/dim]")

    table = Table(show_header=True, border_style="dim")
    table.add_column("工具名称", style="cyan", width=22)
    table.add_column("说明", style="white")

    for t in specs:
        name = escape(t.name)
        desc = escape(t.description.split("\n")[0] if t.description else "（无描述）")
        table.add_row(name, desc)

    console.print(table)
    console.print(f"   [dim]共 {len(specs)} 个生效工具（按当前配置与工作区模式 resolve），Agent 会在需要时自动调用[/dim]\n")


def show_session_history(messages: list, max_count: int = 3, show_all: bool = False):
    """
    登录时展示最近的历史消息面板。

    Args:
        messages: 消息列表（OpenAI dict 格式）
        max_count: 最多显示的消息条数（默认 3 条，即 1.5 轮）
        show_all: 2026-06-16 新增，True 时显示全部历史消息
    """
    from src.cli import console  # 调用期再绑定（拆包兼容层，见模块 docstring）
    if not messages:
        return

    total = len(messages)
    # 2026-06-16: show_all=True 时展示全部消息
    if show_all:
        recent = messages
        console.print(f"  📂 全部 {total} 条历史消息：\n")
    else:
        recent = messages[-max_count:]
        if total > max_count:
            console.print(f"  📂 共 {total} 条历史消息，显示最近 {len(recent)} 条（输入 [yellow]/resume -a[/yellow] 查看全部）：\n")
        else:
            console.print(f"  📂 历史消息：\n")

    for msg in recent:
        role = msg.get("role", "") if isinstance(msg, dict) else ""
        if role == "user":
            # 多模态 content 可能是 list，提取纯文本展示
            content = msg.get("content", "")
            _display = extract_text(content) if not isinstance(content, str) else content
            console.print(Panel(
                _display,
                title=f"👤 user",
                border_style="blue",
                padding=(0, 1),
                expand=False,
            ))
        elif role == "assistant":
            display = msg.get("content") or "（工具调用）"
            if isinstance(display, str) and display.strip():
                console.print(Panel(
                    Markdown(display),
                    title="🤖 HermesMa",
                    border_style="cyan",
                    padding=(0, 1),
                    expand=False,
                ))


def _format_tool_args(tool_args: dict) -> str:
    """格式化工具参数，截断过长的值"""
    args_parts = []
    for k, v in tool_args.items():
        v_str = str(v)
        if len(v_str) > 80:
            v_str = v_str[:77] + "..."
        args_parts.append(f"{k}={v_str!r}" if '"' not in v_str else f"{k}='{v_str}'")
    return ", ".join(args_parts)


def _estimate_lines(text: str, width: int) -> int:
    """估算文本在终端中的显示行数（含自动换行）"""
    if not text:
        return 0
    lines = text.split('\n')
    total = 0
    for line in lines:
        line_len = len(line)
        if line_len == 0:
            total += 1  # 空行
        else:
            total += max(1, (line_len + width - 1) // width)
    return total


def _build_memory_search_panel(event: dict) -> Panel:
    """
    构建记忆检索事件的可视化面板。

    展示：检索用的增强查询（截断）、命中条数、每条命中的记忆预览。

    Args:
        event: stream_invoke 中的 memory_search 事件

    Returns:
        Panel: 渲染好的 Rich Panel（样式与工具面板一致）
    """
    query = event.get("query", "")
    query_display = query if len(query) <= 80 else query[:77] + "..."
    hit_count = event.get("hit_count", 0)
    hits = event.get("hits", [])

    parts = []
    parts.append(Text.from_markup(f"  🔍 [dim]query: {escape(query_display)}[/dim]"))

    if hit_count == 0:
        parts.append(Text("  ⚪ 无命中记忆", style="dim"))
    else:
        parts.append(Text.from_markup(f"  🎯 [dim]命中 {hit_count} 条相关记忆：[/dim]"))
        for i, hit in enumerate(hits[:5]):  # 最多展示 5 条预览
            mem = hit.get("memory", "")
            score = hit.get("score", 0.0)
            mem_display = mem if len(mem) <= 50 else mem[:47] + "..."
            parts.append(
                Text.from_markup(f"     [dim]{i + 1}. {escape(mem_display)} (score: {score:.2f})[/dim]")
            )
        if hit_count > 5:
            parts.append(Text.from_markup(f"     [dim]...还有 {hit_count - 5} 条[/dim]"))

    content = Group(*parts)

    return Panel(
        content,
        title="🧠 记忆检索",
        border_style="dim blue",
        box=box.ASCII,
        padding=(0, 1),
        expand=False,
    )


def _build_tool_panel(info: dict) -> Panel:
    """
    根据工具信息构建独立的 Rich Panel。

    Args:
        info: 工具状态字典，包含 name, args, status, result, is_subagent, text

    Returns:
        Panel: 渲染好的 Rich Panel
    """
    name = info.get("name", "unknown")
    args = info.get("args", {})
    status = info.get("status", "running")
    result = info.get("result", "")
    is_subagent = info.get("is_subagent", False)
    text = info.get("text", "")

    is_done = status == "done"

    if is_subagent:
        title = f"🔧 {name}"
        border_style = "dim magenta" if is_done else "dim blue"
        if is_done:
            content = Markdown(text) if text else Text("✅ 完成", style="dim")
        else:
            content = Markdown(text) if text else Text("⏳ 正在生成...", style="dim italic")
    else:
        args_display = _format_tool_args(args)
        title = f"✅ {name}" if is_done else f"🔧 {name}"
        border_style = "dim green" if is_done else "dim yellow"

        parts = []
        if args_display:
            parts.append(Text.from_markup(f"  📥 [dim]{escape(args_display)}[/dim]"))
        if is_done:
            if result:
                display = result[:120].replace("\n", " ")
                if len(result) > 120:
                    display += "..."
                parts.append(Text.from_markup(f"  ✅ [dim]{escape(display)}[/dim]"))
            else:
                parts.append(Text("  ✅ 完成", style="dim"))
        else:
            parts.append(Text("  ⏳ 执行中...", style="dim italic"))

        content = Group(*parts) if parts else Text("", style="dim")

    return Panel(
        content,
        title=title,
        border_style=border_style,
        box=box.ASCII,
        padding=(0, 1),
        expand=False,
    )
