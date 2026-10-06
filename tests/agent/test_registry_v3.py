"""
ToolRegistryV3 测试 —— 覆盖 execute / coerce / 串行并发 / InterruptSignal 冒泡。

测试范围：
    1. coerce_args 轻量强转（str→int/float/bool + minimum/maximum clamp）
    2. execute 三层权限（deny 返回错误、requireApproval 抛 InterruptSignal、allow 执行）
    3. process_tool_calls 串行/并发决策
    4. InterruptSignal 冒泡（审批工具强制串行 + 异常向上传播 + 并发路径穿透）
    5. state_updates 合并 + todos_update 事件
    6. mode_guidance 文本生成
    7. 未知工具名合成错误 ToolMsg（P2-3 配对不变量）
    8. 串行外层超时触发 executor cancel 钩子（P2-4 杀进程树）

"""

import subprocess
import threading
import time
import types

import pytest

import config
from src.agent.registry_v3 import ToolRegistryV3, coerce_args
from src.agent.tool_result import ToolResult
from src.agent.hitl import InterruptSignal
from src.tools.context import ToolContext
from src.tools.schema import ToolSpec, SideEffects, SideEffectsOverride
from src.tools.executor_base import ToolExecutor
from src.tools.executors import shell as shell_mod
from src.tools.executors.shell import ShellExecutor
from src.agent.mode_guidance import (
    build_mode_prompt_section,
    get_mode_display_name,
    get_mode_guidance,
)


# ════════════════════════════════════════════════════════════════
# 测试用假执行器
# ════════════════════════════════════════════════════════════════

class FakeExecutor(ToolExecutor):
    """可控的假执行器：记录调用，返回预设结果。"""

    def __init__(self, result: ToolResult, name: str = "fake"):
        self.result = result
        self.name = name
        self.calls: list[tuple[dict, ToolContext]] = []

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        self.calls.append((dict(args), ctx))
        return self.result


class ThrowingExecutor(ToolExecutor):
    """执行时抛异常的执行器。"""

    def __init__(self, exc: Exception):
        self.exc = exc

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        raise self.exc


def make_spec(
    name: str = "fake",
    destructive: bool = False,
    executor: ToolExecutor | None = None,
    parameters: dict | None = None,
    se_evaluator=None,
) -> ToolSpec:
    """构造测试用 ToolSpec。"""
    if executor is None:
        executor = FakeExecutor(ToolResult(content="ok"))
    return ToolSpec(
        name=name,
        description="test tool",
        parameters=parameters or {"type": "object", "properties": {}},
        executor=executor,
        side_effects=SideEffects(destructive=destructive),
        se_evaluator=se_evaluator,
    )


# ════════════════════════════════════════════════════════════════
# 1. coerce_args 轻量强转
# ════════════════════════════════════════════════════════════════

class TestCoerceArgs:

    def test_str_to_int(self):
        params = {
            "type": "object",
            "properties": {"timeout": {"type": "integer"}},
        }
        assert coerce_args({"timeout": "60"}, params) == {"timeout": 60}

    def test_str_to_float(self):
        params = {
            "type": "object",
            "properties": {"ratio": {"type": "number"}},
        }
        assert coerce_args({"ratio": "3.14"}, params) == {"ratio": 3.14}

    def test_str_to_bool(self):
        params = {
            "type": "object",
            "properties": {"flag": {"type": "boolean"}},
        }
        assert coerce_args({"flag": "true"}, params) == {"flag": True}
        assert coerce_args({"flag": "false"}, params) == {"flag": False}
        assert coerce_args({"flag": "yes"}, params) == {"flag": True}

    def test_int_to_int(self):
        params = {"type": "object", "properties": {"x": {"type": "integer"}}}
        assert coerce_args({"x": 42}, params) == {"x": 42}

    def test_float_to_int(self):
        """3.0 → 3（int 字段收 float 值时强转）。"""
        params = {"type": "object", "properties": {"x": {"type": "integer"}}}
        assert coerce_args({"x": 3.0}, params) == {"x": 3}

    def test_str_float_to_int(self):
        """"3.0" → 3（int 字段，字符串 "3.0" 经 float 中转）。"""
        params = {"type": "object", "properties": {"x": {"type": "integer"}}}
        assert coerce_args({"x": "3.0"}, params) == {"x": 3}

    def test_invalid_str_kept(self):
        """转不了的字符串保留原值（让 executor 自己处理）。"""
        params = {"type": "object", "properties": {"x": {"type": "integer"}}}
        result = coerce_args({"x": "abc"}, params)
        assert result["x"] == "abc"

    def test_unknown_field_kept(self):
        """未知字段保留（宽松，不剔除）。"""
        params = {"type": "object", "properties": {}}
        result = coerce_args({"extra": "value"}, params)
        assert result["extra"] == "value"

    def test_string_field_not_coerced(self):
        """string 字段不改类型。"""
        params = {"type": "object", "properties": {"name": {"type": "string"}}}
        assert coerce_args({"name": 123}, params) == {"name": 123}  # 保留原值


# P2-4：带 minimum/maximum 的数值参数 clamp 测试共用 schema（bash timeout 同形）
_CLAMP_PARAMS = {
    "type": "object",
    "properties": {
        "timeout": {"type": "integer", "minimum": 0, "maximum": 600},
    },
}


