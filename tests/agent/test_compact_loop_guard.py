"""
compact_conversation 死循环防线回归（2026-09-06）。

用户实录：模型每轮说一句"我先看看项目结构"→ 调 compact_conversation →
工具结果回"上下文压缩已触发。系统将在本轮自动压缩早期对话历史"（将来时）
→ 模型读到"尚未压缩"，下一轮原样重演，循环十几次直到
max_agent_iterations 烧完。根因有三：

1. 工具返回文本是将来时，实际压缩已同步完成（消费 compact_requested
   → _compact_messages）→ 模型每轮重新触发；
2. 循环检测的签名窗口从 messages 反向收集，每次压缩把 messages 替换成
   [摘要]+keep 尾部 → 窗口内签名数被截断，wide 规则结构性不可达，
   压缩重置击穿检测；
3. auto=False 路径不发 auto_compact 事件，用户看不到压缩已发生。

本文件逐道防线锁定：
- 完成时态工具文本 + yaml 描述（tests/test_compact_signal.py 锁信号结构，
  这里锁措辞语义）；
- 每轮一次闸门：compacted_this_turn 标记 → 同轮二次 compact_requested
  不执行压缩且工具结果被改写为"已压缩过"提示；
- 循环检测有状态化：签名历史存 state["_tool_sig_hist"]（compact 只替换
  messages、不动 state 其他键）→ 压缩截窗后 wide 规则仍可达；
- auto_compact 事件 trigger:"tool" 字段。
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

import src.agent  # noqa: F401
from src.agent.agent_v3 import HermesAgentV3
from src.agent.context import CompactResult
from src.agent.registry_v3 import ToolRegistryV3
from src.agent.tool_result import ToolResult
from src.llm.messages import Chunk
from src.tools.compact import _execute_compact
from src.tools.executor_base import ToolExecutor
from src.tools.schema import SideEffects, ToolSpec

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ════════════════════════════════════════════════════════════════
# 测试设施（与 tests/test_agent_events.py 同款最小桩）
# ════════════════════════════════════════════════════════════════

class RecordingLLM:
    """mock LLMClient：按脚本回放 chunk 流，记录每次请求 messages。"""

    def __init__(self, rounds):
        self.rounds = [list(r) for r in rounds]
        self.calls: list[dict] = []

    def stream_chat(self, messages, tools=None, temperature=None, max_tokens=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        return iter(list(self.rounds.pop(0)))


class FakeExecutor(ToolExecutor):
    """假执行器：固定 content + state_updates，记录调用。"""

    def __init__(self, content="ok", state_updates=None):
        self.result = ToolResult(content=content, state_updates=dict(state_updates or {}))
        self.calls: list[dict] = []

    def execute(self, args, ctx) -> ToolResult:
        self.calls.append(dict(args))
        return self.result


def make_tool_spec(name="echo", executor=None):
    return ToolSpec(
        name=name,
        description="test tool",
        parameters={"type": "object", "properties": {}},
        executor=executor or FakeExecutor(),
        side_effects=SideEffects(destructive=False),
    )


def tool_call_chunks(name, call_id, args_json="{}"):
    """单个工具调用的 chunk 流。"""
    return [Chunk(tool_call_deltas=[{
        "index": 0, "id": call_id,
        "function": {"name": name, "arguments": args_json},
    }])]


def parallel_tool_call_chunks(*calls):
    """并发批量工具调用的 chunk 流：一个 Chunk 携带多个 delta（index 区分，
    accumulate 按 index 合并 → 各 delta 的 index 必须互不相同）。"""
    deltas = [
        {"index": i, "id": cid, "function": {"name": name, "arguments": args}}
        for i, (name, cid, args) in enumerate(calls)
    ]
    return [Chunk(tool_call_deltas=deltas)]


def make_memory_manager():
    mm = MagicMock(name="memory_manager")
    mm.search_with_detail.return_value = {
        "filtered_results": [], "raw_count": 0, "hit_count": 0,
    }
    return mm


def make_agent(monkeypatch, specs):
    """构造真实 HermesAgentV3（不连 LLM/MCP，工具列表注入假 spec）。"""
    import importlib

    agent_v3_mod = importlib.import_module("src.agent.agent_v3")
    monkeypatch.setattr(
        agent_v3_mod, "resolve_tools",
        lambda ctx, settings=None, include_mcp=True: list(specs or []),
    )
    with patch("src.tools.remember.set_memory_manager"):
        return HermesAgentV3(
            make_memory_manager(),
            registry=ToolRegistryV3(),
        )


def run_turn(agent, user_input="查一下"):
    return list(agent.stream_invoke("u1", user_input))


def patch_compact(agent, calls_box, summary="对话摘要", original=8, compacted=6):
    """把 context_manager.compact_messages 换成真实形态 CompactResult 桩，
    调用次数记入 calls_box（列表）。压缩结果模拟真实保留形态：
    [system 摘要] + 输入尾部 2 条原样保留（含刚执行的压缩步本身——
    真实 compactor 保留最后 N 条，测试若用纯文本尾巴会让有状态检测
    在链断裂处误清历史，掩盖被测行为）。"""

    def fake_compact(messages, keep_count=None):
        calls_box.append(list(messages))
        return CompactResult(
            compressed_messages=(
                [{"role": "system", "content": f"摘要：{summary}"}]
                + [dict(m) for m in list(messages)[-2:]]
            ),
            summary=summary,
            original_count=original,
            compacted_count=compacted,
        )

    agent.context_manager.compact_messages = MagicMock(side_effect=fake_compact)


COMPACT_CONTENT = "上下文压缩已完成：早期对话已替换为摘要。请直接继续当前任务，不要再次调用本工具。"
GATE_MARK = "已压缩过"


def compact_spec():
    return make_tool_spec(
        name="compact_conversation",
        executor=FakeExecutor(content=COMPACT_CONTENT,
                              state_updates={"compact_requested": True}),
    )


# ════════════════════════════════════════════════════════════════
# 1. 工具文本 / yaml 描述：完成时态 + 去掉诱发条件
# ════════════════════════════════════════════════════════════════

class TestCompactToolWording:

    def test_execute_compact_content_is_past_tense(self):
        """工具结果必须是完成时态 + 明示勿重调——将来时"将在"是死循环根因。"""
        content = _execute_compact().content
        assert "已完成" in content
        assert "不要再次调用" in content
        assert "将在" not in content, "将来时措辞会让模型以为压缩尚未发生"

    def test_yaml_description_removes_inducing_condition(self):
        """yaml"何时使用"删掉"感觉上下文占用过多 token"类诱发条件，
        并写明压缩同步完成、勿重复调用。"""
        cfg = yaml.safe_load(
            (_PROJECT_ROOT / "tools" / "compact_conversation.yaml").read_text(encoding="utf-8")
        )
        desc = cfg["description"]
        assert "感觉上下文占用过多" not in desc, "诱发条件会引导模型无谓触发压缩"
        assert "仅在系统提示" in desc
        assert "不要再次调用" in desc


# ════════════════════════════════════════════════════════════════
# 2. 每轮一次闸门：同轮二次 compact_requested 不执行且结果被改写
# ════════════════════════════════════════════════════════════════

class TestPerTurnCompactGate:

    def test_second_compact_request_gated_and_result_rewritten(self, monkeypatch):
        """compact_requested 二次到达（同轮）：压缩不再执行（call_count==1），
        且二次工具结果被改写为"已压缩过"提示（模型读到的是闸门文本）。"""
        agent = make_agent(monkeypatch, specs=[compact_spec()])
        calls_box: list = []
        patch_compact(agent, calls_box)
        llm = RecordingLLM(rounds=[
            tool_call_chunks("compact_conversation", call_id="c1"),
            tool_call_chunks("compact_conversation", call_id="c2"),
            [Chunk(content_delta="最终"), Chunk(content_delta="回答")],
        ])
        agent._llm_client = llm

        events = run_turn(agent)

        # 压缩只执行了一次（二次请求被闸门拦下）
        assert agent.context_manager.compact_messages.call_count == 1
        # 首次工具结果 = 原始完成时态文本；二次 = 改写后的"已压缩过"
        tools_in = lambda call: {m["tool_call_id"]: m["content"]
                                 for m in call["messages"] if m.get("role") == "tool"}
        assert "已替换为摘要" in tools_in(llm.calls[1])["c1"]
        assert GATE_MARK in tools_in(llm.calls[2])["c2"]
        assert "不要再次调用" in tools_in(llm.calls[2])["c2"]
        # 轮正常收尾（未被闸门卡死）
        assert events[-1] == {"type": "complete", "content": "最终回答"}

    def test_gate_resets_next_turn(self, monkeypatch):
        """闸门标记随 state 每轮重建而重置：下一用户轮可再次压缩
        （两个用户轮各压缩一次，轮间标记不串）。"""
        agent = make_agent(monkeypatch, specs=[compact_spec()])
        calls_box: list = []
        patch_compact(agent, calls_box)
        llm = RecordingLLM(rounds=[
            tool_call_chunks("compact_conversation", call_id="c1"),
            [Chunk(content_delta="第一轮收尾")],
            tool_call_chunks("compact_conversation", call_id="c2"),
            [Chunk(content_delta="第二轮收尾")],
        ])
        agent._llm_client = llm

        first = run_turn(agent, "第一轮")
        second = run_turn(agent, "第二轮")

        assert first[-1]["content"] == "第一轮收尾"
        assert second[-1]["content"] == "第二轮收尾"
        assert agent.context_manager.compact_messages.call_count == 2


# ════════════════════════════════════════════════════════════════
# 3. 循环检测有状态化：签名历史跨压缩存活
# ════════════════════════════════════════════════════════════════

def _cfg(threshold):
    return SimpleNamespace(tool_loop_threshold=threshold)


def _stub_agent(threshold):
    """最小 self 桩（同 tests/test_tool_loop_threshold.py 模式）。"""
    return SimpleNamespace(settings=_cfg(threshold))


def _asst_tc(name, args=None):
    if args is None:
        args = {}
    return {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "x", "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)}}],
    }


def _tool_res():
    return {"role": "tool", "content": "r", "tool_call_id": "x"}


class TestStatefulLoopDetection:

    def test_signature_history_survives_compact_truncation(self):
        """核心回归：[compact, X, compact, X, …] 序列，每次压缩把 messages
        截成 摘要+尾部 2 条（真实 keep 形态）——有状态历史照常累计，
        第 9 步（wide=9，唯一数 2）触发终止。修复前 messages 反向收集
        窗口被截断，结构性不可达。"""
        detect = HermesAgentV3._detect_tool_loop
        agent = _stub_agent(3)  # threshold=3 → wide=9
        state = {"messages": [{"role": "user", "content": "hi"}]}
        results = []
        for step_name in ["compact_conversation", "write_todos"] * 5:
            # 模拟一个工具轮：assistant(tool_calls) + tool 结果进 messages
            state["messages"].append(_asst_tc(step_name))
            state["messages"].append(_tool_res())
            if step_name == "compact_conversation":
                # 模拟压缩截断：messages 整体替换为 摘要 + 尾部 2 条
                state["messages"] = (
                    [{"role": "system", "content": "摘要"}] + state["messages"][-2:]
                )
            results.append(detect(agent, state["messages"], state))
        # 前 8 步窗口不足 wide → 不触发；第 9 步触发
        assert results[:8] == [False] * 8
        assert results[8] is True
        # 触发走的是规则 2（交替型，唯一数 2），不是规则 1（全同）
        hist = state["_tool_sig_hist"]
        assert len(hist) == 9
        assert len(set(hist)) == 2

    def test_message_fallback_cannot_fire_after_compact(self):
        """对照（文档化根因）：同样序列不传 state → messages 反向收集，
        压缩截窗后 wide 永不可达 → 全程不触发。这正是主循环必须走
        state 历史的原因（回退路径保留给无 state 的旧调用方/测试桩）。"""
        detect = HermesAgentV3._detect_tool_loop
        agent = _stub_agent(3)
        messages = [{"role": "user", "content": "hi"}]
        results = []
        for step_name in ["compact_conversation", "write_todos"] * 5:
            messages.append(_asst_tc(step_name))
            messages.append(_tool_res())
            if step_name == "compact_conversation":
                messages = [{"role": "system", "content": "摘要"}] + messages[-2:]
            results.append(detect(agent, messages))
        assert results == [False] * 10

    def test_text_assistant_breaks_stateful_chain(self):
        """最近 assistant 为纯文本 → 链条断裂，历史清空（对齐 messages
        扫描的窗口重置语义），后续需重新累计。"""
        detect = HermesAgentV3._detect_tool_loop
        agent = _stub_agent(3)
        state = {"messages": [_asst_tc("write_todos"), _tool_res()]}
        assert detect(agent, state["messages"], state) is False
        assert len(state["_tool_sig_hist"]) == 1
        # 模型收尾发言（纯文本）→ 断链
        state["messages"].append({"role": "assistant", "content": "先看结构"})
        assert detect(agent, state["messages"], state) is False
        assert state["_tool_sig_hist"] == []

    def test_stateful_rule1_identical_signatures_still_fires(self):
        """规则 1（连续全同）在有状态路径下不退化：threshold 连发同签名触发。"""
        detect = HermesAgentV3._detect_tool_loop
        agent = _stub_agent(3)
        state = {"messages": []}
        for _ in range(2):
            state["messages"] = [_asst_tc("compact_conversation"), _tool_res()]
            assert detect(agent, state["messages"], state) is False
        state["messages"] = [_asst_tc("compact_conversation"), _tool_res()]
        assert detect(agent, state["messages"], state) is True

    def test_threshold_zero_disables_stateful_detection(self):
        """threshold=0 → 有状态路径同样关闭（不写历史）。"""
        detect = HermesAgentV3._detect_tool_loop
        agent = _stub_agent(0)
        state = {"messages": [_asst_tc("compact_conversation"), _tool_res()]}
        assert detect(agent, state["messages"], state) is False
        assert state.get("_tool_sig_hist", []) == []


class TestAlternatingLoopTerminates:
    """端到端：真实 ReAct 循环 + 闸门 + 有状态检测，compact/X 交替在第
    10 次检测处终止（修复前烧满 max_agent_iterations）。"""

    def test_compact_x_alternation_terminates(self, monkeypatch):
        echo_exec = FakeExecutor(content="回声")
        agent = make_agent(monkeypatch, specs=[
            compact_spec(),
            make_tool_spec(name="echo", executor=echo_exec),
        ])
        # threshold=3 → wide=9：9 个工具步（C/X 交替）后触发
        monkeypatch.setattr(agent.settings, "tool_loop_threshold", 3)
        monkeypatch.setattr(agent.settings, "max_agent_iterations", 50)
        calls_box: list = []
        patch_compact(agent, calls_box)
        rounds = []
        for i in range(9):
            name = "compact_conversation" if i % 2 == 0 else "echo"
            rounds.append(tool_call_chunks(name, call_id=f"c{i}"))
        rounds.append([Chunk(content_delta="不该"), Chunk(content_delta="到这里")])
        llm = RecordingLLM(rounds=rounds)
        agent._llm_client = llm

        events = run_turn(agent)

        # 闸门：压缩只执行一次（第 2 次起被拦）
        assert agent.context_manager.compact_messages.call_count == 1
        # 有状态检测：9 个工具步后终止（恰好消耗 9 次 LLM 调用，兜底文本轮未用）
        assert len(llm.calls) == 9
        tokens = [ev["content"] for ev in events if ev["type"] == "token"]
        assert any("检测到重复的工具调用，已终止循环" in t for t in tokens)
        assert events[-1]["type"] == "complete"
        assert "已终止循环" in events[-1]["content"]


# ════════════════════════════════════════════════════════════════
# 4. 可见性：auto=False 压缩也发 auto_compact（trigger:"tool"）
# ════════════════════════════════════════════════════════════════

def _mk_compact_stub_agent():
    """真实构造 agent（同 test_turn_increment 模式），compact 桩固定形态。"""
    agent = HermesAgentV3(MagicMock())
    agent.context_manager = MagicMock()
    compressed = [
        {"role": "system", "content": "摘要：S"},
        {"role": "assistant", "content": "近尾"},
    ]
    agent.context_manager.compact_messages.return_value = CompactResult(
        compressed_messages=compressed,
        summary="S", original_count=5, compacted_count=4,
    )
    return agent, compressed


class TestAutoCompactEventVisibility:

    def test_tool_trigger_path_emits_auto_compact_with_trigger_field(self):
        """auto=False（compact_conversation 触发）：发 auto_compact 事件且
        payload 带 trigger:"tool"（CLI 渲染读 compacted_count/original_count，
        字段名与其 cli.py auto_compact 分支一致）。"""
        agent, compressed = _mk_compact_stub_agent()
        state = {"messages": [{"role": "user", "content": "a"},
                              {"role": "assistant", "content": "b"}]}
        events = list(agent._compact_messages(state, auto=False))
        assert events[0] == {
            "type": "auto_compact",
            "compacted_count": 4,
            "original_count": 5,
            "trigger": "tool",
        }
        assert events[1]["type"] == "messages_snapshot"
        assert state["messages"] == compressed

    def test_auto_threshold_path_keeps_payload_unchanged(self):
        """auto=True（阈值触发）：事件 payload 不加 trigger——既有消费方
        tests/test_agent_events.py::test_threshold_auto_compact_on_first_step
        锁定字典全等，字段集不能漂移。"""
        agent, _ = _mk_compact_stub_agent()
        state = {"messages": [{"role": "user", "content": "a"},
                              {"role": "assistant", "content": "b"}]}
        events = list(agent._compact_messages(state, auto=True))
        assert events[0] == {
            "type": "auto_compact", "compacted_count": 4, "original_count": 5,
        }

    def test_failed_compact_emits_no_event(self):
        """压缩失败/无压缩空间（context_manager 返回 None）：不发事件，
        state 不动——闸门标记在消费点仍置位（见 TestPerTurnCompactGate）。"""
        agent = HermesAgentV3(MagicMock())
        agent.context_manager = MagicMock()
        agent.context_manager.compact_messages.return_value = None
        state = {"messages": [{"role": "user", "content": "a"}]}
        events = list(agent._compact_messages(state, auto=False))
        assert events == []
        assert state["messages"] == [{"role": "user", "content": "a"}]


# ════════════════════════════════════════════════════════════════
# 5. 闸门改写的作用域：并发批量里只改写 compact 那条 tool 结果
# ════════════════════════════════════════════════════════════════

class TestGateRewriteScoping:

    def test_rewrite_only_touches_compact_tool_results(self, monkeypatch):
        """同批并发 compact + echo：闸门只改写 compact_conversation 的
        tool 结果，echo 结果原样保留。"""
        echo_exec = FakeExecutor(content="回声")
        agent = make_agent(monkeypatch, specs=[
            compact_spec(),
            make_tool_spec(name="echo", executor=echo_exec),
        ])
        calls_box: list = []
        patch_compact(agent, calls_box)
        llm = RecordingLLM(rounds=[
            parallel_tool_call_chunks(
                ("compact_conversation", "c1", "{}"), ("echo", "c2", "{}")),
            parallel_tool_call_chunks(
                ("compact_conversation", "c3", "{}"), ("echo", "c4", "{}")),
            [Chunk(content_delta="最终")],
        ])
        agent._llm_client = llm

        run_turn(agent)

        # 闸门：压缩仍只执行一次
        assert agent.context_manager.compact_messages.call_count == 1
        second_tools = [m for m in llm.calls[2]["messages"] if m.get("role") == "tool"]
        by_id = {m["tool_call_id"]: m["content"] for m in second_tools}
        assert GATE_MARK in by_id["c3"]
        assert by_id["c4"] == "回声"
        # 首批两条都不被改写
        first_tools = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
        assert all(GATE_MARK not in m["content"] for m in first_tools)
