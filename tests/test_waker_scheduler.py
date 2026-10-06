"""
WakerScheduler 测试。

覆盖：
- 到期筛选：enabled + is_due 的 waker 才被触发
- 防重入：同一 waker 不重复提交
- 并发上限：max_concurrent 生效（同时运行的任务数受限）
- 异常隔离：单个 waker 失败不影响其他
- _extract_status：协议解析

外加一个 run_waker 的最小 mock 测试：验证 jsonl 写入 + latest_result 写入。

用 fake worker_manager 记录 send 调用，monkeypatch iter_all_wakers 返回
可控的 waker 列表。直接调 _tick()（与统一调度服务周期调的同一个方法）
保证确定性，不依赖真实线程时序。
"""
import json
import threading
import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from src.waker import WakerConfig, WakerStore
from src.waker.scheduler import WakerScheduler


# ============================================
# 辅助
# ============================================
def _make_cfg(name="w1", enabled=True, next_run_at="", **kw):
    """构造一个 WakerConfig（默认 interval 60min，next_run_at 控制是否到期）。"""
    base = dict(
        name=name, enabled=enabled,
        schedule_type="interval", interval_minutes=60,
        task_prompt="做某事",
    )
    base.update(kw)
    cfg = WakerConfig(**base)
    cfg.next_run_at = next_run_at
    return cfg


class FakeWorkerProcess:
    """模拟 WorkerProcess：记录所有 send 调用，可注入延迟/异常。"""

    def __init__(self, uid, send_fn=None, delay=0.0, raise_on_send=False):
        self.uid = uid
        self.calls = []          # [(op, kwargs), ...]
        self.send_fn = send_fn   # 可选：自定义 send 行为
        self.delay = delay       # 模拟 worker 慢
        self.raise_on_send = raise_on_send
        self._lock = threading.Lock()

    def send(self, op, timeout=300.0, lock_wait=5.0, **kwargs):
        with self._lock:
            self.calls.append((op, dict(kwargs)))
        if self.delay:
            time.sleep(self.delay)
        if self.raise_on_send:
            raise RuntimeError(f"worker {self.uid} 模拟失败")
        if self.send_fn:
            return self.send_fn(op, **kwargs)
        # 默认返回 ok
        return [{"type": "result", "data": {"status": "ok", "run_id": kwargs.get("run_id", "")}}]


class FakeWorkerManager:
    """模拟 WorkerManager：get_or_create 返回按 uid 配置的 FakeWorkerProcess。"""

    def __init__(self):
        self._workers = {}
        self._lock = threading.Lock()

    def set_worker(self, uid, wp):
        with self._lock:
            self._workers[uid] = wp

    def get_or_create(self, uid, slot=None):
        # slot：兼容 c3f379a 起 WorkerManager 的按会话亲和槽位签名（fake 忽略）
        with self._lock:
            wp = self._workers.get(uid)
        if wp is None:
            wp = FakeWorkerProcess(uid)
            self.set_worker(uid, wp)
        return wp


def _make_scheduler(tmp_path, fake_mgr, tick=30, max_concurrent=2):
    """直接 new 一个 WakerScheduler 但不 start 线程（手动调 _tick）。

    WakerScheduler.__init__ 会自动 start，我们 stop 它再手动驱动。
    关键：stop() 会 set _stop_event，必须 clear 否则 _tick() 立即返回。
    """
    sched = WakerScheduler(
        fake_mgr, workspace_root=str(tmp_path),
        tick_seconds=tick, max_concurrent=max_concurrent,
    )
    sched.stop()
    sched._stop_event.clear()
    return sched


def _seed_waker(tmp_path, uid, cfg):
    """在 tmp_path 下种一个 waker（写 yaml）。"""
    store = WakerStore(uid, workspace_root=str(tmp_path))
    store.create(cfg, identity="职责", persona="风格", bible="准则")
    # 写回 next_run_at（create 不写状态字段）
    cfg.next_run_at = cfg.next_run_at
    store.save_state(cfg)
    return store


