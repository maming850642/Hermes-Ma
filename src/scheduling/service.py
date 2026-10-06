"""
统一调度服务 SchedulerService（T8a；P3-3 自 src/plugins/scheduler_plugin.py
整体下沉到本层，定义未做任何改动）。

背景：waker / wakerflow / memory 三个调度器原先各自复制同一套
"daemon 线程 + _stop_event.wait(tick) 主循环" 骨架。T8a 把骨架抽成
一个共享服务，三个调度器只保留各自的 due-time 判定与任务执行逻辑。

## 线程模型
- **一个** daemon 线程跑所有注册项（register 加入），主循环以
  min(各注册项 tick) 为步长 `_stop_event.wait(step)` 周期醒来；
  每轮只做单调时钟比较（不执行 fn），开销可忽略。
- 步长附带上限 `_MAX_STEP_SECONDS`：运行期 register 更小 tick 的
  注册项时不必等完上一轮长睡眠才生效（每注册项的到期计时独立，
  提前醒来不会提前触发）。
- 到期的注册项把 fn 提交到**该注册项自己的** bounded
  ThreadPoolExecutor(max_workers) 执行——主循环线程永远不跑用户 fn。

## 防重入
每注册项一个 running 标记：上一轮 fn 未跑完 → 跳过本轮（不提交、
不推进计时）；fn 完成后的第一轮恢复触发（与旧调度器
"_tick 期间 _running 命中则跳过" 语义一致）。

## 生命周期
- Service：Context.register 时 start(ctx) 起线程，teardown 时 stop()
  排空（等各注册项在跑的 fn 自然完成，取消未开始的排队）。
- 也可独立构造使用（三个调度器 None 注入时自建实例）：
  `svc = SchedulerService(); off = svc.register(...); ...; off(); svc.stop()`。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from src.cordis.service import Service

if TYPE_CHECKING:
    from src.cordis.context import Context

# logger 名保持迁移前不变（日志连续性；P3-3 要求零行为变化）
logger = logging.getLogger("hermes.plugins.scheduler")

#: 主循环步长上限（秒）：保证运行期新注册的小 tick 及时生效。
#: 每轮检查只是对各注册项做一次 monotonic 比较，频繁醒来开销可忽略。
_MAX_STEP_SECONDS = 0.5

#: 单注册项 tick 下限（秒）：防 0/负值导致主循环退化成忙轮询。
_MIN_TICK_SECONDS = 0.01

#: stop() 排空总上限（秒，R3-20）：注册项 fn 卡死时关停不被挂死，
#: 超时放弃等待并告警（残留 daemon 线程随进程退出回收）。
_DRAIN_DEADLINE_SECONDS = 30.0


@dataclass
class _Registrant:
    """单个注册项：独立计时 + 独立 bounded 线程池 + 防重入标记。"""

    registrant_id: str
    tick_seconds: float
    fn: Callable[[], None]
    executor: ThreadPoolExecutor
    #: 上次触发到期的时间（time.monotonic()）；跳过轮不推进
    last_scheduled: float
    lock: threading.Lock = field(default_factory=threading.Lock)
    running: bool = False
    active: bool = True  # 注销/停止后置 False，主循环不再提交


class SchedulerService(Service):
    """统一调度服务：单 daemon 线程托管任意多个周期注册项。

    用法：
        svc = SchedulerService()
        off = svc.register("waker", 30.0, waker_scheduler._tick, max_workers=1)
        ...
        off()       # 注销（等在跑的 _tick 完成）
        svc.stop()  # 停主循环 + 排空全部注册项线程池

    作为 Service 经 cordis.yaml 挂载时（键 "schedule"），生命周期由
    Context 托管：boot 起线程、teardown 排空。
    """

    name = "schedule"

    def __init__(self) -> None:
        # 受保护结构：registrant_id -> _Registrant（增删查都走 _lock）
        self._registrants: dict[str, _Registrant] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ════════════════════════════════════════════════════════════
    # Service 生命周期（直接独立使用时无参调用 start/stop）
    # ════════════════════════════════════════════════════════════
    def start(self, ctx: "Context | None" = None) -> None:
        """启动主循环线程（幂等；已 stop 后可重新 start）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="scheduler-service",
        )
        self._thread.start()
        logger.info("SchedulerService 已启动（注册项 %d 个）", len(self._registrants))

    def stop(self, timeout: float = 5.0, drain_timeout: float = _DRAIN_DEADLINE_SECONDS) -> None:
        """停止主循环 + 排空各注册项线程池（幂等）。

        先设 _stop_event 唤醒主循环退出；再逐注册项排空——在跑的 fn 等它
        自然完成，未开始的排队取消。

        R3-20：排空加总上限 drain_timeout（默认 30s）。shutdown(wait=False)
        + 轮询 join 工作线程到 deadline，超时放弃等待并告警（慢任务不再
        把关停挂死；残留线程是非 daemon 的自然回收——随进程退出）。
        """
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        with self._lock:
            registrants = list(self._registrants.values())
            self._registrants.clear()
        deadline = time.monotonic() + max(0.0, drain_timeout)
        for reg in registrants:
            self._shutdown_registrant(reg, deadline=deadline)
        logger.info("SchedulerService 已停止")

    # ════════════════════════════════════════════════════════════
    # 注册 / 注销
    # ════════════════════════════════════════════════════════════
    def register(
        self,
        registrant_id: str,
        tick_seconds: float,
        fn: Callable[[], None],
        *,
        max_workers: int = 1,
    ) -> Callable[[], None]:
        """注册一个周期执行项，返回注销函数（幂等）。

        Args:
            registrant_id: 注册项标识（唯一；重复注册抛 RuntimeError）
            tick_seconds: 触发周期（秒）。首跑在注册后 tick_seconds
            fn: 到期执行的无参函数（在自己的 bounded 线程池里跑，
                异常被隔离记录，不会影响主循环）
            max_workers: 该注册项线程池大小（默认 1；fn 自身内部
                再分发任务时通常 1 就够）

        Returns:
            注销函数：移除注册项并关闭其线程池（等在跑的 fn 完成）。
        """
        tick = max(float(tick_seconds), _MIN_TICK_SECONDS)
        workers = max(1, int(max_workers))
        reg = _Registrant(
            registrant_id=registrant_id,
            tick_seconds=tick,
            fn=fn,
            executor=ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix=f"sched-{registrant_id}",
            ),
            last_scheduled=time.monotonic(),
        )
        with self._lock:
            if registrant_id in self._registrants:
                reg.executor.shutdown(wait=False)
                raise RuntimeError(f"调度注册项已存在: {registrant_id!r}")
            self._registrants[registrant_id] = reg
        # 服务线程可能还没起（直接独立使用场景）——幂等启动
        self.start()
        logger.info(
            f"调度注册: {registrant_id} tick={tick}s max_workers={workers}"
        )

        def _unregister() -> None:
            with self._lock:
                removed = self._registrants.pop(registrant_id, None)
            if removed is None:
                return
            with removed.lock:
                removed.active = False
            self._shutdown_registrant(removed)
            logger.info(f"调度注销: {registrant_id}")

        return _unregister

    @property
    def registrant_ids(self) -> list[str]:
        """当前注册项 id 快照（诊断/测试用）。"""
        with self._lock:
            return list(self._registrants)

    def now(self) -> float:
        """主循环用的时钟（time.monotonic()，测试可观察）。"""
        return time.monotonic()

    # ════════════════════════════════════════════════════════════
    # 主循环
    # ════════════════════════════════════════════════════════════
    def _run(self) -> None:
        """主循环：以 min(tick) 步长周期醒来，检查各注册项到期。"""
        while not self._stop_event.wait(self._compute_step()):
            try:
                self._check_due(time.monotonic())
            except Exception:
                # 主循环绝不能挂（挂了全部注册项都停了）
                logger.exception("SchedulerService 主循环异常（忽略，继续）")

    def _compute_step(self) -> float:
        """主循环步长 = min(各注册项 tick)，附 _MAX_STEP_SECONDS 上限。"""
        with self._lock:
            ticks = [r.tick_seconds for r in self._registrants.values()]
        if not ticks:
            return _MAX_STEP_SECONDS
        return min(min(ticks), _MAX_STEP_SECONDS)

    def _check_due(self, now: float) -> None:
        """单轮检查：到期的注册项提交到自己的线程池（防重入）。"""
        with self._lock:
            due = [
                r for r in self._registrants.values()
                if now - r.last_scheduled >= r.tick_seconds
            ]
        for reg in due:
            with reg.lock:
                if not reg.active:
                    continue
                if reg.running:
                    # 防重入：上一轮未跑完 → 跳过本轮（计时不推进，
                    # fn 完成后的第一轮恢复触发）
                    continue
                reg.running = True
                reg.last_scheduled = now
            try:
                reg.executor.submit(self._run_registrant, reg)
            except RuntimeError:
                # executor 已被并发注销/停止关闭：回退标记
                with reg.lock:
                    reg.running = False

    @staticmethod
    def _run_registrant(reg: _Registrant) -> None:
        """注册项 fn 的异常隔离包装（在线程池里跑）。"""
        try:
            reg.fn()
        except Exception:
            logger.exception(f"调度注册项执行异常（忽略）: {reg.registrant_id}")
        finally:
            with reg.lock:
                reg.running = False

    @staticmethod
    def _shutdown_registrant(reg: _Registrant, deadline: float | None = None) -> None:
        """关闭单个注册项的线程池：在跑的 fn 等完成，排队取消。

        deadline 给定时（stop 的排空总上限，R3-20）：shutdown(wait=False)
        + 轮询 join 工作线程，到 deadline 放弃并告警；None 时保持旧语义
        （在跑的 fn 等到自然完成，注销路径 _unregister 用）。
        """
        if deadline is None:
            try:
                reg.executor.shutdown(wait=True, cancel_futures=True)
            except Exception:
                reg.executor.shutdown(wait=False)
            return
        try:
            reg.executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        threads = list(getattr(reg.executor, "_threads", ()) or ())
        for t in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            t.join(timeout=remaining)
        if any(t.is_alive() for t in threads):
            logger.warning(
                f"调度注册项 {reg.registrant_id} 排空超时，放弃等待"
                f"（fn 仍在跑，线程随进程退出回收）"
            )
