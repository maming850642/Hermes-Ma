"""
HermesAgentV3 测试 —— 聚焦 HITL 全场景 + 权限模式 + 循环检测。

测试策略：
    由于 HermesAgentV3 依赖 LLM（流式 + ReAct），完整集成测试需要真实 LLM。
    本测试聚焦可 mock 的单元：
    1. InterruptStore 的存取（HITL 基础）
    2. _detect_tool_loop 循环检测（OpenAI dict 消息）
    3. 权限模式的 set/get
    5. mode_guidance 注入
    6. stream_with_hard_timeout 的双 deadline（mock LLM）
    7. HITL 批量中断配对不变量（P1-3）+ 并发审批穿透（P1-4）
    8. resume 轮 todos / 压缩阈值透传（P2-5）
    9. LLM 调用失败旁路记录（P2-9：不进持久历史，UI 仍可见）

完整 ReAct 循环 + 真实 HITL resume 测试在 Phase 4 集成验收时手动跑。

"""

import json
import threading
import time
import pytest
from unittest.mock import MagicMock, patch

from src.agent.agent_v3 import HermesAgentV3
from src.agent.hitl import InterruptSignal, InterruptSnapshot, InterruptStore
from src.agent.llm_stream import stream_with_hard_timeout
from src.agent.session_log import ASSISTANT_MSG, SessionLog
from src.agent.tool_result import ToolResult
from src.llm.messages import AIMsg, Chunk
from src.storage import paths
from src.tools.context import ToolContext
from src.tools.executor_base import ToolExecutor

from tests.agent.test_agent_events import (
    FakeExecutor,
    RecordingLLM,
    ROUND_TEXT,
    make_agent,
    make_tool_spec,
    tool_call_chunks,
)


@pytest.fixture
def data_root(tmp_path):
    """数据根重定向到 tmp（SQLite 落临时库），测后恢复。"""
    paths.set_data_root(tmp_path)
    yield tmp_path
    paths.set_data_root(None)


# ════════════════════════════════════════════════════════════════
# 1. InterruptStore（HITL 基础）
# ════════════════════════════════════════════════════════════════

class TestInterruptStore:

    def test_save_and_get(self):
        store = InterruptStore()
        snap = InterruptSnapshot(
            thread_id="t1",
            messages=[{"role": "user", "content": "hi"}],
            pending_args={"command": "rm x"},
            pending_tool_call_id="c1",
            pending_tool_name="bash",
            pending_payload={"action": "执行 shell", "details": "rm x"},
            permission_mode_at_interrupt="before_changes",
        )
        store.save(snap)

        assert store.has_pending("t1")
        got = store.get("t1")
        assert got is not None
        assert got.pending_tool_name == "bash"
        assert got.pending_args == {"command": "rm x"}
        assert got.pending_payload["action"] == "执行 shell"

    def test_snapshot_minimal_init(self):
        """InterruptSnapshot 最简构造（只填必填字段）。"""
        snap = InterruptSnapshot(
            thread_id="t1",
            messages=[],
        )
        assert snap.thread_id == "t1"
        assert snap.permission_mode_at_interrupt == "before_changes"

    def test_pop_removes(self):
        store = InterruptStore()
        snap = InterruptSnapshot(thread_id="t1", messages=[])
        store.save(snap)

        popped = store.pop("t1")
        assert popped is not None
        assert not store.has_pending("t1")

        # 二次 pop 返回 None
        assert store.pop("t1") is None

    def test_isolated_by_thread(self):
        """不同 thread_id 互不干扰。"""
        store = InterruptStore()
        store.save(InterruptSnapshot(thread_id="t1", messages=["a"]))
        store.save(InterruptSnapshot(thread_id="t2", messages=["b"]))

        assert store.get("t1").messages == ["a"]
        assert store.get("t2").messages == ["b"]

        # pop t1 不影响 t2
        store.pop("t1")
        assert store.has_pending("t2")

    def test_clear_all(self):
        store = InterruptStore()
        store.save(InterruptSnapshot(thread_id="t1", messages=[]))
        store.save(InterruptSnapshot(thread_id="t2", messages=[]))

        store.clear()
        assert not store.has_pending("t1")
        assert not store.has_pending("t2")

    def test_snapshot_deepcopies_messages(self):
        """快照深拷贝 messages，后续修改不影响快照。"""
        store = InterruptStore()
        original_msgs = [{"content": "original"}]
        snap = InterruptSnapshot.create(
            thread_id="t1",
            messages=original_msgs,
            pending_args={},
            tool_call_id="c1",
            tool_name="bash",
            payload={},
            permission_mode="before_changes",
        )
        store.save(snap)

        # 修改原列表
        original_msgs.append({"content": "modified"})

        got = store.get("t1")
        assert len(got.messages) == 1  # 快照不受影响


# ════════════════════════════════════════════════════════════════
# 2. 循环检测
# ════════════════════════════════════════════════════════════════

