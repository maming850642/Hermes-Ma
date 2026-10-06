"""
============================================
FlowScheduler —— 主进程内的 WakerFlow 调度器
============================================
结构对照 src/waker/scheduler.py（WakerScheduler），但有两个关键区别：

1. **运行机制不同**：waker 通过 worker_manager.send("waker_run") 走 per-user
   worker IPC；flow 通过 FlowRunner.submit 在主进程线程池跑（已脱离 worker IPC，
   不阻塞 chat）。所以本调度器调 flow_runner.submit，不碰 worker_manager。

2. **状态存储不同**：waker 的状态在 waker.yaml 的 state 节（WakerConfig 内）；
   flow 的状态在单独的 state.json（FlowState）。本调度器用 FlowStore.load_state /
   save_state。

T8a 起不再自持 daemon 线程：周期驱动交给统一调度服务 SchedulerService
（构造时 register 自己的 _tick；不传 schedule 时自建独立实例）。

## Schedulable 适配器
schedule_parse.compute_next_run / is_due 接受 Schedulable Protocol（鸭子类型）。
FlowSpec（定义）和 FlowState（状态）字段分开，需要一个适配器把它们合成一个
满足 Protocol 的对象。_FlowSchedulable 把 spec 的调度配置 + state 的运行状态
合并暴露。

## 线程模型
- 周期驱动：SchedulerService 主循环按 tick 周期调 _tick（防重入由服务保证）
- 单 flow 防重入：_running 集合记录 (uid, name)
- 并发上限：FlowRunner 内部的 ThreadPoolExecutor 限流（调度器本身不再起线程池，
  只是 submit 到 FlowRunner；到期动作为一次性线程提交）
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Callable

from src.scheduling import SchedulerService
from src.waker.schedule_parse import compute_next_run, is_due
from src.wakerflow.parser import FlowParseError, parse_flow
from src.wakerflow.store import FlowStore, iter_all_flows

logger = logging.getLogger("hermes.wakerflow.scheduler")


class _FlowSchedulable:
    """把 FlowSpec（定义）+ FlowState（状态）合并成 Schedulable Protocol 对象。

    compute_next_run/is_due 需要的属性：enabled / schedule_type / interval_minutes /
    daily_at / expire_at / max_runs / run_count / last_run_at / next_run_at。
    前 6 个来自 spec，后 3 个来自 state。
    """

    def __init__(self, spec: Any, state: Any):
        self.enabled = spec.enabled
        self.schedule_type = spec.schedule_type
        self.interval_minutes = spec.interval_minutes
        self.daily_at = spec.daily_at
        self.expire_at = spec.expire_at
        self.max_runs = spec.max_runs
        self.run_count = state.run_count
        self.last_run_at = state.last_run_at
        self.next_run_at = state.next_run_at


class FlowScheduler:
    """主进程 WakerFlow 调度器（SchedulerService 驱动，submit 到 FlowRunner）。

    Args:
        flow_runner: FlowRunner 实例（submit 跑 flow）
        workspace_root: workspace 根（FlowStore / iter_all_flows 用）
        tick_seconds: 扫描间隔
        schedule: 统一调度服务（共享实例）；None 时自建独立实例
    """

    def __init__(
        self,
        flow_runner: Any,
        workspace_root: str = "",
        tick_seconds: int = 30,
        schedule: "SchedulerService | None" = None,
    ):
        self._flow_runner = flow_runner
        self._workspace_root = workspace_root
        self._tick_seconds = max(1, int(tick_seconds))

        # 统一调度服务：None 时自建（独立可用；
        # SchedulerService 顶层 import，src.scheduling 不反向依赖本层）
        self._schedule = schedule if schedule is not None else SchedulerService()
        self._owns_schedule = schedule is None
        self._unregister: Callable[[], None] | None = None

        # shutdown 期间 _tick 快速退出（主循环骨架已移至 SchedulerService）
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._running: set[tuple[str, str]] = set()

        self.start()

    # ════════════════════════════════════════════════════════════
    # 生命周期
    # ════════════════════════════════════════════════════════════
    def start(self) -> None:
        """注册到调度服务（构造时自动调，重复调幂等）。"""
        if self._unregister is not None:
            return
        self._stop_event.clear()
        self._unregister = self._schedule.register(
            "flow", self._tick_seconds, self._tick, max_workers=1,
        )
        logger.info(f"FlowScheduler 已启动: tick={self._tick_seconds}s")

    def stop(self, timeout: float = 5.0) -> None:
        """注销调度（等在跑的 _tick 收尾）；自建服务时停服务。幂等。"""
        self._stop_event.set()
        unreg = self._unregister
        self._unregister = None
        if unreg is not None:
            unreg()
        if self._owns_schedule:
            self._schedule.stop(timeout=timeout)
        logger.info("FlowScheduler 已停止")

    # ════════════════════════════════════════════════════════════
    # 调度入口（由 SchedulerService 周期调用）
    # ════════════════════════════════════════════════════════════
    def _tick(self) -> None:
        """单次扫描：发现到期 flow → submit 到 FlowRunner。"""
        now = datetime.now()
        try:
            all_flows = list(iter_all_flows(self._workspace_root))
        except Exception:
            logger.exception("iter_all_flows 失败（跳过本次 tick）")
            return

        for uid, name, yaml_text in all_flows:
            if self._stop_event.is_set():
                return
            # P1-7：逐条异常隔离。解析失败只是坏条目之一——compute_next_run/
            # is_due 同样可能因脏数据抛错（如带时区的 expire_at），一条坏配置
            # 不得中断其后所有 flow 的调度。
            try:
                self._tick_one(uid, name, yaml_text, now)
            except Exception:
                logger.exception(f"flow 调度项处理失败，跳过: {uid}/{name}")
                continue

    def _tick_one(self, uid: str, name: str, yaml_text: str, now: datetime) -> None:
        """处理单个 flow 的解析、到期判定与提交（调用方逐条隔离异常）。"""
        # 解析 flow 定义（坏的 flow 跳过，不影响其他）
        try:
            spec = parse_flow(yaml_text)
        except FlowParseError:
            logger.warning(f"flow 解析失败，跳过: {uid}/{name}", exc_info=True)
            return
        if not spec.enabled or spec.schedule_type == "none":
            return

        store = FlowStore(uid, workspace_root=self._workspace_root)
        state = store.load_state(name)
        sched = _FlowSchedulable(spec, state)

        # 首次调度入口：next_run_at 为空时先 compute_next_run 初始化（防死循环）
        if not sched.next_run_at:
            nxt = compute_next_run(sched, now)
            if nxt is None:
                return  # 已过期/达 max_runs/none，不调度
            state.next_run_at = nxt.isoformat(timespec="seconds")
            try:
                store.save_state(name, state)
                logger.info(f"flow 首次调度初始化: {uid}/{name} → next={state.next_run_at}")
            except Exception:
                logger.warning(f"flow 首次调度初始化失败: {uid}/{name}", exc_info=True)

        if not is_due(sched, now):
            return

        key = (uid, name)
        with self._lock:
            if key in self._running:
                logger.debug(f"flow 正在跑，跳过: {uid}/{name}")
                return
            self._running.add(key)

        # submit 到 FlowRunner（异步，立即返回 run_id）
        threading.Thread(
            target=self._run_one_safe,
            args=(uid, name, now),
            daemon=True,
            name=f"flow-sched-{name}",
        ).start()

    # ════════════════════════════════════════════════════════════
    # 单 flow 任务
    # ════════════════════════════════════════════════════════════
    def _run_one_safe(self, uid: str, name: str, now: datetime) -> None:
        """异常隔离包装。

        P2-19：防重入键的释放在 flow 真正跑完时（FlowRunner.submit 的
        on_done 完成回调，对齐 waker/scheduler.py 的完成回调释放模式）；
        只有未进入运行期的路径（flow 被删/解析坏/submit 失败）才在此就地
        释放——否则 interval 短于运行时长的 flow 会周期性并发重入。
        """
        try:
            self._run_one(uid, name, now)
        except Exception:
            logger.exception(f"flow 调度运行异常: {uid}/{name}")
            self._release((uid, name))

    def _release(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._running.discard(key)

    def _run_one(self, uid: str, name: str, now: datetime) -> None:
        """跑单个 flow：FlowRunner.submit → 更新 FlowState。

        与 WakerScheduler._run_one 的区别：调 FlowRunner（主进程线程池），
        不走 worker IPC。
        """
        key = (uid, name)
        store = FlowStore(uid, workspace_root=self._workspace_root)
        # 重读 spec（定义可能在两次 tick 间被改）
        yaml_text = store.get(name)
        if yaml_text is None:
            logger.warning(f"flow 不存在（已被删？）: {uid}/{name}")
            self._release(key)
            return
        try:
            spec = parse_flow(yaml_text)
        except FlowParseError:
            logger.warning(f"flow 定义变坏，跳过: {uid}/{name}")
            self._release(key)
            return
        if not spec.enabled:
            self._release(key)
            return

        state = store.load_state(name)
        logger.info(f"flow 触发: {uid}/{name}")

        status = "failed"
        try:
            # P2-19：on_done 在 flow 跑完（含挂起后续跑完成）时释放防重入键
            run_id = self._flow_runner.submit(
                uid, name, inputs={}, on_done=lambda: self._release(key),
            )
            # submit 异步，不等完成。状态从 FlowRunner 内存表或 jsonl 读。
            # 这里记 next_run_at 推进 + run_count++，last_status 等 flow 跑完
            # 由 jsonl 的 flow_end 体现（下次 tick 读 state 时仍是空——
            # 简化：submit 后立即按"已触发"记 ok，详细状态前端查 status 端点）。
            status = "ok"
            logger.info(f"flow 已提交: {uid}/{name} run_id={run_id}")
        except Exception:
            logger.exception(f"flow submit 失败: {uid}/{name}")
            self._release(key)
            status = "error"

        # 更新 state（推进 next_run_at）
        try:
            state = store.load_state(name)  # 重读，防 submit 期间 state 被改
            state.run_count += 1
            state.last_run_at = now.isoformat(timespec="seconds")
            state.last_status = status
            sched = _FlowSchedulable(spec, state)
            nxt = compute_next_run(sched, datetime.now())
            state.next_run_at = nxt.isoformat(timespec="seconds") if nxt else ""
            store.save_state(name, state)
        except Exception:
            logger.exception(f"flow 状态更新失败: {uid}/{name}")

    # ════════════════════════════════════════════════════════════
    # 手动触发（API invoke 调）
    # ════════════════════════════════════════════════════════════
    def submit_now(self, user_id: str, name: str, inputs: dict | None = None) -> str:
        """手动/API 触发一次 flow 运行。

        与定时 tick 的区别：不查 enabled/is_due，立即跑，不重算 next_run_at。
        直接调 FlowRunner.submit（与 invoke 路由共用）。

        Returns:
            run_id
        Raises:
            ValueError: flow 不存在 / 解析失败（FlowParseError 继承 ValueError）
        """
        return self._flow_runner.submit(user_id, name, inputs or {})
