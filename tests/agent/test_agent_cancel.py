"""协作式取消（停止按钮真停得下来）+ 权限模式轮间热刷新。

背景：历史上取消只在 worker drain 循环的事件间隙生效——工具执行期/
首 token 前 agent 零事件，chat_stop 排队干等，worker 锁被占、重发消息
报"worker 忙"。现契约：agent 经 should_cancel 探针在轮间/chunk 流/
工具批量之间轮询，cancelled 轮干净收尾（⏹️标记 + cancelled turn/end +
跳过记忆沉淀）。
"""
import pytest

from src.agent.session_log import SessionLog, TURN_END
from src.llm.messages import Chunk
from src.storage import paths

from tests.agent.test_agent_events import (
    FakeExecutor,
    RecordingLLM,
    ROUND_TOOL,
    ROUND_TEXT,
    make_agent,
    make_tool_spec,
)


@pytest.fixture
def data_root(tmp_path):
    """数据根重定向到 tmp（SQLite 落临时库），测后恢复。"""
    paths.set_data_root(tmp_path)
    yield tmp_path
    paths.set_data_root(None)


def _agent_with_llm(monkeypatch, llm, specs=None, log=None):
    agent = make_agent(monkeypatch, specs=specs or [], session_log=log)
    monkeypatch.setattr(agent, "_get_llm_client", lambda: llm)
    return agent


def _run(agent, should_cancel=None, sid="sw"):
    return list(agent.stream_invoke(
        "u1", "查一下", session_messages=[], session_id=sid,
        thread_id=sid, should_cancel=should_cancel))


class FlipAfterFirstChunkLLM:
    """首个 chunk 消费后置位停止标志的假 LLM（模拟用户在流式中途点停止）。"""

    def __init__(self):
        self.flag = {"stop": False}

    def stream_chat(self, messages, tools=None, temperature=None, max_tokens=None):
        chunks = [Chunk(content_delta="A"), Chunk(content_delta="B"),
                  Chunk(content_delta="C")]

        def gen():
            for c in chunks:
                yield c
                self.flag["stop"] = True
        return gen()


class TestCooperativeCancel:

    def test_mid_stream_cancel_finalizes_with_marker(self, data_root, monkeypatch):
        """流式中途点停止：部分内容 + ⏹️ 标记收尾，complete 带 cancelled，
        turn_messages 照常流出（桶必须能收到部分答复）。"""
        llm = FlipAfterFirstChunkLLM()
        log = SessionLog()
        agent = _agent_with_llm(monkeypatch, llm, log=log)

        events = _run(agent, should_cancel=lambda: llm.flag["stop"])
        types = [e.get("type") for e in events]

        assert "turn_messages" in types and "complete" in types
        tm = next(e for e in events if e.get("type") == "turn_messages")
        assert tm.get("partial") is True
        comp = next(e for e in events if e.get("type") == "complete")
        assert comp.get("cancelled") is True
        assert "⏹️" in comp.get("content", "")
        # 停止后不再有第二轮 LLM 请求
        assert not [e for e in events if e.get("type") == "tool_start"]
        # durable 收轮带 cancelled 标记（回放/审计可见）
        turn_end = [e for e in log.events("sw") if e.get("type") == TURN_END]
        assert turn_end and turn_end[-1]["payload"].get("cancelled") is True

    def test_loop_top_cancel_leaves_visible_marker(self, data_root, monkeypatch):
        """首 token 前就点停止（探针恒真）：无内容也要落"⏹️（已停止）"
        标记消息，会话历史有可见的停止痕迹，收尾事件完整。"""
        log = SessionLog()
        llm = RecordingLLM(rounds=[ROUND_TEXT])
        agent = _agent_with_llm(monkeypatch, llm, log=log)

        events = _run(agent, should_cancel=lambda: True)
        comp = next(e for e in events if e.get("type") == "complete")
        assert comp.get("cancelled") is True
        assert "⏹️" in comp.get("content", "")
        # 一轮 LLM 都没跑
        assert llm.calls == []

    def test_cancelled_turn_without_probe_runs_normally(self, data_root, monkeypatch):
        """should_cancel 缺省（CLI 等调用方）：行为完全不变。"""
        llm = RecordingLLM(rounds=[ROUND_TEXT])
        agent = _agent_with_llm(monkeypatch, llm)
        events = _run(agent)
        types = [e.get("type") for e in events]
        assert "complete" in types
        comp = next(e for e in events if e.get("type") == "complete")
        assert "cancelled" not in comp


