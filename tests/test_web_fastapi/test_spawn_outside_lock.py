"""P2-12 回归：get_or_create 的 spawn + ready 等待在全局锁外。

旧实现在 Manager 全局锁内 Popen + 轮询 ready（最长 60s）——期间
lookup / acquire_chat_slot / cancel_for / remove 全被卡。修复后全局锁内
只做「查表 + 占位去重」，spawn 在全局锁外进行；同槽并发请求经 per-spawn
事件去重（只 spawn 一次）+ double-check 复用同一实例。
"""
import json
import queue
import threading
import time
import types

import pytest

from src.constants import LOCAL_USER
from web_fastapi import worker_manager as wm_mod
from web_fastapi.ipc import encode_message
from web_fastapi.worker_manager import WorkerManager


class _EofStdout:
    """readline 立即返回 ""（EOF）：ready 等待立刻失败，测试桩快速收场。"""

    def readline(self):
        return ""


class _ScriptedStdout:
    """按队列吐行的 stdout 桩：预置一条 ready 握手模拟 spawn 成功。"""

    def __init__(self):
        self._q = queue.Queue()

    def push(self, line):
        self._q.put(line)

    def readline(self):
        try:
            return self._q.get(timeout=5)
        except queue.Empty:
            return ""


class _NullStdin:
    def write(self, data):
        return len(data)

    def flush(self):
        pass


def _wait_until(cond, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _fake_subprocess(popen_cls):
    """替身 subprocess 模块：get_or_create 用到 PIPE 常量与 Popen。"""
    import subprocess as _real
    return types.SimpleNamespace(Popen=popen_cls, PIPE=_real.PIPE,
                                 STDOUT=_real.STDOUT, DEVNULL=_real.DEVNULL)


def test_spawn_does_not_hold_global_lock(monkeypatch, tmp_path):
    m = WorkerManager(max_parallel=2, state_path=tmp_path / "web_state.json")
    gate = threading.Event()
    popen_calls = []

    class _FakePopen:
        def __init__(self, *a, **k):
            popen_calls.append(1)
            if len(popen_calls) == 1:
                gate.wait(5.0)  # 首次：卡在 Popen 内模拟 spawn/ready 慢
            self.pid = id(self)
            self.stdin = _NullStdin()
            self.stdout = _EofStdout()

        def poll(self):
            return None

        def kill(self):
            pass

    monkeypatch.setattr(wm_mod, "subprocess", _fake_subprocess(_FakePopen))

    errors = []

    def _spawn():
        try:
            m.get_or_create(LOCAL_USER, slot="slow01")
        except Exception as e:  # 预期：EOF → 启动失败（测试桩语义）
            errors.append(e)

    t1 = threading.Thread(target=_spawn)
    t1.start()
    assert _wait_until(lambda: len(popen_calls) == 1), "spawn 线程未进入 Popen"

    # spawn 在途：全局锁必须可获取，只读路径照常可用（不再被卡最长 60s）
    assert m._lock.acquire(timeout=1.0), "全局锁被 spawn 持有（P2-12 回归）"
    m._lock.release()

    t0 = time.monotonic()
    assert m.lookup(LOCAL_USER, "slow01") is None
    assert m.acquire_chat_slot("slow01") == "slow01"
    assert m.cancel_for("slow01") is False
    assert time.monotonic() - t0 < 1.0, "lookup/acquire/cancel 被 spawn 阻塞"

    # 同槽并发请求不重复 spawn（占位去重）
    t2 = threading.Thread(target=_spawn)
    t2.start()
    time.sleep(0.3)
    assert len(popen_calls) == 1, "同槽并发请求重复 spawn（占位去重失效）"

    gate.set()
    t1.join(10.0)
    t2.join(10.0)
    assert not t1.is_alive() and not t2.is_alive()
    # 等待者接手重试后同样因 EOF 失败（桩语义），占位被清理
    assert len(errors) == 2
    assert m._spawning == {}


def test_concurrent_get_or_create_same_slot_single_spawn(monkeypatch, tmp_path):
    """ready 握手成功的场景：同槽并发请求只 spawn 一次，双方复用同一实例。"""
    m = WorkerManager(max_parallel=2, state_path=tmp_path / "web_state.json")
    popen_calls = []
    stdout = _ScriptedStdout()

    class _FakePopenReady:
        def __init__(self, *a, **k):
            popen_calls.append(1)
            self.pid = id(self)
            self.stdin = _NullStdin()
            self.stdout = stdout

        def poll(self):
            return None

        def kill(self):
            pass

    monkeypatch.setattr(wm_mod, "subprocess", _fake_subprocess(_FakePopenReady))
    stdout.push(json.dumps({"type": "ready", "user_id": LOCAL_USER,
                            "slot": "hot0001"}) + "\n")

    results = []

    def _spawn():
        results.append(m.get_or_create(LOCAL_USER, slot="hot0001"))

    t1 = threading.Thread(target=_spawn)
    t2 = threading.Thread(target=_spawn)
    t1.start()
    t2.start()
    t1.join(10.0)
    t2.join(10.0)

    assert len(results) == 2
    assert results[0] is results[1], "并发 get_or_create 未复用同一实例"
    assert len(popen_calls) == 1, "同槽并发请求重复 spawn"
    assert m._spawning == {}
    assert m.lookup(LOCAL_USER, "hot0001") is results[0]


def test_remove_during_inflight_spawn_still_recycles(monkeypatch, tmp_path):
    """spawn 移出全局锁后语义保持：删除赶上在途 spawn → 等落地后回收。"""
    m = WorkerManager(max_parallel=2, state_path=tmp_path / "web_state.json")
    popen_calls = []
    stdout = _ScriptedStdout()

    class _FakePopenReady:
        def __init__(self, *a, **k):
            popen_calls.append(1)
            self.pid = id(self)
            self.stdin = _NullStdin()
            self.stdout = stdout

        def poll(self):
            return None

        def kill(self):
            pass

    monkeypatch.setattr(wm_mod, "subprocess", _fake_subprocess(_FakePopenReady))
    stdout.push(json.dumps({"type": "ready", "user_id": LOCAL_USER,
                            "slot": "del0001"}) + "\n")

    spawned = []

    def _spawn():
        spawned.append(m.get_or_create(LOCAL_USER, slot="del0001"))

    t = threading.Thread(target=_spawn)
    t.start()
    assert _wait_until(lambda: len(popen_calls) == 1)

    # spawn 在途发起 remove：应等 spawn 落地后回收，而不是空手而归
    t2 = threading.Thread(target=lambda: m.remove(LOCAL_USER, slot="del0001"))
    t2.start()
    t.join(10.0)
    t2.join(10.0)

    assert spawned and spawned[0] is not None
    assert m.lookup(LOCAL_USER, "del0001") is None, "在途 spawn 的槽未被回收"
