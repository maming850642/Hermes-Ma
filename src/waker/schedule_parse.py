"""
============================================
调度计算 —— waker/flow 何时该跑
============================================
纯函数模块，不碰 IO、不碰线程。主进程调度线程用这些函数决定：
- compute_next_run(cfg, now) → 给定当前时间，下次该跑的时间
- is_due(cfg, now)           → cfg 里记录的 next_run_at 是否已到
- validate_schedule(cfg)     → 创建/更新时校验调度配置合法

时间一律用 naive 本地时间（datetime.now()），与项目其它处风格一致。

## Schedulable Protocol
这些函数不耦合具体类型（WakerConfig / FlowSpec+FlowState 都行），只要求
对象具备以下属性（鸭子类型）：
    enabled / schedule_type / interval_minutes / daily_at / expire_at
    max_runs / run_count / last_run_at / next_run_at
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from src.waker.models import WakerConfig, WakerConfigError, _parse_hhmm


@runtime_checkable
class Schedulable(Protocol):
    """可调度对象的结构契约（鸭子类型）。

    WakerConfig 原生满足；FlowSpec+FlowState 通过适配器组合也满足。
    函数签名用本 Protocol 而非具体类，让两类对象共用同一套调度计算。
    """
    enabled: bool
    schedule_type: str
    interval_minutes: int
    daily_at: str
    expire_at: str
    max_runs: int
    run_count: int
    last_run_at: str
    next_run_at: str


def _parse_iso(s: str) -> datetime | None:
    """解析 ISO 时间字符串。空串返回 None，非法返回 None（容错）。"""
    if not s:
        return None
    try:
        # fromisoformat 支持 "YYYY-MM-DDTHH:MM:SS" 及带微秒/偏移
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _is_expired(cfg: Schedulable, now: datetime) -> bool:
    """是否已超过 expire_at。

    expire_at 带时区（aware）时与 naive 的 now 比较抛 TypeError——调用方
    （调度 tick）逐条隔离并跳过该条，创建/更新入口由 validate_schedule
    拒绝（P1-7）。此处不做静默归一化：剥掉 tzinfo 会把另一时区的时刻
    误读成本地时刻，过期语义失真。
    """
    exp = _parse_iso(cfg.expire_at)
    if exp is None:
        return False
    return now >= exp


def _max_runs_reached(cfg: Schedulable) -> bool:
    """是否已达到 max_runs（0=不限）。"""
    return cfg.max_runs > 0 and cfg.run_count >= cfg.max_runs


def compute_next_run(cfg: Schedulable, now: datetime) -> datetime | None:
    """计算下次该运行的时间。

    规则：
    - enabled=False / schedule_type="none" / 已过期 / 已达 max_runs → None
    - interval：last_run_at 空 → now + interval；否则 last_run + interval
      （若算出的时间已过去，返回 now 表示立即该跑）
    - daily：今天 daily_at 未到且今天没跑过 → 今天 daily_at；
      否则明天 daily_at
    """
    if not cfg.enabled or cfg.schedule_type == "none":
        return None
    if _is_expired(cfg, now) or _max_runs_reached(cfg):
        return None

    if cfg.schedule_type == "interval":
        delta = timedelta(minutes=cfg.interval_minutes)
        last = _parse_iso(cfg.last_run_at)
        if last is None:
            return now + delta
        nxt = last + delta
        # 若已过去，立即跑（返回 now）
        return nxt if nxt > now else now

    if cfg.schedule_type == "daily":
        h, m = _parse_hhmm(cfg.daily_at)  # 已 validate，仍兜底解析
        today_target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        last = _parse_iso(cfg.last_run_at)
        # 今天还没到 daily_at → 今天跑
        if now < today_target:
            return today_target
        # now >= today_target：若今天还没跑过（last 早于 today_target），立即跑
        if last is None or last < today_target:
            return now
        # 今天已跑过 → 明天 daily_at
        return today_target + timedelta(days=1)

    return None


def is_due(cfg: Schedulable, now: datetime) -> bool:
    """cfg.next_run_at 非空且 now >= next_run_at → True。"""
    if not cfg.next_run_at:
        return False
    nxt = _parse_iso(cfg.next_run_at)
    if nxt is None:
        return False
    return now >= nxt


def validate_schedule(cfg: Schedulable) -> None:
    """校验调度相关字段。创建/更新时调用。

    比 WakerConfig.validate 更聚焦：只校验调度字段，并显式给出可读错误。
    """
    if cfg.schedule_type == "interval":
        if not isinstance(cfg.interval_minutes, int) or cfg.interval_minutes <= 0:
            raise WakerConfigError(
                f"interval 模式 interval_minutes 须为正整数，得到 {cfg.interval_minutes!r}"
            )
    elif cfg.schedule_type == "daily":
        # _parse_hhmm 会抛带消息的 WakerConfigError
        _parse_hhmm(cfg.daily_at)
    elif cfg.schedule_type == "none":
        pass
    else:
        raise WakerConfigError(
            f"非法 schedule_type: {cfg.schedule_type!r}（须为 interval/daily/none）"
        )
    if cfg.expire_at:
        exp = _parse_iso(cfg.expire_at)
        if exp is None:
            raise WakerConfigError(
                f"expire_at 须为合法 ISO 时间，得到 {cfg.expire_at!r}"
            )
        # P1-7：调度计算全程 naive 本地时间，aware 的 expire_at 会让
        # _is_expired 的 now >= exp 抛 TypeError，进而停摆其后所有任务。
        # 带时区的解析结果在入口直接判错（不做静默归一化——剥离 tzinfo
        # 会把"另一时区的时刻"误读成本地时刻）。
        if exp.tzinfo is not None:
            raise WakerConfigError(
                f"expire_at 须为不带时区的本地 ISO 时间（如 2026-01-01T00:00:00），"
                f"得到带时区的 {cfg.expire_at!r}"
            )