class TestCoerceArgsClamp:
    """P2-4：JSON Schema 的 minimum/maximum 约束在 coerce_args 生效。"""

    def test_above_maximum_clamped(self):
        assert coerce_args({"timeout": 9999}, _CLAMP_PARAMS) == {"timeout": 600}

    def test_below_minimum_clamped(self):
        assert coerce_args({"timeout": -5}, _CLAMP_PARAMS) == {"timeout": 0}

    def test_zero_survives_clamp(self):
        """bash timeout 的"0=不限"哨兵语义必须原样穿过 clamp。"""
        assert coerce_args({"timeout": 0}, _CLAMP_PARAMS) == {"timeout": 0}

    def test_clamp_after_string_coercion(self):
        """clamp 在类型强转之后：字符串数字先转 int 再钳区间。"""
        assert coerce_args({"timeout": "9999"}, _CLAMP_PARAMS) == {"timeout": 600}
        assert coerce_args({"timeout": "30"}, _CLAMP_PARAMS) == {"timeout": 30}

    def test_in_range_untouched(self):
        assert coerce_args({"timeout": 30}, _CLAMP_PARAMS) == {"timeout": 30}

    def test_bool_and_non_numeric_untouched(self):
        """bool（int 子类）与非数值不参与 clamp。"""
        params = {
            "type": "object",
            "properties": {"flag": {"type": "boolean", "minimum": 0}},
        }
        assert coerce_args({"flag": True}, params) == {"flag": True}
        params2 = {
            "type": "object",
            "properties": {"x": {"type": "integer", "maximum": 10}},
        }
        assert coerce_args({"x": "abc"}, params2) == {"x": "abc"}

    def test_no_bounds_no_clamp(self):
        params = {"type": "object", "properties": {"x": {"type": "integer"}}}
        assert coerce_args({"x": 10**9}, params) == {"x": 10**9}


# ════════════════════════════════════════════════════════════════
# 2. execute 三层权限
# ════════════════════════════════════════════════════════════════

class TestExecutePermission:

    def test_allow_executes(self):
        """非 destructive 工具在 before_changes 放行 + 执行。"""
        executor = FakeExecutor(ToolResult(content="done"))
        spec = make_spec(executor=executor)
        reg = ToolRegistryV3()
        ctx = ToolContext(permission_mode="before_changes")

        result = reg.execute(spec, {"x": 1}, ctx)

        assert result.content == "done"
        assert len(executor.calls) == 1
        assert executor.calls[0][0] == {"x": 1}

    def test_deny_returns_error(self):
        """destructive 工具在 plan 模式被拒绝，返回错误（不执行）。"""
        executor = FakeExecutor(ToolResult(content="should not run"))
        spec = make_spec(destructive=True, executor=executor)
        reg = ToolRegistryV3()
        ctx = ToolContext(permission_mode="plan")

        result = reg.execute(spec, {}, ctx)

        assert "错误" in result.content
        assert "计划模式" in result.content
        assert len(executor.calls) == 0  # executor 没被调用

    def test_require_approval_raises_signal(self):
        """destructive 工具在 before_changes 抛 InterruptSignal。"""
        executor = FakeExecutor(ToolResult(content="should not run"))
        spec = make_spec(destructive=True, executor=executor)
        reg = ToolRegistryV3()
        ctx = ToolContext(permission_mode="before_changes")

        with pytest.raises(InterruptSignal) as exc_info:
            reg.execute(spec, {}, ctx)

        # payload 含工具名
        assert exc_info.value.payload.get("tool_name") == "fake"
        assert len(executor.calls) == 0  # executor 没被调用

    def test_full_access_passes_destructive(self):
        """destructive 工具在 full_access 放行 + 执行。"""
        executor = FakeExecutor(ToolResult(content="executed"))
        spec = make_spec(destructive=True, executor=executor)
        reg = ToolRegistryV3()
        ctx = ToolContext(permission_mode="full_access")

        result = reg.execute(spec, {}, ctx)

        assert result.content == "executed"
        assert len(executor.calls) == 1

    def test_executor_exception_caught(self):
        """executor 抛普通异常 → 返回错误 ToolResult（不向上冒泡）。"""
        executor = ThrowingExecutor(ValueError("boom"))
        spec = make_spec(executor=executor)
        reg = ToolRegistryV3()
        ctx = ToolContext(permission_mode="full_access")

        result = reg.execute(spec, {}, ctx)

        assert "错误" in result.content
        assert "boom" in result.content

    def test_executor_interrupt_signal_propagates(self):
        """executor 抛 InterruptSignal → 向上冒泡（不被 except Exception 吞掉）。"""
        executor = ThrowingExecutor(InterruptSignal({"action": "test"}))
        spec = make_spec(executor=executor)
        reg = ToolRegistryV3()
        ctx = ToolContext(permission_mode="full_access")

        with pytest.raises(InterruptSignal):
            reg.execute(spec, {}, ctx)

    def test_coerce_before_permission(self):
        """强转发生在权限求值之前（evaluator 能看到强转后的值）。"""
        seen_args = {}

        def evaluator(args, ctx):
            seen_args.update(args)
            return SideEffectsOverride()

        executor = FakeExecutor(ToolResult(content="ok"))
        spec = make_spec(
            executor=executor,
            parameters={"type": "object", "properties": {"x": {"type": "integer"}}},
            se_evaluator=evaluator,
        )
        reg = ToolRegistryV3()
        ctx = ToolContext(permission_mode="before_changes")

        reg.execute(spec, {"x": "42"}, ctx)

        # evaluator 看到的是强转后的 int 42，不是原字符串 "42"
        assert seen_args["x"] == 42


# ════════════════════════════════════════════════════════════════
# 3. process_tool_calls 串行/并发决策
# ════════════════════════════════════════════════════════════════