# ============================================
# 到期筛选
# ============================================
def test_tick_filters_disabled(tmp_path, monkeypatch):
    """enabled=False 的 waker 不触发。"""
    _seed_waker(tmp_path, "u1", _make_cfg("w1", enabled=False, next_run_at="2020-01-01T00:00:00"))
    monkeypatch.setattr(
        "src.waker.scheduler.iter_all_wakers",
        lambda ws: iter([("u1", _make_cfg("w1", enabled=False, next_run_at="2020-01-01T00:00:00"))]),
    )
    mgr = FakeWorkerManager()
    sched = _make_scheduler(tmp_path, mgr)
    sched._tick()
    # 让线程池跑完
    sched._executor = __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor(max_workers=2)
    # 因为 _make_scheduler stop 了 executor，重新建一个再 tick
    # 简化：直接验证 running 集合空
    assert sched._running == set()


def test_tick_filters_not_due(tmp_path, monkeypatch):
    """未到期的 waker 不触发。"""
    future = (datetime.now().replace(microsecond=0))
    from datetime import timedelta
    future_str = (future + timedelta(hours=1)).isoformat()
    cfg = _make_cfg("w1", enabled=True, next_run_at=future_str)
    monkeypatch.setattr(
        "src.waker.scheduler.iter_all_wakers",
        lambda ws: iter([("u1", cfg)]),
    )
    mgr = FakeWorkerManager()
    sched = _make_scheduler(tmp_path, mgr)
    # 重建 executor（_make_scheduler 关掉了）
    from concurrent.futures import ThreadPoolExecutor
    sched._executor = ThreadPoolExecutor(max_workers=2)
    try:
        sched._tick()
        # 等线程池消费
        sched._executor.shutdown(wait=True)
    finally:
        sched._executor = None
    # 没触发任何 send
    wp = mgr.get_or_create("u1")
    assert wp.calls == []


def test_tick_triggers_due_waker(tmp_path, monkeypatch):
    """到期 + enabled 的 waker 触发 send。"""
    # next_run_at 在过去 → is_due=True
    cfg = _make_cfg("w1", enabled=True, next_run_at="2020-01-01T00:00:00")
    monkeypatch.setattr(
        "src.waker.scheduler.iter_all_wakers",
        lambda ws: iter([("u1", cfg)]),
    )
    # 同时种一份真实的 yaml（_run_one 会重读 cfg + save_state）
    _seed_waker(tmp_path, "u1", cfg)

    mgr = FakeWorkerManager()
    wp = FakeWorkerProcess("u1")
    mgr.set_worker("u1", wp)

    sched = _make_scheduler(tmp_path, mgr)
    from concurrent.futures import ThreadPoolExecutor
    sched._executor = ThreadPoolExecutor(max_workers=2)
    try:
        sched._tick()
        sched._executor.shutdown(wait=True)
    finally:
        sched._executor = None

    assert len(wp.calls) == 1
    op, kwargs = wp.calls[0]
    assert op == "waker_run"
    assert kwargs["name"] == "w1"
    assert "run_id" in kwargs
    assert kwargs["api_prompt"] is None


