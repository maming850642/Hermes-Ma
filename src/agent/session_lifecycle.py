"""
============================================
SessionLifecycle - 会话生命周期 hook
============================================
统一处理会话结束时的"总结 + 批量记忆"。

由 CLI (/exit, /reset, /switch) 和 Web (session 结束) 在退出会话前调用。
封装在这里避免 hook 点散落各处（设计风险 R3 的缓解）。

I6 修复（2026-06-22）：on_session_end 加超时机制（默认 120s），
避免 /exit 被 LLM 总结长时间阻塞。超时则跳过总结，不影响退出。

2026-06-24 新增：interactive=True 时（仅 /exit）弹选项菜单让用户决定
是否总结、是否展示总结正文；summaries_dir 传入时同步落盘 Markdown。

P2-18（2026-09-05）：超时保护改为真超时——旧实现 future.result(timeout)
超时抛出后，with 块退出的 executor.shutdown(wait=True) 仍会同步 join 到
LLM 跑完，超时被击穿。现在总结跑在自管 daemon 线程上、join 有界：超时
后 on_session_end 立即返回、不再阻塞退出；超时的总结线程若进程保持
存活可能仍在后台完成（结果被丢弃），进程退出则随 daemon 线程放弃。
"""
import logging
import sys
import threading

from rich.console import Console
from rich.prompt import Prompt

logger = logging.getLogger("hermes.agent.session_lifecycle")

# 交互菜单用一个独立 Console（不依赖 cli.py 的全局实例，保持模块解耦）
_console = Console(file=sys.stdout)


def _messages_to_text(messages: list) -> str:
    """把消息列表（OpenAI dict）转成 "用户: ...\\n助手: ..." 文本。

    跳过纯工具调用消息（无 content 的 assistant），只保留可读对话。
    注：有意不处理 tool 消息（工具结果含中间步骤细节，会话总结只需对话结论）。
    这与 context.py compact_messages 的消息转文本行为不同（后者把 tool 消息
    标为"系统"保留），是两处场景差异，非 bug。
    """
    lines = []
    for msg in messages:
        role = msg.get("role", "") if isinstance(msg, dict) else ""
        content = msg.get("content", "") if isinstance(msg, dict) else ""
        if role == "user":
            if content:
                lines.append(f"用户: {content}")
        elif role == "assistant":
            # 跳过纯工具调用消息
            if not msg.get("tool_calls") and content:
                lines.append(f"助手: {content}")
    return "\n".join(lines)


def _ask_exit_choice() -> str:
    """弹退出总结选项菜单，返回 "1"/"2"/"3"。无效输入循环重试。

    默认 "3" 不总结（2026-09-08 用户决策）：会话总结只在用户显式选择时
    进行——直接回车 = 跳过总结，不再默认烧 LLM。
    """
    _console.print("\n  [bold]📝 会话结束，是否生成总结？[/bold]")
    _console.print("    [cyan]1[/cyan] 生成总结并展示在屏幕上")
    _console.print("    [cyan]2[/cyan] 生成总结但不展示（直接保存）")
    _console.print("    [cyan]3[/cyan] 不总结，直接退出")
    while True:
        choice = Prompt.ask("  请选择", default="3", choices=["1", "2", "3"])
        if choice in ("1", "2", "3"):
            return choice


