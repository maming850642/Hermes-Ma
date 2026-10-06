"""
============================================
EventTable 与事件分发单元测试
============================================
覆盖：declare/require 契约校验、emit 注册序、waterfall 洋葱链
（无监听器原样返回 / 共享对象突变 / 短路 / prepend / 值沿链传递）、
parallel 并发与异常传播、serial 顺序收集、事件冒泡与子卸载退出。
"""

from __future__ import annotations

import threading

import pytest

from src.cordis.context import Context
from src.cordis.events import declare, declare_events, mode_of, require


@pytest.fixture()
def ctx() -> Context:
    return Context(name="events-test")


# ----------------------------------------------------------------------
# EventTable 契约
# ----------------------------------------------------------------------


def test_declare_invalid_mode() -> None:
    with pytest.raises(ValueError, match="非法分发模式"):
        declare("evt/bad-mode", "pubsub")


def test_declare_duplicate_same_mode_is_idempotent() -> None:
    declare("evt/dup-ok", "emit")
    declare("evt/dup-ok", "emit")  # 同 mode 幂等通过
    assert mode_of("evt/dup-ok") == "emit"


def test_declare_duplicate_different_mode_raises() -> None:
    declare("evt/dup-conflict", "emit")
    with pytest.raises(ValueError, match="分发模式"):
        declare("evt/dup-conflict", "serial")


def test_declare_events_batch() -> None:
    declare_events({"evt/batch-a": "emit", "evt/batch-b": "waterfall"})
    assert mode_of("evt/batch-a") == "emit"
    assert mode_of("evt/batch-b") == "waterfall"


def test_require_undeclared_raises() -> None:
    with pytest.raises(ValueError, match="事件未声明分发模式: evt/ghost"):
        require("evt/ghost", "emit")


def test_require_mode_mismatch_raises() -> None:
    declare("evt/mismatch", "emit")
    with pytest.raises(ValueError):
        require("evt/mismatch", "serial")


def test_dispatch_undeclared_event_raises() -> None:
    c = Context()
    with pytest.raises(ValueError, match="事件未声明分发模式"):
        c.emit("evt/never-declared")


def test_dispatch_wrong_mode_raises() -> None:
    declare("evt/wrong-mode", "emit")
    c = Context()
    with pytest.raises(ValueError, match="分发模式"):
        c.serial("evt/wrong-mode")


# ----------------------------------------------------------------------
# emit
# ----------------------------------------------------------------------


def test_emit_registration_order(ctx: Context) -> None:
    declare("evt/order", "emit")
    order: list[int] = []
    ctx.on("evt/order", lambda payload: order.append(1))
    ctx.on("evt/order", lambda payload: order.append(2))
    ctx.on("evt/order", lambda payload: order.append(payload))
    ctx.emit("evt/order", 3)
    assert order == [1, 2, 3]


def test_on_off_is_reversible(ctx: Context) -> None:
    declare("evt/off", "emit")
    calls: list[int] = []
    off = ctx.on("evt/off", lambda: calls.append(1))
    off()
    off()  # 幂等
    ctx.emit("evt/off")
    assert calls == []


# ----------------------------------------------------------------------
# waterfall
# ----------------------------------------------------------------------


def test_waterfall_no_listeners_returns_args_untouched(ctx: Context) -> None:
    declare("evt/wf-empty", "waterfall")
    assert ctx.waterfall("evt/wf-empty", 42) == 42
    assert ctx.waterfall("evt/wf-empty", 1, 2) == (1, 2)
    assert ctx.waterfall("evt/wf-empty") == ()


def test_waterfall_mutates_shared_object_then_next(ctx: Context) -> None:
    declare("evt/wf-mutate", "waterfall")
    box: list[int] = []

    def first(value: list[int], nxt):
        value.append(1)
        return nxt()

    def second(value: list[int], nxt):
        value.append(2)  # 能看到 first 的突变
        return len(value)

    ctx.on("evt/wf-mutate", first)
    ctx.on("evt/wf-mutate", second)
    assert ctx.waterfall("evt/wf-mutate", box) == 2
    assert box == [1, 2]


def test_waterfall_not_calling_next_short_circuits(ctx: Context) -> None:
    declare("evt/wf-short", "waterfall")
    reached: list[str] = []

    def blocker(value, nxt):
        reached.append("blocker")
        return "blocked"

    def never(value, nxt):
        reached.append("never")

    ctx.on("evt/wf-short", blocker)
    ctx.on("evt/wf-short", never)
    assert ctx.waterfall("evt/wf-short", "x") == "blocked"
    assert reached == ["blocker"]


