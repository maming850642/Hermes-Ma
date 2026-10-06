"""Waker（数字员工）管理 API。

与 mcp/config 路由的区别：waker 的配置直接落在主进程文件系统
（WakerStore 做 per-user 文件 IO），不走 worker IPC——CRUD 更简单、更快。
只有 invoke 需要 scheduler（scheduler 内部再通过 worker_manager 把任务
派发给对应 user 的 worker 子进程）。

非路由的纯逻辑 / IO 辅助（掩码、jsonl 扫描、请求体转换、run 状态修正）
下沉在 web_fastapi/services/waker_service.py，本文件只留路由与薄封装。
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from src.waker.models import WakerConfigError
from src.waker.schedule_parse import validate_schedule
from src.waker.store import WakerStore
from web_fastapi.dependencies import get_current_user_id
from web_fastapi.models import (
    EnabledBody,
    WakerCreateBody,
    WakerInvokeBody,
    WakerUpdateBody,
)
from web_fastapi.services.waker_service import (
    active_run_ids,
    active_runs_by_name,
    apply_live_status,
    apply_update_body,
    cfg_from_create_body,
    detail_dict,
    masked_dict,
    persist_manual_run_state,
    read_latest_result,
    scan_jsonl,
)

router = APIRouter()


# ============================================
# 辅助（Request/app.state 取用，留在路由侧）
# ============================================
def _store(request: Request, user_id: str) -> WakerStore:
    """构造 WakerStore，workspace_root 与 scheduler 一致。

    优先用 app.state.waker_scheduler._workspace_root（若 scheduler 已挂载），
    其次 app.state.workspace_root（测试在拆掉 scheduler 后仍要隔离目录），
    再回退到空串（WakerStore 内部再解析 settings.workspace_root）。
    """
    scheduler = getattr(request.app.state, "waker_scheduler", None)
    ws = getattr(scheduler, "_workspace_root", "") if scheduler else ""
    if not ws:
        ws = getattr(request.app.state, "workspace_root", "") or ""
    return WakerStore(user_id, workspace_root=ws)


def _runner(request: Request):
    """获取 app.state.waker_async_runner（fork 子进程的异步运行器）。

    未挂载返回 None：runs 查询按无活跃 run 处理；invoke 走
    scheduler.submit_now 回退路径（见下）。
    """
    return getattr(request.app.state, "waker_async_runner", None)


# 逾期容差：手动 invoke 只推进 run_count/last_run_at、不刷新 next_run_at
# （见 waker_service.persist_manual_run_state），留 120s 消化"到点前后
# next_run_at 仍是旧值"的窗口，避免手动跑完立刻误报调度延迟。
OVERDUE_TOLERANCE_S = 120


def _is_overdue(w: dict, active_run_id: str) -> bool:
    """调度延迟判定：已启用、next_run_at 已过（含容差）且当前不在运行。

    next_run_at 为 naive 本地时间 ISO 串（scheduler 写入），全程 naive
    比较、不做时区换算；解析/比较失败（坏串、aware 串混比等）一律 False。
    """
    if not w.get("enabled"):
        return False
    if active_run_id or w.get("last_status") == "running":
        return False
    nxt = w.get("next_run_at") or ""
    if not nxt:
        return False
    try:
        next_dt = datetime.fromisoformat(nxt)
        return datetime.now() > next_dt + timedelta(seconds=OVERDUE_TOLERANCE_S)
    except (TypeError, ValueError):
        return False


# ============================================
# CRUD
# ============================================
@router.get("/items")
async def list_items(request: Request,
                     user_id: str = Depends(get_current_user_id)):
    """列出当前用户的所有 waker。api_token 掩码。

    附 active_run_id：正在跑的 waker（从 WakerAsyncRunner 内存表查），
    让前端卡片显示运行中状态。
    """
    store = _store(request, user_id)
    wakers = [masked_dict(c) for c in store.list()]
    # 查活跃 run（async_runner 内存表），标记 running
    active = active_runs_by_name(_runner(request), user_id)
    for w in wakers:
        name = w.get("name", "")
        rid = active.get(name, "")
        if rid:
            w["active_run_id"] = rid
            w["last_status"] = "running"
        else:
            w["active_run_id"] = ""
        w["overdue"] = _is_overdue(w, rid)
    return {"wakers": wakers}


@router.get("/items/{name}")
async def get_item(name: str, request: Request,
                   user_id: str = Depends(get_current_user_id)):
    """取单个 waker 详情。api_token 掩码；附 identity/persona/bible 文本。"""
    store = _store(request, user_id)
    cfg = store.get(name)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"waker 不存在: {name}")
    return {"waker": detail_dict(store, cfg)}


@router.post("/items")
async def create_item(body: WakerCreateBody, request: Request,
                      user_id: str = Depends(get_current_user_id)):
    """新建 waker。返回明文 api_token（仅此一次）。

    name 冲突/非法 → 400。
    """
    store = _store(request, user_id)
    try:
        cfg = cfg_from_create_body(body)

        # P1-7：创建路径接线 validate_schedule（带时区的 expire_at 等在此
        # 400 拒绝，不再流入调度器后炸 tick、停摆其后所有任务；
        # store.create 内部亦校验，走同一 WakerConfigError 错误通道）
        validate_schedule(cfg)

        store.create(
            cfg,
            identity=body.identity or "",
            persona=body.persona or "",
            bible=body.bible or "",
        )
    except WakerConfigError as e:
        # 来自 __post_init__ / setattr / validate
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        # name 冲突
        raise HTTPException(status_code=400, detail=str(e))

    # 重读以拿到生成出的 api_token + 默认值
    created = store.get(body.name)
    d = detail_dict(store, created)
    # 创建响应返回明文 api_token（仅此一次）
    d["api_token"] = created.api_token
    return {"ok": True, "waker": d}


@router.put("/items/{name}")
async def update_item(name: str, body: WakerUpdateBody, request: Request,
                      user_id: str = Depends(get_current_user_id)):
    """更新 waker 配置 + 人格三文件。全字段可选。name 不存在 → 404。"""
    store = _store(request, user_id)
    cfg = store.get(name)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"waker 不存在: {name}")

    apply_update_body(cfg, body)
    try:
        # P2-25：PUT 此前只改值不校验——schedule_type:"weekly"/
        # interval_minutes:0 会静默落盘，该 waker 从此 compute_next_run
        # 返回 None 永不调度且无任何反馈。补全字段校验；
        # P1-7：调度字段再过 validate_schedule（含带时区 expire_at 的拒绝）。
        cfg.validate()
        validate_schedule(cfg)
        store.update(cfg)
    except WakerConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # 人格三文件（None 不改）
    if body.identity is not None or body.persona is not None or body.bible is not None:
        store.update_persona(
            name,
            identity=body.identity,
            persona=body.persona,
            bible=body.bible,
        )
    return {"ok": True}


@router.patch("/items/{name}/enabled")
async def set_enabled(name: str, body: EnabledBody, request: Request,
                      user_id: str = Depends(get_current_user_id)):
    """启用/禁用 waker。"""
    store = _store(request, user_id)
    try:
        store.set_enabled(name, body.enabled)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"waker 不存在: {name}")
    return {"ok": True}


@router.delete("/items/{name}")
async def delete_item(name: str, request: Request,
                      user_id: str = Depends(get_current_user_id)):
    """删除 waker。不存在 → 404。"""
    store = _store(request, user_id)
    if not store.delete(name):
        raise HTTPException(status_code=404, detail=f"waker 不存在: {name}")
    return {"ok": True}


# ============================================
# 运行记录 / 结果
# ============================================
@router.get("/items/{name}/runs")
async def get_runs(name: str, request: Request, limit: int = 50,
                   user_id: str = Depends(get_current_user_id)):
    """列出该 waker 的历史运行（每条 jsonl 一行摘要，mtime 新→旧）。

    每项含 run_id / started_at / ended_at / status / duration_s / error。
    status：ok / error / running / interrupted（无 run_end 且不在跑则为中断）。
    无 run → {"runs": []}。
    """
    store = _store(request, user_id)
    if store.get(name) is None:
        raise HTTPException(status_code=404, detail=f"waker 不存在: {name}")

    try:
        limit_i = int(limit)
    except (TypeError, ValueError):
        limit_i = 50
    limit_i = max(1, min(limit_i, 200))

    run_dir = store.run_dir(name)
    if not run_dir.is_dir():
        return {"runs": []}

    jsonls = sorted(
        (p for p in run_dir.iterdir() if p.is_file() and p.suffix == ".jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit_i]
    active = active_run_ids(_runner(request), user_id)
    runs = [apply_live_status(scan_jsonl(p, collect_detail=False), active) for p in jsonls]
    return {"runs": runs}


@router.get("/items/{name}/runs/{run_id}")
async def get_run_detail(name: str, run_id: str, request: Request,
                         user_id: str = Depends(get_current_user_id)):
    """读单次 run 的摘要 + 交付结果 + 关键事件（不含 token 流）。

    结果取 jsonl 里最后一条 complete.content；事件只保留工具/起止/错误等。
    卡片进度轮询也走这里（token_count / tool_calls / status）。
    """
    store = _store(request, user_id)
    if store.get(name) is None:
        raise HTTPException(status_code=404, detail=f"waker 不存在: {name}")
    try:
        path = store.run_jsonl_path(name, run_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"非法 run_id: {run_id!r}")
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"run 不存在: {run_id}")
    rec = scan_jsonl(path, collect_detail=True)
    return apply_live_status(rec, active_run_ids(_runner(request), user_id))


@router.get("/items/{name}/result")
async def get_result(name: str, request: Request,
                     user_id: str = Depends(get_current_user_id)):
    """读 latest_result.md。不存在返回空串。"""
    store = _store(request, user_id)
    if store.get(name) is None:
        raise HTTPException(status_code=404, detail=f"waker 不存在: {name}")
    return {"result": read_latest_result(store, name)}


# ============================================
# 手动触发
# ============================================
@router.post("/items/{name}/invoke")
async def invoke_item(name: str, body: WakerInvokeBody, request: Request,
                      user_id: str = Depends(get_current_user_id)):
    """手动触发一次 waker 运行。立即返回 run_id，不等执行完。

    优先走 app.state.waker_async_runner（fork 子进程，不抢 worker 锁）。
    若 async_runner 未挂载，回退 scheduler.submit_now（旧路径，需 waker.enabled=true）。
    两者都不可用 → 503。
    """
    store = _store(request, user_id)
    if store.get(name) is None:
        raise HTTPException(status_code=404, detail=f"waker 不存在: {name}")
    run_id = store.new_run_id()

    # 优先 async_runner（推荐路径，不抢 chat 锁）
    async_runner = _runner(request)
    if async_runner is not None:
        try:
            # submit 异步立即返回；不阻塞 HTTP。状态查 runs/latest_result。
            async_runner.submit(user_id, name, run_id, body.prompt)
        except ValueError:
            raise HTTPException(status_code=400, detail="task_prompt 为空")
        except RuntimeError:
            raise HTTPException(status_code=503, detail="waker 异步运行器未启动")
        # 手动运行即时落库（提交成功即推进 run_count / last_run_at，
        # 缘由见 waker_service.persist_manual_run_state）
        persist_manual_run_state(store, user_id, name)
        return {"ok": True, "run_id": run_id}

    # 回退：scheduler.submit_now（旧路径，会与 chat 抢锁）
    scheduler = getattr(request.app.state, "waker_scheduler", None)
    if scheduler is None:
        raise HTTPException(
            status_code=503,
            detail="waker 运行器未启用（waker.enabled=false 且 async_runner 未挂载）",
        )
    try:
        run_id = scheduler.submit_now(user_id, name, body.prompt)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"waker 不存在: {name}")
    return {"ok": True, "run_id": run_id}
