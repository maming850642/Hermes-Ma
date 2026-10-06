"""WakerFlow（编排）管理 API。

与 waker 路由同构：
- CRUD 直接主进程文件 IO（FlowStore），不走 worker IPC——简单、快。
- invoke 走主进程 FlowRunner（线程池）；审批经 /approvals 端点直写审批文件。

yaml 校验：parse_flow 抛 FlowParseError → 400 + detail。
runs / approvals 扫描：读 FlowStore 给出的目录里的 jsonl / json。

非路由的纯逻辑 / IO 辅助（摘要、jsonl 扫描、请求体转换、审批上下文）
下沉在 web_fastapi/services/flow_service.py，本文件只留路由与薄封装。
"""
import json
import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request

from src.wakerflow.canvas import CanvasError, flow_to_blocks, flow_to_yaml
from src.wakerflow.parser import FlowParseError, parse_flow
from src.wakerflow.store import FlowStore
from web_fastapi.dependencies import get_current_user_id
from web_fastapi.models import (
    EnabledBody,
    FlowApproveBody,
    FlowCreateBody,
    FlowInvokeBody,
    FlowUpdateBody,
)
from web_fastapi.services.flow_service import (
    active_run_ids,
    apply_live_status,
    body_to_spec,
    body_to_yaml,
    flow_summary,
    latest_run,
    read_approval_context,
    scan_flow_jsonl,
)

logger = logging.getLogger("hermes.web.wakerflow")
router = APIRouter()


# ============================================
# 辅助
# ============================================
def _store(request: Request, user_id: str) -> FlowStore:
    """构造 FlowStore，workspace_root 与 scheduler 一致（同 waker 路由）。

    优先用 app.state.waker_scheduler._workspace_root（若 scheduler 已挂载），
    回退到空串（FlowStore 内部再解析 settings.workspace_root）。
    """
    scheduler = getattr(request.app.state, "waker_scheduler", None)
    ws = getattr(scheduler, "_workspace_root", "") if scheduler else ""
    return FlowStore(user_id, workspace_root=ws)


def _runner(request: Request):
    """获取 app.state.flow_runner（主进程 FlowRunner 单例）。

    flow 运行在主进程线程池，**不走 worker IPC**，因此不阻塞用户 chat。
    若 runner 未挂载（启动失败）返回 None，调用方转 503。
    """
    return getattr(request.app.state, "flow_runner", None)


# ============================================
# CRUD
# ============================================
@router.get("/items")
async def list_items(request: Request,
                     user_id: str = Depends(get_current_user_id)):
    """列出当前用户的所有 WakerFlow。

    每项含 name / description / steps_count（从 yaml 解析）+ last_run / last_status
    （从 runs/ 目录最新 jsonl 读，无则空）。
    """
    store = _store(request, user_id)
    # 查活跃 flow（正在跑的），让卡片显示运行中状态
    runner = _runner(request)
    active_runs: dict = {}  # flow_name → run_id
    if runner is not None:
        for rec in runner.list_active(user_id):
            fn = rec.get("flow_name", "")
            if fn and fn not in active_runs:
                active_runs[fn] = rec.get("run_id", "")
    out = []
    for name, yaml_text in store.list():
        summary = flow_summary(name, yaml_text)
        run_id, status = latest_run(store, name)
        summary["last_run"] = run_id
        summary["last_status"] = status
        # 调度信息（enabled/schedule_type/next_run_at 供卡片显示）
        try:
            spec = parse_flow(yaml_text)
            summary["enabled"] = spec.enabled
            summary["schedule_type"] = spec.schedule_type
            summary["steps_total"] = len(spec.step_ids())
            st = store.load_state(name)
            summary["next_run_at"] = st.next_run_at
            summary["run_count"] = st.run_count
        except FlowParseError:
            summary["enabled"] = True
            summary["schedule_type"] = "none"
            summary["steps_total"] = 0
            summary["next_run_at"] = ""
            summary["run_count"] = 0
        # 活跃状态：正在跑则覆盖为 running
        if name in active_runs:
            summary["active_run_id"] = active_runs[name]
            summary["last_status"] = "running"
        else:
            summary["active_run_id"] = ""
        out.append(summary)
    return {"flows": out}


