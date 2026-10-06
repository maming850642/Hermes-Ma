"""
============================================
RunRegistry —— 运行登记表（内存快路径 + kv 写穿持久化，T8b）
============================================
把三处进程内易失的运行状态表（WakerAsyncRunner._runs / FlowRunner._runs /
MemoryConsolidationScheduler._runs）收敛为同一设施，重启后可见。

## 设计
- kv 布局：scope="runs"，key 由宿主指定（"waker_async" / "flow" /
  "memory_consolidation"），value = 该类记录的 dict 列表（每条含 run_id）
- 内存 dict 是快路径：读写全程持锁，零 kv 依赖；每次变更写穿 kv
  （kv 失败仅告警不阻断——登记表不是关键数据，jsonl/latest_result 才是）
- 重启恢复：__init__ 从 kv 加载历史记录；status 属于活跃态（running/
  pending）的记录说明是进程重启前的残留（本进程不可能在跑它），标为
  "interrupted"，不自动续跑，写回 kv 固化

## 宿主接入约定
- 记录用纯 dict（含 run_id 字段）；字段集合由宿主自定义（RunRecord /
  WakerRunRecord 的字段形状由宿主在 upsert/get 时保持）
- upsert(record)：整条覆盖（按 record["run_id"]）
- update_status(run_id, **fields)：增量改字段（记录不存在时 no-op）
- get(run_id) / list()：返回拷贝（外部改不动内部状态）
"""
from __future__ import annotations

import copy
import logging
import threading
from typing import Any

from src.storage.base import KVProtocol

logger = logging.getLogger("hermes.storage.run_registry")

# 所有宿主统一的 kv scope
SCOPE = "runs"

# 进程重启前不可能完成的活跃态 → 恢复时标 interrupted
DEFAULT_ACTIVE_STATUSES = ("running", "pending")

# R3-15：登记表上限（按写入序保留最新 N 条；活跃态优先保留、可超出上限）
MAX_RECORDS = 200