def test_tick_initializes_next_run_for_new_waker(tmp_path, monkeypatch):
    """新建 waker（next_run_at 空）首次被 tick 扫到 → 初始化 next_run_at。

    回归 bug：M1 时 _tick 只看 is_due，但新建 waker next_run_at="" → is_due 永远
    False → 永不触发。修复后 _tick 对 next_run_at 空的先 compute_next_run 初始化。
    这里用 interval=0 让首次初始化后立即到期，验证"初始化 + 触发"全链路。
    """
    # next_run_at 空（新建状态）。interval=1min，但 mock compute_next_run 返回 now
    # （初始化后立即到期），验证"初始化 + 触发"全链路。
    cfg = _make_cfg("fresh", enabled=True, next_run_at="", interval_minutes=1)
    monkeypatch.setattr(
        "src.waker.scheduler.iter_all_wakers",
        lambda ws: iter([("u1", cfg)]),
    )
    # mock compute_next_run 返回 now（首次初始化即到期，立即触发）
    monkeypatch.setattr(
        "src.waker.scheduler.compute_next_run",
        lambda c, n: n,
    )
    _seed_waker(tmp_path, "u1", cfg)

    mgr = FakeWorkerManager()
    wp = FakeWorkerProcess("u1")
    mgr.set_worker("u1", wp)

    sched = _make_scheduler(tmp_path, mgr)
    from concurrent.futures import ThreadPoolExecutor
    sched._executor = ThreadPoolExecutor(max_workers=2)
    try:
        sched._tick()
        sched._executor.shutdown(wait=True)
    finally:
        sched._executor = None

    # 即使初始 next_run_at 为空，也应被初始化并触发
    assert len(wp.calls) == 1, f"新建 waker 未被触发，calls={wp.calls}"
    assert wp.calls[0][0] == "waker_run"
    # next_run_at 应已落盘（save_state 写回）
    from src.waker.store import WakerStore
    saved = WakerStore("u1", workspace_root=str(tmp_path)).get("fresh")
    assert saved.next_run_at, f"next_run_at 未初始化: {saved.next_run_at!r}"


# ============================================
# 防重入
# ============================================
def test_reentrancy_prevented(tmp_path, monkeypatch):
    """同一 waker 正在跑时，下一 tick 不重复提交。

    用慢 worker（delay 0.3s）+ 手动占 running 集合模拟"正在跑"。
    """
    cfg = _make_cfg("w1", next_run_at="2020-01-01T00:00:00")
    monkeypatch.setattr(
        "src.waker.scheduler.iter_all_wakers",
        lambda ws: iter([("u1", cfg)]),
    )
    _seed_waker(tmp_path, "u1", cfg)

    mgr = FakeWorkerManager()
    wp = FakeWorkerProcess("u1", delay=0.3)
    mgr.set_worker("u1", wp)

    sched = _make_scheduler(tmp_path, mgr, max_concurrent=1)
    from concurrent.futures import ThreadPoolExecutor
    sched._executor = ThreadPoolExecutor(max_workers=1)
    try:
        sched._tick()  # 提交第一个
        # 第一 tick 已提交，running 含 ("u1","w1")；立即再 tick 应被跳过
        sched._tick()
        # 等 worker 完成
        sched._executor.shutdown(wait=True)
    finally:
        sched._executor = None

    # 只应有一次 send 调用（第二次 tick 被防重入跳过）
    assert len(wp.calls) == 1


# ============================================
# 并发上限
# ============================================
def test_max_concurrent_limits_parallel_runs(tmp_path, monkeypatch):
    """max_concurrent=2 时，同时跑的任务最多 2 个。

    用 3 个慢 waker（不同 uid/name），验证同时运行的 ≤ 2。
    """
    # 构造 3 个不同 (uid,name) 的到期 waker
    cfgs = [
        ("u1", _make_cfg("w1", next_run_at="2020-01-01T00:00:00")),
        ("u2", _make_cfg("w2", next_run_at="2020-01-01T00:00:00")),
        ("u3", _make_cfg("w3", next_run_at="2020-01-01T00:00:00")),
    ]
    monkeypatch.setattr(
        "src.waker.scheduler.iter_all_wakers",
        lambda ws: iter(cfgs),
    )
    for uid, cfg in cfgs:
        _seed_waker(tmp_path, uid, cfg)

    # 用一个共享计数器追踪并发
    cur = {"n": 0, "max": 0}
    lock = threading.Lock()

    def _send_fn(op, **kwargs):
        with lock:
            cur["n"] += 1
            cur["max"] = max(cur["max"], cur["n"])
        time.sleep(0.2)  # 模拟 LLM 慢
        with lock:
            cur["n"] -= 1
        return [{"type": "result", "data": {"status": "ok", "run_id": kwargs.get("run_id", "")}}]

    mgr = FakeWorkerManager()
    for uid, _ in cfgs:
        mgr.set_worker(uid, FakeWorkerProcess(uid, send_fn=_send_fn))

    sched = _make_scheduler(tmp_path, mgr, max_concurrent=2)
    from concurrent.futures import ThreadPoolExecutor
    sched._executor = ThreadPoolExecutor(max_workers=2)
    try:
        sched._tick()  # 一次性提交 3 个，但线程池只允许 2 并发
        sched._executor.shutdown(wait=True)
    finally:
        sched._executor = None

    assert cur["max"] <= 2, f"并发超过上限: max={cur['max']}"
    assert cur["max"] >= 2, f"未达到并发预期: max={cur['max']}"