@router.get("/items/{name}")
async def get_item(name: str, request: Request,
                   user_id: str = Depends(get_current_user_id)):
    """取单个 flow 详情：原始 yaml 文本 + 解析出的 description / steps_count
    + 积木块 JSON（供拖拽编辑器加载）。

    yaml 解析失败也返回（前端展示原文本让用户修），description 带错误提示。
    blocks 解析失败返回空列表（前端回退到 YAML 编辑）。
    """
    store = _store(request, user_id)
    yaml_text = store.get(name)
    if yaml_text is None:
        raise HTTPException(status_code=404, detail=f"flow 不存在: {name}")
    summary = flow_summary(name, yaml_text)
    # 附带积木块 JSON（canvas 转换层），供拖拽编辑器加载
    blocks: list[dict] = []
    inputs_json: list[dict] = []
    returns_json: dict = {}
    schedule_json: dict = {}
    state_json: dict = {}
    try:
        spec = parse_flow(yaml_text)
        blocks = flow_to_blocks(spec)
        inputs_json = [
            {"name": f.name, "type": f.type, "required": f.required,
             "default": f.default, "enum": f.enum}
            for f in spec.inputs
        ]
        returns_json = dict(spec.returns)
        schedule_json = {
            "enabled": spec.enabled,
            "schedule_type": spec.schedule_type,
            "interval_minutes": spec.interval_minutes,
            "daily_at": spec.daily_at,
            "api_enabled": spec.api_enabled,
            "api_token": spec.api_token,  # 详情接口给明文（创建时已展示过）
            "max_runs": spec.max_runs,
            "expire_at": spec.expire_at,
        }
    except FlowParseError:
        pass  # blocks 留空，前端回退 YAML
    # 运行时状态（next_run_at 等供前端卡片/编辑器显示）
    store_tmp = _store(request, user_id)
    try:
        st = store_tmp.load_state(name)
        state_json = {
            "run_count": st.run_count,
            "last_run_at": st.last_run_at,
            "last_status": st.last_status,
            "next_run_at": st.next_run_at,
        }
    except Exception:
        pass
    return {"flow": {**summary, "yaml": yaml_text, "blocks": blocks,
                     "inputs": inputs_json, "returns": returns_json,
                     "schedule": schedule_json, "state": state_json}}


@router.post("/preview")
async def preview_item(body: FlowCreateBody, request: Request,
                       user_id: str = Depends(get_current_user_id)):
    """blocks⇄yaml 非持久化转换（W4：修复导入/导出的 __tmp__ 死路）。

    - body.blocks 给定 → 校验并返回规范化 yaml + blocks（导出用）
    - body.yaml 给定 → 解析并返回 blocks（导入用）
    不落盘、不需要合法的持久名（name 仅作缺省名）。
    """
    try:
        spec = body_to_spec(body)
    except (FlowParseError, CanvasError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "ok": True,
        "yaml": flow_to_yaml(spec),
        "blocks": flow_to_blocks(spec),
        "name": spec.name,
        "description": spec.description,
        "inputs": [asdict(f) for f in (spec.inputs or [])],
        "schedule": {
            "enabled": spec.enabled, "schedule_type": spec.schedule_type,
            "interval_minutes": spec.interval_minutes, "daily_at": spec.daily_at,
            "api_enabled": spec.api_enabled,
        },
    }