class TestProcessToolCalls:

    def test_single_tool_serial(self):
        """单工具走串行路径。"""
        executor = FakeExecutor(ToolResult(content="single"))
        spec = make_spec(name="t1", executor=executor)
        reg = ToolRegistryV3()
        reg.bind_tools([spec])
        ctx = ToolContext(permission_mode="full_access")

        msgs, updates = reg.process_tool_calls(
            [{"id": "c1", "name": "t1", "args": {}}],
            ctx,
        )

        assert len(msgs) == 1
        assert msgs[0].content == "single"
        assert msgs[0].tool_call_id == "c1"
        assert len(executor.calls) == 1

    def test_unknown_tool_gets_error_msg(self):
        """P2-3：未知工具名合成错误 ToolMsg（不再静默剔除）——保持
        assistant tool_calls 与 tool 消息一一配对，模型拿到明确反馈。"""
        spec = make_spec(name="known")
        reg = ToolRegistryV3()
        reg.bind_tools([spec])
        ctx = ToolContext()

        msgs, _ = reg.process_tool_calls(
            [{"id": "c1", "name": "unknown", "args": {}}],
            ctx,
        )

        assert len(msgs) == 1
        assert msgs[0].tool_call_id == "c1"
        assert "未知工具" in msgs[0].content
        assert "unknown" in msgs[0].content

    def test_unknown_tool_only_batch(self):
        """全批未知：每个 tool_call 都有配对的错误消息。"""
        reg = ToolRegistryV3()
        reg.bind_tools([make_spec(name="known")])
        ctx = ToolContext()

        msgs, updates = reg.process_tool_calls(
            [
                {"id": "c1", "name": "ghost_a", "args": {}},
                {"id": "c2", "name": "ghost_b", "args": {}},
            ],
            ctx,
        )

        assert [m.tool_call_id for m in msgs] == ["c1", "c2"]
        assert all("未知工具" in m.content for m in msgs)
        assert updates == {}

    def test_unknown_tool_mixed_batch_keeps_order_and_results(self):
        """P2-3 混合批量：已知工具结果按原序回填，未知位置填错误消息。"""
        e1 = FakeExecutor(ToolResult(content="r1"))
        e2 = FakeExecutor(ToolResult(content="r2"))
        reg = ToolRegistryV3()
        reg.bind_tools([
            make_spec(name="t1", executor=e1),
            make_spec(name="t2", executor=e2),
        ])
        ctx = ToolContext(permission_mode="full_access")

        msgs, _ = reg.process_tool_calls(
            [
                {"id": "c1", "name": "t1", "args": {}},
                {"id": "c2", "name": "ghost", "args": {}},
                {"id": "c3", "name": "t2", "args": {}},
            ],
            ctx,
        )

        assert [m.tool_call_id for m in msgs] == ["c1", "c2", "c3"]
        assert msgs[0].content == "r1"
        assert "未知工具" in msgs[1].content
        assert msgs[2].content == "r2"
        assert len(e1.calls) == 1 and len(e2.calls) == 1

    def test_multiple_read_tools_concurrent(self):
        """多个非 destructive 工具走并发路径。"""
        e1 = FakeExecutor(ToolResult(content="r1"))
        e2 = FakeExecutor(ToolResult(content="r2"))
        s1 = make_spec(name="t1", executor=e1)
        s2 = make_spec(name="t2", executor=e2)
        reg = ToolRegistryV3()
        reg.bind_tools([s1, s2])
        ctx = ToolContext(permission_mode="full_access")

        msgs, _ = reg.process_tool_calls(
            [
                {"id": "c1", "name": "t1", "args": {}},
                {"id": "c2", "name": "t2", "args": {}},
            ],
            ctx,
        )

        # 按原始顺序返回
        assert len(msgs) == 2
        assert msgs[0].content == "r1"
        assert msgs[1].content == "r2"
        assert len(e1.calls) == 1
        assert len(e2.calls) == 1

    def test_destructive_forces_serial(self):
        """含 destructive 工具时，整批强制串行（即使有多个工具）。"""
        e1 = FakeExecutor(ToolResult(content="r1"))
        e2 = FakeExecutor(ToolResult(content="r2"))
        s1 = make_spec(name="read", executor=e1)
        s2 = make_spec(name="write", destructive=True, executor=e2)
        reg = ToolRegistryV3()
        reg.bind_tools([s1, s2])
        ctx = ToolContext(permission_mode="full_access")  # full_access 让 write 放行

        msgs, _ = reg.process_tool_calls(
            [
                {"id": "c1", "name": "read", "args": {}},
                {"id": "c2", "name": "write", "args": {}},
            ],
            ctx,
        )

        assert len(msgs) == 2
        assert msgs[0].content == "r1"
        assert msgs[1].content == "r2"

    def test_empty_calls(self):
        """空 tool_calls 列表 → 空返回。"""
        reg = ToolRegistryV3()
        ctx = ToolContext()

        msgs, updates = reg.process_tool_calls([], ctx)

        assert msgs == []
        assert updates == {}


# ════════════════════════════════════════════════════════════════
# 4. InterruptSignal 冒泡（HITL）
# ════════════════════════════════════════════════════════════════

