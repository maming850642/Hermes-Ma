"""
CLI chat() 历史一致性回归测试（审查 H1+H2）。

H1：messages_snapshot 必须原地变异 session_messages——重绑定局部名会让
    main() 持有的列表停在压缩前（压缩不落盘、每轮重复触发摘要）。
H2：审批恢复轮（外层 while 重入 stream_invoke）必须重置 last_turn_msg_count
    ——跨流残留的计数会让 resume 增量轮 del[-N:] 清光全部历史。

用假 agent 按真实事件序列（V3 契约：turn_messages 一律增量、
messages_snapshot 一律全量）驱动 chat()，断言主列表终态精确形状。
"""
from unittest.mock import MagicMock, patch

import os
import sys
import threading

import pytest

from src.storage import paths


@pytest.fixture(autouse=True)
def _isolated_data_root(tmp_path):
    paths.set_data_root(tmp_path)
    yield
    paths.set_data_root(None)


class FakeAgent:
    """按脚本逐轮 yield 事件的假 agent（记录收到的 session_messages 快照）。"""

    def __init__(self, rounds):
        # rounds: list[list[dict]] —— 每次调用 stream_invoke 消费一轮事件
        self._rounds = list(rounds)
        self.calls = []  # 每轮收到的 session_messages 引用拷贝
        self.kwargs_by_call: list[dict] = []  # 每轮收到的完整 kwargs

    def stream_invoke(self, *args, **kwargs):
        self.calls.append(list(kwargs.get("session_messages") or args[2] if len(args) > 2 else []))
        self.kwargs_by_call.append(dict(kwargs))
        events = self._rounds.pop(0)
        for ev in events:
            yield ev


def _run_chat(agent, session_messages, rounds_needed=None):
    """跑一次 chat()（屏蔽 Rich 渲染），返回响应文本。"""
    from src import cli
    with patch.object(cli, "console") as mock_console, \
         patch.object(cli, "Live"), \
         patch.object(cli, "Prompt"), \
         patch.object(cli.console, "print"):
        mock_console.size = MagicMock()
        mock_console.size.__getitem__ = lambda s, i: (100, 40)[i]
        resp = cli.chat(agent, "local", "问题", session_messages, session_id="s1")
    return resp


def test_compact_snapshot_mutates_in_place():
    """H1：snapshot 事件后主列表被原地替换（id 不变、内容=压缩快照）。"""
    history = [{"role": "user", "content": "旧1"}, {"role": "assistant", "content": "旧2"}]
    compacted = [{"role": "system", "content": "摘要"}]
    agent = FakeAgent([[
        {"type": "auto_compact", "compacted_count": 2, "original_count": 2},
        {"type": "messages_snapshot", "messages": compacted},
        {"type": "token", "content": "答"},
        {"type": "turn_messages", "messages": compacted + [{"role": "user", "content": "问题"},
                                                           {"role": "assistant", "content": "答"}],
         "partial": False},
        {"type": "complete", "content": "答"},
    ]])
    _run_chat(agent, history)
    # 主列表被原地变异：既不是旧历史，也包含新增量（快照被同轮增量替换）
    assert history == [
        {"role": "system", "content": "摘要"},
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "答"},
    ]


