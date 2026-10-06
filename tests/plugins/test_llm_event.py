"""
llm/stream 事件化测试（T4）。

覆盖：
1. 默认监听器存在：boot 后 service.stream 经 waterfall 分发，默认监听器
   （stream_with_hard_timeout）把 chunk 迭代器放进 req.chunks，调用方迭代得到
2. 外层监听器可观察/包装 chunks（洋葱链顺序：默认在内、外层在后）
3. 无监听器时 passthrough：waterfall 原样返回请求；未 start 的服务兜底直连
"""

from __future__ import annotations

import pytest

from src.cordis import events
from src.cordis.context import Context
from src.cordis.loader import boot
from src.llm.messages import Chunk
from src.plugins.llm_plugin import LlmService, StreamRequest

# 事件契约（模块 import 时声明，此处确认）
assert events.mode_of("llm/stream") == "waterfall"

_ROWS = [
    {"id": "config", "plugin": "src.plugins.config_plugin:apply"},
    {"id": "llm", "plugin": "src.plugins.llm_plugin:apply", "inject": ["config"]},
]


class FakeClient:
    """假 LLMClient：stream_chat 返回预设 chunk 迭代器。"""

    def __init__(self, chunks):
        self._chunks = chunks
        self.calls: list[dict] = []

    def stream_chat(self, messages, tools=None, temperature=None, max_tokens=None):
        self.calls.append({"messages": messages, "tools": tools})
        return iter(list(self._chunks))


@pytest.fixture
def patched_client(monkeypatch):
    """把 LlmService.client 换成 FakeClient 工厂，返回记录器。"""
    holder = {"client": None}

    def _fake_client(self, **overrides):
        return holder["client"]

    monkeypatch.setattr(LlmService, "client", _fake_client)
    return holder


class TestDefaultListener:

    def test_stream_yields_chunks_through_waterfall(self, patched_client):
        ctx = boot([dict(r) for r in _ROWS])
        try:
            svc = ctx.get("llm")
            patched_client["client"] = FakeClient([
                Chunk(content_delta="hello"),
                Chunk(content_delta=" world"),
            ])

            out = list(svc.stream(
                [{"role": "user", "content": "hi"}], tools=None, timeout_s=5,
            ))

            assert [c.content_delta for c in out] == ["hello", " world"]
            # 默认监听器把迭代器放进了结果槽（而非 passthrough 兜底）
            assert patched_client["client"].calls[0]["messages"] == [
                {"role": "user", "content": "hi"}
            ]
        finally:
            ctx.teardown()

    def test_stream_passes_tools_and_kw(self, patched_client):
        ctx = boot([dict(r) for r in _ROWS])
        try:
            svc = ctx.get("llm")
            fake = FakeClient([Chunk(content_delta="x")])
            patched_client["client"] = fake

            tools = [{"type": "function", "function": {"name": "f"}}]
            out = list(svc.stream([{"role": "user", "content": "q"}],
                                  tools=tools, timeout_s=5, temperature=0.1))

            assert [c.content_delta for c in out] == ["x"]
            assert fake.calls[0]["tools"] == tools
        finally:
            ctx.teardown()

    def test_outer_listener_can_wrap_chunks(self, patched_client):
        """外层监听器（root 注册）可包装默认监听器放入的迭代器。"""
        ctx = boot([dict(r) for r in _ROWS])
        try:
            svc = ctx.get("llm")
            patched_client["client"] = FakeClient([
                Chunk(content_delta="a"),
                Chunk(content_delta="b"),
            ])

            seen: list[str] = []

            def observer(req: StreamRequest, next):
                inner = req.chunks

                def _wrapped():
                    for chunk in inner:
                        seen.append(chunk.content_delta)
                        yield chunk

                req.chunks = _wrapped()
                return next()

            ctx.on("llm/stream", observer)

            out = list(svc.stream([{"role": "user", "content": "hi"}], timeout_s=5))

            # 调用方拿到的与观察者看到的完全一致（同一迭代链）
            assert [c.content_delta for c in out] == ["a", "b"]
            assert seen == ["a", "b"]
        finally:
            ctx.teardown()

    def test_outer_listener_can_replace_stream(self, patched_client):
        """外层监听器可整体替换 chunks（如 mock/缓存），短路不调 next。"""
        ctx = boot([dict(r) for r in _ROWS])
        try:
            svc = ctx.get("llm")
            # 真客户端若被调用会走 FakeClient——短路路径不应碰它
            patched_client["client"] = FakeClient([Chunk(content_delta="REAL")])

            def mock(req: StreamRequest, next):
                req.chunks = iter([Chunk(content_delta="MOCKED")])
                return req  # 不调 next：默认监听器不执行，真实客户端不触达

            ctx.on("llm/stream", mock)

            out = list(svc.stream([{"role": "user", "content": "hi"}], timeout_s=5))
            assert [c.content_delta for c in out] == ["MOCKED"]
            assert patched_client["client"].calls == []
        finally:
            ctx.teardown()


class TestPassthrough:

    def test_waterfall_no_listener_returns_request_unchanged(self):
        """无监听器：waterfall 原样返回请求对象，chunks 结果槽仍为 None。"""
        ctx = Context(name="bare")
        req = StreamRequest(messages=[{"role": "user", "content": "hi"}])
        result = ctx.waterfall("llm/stream", req)
        assert result is req
        assert req.chunks is None

    def test_service_without_ctx_falls_back_to_raw_stream(self, patched_client):
        """未 start 的 LlmService（无 ctx）stream 仍可用（passthrough 兜底）。"""
        svc = LlmService()
        patched_client["client"] = FakeClient([Chunk(content_delta="standalone")])

        out = list(svc.stream([{"role": "user", "content": "hi"}], timeout_s=5))
        assert [c.content_delta for c in out] == ["standalone"]


