"""
============================================
Hermes Rich CLI —— 对话核心（chat：Live 流式渲染 + HITL 审批 + 自动分段）
============================================
P2 拆包自 src/cli.py（chat() 函数体原样搬出，行为零变化）。

拆包兼容层（零行为变化）：console / Live / Prompt 拆包前是 cli 单模块
全局，测试常整体替换 `src.cli.console` / `src.cli.Live` / `src.cli.Prompt`
（mock console 驱动流式渲染、拦截 Live 与审批输入）。chat() 入口处调用期
`from src.cli import Live, Prompt, console` 再绑定，保持该 patch 语义不变；
函数体其余部分与拆包前逐行一致。
"""

import logging
import os
import sys
import threading

from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from src.agent import HermesAgentV3

from .render import _build_memory_search_panel, _build_tool_panel, _estimate_lines

logger = logging.getLogger("hermes.cli")


def chat(agent: HermesAgentV3, user_id: str, user_input: str, session_messages: list, session_id: str = "",
         todos: list | None = None, virtual_fs: dict | None = None, waker: str = "",
         thinking: bool = False, reasoning_store: list | None = None) -> str:
    """
    流式调用 Agent 对话（Live 面板 + 自动分段）。

    显示策略：
    - AI 文本：Live 面板流式更新，超过终端高度时自动分段（冻结当前面板，开新面板继续）
    - 推理（reasoning_token）：thinking=True 实时流式（dim 面板，正文开始时冻结保留）；
      thinking=False 折叠——轮末提示字数，完整内容经 reasoning_store 回写供 /reasoning 回看
    - 顺序工具：Live 显示运行中 → 完成时冻结
    - 并发工具：第一个进入 Live 后检测到并发 → 冻结 → 后续全部静态打印
    - 子智能体：静默累积文本，完成时面板展示

    自动分段保证每个 Live 面板高度 < 终端高度，永不堆叠。
    """
    from src.cli import Live, Prompt, console  # 调用期再绑定（拆包兼容层，见模块 docstring）
    console.print()  # 用户输入后加空行

    response = ""

    # ---- 终端高度 & 分段阈值 ----
    term_h = console.size[1]  # ConsoleDimensions 是 (width, height) 命名元组
    max_panel_lines = max(term_h - 6, 10)  # 留 6 行给边框/标题/padding
    console_width = console.size[0]

    # ---- 流式状态 ----
    current_text = ""       # 当前段落积累的文本
    text_started = False    # 是否已开始有效文本
    token_count = 0         # 节流计数器
    segment_chars = 0       # 当前段落已积累的字符数（用于分段检测）
    reasoning_buf: list[str] = []   # 推理全文累积（两种模式都收——/reasoning 回看的数据基础）
    # 推理显示状态机（Web 对齐：默认滚动预览，r 键切换，正文开始自动折叠）：
    #   preview  = Live 面板显示尾部 K 行（高度有界不刷屏）
    #   collapsed= 单行"推理中… N 字 [r 展开]"
    #   正文/工具阶段（面板已折叠成终端里的静态单行）按 r → 静态打印一次
    #   尾部回看（reasoning_tail_printed 门禁防重复刷屏），见主循环顶部消费块
    reasoning_display = "preview"
    reasoning_token_seen = 0        # 节流计数
    reasoning_tail_printed = False  # 正文阶段静态回看已打印（每次折叠期至多一次）

    live = None             # 当前 Live 实例
    live_mode = None        # "text" 或 tool_id

    tool_index: dict[str, dict] = {}   # tool_id → 工具信息
    concurrent_mode = False            # 是否处于并发工具模式
    segmented = False                  # 是否已发生自动分段（Panel > 终端高度时为 True）
    text_finalized = False             # 2026-06-15: 文本是否已被最终渲染（避免 complete 重复打印）

    # 2026-06-16: HITL 中断/恢复控制
    resume_payload = None              # 恢复时透传给 stream_invoke(resume_payload=...)
    last_turn_msg_count = 0            # 上次 turn_messages 写入 session_messages 的消息数（去重用）

    def _start_live(renderable):
        nonlocal live
        live = Live(renderable, console=console, refresh_per_second=4, auto_refresh=False)
        live.start()

    def _stop_live():
        nonlocal live, live_mode
        if live is not None:
            # 推理面板冻结走 _finalize_reasoning_live 的折叠单行（先替换再
            # stop）；此处只负责通用清理，防 Markdown 全量面板残留刷屏
            live.stop()
            live = None
            live_mode = None

    def _make_text_panel(text, is_draft=True):
        """
        构建 AI 文本面板。

        2026-06-15: 流式阶段用 Text（行数精确，避免 Rich Live 残留边框）；
        完成时用 Markdown（带格式渲染）。
        """
        content = Text(text, style="white") if is_draft else Markdown(text)
        return Panel(
            content,
            title="🤖 HermesMa",
            border_style="cyan",
            padding=(0, 1),
            expand=False,
        )

    def _finalize_text_live():
        """2026-06-15: 把文本 Live 转成 Markdown 最终版并 stop，避免 memory_store/tool_start 后 complete 重复打印"""
        nonlocal text_finalized
        if live is not None and live_mode == "text" and current_text.strip():
            live.update(_make_text_panel(current_text, is_draft=False), refresh=True)
            text_finalized = True
        _stop_live()

    def _freeze_and_start_new_segment():
        """冻结当前文本面板（保留在终端），重置为新段落"""
        nonlocal current_text, segment_chars, token_count, segmented
        # 2026-06-15: 冻结前切换为 Markdown 最终版（带格式），再 stop 保留在终端
        if live is not None and live_mode == "text" and current_text.strip():
            live.update(_make_text_panel(current_text, is_draft=False), refresh=True)
        _stop_live()  # 冻结，内容保留在终端
        segmented = True  # 标记已发生分段
        current_text = ""
        segment_chars = 0
        token_count = 0

    def _make_reasoning_panel(text: str, tail_lines: int = 0, title: str = ""):
        """推理滚动预览面板：固定高度显示尾部行（Web 展开块的终端等价，
        高度有界不刷屏）；title 缺省带累计字数与快捷键提示。"""
        shown = text
        if tail_lines > 0:
            all_lines = text.splitlines() or [""]
            if len(all_lines) > tail_lines:
                shown = "…\n" + "\n".join(all_lines[-tail_lines:])
        n_chars = len(text)
        if not title:
            title = f"💭 推理 · {n_chars}字 [r 折叠]"
        return Panel(
            Text(shown, style="dim"),
            title=title,
            border_style="grey50",
            padding=(0, 1),
            expand=False,
        )

    def _make_reasoning_collapsed_line(text: str, streaming: bool = True):
        """推理折叠单行（生成期间 / 结束后的最终形态）。"""
        n = len(text)
        if streaming:
            return Text(f"🧠 推理中… {n} 字 [r 展开预览]", style="dim")
        return Text(f"🧠 推理 {n} 字 · /reasoning 查看完整内容", style="dim")

    def _print_reasoning_tail():
        """正文/工具阶段的 r 键回看：推理面板此时已折叠成终端 scrollback
        里的静态单行（无法撤回重绘），以一次性静态打印尾部预览替代重开
        Live——Rich 单 Live 限制下重开需停掉正文面板：草稿冻结在终端、
        下一 token 重开正文面板会重复显示已流出文本（取舍详见主循环
        顶部消费块注释）。尾部行数与流式预览同参（10 行有界）。"""
        full = "".join(reasoning_buf)
        console.print(_make_reasoning_panel(
            full, tail_lines=10, title=f"💭 推理回看 · {len(full)}字"))

    # ---- r 键切换（生成期间）：TTY 门禁的键盘监听线程 ----
    # 仅在交互终端启用；Git Bash(mintty)/管道/远程等 msvcrt 不可用环境
    # 静默降级（无快捷键，预览仍工作）。Live 非线程安全：线程只置 flag。
    key_thread = None
    key_stop = threading.Event()

    def _key_worker():
        try:
            if os.name == "nt":
                import msvcrt
                while not key_stop.is_set():
                    if msvcrt.kbhit():
                        ch = msvcrt.getwch()
                        # \x12 = Ctrl+R（getwch 把控制键原样读出）
                        if ch in ("r", "R", "\x12"):
                            nonlocal_dict["toggle"] = True
                    else:
                        key_stop.wait(0.05)
            else:
                import select
                import termios
                import tty
                fd = sys.stdin.fileno()
                old = termios.tcgetattr(fd)
                tty.setcbreak(fd)
                try:
                    while not key_stop.is_set():
                        r, _, _ = select.select([sys.stdin], [], [], 0.05)
                        if r:
                            ch = sys.stdin.read(1)
                            if ch in ("r", "R", "\x12"):  # \x12 = Ctrl+R
                                nonlocal_dict["toggle"] = True
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            nonlocal_dict["disabled"] = True  # 任何键盘异常 → 降级（预览照常）

    nonlocal_dict = {"toggle": False, "disabled": False}

    def _pause_key_thread():
        """审批 Prompt.ask 前暂停键盘线程：Event 置位 + join——不暂停的话
        后台线程与 input() 竞争读 stdin，审批输入 approve 里的 r 会被
        线程吃成 toggle。同时丢弃滞留的 toggle，防迟到误翻转。"""
        nonlocal key_thread
        key_stop.set()
        if key_thread is not None:
            key_thread.join(timeout=1.0)
            if key_thread.is_alive():
                # 极端阻塞（<50ms 轮询不该发生）：宁可本轮降级也不双线程抢键
                nonlocal_dict["disabled"] = True
            key_thread = None
        nonlocal_dict["toggle"] = False

    def _ensure_key_thread():
        """（重新）启动键盘监听线程。审批暂停后必须重建——旧线程已随
        置位的 Event 退出不可复用；disabled（键盘异常降级）时静默跳过。"""
        nonlocal key_thread, key_stop
        if not sys.stdin.isatty() or nonlocal_dict["disabled"]:
            return
        if key_thread is not None and key_thread.is_alive():
            return  # 防御：绝不双线程并发读 stdin
        key_stop = threading.Event()  # 新 Event：旧的可能已带 set 状态
        key_thread = threading.Thread(target=_key_worker, daemon=True)
        key_thread.start()

    _ensure_key_thread()

    def _finalize_reasoning_live():
        """推理结束（正文/工具开始时调用）：Live 内容切换为折叠单行后停止
        ——终端只留一行摘要（Web 的\"推理完自动收起\"等价物），全文靠
        /reasoning 回看。"""
        nonlocal reasoning_tail_printed
        if live is not None and live_mode == "reasoning" and reasoning_buf:
            try:
                live.update(_make_reasoning_collapsed_line("".join(reasoning_buf),
                                                           streaming=False), refresh=True)
            except Exception:
                pass
            reasoning_tail_printed = False  # 新折叠期重新允许一次静态回看
            _stop_live()  # 内容已替换为折叠行，stop 保留该行
        else:
            _stop_live()

    # 2026-06-16: 外层 while 支持多次 stream（中断 → 恢复 → 继续 / 再中断）
    try:
        while True:
            interrupted = False

            # H2：每次 stream_invoke 重入（审批恢复轮）重置去重计数——
            # 跨流残留会让 resume 增量轮 del[-N:] 清光历史
            last_turn_msg_count = 0
            # L8：waker 绑定 → 人格文本（与 worker 同法：load_persona_prompt）；
            # 失败降级默认助手
            waker_persona = None
            if waker:
                try:
                    from src.waker.store import WakerStore
                    from src.waker.persona import load_persona_prompt
                    waker_persona = load_persona_prompt(WakerStore(user_id), waker) or None
                except Exception:
                    logger.warning(f"加载 waker 人格失败: {waker}（降级为默认助手）", exc_info=True)
                    waker_persona = None
            stream = agent.stream_invoke(
                user_id, user_input, session_messages, session_id=session_id,
                todos=todos, virtual_fs=virtual_fs, waker_persona=waker_persona,
                thread_id=session_id, resume_payload=resume_payload,
                thinking=thinking,
            )
            for event in stream:
                # ---- r 键切换消费：主循环顶部，与事件类型无关 ----
                # （修复）此前唯一消费点在 reasoning_token 分支内：正文/工具
                # 阶段没有该事件，flag 滞留 → 按键零响应，迟到才在下一个
                # 推理 token 处误翻转。键盘线程只置 flag，Live 更新一律
                # 留在主循环（Live 非线程安全）；先清 flag 防处理中重复触发。
                if nonlocal_dict["toggle"]:
                    nonlocal_dict["toggle"] = False
                    if reasoning_buf:
                        # 本轮已有推理内容才响应；无推理轮不吃键、不打印
                        if live is not None and live_mode == "reasoning":
                            # 推理流式中：Live 内立即重绘（预览 ↔ 折叠单行）
                            reasoning_display = ("collapsed"
                                                 if reasoning_display == "preview"
                                                 else "preview")
                            full = "".join(reasoning_buf)
                            if reasoning_display == "preview":
                                live.update(_make_reasoning_panel(
                                    full, tail_lines=max(4, console.size[1] // 4)),
                                    refresh=True)
                            else:
                                live.update(_make_reasoning_collapsed_line(full),
                                            refresh=True)
                        elif not reasoning_tail_printed:
                            # 正文/工具阶段（推理面板已折叠成静态单行）：静态
                            # 打印一次尾部回看（正文阶段也能展开回看的核心
                            # 诉求）。取舍：Rich 单 Live 限制下重开 Live 需停
                            # 掉正文面板——草稿冻结在终端、下一 token 重开
                            # 正文面板会重复显示已流出文本，故选静态打印；
                            # 代价是打印后无法"再收起"（每折叠期至多一次，
                            # 再按忽略）且仅在下一事件到达时生效（线程只置
                            # flag，静默期如长工具执行中的按键顺延到下一事件）。
                            _print_reasoning_tail()
                            reasoning_tail_printed = True
                event_type = event.get("type")

                # 2026-06-16: HITL 审批请求
                # V3 agent 在 InterruptSignal 捕获后 yield 此事件，chat() 负责弹窗 + 获取输入
                # 下次外层 while 迭代时用 resume_payload 调 stream_invoke(resume_payload=...) 继续
                if event_type == "human_approval_request":
                    _stop_live()
                    action = event.get("action", "")
                    details = event.get("details", "")
                    console.print(Panel(
                        f"[yellow]操作:[/yellow] {action}\n[yellow]详情:[/yellow] {details}",
                        title="🔒 等待人工审批",
                        border_style="yellow",
                        padding=(0, 1),
                    ))
                    # 审批输入期间暂停键盘线程：不暂停会与 Prompt.ask 竞争
                    # 读 stdin——用户输入 approve 里的 r 被后台线程吃成 toggle。
                    # EOF/Interrupt 取消路径直接返回，不再重启（finally 兜底）。
                    _pause_key_thread()
                    try:
                        resume_payload = Prompt.ask(
                            "[bold]请输入审批结果[/bold]\n  [green]approve[/green] = 批准\n  [red]reject:原因[/red] = 拒绝并附带原因\n  [dim]其他任何输入 = 拒绝（内容作为原因）[/dim]",
                            default="reject",
                        )
                    except (EOFError, KeyboardInterrupt):
                        # P2（二轮审查）：审批输入流尽/中断 = 取消本轮（与
                        # /mode 同语义）——干净回到主循环/退出。此前 EOFError
                        # 冲到下面的 except Exception 被吃成"生成回复时出错"
                        # 错误面板。中断快照留在 store（会话审批状态不因输入
                        # 流问题丢失），显式关闭生成器释放 agent 侧状态。
                        console.print("\n  [dim]（审批输入已取消，本轮暂停在审批上）[/dim]\n")
                        try:
                            stream.close()  # noqa: B023 - stream 在 while 内赋值，中断时必已存在
                        except Exception:
                            pass
                        return response
                    _ensure_key_thread()  # 恢复轮重入前重建键盘线程（r 键继续可用）
                    interrupted = True
                    break  # 跳出内层 for，外层 while 用 resume_payload 重入

                # ---- 记忆检索事件 ----
                # 2026-06-14: 非 debug 模式下也能看到"检索发生了 + 命中多少"
                if event_type == "memory_search":
                    _stop_live()
                    console.print(_build_memory_search_panel(event))

                # ---- 推理事件：滚动预览（默认）/ 单行折叠（r 键切换） ----
                # 与 /think 解耦：所有轮次只要模型吐推理就预览；/think 只控制
                # 模型是否思考。结束时由 _finalize_reasoning_live 折叠成一行。
                elif event_type == "reasoning_token":
                    content = event.get("content", "")
                    if content:
                        reasoning_buf.append(content)
                    # r 键切换已在主循环顶部消费（与事件类型无关），此处只管
                    # 按当前显示模式渲染/节流重绘
                    full = "".join(reasoning_buf)
                    reasoning_token_seen += 1
                    if reasoning_display == "preview":
                        if live is None or live_mode != "reasoning":
                            _start_live(_make_reasoning_panel(full,
                                tail_lines=max(4, console.size[1] // 4)))
                            live_mode = "reasoning"
                        elif reasoning_token_seen % 4 == 0:  # 节流重绘
                            live.update(_make_reasoning_panel(full,
                                tail_lines=max(4, console.size[1] // 4)), refresh=True)
                    else:  # collapsed：单行也走 Live（字数增长可见）
                        if reasoning_token_seen % 8 == 0:
                            if live is None or live_mode != "reasoning":
                                _start_live(_make_reasoning_collapsed_line(full))
                                live_mode = "reasoning"
                            else:
                                live.update(_make_reasoning_collapsed_line(full), refresh=True)

                # ---- 自动压缩提示（M2）：上下文被替换必须让用户知情 ----
                elif event_type == "auto_compact":
                    _stop_live()
                    d = event.get("compacted_count")
                    o = event.get("original_count")
                    console.print(f"  [dim]🔄 上下文已自动压缩（{d} 条早期消息 → 摘要，原 {o} 条）[/dim]")

                # ---- 审批结果反馈（M2）：批准/拒绝后给一行确认，慢工具不再像挂死 ----
                elif event_type == "approval_result":
                    d = event or {}
                    if d.get("decision") == "approve":
                        console.print(f"  [green]✅ 已批准执行 {d.get('tool_name', '工具')}[/green]")
                    else:
                        reason = d.get("reason") or ""
                        console.print(f"  [red]❌ 已拒绝执行 {d.get('tool_name', '工具')}{('：' + reason) if reason else ''}[/red]")

                # ---- Token 事件：流式 AI 文本 ----
                elif event_type == "token":
                    content = event.get("content", "")
                    segment_chars += len(content)

                    if not text_started:
                        # 推理→正文切换：冻结推理面板保留在终端（thinking 模式）
                        if live_mode == "reasoning":
                            _finalize_reasoning_live()
                        if not current_text and not content.strip():
                            continue  # 仍是前导空白
                        text_started = True

                    token_count += 1

                    # L4：先判分段再追加本 delta——旧顺序冻结面板已含本 token、
                    # 新段又以同 token 起步，边界字符会渲染两次
                    need_check = (token_count % 8 == 0) or (segment_chars > 200 and token_count % 3 == 0)
                    if need_check and len(current_text) > 100:
                        est = _estimate_lines(current_text, max(console_width - 6, 20))
                        if est >= max_panel_lines:
                            _freeze_and_start_new_segment()
                            current_text = content
                            segment_chars = len(content)
                        else:
                            current_text += content
                    else:
                        current_text += content

                    panel = _make_text_panel(current_text, is_draft=True)

                    if live is None or live_mode != "text":
                        _stop_live()
                        live_mode = "text"
                        _start_live(panel)
                    else:
                        if token_count % 3 == 0 or token_count <= 1:
                            live.update(panel, refresh=True)

                # ---- Tool Start 事件 ----
                elif event_type == "tool_start":
                    tool_id = event.get("tool_id", f"_auto_{len(tool_index)}")
                    tool_name = event.get("tool_name", "")
                    tool_args = event.get("tool_args", {})
                    is_subagent = tool_name == "task"

                    if text_started and current_text.strip():
                        _finalize_text_live()
                    else:
                        if live is not None:
                            live.update(Text(""), refresh=True)
                        _stop_live()

                    tool_info = {
                        "name": tool_name, "args": tool_args,
                        "status": "running", "result": "",
                        "is_subagent": is_subagent, "text": "",
                    }
                    tool_index[tool_id] = tool_info

                    current_text = ""
                    text_started = False
                    segment_chars = 0
                    token_count = 0

                    if live is not None and live_mode and live_mode != "text":
                        concurrent_mode = True
                        _stop_live()
                    elif concurrent_mode:
                        pass
                    else:
                        live_mode = tool_id
                        _start_live(_build_tool_panel(tool_info))

                # ---- Tool End 事件 ----
                elif event_type == "tool_end":
                    tool_id = event.get("tool_id", f"_end_{len(tool_index)}")
                    tool_name = event.get("tool_name", "")
                    result = event.get("result", "")

                    if tool_id in tool_index:
                        tool_index[tool_id]["status"] = "done"
                        tool_index[tool_id]["result"] = result
                        tool_info = tool_index[tool_id]
                    else:
                        tool_info = {
                            "name": tool_name, "args": {},
                            "status": "done", "result": result,
                            "is_subagent": False, "text": "",
                        }
                        tool_index[tool_id] = tool_info

                    if concurrent_mode:
                        console.print(_build_tool_panel(tool_info))
                        pending = [tid for tid, t in tool_index.items() if t["status"] == "running"]
                        if not pending:
                            concurrent_mode = False
                    elif live is not None and live_mode == tool_id:
                        live.update(_build_tool_panel(tool_info), refresh=True)
                        _stop_live()
                    else:
                        console.print(_build_tool_panel(tool_info))

                # ---- Turn Messages 事件 ----
                # 2026-06-16: 用 last_turn_msg_count 去重
                # partial（中断）和 final（完成）都走同一逻辑
                elif event_type == "messages_snapshot":
                    # compact 全量快照：整体替换 session_messages（修复压缩不落盘）
                    # F2：turn_messages 增量、snapshot 全量——last_turn_count 记为
                    # len(snapshot)，后续增量重放时 del[-N:]+extend 替换快照内容
                    snapshot_msgs = event.get("messages", [])
                    # H1：必须原地变异——重绑定局部名会让 main() 持有的列表
                    # 永远停留在压缩前（压缩不落盘、每轮重复触发摘要、上下文无限膨胀）
                    session_messages[:] = list(snapshot_msgs)
                    last_turn_msg_count = len(snapshot_msgs)
                elif event_type == "turn_messages":
                    turn_msgs = event.get("messages", [])
                    if last_turn_msg_count > 0:
                        del session_messages[-last_turn_msg_count:]
                    session_messages.extend(turn_msgs)
                    last_turn_msg_count = len(turn_msgs)

                # ---- Todos Update 事件 ----
                # R2-10：todos 从事件流捕获（旧主循环轮末调 agent 的
                # get_todos stub——恒 []——会清空 current_todos）。
                # P3（二轮审查）：实时原地写回调用方列表——resume 重入
                # stream_invoke 时传入的就是最新 todos（此前只在 chat 结束
                # 的 finally 回写，审批恢复轮拿到轮初陈旧待办）
                elif event_type == "todos_update":
                    if isinstance(todos, list):
                        todos[:] = list(event.get("todos") or [])

                # ---- Complete 事件 ----
                elif event_type == "complete":
                    response = event.get("content", "")

                    # 纯推理轮（无正文/工具触发的 finalize）：补折叠行
                    if live is not None and live_mode == "reasoning" and reasoning_buf:
                        _finalize_reasoning_live()
                    if text_started and current_text.strip():
                        if text_finalized:
                            pass
                        elif live is not None and live_mode == "text":
                            live.update(_make_text_panel(current_text, is_draft=False), refresh=True)
                            _stop_live()
                        elif segmented:
                            # 2026-06-22: 修复 Bug 13 —— 已分段的回复，每段都在流式
                            # 过程中冻结展示了。此时 current_text 只是某段尾的残留
                            # （live 已被 _stop_live），不应再单独打印一个只含尾段的
                            # 小框，否则视觉上回复会被切成两半。
                            pass
                        else:
                            console.print(_make_text_panel(current_text, is_draft=False))
                    elif response and response.strip():
                        _stop_live()
                        console.print(Panel(
                            Markdown(response),
                            title="🤖 HermesMa", border_style="cyan",
                            padding=(0, 1), expand=False,
                        ))
                    else:
                        _stop_live()
                        console.print(Panel(
                            "[red]⚠️ 未收到模型响应，请检查日志或重试。[/red]",
                            title="🤖 HermesMa", border_style="red",
                            padding=(0, 1), expand=False,
                        ))
                        response = "⚠️ 未收到模型响应，请重试。"

            # 2026-06-16: 事件循环结束，判断是否因中断退出
            if not interrupted:
                break
    except KeyboardInterrupt:
        # M1：Ctrl+C 不能丢整轮——把本轮 user 消息与已生成部分补进历史
        # （agent 的 turn_messages 通常尚未发出，不补的话保存的会话里
        #  这轮问题凭空消失）；并显式关闭生成器，让 agent 侧收到 GeneratorExit
        try:
            stream.close()  # noqa: B023 - stream 在 while 内赋值，中断时必已存在
        except Exception:
            pass
        if live is not None and text_started and current_text.strip():
            _stop_live()
        else:
            _stop_live()
        partial = current_text if current_text.strip() else ""
        try:
            if session_messages and session_messages[-1] == {"role": "user", "content": user_input}:
                pass  # resume 轮：user 消息已在历史
            else:
                session_messages.append({"role": "user", "content": user_input})
            if partial:
                session_messages.append({"role": "assistant", "content": partial})
        except Exception:
            logger.warning("中断补写历史失败（忽略）", exc_info=True)
        console.print(f"  [dim]（响应已被 Ctrl+C 中断，已保留已生成内容）[/dim]")
        response = partial if partial else "（响应被中断）"

    except Exception as e:
        _stop_live()
        console.print(Panel(
            f"[red]⚠️ 生成回复时出错: {escape(str(e))}[/red]",
            title="🤖 HermesMa",
            border_style="red",
            padding=(0, 1),
        ))
        logger.error(f"对话错误: {e}", exc_info=True)
        response = "抱歉，生成回复时出现了错误。"
    finally:
        # 2026-06-18: 兜底清理 Live，防止 Rich 全局状态残留导致下次 chat 冲突
        # (Only one live display may be active at once)
        _stop_live()
        # 停键盘监听线程（r 键切换仅生成期间有效；含审批暂停态的兜底）
        _pause_key_thread()
        # 推理全文回写（中断/异常轮也不丢）：供 main 的 /reasoning 回看
        if reasoning_store is not None:
            reasoning_store[:] = ["".join(reasoning_buf)]

    console.print()

    return response