def test_hitl_resume_round_does_not_wipe_history():
    """H2：中断轮 snapshot 设大计数 → resume 轮小增量，不得清光历史。"""
    history = [
        {"role": "user", "content": "旧1"},
        {"role": "assistant", "content": "旧2"},
        {"role": "user", "content": "问题"},
    ]
    snapshot = [{"role": "system", "content": "摘要"}, {"role": "user", "content": "问题"}]
    round1 = [
        {"type": "messages_snapshot", "messages": snapshot},          # 计数=2
        {"type": "human_approval_request", "action": "bash", "details": "rm", "thread_id": "s1"},
    ]
    # resume 轮：增量只含本轮新增（V3 turn_baseline=当前桶长）
    round2 = [
        {"type": "approval_result", "decision": "approve", "tool_name": "bash"},
        {"type": "tool_start", "tool_id": "t1", "tool_name": "bash", "tool_args": {}},
        {"type": "tool_end", "tool_id": "t1", "tool_name": "bash", "result": "ok"},
        {"type": "turn_messages", "messages": [
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "t1", "type": "function",
                "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "ok"},
            {"role": "assistant", "content": "完成"},
        ], "partial": False},
        {"type": "complete", "content": "完成"},
    ]
    agent = FakeAgent([round1, round2])

    from src import cli
    # 第一轮结束在 human_approval_request，Prompt.ask mock 返回 approve → 外层 while 重入
    with patch.object(cli, "console") as mock_console, \
         patch.object(cli, "Live"), \
         patch.object(cli, "Prompt") as mock_prompt, \
         patch.object(cli.console, "print"):
        mock_console.size = MagicMock()
        mock_console.size.__getitem__ = lambda s, i: (100, 40)[i]
        mock_prompt.ask.return_value = "approve"
        cli.chat(agent, "local", "问题", history, session_id="s1")

    # H2 修复前：resume 轮 del[-2:] 会删掉 snapshot 两条，历史只剩增量
    assert history == [
        {"role": "system", "content": "摘要"},
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "t1", "type": "function",
            "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
        {"role": "assistant", "content": "完成"},
    ]


def test_keyboard_interrupt_preserves_turn():
    """M1：Ctrl+C 中断时 user 消息与已生成部分补进历史（不再丢整轮）。"""
    history = []

    class InterruptingAgent:
        def stream_invoke(self, *a, **k):
            yield {"type": "token", "content": "部分回"}
            raise KeyboardInterrupt()

    from src import cli
    with patch.object(cli, "console") as mock_console, \
         patch.object(cli, "Live"), \
         patch.object(cli.console, "print"):
        mock_console.size = MagicMock()
        mock_console.size.__getitem__ = lambda s, i: (100, 40)[i]
        cli.chat(InterruptingAgent(), "local", "我的问题", history, session_id="s1")

    assert {"role": "user", "content": "我的问题"} in history
    assert {"role": "assistant", "content": "部分回"} in history


def test_chat_passes_only_real_stream_invoke_kwargs():
    """L8 回归：chat() 传给 stream_invoke 的关键字参数必须与真实签名可绑定。

    （此 bug 的教训：FakeAgent 用 **kwargs 吞参数，签名不匹配测试照样绿，
    只有真实调用才炸——改用 inspect 对真签名做绑定校验。）
    """
    import inspect
    from src.agent.agent_v3 import HermesAgentV3
    from src import cli

    sig = inspect.signature(HermesAgentV3.stream_invoke)
    src_text = inspect.getsource(cli.chat)
    # 从源码中提取 stream_invoke(...) 调用的 kwargs（粗提取：k=v, 形式）
    import re
    call = re.search(r"agent\.stream_invoke\((.*?)\)\n", src_text, re.S).group(1)
    kwargs = re.findall(r"(\w+)=\w", call)
    for kw in kwargs:
        assert kw in sig.parameters, f"chat() 传了 stream_invoke 不存在的参数: {kw}"


# ════════════════════════════════════════════════════════════════
# P2-6：main() 主循环 except 链顺序 + 命令派发段 EOF/Interrupt 干净退出
# ════════════════════════════════════════════════════════════════

def _fake_ask(responses):
    """按脚本回放的 Prompt.ask 桩：str=正常回答，Exception=抛出。"""
    it = iter(responses)

    def _ask(*args, **kwargs):
        r = next(it)
        if isinstance(r, BaseException):  # KeyboardInterrupt 不是 Exception 子类
            raise r
        return r

    return _ask