# ════════════════════════════════════════════════════════════════
# R2-13：handled 短路语义 + 默认监听器不覆盖已有 chunks
# ════════════════════════════════════════════════════════════════

class TestHandledShortCircuit:

    def test_default_listener_does_not_clobber_existing_chunks(self, patched_client):
        """默认监听器只在 chunks is None 时填——监听器先于它设置了 chunks
        （如 set_llm_params 覆盖/mock）不被丢弃。"""
        svc = LlmService()
        pre_set = iter([Chunk(content_delta="预置流")])
        req = StreamRequest(messages=[{"role": "user", "content": "hi"}])
        req.chunks = pre_set

        svc._default_stream_listener(req, lambda: None)

        assert req.chunks is pre_set, "默认监听器不得无条件覆盖已有 chunks"

    def test_short_circuit_handled_without_chunks_returns_empty(self):
        """监听器短路 + handled=True + 不给 chunks → stream 返回空流
        （尊重短路，不 passthrough 直连真 LLM）。"""
        svc = LlmService()
        svc.start(Context(name="handled-ctx"))
        try:

            def blocker(req: StreamRequest, next):
                req.handled = True  # 短路且已处理：不给 chunks = 空流
                return req  # 不调 next：默认监听器（更内层）不执行

            # prepend 使拦截者先于 start() 注册的默认监听器执行
            svc._ctx.on("llm/stream", blocker, prepend=True)

            out = list(svc.stream([{"role": "user", "content": "hi"}], timeout_s=5))
            assert out == []
        finally:
            svc._ctx.teardown()

    def test_short_circuit_without_handled_keeps_fallback(self):
        """短路但未置 handled（旧式替换）→ chunks 已填则用之（现行为兼容）。"""
        svc = LlmService()
        svc.start(Context(name="legacy-ctx"))
        try:

            def replacer(req: StreamRequest, next):
                req.chunks = iter([Chunk(content_delta="MOCKED")])
                return req  # 短路不置 handled

            svc._ctx.on("llm/stream", replacer)

            out = list(svc.stream([{"role": "user", "content": "hi"}], timeout_s=5))
            assert [c.content_delta for c in out] == ["MOCKED"]
        finally:
            svc._ctx.teardown()


# ════════════════════════════════════════════════════════════════
# R2-13：loader 监听器提升——插件默认监听器对 boot 根可见
# ════════════════════════════════════════════════════════════════

class TestListenerPromotion:

    def setup_method(self):
        events.declare("custom/counting", "emit")
        events.declare("custom/probe", "emit")

    def test_booted_default_listener_visible_from_descendant_ctx(self, patched_client):
        """boot 后插件子 ctx 的 llm/stream 默认监听器提升到根——agent 等
        后代 ctx 分发可命中（兄弟不可见问题修复）。"""
        ctx = boot([dict(r) for r in _ROWS])
        try:
            patched_client["client"] = FakeClient([Chunk(content_delta="via-root")])
            svc = ctx.get("llm")

            # 后代 ctx（模拟 agent scope）分发——经祖先链命中提升来的默认监听器
            descendant = Context(name="agent-scope", parent=ctx)
            req = StreamRequest(messages=[{"role": "user", "content": "hi"}])
            descendant.waterfall("llm/stream", req)

            assert req.chunks is not None
            assert [c.content_delta for c in req.chunks] == ["via-root"]
        finally:
            ctx.teardown()

    def test_promotion_is_move_not_copy_no_double_fire(self):
        """提升是移动不是复制：插件子 ctx 内分发（服务的自有 ctx）与根分发
        各恰好命中一次（复制会导致 executor/流式双跑）。"""
        calls: list[str] = []

        def counting_plugin(ctx: Context, config: dict) -> None:
            def listener(*args):
                calls.append("hit")

            ctx.on("custom/counting", listener)
            ctx.register("counting", {"ok": True})

        import tests.plugins.test_llm_event as this_mod
        this_mod.counting_plugin = counting_plugin
        try:
            root = boot([{"id": "counting",
                          "plugin": "tests.plugins.test_llm_event:counting_plugin"}])
            try:
                # 插件子 ctx（服务自有 dispatch 点）分发一次
                child = next(c for c in root._children if c.name == "counting")
                child.emit("custom/counting", "x")
                assert calls == ["hit"]
                # 根（agent 等后代经祖先链）分发一次
                root.emit("custom/counting", "x")
                assert calls == ["hit", "hit"]
            finally:
                root.teardown()
        finally:
            del this_mod.counting_plugin

    def test_child_teardown_removes_promoted_listener(self):
        """借用语义：子 ctx teardown 后，提升到根的监听器随之摘除。"""
        seen: list[str] = []

        def probe_plugin(ctx: Context, config: dict) -> None:
            ctx.on("custom/probe", lambda *a: seen.append("hit"))

        import tests.plugins.test_llm_event as this_mod
        this_mod.probe_plugin = probe_plugin
        try:
            root = boot([{"id": "probe", "plugin": "tests.plugins.test_llm_event:probe_plugin"}])
            child = next(c for c in root._children if c.name == "probe")
            child.teardown()
            root.emit("custom/probe", "x")
            assert seen == [], "子 ctx teardown 后根上不再有其监听器"
        finally:
            del this_mod.probe_plugin