class TestInterruptSignalPropagation:

    def test_approval_in_before_changes_propagates(self):
        """before_changes 下调 destructive 工具 → InterruptSignal 冒泡到调用方。"""
        executor = FakeExecutor(ToolResult(content="no"))
        spec = make_spec(name="write", destructive=True, executor=executor)
        reg = ToolRegistryV3()
        reg.bind_tools([spec])
        ctx = ToolContext(permission_mode="before_changes")

        with pytest.raises(InterruptSignal):
            reg.process_tool_calls(
                [{"id": "c1", "name": "write", "args": {}}],
                ctx,
            )

    def test_batch_with_approval_propagates(self):
        """批量含审批工具时，InterruptSignal 冒泡（串行路径）。"""
        e1 = FakeExecutor(ToolResult(content="read done"))
        e2 = FakeExecutor(ToolResult(content="no"))
        s1 = make_spec(name="read", executor=e1)
        s2 = make_spec(name="write", destructive=True, executor=e2)
        reg = ToolRegistryV3()
        reg.bind_tools([s1, s2])
        ctx = ToolContext(permission_mode="before_changes")

        with pytest.raises(InterruptSignal):
            reg.process_tool_calls(
                [
                    {"id": "c1", "name": "read", "args": {}},
                    {"id": "c2", "name": "write", "args": {}},
                ],
                ctx,
            )

    def test_batch_interrupt_carries_completed_results(self):
        """P1-3：批量 [普通, 审批] 中断时，已完成的前序结果必须挂在
        InterruptSignal.payload 带出（此前随栈帧丢弃，assistant tool_calls
        永久悬空 → resume 后消息序列非法）。"""
        e1 = FakeExecutor(ToolResult(content="read done"))
        s1 = make_spec(name="read", executor=e1)
        s2 = make_spec(name="write", destructive=True,
                       executor=FakeExecutor(ToolResult(content="no")))
        reg = ToolRegistryV3()
        reg.bind_tools([s1, s2])
        ctx = ToolContext(permission_mode="before_changes")

        with pytest.raises(InterruptSignal) as ei:
            reg.process_tool_calls(
                [
                    {"id": "c1", "name": "read", "args": {}},
                    {"id": "c2", "name": "write", "args": {}},
                ],
                ctx,
            )

        assert ei.value.payload.get("completed_tool_messages") == [
            {"tool_call_id": "c1", "content": "read done"},
        ]

    def test_first_tool_interrupt_carries_empty_list(self):
        """P1-3：首个工具即中断 → completed_tool_messages 为空列表（无前序）。"""
        s1 = make_spec(name="write", destructive=True,
                       executor=FakeExecutor(ToolResult(content="no")))
        reg = ToolRegistryV3()
        reg.bind_tools([s1])
        ctx = ToolContext(permission_mode="before_changes")

        with pytest.raises(InterruptSignal) as ei:
            reg.process_tool_calls([{"id": "c1", "name": "write", "args": {}}], ctx)

        assert ei.value.payload.get("completed_tool_messages") == []

    def test_concurrent_batch_interrupt_signal_propagates(self):
        """P1-4：并发批量里 request_human_approval 式工具（静态非
        destructive、executor 抛 InterruptSignal）必须原样冒泡——此前被
        _run_tool 的 except Exception 吞成"工具执行错误"文本。"""
        class ApprovalExecutor(ToolExecutor):
            def execute(self, args, ctx):
                raise InterruptSignal({"action": "人工介入", "details": "需要人确认"})

        s1 = make_spec(name="read_only", executor=FakeExecutor(ToolResult(content="r1")))
        s2 = make_spec(name="request_human_approval", executor=ApprovalExecutor())
        reg = ToolRegistryV3()
        reg.bind_tools([s1, s2])
        # 两者静态均非 destructive → 走并发路径
        ctx = ToolContext(permission_mode="before_changes")

        with pytest.raises(InterruptSignal) as ei:
            reg.process_tool_calls(
                [
                    {"id": "c1", "name": "read_only", "args": {}},
                    {"id": "c2", "name": "request_human_approval", "args": {}},
                ],
                ctx,
            )

        assert ei.value.payload.get("action") == "人工介入"
        # 兄弟结果挂 payload 带出（P1-3 同格式；并发竞态下可能尚未完成）
        completed = ei.value.payload.get("completed_tool_messages") or []
        for item in completed:
            assert set(item) == {"tool_call_id", "content"}

    def test_concurrent_interrupt_payload_carries_call_identity(self):
        """P1-4 半程：并发批量里 executor 直抛的 InterruptSignal（如
        request_human_approval，payload 只有 action/details）入队前必须
        补齐本调用的 tool_call_id/tool_name——否则快照存出空 id，resume
        时 get_spec("") 落 None-spec 分支，审批决策被静默丢弃。"""
        class ApprovalExecutor(ToolExecutor):
            def execute(self, args, ctx):
                raise InterruptSignal({"action": "人工介入", "details": "需要人确认"})

        reg = ToolRegistryV3()
        reg.bind_tools([
            make_spec(name="read_only", executor=FakeExecutor(ToolResult(content="r1"))),
            make_spec(name="request_human_approval", executor=ApprovalExecutor()),
        ])
        # 两者静态均非 destructive → 走并发路径
        ctx = ToolContext(permission_mode="before_changes")

        with pytest.raises(InterruptSignal) as ei:
            reg.process_tool_calls(
                [
                    {"id": "c1", "name": "read_only", "args": {}},
                    {"id": "c2", "name": "request_human_approval", "args": {}},
                ],
                ctx,
            )

        assert ei.value.payload["tool_call_id"] == "c2"
        assert ei.value.payload["tool_name"] == "request_human_approval"

    def test_serial_executor_interrupt_payload_carries_call_identity(self):
        """P1（二轮审查）：串行腿 executor 直抛的 InterruptSignal（如
        request_human_approval——静态 destructive=false，单发调用出厂必走
        串行）必须在冒泡前补齐本调用的 tool_call_id/tool_name——否则快照
        存出空 id，resume 时 get_spec("") 落 None-spec 分支：审批决策被
        静默丢弃 + 空 id 孤儿 tool 消息进活桶。对齐并发腿（P1-4 半程）。"""
        class ApprovalExecutor(ToolExecutor):
            def execute(self, args, ctx):
                raise InterruptSignal({"action": "人工介入", "details": "需要人确认"})

        reg = ToolRegistryV3()
        reg.bind_tools([
            make_spec(name="request_human_approval", executor=ApprovalExecutor()),
        ])
        ctx = ToolContext(permission_mode="before_changes")

        with pytest.raises(InterruptSignal) as ei:
            reg.process_tool_calls(
                [{"id": "c1", "name": "request_human_approval", "args": {}}],
                ctx,
            )

        assert ei.value.payload["tool_call_id"] == "c1"
        assert ei.value.payload["tool_name"] == "request_human_approval"
        # 首个工具即中断：已完成前缀为空列表，身份键不缺席
        assert ei.value.payload.get("completed_tool_messages") == []

    def test_serial_batch_interrupt_keeps_identity_with_completed_prefix(self):
        """P1（二轮审查）：串行批量 [普通, 审批信号] 中断时，权限/身份补齐
        与已完成前缀带出互不干扰（前序结果 + 真实 id 并存）。"""
        class ApprovalExecutor(ToolExecutor):
            def execute(self, args, ctx):
                raise InterruptSignal({"action": "人工介入", "details": "需要人确认"})

        reg = ToolRegistryV3()
        reg.bind_tools([
            make_spec(name="read_only", executor=FakeExecutor(ToolResult(content="r1"))),
            make_spec(name="heavy", destructive=True,
                      executor=FakeExecutor(ToolResult(content="no"))),
            make_spec(name="request_human_approval", executor=ApprovalExecutor()),
        ])
        # heavy 静态 destructive → 整批强制串行
        ctx = ToolContext(permission_mode="full_access")

        with pytest.raises(InterruptSignal) as ei:
            reg.process_tool_calls(
                [
                    {"id": "c1", "name": "read_only", "args": {}},
                    {"id": "c2", "name": "heavy", "args": {}},
                    {"id": "c3", "name": "request_human_approval", "args": {}},
                ],
                ctx,
            )

        assert ei.value.payload["tool_call_id"] == "c3"
        assert ei.value.payload["tool_name"] == "request_human_approval"
        assert ei.value.payload.get("completed_tool_messages") == [
            {"tool_call_id": "c1", "content": "r1"},
            {"tool_call_id": "c2", "content": "no"},
        ]

    def test_serial_batch_interrupt_carries_state_updates(self):
        """串行批量 [write_todos 式, destructive 审批] 中断时，已执行工具的
        state_updates 必须挂 payload 带出（对齐并发腿 interrupt 分支的同名
        挂载）——此前串行腿只挂 completed_tool_messages，兄弟 todos 随中断
        静默丢失，agent 侧 _handle_interrupt 消费不到 payload["state_updates"]。"""
        todos = [{"id": "1", "content": "写文档", "status": "pending"}]
        e_todos = FakeExecutor(ToolResult(
            content="待办已写", state_updates={"todos": todos}))
        reg = ToolRegistryV3()
        reg.bind_tools([
            make_spec(name="write_todos", executor=e_todos),
            make_spec(name="write", destructive=True,
                      executor=FakeExecutor(ToolResult(content="no"))),
        ])
        # write 静态 destructive → 整批强制串行
        ctx = ToolContext(permission_mode="before_changes")

        with pytest.raises(InterruptSignal) as ei:
            reg.process_tool_calls(
                [
                    {"id": "c1", "name": "write_todos", "args": {}},
                    {"id": "c2", "name": "write", "args": {}},
                ],
                ctx,
            )

        assert ei.value.payload.get("state_updates") == {"todos": todos}
        assert ei.value.payload.get("completed_tool_messages") == [
            {"tool_call_id": "c1", "content": "待办已写"},
        ]

    def test_concurrent_interrupt_drains_queued_sibling_result(self):
        """兄弟结果竞态：兄弟的 tool_end 已入队但排在 interrupt 之后
        （主循环按 FIFO 先消费到 interrupt 就 raise）时，已完成兄弟的
        结果必须挂 payload 带出——不再被 resume 侧误合成"未执行"占位。"""
        release = threading.Event()

        class GatedExecutor(ToolExecutor):
            """等审批工具的 tool_start 被主循环消费后才完成——其 tool_end
            必然排在 interrupt 之后、滞留在队列里。"""

            def execute(self, args, ctx):
                release.wait(timeout=5)
                return ToolResult(content="兄弟结果")

        class ApprovalExecutor(ToolExecutor):
            def execute(self, args, ctx):
                raise InterruptSignal({"action": "人工介入", "details": "需要人确认"})

        reg = ToolRegistryV3()
        reg.bind_tools([
            make_spec(name="read_only", executor=GatedExecutor()),
            make_spec(name="request_human_approval", executor=ApprovalExecutor()),
        ])
        ctx = ToolContext(permission_mode="before_changes")

        def sink(event: dict):
            # 审批工具的 tool_start 被消费时放行兄弟并拖住主循环——给
            # 兄弟的 tool_end 留出入队窗口（先于主循环消费 interrupt）
            if (event.get("type") == "tool_start"
                    and event.get("tool_name") == "request_human_approval"):
                release.set()
                time.sleep(0.3)

        with pytest.raises(InterruptSignal) as ei:
            reg.process_tool_calls(
                [
                    {"id": "c1", "name": "read_only", "args": {}},
                    {"id": "c2", "name": "request_human_approval", "args": {}},
                ],
                ctx,
                event_sink=sink,
            )

        assert {"tool_call_id": "c1", "content": "兄弟结果"} in (
            ei.value.payload.get("completed_tool_messages") or []
        )

    def test_concurrent_interrupt_drain_merges_sibling_state_updates(self):
        """P3（二轮审查）：并发批量 [write_todos, request_human_approval]
        中断时，兄弟滞留队列的 tool_end 携带的 state_updates（todos）必须
        合并进 payload 带出——此前 drain 只取 result，待办随中断丢失且无
        todos_update 事件。"""
        release = threading.Event()

        class GatedTodosExecutor(ToolExecutor):
            def execute(self, args, ctx):
                release.wait(timeout=5)
                return ToolResult(
                    content="待办已写",
                    state_updates={"todos": [{"id": "1", "content": "写文档", "status": "pending"}]},
                )

        class ApprovalExecutor(ToolExecutor):
            def execute(self, args, ctx):
                raise InterruptSignal({"action": "人工介入", "details": "需要人确认"})

        reg = ToolRegistryV3()
        reg.bind_tools([
            make_spec(name="write_todos", executor=GatedTodosExecutor()),
            make_spec(name="request_human_approval", executor=ApprovalExecutor()),
        ])
        ctx = ToolContext(permission_mode="before_changes")

        def sink(event: dict):
            if (event.get("type") == "tool_start"
                    and event.get("tool_name") == "request_human_approval"):
                release.set()
                time.sleep(0.3)

        with pytest.raises(InterruptSignal) as ei:
            reg.process_tool_calls(
                [
                    {"id": "c1", "name": "write_todos", "args": {}},
                    {"id": "c2", "name": "request_human_approval", "args": {}},
                ],
                ctx,
                event_sink=sink,
            )

        assert ei.value.payload.get("state_updates") == {
            "todos": [{"id": "1", "content": "写文档", "status": "pending"}],
        }