def _boot_main(monkeypatch, responses, chat_impl=None, sessions=None,
               boot_ctx=None, agent_cls=None):
    """以最小桩驱动 cli.main()（屏蔽初始化/渲染），返回观测句柄。"""
    from src import cli

    observed = {"saved": [], "shutdown": 0}

    monkeypatch.setattr(cli, "show_logo", lambda: None)
    monkeypatch.setattr(cli, "run_health_check", lambda silent=False: True)
    monkeypatch.setattr(cli, "MemoryManager", lambda: MagicMock())
    monkeypatch.setattr("src.plugins.boot_context", lambda: boot_ctx)
    monkeypatch.setattr(cli, "HermesAgentV3",
                        agent_cls or MagicMock(name="agent-cls"))
    monkeypatch.setattr("src.tools.resolve.resolve_tools", lambda *a, **k: [])
    monkeypatch.setattr(cli, "describe_workspace", lambda: ("未配置", ""))
    monkeypatch.setattr(cli, "_migrate_old_sessions", lambda: None)
    monkeypatch.setattr(cli, "list_sessions",
                        lambda user_id: sessions if sessions is not None else [])
    monkeypatch.setattr(
        cli, "save_session",
        lambda *a, **k: observed["saved"].append({"args": a, "kwargs": k}),
    )
    monkeypatch.setattr(
        cli, "_shutdown_mcp_quietly", lambda agent: observed.__setitem__("shutdown", observed["shutdown"] + 1),
    )
    # MCP 后台连接桩（2026-09-10）：main() 启动会拉起 cli-mcp-connect 守护线程，
    # 异步把真实 mcp_servers/ 的 server 连进 get_client_manager() 进程级单例。
    # 真实连接要 spawn 子进程（秒级），常在本测试结束后才完成——单例被污染后，
    # 后续测试的工具面多出 mcp__* 工具（test_chat_only_mode_hides_fs_tools
    # 曾因此"单跑必过、全量必挂"）。这里把后台连接换成空操作：返回空结果表
    # → 不连任何 server、不触发 rebind，线程即启即退。
    class _StubMCPManager:
        def connect_enabled_all(self):
            return {}

    monkeypatch.setattr("src.mcp.client.get_client_manager",
                        lambda: _StubMCPManager())
    monkeypatch.setattr(cli, "chat", chat_impl or (lambda *a, **k: "ok"))
    monkeypatch.setattr(cli.Prompt, "ask", _fake_ask(responses))
    with patch.object(cli, "console") as mock_console:
        mock_console.size = MagicMock()
        mock_console.size.__getitem__ = lambda s, i: (100, 40)[i]
        observed["console"] = mock_console
        cli.main()  # 修复后：EOF/Interrupt 一律干净退出，绝不抛出
    return observed


def _printed(console, needle):
    """console.print 是否输出过含 needle 的文本。"""
    for call in console.print.call_args_list:
        for arg in call.args:
            if isinstance(arg, str) and needle in arg:
                return True
    return False


def test_main_chat_eof_exits_cleanly_with_save(monkeypatch):
    """P2-6：EOFError 在 chat 路径必须干净退出（此前被 except Exception
    先吃成"对话出错"），退出前先 save_session 再收尾关 MCP。"""
    observed = _boot_main(
        monkeypatch,
        responses=["你好", EOFError()],
        chat_impl=lambda *a, **k: (_ for _ in ()).throw(EOFError()),
    )

    assert _printed(observed["console"], "再见！会话已保存")
    assert not _printed(observed["console"], "对话出错")
    assert len(observed["saved"]) >= 1
    assert observed["shutdown"] == 1


