"""
tools/pre-execute 事件化等价性测试（T4）。

同一组 spec/args/ctx 下，对比两条路径：
- 旧路径：ToolRegistryV3()（kernel_ctx=None，execute 内直调 permissions.decide）
- 新路径：kernel_ctx 给定（ToolsService 或手挂监听器，waterfall 分发）

断言 allow/deny/needs_approval 三分支的对外行为逐字一致，
以及 deny 监听器不调 next 的短路语义。
"""

from __future__ import annotations

import pytest

from src.agent.hitl import InterruptSignal
from src.agent.registry_v3 import ToolExecRequest, ToolRegistryV3
from src.agent.tool_result import ToolResult
from src.cordis import events
from src.cordis.context import Context
from src.plugins.tools_plugin import ToolsService
from src.tools.context import ToolContext
from src.tools.executor_base import ToolExecutor
from src.tools.permissions import PermissionDecision
from src.tools.schema import SideEffects, SideEffectsOverride, ToolSpec

# 事件契约（模块 import 时声明，此处确认）
assert events.mode_of("tools/pre-execute") == "waterfall"


# ════════════════════════════════════════════════════════════════
# 测试用假执行器（同 tests/test_registry_v3.py）
# ════════════════════════════════════════════════════════════════

class FakeExecutor(ToolExecutor):
    def __init__(self, result: ToolResult):
        self.result = result
        self.calls: list[dict] = []

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        self.calls.append(dict(args))
        return self.result


class ThrowingExecutor(ToolExecutor):
    def __init__(self, exc: Exception):
        self.exc = exc

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        raise self.exc


def make_spec(name="fake", destructive=False, executor=None, parameters=None, se_evaluator=None) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="test tool",
        parameters=parameters or {"type": "object", "properties": {}},
        executor=executor or FakeExecutor(ToolResult(content="ok")),
        side_effects=SideEffects(destructive=destructive),
        se_evaluator=se_evaluator,
    )


# ════════════════════════════════════════════════════════════════
# 新路径构造
# ════════════════════════════════════════════════════════════════

@pytest.fixture
def service_ctx():
    """经 ToolsService（默认监听器=permissions.decide）构造的新路径。"""
    ctx = Context(name="tools-test")
    ctx.register("tools", ToolsService())
    return ctx


def new_registry(service_ctx) -> ToolRegistryV3:
    return service_ctx.get("tools").registry


# ════════════════════════════════════════════════════════════════
# 等价性：allow / deny / needs_approval
# ════════════════════════════════════════════════════════════════

