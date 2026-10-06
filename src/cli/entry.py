"""
============================================
Hermes Rich CLI —— main 入口（REPL + 斜杠分发表）
============================================
P2 拆包自 src/cli.py（main() 函数体原样搬出，行为零变化）。

拆包兼容层（零行为变化）：main 的全部可替换依赖（chat / show_* / _cmd_* /
save_session / list_sessions / MemoryManager / HermesAgentV3 /
run_health_check / describe_workspace / _shutdown_mcp_quietly /
_migrate_old_sessions / SUMMARIES_DIR / console / Prompt 等）拆包前都是 cli
单模块全局——测试经 monkeypatch 替换 `src.cli.X` 后 main 必须使用替换后的
绑定。入口处在函数体内调用期 `from src.cli import ...` 再绑定（同时规避
entry ↔ 包的循环 import）；函数体其余部分与拆包前逐行一致。
"""

import logging
import sys
import threading
import uuid

from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel

from config import get_settings
from src.constants import LOCAL_USER
from src.tools.virtual_fs import get_virtual_fs

logger = logging.getLogger("hermes.cli")


def _shutdown_mcp_quietly(agent) -> None:
    """退出路径统一关 MCP（L3：Ctrl+C/EOF 分支此前不关，stdio 子进程悬挂到进程死）。"""
    try:
        if agent is not None and hasattr(agent, "shutdown_mcp"):
            agent.shutdown_mcp()
    except Exception:
        logger.warning("MCP 关闭失败（忽略）", exc_info=True)