# ════════════════════════════════════════════════════════════════
# 5. state_updates 合并 + todos_update 事件
# ════════════════════════════════════════════════════════════════

class TestStateUpdatesAndEvents:

    def test_state_updates_merged(self):
        """工具返回 state_updates → 合并到返回的 updates。"""
        executor = FakeExecutor(ToolResult(
            content="done",
            state_updates={"todos": [{"id": "1", "content": "task", "status": "pending"}]},
        ))
        spec = make_spec(executor=executor)
        reg = ToolRegistryV3()
        reg.bind_tools([spec])
        ctx = ToolContext(permission_mode="full_access")

        _, updates = reg.process_tool_calls(
            [{"id": "c1", "name": "fake", "args": {}}],
            ctx,
        )

        assert "todos" in updates
        assert len(updates["todos"]) == 1

    def test_todos_update_event_pushed(self):
        """todos state_update → 推送 todos_update 事件。"""
        todos_data = [{"id": "1", "content": "task", "status": "pending"}]
        executor = FakeExecutor(ToolResult(
            content="done",
            state_updates={"todos": todos_data},
        ))
        spec = make_spec(executor=executor)
        reg = ToolRegistryV3()
        reg.bind_tools([spec])
        ctx = ToolContext(permission_mode="full_access")

        events: list[dict] = []
        reg.process_tool_calls(
            [{"id": "c1", "name": "fake", "args": {}}],
            ctx,
            event_sink=lambda e: events.append(e),
        )

        # 应该有 tool_start + todos_update + tool_end
        types = [e["type"] for e in events]
        assert "tool_start" in types
        assert "tool_end" in types
        assert "todos_update" in types
        todos_event = next(e for e in events if e["type"] == "todos_update")
        assert todos_event["todos"] == todos_data

    def test_tool_start_end_events(self):
        """单工具执行推送 tool_start + tool_end 事件。"""
        executor = FakeExecutor(ToolResult(content="done"))
        spec = make_spec(executor=executor)
        reg = ToolRegistryV3()
        reg.bind_tools([spec])
        ctx = ToolContext(permission_mode="full_access")

        events: list[dict] = []
        reg.process_tool_calls(
            [{"id": "c1", "name": "fake", "args": {"x": 1}}],
            ctx,
            event_sink=lambda e: events.append(e),
        )

        assert len(events) == 2
        assert events[0]["type"] == "tool_start"
        assert events[0]["tool_name"] == "fake"
        assert events[1]["type"] == "tool_end"
        assert events[1]["result"] == "done"