def test_waterfall_prepend_runs_first(ctx: Context) -> None:
    declare("evt/wf-prepend", "waterfall")
    order: list[str] = []

    def base(value, nxt):
        order.append("base")
        return nxt()

    def front(value, nxt):
        order.append("front")
        return nxt()

    ctx.on("evt/wf-prepend", base)
    ctx.on("evt/wf-prepend", front, prepend=True)
    assert ctx.waterfall("evt/wf-prepend", 0) == 0
    assert order == ["front", "base"]


def test_waterfall_value_passes_through_chain(ctx: Context) -> None:
    declare("evt/wf-chain", "waterfall")
    ctx.on("evt/wf-chain", lambda value, nxt: nxt() + 1)  # 最外层
    ctx.on("evt/wf-chain", lambda value, nxt: nxt() * 10)
    ctx.on("evt/wf-chain", lambda value, nxt: value + 1)  # 链尾不再委托
    assert ctx.waterfall("evt/wf-chain", 5) == ((5 + 1) * 10) + 1 == 61


def test_waterfall_chain_end_next_returns_original(ctx: Context) -> None:
    declare("evt/wf-end", "waterfall")
    ctx.on("evt/wf-end", lambda value, nxt: nxt())  # 全部放行
    assert ctx.waterfall("evt/wf-end", 7) == 7


# ----------------------------------------------------------------------
# parallel
# ----------------------------------------------------------------------


def test_parallel_runs_all_concurrently(ctx: Context) -> None:
    declare("evt/par", "parallel")
    barrier = threading.Barrier(3, timeout=10)
    results: list[int] = []
    lock = threading.Lock()

    def work(value: int) -> int:
        barrier.wait()  # 三者都进入并发段才会放行；串行则超时破裂
        with lock:
            results.append(value)
        return value * 2

    for i in range(3):
        ctx.on("evt/par", lambda v=i: work(v))
    assert ctx.parallel("evt/par") == [0, 2, 4]
    assert sorted(results) == [0, 1, 2]


def test_parallel_exception_propagates_after_all_run(ctx: Context) -> None:
    declare("evt/par-fail", "parallel")
    done: list[str] = []
    lock = threading.Lock()
    started = threading.Event()

    def failing() -> None:
        started.set()
        raise RuntimeError("boom-first")

    def slow() -> None:
        started.wait(timeout=5)
        with lock:
            done.append("slow")

    ctx.on("evt/par-fail", failing)
    ctx.on("evt/par-fail", slow)
    with pytest.raises(RuntimeError, match="boom-first"):
        ctx.parallel("evt/par-fail")
    assert done == ["slow"]  # 异常仍等全部监听器结束后才抛


# ----------------------------------------------------------------------
# serial
# ----------------------------------------------------------------------


def test_serial_order_and_return_values(ctx: Context) -> None:
    declare("evt/ser", "serial")
    ctx.on("evt/ser", lambda x: x * 2)
    ctx.on("evt/ser", lambda x: x + 1)
    assert ctx.serial("evt/ser", 10) == [20, 11]


def test_serial_no_collectors(ctx: Context) -> None:
    declare("evt/ser-empty", "serial")
    assert ctx.serial("evt/ser-empty") == []


# ----------------------------------------------------------------------
# 冒泡
# ----------------------------------------------------------------------


def test_parent_listeners_see_child_events_in_bubble_order() -> None:
    declare("evt/bubble", "emit")
    parent = Context(name="parent")
    child = parent.scope(name="child")
    order: list[str] = []
    child.on("evt/bubble", lambda: order.append("child"))
    parent.on("evt/bubble", lambda: order.append("parent"))
    child.emit("evt/bubble")
    assert order == ["child", "parent"]  # 本 ctx 在前，逐级上溯在后
    parent.emit("evt/bubble")
    assert order[-1] == "parent"  # 父事件不向子传播


def test_grandparent_also_receives() -> None:
    declare("evt/bubble-deep", "emit")
    root = Context(name="root")
    mid = root.scope(name="mid")
    leaf = mid.scope(name="leaf")
    seen: list[str] = []
    root.on("evt/bubble-deep", lambda: seen.append("root"))
    mid.on("evt/bubble-deep", lambda: seen.append("mid"))
    leaf.emit("evt/bubble-deep")
    assert seen == ["mid", "root"]


def test_child_teardown_stops_participation() -> None:
    declare("evt/bubble-gone", "emit")
    parent = Context(name="parent")
    child = parent.scope(name="child")
    parent_calls: list[int] = []
    child_calls: list[int] = []
    parent.on("evt/bubble-gone", lambda: parent_calls.append(1))
    child.on("evt/bubble-gone", lambda: child_calls.append(1))

    child.emit("evt/bubble-gone")
    assert (len(parent_calls), len(child_calls)) == (1, 1)

    child.teardown()
    child.emit("evt/bubble-gone")
    assert len(parent_calls) == 2  # 父监听器照常
    assert len(child_calls) == 1  # 子监听器已随卸载清空