class TestEquivalence:

    def test_allow_same_result(self, service_ctx):
        """full_access 下 destructive 工具放行：两路径执行结果一致。"""
        e_old = FakeExecutor(ToolResult(content="done"))
        e_new = FakeExecutor(ToolResult(content="done"))
        spec_old = make_spec(name="write", destructive=True, executor=e_old)
        spec_new = make_spec(name="write", destructive=True, executor=e_new)
        ctx = ToolContext(permission_mode="full_access")

        r_old = ToolRegistryV3().execute(spec_old, {"x": 1}, ctx)
        r_new = new_registry(service_ctx).execute(spec_new, {"x": 1}, ctx)

        assert r_old.content == r_new.content == "done"
        assert e_old.calls == e_new.calls == [{"x": 1}]

    def test_allow_non_destructive_same_result(self, service_ctx):
        """before_changes 下非破坏性工具放行：一致。"""
        e_old = FakeExecutor(ToolResult(content="r"))
        e_new = FakeExecutor(ToolResult(content="r"))
        spec_old = make_spec(executor=e_old)
        spec_new = make_spec(executor=e_new)
        ctx = ToolContext(permission_mode="before_changes")

        r_old = ToolRegistryV3().execute(spec_old, {}, ctx)
        r_new = new_registry(service_ctx).execute(spec_new, {}, ctx)

        assert r_old.content == r_new.content == "r"

    def test_deny_same_message(self, service_ctx):
        """plan 下 destructive 工具被拒：ToolResult 文案逐字一致。"""
        e_old = FakeExecutor(ToolResult(content="should not run"))
        e_new = FakeExecutor(ToolResult(content="should not run"))
        spec_old = make_spec(destructive=True, executor=e_old)
        spec_new = make_spec(destructive=True, executor=e_new)
        ctx = ToolContext(permission_mode="plan")

        r_old = ToolRegistryV3().execute(spec_old, {}, ctx)
        r_new = new_registry(service_ctx).execute(spec_new, {}, ctx)

        assert r_old.content == r_new.content
        assert r_new.content == "错误：计划模式：destructive 工具被禁止"
        assert e_old.calls == e_new.calls == []  # 都未执行

    def test_force_deny_same_message(self, service_ctx):
        """Layer 3 force_deny 硬底线：两路径文案一致。"""

        def evaluator(args, ctx):
            return SideEffectsOverride(force_deny=True)

        e_old = FakeExecutor(ToolResult(content="no"))
        e_new = FakeExecutor(ToolResult(content="no"))
        spec_old = make_spec(executor=e_old, se_evaluator=evaluator)
        spec_new = make_spec(executor=e_new, se_evaluator=evaluator)
        ctx = ToolContext(permission_mode="full_access")  # 任何模式都拦

        r_old = ToolRegistryV3().execute(spec_old, {}, ctx)
        r_new = new_registry(service_ctx).execute(spec_new, {}, ctx)

        assert r_old.content == r_new.content == "错误：硬底线拦截（force_deny）"

    def test_needs_approval_same_payload(self, service_ctx):
        """before_changes 下 destructive 工具：两路径都抛 InterruptSignal，payload 一致。"""
        e_old = FakeExecutor(ToolResult(content="no"))
        e_new = FakeExecutor(ToolResult(content="no"))
        spec_old = make_spec(name="rm", destructive=True, executor=e_old)
        spec_new = make_spec(name="rm", destructive=True, executor=e_new)
        ctx = ToolContext(permission_mode="before_changes")

        with pytest.raises(InterruptSignal) as old_info:
            ToolRegistryV3().execute(spec_old, {"path": "/tmp/x"}, ctx, "call-1")
        with pytest.raises(InterruptSignal) as new_info:
            new_registry(service_ctx).execute(spec_new, {"path": "/tmp/x"}, ctx, "call-1")

        assert old_info.value.payload == new_info.value.payload
        assert new_info.value.payload == {
            "action": "执行工具 rm",
            "details": "参数: {'path': '/tmp/x'}",
            "tool_name": "rm",
            "tool_args": {"path": "/tmp/x"},
            "tool_call_id": "call-1",
        }
        assert e_old.calls == e_new.calls == []

    def test_coerce_before_decision_both_paths(self, service_ctx):
        """强转时机不变（事件前）：evaluator 在两路径都看到强转后的值。"""
        params = {"type": "object", "properties": {"x": {"type": "integer"}}}
        seen_old, seen_new = {}, {}

        def ev_old(args, ctx):
            seen_old.update(args)
            return SideEffectsOverride()

        def ev_new(args, ctx):
            seen_new.update(args)
            return SideEffectsOverride()

        spec_old = make_spec(parameters=params, se_evaluator=ev_old)
        spec_new = make_spec(parameters=params, se_evaluator=ev_new)
        ctx = ToolContext(permission_mode="before_changes")

        ToolRegistryV3().execute(spec_old, {"x": "42"}, ctx)
        new_registry(service_ctx).execute(spec_new, {"x": "42"}, ctx)

        assert seen_old == seen_new == {"x": 42}

    def test_executor_exception_same_result(self, service_ctx):
        """executor 抛普通异常：两路径都转错误 ToolResult，文案一致。"""
        spec_old = make_spec(executor=ThrowingExecutor(ValueError("boom")))
        spec_new = make_spec(executor=ThrowingExecutor(ValueError("boom")))
        ctx = ToolContext(permission_mode="full_access")

        r_old = ToolRegistryV3().execute(spec_old, {}, ctx)
        r_new = new_registry(service_ctx).execute(spec_new, {}, ctx)

        assert r_old.content == r_new.content == "错误：工具执行失败 - boom"


# ════════════════════════════════════════════════════════════════
# 短路语义 + decision 传播
# ════════════════════════════════════════════════════════════════

