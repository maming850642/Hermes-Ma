"""
R2-10 todos 不再被清空 —— worker drain / CLI chat 从事件流捕获 todos。

背景：worker_process._op_chat 在轮末执行 `bucket.todos = agent.get_todos()`，
而 V3 的 get_todos() 是恒返回 [] 的 stub → 每轮把桶里的 todos 清空；CLI 的
`current_todos = agent.get_todos()` 同型。修复后 todos 只从 todos_update 事件
payload 捕获，get_todos 不再被消费（stub 保留）。
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest

import src.agent  # noqa: F401  import 环兜底
from src.storage import paths


@pytest.fixture
def data_root(tmp_path):
    paths.set_data_root(tmp_path)
    yield tmp_path
    paths.set_data_root(None)


def make_agent_with_todos_tool(monkeypatch, todos_payload):
    """构造真实 HermesAgentV3：echo 工具返回 state_updates={"todos": ...}。"""
    from src.agent.agent_v3 import HermesAgentV3
    from src.agent.registry_v3 import ToolRegistryV3
    from src.agent.tool_result import ToolResult
    from src.llm.messages import Chunk
    from src.tools.executor_base import ToolExecutor
    from src.tools.schema import SideEffects, ToolSpec

    class FakeExecutor(ToolExecutor):
        def __init__(self, result):
            self.result = result

        def execute(self, args, ctx) -> ToolResult:
            return self.result

    spec = ToolSpec(
        name="write_todos", description="test",
        parameters={"type": "object", "properties": {}},
        executor=FakeExecutor(ToolResult(content="已记录", state_updates={"todos": todos_payload})),
        side_effects=SideEffects(destructive=False), se_evaluator=None,
    )

    mm = MagicMock(name="memory_manager")
    mm.search_with_detail.return_value = {
        "filtered_results": [], "raw_count": 0, "hit_count": 0,
    }

    import importlib
    agent_v3_mod = importlib.import_module("src.agent.agent_v3")
    monkeypatch.setattr(
        agent_v3_mod, "resolve_tools",
        lambda ctx, settings=None, include_mcp=True: [spec],
    )

    class RecordingLLM:
        def __init__(self):
            turn = [
                [Chunk(tool_call_deltas=[{
                    "index": 0, "id": "call_1",
                    "function": {"name": "write_todos", "arguments": "{}"},
                }])],
                [Chunk(content_delta="好的")],
            ]
            self.rounds = turn + turn  # 两轮对话各消耗一组

        def stream_chat(self, messages, tools=None, temperature=None, max_tokens=None):
            return iter(list(self.rounds.pop(0)))

    with patch("src.tools.remember.set_memory_manager"):
        agent = HermesAgentV3(mm, registry=ToolRegistryV3())
    agent._llm_client = RecordingLLM()
    return agent, spec


# ════════════════════════════════════════════════════════════════
# worker：_drain_stream_events 捕获 todos_update → bucket.todos
# ════════════════════════════════════════════════════════════════

class TestWorkerTodosStream:

    def test_drain_captures_todos_update_into_bucket(self, data_root):
        """drain 遇 todos_update 事件 → 写入 bucket.todos（转发照旧）。"""
        import web_fastapi.worker_process as wp

        bucket = wp.SessionBucket("s1")
        stream = iter([
            {"type": "todos_update", "todos": [{"id": "1", "content": "t", "status": "pending"}]},
            {"type": "complete", "content": "done"},
        ])
        sent: list[dict] = []
        state = MagicMock(name="state")

        orig_send = wp._send
        try:
            wp._send = lambda msg: sent.append(msg)
            wp._drain_stream_events(state, "req1", stream, bucket)
        finally:
            wp._send = orig_send

        assert bucket.todos == [{"id": "1", "content": "t", "status": "pending"}]
        # 事件仍转发给前端
        assert any(m.get("type") == "event" and m.get("event") == "todos_update" for m in sent)

    def test_two_rounds_todos_preserved_in_bucket(self, data_root, monkeypatch):
        """两轮对话：第一轮工具写入 todos → 第二轮 stream_invoke 收到的初始
        todos 保持（不再被 get_todos() stub 清空）。"""
        import sys as _sys
        if not hasattr(_sys.stdin, "reconfigure"):
            _sys.stdin = MagicMock(reconfigure=lambda **kw: None)
        import web_fastapi.worker_process as wp

        todos_payload = [{"id": "1", "content": "买牛奶", "status": "pending"}]
        agent, spec = make_agent_with_todos_tool(monkeypatch, todos_payload)

        # 记录每次 stream_invoke 收到的 todos 入参（_op_chat 会调
        # set_llm_params 作废 _llm_client，故先抓住实例再 patch _get_llm_client）
        recording_llm = agent._llm_client
        seen_todos: list[list] = []
        real_stream_invoke = agent.stream_invoke

        def recording_invoke(*args, **kw):
            seen_todos.append(list(kw.get("todos") or []))
            return real_stream_invoke(*args, **kw)

        agent.stream_invoke = recording_invoke
        monkeypatch.setattr(agent, "_get_llm_client", lambda: recording_llm)

        bucket = wp.SessionBucket("sw")
        state = MagicMock(name="worker-state")
        state.user_id = "u1"
        state.prefs = {"temperature": 0.7, "max_tokens": 2000, "compact_threshold_pct": 80}
        state.current_sid = "sw"
        state.get_bucket = lambda sid: bucket
        state.agent = agent
        state._save_bucket = lambda b: None

        monkeypatch.setattr(wp, "_send", lambda msg: None)

        wp._op_chat(state, "req1", {
            "id": "req1", "op": "chat", "message": "记一下", "session_id": "sw",
        })
        # 第一轮：初始 todos 为空，工具写入后桶里应有 todos（事件流捕获）
        assert seen_todos[0] == []
        assert bucket.todos == todos_payload

        # 第二轮：传入的初始 todos 保持第一轮的结果（回归点：旧实现此处被
        # get_todos() 清空成 []）
        wp._op_chat(state, "req2", {
            "id": "req2", "op": "chat", "message": "然后呢", "session_id": "sw",
        })
        assert seen_todos[1] == todos_payload

    def test_op_chat_no_longer_consumes_get_todos(self, data_root, monkeypatch):
        """_op_chat 不再调用 agent.get_todos()（stub 恒 []，旧路径每轮清空）。"""
        import sys as _sys
        if not hasattr(_sys.stdin, "reconfigure"):
            _sys.stdin = MagicMock(reconfigure=lambda **kw: None)
        import web_fastapi.worker_process as wp

        todos_payload = [{"id": "9", "content": "x", "status": "done"}]
        agent, spec = make_agent_with_todos_tool(monkeypatch, todos_payload)
        monkeypatch.setattr(agent, "_get_llm_client", lambda: agent._llm_client)

        def _explode():
            raise AssertionError("get_todos 不应再被 _op_chat 消费")

        agent.get_todos = _explode

        bucket = wp.SessionBucket("sx")
        bucket.todos = [{"id": "old", "content": "旧任务", "status": "pending"}]
        state = MagicMock(name="worker-state")
        state.user_id = "u1"
        state.prefs = {}
        state.current_sid = "sx"
        state.get_bucket = lambda sid: bucket
        state.agent = agent
        state._save_bucket = lambda b: None
        monkeypatch.setattr(wp, "_send", lambda msg: None)

        wp._op_chat(state, "req1", {
            "id": "req1", "op": "chat", "message": "记一下", "session_id": "sx",
        })
        # 第一轮没有 todos_update（工具才有）→ 桶保持原值，不被清空
        assert bucket.todos == [{"id": "old", "content": "旧任务", "status": "pending"}]


# ════════════════════════════════════════════════════════════════
# CLI：chat() 从事件流捕获 todos（原主循环 get_todos() 赋值删除）
# ════════════════════════════════════════════════════════════════

class TestCliChatTodosStream:

    def test_chat_captures_todos_update_into_caller_list(self, monkeypatch):
        """chat() 消费 todos_update 事件 → 原地更新调用方传入的 todos 列表
        （主循环的 current_todos 跨轮保持）。"""
        from rich.console import Console
        import src.cli as cli_mod

        buf = io.StringIO()
        monkeypatch.setattr(cli_mod, "console", Console(file=buf, force_terminal=False, width=120))

        todos_payload = [{"id": "1", "content": "买牛奶", "status": "pending"}]
        agent = MagicMock(name="agent")
        agent.stream_invoke.return_value = iter([
            {"type": "todos_update", "todos": todos_payload},
            {"type": "turn_messages", "messages": [
                {"role": "user", "content": "记一下"},
                {"role": "assistant", "content": "好的"},
            ], "partial": False},
            {"type": "complete", "content": "好的"},
        ])
        agent.get_todos.side_effect = AssertionError("get_todos 不应再被 chat 主循环消费")

        session_messages: list = []
        current_todos: list = []

        cli_mod.chat(agent, "u1", "记一下", session_messages,
                     session_id="s1", todos=current_todos, virtual_fs={})

        # todos_update 事件 payload 原地写进调用方的列表
        assert current_todos == todos_payload
        # stream_invoke 收到的 todos 是同一个列表对象（跨轮共享）
        kw = agent.stream_invoke.call_args.kwargs
        assert kw.get("todos") is current_todos

    def test_chat_without_todos_update_keeps_list(self, monkeypatch):
        """无 todos_update 事件的轮次：todos 列表保持不变（不被清空）。"""
        from rich.console import Console
        import src.cli as cli_mod

        buf = io.StringIO()
        monkeypatch.setattr(cli_mod, "console", Console(file=buf, force_terminal=False, width=120))

        agent = MagicMock(name="agent")
        agent.stream_invoke.return_value = iter([
            {"type": "complete", "content": "ok"},
        ])

        current_todos = [{"id": "1", "content": "旧任务", "status": "pending"}]
        cli_mod.chat(agent, "u1", "你好", [], session_id="s1",
                     todos=current_todos, virtual_fs={})
        assert current_todos == [{"id": "1", "content": "旧任务", "status": "pending"}]

    def test_main_loop_no_get_todos_assignment(self):
        """cli 包主循环不再出现 `agent.get_todos()` 赋值（源码锁）。"""
        import inspect
        from pathlib import Path

        import src.cli as cli_mod

        # P2 拆包后 cli 是包：扫描包内全部模块（getsource(包) 只读 __init__）
        pkg_dir = Path(inspect.getsourcefile(cli_mod)).parent
        sources = "".join(
            p.read_text(encoding="utf-8") for p in sorted(pkg_dir.glob("*.py"))
        )
        assert "agent.get_todos()" not in sources, (
            "cli 不应再消费 agent.get_todos()（stub 恒 []，会清空 current_todos）"
        )