# ============================================
# 异常隔离
# ============================================
def test_exception_isolation(tmp_path, monkeypatch):
    """一个 waker send 抛异常，其他 waker 仍正常运行。"""
    cfgs = [
        ("u1", _make_cfg("w1", next_run_at="2020-01-01T00:00:00")),
        ("u2", _make_cfg("w2", next_run_at="2020-01-01T00:00:00")),
    ]
    monkeypatch.setattr(
        "src.waker.scheduler.iter_all_wakers",
        lambda ws: iter(cfgs),
    )
    for uid, cfg in cfgs:
        _seed_waker(tmp_path, uid, cfg)

    mgr = FakeWorkerManager()
    wp1 = FakeWorkerProcess("u1", raise_on_send=True)
    wp2 = FakeWorkerProcess("u2")
    mgr.set_worker("u1", wp1)
    mgr.set_worker("u2", wp2)

    sched = _make_scheduler(tmp_path, mgr, max_concurrent=2)
    from concurrent.futures import ThreadPoolExecutor
    sched._executor = ThreadPoolExecutor(max_workers=2)
    try:
        sched._tick()
        sched._executor.shutdown(wait=True)
    finally:
        sched._executor = None

    # u1 抛异常但仍被调用过
    assert len(wp1.calls) == 1
    # u2 正常完成
    assert len(wp2.calls) == 1
    # running 集合最终被清空（异常也被回调处理）
    assert sched._running == set()
    # u1 的 state 被更新为 error（save_state 兜底）
    s1 = WakerStore("u1", workspace_root=str(tmp_path)).get("w1")
    assert s1.last_status == "error"
    s2 = WakerStore("u2", workspace_root=str(tmp_path)).get("w2")
    assert s2.last_status == "ok"


def test_tick_bad_config_skipped_not_blocking(tmp_path, monkeypatch):
    """P1-7：一条坏配置（带时区的 expire_at → compute_next_run 抛
    TypeError）只跳过该条，按序排在其后的 waker 照常被调度。

    回归：修复前异常穿出 _tick 被外层吞为一条日志，其后所有 waker 永久停摆。
    """
    bad = _make_cfg("a_bad", next_run_at="", expire_at="2026-01-01T00:00:00+08:00")
    good = _make_cfg("z_good", next_run_at="2020-01-01T00:00:00")
    cfgs = [("u1", bad), ("u2", good)]
    monkeypatch.setattr(
        "src.waker.scheduler.iter_all_wakers",
        lambda ws: iter(cfgs),
    )
    _seed_waker(tmp_path, "u2", good)

    mgr = FakeWorkerManager()
    wp = FakeWorkerProcess("u2")
    mgr.set_worker("u2", wp)

    sched = _make_scheduler(tmp_path, mgr)
    from concurrent.futures import ThreadPoolExecutor
    sched._executor = ThreadPoolExecutor(max_workers=2)
    try:
        sched._tick()  # 修复前：TypeError 穿出 _tick
        sched._executor.shutdown(wait=True)
    finally:
        sched._executor = None

    # 坏条目之后的 waker 仍被调度
    assert len(wp.calls) == 1
    assert wp.calls[0][1]["name"] == "z_good"


