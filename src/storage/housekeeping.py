"""
StorageHousekeeping —— 存储卫生周期任务（ADR-0004-D3 / ADR-0002 纪律①）。

职责（单线程、低频、异常全隔离，绝不影响前台服务）：
- WAL checkpoint：-wal 超过阈值字节或距上次 TRUNCATE ≥24h 时执行
  checkpoint(TRUNCATE)，防止 -wal 无界膨胀（审计实测曾达主库 4.6 倍且无管理）
- interrupt 事件 TTL：删除「已完结且陈旧」的中断事件对（pending 审批永不删，
  见 SQLiteProvider.purge_resolved_interrupts）
- LLM 用量保留期：删除 llm_usage 表早于 180 天的记账行（usage_store.purge_before）
- 会话事件冷归档（P3）：对"有 COMPACT_APPLIED 且静默足够久"的会话执行
  SessionLog.archive_compacted_events（最后 compact 之前的热行导出冷文件
  并删热行；compact 及之后永远留热表，详见 session_log 模块 docstring）

周期驱动交给统一调度服务 SchedulerService（自建实例兜底）；
实际动作在其注册的 tick 里同步执行（两步都是廉价操作，无需独立线程池）。
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from src.scheduling import SchedulerService

logger = logging.getLogger("hermes.storage.housekeeping")

#: WAL 文件超过该字节数即触发 TRUNCATE checkpoint
WAL_CHECKPOINT_THRESHOLD_BYTES = 2 * 1024 * 1024
#: 距上次 TRUNCATE 超过该秒数则无条件执行一次（兜底防低写入库 wal 永不收缩）
TRUNCATE_MIN_INTERVAL_SECONDS = 24 * 3600
#: 已完结中断事件的保留天数
INTERRUPT_TTL_DAYS = 7
#: LLM 用量记录（llm_usage 表）的保留天数
USAGE_RETENTION_DAYS = 180
#: 冷归档静默门槛：会话最后一条事件距今超过该秒数才允许归档（并发防护兜底，
#: 见 ARCHIVE_QUIET_SECONDS 注释）
ARCHIVE_QUIET_SECONDS = 15 * 60


class StorageHousekeeping:
    """存储卫生调度器（SchedulerService 驱动）。"""

    def __init__(
        self,
        tick_seconds: int = 3600,
        schedule: "SchedulerService | None" = None,
        storage=None,
        wal_checkpoint_threshold_bytes: int = WAL_CHECKPOINT_THRESHOLD_BYTES,
        interrupt_ttl_days: int = INTERRUPT_TTL_DAYS,
        usage_retention_days: int = USAGE_RETENTION_DAYS,
        events_archive_enabled: bool = True,
        archive_quiet_seconds: int = ARCHIVE_QUIET_SECONDS,
        is_session_active: "Callable[[str], bool] | None" = None,
    ):
        self._tick_seconds = max(60, int(tick_seconds))
        self._wal_threshold = max(0, int(wal_checkpoint_threshold_bytes))
        self._interrupt_ttl_days = max(1, int(interrupt_ttl_days))
        self._usage_retention_days = max(1, int(usage_retention_days))
        self._events_archive_enabled = bool(events_archive_enabled)
        self._archive_quiet_seconds = max(0, int(archive_quiet_seconds))
        # 活跃会话判定（web 装配层注入：查 worker_manager 槽位 streaming /
        # last_active）。None 时仅靠静默时间门槛兜底（CLI 进程 / 测试场景）。
        self._is_session_active = is_session_active

        # 统一调度服务：None 时自建（独立可用）
        self._schedule = schedule if schedule is not None else SchedulerService()
        self._owns_schedule = schedule is None
        self._unregister: Callable[[], None] | None = None

        # provider：None 时用默认库；测试可注入临时实例
        if storage is not None:
            self._provider = storage
        else:
            from src.storage.sqlite_provider import SQLiteProvider
            self._provider = SQLiteProvider()

    # ── 生命周期 ──
    def start(self) -> None:
        """注册到调度服务（幂等；本类不随构造自动启动）。"""
        if self._unregister is not None:
            return
        self._unregister = self._schedule.register(
            "storage_housekeeping", self._tick_seconds, self._tick, max_workers=1,
        )
        logger.info(
            f"StorageHousekeeping 已启动: tick={self._tick_seconds}s, "
            f"wal_threshold={self._wal_threshold}B, interrupt_ttl={self._interrupt_ttl_days}d, "
            f"events_archive={'on' if self._events_archive_enabled else 'off'} "
            f"(quiet={self._archive_quiet_seconds}s)"
        )

    def stop(self, timeout: float = 5.0) -> None:
        """注销调度（幂等）。自建服务时停服务。"""
        unreg = self._unregister
        self._unregister = None
        if unreg is not None:
            unreg()
        if self._owns_schedule:
            self._schedule.stop(timeout=timeout)
        logger.info("StorageHousekeeping 已停止")

    # ── 周期入口 ──
    def _tick(self) -> None:
        try:
            self.run_once()
        except Exception:
            logger.exception("存储卫生任务异常（本周期跳过）")

    def run_once(self) -> dict:
        """单次卫生执行（也供测试/手动调用）。返回动作摘要。"""
        summary: dict = {"checkpointed": False, "purged_events": 0,
                         "purged_usage": 0, "archived_events": 0}

        should_truncate = False
        wal_size = self._provider.wal_size_bytes()
        if wal_size > self._wal_threshold:
            should_truncate = True
        else:
            try:
                last = float(self._provider.kv_get(
                    "housekeeping", "last_wal_truncate", 0.0) or 0.0)
                if time.time() - last >= TRUNCATE_MIN_INTERVAL_SECONDS:
                    should_truncate = True
            except Exception:
                logger.warning("读取 housekeeping kv 失败，跳过时间兜底判断", exc_info=True)

        if should_truncate and wal_size > 0:
            self._provider.checkpoint_wal()
            summary["checkpointed"] = True
            try:
                self._provider.kv_put(
                    "housekeeping", "last_wal_truncate", time.time())
            except Exception:
                logger.warning("记录 last_wal_truncate 失败（不影响功能）", exc_info=True)

        try:
            summary["purged_events"] = self._provider.purge_resolved_interrupts(
                self._interrupt_ttl_days)
        except AttributeError:
            logger.warning("provider 不支持 purge_resolved_interrupts，跳过 TTL 清理")

        # LLM 用量保留期清理（D1）：走本实例的 provider（测试可注入临时库）；
        # 失败只告警，不影响其他清理步骤
        try:
            from src.storage.usage_store import UsageStore
            summary["purged_usage"] = UsageStore(self._provider).purge_before(
                time.time() - self._usage_retention_days * 86400)
        except Exception:
            logger.warning("llm_usage 保留期清理失败（跳过，下周期重试）", exc_info=True)

        if self._events_archive_enabled:
            try:
                summary["archived_events"] = self._archive_compacted_sessions()
            except Exception:
                # 归档是纯优化（读侧合并视图兜底），失败只降级不留患
                logger.exception("会话事件冷归档异常（本周期跳过）")
        return summary

    # ── 会话事件冷归档（P3）──

    def _archive_compacted_sessions(self) -> int:
        """对本 tick 扫到的每个可归档会话执行冷归档，返回归档总行数。

        候选条件（逐会话）：
        - scope="chat" 下出现过事件（provider.event_session_ids 枚举）；
        - 存在 COMPACT_APPLIED（last_event_id_of_type > 0，SQL 廉价判定）；
        - 并发防护（双保险）：
          ① 注入的 is_session_active(sid)（web 装配层查 worker_manager：
             该 sid 有存活槽且 streaming 或 last_active 很近）→ 跳过；
             判定回调抛异常按"活跃"处理（保守跳过，宁可晚归档不抢写）。
          ② 静默时间门槛：会话最后一条热事件距今不足
             ARCHIVE_QUIET_SECONDS → 跳过。流式一轮会持续写 turn/user/
             assistant 事件，"最后事件很新"可靠覆盖主槽会话与判定回调
             缺席的场景（CLI 进程 / 测试）。
        """
        from src.agent.session_log import COMPACT_APPLIED, SessionLog

        session_ids = getattr(self._provider, "event_session_ids", None)
        if not callable(session_ids):
            return 0  # 自定义 provider 缺扫描原语 → 本周期不归档
        log = SessionLog(provider=self._provider)
        now = time.time()
        archived = 0
        for sid in session_ids("chat"):
            try:
                compact_id = self._provider.last_event_id_of_type(
                    "chat", sid, COMPACT_APPLIED)
                if compact_id <= 0:
                    continue  # 无 compact：会话不动
                if self._is_session_active is not None:
                    try:
                        if self._is_session_active(sid):
                            continue  # 活跃会话跳过（槽位判定）
                    except Exception:
                        logger.warning(
                            f"活跃判定回调异常（按活跃处理，跳过）: sid={sid}",
                            exc_info=True)
                        continue
                hot = self._provider.iter_events("chat", sid)
                if not hot or now - float(hot[-1].get("ts") or 0.0) < self._archive_quiet_seconds:
                    continue  # 静默门槛：最近仍有写入 → 跳过
                archived += log.archive_compacted_events(sid)
            except Exception:
                logger.exception(f"会话冷归档失败（跳过该会话）: sid={sid}")
        return archived


# ── 启动 ops 心跳（P2-6；web_fastapi/app.py 的 lifespan 装配完成后调用一次）──

def collect_ops_snapshot(storage=None) -> dict:
    """采集启动快照：WAL 字节数 + events/memories 行数（只读、绝不抛异常）。

    返回 {"wal_bytes": int|None, "events_rows": int|None,
    "memories_rows": int|None}。单项查询失败（全新环境无库/无表、provider
    不支持对应入口等）该键置 None，由调用方渲染为 "-"——本函数自身吞掉
    一切异常，绝不影响启动流程。storage=None 时自建默认库连接（与
    StorageHousekeeping 的兜底同形，用完即关；测试可注入临时实例）。
    """
    snap: dict = {"wal_bytes": None, "events_rows": None, "memories_rows": None}
    provider = storage
    created = False
    if provider is None:
        try:
            from src.storage.sqlite_provider import SQLiteProvider
            provider = SQLiteProvider()
            created = True
        except Exception:
            return snap
    try:
        try:
            snap["wal_bytes"] = int(provider.wal_size_bytes())
        except Exception:
            pass
        # 行数走 provider.query 通用只读入口（表名与 sqlite_provider._DDL
        # 对齐）；自定义 provider 无 query 时按"该项不可得"降级。
        query = getattr(provider, "query", None)
        if callable(query):
            for key, table in (("events_rows", "events"),
                               ("memories_rows", "memories")):
                try:
                    snap[key] = int(query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])
                except Exception:
                    pass
    finally:
        if created:
            try:
                provider.close()
            except Exception:
                pass
    return snap