class TestPermissionModeHotRefresh:

    def test_mode_change_applies_next_round(self, data_root, monkeypatch):
        """对话进行中切权限模式：下一个 LLM 轮的工具 ctx 立即拿到新值，
        不必等下一条消息（历史行为：ctx 在 stream_invoke 开头固化）。"""
        seen_modes = []

        class CtxRecordingExecutor(FakeExecutor):
            def execute(self, args, ctx):
                seen_modes.append(ctx.permission_mode)
                return self.result

        spec = make_tool_spec(name="echo",
                              executor=CtxRecordingExecutor(content="ok"))
        llm = RecordingLLM(rounds=[ROUND_TOOL, ROUND_TOOL, ROUND_TEXT])
        agent = _agent_with_llm(monkeypatch, llm, specs=[spec])

        def flip_mode():
            # 首次工具执行后切换权限模式（模拟 worker 内联命令）
            if len(seen_modes) == 1:
                agent.set_permission_mode("full_access")
            return False

        _run(agent, should_cancel=flip_mode)
        assert seen_modes == ["before_changes", "full_access"]


class TestRegistryBetweenToolsCancel:

    @staticmethod
    def _serial_registry():
        """双工具 registry，强制走串行路径（批量并发路径不受取消影响：
        工具已在并行执行，轮间 cancel 探针负责在下一轮前终止）。"""
        from src.agent.registry_v3 import ToolRegistryV3
        reg = ToolRegistryV3()

        executed = []

        class RecExecutor(FakeExecutor):
            def execute(self, args, ctx):
                executed.append(True)
                return self.result

        reg.bind_tools([
            make_tool_spec(name="t1", executor=RecExecutor(content="一")),
            make_tool_spec(name="t2", executor=RecExecutor(content="二")),
        ])
        return reg, executed

    def test_cancel_before_batch_skips_all(self, data_root, monkeypatch):
        from src.agent.registry_v3 import ToolRegistryV3
        from src.tools.context import ToolContext
        reg, executed = self._serial_registry()
        monkeypatch.setattr(ToolRegistryV3, "_contains_approval_tool",
                            lambda self, calls: True)  # 强制串行
        ctx = ToolContext(permission_mode="before_changes",
                          should_cancel=lambda: True)
        msgs, _ = reg.process_tool_calls([
            {"id": "c1", "name": "t1", "args": {}},
            {"id": "c2", "name": "t2", "args": {}},
        ], ctx)
        assert executed == []
        assert [m.tool_call_id for m in msgs] == ["c1", "c2"]
        assert all("已停止" in m.content for m in msgs)

    def test_cancel_after_first_skips_rest(self, data_root, monkeypatch):
        from src.agent.registry_v3 import ToolRegistryV3
        from src.tools.context import ToolContext
        reg, executed = self._serial_registry()
        monkeypatch.setattr(ToolRegistryV3, "_contains_approval_tool",
                            lambda self, calls: True)  # 强制串行
        ctx = ToolContext(permission_mode="before_changes",
                          should_cancel=lambda: len(executed) >= 1)
        msgs, _ = reg.process_tool_calls([
            {"id": "c1", "name": "t1", "args": {}},
            {"id": "c2", "name": "t2", "args": {}},
        ], ctx)
        assert len(executed) == 1, "取消后第二个工具不应被执行"
        assert [m.tool_call_id for m in msgs] == ["c1", "c2"]
        assert msgs[0].content == "一"
        assert "已停止" in msgs[1].content
