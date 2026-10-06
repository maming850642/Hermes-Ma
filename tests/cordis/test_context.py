"""
============================================
Context 单元测试
============================================
覆盖：服务仓库（register/get/__getattr__/上溯/KeyError）、
effect LIFO 回滚、teardown 幂等、plugin 子上下文挂载与卸载、
scope 对父服务的遮蔽。
"""

from __future__ import annotations

import pytest

from src.cordis.context import Context
from src.cordis.service import Service


class Recorder(Service):
    """记录 start/stop 调用的测试服务。"""

    name = "recorder"

    def __init__(self) -> None:
        self.start_calls: list[Context] = []
        self.stop_calls: int = 0

    def start(self, ctx: Context) -> None:
        self.start_calls.append(ctx)

    def stop(self) -> None:
        self.stop_calls += 1


# ----------------------------------------------------------------------
# 服务仓库
# ----------------------------------------------------------------------


def test_register_get_and_getattr() -> None:
    ctx = Context(name="root")
    tools = {"kind": "tools"}
    ctx.register("tools", tools)
    assert ctx.get("tools") is tools
    assert ctx.tools is tools  # __getattr__ 委托 get


def test_register_duplicate_raises() -> None:
    ctx = Context()
    ctx.register("tools", object())
    with pytest.raises(RuntimeError):
        ctx.register("tools", object())


def test_get_walks_parent_chain() -> None:
    root = Context(name="root")
    middle = Context(name="middle", parent=root)
    leaf = Context(name="leaf", parent=middle)
    root.register("llm", "root-llm")
    middle.register("tools", "middle-tools")
    assert leaf.get("llm") == "root-llm"  # 跨两级上溯
    assert leaf.get("tools") == "middle-tools"
    assert middle.get("llm") == "root-llm"


def test_get_missing_raises_keyerror() -> None:
    ctx = Context()
    with pytest.raises(KeyError, match="服务未注册: tools"):
        ctx.get("tools")


def test_try_get_and_has_do_not_raise() -> None:
    ctx = Context()
    assert ctx.try_get("tools") is None
    assert ctx.has("tools") is False
    ctx.register("tools", 42)
    assert ctx.try_get("tools") == 42
    assert ctx.has("tools") is True


def test_getattr_missing_raises_attributeerror() -> None:
    ctx = Context()
    with pytest.raises(AttributeError):
        _ = ctx.no_such_service


# ----------------------------------------------------------------------
# 可逆注册与 teardown
# ----------------------------------------------------------------------


def test_effect_lifo_rollback_order() -> None:
    ctx = Context()
    order: list[str] = []

    def make(tag: str):
        def setup():
            order.append(f"setup-{tag}")

            def dispose() -> None:
                order.append(f"dispose-{tag}")

            return dispose

        return setup

    ctx.effect(make("a"))
    ctx.effect(make("b"))
    ctx.effect(lambda: None)  # 返回 None：无清理动作
    assert order == ["setup-a", "setup-b"]  # fn 立即执行
    ctx.teardown()
    assert order == ["setup-a", "setup-b", "dispose-b", "dispose-a"]  # LIFO


def test_teardown_idempotent() -> None:
    ctx = Context()
    svc = Recorder()
    ctx.register("recorder", svc)
    ctx.teardown()
    ctx.teardown()
    ctx.teardown()
    assert svc.stop_calls == 1


def test_teardown_clears_own_services_and_listeners() -> None:
    ctx = Context()
    ctx.register("tools", object())
    calls: list[int] = []
    ctx.on("evt/after", lambda: calls.append(1))
    ctx.teardown()
    assert ctx.try_get("tools") is None
    assert ctx.has("tools") is False


# ----------------------------------------------------------------------
# plugin 与 scope
# ----------------------------------------------------------------------


def test_plugin_mounts_child_and_teardown_unmounts() -> None:
    parent = Context(name="root")
    seen_ctx: list[Context] = []
    svc = Recorder()

    def plugin(child: Context, config: dict) -> None:
        seen_ctx.append(child)
        child.register("inner", svc)
        child.on("evt/x", lambda: None)

    child = parent.plugin(plugin, config={"k": 1}, name="p1")
    assert child.parent is parent
    assert seen_ctx == [child]
    assert child.get("inner") is svc
    assert svc.start_calls == [child]

    parent.teardown()  # 子插件随之逆序卸载
    assert svc.stop_calls == 1
    assert child.try_get("inner") is None


def test_plugin_default_config_is_empty_dict() -> None:
    parent = Context()
    got: list[dict] = []

    def plugin(child: Context, config: dict) -> None:
        got.append(config)

    parent.plugin(plugin)
    parent.plugin(plugin, config=None)
    assert got == [{}, {}]


def test_scope_shadows_parent_service() -> None:
    parent = Context(name="root")
    parent.register("tools", "parent-tools")
    child = parent.scope(name="run-1")
    assert child.parent is parent
    child.register("tools", "child-tools")  # 子层独立注册，不报键冲突
    assert child.get("tools") == "child-tools"  # 子 ctx 拿到自己的
    assert parent.get("tools") == "parent-tools"  # 父不受影响


def test_scope_teardown_detaches_from_parent() -> None:
    parent = Context()
    child = parent.scope(name="temp")
    svc = Recorder()
    child.register("recorder", svc)
    child.teardown()
    assert svc.stop_calls == 1
    assert child not in parent._children  # 已从父挂载列表摘除