def test_main_ctrl_c_saves_session_and_continues(monkeypatch):
    """P2-6：Ctrl+C 中断对话必须落盘（此前分支无 save_session），
    且循环继续到主提示符的下一次 EOF 干净退出。"""
    observed = _boot_main(
        monkeypatch,
        responses=["hi", EOFError()],
        chat_impl=lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    assert not _printed(observed["console"], "对话出错")
    # Ctrl+C 分支的 save（后续主输入 EOF 分支不再 save）
    assert len(observed["saved"]) == 1


def test_main_resume_menu_eof_exits_cleanly(monkeypatch):
    """P2-6：/resume 菜单 Prompt.ask 撞上 EOF → 取消选择 → 主提示符
    EOF 干净退出（此前异常冲出 main，收尾被跳过）。"""
    sessions = [{
        "session_id": "abc123", "name": "", "updated_at": "2026-01-01T00:00:00",
        "message_count": 1, "preview": "",
    }]
    observed = _boot_main(
        monkeypatch,
        responses=["/resume", EOFError(), EOFError()],
        sessions=sessions,
    )

    assert not _printed(observed["console"], "对话出错")
    assert not _printed(observed["console"], "Traceback")


def test_main_mode_prompt_interrupt_cancels(monkeypatch):
    """P2-6：/mode 确认输入 Ctrl+C = 取消切换回主提示符，不冲出 main。"""
    observed = _boot_main(
        monkeypatch,
        responses=["/mode", KeyboardInterrupt(), EOFError()],
    )

    assert not _printed(observed["console"], "对话出错")


# ════════════════════════════════════════════════════════════════
# 二轮审查 + CLI 实操：interrupt_store 接线 / 审批 EOF / 主提示符
# Ctrl+C / 手动 compact 事件 / resume 腿 todos 实时性
# ════════════════════════════════════════════════════════════════

def test_main_wires_interrupt_store_from_boot_ctx(monkeypatch):
    """P2（二轮审查第 7 项）：main() 构造 HermesAgentV3 必须传
    interrupt_store（绑定 boot_ctx.sessions + recover_into 恢复）——否则
    CLI 审批事件从不落库（/events 无审批轨迹、崩溃后无法恢复 pending
    审批）。对齐 worker_process 的接线。"""
    from types import SimpleNamespace

    from src.agent.hitl import InterruptStore, InterruptSnapshot
    from src.agent.session_log import SessionLog

    log = SessionLog()
    # 预置一个 pending 审批（模拟上次进程崩溃时留下的中断）
    log.save_interrupt(InterruptSnapshot.create(
        thread_id="t1",
        messages=[{"role": "user", "content": "hi"}],
        pending_args={},
        tool_call_id="c1",
        tool_name="bash",
        payload={"action": "a", "details": "b"},
        permission_mode="before_changes",
    ))

    boot = SimpleNamespace(
        get=lambda name: SimpleNamespace(registry=object()) if name == "tools" else None,
        try_get=lambda name: log if name == "sessions" else None,
        teardown=lambda: None,
    )
    captured: dict = {}

    def fake_agent_cls(memory_manager, **kwargs):
        captured.update(kwargs)
        return MagicMock(name="agent")

    _boot_main(
        monkeypatch,
        responses=["/exit"],
        boot_ctx=boot,
        agent_cls=fake_agent_cls,
    )

    store = captured.get("interrupt_store")
    assert isinstance(store, InterruptStore)
    assert store._log is log, "interrupt_store 必须绑定 sessions（审批事件才落库）"
    # 崩溃前 pending 的审批被 recover_into 恢复进内存 store
    assert store.has_pending("t1")
    assert captured["kernel_ctx"] is boot


def test_approval_prompt_eof_cancels_round_without_error_panel():
    """P2（二轮审查第 8 项）：审批提示符输入流尽（管道 EOF）→ 取消本轮
    干净返回，不再落"生成回复时出错"错误面板（此前 EOFError 被 chat 的
    except Exception 吃掉显示错误面板）。"""
    round1 = [{"type": "human_approval_request", "action": "bash",
               "details": "rm -rf /tmp/x", "thread_id": "s1"}]
    agent = FakeAgent([round1])
    history: list = []

    from src import cli
    with patch.object(cli, "console") as mock_console, \
         patch.object(cli, "Live"), \
         patch.object(cli, "Prompt") as mock_prompt, \
         patch.object(cli.console, "print"):
        mock_console.size = MagicMock()
        mock_console.size.__getitem__ = lambda s, i: (100, 40)[i]
        mock_prompt.ask.side_effect = EOFError()
        resp = cli.chat(agent, "local", "问题", history, session_id="s1")

    assert resp == ""
    assert not _printed(mock_console, "生成回复时出错")


def test_main_approval_eof_clean_exit(monkeypatch):
    """P2（二轮审查第 8 项）主循环集成：审批处 EOF → chat 取消本轮 →
    主提示符 EOF 干净退出（退出码 0 路径：save + 收尾，无错误面板）。"""
    from src import cli

    fake = FakeAgent([[{"type": "human_approval_request", "action": "a",
                        "details": "d", "thread_id": "s1"}]])
    observed = _boot_main(
        monkeypatch,
        responses=["你好", EOFError(), EOFError()],
        chat_impl=cli.chat,  # 真 chat（经 FakeAgent 驱动）
        agent_cls=lambda *a, **k: fake,
    )

    assert not _printed(observed["console"], "生成回复时出错")
    assert not _printed(observed["console"], "对话出错")
    # chat 正常返回后走常规落盘，主提示符 EOF 顶层 break 干净退出（不报错）
    assert len(observed["saved"]) >= 1


def test_main_ctrl_c_at_prompt_cancels_and_continues(monkeypatch):
    """P2（二轮审查第 9 项）：主提示符 Ctrl+C 不再整进程崩溃（Windows
    STATUS_CONTROL_C_EXIT 跳过 save/teardown）——取消当前输入回主提示符，
    下一次 EOF 干净退出。"""
    observed = _boot_main(
        monkeypatch,
        responses=[KeyboardInterrupt(), EOFError()],
    )

    assert not _printed(observed["console"], "对话出错")
    assert not _printed(observed["console"], "Traceback")
    assert not _printed(observed["console"], "生成回复时出错")


def test_manual_compact_writes_compact_applied_and_fork_stays_compact(monkeypatch):
    """P3（二轮审查第 10 项）：手动 /compact 落定后必须写 compact/applied
    durable 事件（对齐 agent 的 _compact_messages）——否则 /fork 按事件流
    复制分支时投影重建出压缩前全量历史（压缩前消息复活）。"""
    from types import SimpleNamespace

    from src import cli
    from src.agent.context import CompactResult
    from src.agent.session_log import (
        SessionLog, COMPACT_APPLIED, USER_MSG, ASSISTANT_MSG,
    )

    log = SessionLog()
    # 源会话事件：2 轮对话（投影 4 条消息）
    log.append("s1", USER_MSG, {"content": "问1"})
    log.append("s1", ASSISTANT_MSG, {"content": "答1"})
    log.append("s1", USER_MSG, {"content": "问2"})
    log.append("s1", ASSISTANT_MSG, {"content": "答2"})

    session_messages = log.derive_messages("s1")
    assert len(session_messages) == 4

    class FakeCtxMgr:
        def should_auto_compact(self, messages):
            return True

        def compact_messages(self, messages):
            kept = [dict(m) for m in messages[-2:]]
            messages.clear()
            messages.append({"role": "system", "content": "摘要S"})
            messages.extend(kept)
            return CompactResult(
                compressed_messages=list(messages),
                summary="摘要S",
                original_count=4,
                compacted_count=2,
            )

    monkeypatch.setattr(cli, "ContextManager", FakeCtxMgr)

    boot = SimpleNamespace(get=lambda name: log if name == "sessions" else None)
    cli.compact_session(boot, "s1", session_messages)

    # compact/applied 事件落库，保留区 = 压缩后保留的近 2 条
    compact_events = [e for e in log.events("s1") if e["type"] == COMPACT_APPLIED]
    assert len(compact_events) == 1
    payload = compact_events[0]["payload"]
    assert payload["summary"] == "摘要S"
    assert payload["original_count"] == 4
    assert payload["compacted_count"] == 2
    assert payload["kept_messages"] == [
        {"role": "user", "content": "问2"},
        {"role": "assistant", "content": "答2"},
    ]

    # fork 分支：投影不含压缩前消息（只有摘要 + 保留区）
    new_sid = cli._cmd_fork(boot, "local", "s1", session_messages, "")
    assert new_sid
    derived = log.derive_messages(new_sid)
    assert derived == [
        {"role": "system", "content": "摘要S"},
        {"role": "user", "content": "问2"},
        {"role": "assistant", "content": "答2"},
    ]


def test_resume_round_receives_fresh_todos():
    """P3（二轮审查第 11 项）：同轮 todos_update 之后触发审批，resume 重入
    stream_invoke 必须拿到最新 todos（此前只在 chat 结束的 finally 回写，
    审批恢复轮传的是轮初陈旧值）。"""
    fresh_todos = [{"id": "1", "content": "新待办", "status": "pending"}]
    round1 = [
        {"type": "todos_update", "todos": fresh_todos},
        {"type": "human_approval_request", "action": "bash", "details": "rm",
         "thread_id": "s1"},
    ]
    round2 = [{"type": "complete", "content": "ok"}]
    agent = FakeAgent([round1, round2])
    todos: list = []

    from src import cli
    with patch.object(cli, "console") as mock_console, \
         patch.object(cli, "Live"), \
         patch.object(cli, "Prompt") as mock_prompt, \
         patch.object(cli.console, "print"):
        mock_console.size = MagicMock()
        mock_console.size.__getitem__ = lambda s, i: (100, 40)[i]
        mock_prompt.ask.return_value = "approve"
        cli.chat(agent, "local", "问题", [], session_id="s1", todos=todos)

    # resume 重入轮收到的 todos 是事件流里的最新值；调用方列表同步更新
    assert len(agent.kwargs_by_call) == 2
    assert agent.kwargs_by_call[1]["todos"] == fresh_todos
    assert todos == fresh_todos


# ════════════════════════════════════════════════════════════════
# r 键 toggle 消费回归（主循环顶部、与事件类型无关）：
#   1) 正文/工具阶段按 r → 静态打印一次推理尾部回看（修复前 flag 滞留，
#      正文/工具阶段没有 reasoning_token 事件 → 按键零响应）
#   2) 推理流式中按 r → Live 内立即预览↔折叠重绘
#   3) 无推理轮按 r → 忽略（不吃键、不打印、flag 清零防迟到误翻转）
# 键入经 sys.modules 注入假 msvcrt + Event 握手确定性驱动键盘线程。
# ════════════════════════════════════════════════════════════════

class _KeyInjectMsvcrt:
    """msvcrt 假件：press 置位后 kbhit()=True，getwch() 交付一次 'r' 并 ack。"""

    def __init__(self, press, ack):
        self._press = press
        self._ack = ack

    def kbhit(self):
        return self._press.is_set()

    def getwch(self):
        self._press.clear()
        self._ack.set()
        return "r"


@pytest.fixture
def key_injector(monkeypatch):
    """向 chat() 的键盘线程注入 r 键：返回 (press, ack) Event 对。"""
    press, ack = threading.Event(), threading.Event()
    monkeypatch.setitem(sys.modules, "msvcrt", _KeyInjectMsvcrt(press, ack))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    return press, ack


def _press_and_wait(press, ack, timeout=5.0):
    """注入一次 r 键并等键盘线程确认消费（超时断言失败防挂死）。"""
    ack.clear()
    press.set()
    assert ack.wait(timeout), "键盘线程未在限时内消费 r 键"


class HookAgent:
    """FakeAgent 变体：事件序列可插入 callable（事件间隙钩子）。"""

    def __init__(self, rounds):
        self._rounds = [list(r) for r in rounds]
        self.kwargs_by_call: list[dict] = []

    def stream_invoke(self, *args, **kwargs):
        self.kwargs_by_call.append(dict(kwargs))
        for item in self._rounds.pop(0):
            if callable(item):
                item()
            else:
                yield item


def _tail_panels(console):
    """console.print 打过的"推理回看"静态面板（按 title 标记识别）。"""
    return [arg for call in console.print.call_args_list for arg in call.args
            if hasattr(arg, "title") and "推理回看" in str(getattr(arg, "title", ""))]


@pytest.mark.skipif(os.name != "nt", reason="假 msvcrt 键盘注入仅覆盖 Windows 键盘线程路径")
def test_r_key_during_text_phase_prints_reasoning_tail_once(key_injector):
    """核心修复：正文流式阶段（推理面板已折叠成静态单行）按 r 必须在
    下一事件顶部立即消费——静态打印一次推理尾部回看。"""
    press, ack = key_injector
    agent = HookAgent([[
        {"type": "reasoning_token", "content": "思考A"},
        {"type": "reasoning_token", "content": "思考B"},
        {"type": "token", "content": "答"},   # 此事件处理中折叠推理面板
        lambda: _press_and_wait(press, ack),  # 正文流式中按 r
        {"type": "token", "content": "案"},   # 下一事件顶部消费 → 静态回看
        {"type": "turn_messages", "messages": [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "答案"}], "partial": False},
        {"type": "complete", "content": "答案"},
    ]])
    history: list = []

    from src import cli
    with patch.object(cli, "console") as mock_console, \
         patch.object(cli, "Live"):
        mock_console.size = MagicMock()
        mock_console.size.__getitem__ = lambda s, i: (100, 40)[i]
        resp = cli.chat(agent, "local", "问题", history, session_id="s1")

    # 注意：不要再 patch.object(cli.console, "print")——with 链里该表达式在
    # 第一个 patcher 进入后才求值，会把 mock_console.print 换成临时 mock 并
    # 在退出时恢复，录制随之丢失；MagicMock 的 .print 子属性本身就记录调用。
    assert resp == "答案"
    assert len(_tail_panels(mock_console)) == 1, "正文阶段按 r 应静态打印一次推理回看"
    assert history[-1] == {"role": "assistant", "content": "答案"}


