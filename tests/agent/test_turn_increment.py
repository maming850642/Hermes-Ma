"""
F2 回归：turn_messages 一律增量、messages_snapshot 一律全量。

背景（2026-08-15 R1 深度 review）：
    agent_v3._finish_turn yield state["messages"] 全量；worker/CLI 的
    del[-last_turn_count:]+extend 只在「桶从空开始」时自洽——
    messages_snapshot（压缩）整体替换桶并清零 last_turn_count 后，
    下一个全量 turn_messages 把压缩前缀二次叠加，逐轮腐化。

契约（本文件锁定）：
    - turn_messages：本轮增量（自 stream 基线或上一张快照起算，可为空）；
    - messages_snapshot：全量权威状态（消费方整体替换，并把
      last_turn_count 记为 len(snapshot)，让后续增量重放替换快照）。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import src.agent  # noqa: F401
from src.agent.agent_v3 import HermesAgentV3
from src.tools.context import ToolContext


def _mk_agent() -> HermesAgentV3:
    """真实构造的 agent（LLM 客户端延迟，不连网）。"""
    return HermesAgentV3(MagicMock())


def _fake_loop(appender):
    """构造替代 _react_loop 的生成器：往 state['messages'] 追加后返回。"""
    def loop(state, ctx, tools, memory_pending=False):
        appender(state)
        return
        yield  # pragma: no cover（generator 语义占位）
    return loop


def _events(agent_stream) -> list:
    return list(agent_stream)


def _tm(msgs, partial=False):
    return {"type": "turn_messages", "messages": msgs, "partial": partial}


def _snap(msgs):
    return {"type": "messages_snapshot", "messages": msgs}


# ============================================
# Agent 层：turn_messages 增量
# ============================================

class TestAgentIncrement:

    def test_fresh_turn_yields_increment_only(self):
        """turn_messages 只含本轮新增（user + assistant），不含历史前缀。"""
        agent = _mk_agent()
        history = [
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "旧回答"},
        ]
        agent._react_loop = _fake_loop(
            lambda s: s["messages"].append({"role": "assistant", "content": "新回答"})
        )
        events = _events(agent.stream_invoke(
            "u1", "新问题", session_messages=[dict(m) for m in history],
        ))
        tm = [e for e in events if e["type"] == "turn_messages"]
        assert len(tm) == 1
        assert tm[0]["messages"] == [
            {"role": "user", "content": "新问题"},
            {"role": "assistant", "content": "新回答"},
        ], "turn_messages 必须是增量（不含传入的历史消息）"

    def test_finish_turn_without_baseline_yields_all(self):
        """防御：state 无 turn_baseline 键时按 0 处理（全量=增量）。"""
        agent = _mk_agent()
        agent._react_loop = _fake_loop(lambda s: None)
        state = {
            "messages": [{"role": "user", "content": "hi"}],
            "iteration_count": 0,
        }
        events = list(agent._finish_turn(state, ToolContext(), [], "t1"))
        tm = [e for e in events if e["type"] == "turn_messages"]
        assert tm[0]["messages"] == [{"role": "user", "content": "hi"}]

    def test_compact_midturn_snapshot_then_increment(self):
        """压缩后：snapshot 承担全量职责，turn_messages 从 0 基线重放。"""
        agent = _mk_agent()
        # 压缩结果：摘要 + 保留消息
        compressed = [
            {"role": "user", "content": "<summary>已压缩</summary>"},
            {"role": "assistant", "content": "压缩后保留的近期回复"},
        ]
        agent.context_manager = MagicMock()
        agent.context_manager.compact_messages.return_value = SimpleNamespace(
            compressed_messages=compressed,
            original_count=5,
            compacted_count=4,
            summary="已压缩",
        )

        def loop(state, ctx, tools, memory_pending=False):
            state["messages"].append({"role": "user", "content": "触发压缩"})
            yield from agent._compact_messages(state, auto=True)
            state["messages"].append({"role": "assistant", "content": "压缩后新答"})
            return

        agent._react_loop = loop
        events = _events(agent.stream_invoke("u1", "hi", session_messages=[]))
        snaps = [e for e in events if e["type"] == "messages_snapshot"]
        tms = [e for e in events if e["type"] == "turn_messages"]
        assert len(snaps) == 1
        assert snaps[0]["messages"] == compressed, "messages_snapshot 必须是全量"
        assert len(tms) == 1
        # 基线已被压缩重置为 0：增量 = 压缩后全量 + 后续新增
        assert tms[0]["messages"] == compressed + [
            {"role": "assistant", "content": "压缩后新答"}
        ]


# ============================================
# Worker 层：桶消费契约
# ============================================

class TestWorkerBucket:

    @pytest.fixture
    def drain(self, monkeypatch):
        """隔离 IPC/内联命令后的 _drain_stream_events。"""
        import web_fastapi.worker_process as wp
        monkeypatch.setattr(wp, "_send", MagicMock())
        monkeypatch.setattr(wp, "_try_inline_command", lambda state: False)
        fake_cancel = MagicMock()
        fake_cancel.is_set = lambda: False
        monkeypatch.setattr(wp, "_cancel_event", fake_cancel)
        state = MagicMock()
        return lambda bucket, events: wp._drain_stream_events(
            state, "req-1", iter(events), bucket,
        )

    @pytest.fixture
    def bucket(self):
        from web_fastapi.worker_process import SessionBucket
        return SessionBucket("s1")

    def test_three_turns_no_duplication(self, drain, bucket):
        """三轮（中间含工具调用）桶内序列精确：增量依序拼接、无重复。"""
        u1 = {"role": "user", "content": "1"}
        a1 = {"role": "assistant", "content": "答1"}
        drain(bucket, [_tm([u1, a1]), {"type": "complete", "content": "答1"}])

        u2 = {"role": "user", "content": "2"}
        asst_tc = {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "ls", "arguments": "{}"}}]}
        tool_r = {"role": "tool", "content": "结果", "tool_call_id": "c1"}
        a2 = {"role": "assistant", "content": "答2"}
        drain(bucket, [_tm([u2, asst_tc, tool_r, a2]), {"type": "complete", "content": "答2"}])

        u3 = {"role": "user", "content": "3"}
        a3 = {"role": "assistant", "content": "答3"}
        drain(bucket, [_tm([u3, a3]), {"type": "complete", "content": "答3"}])

        assert bucket.messages == [u1, a1, u2, asst_tc, tool_r, a2, u3, a3]

    def test_replayed_turn_messages_replaces_previous(self, drain, bucket):
        """同一 stream 内二次 turn_messages：后一份替换前一份（del 语义）。"""
        first = [{"role": "user", "content": "u"}]
        final = [{"role": "user", "content": "u"},
                 {"role": "assistant", "content": "final"}]
        drain(bucket, [_tm(first, partial=True), _tm(final), {"type": "complete", "content": "final"}])
        assert bucket.messages == final

    def test_snapshot_then_increment_no_prefix_duplication(self, drain, bucket):
        """压缩快照 + 后续增量：桶 = 快照 + 增量，无前缀二次叠加。"""
        drain(bucket, [_tm([{"role": "user", "content": "1"},
                            {"role": "assistant", "content": "答1"}]),
                       {"type": "complete", "content": "答1"}])
        snapshot = [
            {"role": "user", "content": "<summary>"},
            {"role": "assistant", "content": "保留近期"},
        ]
        increment = snapshot + [{"role": "user", "content": "新轮"}]
        drain(bucket, [
            _snap(snapshot),
            _tm(increment),
            {"type": "complete", "content": "done"},
        ])
        # 修复前：snapshot 清零 ltc 后 extend 全量 → 快照前缀叠加两遍
        assert bucket.messages == [
            {"role": "user", "content": "<summary>"},
            {"role": "assistant", "content": "保留近期"},
            {"role": "user", "content": "新轮"},
        ]

    def test_interrupt_then_resume_increment(self, drain, bucket):
        """中断轮（无 turn_messages）+ resume 增量：桶不缺 assistant/tool 配对。"""
        drain(bucket, [
            {"type": "tool_start", "tool_name": "bash", "tool_args": {}, "tool_id": "t1"},
            {"type": "human_approval_request", "action": "执行", "details": "d", "thread_id": "s1"},
        ])
        assert bucket.messages == []
        resume_inc = [
            {"role": "user", "content": "2"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "content": "批准后结果", "tool_call_id": "t1"},
            {"role": "assistant", "content": "答2"},
        ]
        drain(bucket, [_tm(resume_inc), {"type": "complete", "content": "答2"}])
        assert bucket.messages == resume_inc