# ============================================
# 状态更新（run_count / last_run_at / next_run_at）
# ============================================
def test_state_updated_after_run(tmp_path, monkeypatch):
    cfg = _make_cfg("w1", next_run_at="2020-01-01T00:00:00", interval_minutes=60)
    monkeypatch.setattr(
        "src.waker.scheduler.iter_all_wakers",
        lambda ws: iter([("u1", cfg)]),
    )
    _seed_waker(tmp_path, "u1", cfg)

    mgr = FakeWorkerManager()
    mgr.set_worker("u1", FakeWorkerProcess("u1"))

    sched = _make_scheduler(tmp_path, mgr)
    from concurrent.futures import ThreadPoolExecutor
    sched._executor = ThreadPoolExecutor(max_workers=1)
    try:
        sched._tick()
        sched._executor.shutdown(wait=True)
    finally:
        sched._executor = None

    got = WakerStore("u1", workspace_root=str(tmp_path)).get("w1")
    assert got.run_count == 1
    assert got.last_status == "ok"
    assert got.last_run_at != ""
    # next_run_at 应被推进（interval 模式，last_run 后 60min）
    assert got.next_run_at != ""


# ============================================
# _extract_status 单测
# ============================================
def test_extract_status_ok():
    events = [{"type": "result", "data": {"status": "ok", "run_id": "r1"}}]
    assert WakerScheduler._extract_status(events) == "ok"


def test_extract_status_error():
    events = [{"type": "result", "data": {"status": "error", "message": "boom"}}]
    assert WakerScheduler._extract_status(events) == "error"


def test_extract_status_empty_events():
    assert WakerScheduler._extract_status([]) == "ok"


def test_extract_status_no_result_event():
    # 只有 event 没有 result
    events = [{"type": "event", "data": {"type": "token"}}]
    assert WakerScheduler._extract_status(events) == "ok"


# ============================================
# start / stop 生命周期
# ============================================
def test_scheduler_stop_is_idempotent(tmp_path):
    """构造即启动（自建 SchedulerService 的主循环线程），stop 干净退出且幂等。"""
    mgr = FakeWorkerManager()
    sched = WakerScheduler(mgr, workspace_root=str(tmp_path), tick_seconds=1)
    # 已注册到统一调度服务，主循环线程在跑
    assert sched._unregister is not None
    assert sched._schedule._thread is not None and sched._schedule._thread.is_alive()
    sched.stop()
    # 注销 + 自建服务线程退出
    assert sched._unregister is None
    assert sched._schedule._thread is None
    assert not sched._owns_schedule or sched._schedule.registrant_ids == []
    # 二次 stop 不报错
    sched.stop()


def test_scheduler_thread_exits_on_stop(tmp_path, monkeypatch):
    """stop() 唤醒服务主循环，线程立即退出。"""
    monkeypatch.setattr(
        "src.waker.scheduler.iter_all_wakers",
        lambda ws: iter([]),
    )
    mgr = FakeWorkerManager()
    sched = WakerScheduler(mgr, workspace_root=str(tmp_path), tick_seconds=0.05)
    thread = sched._schedule._thread
    assert thread is not None and thread.is_alive()
    sched.stop(timeout=2.0)
    assert not thread.is_alive()


# ============================================
# run_waker 最小 mock 测试
# ============================================
class _FakeAgent:
    """模拟 HermesAgentV3：stream_invoke 返回预设事件列表。"""

    def __init__(self, events):
        self._events = events
        self._mode = "before_changes"

    def stream_invoke(self, user_id, task_input, **kw):
        # 验证 waker_persona 透传
        self.last_waker_persona = kw.get("waker_persona")
        for ev in self._events:
            yield ev

    def get_permission_mode(self):
        return self._mode

    def set_permission_mode(self, mode):
        self._mode = mode