def on_session_end(
    manager,
    user_id: str,
    messages: list,
    session_id: str,
    timeout: float = 120.0,
    interactive: bool = False,
    summaries_dir=None,
) -> dict:
    """
    会话结束时调用：生成总结 + 提取长期事实。

    Args:
        manager: MemoryManager 实例
        user_id: 用户 ID
        messages: 会话消息列表（OpenAI dict 格式，role/content）
        session_id: 会话 ID
        timeout: 超时秒数（I6 修复，P2-18 改为真超时）。超时则跳过总结
            返回未存储，避免阻塞 /exit；超时后总结可能仍在后台完成，
            但结果不再被本调用采用。
        interactive: 是否弹选项菜单（仅 /exit 传 True）。
            True 时由用户决定是否总结、是否展示正文；False 时自动总结不询问。
        summaries_dir: 总结根目录 Path（如 PROJECT_ROOT/data/summaries）。
            传入则让 Summarizer 同步落盘一份 Markdown；None 时仅写记忆（profile.md）。

    Returns:
        Summarizer 的返回 dict（{summary_stored, facts_count, markdown_path}），
        失败/超时返回 {"summary_stored": False, "facts_count": 0, "markdown_path": None}
    """
    if not messages:
        logger.debug("会话无消息，跳过总结")
        if interactive:
            _console.print("  [dim]会话无可总结内容[/dim]")
        return {"summary_stored": False, "facts_count": 0, "markdown_path": None, "summary_text": ""}

    conv_text = _messages_to_text(messages)
    if not conv_text.strip():
        logger.debug("会话无可读对话内容，跳过总结")
        if interactive:
            _console.print("  [dim]会话无可总结内容[/dim]")
        return {"summary_stored": False, "facts_count": 0, "markdown_path": None, "summary_text": ""}

    # interactive 模式：先问用户是否总结、是否展示
    show_summary = False
    if interactive:
        choice = _ask_exit_choice()
        if choice == "3":
            _console.print("  [dim]已跳过总结[/dim]")
            return {"summary_stored": False, "facts_count": 0, "markdown_path": None, "summary_text": ""}
        show_summary = (choice == "1")
        _console.print(f"  [dim]📝 正在总结会话记忆...[/dim]")
    # 非交互模式：沿用旧行为，不打印进度（由调用方自行提示）

    def _do_summarize() -> dict:
        from src.memory.summarizer import Summarizer
        from config import get_settings
        s = get_settings()
        summarizer = Summarizer(
            manager=manager,
            api_key=s.openai_api_key,
            base_url=s.openai_base_url,
            model=s.llm_model_name,
            # 复用 chat 侧共享 client（worker 已绑定；独立场景为 None 走兜底）
            llm=getattr(manager, "llm_shared", None),
        )
        result = summarizer.summarize_and_store(
            user_id, conv_text, session_id, summaries_dir=summaries_dir
        )
        logger.info(
            f"会话结束总结完成: user={user_id}, session={session_id}, "
            f"summary_stored={result['summary_stored']}, facts={result['facts_count']}"
        )
        return result

    # P2-18：真超时。总结跑在自管 daemon 线程，join 有界——超时后本调用
    # 立即返回、不再阻塞退出；线程本身可能仍在后台完成总结（结果丢弃），
    # 进程退出时 daemon 线程直接放弃。
    box: dict = {}

    def _run() -> None:
        try:
            box["result"] = _do_summarize()
        except Exception as e:
            box["error"] = e

    thread = threading.Thread(target=_run, name="session-summary", daemon=True)
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        logger.warning(
            f"会话结束总结超时（{timeout}s），跳过总结"
            f"（超时后总结可能仍在后台完成，结果不再采用）"
        )
        if interactive:
            _console.print("  [yellow]⚠️ 总结超时，已跳过[/yellow]")
        return {"summary_stored": False, "facts_count": 0, "markdown_path": None, "summary_text": ""}
    if "error" in box:
        logger.error(f"会话结束总结失败: {box['error']}", exc_info=box["error"])
        if interactive:
            _console.print(f"  [yellow]⚠️ 会话结束总结失败（不影响退出）[/yellow]")
        return {"summary_stored": False, "facts_count": 0, "markdown_path": None, "summary_text": ""}

    result = box["result"]

    # interactive 模式：展示结果
    if interactive:
        if result["summary_stored"]:
            if show_summary:
                # 从总结前缀还原正文：summarizer 写入的 content 形如 "[会话总结 {sid}]\n..."
                _console.print(f"\n  [green]✅ 会话总结：[/green]")
                _console.print(f"  [white]{result.get('summary_text', '')}[/white]\n")
            else:
                _console.print(
                    f"  [green]✅ 会话总结已保存（事实 {result['facts_count']} 条）[/green]\n"
                )
        else:
            _console.print("  [dim]会话无可总结内容[/dim]\n")

    return result
