"""LLM 用量看板查询（ADR-0005 D2）：GET /api/usage/summary + /api/usage/records
+ /api/usage/records/{id}/detail。

只读门面：聚合走 UsageStore.summary（本地日 × scope，含 error 计数），
明细走 UsageStore.records（ts 倒序分页，带 id 供 👀 详情拉取）；
单条详情走 UsageStore.detail（请求消息/推理/最终输出全文）。days 只放行
1/7/30 三档，其余一律回落 7。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from web_fastapi.dependencies import get_current_user_id

logger = logging.getLogger("hermes.web.usage")
router = APIRouter()

#: 看板时间窗白名单：今天 / 近 7 天 / 近 30 天，其余 clamp 到 7
ALLOWED_DAYS = (1, 7, 30)
#: records 单页大小 clamp 范围（store 层还有 200 的硬顶，这里按 UI 口径收紧）
MAX_PAGE_SIZE = 100
#: 详情单字段响应钳制（写入口已有截断，这里是防御性双保险）
DETAIL_FIELD_CLAMP = 65536


def _usage_store(request: Request):
    """取库姿势同 runs.py：cordis_ctx 优先，回退默认 provider。"""
    ctx = getattr(request.app.state, "cordis_ctx", None)
    storage = None
    if ctx is not None:
        try:
            storage = ctx.try_get("storage")
        except Exception:
            storage = None
    if storage is None:
        from src.storage.sqlite_provider import SQLiteProvider
        storage = SQLiteProvider()
    from src.storage.usage_store import UsageStore
    return UsageStore(storage)


def _clamp_days(raw: str | None) -> int:
    """days 参数只认 1/7/30；缺失/非数字/越界一律回落 7。"""
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return 7
    return days if days in ALLOWED_DAYS else 7


@router.get("/summary")
async def usage_summary(request: Request, days: str = "7", model: str = "",
                        user_id: str = Depends(get_current_user_id)):
    """窗口内按日 × scope × model 聚合 + 窗口合计（tokens/calls/errors）。

    合计直接由聚合行汇总得出（每行都在窗口内，无需二次查询）。
    model 维度（2026-09-19 按日趋势堆叠/模型筛选）：响应恒带 models 列表
    （**未过滤**，供前端筛选下拉；天然跟随时间窗联动），model 参数非空时
    rows/totals 只含该模型。
    """
    store = _usage_store(request)
    d = _clamp_days(days)
    rows_all = store.summary(days=d)
    models = sorted({r.get("model") for r in rows_all if r.get("model")})
    rows = ([r for r in rows_all if r.get("model") == model]
            if model else rows_all)
    totals = {
        "tokens_in": sum(int(r.get("tokens_in") or 0) for r in rows),
        "tokens_out": sum(int(r.get("tokens_out") or 0) for r in rows),
        "calls": sum(int(r.get("calls") or 0) for r in rows),
        "errors": sum(int(r.get("errors") or 0) for r in rows),
    }
    return {"days": d, "totals": totals, "rows": rows, "models": models}


@router.get("/records")
async def usage_records(request: Request, page: int = 1, page_size: int = 20,
                        model: str = "",
                        user_id: str = Depends(get_current_user_id)):
    """明细分页（新→旧）。行带 id（👀 详情按它取全文），不带大字段。

    model 非空时按模型过滤（total 与列表同 WHERE）。
    """
    store = _usage_store(request)
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), MAX_PAGE_SIZE)
    data = store.records(page=page, page_size=page_size, model=model)
    fields = ("id", "ts", "session_id", "scope", "caller", "model", "tools",
              "tokens_in", "tokens_out", "duration_ms", "status")
    records = [
        {k: item.get(k) for k in fields}
        for item in data.get("items", [])
    ]
    return {
        "records": records,
        "total": int(data.get("total") or 0),
        "page": data.get("page", page),
        "page_size": data.get("page_size", page_size),
    }


@router.get("/records/{record_id}/detail")
async def usage_record_detail(record_id: int, request: Request,
                              user_id: str = Depends(get_current_user_id)):
    """单次调用详情（👀 弹窗）：请求消息 JSON / 推理 / 最终输出 + 元数据。

    v5 之前的存量行三列为空串，前端按「—」展示。
    """
    store = _usage_store(request)
    item = store.detail(record_id)
    if item is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    for k in ("req_messages", "reasoning", "output"):
        item[k] = (item.get(k) or "")[:DETAIL_FIELD_CLAMP]
    # 是否有详情留底：v5 上线（2026-09-19）前的旧行三列恒空串——前端据此
    # 区分「旧数据无留底」与「工具调用轮无文本输出」两种空 output
    item["has_detail"] = bool(item.get("req_messages")
                              or item.get("reasoning") or item.get("output"))
    return {"record": item}