class _FakeState:
    """模拟 worker_process.WorkerState。"""

    def __init__(self, user_id, agent):
        self.user_id = user_id
        self.agent = agent
        self.permission_mode = "before_changes"


def test_run_waker_writes_jsonl_and_result(tmp_path, monkeypatch):
    """验证 run_waker：jsonl 写每条事件 + latest_result.md 写最终文本。"""
    from src.waker.runner import run_waker

    # 固定 workspace_root，让 runner 内的 WakerStore 用 tmp_path
    monkeypatch.setattr("src.waker.store._resolve_workspace", lambda ws="": tmp_path)

    cfg = _make_cfg(
        "w1", task_prompt="检查任务",
        permission_mode="full_access",
    )
    store = WakerStore("u1", workspace_root=str(tmp_path))
    store.create(cfg, identity="职责", persona="风格", bible="准则")

    events = [
        {"type": "token", "content": "hello"},
        {"type": "tool_start", "tool_name": "fake_tool"},
        {"type": "tool_end", "tool_name": "fake_tool", "result": "done"},
        {"type": "complete", "content": "任务完成报告"},
    ]
    agent = _FakeAgent(events)
    state = _FakeState("u1", agent)

    result = run_waker(state, name="w1", run_id="r-001", api_prompt=None)

    assert result["status"] == "ok"
    assert result["run_id"] == "r-001"
    assert result["name"] == "w1"

    # jsonl 存在且每行是合法 json
    jsonl = store.run_dir("w1") / "r-001.jsonl"
    assert jsonl.exists()
    lines = jsonl.read_text(encoding="utf-8").strip().split("\n")
    parsed = [json.loads(l) for l in lines]
    types = [e.get("type") for e in parsed]
    # 起止标记 + 4 个事件
    assert "run_start" in types
    assert "run_end" in types
    assert "complete" in types
    # 最终文本正确
    end = [e for e in parsed if e.get("type") == "run_end"][0]
    assert end["status"] == "ok"

    # latest_result.md 被写
    result_md = store.latest_result_path("w1")
    assert result_md.exists()
    text = result_md.read_text(encoding="utf-8")
    assert "任务完成报告" in text
    assert "r-001" in text

    # waker_persona 透传到 stream_invoke
    assert agent.last_waker_persona is not None
    assert "风格" in agent.last_waker_persona or "职责" in agent.last_waker_persona

    # permission_mode 被临时切换为 cfg 的值，运行后恢复
    assert agent.get_permission_mode() == "before_changes"  # 已恢复


def test_run_waker_error_on_missing_waker(tmp_path, monkeypatch):
    """waker 不存在时返回 error。"""
    from src.waker.runner import run_waker
    monkeypatch.setattr("src.waker.store._resolve_workspace", lambda ws="": tmp_path)
    agent = _FakeAgent([])
    state = _FakeState("u1", agent)
    result = run_waker(state, name="nope", run_id="r-002")
    assert result["status"] == "error"
    assert "不存在" in result["message"]


def test_run_waker_api_prompt_appended(tmp_path, monkeypatch):
    """api_prompt 非空时拼到 task_prompt 后。"""
    from src.waker.runner import run_waker
    monkeypatch.setattr("src.waker.store._resolve_workspace", lambda ws="": tmp_path)

    cfg = _make_cfg("w1", task_prompt="基础任务")
    store = WakerStore("u1", workspace_root=str(tmp_path))
    store.create(cfg)

    captured = {}

    class _Cap(_FakeAgent):
        def stream_invoke(self, user_id, task_input, **kw):
            captured["task_input"] = task_input
            yield {"type": "complete", "content": "ok"}

    state = _FakeState("u1", _Cap([]))
    run_waker(state, name="w1", run_id="r-003", api_prompt="额外指令XYZ")
    assert "基础任务" in captured["task_input"]
    assert "额外指令XYZ" in captured["task_input"]
    assert "API 触发附加指令" in captured["task_input"]


