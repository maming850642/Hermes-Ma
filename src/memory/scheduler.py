"""
MemoryConsolidationScheduler —— 主进程内的记忆聚合调度器。

T8a 起不再自持 daemon 线程：周期驱动交给统一调度服务 SchedulerService
（src/plugins/scheduler_plugin.py，构造/ start 时 register 自己的 _tick）。
保留本类特有逻辑：
- 后台定时：单用户（LOCAL_USER），读 kv 聚合配置，达标则聚合
  （auto_consolidate=true && 距上次聚合 ≥ interval_hours && 活跃记忆数 > threshold）
- 手动触发：submit_now(uid) 立即跑（不查开关/阈值），返回 run_id 供前端轮询

聚合在主进程线程池里跑：新建独立 MemoryManager（默认 SQLite 后端 + 1 次 LLM，
不需 agent），不抢 per-user worker IPC 锁，不 fork 子进程，不阻塞 chat。

T2b-②：单用户拍平。不再扫描 users/*/ 目录（_resolve_workspace /
_iter_users_with_memories 已删），直接对 LOCAL_USER 判定。
"""
from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable

from src.constants import LOCAL_USER
from src.memory import config_store
from src.scheduling import SchedulerService
from src.storage.run_registry import RunRegistry

logger = logging.getLogger("hermes.memory.scheduler")