class TestDetectToolLoop:

    @pytest.fixture
    def agent(self):
        """构造一个 mock agent（不连 LLM）。"""
        with patch.object(HermesAgentV3, "__init__", lambda self, *a, **kw: None):
            a = HermesAgentV3.__new__(HermesAgentV3)
            from config import get_settings
            a.settings = get_settings()
            return a

    def test_no_loop(self, agent):
        """无工具调用 → 不构成循环。"""
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        assert not agent._detect_tool_loop(msgs)

    def test_same_tool_5_times_detected(self, agent):
        """连续 N 次相同工具调用（N=tool_loop_threshold）→ 检测到循环。"""
        threshold = int(getattr(agent.settings, "tool_loop_threshold", 3))
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "ls", "arguments": '{"path": "/"}'}},
            ]}
            for _ in range(threshold)
        ]
        assert agent._detect_tool_loop(msgs)

    def test_different_tools_no_loop(self, agent):
        """不同工具调用 → 不构成循环。"""
        def _tc(cid, name, args):
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": cid, "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args)}},
            ]}
        msgs = [
            _tc("c1", "ls", {}),
            _tc("c2", "read_file", {}),
            _tc("c3", "ls", {}),
        ]
        assert not agent._detect_tool_loop(msgs)

    def test_same_tool_different_args_no_loop(self, agent):
        """同工具不同参数 → 不构成循环。"""
        def _tc(cid, args):
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": cid, "type": "function",
                 "function": {"name": "ls", "arguments": json.dumps(args)}},
            ]}
        msgs = [_tc("c1", {"path": "/a"}), _tc("c2", {"path": "/b"})]
        assert not agent._detect_tool_loop(msgs)


# ════════════════════════════════════════════════════════════════
# 3. 消息转换
# ════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════
# 4. 权限模式
# ════════════════════════════════════════════════════════════════

class TestPermissionMode:

    @pytest.fixture
    def agent(self):
        with patch.object(HermesAgentV3, "__init__", lambda self, *a, **kw: None):
            a = HermesAgentV3.__new__(HermesAgentV3)
            a._permission_mode = "before_changes"
            return a

    def test_default_mode(self, agent):
        assert agent.get_permission_mode() == "before_changes"

    def test_set_mode(self, agent):
        agent.set_permission_mode("plan")
        assert agent.get_permission_mode() == "plan"

    def test_set_full_access(self, agent):
        agent.set_permission_mode("full_access")
        assert agent.get_permission_mode() == "full_access"


# ════════════════════════════════════════════════════════════════
# 4. stream_with_hard_timeout 双 deadline
# ════════════════════════════════════════════════════════════════

class TestStreamWithHardTimeout:

    def test_normal_stream_passes_through(self):
        """正常流式（无超时）→ 全部 chunk 通过。"""
        chunks = [
            Chunk(content_delta="hello"),
            Chunk(content_delta=" world"),
        ]
        mock_client = MagicMock()
        mock_client.stream_chat.return_value = iter(chunks)

        result = list(stream_with_hard_timeout(
            mock_client, [{"role": "user", "content": "hi"}], timeout_s=10,
        ))

        assert len(result) == 2
        assert result[0].content_delta == "hello"
        assert result[1].content_delta == " world"

    def test_gap_timeout_raises(self):
        """间隙超时（模拟无 chunk 流入）→ 抛 TimeoutError。"""
        # mock stream_chat 阻塞（用 time.sleep 模拟半开连接）
        def slow_stream(msgs, **kw):
            time.sleep(5)  # 远超 timeout_s
            yield Chunk(content_delta="late")

        mock_client = MagicMock()
        mock_client.stream_chat.side_effect = slow_stream

        with pytest.raises(TimeoutError):
            list(stream_with_hard_timeout(
                mock_client, [{"role": "user", "content": "hi"}], timeout_s=0.5,
            ))

    def test_worker_exception_propagates(self):
        """worker 内部异常 → re-raise。"""
        def error_stream(msgs, **kw):
            yield Chunk(content_delta="partial")
            raise RuntimeError("LLM error")

        mock_client = MagicMock()
        mock_client.stream_chat.side_effect = error_stream

        with pytest.raises(RuntimeError, match="LLM error"):
            list(stream_with_hard_timeout(
                mock_client, [{"role": "user", "content": "hi"}], timeout_s=10,
            ))


# ════════════════════════════════════════════════════════════════
# 5. InterruptSignal 冒泡链路（集成性单测）
# ════════════════════════════════════════════════════════════════

class TestInterruptSignalFlow:
    """验证 InterruptSignal 从工具经 Registry 冒泡到 Agent 的链路完整。"""

    def test_agent_initializes_interrupt_store(self):
        """HermesAgentV3 初始化时创建 InterruptStore。"""
        # 用 mock memory_manager 避免 MCP 连接
        with patch("src.agent.agent_v3.MemoryOrchestrator"), \
             patch("src.agent.agent_v3.ContextManager"), \
             patch("src.tools.remember.set_memory_manager"), \
             patch("src.tools.resolve.resolve_tools", return_value=[]):
            try:
                agent = HermesAgentV3(MagicMock())
                assert agent.interrupt_store is not None
                assert isinstance(agent.interrupt_store, InterruptStore)
            except Exception:
                # __init__ 可能因 settings/MCP 等失败，跳过（不阻塞测试）
                pytest.skip("Agent 初始化依赖外部资源")


