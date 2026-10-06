"""
T6 agent 循环事件化测试 —— 等价性 + durable 事件 + 事件面（kernel/standalone）。

行为锁：对外 13 种 UI 事件 dict 契约一字不变（现有 agent 测试即锁），
本文件补充验证：
1. 完整两轮 ReAct（先 tool_calls 后纯文本）下的 UI 事件序列与 durable
   事件序列（turn/start 含 workspace_mode、user/assistant/tool/compact）
2. 不变量："模型可见即可从日志重建"——mock LLM 捕获每次请求 messages，
   发请求那一刻 SessionLog.derive_messages == 该请求 messages 去掉 system
   项；轮末 derive == 最后一次请求去 system + 最终 assistant 消息
3. agent/pre-step 监听器：改写 messages 生效、reject=True 不发 LLM 请求
   且 durable 有 turn/end
4. agent/request 改写生效；agent/turn-stopping serial 被调
5. kernel 路径 vs standalone 路径等价（同一 mock 下事件序列一致）
6. llm/stream 恰好一个默认监听器（kernel ctx 下不重复；上游已有默认时
   agent 不再注册自己的）
7. HITL resume：turn/start{resume}、interrupt/resolved、工具续跑 durable 齐全
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.agent.agent_v3 import HermesAgentV3, StepRequest
from src.agent.hitl import InterruptSignal, InterruptStore
from src.agent.registry_v3 import ToolRegistryV3
from src.agent.session_log import (
    SessionLog,
    ASSISTANT_MSG,
    COMPACT_APPLIED,
    INTERRUPT_REQUESTED,
    INTERRUPT_RESOLVED,
    SCOPE_INTERRUPT,
    TOOL_CALL,
    TOOL_RESULT,
    TURN_END,
    TURN_START,
    USER_MSG,
)
from src.agent.tool_result import ToolResult
from src.llm.messages import Chunk
from src.storage import paths
from src.tools.context import ToolContext
from src.tools.executor_base import ToolExecutor
from src.tools.schema import SideEffects, ToolSpec


# ════════════════════════════════════════════════════════════════
# 测试设施：假 LLM / 假工具 / agent 工厂
# ════════════════════════════════════════════════════════════════

class RecordingLLM:
    """mock LLMClient：按脚本回放 chunk 流，记录每次请求。

    derived_at_request：发每次请求那一刻的 derive_messages 快照
    （不变量断言用——异常若在 stream_chat 里抛会被 agent 的兜底
    except 吞掉，所以这里只记录、测试体里断言）。
    """

    def __init__(self, rounds, session_log=None, sid=""):
        self.rounds = [list(r) for r in rounds]
        self.calls: list[dict] = []
        self.derived_at_request: list = []
        self.session_log = session_log
        self.sid = sid

    def stream_chat(self, messages, tools=None, temperature=None, max_tokens=None):
        self.calls.append({
            "messages": [dict(m) for m in messages],
            "tools": tools,
        })
        if self.session_log is not None:
            self.derived_at_request.append(self.session_log.derive_messages(self.sid))
        else:
            self.derived_at_request.append(None)
        return iter(list(self.rounds.pop(0)))


class FakeExecutor(ToolExecutor):
    """假执行器：固定 content + state_updates，记录调用。"""

    def __init__(self, content="ok", state_updates=None):
        self.result = ToolResult(content=content, state_updates=dict(state_updates or {}))
        self.calls: list[dict] = []

    def execute(self, args, ctx) -> ToolResult:
        self.calls.append(dict(args))
        return self.result


def make_tool_spec(name="echo", destructive=False, executor=None):
    return ToolSpec(
        name=name,
        description="test tool",
        parameters={"type": "object", "properties": {}},
        executor=executor or FakeExecutor(),
        side_effects=SideEffects(destructive=destructive),
        se_evaluator=None,
    )


def tool_call_chunks(name="echo", args_json="{\"q\": \"hi\"}", call_id="call_1"):
    return [Chunk(tool_call_deltas=[{
        "index": 0, "id": call_id,
        "function": {"name": name, "arguments": args_json},
    }])]


TOOL_CALL_CHUNK = tool_call_chunks("echo")
ROUND_TOOL = [Chunk(reasoning_delta="思考")] + TOOL_CALL_CHUNK
ROUND_TEXT = [Chunk(content_delta="最终"), Chunk(content_delta="回答")]


def make_memory_manager():
    mm = MagicMock(name="memory_manager")
    mm.search_with_detail.return_value = {
        "filtered_results": [{"memory": "旧记忆", "score": 0.9}],
        "raw_count": 3,
        "hit_count": 1,
    }
    return mm


def make_agent(monkeypatch, specs=None, kernel_ctx=None, session_log=None,
               interrupt_store=None, memory_manager=None):
    """构造真实 HermesAgentV3（不连 LLM/MCP，工具列表注入假 spec）。

    resolve_tools 的 patch 必须覆盖 stream_invoke 的整个消费区间
    （generator 体在迭代时才执行 _resolve_and_bind_tools），故用
    monkeypatch 挂到测试结束。注意 src.tools 包的 remember 属性被
    同名工具对象遮蔽，set_memory_manager 只能用 mock.patch 解析。
    """
    import importlib

    mm = memory_manager or make_memory_manager()
    agent_v3_mod = importlib.import_module("src.agent.agent_v3")
    monkeypatch.setattr(
        agent_v3_mod, "resolve_tools",
        lambda ctx, settings=None, include_mcp=True: list(specs or []),
    )
    with patch("src.tools.remember.set_memory_manager"):
        return HermesAgentV3(
            mm,
            registry=ToolRegistryV3(),
            interrupt_store=interrupt_store,
            kernel_ctx=kernel_ctx,
            session_log=session_log,
        )


@pytest.fixture
def data_root(tmp_path):
    """数据根重定向到 tmp（SQLite 落临时库），测后恢复。"""
    paths.set_data_root(tmp_path)
    yield tmp_path
    paths.set_data_root(None)


def run_turn(agent, user_input="查一下", **kw):
    """跑一轮 stream_invoke，收集全部 UI 事件。"""
    return list(agent.stream_invoke("u1", user_input, **kw))


def event_types(events):
    return [ev.get("type") for ev in events]


def find_event(events, etype):
    matched = [ev for ev in events if ev.get("type") == etype]
    assert matched, f"缺少事件 {etype}，实际序列: {event_types(events)}"
    return matched[0]


def durable_types(log, sid):
    return [ev["type"] for ev in log.events(sid)]


def fingerprint(events):
    """事件序列指纹（kernel vs standalone 等价性比较用）。"""
    fps = []
    for ev in events:
        if ev.get("type") == "turn_messages":
            fps.append(("turn_messages", ev.get("partial"), [
                (type(m).__name__, getattr(m, "content", None),
                 tuple(sorted((getattr(m, "additional_kwargs", None) or {}).keys())))
                for m in ev["messages"]
            ]))
        else:
            fps.append(json.dumps(ev, ensure_ascii=False, sort_keys=True, default=str))
    return fps


# ════════════════════════════════════════════════════════════════
# 1. 完整两轮 ReAct：UI 契约 + durable 事件 + 不变量
# ════════════════════════════════════════════════════════════════

class TestFullTurn:

    @pytest.fixture
    def setup(self, data_root, monkeypatch):
        log = SessionLog()
        spec = make_tool_spec(name="echo", executor=FakeExecutor(content="回声:hi"))
        agent = make_agent(monkeypatch, specs=[spec], session_log=log)
        llm = RecordingLLM(rounds=[ROUND_TOOL, ROUND_TEXT], session_log=log, sid="s1")
        agent._llm_client = llm
        return agent, llm, log, spec

    def test_ui_event_sequence_and_contract(self, setup):
        """UI 事件序列与改造前契约一致（dict 字段一字不差）。"""
        agent, llm, log, spec = setup
        events = run_turn(agent, session_id="s1")

        assert event_types(events) == [
            "memory_search", "reasoning_token",
            "tool_start", "tool_end",
            "token", "token",
            "turn_messages", "complete",
        ]

        # memory_search：query/raw_count/hit_count/hits（MemoryOrchestrator 详情透传；
        # mock 的 filtered_results 无 detail → hits 条目 detail=None）
        assert find_event(events, "memory_search") == {
            "type": "memory_search",
            "query": "查一下",
            "raw_count": 3,
            "hit_count": 1,
            "hits": [{"memory": "旧记忆", "score": 0.9, "detail": None}],
        }
        # token / reasoning_token
        assert find_event(events, "reasoning_token") == {
            "type": "reasoning_token", "content": "思考",
        }
        tokens = [ev["content"] for ev in events if ev["type"] == "token"]
        assert tokens == ["最终", "回答"]
        # tool_start / tool_end（registry event_sink 透传）
        assert find_event(events, "tool_start") == {
            "type": "tool_start",
            "tool_name": "echo",
            "tool_args": {"q": "hi"},
            "tool_id": "call_1",
        }
        assert find_event(events, "tool_end") == {
            "type": "tool_end",
            "tool_name": "echo",
            "tool_id": "call_1",
            "result": "回声:hi",
        }
        # turn_messages / complete
        tm = find_event(events, "turn_messages")
        assert tm["partial"] is False
        # T7 后 state 全 OpenAI dict（不再有 langchain 消息对象）
        assert [m["role"] for m in tm["messages"]] == [
            "user", "assistant", "tool", "assistant",
        ]
        assert find_event(events, "complete") == {
            "type": "complete", "content": "最终回答",
        }
        # 工具确实执行了一次（强转后的 args）
        assert spec.executor.calls == [{"q": "hi"}]

    def test_durable_event_sequence(self, setup):
        """durable 事件齐全：turn/start（含 workspace_mode）、user/assistant/tool。"""
        agent, llm, log, spec = setup
        events = run_turn(agent, session_id="s1")

        assert durable_types(log, "s1") == [
            TURN_START, USER_MSG, ASSISTANT_MSG, TOOL_CALL, TOOL_RESULT,
            ASSISTANT_MSG, TURN_END,
        ]
        evs = log.events("s1")

        # turn/start：input + 本轮工作区模式（未配置 = None，但键必须在）
        assert evs[0]["payload"]["input"] == "查一下"
        assert "workspace_mode" in evs[0]["payload"]

        assert evs[1]["payload"] == {"content": "查一下"}

        # 带 tool_calls 的 assistant/message（OpenAI 格式，比旧 shim 完整）
        tc_payload = evs[2]["payload"]
        assert tc_payload["content"] == ""
        assert len(tc_payload["tool_calls"]) == 1
        assert tc_payload["tool_calls"][0]["id"] == "call_1"
        assert tc_payload["tool_calls"][0]["type"] == "function"
        assert tc_payload["tool_calls"][0]["function"]["name"] == "echo"
        assert json.loads(tc_payload["tool_calls"][0]["function"]["arguments"]) == {"q": "hi"}

        assert evs[3]["payload"] == {
            "tool_call_id": "call_1", "name": "echo", "args": {"q": "hi"},
        }
        assert evs[4]["payload"]["tool_call_id"] == "call_1"
        assert evs[4]["payload"]["name"] == "echo"
        assert evs[4]["payload"]["content"] == "回声:hi"

        assert evs[5]["payload"] == {"content": "最终回答"}
        assert evs[6]["payload"]["message_count"] == 4

    def test_invariant_derive_equals_requests_minus_system(self, setup):
        """不变量："模型可见即可从日志重建"。

        - 发每次 LLM 请求那一刻：derive_messages == 该请求 messages 去 system
        - 轮末：derive == 最后一次请求去 system + 最终 assistant 消息
        """
        agent, llm, log, spec = setup
        events = run_turn(agent, session_id="s1")

        assert len(llm.calls) == 2
        for i, call in enumerate(llm.calls):
            expected = [m for m in call["messages"] if m.get("role") != "system"]
            assert llm.derived_at_request[i] == expected, f"第 {i + 1} 次请求前日志投影不完整"

        last_request = [m for m in llm.calls[-1]["messages"] if m.get("role") != "system"]
        assert log.derive_messages("s1") == last_request + [
            {"role": "assistant", "content": "最终回答"},
        ]
        # 投影里的 assistant tool_calls 与请求同构（OpenAI 格式透传）
        derived = log.derive_messages("s1")
        assert derived[1]["role"] == "assistant"
        assert json.loads(derived[1]["tool_calls"][0]["function"]["arguments"]) == {"q": "hi"}
        assert derived[2] == {
            "role": "tool", "tool_call_id": "call_1", "content": "回声:hi",
        }

    def test_empty_session_id_writes_nothing(self, setup):
        """session_id 空串时不写 durable 事件。"""
        agent, llm, log, spec = setup
        run_turn(agent, session_id="")
        assert durable_types(log, "s1") == []


# ════════════════════════════════════════════════════════════════
# 2. 压缩：阈值 auto 压缩 + compact_requested 再压缩
# ════════════════════════════════════════════════════════════════

class TestCompact:

    def _patch_compact(self, agent, summary="对话摘要", original=5, compacted=4, kept=None):
        """R2-9：用真实形态 CompactResult（summary system + keep 保留集），
        而非 summary-only 的 fake（后者掩盖了保留区丢失问题）。"""
        from src.agent.context import CompactResult

        kept_messages = list(kept if kept is not None else [
            {"role": "user", "content": "保留问题"},
            {"role": "assistant", "content": "保留回答"},
        ])

        def fake_compact(messages, keep_count=None):
            return CompactResult(
                compressed_messages=[
                    {"role": "system", "content": f"摘要：{summary}"},
                ] + [dict(m) for m in kept_messages],
                summary=summary,
                original_count=original,
                compacted_count=compacted,
            )

        agent.context_manager.compact_messages = fake_compact
        return kept_messages

    def test_threshold_auto_compact_on_first_step(self, data_root, monkeypatch):
        """启动超阈值 → 首步 pre-step 压缩一次（auto_compact + messages_snapshot +
        durable compact/applied，计数与摘要来自 CompactResult）。"""
        monkeypatch.setattr("src.agent.token_counter.count_tokens", lambda msgs: 999999)
        monkeypatch.setattr("src.agent.context_window.get_context_window", lambda: 100000)

        log = SessionLog()
        agent = make_agent(monkeypatch, specs=[make_tool_spec()], session_log=log)
        kept = self._patch_compact(agent, summary="S", original=1, compacted=1)
        agent._llm_client = RecordingLLM(rounds=[ROUND_TEXT])

        events = run_turn(agent, session_id="sc")

        assert event_types(events) == [
            "memory_search", "auto_compact", "messages_snapshot",
            "token", "token", "turn_messages", "complete",
        ]
        assert find_event(events, "auto_compact") == {
            "type": "auto_compact", "compacted_count": 1, "original_count": 1,
        }
        assert durable_types(log, "sc") == [
            TURN_START, USER_MSG, COMPACT_APPLIED,
            ASSISTANT_MSG, TURN_END,
        ]
        compact_ev = log.events("sc")[2]
        # R2-9：compact 事件带 kept_messages 保留区（lc_to_dict 规范化）
        assert compact_ev["payload"]["summary"] == "S"
        assert compact_ev["payload"]["original_count"] == 1
        assert compact_ev["payload"]["compacted_count"] == 1
        assert compact_ev["payload"]["kept_messages"] == kept

    def test_compact_retention_zone_survives_reload(self, data_root, monkeypatch):
        """R2-9 不变量（真实压缩形态）：压缩后重载（derive_messages）必须
        还原 [system:summary] + 保留区——重载后保留区消息不丢。"""
        monkeypatch.setattr("src.agent.token_counter.count_tokens", lambda msgs: 999999)
        monkeypatch.setattr("src.agent.context_window.get_context_window", lambda: 100000)

        log = SessionLog()
        agent = make_agent(monkeypatch, specs=[make_tool_spec()], session_log=log)
        kept = [
            {"role": "user", "content": "保留问题"},
            {"role": "assistant", "content": "保留回答"},
        ]
        self._patch_compact(agent, summary="S", original=1, compacted=1, kept=kept)
        agent._llm_client = RecordingLLM(rounds=[ROUND_TEXT])

        run_turn(agent, session_id="scr")

        # 重载（derive）= [system:summary] + 保留区 + 本轮最终 assistant
        # ——压缩真实保留了最后 N 条，事件流重放后保留区在场
        assert log.derive_messages("scr") == [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "保留问题"},
            {"role": "assistant", "content": "保留回答"},
            {"role": "assistant", "content": "最终回答"},
        ]

    def test_compact_requested_signal_mid_loop(self, data_root, monkeypatch):
        """compact_conversation 工具信号 → 循环中再压缩（2026-09-06 起
        auto=False 同样发 auto_compact UI 事件、带 trigger:"tool" 标注来源，
        durable compact/applied 照写 + messages_snapshot 快照）。"""
        log = SessionLog()
        spec = make_tool_spec(
            name="compact_conversation",
            executor=FakeExecutor(content="已压缩", state_updates={"compact_requested": True}),
        )
        agent = make_agent(monkeypatch, specs=[spec], session_log=log)
        self._patch_compact(agent, summary="S2", original=3, compacted=2)
        llm = RecordingLLM(rounds=[
            tool_call_chunks("compact_conversation"), ROUND_TEXT,
        ])
        agent._llm_client = llm

        events = run_turn(agent, session_id="sc2")

        types = event_types(events)
        # tool_end 之后（压缩落定）先 auto_compact 再 messages_snapshot
        assert types.index("tool_end") < types.index("auto_compact")
        assert types.index("auto_compact") < types.index("messages_snapshot")
        assert find_event(events, "auto_compact") == {
            "type": "auto_compact", "compacted_count": 2, "original_count": 3,
            "trigger": "tool",
        }
        assert durable_types(log, "sc2") == [
            TURN_START, USER_MSG, ASSISTANT_MSG, TOOL_CALL, TOOL_RESULT,
            COMPACT_APPLIED, ASSISTANT_MSG, TURN_END,
        ]
        compact_payload = [e["payload"] for e in log.events("sc2") if e["type"] == COMPACT_APPLIED][0]
        assert compact_payload["summary"] == "S2"
        assert compact_payload["original_count"] == 3
        assert compact_payload["compacted_count"] == 2
        # R2-9：mid-loop 压缩同样携带保留区
        assert compact_payload["kept_messages"] == [
            {"role": "user", "content": "保留问题"},
            {"role": "assistant", "content": "保留回答"},
        ]


# ════════════════════════════════════════════════════════════════
# 3. agent/pre-step：外部监听器改写 / reject
# ════════════════════════════════════════════════════════════════

class TestPreStepListeners:

    def test_external_listener_rewrites_messages(self, data_root, monkeypatch):
        """外部 pre-step 监听器改写 messages → LLM 收到改写后版本。"""

        agent = make_agent(monkeypatch, specs=[make_tool_spec()])
        llm = RecordingLLM(rounds=[ROUND_TEXT])
        agent._llm_client = llm

        def rewriter(req: StepRequest, next):
            req.messages = list(req.messages) + [
                {"role": "user", "content": "外部注入:secret"}
            ]
            return next()

        agent.scope.on("agent/pre-step", rewriter)
        run_turn(agent, session_id="")

        assert len(llm.calls) == 1
        contents = [m.get("content") for m in llm.calls[0]["messages"]]
        assert "外部注入:secret" in contents
        # 注入的是历史消息（system 之后、本轮 user 之前也行，必须在场）
        assert llm.calls[0]["messages"][0]["role"] == "system"

    def test_reject_skips_llm_and_closes_turn(self, data_root, monkeypatch):
        """reject=True → 无 LLM 调用、直收 turn、durable 有 turn/end。"""
        log = SessionLog()
        agent = make_agent(monkeypatch, specs=[make_tool_spec()], session_log=log)
        llm = RecordingLLM(rounds=[ROUND_TEXT])
        agent._llm_client = llm

        def rejector(req: StepRequest, next):
            req.reject = True
            return next()

        agent.scope.on("agent/pre-step", rejector)
        events = run_turn(agent, session_id="sr")

        assert llm.calls == []
        assert event_types(events) == ["memory_search", "turn_messages", "complete"]
        assert find_event(events, "complete") == {"type": "complete", "content": ""}
        assert durable_types(log, "sr") == [TURN_START, USER_MSG, ASSISTANT_MSG, TURN_END]

    def test_memory_retrieved_once_per_turn(self, data_root, monkeypatch):
        """记忆检索频次不变：一次 stream_invoke 只检索一次（多迭代不重复）。"""
        agent = make_agent(monkeypatch, specs=[make_tool_spec()])
        agent._llm_client = RecordingLLM(rounds=[[TOOL_CALL_CHUNK], ROUND_TEXT])
        events = run_turn(agent, session_id="")
        assert event_types(events).count("memory_search") == 1


# ════════════════════════════════════════════════════════════════
# 4. agent/request + agent/turn-stopping
# ════════════════════════════════════════════════════════════════

class TestRequestAndTurnStopping:

    def test_request_listener_rewrite_applied(self, data_root, monkeypatch):
        """agent/request 监听器改写 messages → 实际请求用改写后版本。"""
        agent = make_agent(monkeypatch, specs=[make_tool_spec()])
        llm = RecordingLLM(rounds=[ROUND_TEXT])
        agent._llm_client = llm

        def rewriter(payload: dict, next):
            payload["messages"] = payload["messages"] + [
                {"role": "user", "content": "REQ注入"},
            ]
            return next()

        agent.scope.on("agent/request", rewriter)
        run_turn(agent, session_id="")

        assert len(llm.calls) == 1
        assert llm.calls[0]["messages"][-1] == {"role": "user", "content": "REQ注入"}

    def test_turn_stopping_serial_dispatched(self, data_root, monkeypatch):
        """agent/turn-stopping（serial）在循环结束、complete 前被调一次。"""
        agent = make_agent(monkeypatch, specs=[make_tool_spec()])
        agent._llm_client = RecordingLLM(rounds=[ROUND_TEXT])

        seen: list[dict] = []

        def stopper(state: dict):
            seen.append(state)

        agent.scope.on("agent/turn-stopping", stopper)
        events = run_turn(agent, session_id="")

        assert len(seen) == 1
        assert isinstance(seen[0], dict) and "messages" in seen[0]
        # 分发时机：complete 事件之前已经跑过（yield complete 前同步分发）
        assert "complete" in event_types(events)


# ════════════════════════════════════════════════════════════════
# 5. kernel 路径 vs standalone 路径等价
# ════════════════════════════════════════════════════════════════

class TestKernelStandaloneEquivalence:

    def test_same_event_sequence(self, data_root, monkeypatch):
        """同一 mock 下 kernel（scope 于父 ctx）与 standalone 事件序列一致。"""
        from src.cordis.context import Context

        def run(kernel_ctx):
            agent = make_agent(monkeypatch, specs=[make_tool_spec()], kernel_ctx=kernel_ctx)
            agent._llm_client = RecordingLLM(rounds=[TOOL_CALL_CHUNK, ROUND_TEXT])
            return run_turn(agent, session_id="")

        events_kernel = run(Context(name="kernel"))
        events_standalone = run(None)

        assert fingerprint(events_kernel) == fingerprint(events_standalone)
        assert event_types(events_kernel) == [
            "memory_search", "tool_start", "tool_end",
            "token", "token", "turn_messages", "complete",
        ]


# ════════════════════════════════════════════════════════════════
# 6. llm/stream：恰好一个默认监听器
# ════════════════════════════════════════════════════════════════

class TestLlmStreamSingleDefault:

    def test_exactly_one_default_under_booted_kernel(self, data_root, monkeypatch):
        """真实 boot 的 kernel ctx 下（llm 插件在场 + R2-13 监听器提升）：
        插件默认监听器对 agent 可见——agent 不再注册自己的默认监听器，
        实际流式恰好一次且由插件默认监听器完成（不再重复注册）。"""
        from src.cordis.loader import boot

        ctx = boot([
            {"id": "config", "plugin": "src.plugins.config_plugin:apply"},
            {"id": "llm", "plugin": "src.plugins.llm_plugin:apply", "inject": ["config"]},
        ])
        try:
            agent = make_agent(monkeypatch, specs=[make_tool_spec()], kernel_ctx=ctx)
            # 插件默认监听器优先用 req.client——把 agent 的客户端接成记录器
            # （等价 worker 的 set_llm_params / 测试注入 mock 的真实路径）
            llm = RecordingLLM(rounds=[ROUND_TEXT])
            monkeypatch.setattr(agent, "_get_llm_client", lambda: llm)

            # agent 未注册自己的 llm/stream 默认监听器（上游提升来的已可见）
            with agent.scope._lock:
                assert not agent.scope._listeners.get("llm/stream"), (
                    "上游已有插件默认监听器时 agent 不得重复注册"
                )

            # 根 ctx 上的外部观察者经冒泡可见 agent 环节
            seen_payloads: list[dict] = []
            ctx.on("agent/request", lambda payload, next: (
                seen_payloads.append(payload), next(),
            )[1])

            events = run_turn(agent, session_id="")
            assert len(llm.calls) == 1  # 恰一次实际流式（由插件默认监听器完成）
            assert find_event(events, "complete") == {"type": "complete", "content": "最终回答"}
            assert len(seen_payloads) == 1  # 根观察者冒泡可见
            assert seen_payloads[0]["messages"][0]["role"] == "system"
        finally:
            ctx.teardown()

    def test_booted_kernel_passes_llm_param_overrides(self, data_root, monkeypatch):
        """R2-13：booted kernel 路径下 set_llm_params 的参数经 StreamRequest
        透传到 stream_chat（插件默认监听器执行流式，覆盖值不丢）。"""
        from src.cordis.loader import boot

        ctx = boot([
            {"id": "config", "plugin": "src.plugins.config_plugin:apply"},
            {"id": "llm", "plugin": "src.plugins.llm_plugin:apply", "inject": ["config"]},
        ])
        try:
            seen_params: list[dict] = []

            class ParamLLM:
                def stream_chat(self, messages, tools=None, temperature=None, max_tokens=None):
                    seen_params.append({"temperature": temperature, "max_tokens": max_tokens})
                    return iter(list(ROUND_TEXT))

            agent = make_agent(monkeypatch, specs=[make_tool_spec()], kernel_ctx=ctx)
            agent.set_llm_params(temperature=0.11, max_tokens=777)
            monkeypatch.setattr(agent, "_get_llm_client", lambda: ParamLLM())

            run_turn(agent, session_id="")
            assert len(seen_params) == 1
            assert seen_params[0] == {"temperature": 0.11, "max_tokens": 777}
        finally:
            ctx.teardown()

    def test_agent_handled_short_circuit_yields_empty_stream(self, data_root, monkeypatch):
        """R2-13：agent 侧 llm/stream 监听器短路 + handled=True 且不给
        chunks → 尊重短路（空流收尾），不直连真 LLM。"""
        from src.cordis.context import Context

        upstream = Context(name="upstream-block")

        def blocker(req, next):
            req.handled = True
            return req  # 短路：不给 chunks

        upstream.on("llm/stream", blocker)

        agent = make_agent(monkeypatch, specs=[make_tool_spec()], kernel_ctx=upstream)
        llm = RecordingLLM(rounds=[ROUND_TEXT])
        agent._llm_client = llm

        events = run_turn(agent, session_id="")
        assert llm.calls == [], "handled 短路后不得兜底直连"
        # 空流收尾：无 content → complete 内容为空
        assert find_event(events, "complete") == {"type": "complete", "content": ""}

    def test_upstream_default_wins_no_duplicate(self, data_root, monkeypatch):
        """祖先 ctx 已有 llm/stream 默认监听器时 agent 不再注册自己的：
        上游的 chunks 生效、agent 客户端零调用（无双重流式）。"""
        from src.cordis.context import Context

        upstream = Context(name="upstream-llm")

        def upstream_default(req, next):
            req.chunks = iter([Chunk(content_delta="上游流")])
            return next()

        upstream.on("llm/stream", upstream_default)

        agent = make_agent(monkeypatch, specs=[make_tool_spec()], kernel_ctx=upstream)
        llm = RecordingLLM(rounds=[ROUND_TEXT])
        agent._llm_client = llm

        events = run_turn(agent, session_id="")
        assert llm.calls == []  # agent 默认监听器未注册，客户端未触达
        assert find_event(events, "complete") == {"type": "complete", "content": "上游流"}


# ════════════════════════════════════════════════════════════════
# 7. HITL interrupt → resume（durable 事件 + decision 传全）
# ════════════════════════════════════════════════════════════════

class TestHitlDurableEvents:

    def test_interrupt_then_resume_approve(self, data_root, monkeypatch):
        """destructive 工具 before_changes 下：中断轮写 interrupt/requested +
        turn/end；resume(approve) 写 turn/start{resume}、interrupt/resolved、
        tool/result（R3-18：不再重复 tool/call——中断轮 event_sink 已写过）、
        assistant/message、turn/end；resume 不再检索记忆。"""
        log = SessionLog()
        store = InterruptStore(session_log=log)
        spec = make_tool_spec(name="rm", destructive=True, executor=FakeExecutor(content="已删除"))
        agent = make_agent(
            monkeypatch, specs=[spec], session_log=log, interrupt_store=store,
        )
        # 工具名对齐：chunk 的 function.name 指向 rm（带 reasoning chunk）
        llm = RecordingLLM(rounds=[
            [Chunk(reasoning_delta="思考")] + tool_call_chunks("rm"),
            ROUND_TEXT,
        ])
        agent._llm_client = llm

        # ── 第一轮：触发中断 ──
        # 注：中断轮的 tool_start 在 event_sink 里即时映射出 tool/call
        # durable 事件（UI 侧因异常冒泡未 yield tool_start，与改造前一致）
        events1 = run_turn(agent, session_id="sh", thread_id="sh")
        assert event_types(events1) == ["memory_search", "reasoning_token", "human_approval_request"]
        assert durable_types(log, "sh") == [
            TURN_START, USER_MSG, ASSISTANT_MSG, TOOL_CALL, TURN_END,
        ]
        # interrupt 事件流（scope=interrupt，session_id 列存 thread_id）
        interrupt_events = log.provider.iter_events(SCOPE_INTERRUPT, "sh")
        assert [e["type"] for e in interrupt_events] == [INTERRUPT_REQUESTED]

        # ── 第二轮：approve 恢复 ──
        events2 = list(agent.stream_invoke(
            "u1", "(resume)", session_id="sh", thread_id="sh", resume_payload="approve",
        ))
        assert event_types(events2) == [
            "approval_result", "tool_start", "tool_end",
            "token", "token", "turn_messages", "complete",
        ]
        # resume 不检索记忆（memory_search 不在）
        assert "memory_search" not in event_types(events2)
        assert find_event(events2, "complete") == {"type": "complete", "content": "最终回答"}

        # durable：第一轮 + resume 轮（turn/start{resume} + tool/result 闭环 +
        # 收尾）。R3-18：resume 轮不写 tool/call（中断轮已写过，不重复）
        assert durable_types(log, "sh") == [
            TURN_START, USER_MSG, ASSISTANT_MSG, TOOL_CALL, TURN_END,          # 第一轮（中断）
            TURN_START, TOOL_RESULT, ASSISTANT_MSG, TURN_END,                  # resume 轮
        ]
        evs = log.events("sh")
        assert evs[5]["payload"] == {"input": "(resume)", "resume": "approve"}
        assert evs[6]["payload"] == {
            "tool_call_id": "call_1", "name": "rm", "content": "已删除",
        }
        assert evs[7]["payload"] == {"content": "最终回答"}

        # interrupt 流补上 resolved（decision=approve）
        interrupt_events = log.provider.iter_events(SCOPE_INTERRUPT, "sh")
        assert [e["type"] for e in interrupt_events] == [INTERRUPT_REQUESTED, INTERRUPT_RESOLVED]
        assert interrupt_events[1]["payload"] == {"thread_id": "sh", "decision": "approve"}
        # 工具在 approve 后真正执行了一次
        assert spec.executor.calls == [{"q": "hi"}]

    def test_resume_reject_records_reason(self, data_root, monkeypatch):
        """resume('reject:太危险') → interrupt/resolved 记 decision=reject + reason。"""
        log = SessionLog()
        store = InterruptStore(session_log=log)
        spec = make_tool_spec(name="rm", destructive=True, executor=FakeExecutor())
        agent = make_agent(monkeypatch, specs=[spec], session_log=log, interrupt_store=store)
        llm = RecordingLLM(rounds=[
            [Chunk(tool_call_deltas=[{
                "index": 0, "id": "call_1",
                "function": {"name": "rm", "arguments": "{}"},
            }])],
            ROUND_TEXT,
        ])
        agent._llm_client = llm

        run_turn(agent, session_id="sj", thread_id="sj")
        events2 = list(agent.stream_invoke(
            "u1", "(resume)", session_id="sj", thread_id="sj",
            resume_payload="reject:太危险",
        ))

        # UI 回显事件：reason 已剥 'reject:' 指令前缀（CLI/Web 直接展示该文本，
        # 带前缀会以 "已拒绝执行 rm：reject:太危险" 形式泄漏指令语法）
        ar = find_event(events2, "approval_result")
        assert ar["decision"] == "reject"
        assert ar["reason"] == "太危险"

        interrupt_events = log.provider.iter_events(SCOPE_INTERRUPT, "sj")
        assert [e["type"] for e in interrupt_events] == [INTERRUPT_REQUESTED, INTERRUPT_RESOLVED]
        assert interrupt_events[1]["payload"] == {
            "thread_id": "sj", "decision": "reject", "reason": "太危险",
        }
        # R2-8：拒绝路径补写 tool/result——中断轮的 tool/call 悬空至此闭环
        # （事件流自洽，derive 不再依赖占位合成）
        assert durable_types(log, "sj") == [
            TURN_START, USER_MSG, ASSISTANT_MSG, TOOL_CALL, TURN_END,
            TURN_START, TOOL_RESULT, ASSISTANT_MSG, TURN_END,
        ]
        reject_result = [e for e in log.events("sj") if e["type"] == TOOL_RESULT][0]
        assert reject_result["payload"]["tool_call_id"] == "call_1"
        assert reject_result["payload"]["name"] == "rm"
        assert "命令未获批准" in reject_result["payload"]["content"]
        assert "太危险" in reject_result["payload"]["content"]

    def test_resume_reject_free_text_is_reason(self, data_root, monkeypatch):
        """CLI 契约："其他任何输入 = 拒绝（内容作为原因）"。

        自由文本（如直接输入"太危险了"）必须解析为 decision=reject +
        reason=全文——而不是把自由文本塞进 decision 字段（旧实现：
        turn/start{resume} 与 interrupt/resolved 会记 decision="太危险了"，
        审计轨迹语义被污染，拒绝成立只是因为 decision != "approve" 的巧合）。
        """
        log = SessionLog()
        store = InterruptStore(session_log=log)
        spec = make_tool_spec(name="rm", destructive=True, executor=FakeExecutor())
        agent = make_agent(monkeypatch, specs=[spec], session_log=log, interrupt_store=store)
        llm = RecordingLLM(rounds=[
            [Chunk(tool_call_deltas=[{
                "index": 0, "id": "call_1",
                "function": {"name": "rm", "arguments": "{}"},
            }])],
            ROUND_TEXT,
        ])
        agent._llm_client = llm

        run_turn(agent, session_id="sf", thread_id="sf")
        events2 = list(agent.stream_invoke(
            "u1", "(resume)", session_id="sf", thread_id="sf",
            resume_payload="太危险了",   # 自由文本，无 reject: 前缀
        ))

        # decision 恒为二值；reason = 全文
        ar = find_event(events2, "approval_result")
        assert ar["decision"] == "reject"
        assert ar["reason"] == "太危险了"
        interrupt_events = log.provider.iter_events(SCOPE_INTERRUPT, "sf")
        assert interrupt_events[1]["payload"] == {
            "thread_id": "sf", "decision": "reject", "reason": "太危险了",
        }
        # durable turn/start 的 resume 字段同样是二值 decision
        evs = log.events("sf")
        resume_start = [e for e in evs if e["type"] == TURN_START][1]
        assert resume_start["payload"] == {"input": "(resume)", "resume": "reject"}
        # 工具拒绝消息含原因、无指令前缀泄漏
        reject_result = [e for e in evs if e["type"] == TOOL_RESULT][0]
        assert "命令未获批准：太危险了" in reject_result["payload"]["content"]

    def test_resume_reject_dangling_repaired_by_derive(self, data_root, monkeypatch):
        """R2-8 悬空修复的中断→拒绝场景 derive：拒绝轮的 tool/result 与
        中断轮的 assistant(tool_calls) 配对，投影序列合法（无 LLM 400 形态）。"""
        log = SessionLog()
        store = InterruptStore(session_log=log)
        spec = make_tool_spec(name="rm", destructive=True, executor=FakeExecutor())
        agent = make_agent(monkeypatch, specs=[spec], session_log=log, interrupt_store=store)
        agent._llm_client = RecordingLLM(rounds=[
            [Chunk(tool_call_deltas=[{
                "index": 0, "id": "call_1",
                "function": {"name": "rm", "arguments": "{}"},
            }])],
            ROUND_TEXT,
        ])

        run_turn(agent, session_id="sd", thread_id="sd")
        list(agent.stream_invoke(
            "u1", "(resume)", session_id="sd", thread_id="sd",
            resume_payload="reject:不要",
        ))

        msgs = log.derive_messages("sd")
        assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]
        assert msgs[1].get("tool_calls"), "中断轮的 assistant 应带 tool_calls"
        assert msgs[2]["tool_call_id"] == "call_1"
        assert "命令未获批准" in msgs[2]["content"]
        # 序列配对合法：assistant(tool_calls) 的每个 call_id 都有紧随的 tool
        assert msgs[2]["tool_call_id"] == msgs[1]["tool_calls"][0]["id"]

    def test_resume_force_deny_writes_tool_result(self, data_root, monkeypatch):
        """R2-8：用户 approve 但执行时被安全规则硬拦截（executor 抛
        InterruptSignal，如 force_deny 类内部底线）→ 也补写 tool/result
        （content 标明拦截），事件流自洽。"""
        class DenyingExecutor(ToolExecutor):
            def __init__(self):
                self.calls = []

            def execute(self, args, ctx) -> ToolResult:
                self.calls.append(dict(args))
                raise InterruptSignal({"action": "force_deny 硬底线", "tool_name": "bomb"})

        log = SessionLog()
        store = InterruptStore(session_log=log)
        spec = make_tool_spec(name="bomb", destructive=True, executor=DenyingExecutor())
        agent = make_agent(
            monkeypatch, specs=[spec], session_log=log, interrupt_store=store,
        )
        agent._llm_client = RecordingLLM(rounds=[
            [Chunk(tool_call_deltas=[{
                "index": 0, "id": "call_1",
                "function": {"name": "bomb", "arguments": "{}"},
            }])],
        ])

        run_turn(agent, session_id="sf", thread_id="sf")
        events2 = list(agent.stream_invoke(
            "u1", "(resume)", session_id="sf", thread_id="sf", resume_payload="approve",
        ))

        # 硬拦截：告知用户 + 收 turn（工具执行被拦）
        assert find_event(events2, "approval_result")["decision"] == "approve"
        assert spec.executor.calls == [{}]
        # R3-18：resume 轮不重复写 tool/call（中断轮已写过），只补 tool/result
        assert durable_types(log, "sf") == [
            TURN_START, USER_MSG, ASSISTANT_MSG, TOOL_CALL, TURN_END,
            TURN_START, TOOL_RESULT, ASSISTANT_MSG, TURN_END,
        ]
        tr = [e for e in log.events("sf") if e["type"] == TOOL_RESULT][0]
        assert tr["payload"]["tool_call_id"] == "call_1"
        assert "force_deny" in tr["payload"]["content"]

    def test_resume_tool_missing_writes_tool_result(self, data_root, monkeypatch):
        """R2-8：resume 时工具已不可用（MCP 断开）→ 补写 tool/result，
        中断轮悬空 call_id 闭环。"""
        log = SessionLog()
        store = InterruptStore(session_log=log)
        spec = make_tool_spec(name="gone", destructive=True, executor=FakeExecutor())
        agent = make_agent(monkeypatch, specs=[spec], session_log=log, interrupt_store=store)
        agent._llm_client = RecordingLLM(rounds=[
            [Chunk(tool_call_deltas=[{
                "index": 0, "id": "call_1",
                "function": {"name": "gone", "arguments": "{}"},
            }])],
        ])

        run_turn(agent, session_id="sm", thread_id="sm")
        # 模拟工具消失（MCP 断开）：resume 时 resolve_tools 不再提供该工具
        # （stream_invoke 每次重新组装，直接 bind_tools([]) 会被覆盖）
        import importlib as _il
        agent_v3_mod = _il.import_module("src.agent.agent_v3")
        monkeypatch.setattr(agent_v3_mod, "resolve_tools", lambda ctx, settings=None, include_mcp=True: [])
        list(agent.stream_invoke(
            "u1", "(resume)", session_id="sm", thread_id="sm", resume_payload="approve",
        ))

        assert durable_types(log, "sm") == [
            TURN_START, USER_MSG, ASSISTANT_MSG, TOOL_CALL, TURN_END,
            TURN_START, TOOL_RESULT, ASSISTANT_MSG, TURN_END,
        ]
        tr = [e for e in log.events("sm") if e["type"] == TOOL_RESULT][0]
        assert tr["payload"]["tool_call_id"] == "call_1"
        assert "不可用" in tr["payload"]["content"]


# ════════════════════════════════════════════════════════════════
# 8. set_llm_params（取代 llm_with_tools mock 的参数覆盖通道）
# ════════════════════════════════════════════════════════════════

class TestSetLlmParams:

    def test_overrides_reset_client(self, data_root, monkeypatch):
        """set_llm_params 记录覆盖值并作废已构造客户端。"""
        agent = make_agent(monkeypatch, specs=[])
        agent.set_llm_params(temperature=0.2, max_tokens=512)
        assert agent._llm_temperature == 0.2
        assert agent._llm_max_tokens == 512

        fake = MagicMock(name="old-client")
        agent._llm_client = fake
        agent.set_llm_params(temperature=0.9)
        assert agent._llm_client is None  # 旧客户端作废，按新参数重建

    def test_llm_with_tools_removed(self, data_root, monkeypatch):
        """死代码清理：V3 不再有 llm_with_tools mock 属性。"""
        agent = make_agent(monkeypatch, specs=[])
        assert not hasattr(agent, "llm_with_tools")


# ════════════════════════════════════════════════════════════════
# 9. worker _op_chat 端到端（真 agent + 真 drain，验证 shim 移除后
#    durable 事件链路由 agent 完整落日志）
# ════════════════════════════════════════════════════════════════

class TestWorkerOpChatEndToEnd:

    def test_op_chat_full_chain(self, data_root, monkeypatch):
        import sys as _sys
        if not hasattr(_sys.stdin, "reconfigure"):
            _sys.stdin = MagicMock(reconfigure=lambda **kw: None)
        import web_fastapi.worker_process as wp

        log = SessionLog()
        spec = make_tool_spec(name="echo", executor=FakeExecutor(content="回声:hi"))
        agent = make_agent(monkeypatch, specs=[spec], session_log=log)
        # _op_chat 会调 set_llm_params（作废已构造客户端），所以 patch
        # _get_llm_client 而非直接注入 _llm_client
        recording = RecordingLLM(rounds=[ROUND_TOOL, ROUND_TEXT])
        monkeypatch.setattr(agent, "_get_llm_client", lambda: recording)

        bucket = wp.SessionBucket("sw")
        state = MagicMock(name="worker-state")
        state.user_id = "u1"
        state.prefs = {"temperature": 0.7, "max_tokens": 2000, "compact_threshold_pct": 80}
        state.current_sid = "sw"
        state.get_bucket = lambda sid: bucket
        state.agent = agent
        state._save_bucket = lambda b: None

        sent: list[dict] = []
        monkeypatch.setattr(wp, "_send", lambda msg: sent.append(msg))

        wp._op_chat(state, "req1", {
            "id": "req1", "op": "chat", "message": "查一下", "session_id": "sw",
        })

        # IPC 转发：token/tool/complete 等都到了 stdout 通道
        ipc_events = [m["event"] for m in sent if m.get("type") == "event"]
        assert ipc_events == [
            "memory_search", "reasoning_token", "tool_start", "tool_end",
            "token", "token", "complete",
        ]
        # done 信号
        assert any(m.get("type") == "done" for m in sent)
        # 桶回写：turn_messages 消化进 bucket（T7 后为 OpenAI dict）
        assert [m["role"] for m in bucket.messages] == [
            "user", "assistant", "tool", "assistant",
        ]
        # durable：T3 shim 移除后由 agent 循环本体写全
        assert durable_types(log, "sw") == [
            TURN_START, USER_MSG, ASSISTANT_MSG, TOOL_CALL, TOOL_RESULT,
            ASSISTANT_MSG, TURN_END,
        ]
        turn_start = log.events("sw")[0]["payload"]
        assert turn_start["input"] == "查一下"
        assert "workspace_mode" in turn_start