# ════════════════════════════════════════════════════════════════
# 6. mode_guidance 文本生成
# ════════════════════════════════════════════════════════════════

class TestModeGuidance:

    def test_display_name(self):
        assert get_mode_display_name("full_access") == "完全访问"
        assert get_mode_display_name("before_changes") == "变更前访问"
        assert get_mode_display_name("plan") == "计划模式"

    def test_plan_guidance_mentions_remember(self):
        """plan 模式的 guidance 显式点名 remember/write_todos 可以（防过度泛化）。"""
        guidance = get_mode_guidance("plan")
        assert "remember" in guidance
        assert "write_todos" in guidance
        assert "compact_conversation" in guidance

    def test_build_prompt_section(self):
        """build_mode_prompt_section 产出完整的 prompt 段落。"""
        section = build_mode_prompt_section("plan")
        assert "计划模式" in section
        assert "## 当前权限模式" in section

    def test_unknown_mode_falls_back(self):
        """未知 mode 回落为 before_changes guidance。"""
        guidance = get_mode_guidance("unknown_mode")
        assert "变更前访问" in guidance or "审批" in guidance or guidance != ""


# ════════════════════════════════════════════════════════════════
# 7. 串行外层超时 → executor cancel 钩子（P2-4 杀进程树）
# ════════════════════════════════════════════════════════════════

