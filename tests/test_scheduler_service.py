"""
SchedulerService（统一调度服务，T8a）测试。

覆盖：
- register 两项不同 tick：各自按自己的周期独立计时触发
- min(tick) 步长：_compute_step 取注册项最小 tick
- 防重入：慢 fn 期间到期轮被跳过，不叠加执行（并发恒 ≤ 1）
- 注销后不再跑
- stop 排空：在跑的 fn 自然完成后 stop 才返回
- 重复注册同 id 报错；异常隔离：fn 抛异常不影响后续轮

计时测试用宽裕的容忍度（Windows 计时器粒度 ~15ms，CI 抖动大）。
"""
import threading
import time

import pytest

from src.plugins.scheduler_plugin import SchedulerService


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """轮询等待 predicate 为 True（超时返回 False）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ============================================
# 注册与步长
# ============================================
def test_register_starts_service_and_min_tick_step():
    """注册即启动主循环；步长 = min(各注册项 tick)。"""
    svc = SchedulerService()
    try:
        assert svc._thread is None  # 未注册未启动
        off_a = svc.register("a", 0.2, lambda: None)
        off_b = svc.register("b", 5.0, lambda: None)
        # 注册即启动主循环线程
        assert svc._thread is not None and svc._thread.is_alive()
        assert svc.registrant_ids == ["a", "b"]
        # min(tick) 步长（未超上限时取精确最小值）
        assert svc._compute_step() == pytest.approx(0.2)
        off_a()
        off_b()
    finally:
        svc.stop()


def test_compute_step_capped_when_ticks_large():
    """所有 tick 都大于上限时，步长封顶（保证新注册小 tick 及时生效）。"""
    from src.plugins.scheduler_plugin import _MAX_STEP_SECONDS
    svc = SchedulerService()
    try:
        off = svc.register("slow", 60.0, lambda: None)
        assert svc._compute_step() == pytest.approx(_MAX_STEP_SECONDS)
        off()
    finally:
        svc.stop()


def test_independent_ticks_two_registrants():
    """两项不同 tick：快项多次触发、慢项至多 1 次（各自独立计时）。"""
    counts = {"fast": 0, "slow": 0}
    lock = threading.Lock()

    def fast_fn():
        with lock:
            counts["fast"] += 1

    def slow_fn():
        with lock:
            counts["slow"] += 1

    svc = SchedulerService()
    try:
        off_fast = svc.register("fast", 0.05, fast_fn)
        off_slow = svc.register("slow", 10.0, slow_fn)
        # 等 ~0.6s：fast（0.05s tick）应跑多次，slow 一次都不该跑
        assert _wait_until(lambda: counts["fast"] >= 5, timeout=5.0), (
            f"fast 触发次数不足: {counts}"
        )
        assert counts["slow"] == 0, f"slow 不应触发: {counts}"
        off_fast()
        off_slow()
    finally:
        svc.stop()


# ============================================
# 防重入
# ============================================
def test_slow_fn_does_not_stack():
    """慢 fn（执行时间 > tick）期间到期轮被跳过，绝不并发叠加。"""
    state = {"running": 0, "max_running": 0, "done": 0}
    lock = threading.Lock()

    def slow_fn():
        with lock:
            state["running"] += 1
            state["max_running"] = max(state["max_running"], state["running"])
        time.sleep(0.3)
        with lock:
            state["running"] -= 1
            state["done"] += 1

    svc = SchedulerService()
    try:
        off = svc.register("slow", 0.02, slow_fn)  # tick 远小于 fn 耗时
        # 等到至少完成 3 次（0.3s/次 → ~1s）
        assert _wait_until(lambda: state["done"] >= 3, timeout=10.0), (
            f"完成次数不足: {state}"
        )
        # 全程并发恒 1（上一轮没跑完的到期轮被跳过，不叠加）
        assert state["max_running"] == 1, f"fn 出现并发叠加: {state}"
        off()
    finally:
        svc.stop()


# ============================================
# 注销 / 停止
# ============================================
def test_unregister_stops_further_runs():
    """注销后不再触发（已注册的执行自然收尾）。"""
    count = {"n": 0}
    lock = threading.Lock()

    def fn():
        with lock:
            count["n"] += 1

    svc = SchedulerService()
    try:
        off = svc.register("r", 0.05, fn)
        assert _wait_until(lambda: count["n"] >= 2, timeout=5.0)
        off()
        # 注销幂等
        off()
        assert svc.registrant_ids == []
        # de-flake（reviewall）：off() 前可能有一次 fn 已出队执行中——先等执行
        # 平静再取基线（旧写法立即取基线，飞行中的 fn 完成后 count+1 → 偶发红）
        # 等计数静止（飞行中的 fn 收尾）再取基线
        _wait_until(lambda: count["n"] == count["n"], timeout=0.1)  # 让出调度
        prev, stable = -1, 0
        while stable < 2:
            if count["n"] == prev:
                stable += 1
            else:
                stable = 0
                prev = count["n"]
            time.sleep(0.1)
        baseline = count["n"]
        time.sleep(0.3)  # 远超 tick
        assert count["n"] == baseline, f"注销后仍在触发: {count}"
    finally:
        svc.stop()


def test_stop_drains_inflight_fn():
    """stop 排空：在跑的 fn 自然完成后 stop 才返回。"""
    finished = threading.Event()
    started = threading.Event()

    def slow_fn():
        started.set()
        time.sleep(0.4)
        finished.set()

    svc = SchedulerService()
    off = svc.register("drain", 0.01, slow_fn)
    try:
        assert _wait_until(started.is_set, timeout=5.0), "fn 未启动"
        t0 = time.monotonic()
        svc.stop(timeout=5.0)
        elapsed = time.monotonic() - t0
        # stop 等到了在跑的 fn 完成（而不是立即返回）
        assert finished.is_set(), "stop 未排空在跑的 fn"
        assert elapsed >= 0.1, f"stop 过早返回({elapsed:.2f}s)，未排空"
        assert svc._thread is None
        assert svc.registrant_ids == []
    finally:
        off()
        svc.stop()


def test_stop_idempotent_and_service_lifecycle_via_context():
    """stop 幂等；作为 Service 挂到 Context 时 start/stop 由生命周期托管。"""
    from src.cordis.context import Context

    svc = SchedulerService()
    off = svc.register("x", 60.0, lambda: None)
    svc.stop()
    svc.stop()  # 幂等
    off()       # stop 后注销仍安全（幂等）

    # Context 托管：register 触发 start(ctx)，teardown 触发 stop
    ctx = Context(name="test")
    svc2 = SchedulerService()
    ctx.register("schedule", svc2)
    assert svc2._thread is not None and svc2._thread.is_alive()
    svc2.register("y", 60.0, lambda: None)
    ctx.teardown()
    assert svc2._thread is None


# ============================================
# 注册约束与异常隔离
# ============================================
def test_duplicate_registrant_id_raises():
    svc = SchedulerService()
    try:
        off = svc.register("dup", 60.0, lambda: None)
        with pytest.raises(RuntimeError, match="dup"):
            svc.register("dup", 1.0, lambda: None)
        off()
    finally:
        svc.stop()


def test_fn_exception_does_not_kill_loop():
    """fn 抛异常被隔离：主循环继续，后续轮照常触发。"""
    calls = {"n": 0}
    lock = threading.Lock()

    def bad_fn():
        with lock:
            calls["n"] += 1
        raise RuntimeError("boom")

    svc = SchedulerService()
    try:
        off = svc.register("bad", 0.05, bad_fn)
        assert _wait_until(lambda: calls["n"] >= 3, timeout=5.0), (
            f"异常后主循环未继续触发: {calls}"
        )
        assert svc._thread is not None and svc._thread.is_alive()
        off()
    finally:
        svc.stop()