@router.post("/items")
async def create_item(body: FlowCreateBody, request: Request,
                      user_id: str = Depends(get_current_user_id)):
    """新建 flow。支持两种方式：
    - yaml 文本（parser 校验）
    - blocks JSON（canvas 转换层 → flow_to_yaml）

    校验失败 / name 非法 → 400 + detail。
    """
    store = _store(request, user_id)
    try:
        yaml_text = body_to_yaml(body)
        store.save(body.name, yaml_text)
    except (FlowParseError, CanvasError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        # name 非法（_ 开头 / 路径穿越等，FlowStore.save 抛）或 body 缺字段
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@router.put("/items/{name}")
async def update_item(name: str, body: FlowUpdateBody, request: Request,
                      user_id: str = Depends(get_current_user_id)):
    """更新 flow。支持 yaml 文本或 blocks JSON。

    blocks 模式时 name 取路径参数（body.name 不用）。
    不存在 → 404。
    """
    store = _store(request, user_id)
    if store.get(name) is None:
        raise HTTPException(status_code=404, detail=f"flow 不存在: {name}")
    try:
        # blocks 模式需把 name 注入 body（body_to_yaml 读 body.name）
        if body.blocks is not None and not getattr(body, "name", None):
            body.name = name
        yaml_text = body_to_yaml(body)
        store.save(name, yaml_text)
    except (FlowParseError, CanvasError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@router.delete("/items/{name}")
async def delete_item(name: str, request: Request,
                      user_id: str = Depends(get_current_user_id)):
    """删除 flow（含 runs + state.json）。不存在 → 404。"""
    store = _store(request, user_id)
    try:
        if not store.delete(name):
            raise HTTPException(status_code=404, detail=f"flow 不存在: {name}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@router.patch("/items/{name}/enabled")
async def set_enabled(name: str, body: EnabledBody, request: Request,
                      user_id: str = Depends(get_current_user_id)):
    """启用/禁用 flow 调度。

    实现方式：读 flow.yaml → parse → 改 spec.enabled → flow_to_yaml 覆盖存盘。
    不动 state.json（运行历史保留）。
    """
    store = _store(request, user_id)
    yaml_text = store.get(name)
    if yaml_text is None:
        raise HTTPException(status_code=404, detail=f"flow 不存在: {name}")
    try:
        spec = parse_flow(yaml_text)
        spec.enabled = body.enabled
        store.save(name, flow_to_yaml(spec))
    except FlowParseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "enabled": body.enabled}


@router.post("/items/{name}/trigger")
async def trigger_item(name: str, request: Request):
    """API token 触发 flow 运行（外部系统调用，无 cookie 也能用）。

    鉴权：header `X-Flow-Token` 须匹配 flow 的 api_token。
    （注意：本端点**不挂 cookie 依赖**——外部调用方没有登录态，
    X-Flow-Token 才是鉴权门。）
    flow 的 api_enabled 须为 True，否则 403。
    立即返回 run_id（异步运行）。
    """
    from src.constants import LOCAL_USER
    user_id = LOCAL_USER
    try:
        store = _store(request, user_id)
        yaml_text = store.get(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if yaml_text is None:
        raise HTTPException(status_code=404, detail=f"flow 不存在: {name}")
    try:
        spec = parse_flow(yaml_text)
    except FlowParseError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 鉴权
    if not spec.api_enabled:
        raise HTTPException(status_code=403, detail="该 flow 未启用 API 触发（api_enabled=false）")
    token = request.headers.get("X-Flow-Token", "")
    if not token or not spec.api_token or token != spec.api_token:
        raise HTTPException(status_code=401, detail="X-Flow-Token 无效")

    runner = _runner(request)
    if runner is None:
        raise HTTPException(status_code=503, detail="WakerFlow 运行器未启动")
    try:
        run_id = runner.submit(user_id, name, {})
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FlowParseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "run_id": run_id, "status": "running"}


# ============================================
# 运行记录
# ============================================
@router.get("/items/{name}/runs")
async def get_runs(name: str, request: Request, limit: int = 50,
                   user_id: str = Depends(get_current_user_id)):
    """列出该 flow 的历史运行（每条 jsonl 一行摘要，mtime 新→旧）。

    status：completed / failed / running / interrupted。
    无 run → {"runs": []}。
    """
    store = _store(request, user_id)
    if store.get(name) is None:
        raise HTTPException(status_code=404, detail=f"flow 不存在: {name}")

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
    runs = [apply_live_status(scan_flow_jsonl(p, collect_detail=False), active) for p in jsonls]
    return {"runs": runs}


@router.get("/items/{name}/runs/{run_id}")
async def get_run_detail(name: str, run_id: str, request: Request,
                         user_id: str = Depends(get_current_user_id)):
    """单次 flow run：摘要 + returns/节点产出 + 编排级事件（不含 worker token 流）。"""
    store = _store(request, user_id)
    if store.get(name) is None:
        raise HTTPException(status_code=404, detail=f"flow 不存在: {name}")
    try:
        path = store.run_jsonl_path(name, run_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"非法 run_id: {run_id!r}")
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"run 不存在: {run_id}")
    rec = scan_flow_jsonl(path, collect_detail=True)
    return apply_live_status(rec, active_run_ids(_runner(request), user_id))


@router.get("/items/{name}/result")
async def get_result(name: str, request: Request,
                     user_id: str = Depends(get_current_user_id)):
    """读 flow 最近一次 run 的最终结果（returns + 关键节点产出）。

    轻量端点：只提取 flow_end 的 returns + 各 worker 节点的 result 文本，
    不返回原始事件流（那个用 /runs 看）。

    Returns:
        {run_id, status, returns: {...}, nodes: [{node_id, status, result}]}
        无 run → {run_id: null, status: "", returns: {}, nodes: []}
    """
    store = _store(request, user_id)
    if store.get(name) is None:
        raise HTTPException(status_code=404, detail=f"flow 不存在: {name}")

    run_dir = store.run_dir(name)
    if not run_dir.is_dir():
        return {"run_id": None, "status": "", "returns": {}, "nodes": []}

    jsonls = sorted(
        (p for p in run_dir.iterdir() if p.is_file() and p.suffix == ".jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not jsonls:
        return {"run_id": None, "status": "", "returns": {}, "nodes": []}

    latest = jsonls[0]
    run_id = latest.stem
    status = ""
    returns: dict = {}
    nodes: list = []
    try:
        with latest.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                etype = ev.get("type", "")
                if etype == "flow_end":
                    status = ev.get("status", status)
                    # executor 的 flow_end 带 returns（新版）；旧版没有则留空
                    ret = ev.get("returns")
                    if isinstance(ret, dict) and ret:
                        returns = ret
                elif etype == "node_result":
                    nid = ev.get("node_id", "")
                    nstatus = ev.get("status", "")
                    r = ev.get("result", {})
                    rtext = ""
                    has_sub = False
                    if isinstance(r, dict):
                        # worker 节点有 result，ask_user 有 answer
                        rtext = r.get("result", "") or r.get("answer", "") or ""
                        has_sub = bool(r.get("sub_results"))
                    # 只收 worker/ask_user 节点（有实质产出），跳过 parallel/pipeline 容器（has_sub）
                    if rtext and not has_sub:
                        nodes.append({
                            "node_id": nid, "status": nstatus,
                            "result": str(rtext)[:5000],  # 截断防止过大
                        })
    except OSError:
        return {"run_id": run_id, "status": status, "returns": returns, "nodes": nodes}

    # 若 flow_end 没有 returns（旧 jsonl），从 nodes 兜底：用同名节点结果填
    if not returns and nodes:
        for n in nodes:
            returns[n["node_id"]] = n["result"]

    return {"run_id": run_id, "status": status, "returns": returns, "nodes": nodes}


# ============================================
# 手动触发（主进程后台线程，不占 worker IPC）
# ============================================
@router.post("/items/{name}/invoke")
async def invoke_item(name: str, body: FlowInvokeBody, request: Request,
                      user_id: str = Depends(get_current_user_id)):
    """异步触发一次 flow 运行（主进程线程池，立即返回 run_id）。

    flow 在后台跑，**不阻塞用户 chat**（与 worker IPC 完全解耦）。
    前端拿 run_id 轮询 GET /items/{name}/runs/{run_id}/status 查进度。

    Returns:
        {ok, run_id, status:"running"}（已提交）
        或 flow 不存在 404 / 解析失败 400 / runner 未启动 503
    """
    store = _store(request, user_id)
    if store.get(name) is None:
        raise HTTPException(status_code=404, detail=f"flow 不存在: {name}")

    runner = _runner(request)
    if runner is None:
        raise HTTPException(status_code=503, detail="WakerFlow 运行器未启动")

    try:
        run_id = runner.submit(user_id, name, body.inputs or {})
    except FlowParseError as e:
        # flow 定义解析失败（FlowParseError 继承 ValueError，须先于 ValueError 捕获）
        raise HTTPException(status_code=400, detail=f"flow 定义解析失败: {e}")
    except ValueError as e:
        # flow 不存在（已被删）
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    return {"ok": True, "run_id": run_id, "status": "running"}


@router.get("/items/{name}/runs/{run_id}/status")
async def get_run_status(name: str, run_id: str, request: Request,
                         user_id: str = Depends(get_current_user_id)):
    """查某次 run 的实时状态 + 节点进度（前端轮询用）。

    返回:
        {run_id, flow_name, status, current_node, completed_nodes, total_nodes,
         pending_approval, error}

    - status: pending/running/completed/failed/error/unknown
    - current_node: 当前正在跑的节点 id（从 jsonl 最后一个 node_start 读）
    - completed_nodes/total_nodes: 进度（如 2/3）
    - pending_approval: 是否卡在 ask_user 审批
    - error: 失败原因
    """
    runner = _runner(request)
    base_status = "unknown"
    error = ""
    # 1. 从 FlowRunner 内存表取整体状态
    if runner is not None:
        rec = runner.get_status(run_id)
        if rec is not None:
            base_status = rec.get("status", "unknown")
            error = rec.get("error", "")

    # 2. 从 jsonl 取节点进度（无论内存表有没有，jsonl 都是最详尽的）
    store = _store(request, user_id)
    current_node = ""
    completed_nodes = 0
    total_nodes = 0
    pending_approval = False
    try:
        # P3-4：run_id 过 store 层 _safe_component（防 %5C 反斜杠在 Windows
        # 穿越出 runs 目录），与写侧 run_jsonl_path 同一构造入口
        jp = store.run_jsonl_path(name, run_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"非法 run_id: {run_id!r}")
    if jp.exists():
        # 先数总节点数（从 flow 定义）
        try:
            yaml_text = store.get(name)
            if yaml_text:
                spec = parse_flow(yaml_text)
                total_nodes = len(spec.step_ids())
        except Exception:
            pass
        # 扫 jsonl 统计进度
        started: set = set()
        ended_ok: set = set()
        try:
            with jp.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    etype = ev.get("type", "")
                    nid = ev.get("node_id", "")
                    if etype == "node_start" and nid:
                        started.add(nid)
                        current_node = nid
                    elif etype == "node_end" and nid:
                        ended_ok.add(nid)
                        if nid == current_node:
                            current_node = ""  # 这个节点跑完了
                    elif etype == "approval_required":
                        pending_approval = True
                    elif etype == "flow_end":
                        base_status = ev.get("status", base_status)
            completed_nodes = len(ended_ok)
        except OSError:
            pass

    return {
        "run_id": run_id, "flow_name": name, "status": base_status,
        "current_node": current_node,
        "completed_nodes": completed_nodes,
        "total_nodes": total_nodes,
        "pending_approval": pending_approval,
        "error": error,
    }


# ============================================
# 审批
# ============================================
@router.get("/approvals")
async def list_approvals(request: Request,
                         user_id: str = Depends(get_current_user_id)):
    """列出当前用户所有 pending 审批。

    扫 FlowStore.approvals_dir() 下所有 *.json，过滤 status=="pending"。
    每个 approval 附带 context：审批前各节点的结果摘要（从 run jsonl 读），
    让用户审批时有上下文（知道前面跑出了什么才决定是否批准）。

    Returns:
        {approvals: [{run_id, flow_name, node_id, question, options, created_ts, context}]}
        context: [{node_id, status, result(截断)}]
    """
    store = _store(request, user_id)
    adir = store.approvals_dir()
    out = []
    if not adir.is_dir():
        return {"approvals": out}
    for jp in sorted(adir.glob("*.json")):
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("status") != "pending":
            continue
        # 读对应 run 的 jsonl，提取审批前的 node_result 作为上下文
        flow_name = data.get("flow_name", "")
        run_id = data.get("run_id", "")
        node_id = data.get("node_id", "")
        context = read_approval_context(store, flow_name, run_id, node_id)
        out.append({
            "run_id": run_id,
            "flow_name": flow_name,
            "node_id": node_id,
            "question": data.get("question", ""),
            "options": data.get("options", []),
            "created_ts": data.get("created_ts", ""),
            "context": context,
        })
    return {"approvals": out}


@router.post("/approvals/{run_id}")
async def approve_item(run_id: str, body: FlowApproveBody, request: Request,
                       user_id: str = Depends(get_current_user_id)):
    """提交审批响应（主进程直接写文件，不走 worker IPC）。

    FlowRunner 的 executor 在主进程线程里轮询读 approval 文件，
    status 变 answered 后继续。所以这里直接写文件即可。
    """
    runner = _runner(request)
    if runner is None:
        raise HTTPException(status_code=503, detail="WakerFlow 运行器未启动")
    ok = runner.approve(user_id, run_id, body.answer, answered_by=user_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"无 pending 审批: {run_id}")
    return {"ok": True, "run_id": run_id, "answer": body.answer}


@router.post("/approvals/{run_id}/cancel")
async def cancel_approval(run_id: str, request: Request,
                          user_id: str = Depends(get_current_user_id)):
    """取消挂起中的审批（主进程直接把审批文件写为 cancelled）。

    看护线程读到 cancelled 提前终止等待 → flow 以 failed 终态收尾。
    误触发的 flow 不必干等 ask 超时（默认 24h）：终态落地后 on_done
    释放防重入键，该 flow 当日即可再次调度。
    """
    runner = _runner(request)
    if runner is None:
        raise HTTPException(status_code=503, detail="WakerFlow 运行器未启动")
    ok = runner.cancel(user_id, run_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"无 pending 审批: {run_id}")
    return {"ok": True, "run_id": run_id, "status": "cancelled"}