class TestSerialTimeoutCancelHook:

    def test_timeout_invokes_cancel_hook(self, monkeypatch):
        """外层超时必须调用 executor.cancel（ShellExecutor 借此杀命令进程
        树），不再遗弃工作线程下的命令进程。"""
        class SlowCancellableExecutor(ToolExecutor):
            def __init__(self):
                self.release = threading.Event()
                self.cancel_called = False

            def execute(self, args, ctx):
                # 模拟长命令：cancel 置位前不返回（外层已按超时放弃等待）
                self.release.wait(timeout=5)
                return ToolResult(content="late")

            def cancel(self):
                self.cancel_called = True
                self.release.set()

        executor = SlowCancellableExecutor()
        reg = ToolRegistryV3()
        reg.bind_tools([make_spec(name="slow", executor=executor)])
        ctx = ToolContext(permission_mode="full_access")
        monkeypatch.setitem(config.settings, "tool_timeout", 0.2)

        msgs, _ = reg.process_tool_calls([{"id": "c1", "name": "slow", "args": {}}], ctx)

        assert executor.cancel_called is True
        assert "超时" in msgs[0].content

    def test_timeout_without_cancel_hook_still_returns(self, monkeypatch):
        """无 cancel 钩子的 executor（鸭子类型探测）超时路径照常返回错误结果。"""
        class SlowExecutor(ToolExecutor):
            def execute(self, args, ctx):
                time.sleep(0.4)
                return ToolResult(content="late")

        reg = ToolRegistryV3()
        reg.bind_tools([make_spec(name="slow", executor=SlowExecutor())])
        ctx = ToolContext(permission_mode="full_access")
        monkeypatch.setitem(config.settings, "tool_timeout", 0.1)

        msgs, _ = reg.process_tool_calls([{"id": "c1", "name": "slow", "args": {}}], ctx)

        assert "超时" in msgs[0].content

    def test_timeout_passes_call_token_to_cancel(self, monkeypatch):
        """P2-4b：cancel 支持可选 token 时，registry 超时路径按调用粒度
        传入 call_token（= 本次调用 ctx 的 id）——共享执行器实例上的
        cancel 不再无差别全杀；无参签名回退 cancel()（上一测试锁定）。"""
        received: list = []
        release = threading.Event()

        class SlowCancellableExecutor(ToolExecutor):
            def execute(self, args, ctx):
                release.wait(timeout=5)
                return ToolResult(content="late")

            def cancel(self, call_token=None):
                received.append(call_token)
                release.set()

        ctx = ToolContext(permission_mode="full_access")
        reg = ToolRegistryV3()
        reg.bind_tools([make_spec(name="slow", executor=SlowCancellableExecutor())])
        monkeypatch.setitem(config.settings, "tool_timeout", 0.2)

        msgs, _ = reg.process_tool_calls([{"id": "c1", "name": "slow", "args": {}}], ctx)

        assert received == [id(ctx)]
        assert "超时" in msgs[0].content


# ════════════════════════════════════════════════════════════════
# 8. ShellExecutor.cancel 按调用粒度（P2-4b：共享实例不牵连兄弟执行）
# ════════════════════════════════════════════════════════════════