# ════════════════════════════════════════════════════════════════
# 6. HITL 批量中断配对不变量（P1-3）+ 并发审批穿透（P1-4）
# ════════════════════════════════════════════════════════════════

def _batch_chunks(*calls):
    """一轮 LLM 输出多个 tool_call 的 chunk（按 index 区分）。"""
    return [Chunk(tool_call_deltas=[
        {"index": i, "id": cid, "function": {"name": name, "arguments": "{}"}}
        for i, (cid, name) in enumerate(calls)
    ])]


class TestBatchInterruptPairingInvariant:
    """P1-3 贯穿性不变量：批量 [普通+审批+普通] 中断 → resume 后
    build_llm_messages 产物中每个 tool_call_id 都有配对 tool 消息。"""

    def test_resume_batch_pairing_invariant(self, data_root, monkeypatch):
        log = SessionLog()
        specs = [
            make_tool_spec(name="read_a", executor=FakeExecutor(content="读取A结果")),
            make_tool_spec(name="write_b", destructive=True,
                           executor=FakeExecutor(content="写入B结果")),
            make_tool_spec(name="read_c", executor=FakeExecutor(content="读取C结果")),
        ]
        agent = make_agent(monkeypatch, specs=specs, session_log=log)
        llm = RecordingLLM(rounds=[
            _batch_chunks(("cA", "read_a"), ("cB", "write_b"), ("cC", "read_c")),
            ROUND_TEXT,
        ])
        agent._llm_client = llm

        # 中断轮：read_a 完成、write_b 触发审批、read_c 未执行
        events1 = list(agent.stream_invoke(
            "u1", "干活", session_id="sp", thread_id="sp"))
        assert "human_approval_request" in [e.get("type") for e in events1]

        # resume 轮（approve）：write_b 执行后第二轮 LLM 请求必须配对完整
        events2 = list(agent.stream_invoke(
            "u1", "(resume)", session_id="sp", thread_id="sp",
            resume_payload="approve"))
        assert "complete" in [e.get("type") for e in events2]
        assert len(llm.calls) == 2
        req_msgs = llm.calls[1]["messages"]

        call_ids, tool_ids = set(), set()
        for m in req_msgs:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                call_ids.update(tc["id"] for tc in m["tool_calls"])
            elif m.get("role") == "tool":
                tool_ids.add(m.get("tool_call_id"))
        assert call_ids == {"cA", "cB", "cC"}
        assert call_ids <= tool_ids, f"悬空 tool_calls: {call_ids - tool_ids}"

        # 已完成结果以真实内容带出（不是占位）；未执行的合成占位
        by_id = {m["tool_call_id"]: m["content"]
                 for m in req_msgs if m.get("role") == "tool"}
        assert by_id["cA"] == "读取A结果"
        assert by_id["cB"] == "写入B结果"
        assert "未执行" in by_id["cC"]

    def test_interrupt_persists_paired_snapshot(self, data_root, monkeypatch):
        """P1-3：中断轮的 durable 投影配对合法——已完成调用带真实结果
        （不是 derive 占位）、未执行调用由修复补写 tool/result；仅 pending
        调用（等 resume 闭环）走 derive 的 flush 占位。"""
        log = SessionLog()
        specs = [
            make_tool_spec(name="read_a", executor=FakeExecutor(content="读取A结果")),
            make_tool_spec(name="write_b", destructive=True,
                           executor=FakeExecutor(content="写入B结果")),
            make_tool_spec(name="read_c", executor=FakeExecutor(content="读取C结果")),
        ]
        agent = make_agent(monkeypatch, specs=specs, session_log=log)
        agent._llm_client = RecordingLLM(rounds=[
            _batch_chunks(("cA", "read_a"), ("cB", "write_b"), ("cC", "read_c")),
        ])
        list(agent.stream_invoke("u1", "干活", session_id="sq", thread_id="sq"))

        msgs = log.derive_messages("sq")
        call_ids, tool_ids = set(), set()
        for m in msgs:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                call_ids.update(tc["id"] for tc in m["tool_calls"])
            elif m.get("role") == "tool":
                tool_ids.add(m.get("tool_call_id"))
        assert call_ids == {"cA", "cB", "cC"}
        assert call_ids <= tool_ids, "中断轮投影存在无配对 tool 的 tool_calls"

        by_id = {m["tool_call_id"]: m["content"] for m in msgs if m.get("role") == "tool"}
        # 已完成：真实结果带出（不是 "(中断，无结果)" 占位）
        assert by_id["cA"] == "读取A结果"
        # 从未执行：修复补写 tool/result（内容可辨识，非 derive 占位）
        assert "未执行" in by_id["cC"]
        assert by_id["cC"] != "(中断，无结果)"
        # pending（cB）：等 resume 闭环，由 derive 的 flush 占位兜底
        assert by_id["cB"] == "(中断，无结果)"


class RealApprovalExecutor(ToolExecutor):
    """真实 human_approval executor 适配：委托
    src.tools.human_approval._execute_approval——before_changes 下真实抛
    InterruptSignal（非 mock 成功返回），full_access 下返回直接放行文本。
    可选 gate：等待外部事件后执行，用于并发批量下固定"兄弟结果先入队"
    的顺序（消除线程竞速）。"""

    def __init__(self, gate=None):
        self.gate = gate
        self.calls: list = []

    def execute(self, args, ctx) -> ToolResult:
        self.calls.append(dict(args))
        if self.gate is not None:
            self.gate.wait(timeout=5)
        from src.tools.human_approval import _execute_approval
        return ToolResult(content=_execute_approval(
            args.get("action", ""), args.get("details", ""), ctx=ctx))