class TestShortCircuit:

    def test_deny_listener_skips_later_listeners(self):
        """deny 监听器不调 next：后续监听器不执行，决策取该监听器的值。"""
        ctx = Context(name="short-circuit")
        ran: list[str] = []

        def denier(req: ToolExecRequest, next):
            ran.append("denier")
            req.decision = PermissionDecision(action="deny", reason="外层硬拦截")
            return req  # 不调 next —— 短路

        def follower(req: ToolExecRequest, next):
            ran.append("follower")
            return next()

        ctx.on("tools/pre-execute", denier)
        ctx.on("tools/pre-execute", follower)

        executor = FakeExecutor(ToolResult(content="no"))
        spec = make_spec(executor=executor)
        result = ToolRegistryV3(kernel_ctx=ctx).execute(
            spec, {}, ToolContext(permission_mode="full_access"),
        )

        assert ran == ["denier"]  # follower 被短路
        assert result.content == "错误：外层硬拦截"
        assert executor.calls == []

    def test_outer_listener_overrides_default(self, service_ctx):
        """外层监听器（root 上注册）可改写默认监听器填的 decision。"""
        def override(req: ToolExecRequest, next):
            req.decision = PermissionDecision(action="deny", reason="审计规则拒绝")
            return req  # 短路，不再放行后续

        service_ctx.on("tools/pre-execute", override)

        executor = FakeExecutor(ToolResult(content="no"))
        spec = make_spec(destructive=True, executor=executor)
        result = new_registry(service_ctx).execute(
            spec, {}, ToolContext(permission_mode="full_access"),
        )
        assert result.content == "错误：审计规则拒绝"
        assert executor.calls == []

    def test_no_listener_decision_none_allows(self):
        """kernel ctx 上无监听器：decision 保持 None → 回退直调 decide（fail-safe，
        R2-12 前是 fail-open 按 allow 放行）。plan 下非破坏性工具照常放行。"""
        ctx = Context(name="no-listener")
        executor = FakeExecutor(ToolResult(content="executed"))
        spec = make_spec(executor=executor)

        result = ToolRegistryV3(kernel_ctx=ctx).execute(
            spec, {}, ToolContext(permission_mode="plan"),  # plan 本会放行非破坏性工具
        )
        assert result.content == "executed"
        assert executor.calls == [{}]

    def test_torn_down_kernel_fails_safe_not_open(self):
        """R2-12（内核 H3）：kernel_ctx 已 teardown（监听器全摘）→ 不得按
        allow 放行，必须回退直调 permissions.decide——destructive 工具在
        before_changes 下仍被拦（requireApproval → InterruptSignal）。"""
        from src.plugins.tools_plugin import ToolsService

        kernel = Context(name="kernel-teardown")
        kernel.register("tools", ToolsService())
        registry = ToolRegistryV3(kernel_ctx=kernel)
        kernel.teardown()  # 模拟组合根拆卸后 registry 仍被引用

        executor = FakeExecutor(ToolResult(content="should not run"))
        spec = make_spec(name="rm", destructive=True, executor=executor)

        with pytest.raises(InterruptSignal):
            registry.execute(spec, {}, ToolContext(permission_mode="before_changes"))
        assert executor.calls == []

    def test_bare_kernel_deny_falls_back_to_decide(self):
        """R2-12：从未挂默认监听器的 kernel ctx（未装 tools 插件）——plan 模式
        destructive 工具回退 decide 后被 deny（fail-safe，非 allow）。"""
        ctx = Context(name="bare-kernel")
        executor = FakeExecutor(ToolResult(content="no"))
        spec = make_spec(name="rm", destructive=True, executor=executor)

        result = ToolRegistryV3(kernel_ctx=ctx).execute(
            spec, {}, ToolContext(permission_mode="plan"),
        )
        assert "计划模式" in result.content
        assert executor.calls == []

    def test_malformed_decision_treated_as_none(self, caplog):
        """R2-14（内核 M3）：恶意/劣化监听器把 decision 置为非 PermissionDecision
        （如字符串 "deny"）→ 按None 处理（进 R2-12 回退直调 decide），不炸、
        且不再 fail-open。"""
        import logging as _logging

        ctx = Context(name="bad-decision")

        def malicious(req: ToolExecRequest, next):
            req.decision = "deny"  # 字符串，不是 PermissionDecision
            return req

        ctx.on("tools/pre-execute", malicious)

        executor = FakeExecutor(ToolResult(content="executed"))
        spec = make_spec(name="rm", destructive=True, executor=executor)

        with caplog.at_level(_logging.WARNING, logger="hermes.agent.registry_v3"):
            with pytest.raises(InterruptSignal):
                ToolRegistryV3(kernel_ctx=ctx).execute(
                    spec, {}, ToolContext(permission_mode="before_changes"),
                )
        assert executor.calls == []
        assert any("decision 类型" in r.message for r in caplog.records)

    def test_observer_listener_does_not_change_outcome(self, service_ctx):
        """纯观察监听器（调 next、不动 decision）不改变默认决策结果。"""
        seen: list[str] = []

        def observer(req: ToolExecRequest, next):
            seen.append(req.spec.name)
            return next()

        service_ctx.on("tools/pre-execute", observer)

        executor = FakeExecutor(ToolResult(content="ok"))
        spec = make_spec(executor=executor)
        result = new_registry(service_ctx).execute(
            spec, {}, ToolContext(permission_mode="before_changes"),
        )
        assert seen == ["fake"]
        assert result.content == "ok"


