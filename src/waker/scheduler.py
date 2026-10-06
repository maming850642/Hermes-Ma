"""
============================================
WakerScheduler —— 主进程内的 waker 调度器
============================================
T8a 起不再自持 daemon 线程：周期驱动交给统一调度服务 SchedulerService
（src/scheduling/service.py，经插件键 "schedule" 装配），本类只保留 waker
特有逻辑——扫描 iter_all_wakers、筛出 enabled 且到期的（is_due）、
提交到本类自己的 ThreadPoolExecutor 跑一轮 waker_run。

设计要点：
- 周期驱动：构造时向 SchedulerService register 自己的 _tick（tick_seconds
  周期）；不传 schedule 时自建独立 SchedulerService 实例（保持单独可用）
- 单 waker 防重入：内存级 running 集合，记录 (uid, name)，避免一个 waker
  还在跑就被重复提交（同时守住 submit_now 与 tick 的并发）
- 并发上限：ThreadPoolExecutor(max_workers=max_concurrent) 限流
- 异常隔离：单个 waker 任务出错只记日志、写 last_status=error，不影响其他

线程模型：SchedulerService 主循环只做"发现 + 提交"，真正的 waker_run
在本类线程池里跑，通过 worker_manager.get_or_create(uid).send("waker_run", ...)
与 worker 子进程同步通信（send 会等 worker 返回 result）。
"""
from __future__ import annotations

import logging
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable

from src.scheduling import SchedulerService
from src.waker.store import iter_all_wakers
from src.waker.schedule_parse import compute_next_run, is_due

logger = logging.getLogger("hermes.waker.scheduler")

# waker_run 默认超时（秒）。LLM 调用偏慢，比普通 chat 的 300s 默认值更宽裕。
WAKER_RUN_TIMEOUT = 600.0


def _run_timeout() -> float:
    """waker_run 超时（秒）：config.waker_node_timeout 可覆盖（与
    async_runner._node_timeout 同键同回退），缺省/非法回退 600。"""
    from config import get_settings
    try:
        v = float(getattr(get_settings(), "waker_node_timeout", 0) or 0)
    except Exception:
        v = 0.0
    return v if v > 0 else WAKER_RUN_TIMEOUT