class TestShellExecutorCancelPerCall:
    """ShellExecutor 实例经 loader 进程级缓存共享——同一实例可能同时
    服务两个 task 子代理的 bash。cancel 必须按 execute 调用粒度（发起
    调用的 ToolContext 身份）杀进程，一个超时不许杀掉兄弟的命令。"""

    @pytest.fixture
    def shell_env(self, tmp_path, monkeypatch):
        """workspace 锚点 + 假 Popen（无需真进程/真 bash，跨平台确定）。"""
        monkeypatch.setattr(shell_mod, "resolve_workspace_root", lambda: tmp_path)

        spawned: list = []

        class FakePopen:
            def __init__(self, args, **kwargs):
                self.args = args
                self.pid = 1000 + len(spawned)
                self.returncode = None
                self.done = threading.Event()
                spawned.append(self)

            def poll(self):
                return self.returncode

            def communicate(self, timeout=None):
                # 真实语义：等进程结束；超时未结束抛 TimeoutExpired
                wait = timeout if timeout and timeout > 0 else None
                if self.done.wait(timeout=wait):
                    return b"", b""
                raise subprocess.TimeoutExpired(self.args, timeout)

        monkeypatch.setattr(shell_mod, "subprocess", types.SimpleNamespace(
            Popen=FakePopen,
            TimeoutExpired=subprocess.TimeoutExpired,
            PIPE=subprocess.PIPE,
            DEVNULL=subprocess.DEVNULL,
        ))

        def fake_kill(proc):
            # cancel 杀树：置结束态解除 communicate 阻塞
            killed.append(proc)
            proc.returncode = -9
            proc.done.set()

        killed: list = []
        monkeypatch.setattr(shell_mod, "_kill_process_tree", fake_kill)
        return shell_mod.ShellExecutor(), spawned, killed

    def _wait_registered(self, executor, proc, timeout=5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with executor._proc_lock:
                if any(proc in s for s in executor._active_procs.values()):
                    return
            time.sleep(0.01)
        pytest.fail("进程未在超时前登记进 cancel 表")

    def test_cancel_one_call_spares_sibling(self, shell_env):
        """同实例两次并发 execute，cancel 其中一次只杀该次调用的进程，
        另一次照常跑完（旧实现实例级全杀会误杀兄弟）。"""
        executor, spawned, killed = shell_env
        ctx_a = ToolContext(permission_mode="full_access")
        ctx_b = ToolContext(permission_mode="full_access")
        results: dict = {}

        def run(tag, ctx):
            results[tag] = executor.execute(
                {"command": f"run-{tag}", "timeout": 30}, ctx,
            )

        ta = threading.Thread(target=run, args=("a", ctx_a))
        tb = threading.Thread(target=run, args=("b", ctx_b))

        ta.start()
        self._wait_registered(executor, self._await_spawned(spawned, 1))
        tb.start()
        self._wait_registered(executor, self._await_spawned(spawned, 2))

        proc_a, proc_b = spawned[0], spawned[1]

        # a 命中外层超时 → registry 按 ctx 粒度 cancel
        executor.cancel(id(ctx_a))

        assert killed == [proc_a], "只允许杀 a 的进程"
        assert proc_b.returncode is None, "兄弟进程不得被牵连"

        # b 正常跑完
        proc_b.returncode = 0
        proc_b.done.set()
        ta.join(timeout=5)
        tb.join(timeout=5)
        assert not ta.is_alive() and not tb.is_alive()

        assert "退出码 -9" in results["a"].content
        assert "退出码 0" in results["b"].content
        # 两次调用结束后登记表清空（不残留僵尸登记）
        assert executor._active_procs == {}

    @staticmethod
    def _await_spawned(spawned, count) -> object:
        """等第 count 个 Popen 被创建（execute 已走到登记点之前）。"""
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if len(spawned) >= count:
                return spawned[count - 1]
            time.sleep(0.01)
        pytest.fail(f"Popen 未按时创建（期望 {count} 个）")


# ════════════════════════════════════════════════════════════════
# 9. 并发批量整批超时 → executor cancel 钩子（P3 二轮审查：不遗弃进程树）
# ════════════════════════════════════════════════════════════════

class TestConcurrentBatchTimeoutCancelHook:
    """并发批量 120s 总超时必须对齐串行路径 _execute_with_timeout：对每个
    超时工具调 executor 的 cancel 钩子（按调用粒度传 id(ctx)）——否则 bash
    进程树被遗弃成孤儿，工作线程也随批放弃后继续占用。"""

    @pytest.fixture
    def shell_env(self, tmp_path, monkeypatch):
        """workspace 锚点 + 假 Popen（与 TestShellExecutorCancelPerCall 同构，
        无需真进程/真 bash，跨平台确定）。"""
        monkeypatch.setattr(shell_mod, "resolve_workspace_root", lambda: tmp_path)

        spawned: list = []
        killed: list = []

        class FakePopen:
            def __init__(self, args, **kwargs):
                self.args = args
                self.pid = 1000 + len(spawned)
                self.returncode = None
                self.done = threading.Event()
                spawned.append(self)

            def poll(self):
                return self.returncode

            def communicate(self, timeout=None):
                # 真实语义：等进程结束；超时未结束抛 TimeoutExpired
                wait = timeout if timeout and timeout > 0 else None
                if self.done.wait(timeout=wait):
                    return b"", b""
                raise subprocess.TimeoutExpired(self.args, timeout)

        monkeypatch.setattr(shell_mod, "subprocess", types.SimpleNamespace(
            Popen=FakePopen,
            TimeoutExpired=subprocess.TimeoutExpired,
            PIPE=subprocess.PIPE,
            DEVNULL=subprocess.DEVNULL,
        ))

        def fake_kill(proc):
            # cancel 杀树：置结束态解除 communicate 阻塞
            killed.append(proc)
            proc.returncode = -9
            proc.done.set()

        monkeypatch.setattr(shell_mod, "_kill_process_tree", fake_kill)
        return shell_mod.ShellExecutor(), spawned, killed

    def test_batch_timeout_kills_process_trees(self, shell_env, monkeypatch):
        """两个 bash 并发跑长命令 → 整批超时 → 两个进程树都被 cancel 杀掉，
        调用方拿到超时文本（此前只标记超时，进程树被遗弃）。"""
        from src.agent import registry_v3 as registry_v3_mod

        executor, spawned, killed = shell_env
        ctx = ToolContext(permission_mode="full_access")

        real_time = time.time
        clock = {"primed": False}

        def fake_time():
            if not clock["primed"]:
                # 首次调用 = 整批 deadline 设置点：等两个 Popen 都登记进
                # cancel 表再放行（超时分支触发时 cancel 才有进程可杀）
                deadline = real_time() + 5.0
                while real_time() < deadline:
                    with executor._proc_lock:
                        if len(executor._active_procs.get(id(ctx), ())) >= 2:
                            break
                    time.sleep(0.01)
                clock["primed"] = True
                return real_time()
            # 之后一律返回"已过整批期限"的时刻 → 首轮循环判定即超时
            return real_time() + 10_000.0

        # 只替换 registry_v3 模块视野里的 time 引用（`import time as _t`），
        # 不污染全局 time.time
        monkeypatch.setattr(registry_v3_mod, "_t", types.SimpleNamespace(time=fake_time))

        reg = ToolRegistryV3()
        reg.bind_tools([
            make_spec(name="bash_a", executor=executor),
            make_spec(name="bash_b", executor=executor),
        ])

        msgs, _ = reg.process_tool_calls([
            {"id": "c1", "name": "bash_a", "args": {"command": "run-a", "timeout": 30}},
            {"id": "c2", "name": "bash_b", "args": {"command": "run-b", "timeout": 30}},
        ], ctx)

        # 两个未完成工具都被标记超时
        assert [m.tool_call_id for m in msgs] == ["c1", "c2"]
        assert all("超时" in m.content for m in msgs)
        # cancel 杀树：同 ctx 名下两个进程都被终止（第一个 cancel(id(ctx))
        # 杀整组，第二个对已清空的登记表是 no-op）
        assert sorted(p.pid for p in killed) == sorted(p.pid for p in spawned)