class MemoryConsolidationScheduler:
    """记忆聚合调度器（SchedulerService 驱动 + 线程池）。

    Args:
        workspace_root: 已废弃（保参兼容旧调用面，被忽略）
        tick_seconds: 扫描间隔（秒），默认 300s（聚合非高频）
        max_concurrent: 并发聚合上限（单用户下实际至多 1）
        schedule: 统一调度服务（共享实例）；None 时自建独立实例
        storage: KVProtocol（手动触发的运行登记表持久化用）。None 时用
            默认库的 SQLiteProvider（data/hermes.db）
    """

    def __init__(
        self,
        workspace_root: str = "",
        tick_seconds: int = 300,
        max_concurrent: int = 2,
        schedule: "SchedulerService | None" = None,
        storage=None,
    ):
        self._workspace_root = workspace_root  # 已废弃，仅保参
        self._tick_seconds = max(60, int(tick_seconds))  # 最小 60s，避免空转过频
        self._max_concurrent = max(1, int(max_concurrent))

        # 统一调度服务：None 时自建（独立可用）
        self._schedule = schedule if schedule is not None else SchedulerService()
        self._owns_schedule = schedule is None
        self._unregister: Callable[[], None] | None = None

        # shutdown 期间 _tick 快速退出（主循环骨架已移至 SchedulerService）
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        # 正在自动聚合的标记，防重入
        self._running: set[str] = set()
        self._executor: ThreadPoolExecutor | None = None
        # 手动触发的运行状态：run_id -> {status, result, user_id}。
        # T8b 起经 RunRegistry 写穿 kv（scope="runs", key="memory_consolidation"），
        # 重启后可见；残留 running 标 interrupted。
        self._runs = RunRegistry("memory_consolidation", storage)
        self._pending_futures: set = set()

    # ════════════════════════════════════════════════════════════════
    # 生命周期
    # ════════════════════════════════════════════════════════════════
    def start(self) -> None:
        """注册到调度服务 + 起任务线程池（幂等；本类不随构造自动启动）。"""
        if self._unregister is not None:
            return
        self._stop_event.clear()
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_concurrent,
            thread_name_prefix="mem-consolidate",
        )
        self._unregister = self._schedule.register(
            "memory", self._tick_seconds, self._tick, max_workers=1,
        )
        logger.info(
            f"MemoryConsolidationScheduler 已启动: tick={self._tick_seconds}s, "
            f"max_concurrent={self._max_concurrent}"
        )

    def stop(self, timeout: float = 5.0) -> None:
        """注销调度 + 关闭任务线程池（幂等）。自建服务时停服务。"""
        self._stop_event.set()
        unreg = self._unregister
        self._unregister = None
        if unreg is not None:
            unreg()
        executor = self._executor
        if executor is not None:
            try:
                executor.shutdown(wait=True, cancel_futures=True)
            except Exception:
                executor.shutdown(wait=False)
        self._executor = None
        if self._owns_schedule:
            self._schedule.stop(timeout=timeout)
        logger.info("MemoryConsolidationScheduler 已停止")

    # ════════════════════════════════════════════════════════════════
    # 后台定时入口（由 SchedulerService 周期调用）
    # ════════════════════════════════════════════════════════════════
    def _tick(self) -> None:
        """单次检查：单用户（LOCAL_USER）达标 → 提交到线程池。"""
        now = datetime.now()
        uid = LOCAL_USER
        if self._stop_event.is_set():
            return
        if not self._should_auto_run(uid, now):
            return
        with self._lock:
            if uid in self._running:
                return
            self._running.add(uid)
        executor = self._executor
        if executor is None:
            self._release(uid)
            return
        future = executor.submit(self._run_one_safe, uid)
        self._pending_futures.add(future)
        future.add_done_callback(self._make_done_callback(uid))

    def _should_auto_run(self, uid: str, now: datetime) -> bool:
        """读 kv 配置，判定是否该自动聚合（单用户）。"""
        try:
            cfg = config_store.load(self._workspace_root, uid)
        except Exception:
            logger.warning(f"读取聚合配置失败，跳过: {uid}", exc_info=True)
            return False
        if not cfg["auto_consolidate"]:
            return False
        # 间隔检查
        last = cfg.get("last_run_at") or ""
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                elapsed_h = (now - last_dt).total_seconds() / 3600
                if elapsed_h < cfg["interval_hours"]:
                    return False
            except ValueError:
                # last_run_at 解析失败：当作从未跑过，允许触发
                pass
        # 条数阈值：读当前记忆计数（默认 SQLite 后端）
        try:
            from src.memory.manager import MemoryManager
            mgr = MemoryManager()
            count = len(mgr.store.get_all())
        except Exception:
            logger.warning(f"读取记忆计数失败，跳过: {uid}", exc_info=True)
            return False
        if count <= cfg["threshold"]:
            return False
        return True

    def _make_done_callback(self, uid: str):
        def _cb(fut) -> None:
            self._release(uid)
            try:
                self._pending_futures.discard(fut)
            except Exception:
                pass
        return _cb

    def _release(self, uid: str) -> None:
        with self._lock:
            self._running.discard(uid)

    def _run_one_safe(self, uid: str) -> None:
        """自动聚合的异常隔离包装。"""
        try:
            self._run_one(uid)
        except Exception:
            logger.exception(f"记忆聚合异常: {uid}")

    def _run_one(self, uid: str) -> None:
        """跑单用户聚合（自动调度路径）。"""
        from src.memory.manager import MemoryManager
        mgr = MemoryManager()
        result = mgr.consolidate(uid)
        if result.get("ok"):
            # 仅成功才推进 last_run_at（失败下次 tick 还会重试）
            try:
                config_store.touch_last_run(self._workspace_root, uid)
            except Exception:
                logger.warning(f"更新 last_run_at 失败: {uid}", exc_info=True)
        logger.info(f"自动聚合完成: {uid}, result={result}")

    # ════════════════════════════════════════════════════════════════
    # 手动触发（API 调用）—— submit-and-poll
    # ════════════════════════════════════════════════════════════════
    def submit_now(self, user_id: str) -> str:
        """手动立即触发聚合（不查开关/阈值），返回 run_id 供轮询。

        用独立的运行状态跟踪（不进 _running 防重入集合——手动触发优先）。
        调度器已 stop 时临时起一次性线程兜底，保证不失败。
        """
        run_id = uuid.uuid4().hex
        self._runs.upsert({
            "run_id": run_id,
            "status": "running", "result": None, "user_id": user_id,
        })

        executor = self._executor
        if executor is None:
            t = threading.Thread(
                target=self._run_manual_safe, args=(run_id, user_id),
                daemon=True, name=f"mem-consolidate-manual-{user_id}",
            )
            t.start()
            return run_id

        future = executor.submit(self._run_manual_safe, run_id, user_id)
        self._pending_futures.add(future)
        future.add_done_callback(self._make_manual_done_cb())
        return run_id

    def get_status(self, run_id: str) -> dict:
        """查运行状态。run_id 不存在返回 not_found。"""
        r = self._runs.get(run_id)
        if r is None:
            return {"status": "not_found", "result": None}
        return {"status": r.get("status"), "result": r.get("result")}

    def _make_manual_done_cb(self):
        def _cb(fut) -> None:
            try:
                self._pending_futures.discard(fut)
            except Exception:
                pass
        return _cb

    def _run_manual_safe(self, run_id: str, user_id: str) -> None:
        try:
            self._run_manual(run_id, user_id)
        except Exception as e:
            logger.exception(f"手动聚合异常: {user_id}/{run_id}")
            self._runs.update_status(
                run_id,
                status="error",
                result={"ok": False, "error": str(e)},
                user_id=user_id,
            )

    def _run_manual(self, run_id: str, user_id: str) -> None:
        from src.memory.manager import MemoryManager
        mgr = MemoryManager()
        result = mgr.consolidate(user_id)
        status = "done" if result.get("ok") else "done"  # 完成（无论是否聚合成功）
        # too_few / llm_failed 也算"执行完毕"，区别于运行中/异常
        self._runs.update_status(run_id, status=status, result=result, user_id=user_id)
        logger.info(f"手动聚合完成: {user_id}/{run_id}, ok={result.get('ok')}")