# ============================================
# async_runner 路径（fork 子进程，不抢 worker 锁）
# ============================================
class FakeAsyncRunner:
    """替身 WakerAsyncRunner：记录 run_sync 调用，立即返回 status。"""

    def __init__(self):
        self.calls = []  # [{uid, name, run_id, api_prompt}]
        self.next_status = "ok"

    def run_sync(self, uid, name, run_id, api_prompt=None):
        self.calls.append({
            "uid": uid, "name": name,
            "run_id": run_id, "api_prompt": api_prompt,
        })
        return self.next_status

    def submit(self, uid, name, run_id, api_prompt=None):
        # 异步接口（路由层用），测试里同步模拟
        self.calls.append({
            "uid": uid, "name": name,
            "run_id": run_id, "api_prompt": api_prompt,
        })
        return run_id


def test_tick_uses_async_runner_when_provided(tmp_path, monkeypatch):
    """传了 waker_runner 时，_run_one 走 async_runner 而非 worker_manager。"""
    cfg = _make_cfg("w1", enabled=True, next_run_at="2020-01-01T00:00:00")
    monkeypatch.setattr(
        "src.waker.scheduler.iter_all_wakers",
        lambda ws: iter([("u1", cfg)]),
    )
    _seed_waker(tmp_path, "u1", cfg)

    fake_mgr = FakeWorkerManager()
    async_runner = FakeAsyncRunner()
    sched = WakerScheduler(
        fake_mgr, workspace_root=str(tmp_path),
        tick_seconds=30, waker_runner=async_runner,
    )
    sched.stop()
    sched._stop_event.clear()
    from concurrent.futures import ThreadPoolExecutor
    sched._executor = ThreadPoolExecutor(max_workers=2)
    try:
        sched._tick()
        sched._executor.shutdown(wait=True)
    finally:
        sched._executor = None

    # async_runner 收到调用
    assert len(async_runner.calls) == 1
    assert async_runner.calls[0]["name"] == "w1"
    assert async_runner.calls[0]["uid"] == "u1"

    # state 被更新（last_status 来自 async_runner 返回值）
    store = WakerStore("u1", workspace_root=str(tmp_path))
    cfg2 = store.get("w1")
    assert cfg2.run_count == 1
    assert cfg2.last_status == "ok"


def test_tick_falls_back_to_worker_when_no_runner(tmp_path, monkeypatch):
    """没传 waker_runner 时，_run_one 回退走 worker_manager（兼容旧路径）。"""
    cfg = _make_cfg("w1", enabled=True, next_run_at="2020-01-01T00:00:00")
    monkeypatch.setattr(
        "src.waker.scheduler.iter_all_wakers",
        lambda ws: iter([("u1", cfg)]),
    )
    _seed_waker(tmp_path, "u1", cfg)

    mgr = FakeWorkerManager()
    wp = FakeWorkerProcess("u1")
    mgr.set_worker("u1", wp)
    sched = WakerScheduler(
        mgr, workspace_root=str(tmp_path),
        tick_seconds=30,  # 不传 waker_runner → None → 旧路径
    )
    sched.stop()
    sched._stop_event.clear()
    from concurrent.futures import ThreadPoolExecutor
    sched._executor = ThreadPoolExecutor(max_workers=2)
    try:
        sched._tick()
        sched._executor.shutdown(wait=True)
    finally:
        sched._executor = None

    # worker 收到了 waker_run
    assert len(wp.calls) == 1
    op, kwargs = wp.calls[0]
    assert op == "waker_run"