def main():
    """CLI 主入口函数"""
    # 拆包兼容层（零行为变化）：下列名字拆包前都是 cli 单模块全局——测试与
    # 外部代码 monkeypatch/patch.object 替换 `src.cli.X` 后，main 的初始化与
    # 斜杠分发必须使用替换后的绑定。调用期经包属性再绑定（同时规避 entry ↔
    # 包的循环 import）。
    from src.cli import (
        SUMMARIES_DIR,
        HermesAgentV3,
        MemoryManager,
        Prompt,
        _cmd_events,
        _cmd_flow,
        _cmd_fork,
        _cmd_model,
        _cmd_project,
        _cmd_waker,
        _migrate_old_sessions,
        _show_and_pick_session,
        _shutdown_mcp_quietly,
        chat,
        clear_memory,
        compact_session,
        console,
        describe_workspace,
        list_sessions,
        load_session,
        run_health_check,
        save_session,
        show_help,
        show_logo,
        show_memory,
        show_session_history,
        show_skills,
        show_tools,
    )
    # 1. 显示 Logo
    show_logo()

    # 2. 启动健康检查（F2：不阻断——LLM 断连时 /resume、/help 等本地功能
    # 应仍可用，LLM 也可能中途恢复；仅显著警告）
    if not run_health_check(silent=False):
        console.print("  [bold red]⚠️ 健康检查未全部通过（如 LLM 不可达）——对话可能失败，"
                      "本地命令（/resume /help 等）仍可用。[/bold red]")

    # 3. 初始化记忆管理器
    console.print("  ⏳ 正在初始化记忆系统...", style="dim")
    try:
        memory_manager = MemoryManager()
    except Exception as e:
        console.print(f"\n  ❌ [red]记忆系统初始化失败: {escape(str(e))}[/red]")
        logger.error(f"MemoryManager 初始化失败: {e}", exc_info=True)
        sys.exit(1)

    # 3.5 组合根上下文（F3：对齐 worker——不 boot 时进程内无 WorkspaceService，
    #     resolve_tools 安全回退 chat-only，但 /tools 与 MCP 菜单却展示全量，误导）
    boot_ctx = None
    try:
        from src.plugins import boot_context
        boot_ctx = boot_context()
    except Exception as e:
        console.print(f"\n  ⚠️ [yellow]组合根上下文启动失败，工具面降级为 chat-only: {escape(str(e))}[/yellow]")
        logger.exception("boot_context 启动失败")

    # 4. 初始化 Agent（工具调用事件通过 stream_invoke 事件流传递，无需 callback）
    console.print("  ⏳ [dim]正在初始化 AI Agent...[/dim]", style="dim")
    try:
        # P2（二轮审查）：对齐 worker_process 的接线——CLI 也构造
        # InterruptStore(session_log=sessions) 传入 agent，审批事件才落
        # 事件库（/events 有审批轨迹；崩溃后 recover_into 可恢复 pending
        # 审批）。失败降级为纯内存（agent 内自建），不阻塞启动。
        agent_kwargs: dict = {
            "registry": boot_ctx.get("tools").registry if boot_ctx is not None else None,
            "kernel_ctx": boot_ctx,
        }
        sessions = boot_ctx.try_get("sessions") if boot_ctx is not None else None
        if sessions is not None:
            try:
                from src.agent.hitl import InterruptStore
                interrupt_store = InterruptStore(session_log=sessions)
                recovered = sessions.recover_into(interrupt_store)
                agent_kwargs["interrupt_store"] = interrupt_store
                logger.info(f"SessionLog 就绪，恢复 pending 中断 {recovered} 个")
            except Exception:
                logger.warning("SessionLog 初始化失败，事件溯源降级关闭", exc_info=True)
        agent = HermesAgentV3(memory_manager, tool_callback=None, **agent_kwargs)
    except Exception as e:
        console.print(f"\n  ❌ [red]Agent 初始化失败: {escape(str(e))}[/red]")
        logger.error(f"HermesAgentV3 初始化失败: {e}", exc_info=True)
        if boot_ctx is not None:
            try:
                boot_ctx.teardown()
            except Exception:
                pass
        sys.exit(1)

    console.print("  ✅ [green]系统初始化完成！[/green]\n")

    # 4.1 启动横幅：实际生效工具数 + 工作区模式（F3——如实展示工具面）
    try:
        from src.tools.context import ToolContext
        from src.tools.resolve import resolve_tools
        effective = resolve_tools(
            ToolContext(caller_context="main"), get_settings(), include_mcp=True,
        )
        mode_desc, detail = describe_workspace()
        console.print(
            f"  🔧 生效工具 {len(effective)} 个 | 工作区: {mode_desc}"
            + (f"（{detail}）" if detail else "")
        )
        console.print()
    except Exception:
        logger.warning("启动横幅工具统计失败（忽略）", exc_info=True)

    # 4.2 MCP 后台连接（对齐 web worker：不阻塞启动，成功后下一 turn resolve 可见）
    def _connect_mcp_bg() -> None:
        try:
            from src.mcp.client import get_client_manager
            results = get_client_manager().connect_enabled_all()
        except Exception:
            logger.warning("CLI 启动期 MCP 后台连接失败（降级为无 MCP 工具）",
                           exc_info=True)
            return
        connected = [n for n, (ok, _) in results.items() if ok]
        for n, (ok, msg) in results.items():
            (logger.info if ok else logger.warning)(f"MCP 后台连接 {n}: {msg}")
        if connected:
            try:
                agent.rebind_tools()
                logger.info(f"MCP 工具已 rebind: {', '.join(connected)}")
            except Exception:
                logger.warning("MCP 工具 rebind 失败（下一 turn resolve 重试）",
                               exc_info=True)
    threading.Thread(target=_connect_mcp_bg, daemon=True,
                     name="cli-mcp-connect").start()

    # 4.5 迁移旧格式会话文件
    _migrate_old_sessions()

    # 5. 单用户身份（M5：多用户登录提示与 /switch 已退役——记忆/存储本就是
    # 全局单库，旧"切换用户"只是换了会话目录命名空间，还宣称"记忆隔离"，误导）
    user_id = LOCAL_USER

    # 默认开启新会话（不再自动加载上次对话）
    session_messages = []
    session_id = str(uuid.uuid4())[:8]
    # 跨轮次状态：todos、virtual_fs、waker 绑定（持久化 + 传给 stream_invoke）
    current_todos: list = []
    current_vfs: dict = {}
    current_waker: str = ""
    current_thinking: bool = False  # 思考模式开关（/think，与 Web 推理 chip 对齐）
    last_reasoning: list[str] = [""]  # 最近一轮推理全文（chat() 回写，/reasoning 回看）
    logger.debug(f"新会话 session_id: {session_id}")

    # 提示用户可恢复历史会话
    history = list_sessions(user_id)
    history_hint = f"（有 {len(history)} 个历史会话，输入 [yellow]/resume[/yellow] 恢复）" if history else ""

    console.print(f"  👋 欢迎回来！🆕 已开启新会话。{history_hint}")
    console.print(f"  输入消息开始对话，或输入 [yellow]/help[/yellow] 查看命令。\n")

    # 6. 对话循环
    while True:
        try:
            user_input = Prompt.ask(f"  👤 {user_id}")
        except UnicodeDecodeError:
            # 管道/重定向喂入的编码与 PYTHONIOENCODING 不符（Windows 下
            # GBK 字节最常见）：跳过这一行而不是让整个 CLI 崩溃退出
            console.print("  [red]⚠️ 输入编码无法解码，该行已跳过（检查输入源编码或设置 PYTHONIOENCODING）[/red]")
            continue
        except EOFError:
            break
        except KeyboardInterrupt:
            # P2（二轮审查）：主提示符 Ctrl+C 不再整进程崩掉（Windows
            # STATUS_CONTROL_C_EXIT 直接跳过 save_session/boot_ctx teardown）
            # ——与 /mode 等子提示符同语义：取消当前输入回主提示符；退出走
            # /exit 或 EOF
            console.print("\n  [dim]已取消（输入 /exit 退出）[/dim]\n")
            continue
        # rich Prompt.ask 在管道 stdin EOF 时返回空串而非抛 EOFError（实测
        # cat 文件驱动）。非 TTY 下空串须区分空行与流尽：TextIOWrapper 无
        # peek（AttributeError），buffer.peek 又会被自身预读缓冲欺骗——
        # 用一行试探性 readline 判定：仅真 EOF 返回 ""；读到内容则作为
        # 本条输入继续处理（不丢行）。TTY 不进此分支（空回车是合法空输入）。
        if user_input == "" and not sys.stdin.isatty():
            user_input = sys.stdin.readline()
            if user_input == "":
                console.print("\n  [dim]（输入流结束）[/dim]")
                break

        if not user_input.strip():
            continue

        # BOM(\ufeff) 剥离：Windows 下 PowerShell/记事本导出的 UTF-8 文件
        # 带 BOM，管道/重定向喂入时粘在首行开头。str.strip() 不视其为空白，
        # "/mode" 会变成 "\ufeff/mode" → startswith("/") 判否 → 命令被当
        # 用户消息发给模型（实测）。BOM 是流级标记不是内容，读入即剥。
        user_input = user_input.lstrip("\ufeff").strip()

        # ============================================
        # 处理内置命令
        # ============================================
        if user_input.startswith("/"):
            cmd_parts = user_input.split(maxsplit=1)
            cmd = cmd_parts[0].lower()
            cmd_arg = cmd_parts[1] if len(cmd_parts) > 1 else ""

            if cmd == "/exit":
                # 2026-06-24: 退出总结改为交互式选项菜单（用户决定是否总结/展示）
                # UI 进度/正文展示由 on_session_end(interactive=True) 内部自管
                if session_messages:
                    try:
                        from src.agent.session_lifecycle import on_session_end
                        result = on_session_end(
                            memory_manager, user_id, session_messages, session_id,
                            interactive=True,
                            summaries_dir=SUMMARIES_DIR,
                        )
                        md = result.get("markdown_path")
                        if md:
                            console.print(f"  [dim]📄 总结已保存: {md}[/dim]")
                    except Exception as e:
                        logger.warning(f"会话结束总结失败（不影响退出）: {e}")
                save_session(user_id, session_messages, session_id, todos=current_todos, virtual_fs=current_vfs)
                # 2026-06-19: 关闭所有 MCP 连接
                agent.shutdown_mcp()
                console.print(f"\n  👋 再见，[cyan]{user_id}[/cyan]！会话已保存。\n")
                break

            elif cmd == "/resume":
                # 2026-06-16: 支持 /resume -a 查看全部历史
                show_all_flag = False
                if cmd_arg:
                    if cmd_arg.strip() == "-a":
                        show_all_flag = True
                        resume_id = None
                    else:
                        resume_id = cmd_arg.strip()
                else:
                    resume_id = None

                if not resume_id:
                    # 无参数：显示列表让用户选择
                    resume_id = _show_and_pick_session(user_id)
                    if not resume_id:
                        continue

                # 保存当前会话
                if session_messages:
                    save_session(user_id, session_messages, session_id, todos=current_todos, virtual_fs=current_vfs)

                # 加载目标会话（load_session 返回 tuple: messages, todos, virtual_fs, waker）
                loaded_msgs, loaded_todos, loaded_vfs, loaded_waker = load_session(user_id, resume_id)
                if not loaded_msgs:
                    console.print(f"  [red]会话 {resume_id} 不存在或为空[/red]\n")
                    continue

                session_messages = loaded_msgs
                session_id = resume_id
                current_todos = loaded_todos
                current_vfs = loaded_vfs
                # L8：waker 人格绑定随会话恢复（此前读了就丢，绑定的会话
                # 恢复后静默跑默认人格）
                current_waker = loaded_waker or ""
                if current_waker:
                    console.print(f"  [dim]🧑‍💼 会话绑定数字员工: {current_waker}[/dim]")

                # 虚拟文件系统模式：回填全局 _virtual_fs（工具直接操作全局变量）
                if current_vfs:
                    from src.tools.virtual_fs import _virtual_fs as vfs_global
                    vfs_global.clear()
                    vfs_global.update(current_vfs)
                logger.debug(f"恢复会话: session_id={session_id}")

                # 2026-06-16: 显示会话名（如果有）
                name_hint = ""
                for s in list_sessions(user_id):
                    if s["session_id"] == resume_id and s.get("name"):
                        name_hint = f" [cyan]{s['name']}[/cyan]（"
                        break

                if name_hint:
                    console.print(f"\n  ✅ 已恢复会话{name_hint}ID: [cyan]{session_id}[/cyan]）")
                else:
                    console.print(f"\n  ✅ 已恢复会话 ID: [cyan]{session_id}[/cyan]")
                show_session_history(session_messages, show_all=show_all_flag)

            elif cmd == "/mode":
                # L7：CLI 权限模式切换（此前只有 Web 能切——V3 的"审批时
                # 切 mode 自动放行"路径在 CLI 不可达）
                cur = agent.get_permission_mode()
                console.print(f"\n  当前权限模式: [cyan]{cur}[/cyan]")
                console.print("  [dim]full_access=完全访问 / before_changes=变更前审批（默认）/ plan=计划模式（拒绝变更）[/dim]")
                try:
                    new_mode = Prompt.ask("  切换为（回车=不切换）", default="")
                except (EOFError, KeyboardInterrupt):
                    # P2-6：输入流尽/中断 = 取消切换，回主提示符
                    # （此前异常冲出 main，会话收尾被跳过）
                    console.print("\n  [dim]已取消[/dim]\n")
                    continue
                new_mode = new_mode.strip()
                if not new_mode:
                    continue
                if new_mode not in ("full_access", "before_changes", "plan"):
                    console.print(f"  [red]未知模式: {new_mode}[/red]\n")
                    continue
                agent.set_permission_mode(new_mode)
                console.print(f"  ✅ [green]权限模式已切换为: {new_mode}[/green]\n")

            elif cmd == "/model":
                # 模型热切换：/model 查看当前与档案列表，/model <id> 切换
                # （经 agent.set_llm_params 注入，下一轮对话生效，无需重启）
                _cmd_model(agent, cmd_arg)

            elif cmd == "/memory":
                show_memory(memory_manager, user_id)

            elif cmd == "/clear":
                clear_memory(memory_manager, user_id)

            elif cmd == "/save":
                save_session(user_id, session_messages, session_id, todos=current_todos, virtual_fs=current_vfs)
                console.print(f"  ✅ [green]会话已保存（ID: {session_id}，{len(session_messages)} 条消息）[/green]\n")

            elif cmd == "/rename":
                # 2026-06-16: 重命名当前会话
                if not cmd_arg:
                    console.print("  [yellow]用法: /rename <新名称>[/yellow]\n")
                    continue
                new_name = cmd_arg.strip()
                save_session(user_id, session_messages, session_id,
                             todos=current_todos, virtual_fs=current_vfs, name=new_name)
                console.print(f"  ✅ [green]会话已重命名为: {new_name}[/green]\n")

            elif cmd == "/skill":
                # 技能列表（只读）。技能由 LLM 自主调用 use_skill 加载。
                show_skills()

            elif cmd == "/mcp":
                # MCP 服务器管理菜单（交互式二级菜单）
                from src.cli_mcp_menu import McpMenu
                McpMenu(agent).run()

            elif cmd == "/tools":
                show_tools(agent)

            elif cmd == "/compact":
                compact_session(boot_ctx, session_id, session_messages)

            elif cmd == "/reset":
                console.print(f"\n⚠️  [yellow]确定要重置当前会话吗？所有对话历史将丢失！[/yellow]")
                try:
                    confirm = Prompt.ask("   请输入 yes 确认", default="no")
                except (EOFError, KeyboardInterrupt):
                    # P2-6：输入流尽/中断 = 取消重置，回主提示符
                    # （此前异常冲出 main，会话收尾被跳过）
                    console.print("\n  [dim]已取消[/dim]\n")
                    continue
                if confirm.lower() == "yes":
                    # 会话总结按需触发（2026-09-08）：reset 不再自动总结——
                    # 只有 /exit 菜单里显式选择才总结
                    # 保存旧会话后开启新会话
                    save_session(user_id, session_messages, session_id, todos=current_todos, virtual_fs=current_vfs)
                    session_messages.clear()
                    session_id = str(uuid.uuid4())[:8]
                    # M4：todos/vfs/waker 一并重置——此前新会话继承上一会话的
                    # 待办与虚拟文件系统（工具还能看到旧文件）
                    current_todos = []
                    current_vfs = {}
                    current_waker = ""
                    try:
                        from src.tools.virtual_fs import reset_virtual_fs
                        reset_virtual_fs()
                    except Exception:
                        pass
                    logger.debug(f"重置会话，新 session_id: {session_id}")
                    console.print(f"  ✅ [green]会话已重置，新会话 ID: {session_id}[/green]\n")
                else:
                    console.print("  [dim]已取消[/dim]\n")

            elif cmd == "/project":
                if boot_ctx is not None:
                    _cmd_project(boot_ctx, cmd_arg)
                else:
                    console.print("  [red]组合根未启动，项目空间不可用[/red]")

            elif cmd == "/events":
                if boot_ctx is not None:
                    _cmd_events(boot_ctx, session_id, cmd_arg)
                else:
                    console.print("  [red]组合根未启动，事件流不可用[/red]")

            elif cmd == "/fork":
                if boot_ctx is None:
                    console.print("  [red]组合根未启动，fork 不可用[/red]")
                    continue
                if session_messages:
                    save_session(user_id, session_messages, session_id,
                                 todos=current_todos, virtual_fs=current_vfs)
                new_sid = _cmd_fork(boot_ctx, user_id, session_id, session_messages, cmd_arg)
                if new_sid:
                    # 切换到分支：从事件投影重建消息（含历史回显）
                    loaded_msgs, loaded_todos, loaded_vfs, loaded_waker = load_session(user_id, new_sid)
                    session_messages = loaded_msgs
                    session_id = new_sid
                    current_todos = loaded_todos
                    current_vfs = loaded_vfs
                    current_waker = loaded_waker or ""
                    show_session_history(session_messages)

            elif cmd == "/reasoning":
                # 回看最近一轮完整推理（折叠模式下推理不打正文，此处是唯一全文入口）
                text = last_reasoning[0] if last_reasoning else ""
                if text.strip():
                    console.print(Panel(
                        Markdown(text),
                        title="💭 最近一轮推理",
                        border_style="grey50",
                        padding=(0, 1),
                        expand=False,
                    ))
                    console.print("  [dim]💡 生成中按 r 可折叠/展开推理预览[/dim]\n")
                else:
                    console.print("  [dim]本轮会话尚无推理内容（模型未开启思考或还没有对话轮）[/dim]\n")

            elif cmd == "/think":
                arg_l = cmd_arg.strip().lower()
                if arg_l in ("on", "off"):
                    current_thinking = arg_l == "on"
                elif arg_l in ("", "status"):
                    pass
                else:
                    console.print("  [red]用法: /think [on|off][/red]")
                    continue
                console.print(
                    f"  思考模式: [cyan]{'开（模型输出推理过程）' if current_thinking else '关'}[/cyan] · 推理预览与 r 键切换不受此开关影响"
                )

            elif cmd == "/waker":
                _cmd_waker(cmd_arg, user_id, agent)

            elif cmd == "/flow":
                _cmd_flow(cmd_arg, user_id, agent)

            elif cmd == "/help":
                show_help()

            else:
                console.print(f"  [red]未知命令: {cmd}[/red]，输入 [yellow]/help[/yellow] 查看可用命令\n")

            continue

        # ============================================
        # 普通消息：调用 Agent 对话（支持工具调用）
        # ============================================
        try:
            chat(agent, user_id, user_input, session_messages, session_id,
                  todos=current_todos, virtual_fs=current_vfs, waker=current_waker,
                  thinking=current_thinking, reasoning_store=last_reasoning)
            # 对话后更新跨轮次状态（todos 由 chat() 从 todos_update 事件
            # 原地写回 current_todos（R2-10）；virtual_fs 仍从全局回读）
            current_vfs = dict(get_virtual_fs())
            save_session(user_id, session_messages, session_id,
                         todos=current_todos, virtual_fs=current_vfs)

        except KeyboardInterrupt:
            # P2-6：Ctrl+C 中断输出不退出，但本轮对话要落盘（此前分支无
            # save_session，中断的内容不进持久化会话）
            console.print("\n  [dim]（输出被中断）[/dim]\n")
            save_session(user_id, session_messages, session_id,
                         todos=current_todos, virtual_fs=current_vfs)
        except EOFError:
            # P2-6：EOFError 必须在 except Exception 之前（它是 Exception
            # 子类，此前被先吃成"对话出错"而非干净退出；退出分支统一
            # 先 save_session 再收尾）
            console.print("\n")
            save_session(user_id, session_messages, session_id,
                         todos=current_todos, virtual_fs=current_vfs)
            _shutdown_mcp_quietly(agent)
            console.print(f"  👋 再见！会话已保存。\n")
            break
        except Exception as e:
            console.print(f"\n  [red]⚠️ 对话出错: {escape(str(e))}[/red]")
            console.print("  [dim]详情见 logs/error.log[/dim]\n")
            logger.error(f"对话错误: {e}", exc_info=True)

    # F3：拆卸组合根上下文（进程级生命周期与 main() 一致；幂等）
    if boot_ctx is not None:
        try:
            boot_ctx.teardown()
        except Exception:
            logger.warning("组合根上下文 teardown 失败（忽略）", exc_info=True)


# ============================================
# 脚本入口
# ============================================
if __name__ == "__main__":
    main()