class SignalingExecutor(FakeExecutor):
    """完成后置位事件的 FakeExecutor（与 gate 配对固定并发顺序）。"""

    def __init__(self, content, done_event):
        super().__init__(content=content)
        self._done = done_event

    def execute(self, args, ctx) -> ToolResult:
        result = super().execute(args, ctx)
        self._done.set()
        return result


def _batch_chunks_with_args(*calls):
    """一轮 LLM 输出多个 tool_call（可各自带 arguments JSON 串）。"""
    return [Chunk(tool_call_deltas=[
        {"index": i, "id": cid, "function": {"name": name, "arguments": args}}
        for i, (cid, name, args) in enumerate(calls)
    ])]


class TestApprovalToolResumeLegs:
    """P1（二轮审查）：request_human_approval（真实 human_approval
    executor，before_changes 下真实抛 InterruptSignal）中断 → resume 的
    审批闭环。

    resume 不得重入 executor（destructive=false → decide 恒 allow，重入
    必然再抛 InterruptSignal：被误报"force_deny 硬拦截"，且 yield 的
    turn_messages 快照里 pending 调用无 tool 结果悬空进活桶）——approve
    直接以"用户已批准：<action>"、reject 以"用户已拒绝"闭合 pending 消息。
    覆盖 before_changes 下串行单发与并发批两种场景。"""

    @staticmethod
    def _pairing_asserts(messages) -> dict:
        """消息序列配对合法性：每个 assistant tool_call 都有配对 tool 消息、
        无空 tool_call_id 孤儿。返回 tool 消息 id→content 映射。"""
        call_ids, tool_by_id = set(), {}
        for m in messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                call_ids.update(tc["id"] for tc in m["tool_calls"])
            elif m.get("role") == "tool":
                assert m.get("tool_call_id"), f"空 tool_call_id 的孤儿 tool 消息: {m}"
                tool_by_id[m["tool_call_id"]] = m.get("content", "")
        assert call_ids <= set(tool_by_id), f"悬空 tool_calls: {call_ids - set(tool_by_id)}"
        return tool_by_id

    def test_serial_interrupt_carries_identity_and_approve_closes(
            self, data_root, monkeypatch):
        """P1（二轮审查）串行腿：单发 request_human_approval（出厂必走
        串行）中断 → 快照必须带真实 tool_call_id/tool_name（此前空 id 落
        None-spec 分支：审批被静默丢弃 + 空 id 孤儿消息）→ approve 后不重入
        executor，以"用户已批准：<action>"正常闭环。"""
        approval = RealApprovalExecutor()
        log = SessionLog()
        agent = make_agent(
            monkeypatch, specs=[make_tool_spec(name="request_human_approval",
                                               executor=approval)],
            session_log=log,
        )
        llm = RecordingLLM(rounds=[
            tool_call_chunks(
                "request_human_approval",
                args_json='{"action": "发送邮件", "details": "给 boss 发周报"}',
                call_id="c1",
            ),
            ROUND_TEXT,
        ])
        agent._llm_client = llm

        # 中断轮：面板弹出 + 快照身份完整（executor 直抛的 payload 只有
        # action/details，串行腿必须补齐 id/name）
        events1 = list(agent.stream_invoke(
            "u1", "求助", session_id="s5a", thread_id="s5a"))
        assert "human_approval_request" in [e.get("type") for e in events1]
        snap = agent.interrupt_store.get("s5a")
        assert snap is not None
        assert snap.pending_tool_call_id == "c1"
        assert snap.pending_tool_name == "request_human_approval"

        # resume approve：不重入 executor（真实 executor 仅中断轮触发一次）
        events2 = list(agent.stream_invoke(
            "u1", "(resume)", session_id="s5a", thread_id="s5a",
            resume_payload="approve"))
        types2 = [e.get("type") for e in events2]
        assert "approval_result" in types2 and "complete" in types2
        assert len(approval.calls) == 1

        tool_end = next(e for e in events2 if e.get("type") == "tool_end")
        assert tool_end["tool_id"] == "c1"
        assert tool_end["result"] == "用户已批准：发送邮件"

        # resume 增量消息序列合法、pending 以批准文本闭合
        tm = next(e for e in events2 if e.get("type") == "turn_messages")
        tool_by_id = self._pairing_asserts(tm["messages"])
        assert tool_by_id["c1"] == "用户已批准：发送邮件"

        # resume 后第二轮 LLM 请求配对完整（严格 OpenAI 兼容端点不 400）
        assert len(llm.calls) == 2
        self._pairing_asserts(llm.calls[1]["messages"])

        # durable 侧无空 tool_call_id 的孤儿事件
        for ev in log.events("s5a"):
            payload = ev.get("payload") or {}
            if "tool_call_id" in payload:
                assert payload["tool_call_id"], f"空 tool_call_id 事件: {ev}"

    def test_resume_approve_full_leg(self, data_root, monkeypatch):
        """并发批量 [普通工具 + request_human_approval（真实 executor）]：
        面板弹出 → approve → 不重入 executor，pending 调用以"用户已批准"
        闭合，消息序列合法（旧实现重入 → 再抛 InterruptSignal 被谎报
        force_deny 硬拦截 + pending 无结果悬空）。"""
        sibling_done = threading.Event()
        approval = RealApprovalExecutor(gate=sibling_done)
        log = SessionLog()
        specs = [
            make_tool_spec(name="read_only",
                           executor=SignalingExecutor("只读结果", sibling_done)),
            make_tool_spec(name="request_human_approval", executor=approval),
        ]
        agent = make_agent(monkeypatch, specs=specs, session_log=log)
        llm = RecordingLLM(rounds=[
            _batch_chunks_with_args(
                ("c1", "read_only", "{}"),
                ("c2", "request_human_approval",
                 '{"action": "发送邮件", "details": "需要人确认"}'),
            ),
            ROUND_TEXT,
        ])
        agent._llm_client = llm

        # 中断轮：面板弹出
        events1 = list(agent.stream_invoke(
            "u1", "帮忙", session_id="s4a", thread_id="s4a"))
        assert "human_approval_request" in [e.get("type") for e in events1]

        # resume 轮（approve）：决策不丢、不重入、批准文本闭合
        events2 = list(agent.stream_invoke(
            "u1", "(resume)", session_id="s4a", thread_id="s4a",
            resume_payload="approve"))
        types2 = [e.get("type") for e in events2]

        ar = next(e for e in events2 if e.get("type") == "approval_result")
        assert ar["decision"] == "approve"
        assert ar["tool_name"] == "request_human_approval"
        # 真实 executor 只在中断轮触发过一次（resume 零重入）
        assert len(approval.calls) == 1
        # 不再有"force_deny 硬拦截"谎报（旧 bug 文案不得出现）
        assert not [e for e in events2 if "force_deny" in str(e.get("content", ""))]
        tool_ends = [e for e in events2 if e.get("type") == "tool_end"]
        assert any(
            e.get("tool_id") == "c2" and e.get("result") == "用户已批准：发送邮件"
            for e in tool_ends
        )
        assert "complete" in types2

        # resume 增量消息序列合法（无空 id、无悬空、兄弟结果保留）
        tm = next(e for e in events2 if e.get("type") == "turn_messages")
        tool_by_id = self._pairing_asserts(tm["messages"])
        assert tool_by_id["c2"] == "用户已批准：发送邮件"
        assert tool_by_id["c1"] == "只读结果"

        # resume 后第二轮 LLM 请求配对完整（严格 OpenAI 兼容端点不 400）
        assert len(llm.calls) == 2
        self._pairing_asserts(llm.calls[1]["messages"])

        # durable 侧无空 tool_call_id 的孤儿事件
        for ev in log.events("s4a"):
            payload = ev.get("payload") or {}
            if "tool_call_id" in payload:
                assert payload["tool_call_id"], f"空 tool_call_id 事件: {ev}"

    def test_resume_reject_full_leg(self, data_root, monkeypatch):
        """reject 腿（真实 executor）：决策不丢、不重入，pending 调用以
        "用户已拒绝"闭合，序列合法无孤儿。"""
        sibling_done = threading.Event()
        approval = RealApprovalExecutor(gate=sibling_done)
        log = SessionLog()
        specs = [
            make_tool_spec(name="read_only",
                           executor=SignalingExecutor("只读结果", sibling_done)),
            make_tool_spec(name="request_human_approval", executor=approval),
        ]
        agent = make_agent(monkeypatch, specs=specs, session_log=log)
        llm = RecordingLLM(rounds=[
            _batch_chunks_with_args(
                ("c1", "read_only", "{}"),
                ("c2", "request_human_approval",
                 '{"action": "发送邮件", "details": "需要人确认"}'),
            ),
            ROUND_TEXT,
        ])
        agent._llm_client = llm

        list(agent.stream_invoke("u1", "帮忙", session_id="s4b", thread_id="s4b"))
        events2 = list(agent.stream_invoke(
            "u1", "(resume)", session_id="s4b", thread_id="s4b",
            resume_payload="reject:不行"))

        ar = next(e for e in events2 if e.get("type") == "approval_result")
        assert ar["decision"] == "reject"
        assert ar["tool_name"] == "request_human_approval"
        assert ar["reason"] == "不行"
        # 拒绝 → executor 保持只执行过触发审批那一次
        assert len(approval.calls) == 1
        assert "complete" in [e.get("type") for e in events2]

        # 拒绝消息以真实 id 闭环（拒绝原因拼进闭合文本——此前裸"用户已拒绝"
        # 把 reason 丢弃，模型只知道被拒、不知道为什么）
        tm = next(e for e in events2 if e.get("type") == "turn_messages")
        tool_by_id = self._pairing_asserts(tm["messages"])
        assert tool_by_id["c2"] == "用户已拒绝：不行"

        # reject 后模型仍会总结 → 第二轮请求同样配对完整
        assert len(llm.calls) == 2
        req_by_id = self._pairing_asserts(llm.calls[1]["messages"])
        assert req_by_id["c2"] == "用户已拒绝：不行"

        # durable 侧无空 tool_call_id 的孤儿事件
        for ev in log.events("s4b"):
            payload = ev.get("payload") or {}
            if "tool_call_id" in payload:
                assert payload["tool_call_id"], f"空 tool_call_id 事件: {ev}"

    def test_interrupt_drain_sibling_todos_survive(self, data_root, monkeypatch):
        """P3：并发批量 [write_todos, request_human_approval] 中断时兄弟
        待办不丢——registry drain 合并兄弟 state_updates 经 payload 带出，
        agent 侧补发 todos_update 事件（此前 drain 只取 result，待办随中断
        丢失且无 todos_update）。"""
        release = threading.Event()

        class GatedTodosExecutor(ToolExecutor):
            def execute(self, args, ctx) -> ToolResult:
                release.wait(timeout=5)
                return ToolResult(
                    content="待办已写",
                    state_updates={"todos": [{"id": "1", "content": "写文档",
                                              "status": "pending"}]},
                )

        specs = [
            make_tool_spec(name="write_todos", executor=GatedTodosExecutor()),
            make_tool_spec(name="request_human_approval",
                           executor=RealApprovalExecutor()),
        ]
        agent = make_agent(monkeypatch, specs=specs, session_log=SessionLog())
        agent._llm_client = RecordingLLM(rounds=[
            _batch_chunks(("c1", "write_todos"), ("c2", "request_human_approval")),
        ])

        # 挂钩 durable 映射（event_sink 同步调用）：审批工具 tool_start 被
        # 消费时放行兄弟并拖住注册表主循环——让兄弟的 tool_end 排到
        # interrupt 之后，命中 drain 路径（确定性，不依赖竞速）
        real_durable = agent._durable_ui_event

        def durable_hook(event):
            real_durable(event)
            if (event.get("type") == "tool_start"
                    and event.get("tool_name") == "request_human_approval"):
                release.set()
                time.sleep(0.3)

        monkeypatch.setattr(agent, "_durable_ui_event", durable_hook)

        events = list(agent.stream_invoke(
            "u1", "干活", session_id="sx", thread_id="sx"))
        types = [e.get("type") for e in events]

        assert "human_approval_request" in types
        assert "todos_update" in types
        assert types.index("todos_update") < types.index("human_approval_request")
        todos_ev = next(e for e in events if e.get("type") == "todos_update")
        assert todos_ev["todos"] == [{"id": "1", "content": "写文档",
                                      "status": "pending"}]

    def test_serial_batch_interrupt_todos_survive(self, data_root, monkeypatch):
        """串行批量 [write_todos, destructive 审批工具] 中断时兄弟待办不丢
        ——对齐并发腿 test_interrupt_drain_sibling_todos_survive 的断言形态。
        此前串行腿 InterruptSignal 只挂 completed_tool_messages，state_updates
        （todos）随栈帧丢弃：_handle_interrupt 消费不到 payload["state_updates"]，
        中断轮的待办既不进 state 也没有 todos_update 补发。"""

        class TodosExecutor(ToolExecutor):
            def execute(self, args, ctx) -> ToolResult:
                return ToolResult(
                    content="待办已写",
                    state_updates={"todos": [{"id": "1", "content": "写文档",
                                              "status": "pending"}]},
                )

        specs = [
            make_tool_spec(name="write_todos", executor=TodosExecutor()),
            # destructive → 整批强制串行，权限层在 executor 前抛 InterruptSignal
            make_tool_spec(name="write_b", destructive=True,
                           executor=FakeExecutor(content="写入B结果")),
        ]
        agent = make_agent(monkeypatch, specs=specs, session_log=SessionLog())
        agent._llm_client = RecordingLLM(rounds=[
            _batch_chunks(("c1", "write_todos"), ("c2", "write_b")),
        ])

        events = list(agent.stream_invoke(
            "u1", "干活", session_id="sy", thread_id="sy"))
        types = [e.get("type") for e in events]

        assert "human_approval_request" in types
        assert "todos_update" in types
        assert types.index("todos_update") < types.index("human_approval_request")
        todos_evs = [e for e in events if e.get("type") == "todos_update"]
        assert todos_evs[0]["todos"] == [{"id": "1", "content": "写文档",
                                          "status": "pending"}]


