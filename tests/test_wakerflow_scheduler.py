"""FlowScheduler 测试（P1-7 tick 逐条异常隔离 + P2-19 防重入覆盖运行期）。

与 tests/test_waker_scheduler.py 同思路：FakeFlowRunner 记录 submit、
手动触发 on_done；直接驱动 _tick()（与统一调度服务周期调的同一方法），
保证确定性，不依赖真实线程时序。
"""
import time

import pytest

from src.constants import LOCAL_USER
from src.wakerflow.models import FlowState
from src.wakerflow.scheduler import FlowScheduler
from src.wakerflow.store import FlowStore


# ============================================
# 辅助
# ============================================
class FakeFlowRunner:
    """替身 FlowRunner：记录 submit；on_done 由测试手动触发（模拟跑完）。"""

    def __init__(self):
        self.submits = []       # [(user_id, flow_name), ...]
        self.on_dones = {}      # run_id -> callback

    def submit(self, user_id, flow_name, inputs=None, on_done=None):
        rid = f"run-{len(self.submits) + 1}"
        self.submits.append((user_id, flow_name))
        if on_done is not None:
            self.on_dones[rid] = on_done
        return rid

    def complete(self, run_id):
        """模拟 flow 真正跑完（触发完成回调）。"""
        cb = self.on_dones.pop(run_id, None)
        if cb is not None:
            cb()


_FLOW_YAML = (
    "name: {name}\n"
    "schedule_type: interval\n"
    "interval_minutes: 1\n"
    "steps: []\n"
)


def _make_sched(tmp_path, runner):
    """构造 FlowScheduler 并停掉自建调度服务线程（手动驱动 _tick）。"""
    sched = FlowScheduler(runner, workspace_root=str(tmp_path), tick_seconds=30)
    sched.stop()
    sched._stop_event.clear()
    return sched


def _seed_flow(tmp_path, name, yaml_text=None, next_run_at="2020-01-01T00:00:00"):
    """落一个 flow 定义 + 到期 state。"""
    store = FlowStore(LOCAL_USER, workspace_root=str(tmp_path))
    store.save(name, yaml_text if yaml_text is not None else _FLOW_YAML.format(name=name))
    store.save_state(name, FlowState(run_count=0, next_run_at=next_run_at))


def _flow_yaml_on_disk(tmp_path, name):
    return FlowStore(LOCAL_USER, workspace_root=str(tmp_path)).get(name)


def _wait_until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


# ============================================
# P1-7：tick 逐条异常隔离
# ============================================
class TestTickIsolation:
    def test_bad_flow_config_does_not_block_following_flows(self, tmp_path, monkeypatch):
        """坏配置（带时区 expire_at → compute_next_run 抛 TypeError）只跳过
        该条，其后 flow 照常被调度。

        回归：修复前异常穿出 _tick 被外层吞为一条日志，其后所有 flow 永久停摆。
        """
        _seed_flow(
            tmp_path, "a_bad",
            yaml_text=(
                _FLOW_YAML.format(name="a_bad")
                + "expire_at: 2026-01-01T00:00:00+08:00\n"
            ),
            next_run_at="",  # 空 → tick 内走 compute_next_run → TypeError
        )
        _seed_flow(tmp_path, "z_good")
        flows = [
            (LOCAL_USER, "a_bad", _flow_yaml_on_disk(tmp_path, "a_bad")),
            (LOCAL_USER, "z_good", _flow_yaml_on_disk(tmp_path, "z_good")),
        ]
        monkeypatch.setattr(
            "src.wakerflow.scheduler.iter_all_flows", lambda ws: iter(flows)
        )

        runner = FakeFlowRunner()
        sched = _make_sched(tmp_path, runner)
        sched._tick()  # 修复前：TypeError 穿出 _tick

        assert _wait_until(lambda: runner.submits == [(LOCAL_USER, "z_good")]), (
            f"坏条目之后的 flow 未被调度: {runner.submits}"
        )

    def test_unparseable_flow_does_not_block_following_flows(self, tmp_path, monkeypatch):
        """解析失败的 flow 跳过（原有语义），其后 flow 仍调度（防回归）。"""
        _seed_flow(tmp_path, "a_broken", yaml_text=": [不是 flow 的 yaml\n")
        _seed_flow(tmp_path, "z_good")
        flows = [
            (LOCAL_USER, "a_broken", _flow_yaml_on_disk(tmp_path, "a_broken")),
            (LOCAL_USER, "z_good", _flow_yaml_on_disk(tmp_path, "z_good")),
        ]
        monkeypatch.setattr(
            "src.wakerflow.scheduler.iter_all_flows", lambda ws: iter(flows)
        )

        runner = FakeFlowRunner()
        sched = _make_sched(tmp_path, runner)
        sched._tick()
        assert _wait_until(lambda: runner.submits == [(LOCAL_USER, "z_good")])


# ============================================
# P2-19：防重入覆盖整个运行期
# ============================================
class TestReentrancyCoversRun:
    def test_running_key_held_until_flow_completes(self, tmp_path, monkeypatch):
        """防重入键在 flow 跑完（on_done）时才释放，submit 返回不再立即释放。

        回归：interval 5min、运行 10min 的 flow 此前会周期性并发重入。
        """
        _seed_flow(tmp_path, "f1")
        monkeypatch.setattr(
            "src.wakerflow.scheduler.iter_all_flows",
            lambda ws: iter([(LOCAL_USER, "f1", _flow_yaml_on_disk(tmp_path, "f1"))]),
        )

        runner = FakeFlowRunner()
        sched = _make_sched(tmp_path, runner)

        sched._tick()
        assert _wait_until(lambda: len(runner.submits) == 1)
        # 运行期：键仍被持有（_tick_one 在起线程前加入，submit 后不释放）
        assert (LOCAL_USER, "f1") in sched._running

        # 运行期再扫：不重复提交
        sched._tick()
        assert len(runner.submits) == 1

        # flow 真正跑完 → on_done → 键释放 → 下一 tick 可再次调度
        runner.complete("run-1")
        assert _wait_until(lambda: (LOCAL_USER, "f1") not in sched._running)
        # 首跑已把 next_run_at 推进到未来（interval 未到），重置为到期
        FlowStore(LOCAL_USER, workspace_root=str(tmp_path)).save_state(
            "f1", FlowState(run_count=1, next_run_at="2020-01-01T00:00:00")
        )
        sched._tick()
        assert _wait_until(lambda: len(runner.submits) == 2)

    def test_key_released_when_submit_fails(self, tmp_path, monkeypatch):
        """submit 抛异常（未进入运行期）→ 键就地释放，不永久卡死该 flow。"""
        _seed_flow(tmp_path, "f1")

        class BoomRunner(FakeFlowRunner):
            def submit(self, user_id, flow_name, inputs=None, on_done=None):
                raise RuntimeError("runner 未启动")

        monkeypatch.setattr(
            "src.wakerflow.scheduler.iter_all_flows",
            lambda ws: iter([(LOCAL_USER, "f1", _flow_yaml_on_disk(tmp_path, "f1"))]),
        )
        sched = _make_sched(tmp_path, BoomRunner())
        sched._tick()
        assert _wait_until(lambda: (LOCAL_USER, "f1") not in sched._running)