class WakerScheduler:
    """主进程内的 waker 调度器（SchedulerService 驱动 + 线程池）。

    构造即注册到调度服务；stop() 注销并排空线程池。

    Args:
        worker_manager: WorkerManager 实例（用于 get_or_create 拿 worker）
        workspace_root: workspace 根路径（与 WakerStore 一致）
        tick_seconds: 扫描间隔（秒）
        max_concurrent: 同时运行的 waker 任务上限
        waker_runner: WakerAsyncRunner 实例（fork 子进程跑 waker，不抢 worker 锁）
        schedule: 统一调度服务（共享实例）；None 时自建独立实例
    """

    def __init__(
        self,
        worker_manager: Any,
        workspace_root: str = "",
        tick_seconds: int = 30,
        max_concurrent: int = 2,
        waker_runner: Any = None,
        schedule: "SchedulerService | None" = None,
    ):
        """构造 WakerScheduler。

        Args:
            worker_manager: WorkerManager 实例（保留兼容，新版主用 waker_runner）
            workspace_root: workspace 根路径（与 WakerStore 一致）
            tick_seconds: 扫描间隔（秒）
            max_concurrent: 同时运行的 waker 任务上限
            waker_runner: WakerAsyncRunner 实例（fork 子进程跑 waker，不抢 worker 锁）。
                传入则用 async_runner（推荐）；None 则回退走 worker_manager.send（旧路径，
                会和 chat 抢锁，保留兼容）。
            schedule: SchedulerService 共享实例（web 主进程传 cordis_ctx.schedule）；
                None 时自建独立实例（构造即随本类生命周期）。
        """
        self._worker_manager = worker_manager
        self._workspace_root = workspace_root
        self._tick_seconds = max(1, int(tick_seconds))
        self._max_concurrent = max(1, int(max_concurrent))
        self._waker_runner = waker_runner  # None = 旧路径（worker IPC）

        # 统一调度服务：None 时自建（独立可用，现有测试直接构造本类；
        # SchedulerService 顶层 import，src.scheduling 不反向依赖本层）
        self._schedule = schedule if schedule is not None else SchedulerService()
        self._owns_schedule = schedule is None
        self._unregister: Callable[[], None] | None = None

        # shutdown 期间 _tick 快速退出（主循环骨架已移至 SchedulerService）
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        # 正在运行的 (uid, name) 集合，防止单 waker 重入（含 submit_now 并发）
        self._running: set[tuple[str, str]] = set()
        self._executor: ThreadPoolExecutor | None = None
        # 已提交但未完成的 future（仅供 stop 时引用，避免被 GC）
        self._pending_futures: set = set()

        self.start()

    # ════════════════════════════════════════════════════════════════
    # 生命周期
    # ════════════════════════════════════════════════════════════════
    def start(self) -> None:
        """注册到调度服务 + 起任务线程池（构造时自动调，重复调幂等）。"""
        if self._unregister is not None:
            return
        self._stop_event.clear()
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_concurrent,
            thread_name_prefix="waker-run",
        )
        self._unregister = self._schedule.register(
            "waker", self._tick_seconds, self._tick, max_workers=1,
        )
        logger.info(
            f"WakerScheduler 已启动: tick={self._tick_seconds}s, "
            f"max_concurrent={self._max_concurrent}"
        )

    def stop(self, timeout: float = 5.0) -> None:
        """注销调度 + 关闭任务线程池。构造未 start 时安全（幂等）。

        先设 _stop_event（在跑的 _tick 尽快退出、不再提交新任务）；
        再注销（等在跑的 _tick 收尾）；再 shutdown 任务线程池（不强行
        取消正在跑的任务——它们已经在 worker 子进程里，让它们自然完成）；
        自建调度服务时最后停服务本身。
        """
        self._stop_event.set()
        unreg = self._unregister
        self._unregister = None
        if unreg is not None:
            unreg()
        executor = self._executor
        if executor is not None:
            # wait=True 让在跑的 waker_run 完成（best-effort，避免半途杀进程
            # 导致 worker 的 send 永久挂起）。配合 lifespan 里"先 stop scheduler
            # 再 shutdown_all worker"的顺序，能保证任务干净收尾。
            try:
                executor.shutdown(wait=True, cancel_futures=True)
            except Exception:
                executor.shutdown(wait=False)
        self._executor = None
        if self._owns_schedule:
            self._schedule.stop(timeout=timeout)
        logger.info("WakerScheduler 已停止")

    # ════════════════════════════════════════════════════════════════
    # 调度入口（由 SchedulerService 周期调用）
    # ════════════════════════════════════════════════════════════════
    def _tick(self) -> None:
        """单次扫描：发现到期 waker → 提交到线程池。"""
        now = datetime.now()
        try:
            all_wakers = list(iter_all_wakers(self._workspace_root))
        except Exception:
            logger.exception("iter_all_wakers 失败（跳过本次 tick）")
            return

        for uid, cfg in all_wakers:
            if self._stop_event.is_set():
                # shutdown 期间不再提交新任务
                return
            if not cfg.enabled:
                continue
            # P1-7：逐条异常隔离。一条坏配置（如带时区的 expire_at 让
            # compute_next_run/is_due 抛 TypeError）只跳过该条，不再把
            # 按目录名排序在其后的所有 waker 永久停摆。
            try:
                self._tick_one(uid, cfg, now)
            except Exception:
                logger.exception(f"waker 调度项处理失败，跳过: {uid}/{cfg.name}")
                continue

    def _tick_one(self, uid: str, cfg, now: datetime) -> None:
        """处理单个 waker 的到期判定与提交（调用方逐条隔离异常）。"""
        # 首次调度入口：next_run_at 为空时，先 compute_next_run 初始化它并落盘。
        # 否则新建 waker 的 next_run_at 永远空 → is_due 永远 False → 死锁不触发。
        if not cfg.next_run_at:
            nxt = compute_next_run(cfg, now)
            if nxt is None:
                return  # 已过期/达 max_runs/none 类型，不调度
            cfg.next_run_at = nxt.isoformat(timespec="seconds")
            try:
                from src.waker.store import WakerStore
                WakerStore(uid, workspace_root=self._workspace_root).save_state(cfg)
                logger.info(f"waker 首次调度初始化: {uid}/{cfg.name} → next={cfg.next_run_at}")
            except Exception:
                logger.warning(f"waker 首次调度初始化失败: {uid}/{cfg.name}", exc_info=True)
        if not is_due(cfg, now):
            return

        key = (uid, cfg.name)
        with self._lock:
            if key in self._running:
                logger.debug(f"waker 正在跑，跳过: {uid}/{cfg.name}")
                return
            self._running.add(key)

        executor = self._executor
        if executor is None:
            # 已 stop
            self._release(key)
            return

        future = executor.submit(self._run_one_safe, uid, cfg.name, now)
        self._pending_futures.add(future)
        future.add_done_callback(self._make_done_callback(key))

    # ════════════════════════════════════════════════════════════════
    # 单 waker 任务
    # ════════════════════════════════════════════════════════════════
    def _make_done_callback(self, key: tuple[str, str]):
        """构造 future 完成回调：从 running 集合移除 + 清理 future 引用。"""
        def _cb(fut) -> None:
            self._release(key)
            try:
                self._pending_futures.discard(fut)
            except Exception:
                pass
        return _cb

    def _release(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._running.discard(key)

    def _run_one_safe(self, uid: str, name: str, now: datetime) -> None:
        """_run_one 的异常隔离包装：保证任何异常都不冒泡到线程池。"""
        try:
            self._run_one(uid, name, now)
        except Exception:
            logger.exception(f"waker 运行异常: {uid}/{name}")

    def _run_one(self, uid: str, name: str, now: datetime) -> None:
        """跑单个 waker：worker.send("waker_run") → 根据结果更新 state。"""
        # 每次重新读 cfg（config 可能在两次 tick 间被改）
        from src.waker.store import WakerStore
        store = WakerStore(uid, workspace_root=self._workspace_root)
        cfg = store.get(name)
        if cfg is None:
            logger.warning(f"waker 不存在（已被删？）: {uid}/{name}")
            return
        if not cfg.enabled:
            logger.debug(f"waker 已禁用，跳过: {uid}/{name}")
            return

        run_id = store.new_run_id()
        logger.info(f"waker 触发: {uid}/{name} run_id={run_id}")

        status = "ok"
        try:
            if self._waker_runner is not None:
                # 新路径：fork worker_node 子进程（不抢 worker 锁，不影响 chat）
                status = self._waker_runner.run_sync(uid, name, run_id)
            else:
                # 旧路径（兼容）：走 per-user worker IPC，会与 chat 抢锁
                wp = self._worker_manager.get_or_create(uid, slot="main")
                events = wp.send(
                    "waker_run",
                    timeout=_run_timeout(),
                    name=name,
                    run_id=run_id,
                    api_prompt=None,
                )
                status = self._extract_status(events)
        except Exception:
            logger.exception(f"waker_run 失败: {uid}/{name}")
            status = "error"

        # 更新 state（无论成功失败都更新，让 next_run_at 推进）
        try:
            # 重读 cfg 以防 worker 侧改了 run_count（实际上 worker 不写 yaml，
            # 但 max_runs 等可能在调度期间被改；这里保守重读）
            cfg = store.get(name) or cfg
            cfg.run_count += 1
            cfg.last_run_at = now.isoformat(timespec="seconds")
            cfg.last_status = status
            nxt = compute_next_run(cfg, datetime.now())
            cfg.next_run_at = nxt.isoformat(timespec="seconds") if nxt else ""
            store.save_state(cfg)
        except Exception:
            logger.exception(f"waker 状态更新失败: {uid}/{name}")

    # ════════════════════════════════════════════════════════════════
    # 手动触发（API invoke）
    # ════════════════════════════════════════════════════════════════
    def submit_now(self, user_id: str, name: str, api_prompt: str | None = None) -> str:
        """手动触发一次 waker 运行（API invoke 调用）。

        与定时 tick 的区别：
        - 不检查 cfg.enabled（手动触发即使未 enabled 也允许）
        - 不检查 is_due（立即跑）
        - 不进 _running 防重入集合（手动触发优先；run_id 唯一所以 jsonl
          文件不会撞，并发安全）
        - 不重算 next_run_at（手动触发不影响定时节奏）
        - 仍更新 last_run_at / last_status / run_count

        Returns:
            run_id（提交后立即返回，不等执行完）
        Raises:
            ValueError: waker 不存在
        """
        from src.waker.store import WakerStore
        store = WakerStore(user_id, workspace_root=self._workspace_root)
        cfg = store.get(name)
        if cfg is None:
            raise ValueError(f"waker 不存在: {user_id}/{name}")

        run_id = store.new_run_id()
        logger.info(f"waker 手动触发: {user_id}/{name} run_id={run_id}")

        executor = self._executor
        if executor is None:
            # 调度器已 stop（极端情况）：临时起一个一次性线程兜底，
            # 保证 invoke 不因调度器关闭而失败。
            import threading
            t = threading.Thread(
                target=self._run_manual_safe,
                args=(user_id, name, run_id, api_prompt),
                daemon=True,
                name=f"waker-manual-{name}",
            )
            t.start()
            return run_id

        future = executor.submit(
            self._run_manual_safe, user_id, name, run_id, api_prompt
        )
        self._pending_futures.add(future)
        future.add_done_callback(self._make_done_callback_manual())
        return run_id

    def _make_done_callback_manual(self):
        """手动触发 future 的完成回调：仅清理 future 引用（不动 _running）。"""
        def _cb(fut) -> None:
            try:
                self._pending_futures.discard(fut)
            except Exception:
                pass
        return _cb

    def _run_manual_safe(
        self, uid: str, name: str, run_id: str, api_prompt: str | None
    ) -> None:
        """手动触发的异常隔离包装。"""
        try:
            self._run_manual(uid, name, run_id, api_prompt)
        except Exception:
            logger.exception(f"waker 手动运行异常: {uid}/{name}/{run_id}")

    def _run_manual(
        self, uid: str, name: str, run_id: str, api_prompt: str | None
    ) -> None:
        """手动触发跑一次：复用 worker.send("waker_run")，但不重算 next_run_at。"""
        from src.waker.store import WakerStore
        store = WakerStore(uid, workspace_root=self._workspace_root)
        cfg = store.get(name)
        if cfg is None:
            logger.warning(f"waker 不存在（已被删？）: {uid}/{name}")
            return

        status = "ok"
        try:
            if self._waker_runner is not None:
                # 新路径：fork worker_node 子进程（不抢 worker 锁）
                status = self._waker_runner.run_sync(uid, name, run_id, api_prompt)
            else:
                # 旧路径（兼容）
                wp = self._worker_manager.get_or_create(uid, slot="main")
                events = wp.send(
                    "waker_run",
                    timeout=_run_timeout(),
                    name=name,
                    run_id=run_id,
                    api_prompt=api_prompt,
                )
                status = self._extract_status(events)
        except Exception:
            logger.exception(f"waker_run 失败: {uid}/{name}/{run_id}")
            status = "error"

        # 更新 last_run_at / last_status / run_count；不重算 next_run_at
        # （手动触发不影响定时节奏）。
        try:
            cfg = store.get(name) or cfg
            cfg.run_count += 1
            cfg.last_run_at = datetime.now().isoformat(timespec="seconds")
            cfg.last_status = status
            store.save_state(cfg)
        except Exception:
            logger.exception(f"waker 手动状态更新失败: {uid}/{name}/{run_id}")

    @staticmethod
    def _extract_status(events: list[dict]) -> str:
        """从 send 返回的事件列表里取最终 status。

        worker_process 把 run_waker 的返回 dict 作为 result 事件回写：
        {"type": "result", "data": {"status": "ok"/"error", ...}}。
        找不到时回退 "ok"（避免 worker 协议变更时误报 error）。
        """
        if not events:
            return "ok"
        for ev in reversed(events):
            if ev.get("type") == "result":
                data = ev.get("data") or {}
                s = data.get("status")
                if s in ("ok", "error"):
                    return s
                # 旧协议可能用 ok 字段
                if data.get("ok") is False:
                    return "error"
                return "ok"
        return "ok"
