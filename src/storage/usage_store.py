"""
UsageStore —— LLM 用量记账（ADR-0005 D1 数据层）。

表 llm_usage（见 sqlite_provider._DDL，schema v4 起建，v5 起带详情三列）
逐次 LLM 调用记一行：
- ts / duration_ms：请求时刻与耗时（stream 为整条流耗时）
- session_id / scope / caller：业务归属。scope 由 session_log.sid_scope 推导
  （waker:/wakerflow: 前缀 → "waker"，否则 "chat"）；caller 为
  stream_invoke 的 caller_context（"main" / "employee" 等）
- tokens_in / tokens_out 允许 NULL——服务端不给 usage（或非 openai 兼容
  端点）时仍记一行（status=ok、tokens NULL），调用量本身也是信号
- status ∈ ok | error；error 存异常摘要（截断 ~200 字）
- req_messages / reasoning / output（v5+）：调用详情留底（请求消息 JSON
  序列 / 推理文本 / 最终输出文本），写入口径截断见 DETAIL 常量；v5 之前的
  存量行与未传详情的调用方落空串

写入口 record_usage 绝不抛异常（失败只 log.error）——记账是旁路设施，
任何存储故障都不能打断 LLM 业务主路径。查询/清理接口供后续看板与
StorageHousekeeping 保留期清理（180 天）使用。

模式同 projects_store / session_state_store：模块级懒加载默认库连接
（路径键控缓存），测试可 set_data_root + reset_default_provider 隔离，
或直接构造 UsageStore(provider) 注入临时实例。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger("hermes.storage.usage")

#: 看板/清理的默认窗口（天）
DEFAULT_SUMMARY_DAYS = 30
#: records 单页上限（防一次拉全表）
MAX_PAGE_SIZE = 200
#: error 摘要截断长度（与 client.py 侧写入口径一致，双保险）
ERROR_SUMMARY_MAX = 200
#: 调用详情截断上限（字符）。req_messages 存 JSON 消息序列（client 侧已剥
#: 图片 base64、逐条限长），reasoning/output 存纯文本。llm_usage 是高频表
#: 且有 180 天保留期，不设上限会让库体量失控——超限尾部以省略标记收尾。
REQ_MESSAGES_MAX = 12000
REASONING_MAX = 8000
OUTPUT_MAX = 8000
#: 截断标记（写入侧统一追加，前端原样展示）
_TRUNC_MARK = "…[已截断]"


def _clip(text: str, limit: int) -> str:
    """详情字段统一截断（尾部加标记）；None/空原样归空串。"""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNC_MARK


class UsageStore:
    """llm_usage 表的读写门面。provider = SQLiteProvider 实例（query/execute
    已是公开通用入口，模式同 projects_store.ProjectStore）。"""

    def __init__(self, provider) -> None:
        self._db = provider

    # ── 写入 ──

    def record(
        self,
        *,
        ts: float | None = None,
        session_id: str = "",
        scope: str = "",
        caller: str = "",
        model: str = "",
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        duration_ms: float | None = None,
        status: str = "ok",
        error: str = "",
        req_messages: str = "",
        reasoning: str = "",
        output: str = "",
        tools: str = "",
    ) -> None:
        """插入一行用量记录。tokens 允许 None（拿不到 usage 的调用）。

        详情列（req_messages/reasoning/output/tools）可缺省——旧调用方与
        记账失败兜底路径都不传，落空串。tools 为该次调用发起的工具名
        （逗号分隔，v6+，工具调用轮才有值）。
        """
        self._db.execute(
            "INSERT INTO llm_usage(ts, session_id, scope, caller, model, "
            "tokens_in, tokens_out, duration_ms, status, error, "
            "req_messages, reasoning, output, tools) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                float(ts) if ts is not None else time.time(),
                session_id or "",
                scope or "",
                caller or "",
                model or "",
                tokens_in,
                tokens_out,
                duration_ms,
                status or "ok",
                (error or "")[:ERROR_SUMMARY_MAX],
                _clip(req_messages, REQ_MESSAGES_MAX),
                _clip(reasoning, REASONING_MAX),
                _clip(output, OUTPUT_MAX),
                (tools or "")[:200],
            ),
        )

    # ── 查询（供后续看板；先简单实现）──

    def summary(self, days: int = DEFAULT_SUMMARY_DAYS) -> list[dict]:
        """按 本地日 × scope × model 聚合：SUM(tokens_in/tokens_out) + COUNT(*)。

        返回 [{day, scope, model, tokens_in, tokens_out, calls, errors}]，
        day 倒序。tokens 列 COALESCE 为 0（NULL 行参与计次不计 token）；
        errors 为 status='error' 的行数（D2 看板窗口合计用它汇总）。
        model 维度（2026-09-19，按日趋势堆叠/模型筛选）：旧行为按 day×scope
        累加的消费方对多出的 model 行天然兼容（互斥分组，累加无重复计数）。
        """
        cutoff = time.time() - max(1, int(days)) * 86400
        rows = self._db.query(
            "SELECT date(ts, 'unixepoch', 'localtime') AS day, scope, model, "
            "COALESCE(SUM(tokens_in), 0) AS tokens_in, "
            "COALESCE(SUM(tokens_out), 0) AS tokens_out, "
            "COUNT(*) AS calls, "
            "COALESCE(SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), 0) AS errors "
            "FROM llm_usage WHERE ts >= ? "
            "GROUP BY day, scope, model ORDER BY day DESC, scope, model",
            (cutoff,),
        )
        return [dict(r) for r in rows]

    def records(self, page: int = 1, page_size: int = 50,
                model: str = "") -> dict:
        """按 ts 倒序分页（新→旧），返回 {total, page, page_size, items}。

        model 非空时按模型过滤（2026-09-19 看板模型筛选）——COUNT 与分页
        SELECT 必须带同一 WHERE，total 才与列表一致。
        """
        page = max(1, int(page))
        page_size = min(max(1, int(page_size)), MAX_PAGE_SIZE)
        where, params = "", []
        if model:
            where = " WHERE model = ?"
            params.append(model)
        total_rows = self._db.query(
            f"SELECT COUNT(*) AS n FROM llm_usage{where}", tuple(params))
        total = int(total_rows[0]["n"]) if total_rows else 0
        rows = self._db.query(
            "SELECT id, ts, session_id, scope, caller, model, tokens_in, "
            "tokens_out, duration_ms, status, error, tools "
            f"FROM llm_usage{where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            tuple(params) + (page_size, (page - 1) * page_size),
        )
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [dict(r) for r in rows],
        }

    def detail(self, record_id: int) -> dict | None:
        """单条记录全文（含详情列与 tools/error），看板 👀 弹窗用。无则 None。"""
        rows = self._db.query(
            "SELECT id, ts, session_id, scope, caller, model, tokens_in, "
            "tokens_out, duration_ms, status, error, tools, "
            "req_messages, reasoning, output "
            "FROM llm_usage WHERE id = ?",
            (int(record_id),),
        )
        return dict(rows[0]) if rows else None

    # ── 清理 ──

    def purge_before(self, ts: float) -> int:
        """删除 ts 早于截止时间的行，返回删除行数（保留期清理用）。"""
        return int(self._db.execute("DELETE FROM llm_usage WHERE ts < ?", (float(ts),)))


# ============================================
# 模块级默认连接（懒加载、路径键控缓存）
# ============================================

_default_provider: Any = None
_default_provider_path: str | None = None
_default_lock = threading.Lock()


def _get_default_provider():
    """默认库连接（路径键控缓存）：provider 未建或数据根已切换时重建。
    模式同 projects_store._get_default_provider。"""
    global _default_provider, _default_provider_path
    from src.storage import paths

    path = str(paths.data_dir("hermes.db"))
    with _default_lock:
        if _default_provider is None or _default_provider_path != path:
            from src.storage.sqlite_provider import SQLiteProvider

            old, _default_provider = _default_provider, SQLiteProvider()
            _default_provider_path = path
            if old is not None:
                try:
                    old.close()
                except Exception:
                    logger.debug("旧默认连接关闭失败（忽略）", exc_info=True)
    return _default_provider


def reset_default_provider() -> None:
    """关闭并清空默认连接缓存（测试隔离用：set_data_root 换根后调用）。"""
    global _default_provider, _default_provider_path
    with _default_lock:
        old, _default_provider, _default_provider_path = _default_provider, None, None
        if old is not None:
            try:
                old.close()
            except Exception:
                logger.debug("默认连接关闭失败（忽略）", exc_info=True)


# ============================================
# 模块级门面（client.py / housekeeping.py 的调用入口）
# ============================================

def record_usage(provider=None, **row: Any) -> None:
    """记账唯一入口：插一行用量记录，**任何失败只 log.error 绝不抛**。

    记账是旁路设施——SQLite 故障 / 表缺失 / 连接耗尽都不能打断 LLM 业务
    主路径。双重隔离：本函数兜底 try/except + 调用方（client.py）再包一层。
    """
    try:
        p = provider if provider is not None else _get_default_provider()
        UsageStore(p).record(**row)
    except Exception:
        logger.error("LLM 用量记账失败（忽略，不影响业务）", exc_info=True)


def summary(days: int = DEFAULT_SUMMARY_DAYS, provider=None) -> list[dict]:
    return UsageStore(provider if provider is not None else _get_default_provider()).summary(days)


def records(page: int = 1, page_size: int = 50, model: str = "",
            provider=None) -> dict:
    return UsageStore(provider if provider is not None else _get_default_provider()).records(page, page_size, model)


def detail(record_id: int, provider=None) -> dict | None:
    return UsageStore(provider if provider is not None else _get_default_provider()).detail(record_id)


def purge_before(ts: float, provider=None) -> int:
    return UsageStore(provider if provider is not None else _get_default_provider()).purge_before(ts)