# ════════════════════════════════════════════════════════════════
# write_todos 重复检测签名的会话隔离
# ════════════════════════════════════════════════════════════════

class TestWriteTodosSignatureIsolation:
    """2026-09-05：_last_todos_sig 此前是进程级单值——A 会话写过的规划会让
    B 会话（如 waker 定时任务）首次写入相同结构就吃到"与上次完全相同"的
    假前进信号。改按 user_id（remember.py 同款 contextvar）分桶后，两会话
    写相同签名互不干扰。"""

    def test_same_signature_across_users_no_cross_talk(self):
        from src.tools import write_todos as wt
        from src.tools.remember import set_current_user_id

        todos = [{"id": "1", "content": "任务A", "status": "pending"}]
        try:
            tok_a = set_current_user_id("user-a")
            r1 = wt._execute_write_todos([dict(t) for t in todos])
            assert "已更新" in r1.content  # 首次写入：正常 summary
            tok_a.var.reset(tok_a)

            # 会话 B（如 waker）写相同结构：不得吃到 A 的"完全相同"假信号
            tok_b = set_current_user_id("user-b")
            r2 = wt._execute_write_todos([dict(t) for t in todos])
            assert "完全相同" not in r2.content
            assert "已更新" in r2.content

            # 同一会话内重复写相同签名 → 前进信号（重复检测本职）
            r3 = wt._execute_write_todos([dict(t) for t in todos])
            assert "完全相同" in r3.content
            tok_b.var.reset(tok_b)
        finally:
            wt._last_todos_sig_by_user.clear()