class RunRegistry:
    """运行登记表（内存 dict + kv 写穿，线程安全）。

    Args:
        scope_key: kv key（scope 恒为 "runs"），如 "waker_async" / "flow"
        provider: KVProtocol 实现（如 SQLiteProvider）。None 时用默认库的
            SQLiteProvider（data/hermes.db）
        interrupted_status: 恢复时残留活跃态改写成的状态值
        active_statuses: 视为"进程重启前还在跑"的状态集合
    """

    def __init__(
        self,
        scope_key: str,
        provider: KVProtocol | None = None,
        *,
        interrupted_status: str = "interrupted",
        active_statuses: tuple[str, ...] = DEFAULT_ACTIVE_STATUSES,
    ):
        if provider is None:
            from src.storage.sqlite_provider import SQLiteProvider
            provider = SQLiteProvider()
        self._scope_key = scope_key
        self._provider = provider
        self._interrupted_status = interrupted_status
        self._active_statuses = tuple(active_statuses)
        self._lock = threading.RLock()
        self._records: dict[str, dict] = {}
        self._load_from_kv()
        # R3-15：kv 历史可能超上限（旧版本无修剪）——加载后修剪并固化
        before = len(self._records)
        self._prune()
        if len(self._records) != before:
            self._write_through()

    # ============================================
    # 重启恢复
    # ============================================
    def _load_from_kv(self) -> None:
        """从 kv 加载历史记录；残留活跃态标 interrupted 并写回。"""
        try:
            raw = self._provider.kv_get(SCOPE, self._scope_key)
        except Exception:
            logger.warning(
                f"RunRegistry({self._scope_key}) 从 kv 加载失败，按空表启动",
                exc_info=True,
            )
            return
        if not isinstance(raw, list):
            return
        records: dict[str, dict] = {}
        for item in raw:
            if isinstance(item, dict) and item.get("run_id"):
                records[str(item["run_id"])] = item
        self._records = records

        # 残留活跃态 → interrupted（进程已重启，这些 run 不可能还在跑）
        interrupted = [
            r for r in self._records.values()
            if r.get("status") in self._active_statuses
        ]
        if interrupted:
            for r in interrupted:
                r["status"] = self._interrupted_status
            logger.info(
                f"RunRegistry({self._scope_key}) 恢复 {len(self._records)} 条记录，"
                f"其中 {len(interrupted)} 条残留运行态标为 {self._interrupted_status!r}"
            )
            self._write_through()

    # ============================================
    # 读写接口（内存快路径 + kv 写穿）
    # ============================================
    def upsert(self, record: dict) -> None:
        """整条写入/覆盖（按 record["run_id"]）。"""
        run_id = record.get("run_id", "")
        if not run_id:
            raise ValueError("RunRegistry.upsert 需要 record 含非空 run_id")
        with self._lock:
            self._records[str(run_id)] = copy.deepcopy(record)
            self._prune()
            self._write_through()

    def _read_kv_records(self) -> dict[str, dict] | None:
        """仅从 kv 读取当前记录（不做 interrupted 改写、不回写）。

        返回 None 表示读取失败（调用方回落内存缓存）。跨实例/跨进程
        可见性依赖这里每次直读——主进程路由与 worker 各持实例时，
        写穿后的记录必须立即可见于对方。
        """
        try:
            raw = self._provider.kv_get(SCOPE, self._scope_key)
        except Exception:
            logger.warning(
                f"RunRegistry({self._scope_key}) 直读 kv 失败，回落内存缓存",
                exc_info=True,
            )
            return None
        if not isinstance(raw, list):
            return {}
        out: dict[str, dict] = {}
        for item in raw:
            if isinstance(item, dict) and item.get("run_id"):
                out[str(item["run_id"])] = item
        return out

    def get(self, run_id: str) -> dict | None:
        """查单条（拷贝）。不存在返回 None。

        读取顺序：kv 直读命中 → 返回；未命中回落内存缓存（覆盖两种真实
        场景：另一实例刚写入（kv 可见），以及本实例写穿失败但内存仍持有
        最新值（R3-15 韧性测试锁定的语义）。二者皆无 → None。
        """
        fresh = self._read_kv_records()
        if fresh is not None and str(run_id) in fresh:
            rec = fresh[str(run_id)]
            return copy.deepcopy(rec)
        with self._lock:
            rec = self._records.get(str(run_id))
            return copy.deepcopy(rec) if rec is not None else None

    def list(self) -> list[dict]:
        """列出全部记录（拷贝，保持写入顺序）。读取策略同 get。"""
        fresh = self._read_kv_records()
        if fresh is not None:
            return copy.deepcopy(list(fresh.values()))
        with self._lock:
            return copy.deepcopy(list(self._records.values()))

    def update_status(self, run_id: str, **fields: Any) -> None:
        """增量更新已有记录的字段（status 及任意附带字段）。

        记录不存在时 no-op（与旧内存表 `rec = _runs.get(); if rec is None: return`
        语义一致）。
        """
        with self._lock:
            rec = self._records.get(str(run_id))
            if rec is None:
                return
            rec.update(fields)
            self._write_through()

    def delete(self, run_id: str) -> bool:
        """删除一条记录。不存在返回 False；活跃态（running/pending）拒绝删除返回 False。

        首页死信芯片关掉走这条：只清账本，不影响已落盘的项目目录。
        """
        rid = str(run_id)
        with self._lock:
            fresh = self._read_kv_records()
            if fresh is not None:
                self._records = fresh
            rec = self._records.get(rid)
            if rec is None:
                return False
            if rec.get("status") in self._active_statuses:
                return False
            del self._records[rid]
            self._write_through()
            return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def __contains__(self, run_id: object) -> bool:
        with self._lock:
            return str(run_id) in self._records

    # ============================================
    # 内部
    # ============================================
    def _prune(self) -> None:
        """R3-15：列表超 MAX_RECORDS → 丢弃尾部（最旧）终态记录，回到上限。

        活跃态（status 命中 active_statuses）优先保留——即便最旧也不丢，
        宁可暂时超出上限（活跃记录丢了会把进行中的 run 变成孤儿）。
        按写入序（dict 插入序）判定新旧；被丢记录数返回值供调用方决定是否
        额外写穿（本方法不写穿——调用方 upsert/__init__ 紧跟 _write_through）。
        """
        if len(self._records) <= MAX_RECORDS:
            return
        overflow = len(self._records) - MAX_RECORDS
        dropped: list[str] = []
        # 最旧 → 最新扫：跳过活跃态，丢终态直到回到上限
        for run_id, rec in self._records.items():
            if overflow - len(dropped) <= 0:
                break
            if rec.get("status") in self._active_statuses:
                continue
            dropped.append(run_id)
        for run_id in dropped:
            del self._records[run_id]
        if dropped:
            logger.info(
                f"RunRegistry({self._scope_key}) 超上限 {MAX_RECORDS}，"
                f"丢弃最旧终态记录 {len(dropped)} 条"
            )

    def _write_through(self) -> None:
        """把全量记录写穿到 kv。失败仅告警（登记表非关键数据，不阻断运行）。"""
        try:
            self._provider.kv_put(SCOPE, self._scope_key, list(self._records.values()))
        except Exception:
            logger.warning(
                f"RunRegistry({self._scope_key}) 写穿 kv 失败（仅影响重启恢复，"
                f"当前进程内状态不受影响）",
                exc_info=True,
            )