# ════════════════════════════════════════════════════════════════
# tools/execute + tools/post-execute（T6 执行段事件化）
# ════════════════════════════════════════════════════════════════

class TestExecuteEvent:

    def test_execute_and_post_execute_fire(self, service_ctx):
        """kernel 路径：tools/execute waterfall（默认监听器执行入槽）+
        tools/post-execute emit（观察 spec/args/ctx/result）。"""
        runs: list[str] = []
        posts: list[tuple] = []

        def execute_observer(req, next):
            runs.append(req.spec.name)
            # 本文件 fixture 里默认监听器与观察者同 ctx 注册（默认先注册=外层），
            # 观察者运行时默认监听器已把 result 入槽
            assert req.result is not None
            return next()

        service_ctx.on("tools/execute", execute_observer)
        service_ctx.on(
            "tools/post-execute",
            lambda spec, args, tctx, result: posts.append((spec.name, result.content)),
        )

        executor = FakeExecutor(ToolResult(content="done"))
        spec = make_spec(name="write", destructive=True, executor=executor)
        result = new_registry(service_ctx).execute(
            spec, {}, ToolContext(permission_mode="full_access"), "call-9",
        )

        assert result.content == "done"
        assert executor.calls == [{}]
        assert runs == ["write"]
        assert posts == [("write", "done")]

    def test_outer_listener_can_replace_result(self, service_ctx):
        """外层监听器可在 next() 之后替换默认监听器的执行结果。"""

        def replacer(req, next):
            inner = next()  # 默认监听器先执行
            req.result = ToolResult(content="审计替换")
            return inner

        service_ctx.on("tools/execute", replacer)

        executor = FakeExecutor(ToolResult(content="real"))
        spec = make_spec(executor=executor)
        result = new_registry(service_ctx).execute(
            spec, {}, ToolContext(permission_mode="full_access"),
        )
        assert result.content == "审计替换"
        assert executor.calls == [{}]  # 真执行发生过

    def test_kernel_without_default_falls_back_direct(self):
        """kernel ctx 上无 tools/execute 监听器 → 兜底直连执行（等价旧路径）。"""
        from src.cordis import events as ev
        assert ev.mode_of("tools/execute") == "waterfall"
        assert ev.mode_of("tools/post-execute") == "emit"

        ctx = Context(name="no-execute-listener")
        executor = FakeExecutor(ToolResult(content="fallback"))
        spec = make_spec(executor=executor)

        result = ToolRegistryV3(kernel_ctx=ctx).execute(
            spec, {}, ToolContext(permission_mode="full_access"),
        )
        assert result.content == "fallback"
        assert executor.calls == [{}]

    # ─── R2-13：短路 + handled → 调用方尊重拦截，不再 fallback 直连 ───

    def test_short_circuit_handled_intercepts_execution(self):
        """监听器短路且置 handled=True（无 result）→ executor 不执行，
        返回错误 ToolResult（不再 fallback 直连——旧语义拦截不住）。"""
        ctx = Context(name="handled-intercept")

        def interceptor(req, next):
            req.handled = True  # 短路 + 声明已处理：调用方不得兜底直连
            return req  # 不调 next

        ctx.on("tools/execute", interceptor)

        executor = FakeExecutor(ToolResult(content="should not run"))
        spec = make_spec(executor=executor)
        result = ToolRegistryV3(kernel_ctx=ctx).execute(
            spec, {}, ToolContext(permission_mode="full_access"),
        )
        assert executor.calls == [], "handled 短路后 executor 不得被兜底直连执行"
        assert "错误" in result.content
        assert "拦截" in result.content

    def test_short_circuit_handled_respects_result_slot(self):
        """短路 + handled + 填了 result 槽 → 尊重结果槽（被拦截的替代结果）。"""
        ctx = Context(name="handled-result")

        def interceptor(req, next):
            req.handled = True
            req.result = ToolResult(content="审计替换结果")
            return req

        ctx.on("tools/execute", interceptor)

        executor = FakeExecutor(ToolResult(content="real"))
        spec = make_spec(executor=executor)
        result = ToolRegistryV3(kernel_ctx=ctx).execute(
            spec, {}, ToolContext(permission_mode="full_access"),
        )
        assert result.content == "审计替换结果"
        assert executor.calls == []

    def test_short_circuit_without_handled_keeps_fallback_compat(self):
        """短路但未置 handled（旧式观察者）→ 维持 fallback 直连（兼容）。"""
        ctx = Context(name="legacy-short")

        def legacy(req, next):
            return req  # 短路、不置 handled、不填 result

        ctx.on("tools/execute", legacy)

        executor = FakeExecutor(ToolResult(content="fallback-direct"))
        spec = make_spec(executor=executor)
        result = ToolRegistryV3(kernel_ctx=ctx).execute(
            spec, {}, ToolContext(permission_mode="full_access"),
        )
        assert result.content == "fallback-direct"
        assert executor.calls == [{}]

    def test_executor_exception_same_message_via_event(self, service_ctx):
        """默认监听器内 executor 异常 → 同旧路径的错误 ToolResult 文案。"""
        executor = ThrowingExecutor(ValueError("boom"))
        spec = make_spec(executor=executor)
        result = new_registry(service_ctx).execute(
            spec, {}, ToolContext(permission_mode="full_access"),
        )
        assert result.content == "错误：工具执行失败 - boom"

    def test_no_kernel_direct_path_unchanged(self):
        """kernel_ctx=None → 旧直调路径，无事件分发。"""
        executor = FakeExecutor(ToolResult(content="direct"))
        spec = make_spec(executor=executor)
        result = ToolRegistryV3().execute(
            spec, {}, ToolContext(permission_mode="full_access"),
        )
        assert result.content == "direct"
        assert executor.calls == [{}]


# ════════════════════════════════════════════════════════════════
# ToolsService 门面
# ════════════════════════════════════════════════════════════════

class TestToolsServiceFacade:

    def test_bind_and_execute_forward_to_registry(self, service_ctx):
        """service.bind / service.execute 转发内部 registry。"""
        tools = service_ctx.get("tools")
        executor = FakeExecutor(ToolResult(content="via-service"))
        spec = make_spec(name="t", executor=executor)
        tools.bind([spec])

        result = tools.execute(spec, {}, ToolContext(permission_mode="full_access"), "c1")

        assert result.content == "via-service"
        assert tools.registry.get_spec("t") is spec

    def test_resolve_returns_visible_specs(self, service_ctx):
        """service.resolve 包 resolve_tools（返回 ToolSpec 列表）。"""
        from src.tools.resolve import resolve_tools

        tools = service_ctx.get("tools")
        ctx = ToolContext(permission_mode="full_access")
        specs = tools.resolve(ctx)
        assert isinstance(specs, list)
        assert all(isinstance(s, ToolSpec) for s in specs)
        assert {s.name for s in specs} == {s.name for s in resolve_tools(ctx, None)}