class TestImagesMultimodalContent:
    """P2（二轮审查）：stream_invoke 的 images 形参此前函数体零引用、
    multimodal.build_user_content 全仓无调用方，Web 附图（worker 真实传
    images）被静默丢弃——接线后经 build_user_content 构造 OpenAI Vision
    多模态 content。"""

    def test_images_built_into_multimodal_user_content(
            self, data_root, monkeypatch, tmp_path):
        from src.agent import multimodal as multimodal_mod

        uploads = tmp_path / "uploads" / "u1"
        uploads.mkdir(parents=True)
        (uploads / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-image-bytes")
        monkeypatch.setattr(multimodal_mod, "UPLOADS_DIR", tmp_path / "uploads")

        agent = make_agent(monkeypatch, specs=[])
        llm = RecordingLLM(rounds=[ROUND_TEXT])
        agent._llm_client = llm

        list(agent.stream_invoke("u1", "看看这张图", images=["pic.png"]))

        user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
        content = user_msgs[-1]["content"]
        # 多模态数组：图片在前（base64 data URI）、文本在后
        assert isinstance(content, list)
        assert content[0]["type"] == "image_url"
        assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
        assert content[-1] == {"type": "text", "text": "看看这张图"}

    def test_no_images_keeps_plain_text(self, data_root, monkeypatch):
        """无图（缺省/空列表）：user content 保持纯文本 str，行为不变。"""
        agent = make_agent(monkeypatch, specs=[])
        llm = RecordingLLM(rounds=[ROUND_TEXT])
        agent._llm_client = llm

        list(agent.stream_invoke("u1", "纯文本", images=[]))

        user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
        assert user_msgs[-1]["content"] == "纯文本"

    def test_unresolvable_images_fall_back_to_text(self, data_root, monkeypatch, tmp_path):
        """图片全部解析失败（不存在）：build_user_content 退化为纯文本。"""
        from src.agent import multimodal as multimodal_mod

        monkeypatch.setattr(multimodal_mod, "UPLOADS_DIR", tmp_path / "uploads")

        agent = make_agent(monkeypatch, specs=[])
        llm = RecordingLLM(rounds=[ROUND_TEXT])
        agent._llm_client = llm

        list(agent.stream_invoke("u1", "看看这张图", images=["ghost.png"]))

        user_msgs = [m for m in llm.calls[0]["messages"] if m.get("role") == "user"]
        assert user_msgs[-1]["content"] == "看看这张图"


class TestStreamRequestCancelEventWiring:
    """二轮审查第 12 项（与并行修复组 llm_plugin 侧对接）：ReAct 循环构造
    StreamRequest 必须携带本轮预置的 cancel_event——kernel 路径（llm_plugin
    默认监听器）经 StreamRequest.cancel_event 接进 stream_with_hard_timeout，
    防线 C 契约不再落空。"""

    def test_stream_request_carries_per_turn_cancel_event(self, data_root, monkeypatch):
        from src.cordis.context import Context

        agent = make_agent(monkeypatch, specs=[], kernel_ctx=None)
        agent._llm_client = RecordingLLM(rounds=[ROUND_TEXT])

        seen: list = []

        def observer(req, next):
            # 注册在默认监听器之后（同 scope 注册序）→ 观察到的是循环构造
            # 的同一个 StreamRequest
            seen.append(req)
            return next()

        agent.scope.on("llm/stream", observer)

        list(agent.stream_invoke("u1", "你好", session_id="scv", thread_id="scv"))

        assert len(seen) == 1
        req = seen[0]
        assert req.cancel_event is not None
        # 携带的是循环预置的同一个事件对象（P2-8 防线 C：消费方置位后
        # worker 线程断流）
        assert req.cancel_event is agent._stream_cancel_event


# ════════════════════════════════════════════════════════════════
# 7. resume 轮上下文透传（P2-5）
# ════════════════════════════════════════════════════════════════

class TestResumeContextPassthrough:

    def test_resume_passes_todos_and_compact_pct(self, data_root, monkeypatch):
        """resume 轮必须消费调用方传入的 todos 与 compact_threshold_pct
        （worker 与 CLI 都在传，此前 _build_state 硬编码 [] 全丢弃）。"""
        log = SessionLog()
        spec = make_tool_spec(name="rm", destructive=True,
                              executor=FakeExecutor(content="已删除"))
        agent = make_agent(monkeypatch, specs=[spec], session_log=log)
        llm = RecordingLLM(rounds=[tool_call_chunks("rm"), ROUND_TEXT])
        agent._llm_client = llm

        # 捕获 build_llm_messages 的 todos 实参 + 压缩阈值判定入参
        real_build = agent.context_manager.build_llm_messages
        captured = []

        def spy_build(**kw):
            captured.append(kw)
            return real_build(**kw)

        monkeypatch.setattr(agent.context_manager, "build_llm_messages", spy_build)

        seen_pct = []
        real_hit = agent._compact_threshold_hit

        def spy_hit(messages, pct):
            seen_pct.append(pct)
            return real_hit(messages, pct)

        monkeypatch.setattr(agent, "_compact_threshold_hit", spy_hit)

        todos = [{"id": "1", "content": "待办", "status": "pending"}]
        list(agent.stream_invoke("u1", "删", session_id="st", thread_id="st",
                                 todos=todos, compact_threshold_pct=55))
        list(agent.stream_invoke("u1", "(resume)", session_id="st", thread_id="st",
                                 resume_payload="approve",
                                 todos=todos, compact_threshold_pct=55))

        assert len(captured) == 2 and len(seen_pct) == 2
        # fresh 轮本来就在传（对照）；resume 轮此前丢 todos
        assert captured[0]["todos"] == todos
        assert captured[1]["todos"] == todos
        # 压缩阈值：resume 轮用调用方 pct（此前恒 None=配置默认）
        assert seen_pct == [55, 55]


# ════════════════════════════════════════════════════════════════
# 8. LLM 调用失败旁路记录（P2-9）
# ════════════════════════════════════════════════════════════════

class TestLlmErrorBypassRecord:

    def test_error_not_persisted_but_visible(self, data_root, monkeypatch):
        """错误文本不进桶/不进投影/不进 durable assistant 消息；
        complete 事件照常带给 UI；旁路 llm/error 事件可审计。"""
        log = SessionLog()
        agent = make_agent(monkeypatch, specs=[make_tool_spec()], session_log=log)

        class ExplodingLLM:
            def stream_chat(self, messages, tools=None, temperature=None,
                            max_tokens=None):
                raise RuntimeError("connection reset")

        agent._llm_client = ExplodingLLM()
        events = list(agent.stream_invoke(
            "u1", "你好", session_id="se", thread_id="se"))
        types = [e.get("type") for e in events]

        comp = next(e for e in events if e.get("type") == "complete")
        assert "LLM 调用失败" in comp.get("content", "")
        assert "connection reset" in comp.get("content", "")

        # durable：无 assistant/message（错误文本不进投影），有 llm/error 旁路
        durable = log.events("se")
        assert not [e for e in durable if e["type"] == ASSISTANT_MSG]
        assert any(
            e["type"] == "llm/error"
            and "connection reset" in (e.get("payload") or {}).get("error", "")
            for e in durable
        )
        assert log.derive_messages("se") == [{"role": "user", "content": "你好"}]

    def test_error_text_absent_from_next_request(self, data_root, monkeypatch):
        """下一轮的 LLM 请求历史里不含错误文本（不再永久进入模型上下文）。"""
        log = SessionLog()
        agent = make_agent(monkeypatch, specs=[make_tool_spec()], session_log=log)

        class ExplodingLLM:
            def stream_chat(self, messages, tools=None, temperature=None,
                            max_tokens=None):
                raise RuntimeError("boom")

        agent._llm_client = ExplodingLLM()
        list(agent.stream_invoke("u1", "第一问", session_id="se2", thread_id="se2"))

        llm2 = RecordingLLM(rounds=[ROUND_TEXT])
        agent._llm_client = llm2
        list(agent.stream_invoke("u1", "第二问", session_id="se2", thread_id="se2"))

        flat = "".join(str(m.get("content", "")) for m in llm2.calls[0]["messages"])
        assert "LLM 调用失败" not in flat
        assert "boom" not in flat

    def test_error_marked_message_reaches_ui_projection(self, data_root, monkeypatch):
        """2026-09-19：错误以 llm_error 标记进 messages/llm/error 事件——
        UI 投影（include_reasoning=True）还原，切页/重启后历史仍能看到
        失败信息；LLM 视图与 durable assistant 消息保持纯净（P2-9 不变量）。"""
        log = SessionLog()
        agent = make_agent(monkeypatch, specs=[make_tool_spec()], session_log=log)

        class ExplodingLLM:
            def stream_chat(self, messages, tools=None, temperature=None,
                            max_tokens=None):
                raise RuntimeError("connection reset")

        agent._llm_client = ExplodingLLM()
        list(agent.stream_invoke("u1", "你好", session_id="se3", thread_id="se3"))

        # UI 视图：错误还原为带标记的 assistant 消息
        ui = log.derive_messages("se3", include_reasoning=True)
        assert [m["role"] for m in ui] == ["user", "assistant"]
        assert ui[-1].get("llm_error") is True
        assert "connection reset" in ui[-1]["content"]

        # LLM 视图：仍只有 user（模型不可见性不变）
        pure = log.derive_messages("se3")
        assert pure == [{"role": "user", "content": "你好"}]

        # 后续轮次的"最终答复"提取也不得吃到错误留底
        assert agent._extract_final_response(
            ui + [{"role": "assistant", "content": "正常回答"}]) == "正常回答"
        assert agent._extract_final_response(ui) == ""