@pytest.mark.skipif(os.name != "nt", reason="假 msvcrt 键盘注入仅覆盖 Windows 键盘线程路径")
def test_r_key_during_reasoning_stream_collapses_immediately(key_injector):
    """推理流式中按 r：主循环顶部立即把预览面板重绘为折叠单行
    （修复前必须等下一个 reasoning_token，token 停顿时按 r 零响应）。"""
    press, ack = key_injector
    agent = HookAgent([[
        {"type": "reasoning_token", "content": "想"},
        lambda: _press_and_wait(press, ack),
        {"type": "reasoning_token", "content": "更多"},
        {"type": "token", "content": "答"},
        {"type": "turn_messages", "messages": [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "答"}], "partial": False},
        {"type": "complete", "content": "答"},
    ]])
    history: list = []

    from rich.text import Text

    from src import cli
    with patch.object(cli, "console") as mock_console, \
         patch.object(cli, "Live") as MockLive:
        mock_console.size = MagicMock()
        mock_console.size.__getitem__ = lambda s, i: (100, 40)[i]
        cli.chat(agent, "local", "问题", history, session_id="s1")
        mock_live = MockLive.return_value

    # streaming 折叠单行只可能来自 r 键消费路径：token 仅 2 个走不到 %8
    # 节流，finalize 用的是非 streaming 形态（"🧠 推理 N 字 ·"）
    collapsed = [a for call in mock_live.update.call_args_list for a in call.args
                 if isinstance(a, Text) and a.plain.startswith("🧠 推理中")]
    assert collapsed, "按 r 后 Live 应立即收到折叠单行重绘"
    assert not _tail_panels(mock_console), "推理 Live 存活时 r 是切换而非静态回看"


@pytest.mark.skipif(os.name != "nt", reason="假 msvcrt 键盘注入仅覆盖 Windows 键盘线程路径")
def test_r_key_without_reasoning_is_ignored(key_injector):
    """无推理轮按 r：flag 消费即清零，不打印、不误触发任何面板。"""
    press, ack = key_injector
    agent = HookAgent([[
        lambda: _press_and_wait(press, ack),  # 第一个事件前按 r
        {"type": "token", "content": "答案"},
        {"type": "turn_messages", "messages": [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "答案"}], "partial": False},
        {"type": "complete", "content": "答案"},
    ]])
    history: list = []

    from src import cli
    with patch.object(cli, "console") as mock_console, \
         patch.object(cli, "Live"):
        mock_console.size = MagicMock()
        mock_console.size.__getitem__ = lambda s, i: (100, 40)[i]
        cli.chat(agent, "local", "问题", history, session_id="s1")

    assert not _tail_panels(mock_console)
    assert history[-1] == {"role": "assistant", "content": "答案"}
