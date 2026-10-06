"""Flow 服务层：WakerFlow 路由的非路由辅助逻辑。

从 web_fastapi/routers/wakerflow.py 下沉（P2 瘦身，零行为变化）：
- flow 摘要 / 最新 run 概况（flow_summary / latest_run）
- run jsonl 扫描与事件裁剪（scan_flow_jsonl）
- 请求体 ⇄ FlowSpec / YAML 转换（body_to_spec / body_to_yaml）
- 活跃 run 收集 / 状态修正（active_run_ids / apply_live_status）
- 审批上下文读取（read_approval_context）

纯逻辑 + 文件 IO，不依赖 FastAPI Request——路由层只做薄封装
（Request/app.state 的取用留在路由侧）。
"""
import json
from datetime import datetime

from src.wakerflow.canvas import blocks_to_flow, flow_to_yaml
from src.wakerflow.parser import FlowParseError, parse_flow
from src.wakerflow.store import FlowStore


def flow_summary(name: str, yaml_text: str) -> dict:
    """构造列表项：name + 解析出的 description / steps_count。

    parse 失败也列入（前端展示原 yaml；description 空、steps_count=0）。
    last_run / last_status 由调用方补充（需读 runs 目录）。
    """
    description = ""
    steps_count = 0
    try:
        spec = parse_flow(yaml_text)
        description = getattr(spec, "description", "") or ""
        steps_count = len(getattr(spec, "steps", []) or [])
    except FlowParseError as e:
        # 解析失败仍展示（让用户能看到坏掉的 flow 去修），描述带错误提示
        description = f"[解析失败] {e}"
    return {
        "name": name,
        "description": description,
        "steps_count": steps_count,
    }


def latest_run(store: FlowStore, name: str) -> tuple[str | None, str]:
    """读该 flow 最新 run jsonl 的 (run_id, last_status)。

    last_status 取 flow_end 事件的 status 字段；无 flow_end 则取最后一条
    事件的 status/type 兜底；无 run → (None, "")。
    解析失败的整文件 / 整行都跳过。
    """
    run_dir = store.run_dir(name)
    if not run_dir.is_dir():
        return None, ""
    jsonls = sorted(
        (p for p in run_dir.iterdir() if p.is_file() and p.suffix == ".jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not jsonls:
        return None, ""
    latest = jsonls[0]
    run_id = latest.stem
    status = ""
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
                if not isinstance(ev, dict):
                    continue
                if ev.get("type") == "flow_end":
                    status = ev.get("status", "") or status
                elif "status" in ev:
                    # 兜底：用最后一条带 status 的事件
                    if not status or ev.get("type") == "flow_end":
                        status = ev.get("status", "") or status
    except OSError:
        return run_id, ""
    return run_id, status


def body_to_spec(body):
    """把请求体解析成 FlowSpec（不落盘）。

    优先级：
    - body.yaml 非空 → parse_flow（手写 YAML 路径）
    - body.blocks 非空 → canvas.blocks_to_flow
    - 两者都空 → ValueError

    canvas 转换失败抛 CanvasError（路由层捕获转 400）。
    parse_flow 失败抛 FlowParseError。
    """
    if getattr(body, "yaml", None):
        return parse_flow(body.yaml)

    blocks = getattr(body, "blocks", None)
    if blocks is not None:
        # 收集 schedule 字段（body 上有哪个传哪个，None 的让 FlowSpec 用默认值）
        schedule_keys = ("enabled", "schedule_type", "interval_minutes", "daily_at",
                         "api_enabled", "api_token", "max_runs", "expire_at")
        schedule = {k: getattr(body, k, None) for k in schedule_keys
                    if getattr(body, k, None) is not None}
        return blocks_to_flow(
            blocks,
            name=getattr(body, "name", "") or "",
            description=getattr(body, "description", "") or "",
            inputs=getattr(body, "inputs", None),
            returns=getattr(body, "returns", None),
            schedule=schedule or None,
        )

    raise ValueError("请求体须提供 yaml 或 blocks")


def body_to_yaml(body) -> str:
    """把请求体转成 YAML 文本（供 FlowStore.save）。"""
    return flow_to_yaml(body_to_spec(body))


# worker_event 里夹着 token 流，事件表只留编排级节点。
SIGNIFICANT_EVENT_TYPES = frozenset({
    "flow_start", "flow_end",
    "node_start", "node_end", "node_result",
    "worker_spawn", "worker_ready",
})


def ts_from_run_id(run_id: str) -> str:
    head = (run_id or "").split("-", 1)[0]
    if len(head) == 15 and head[8] == "T" and head[:8].isdigit() and head[9:].isdigit():
        return (
            f"{head[0:4]}-{head[4:6]}-{head[6:8]}T"
            f"{head[9:11]}:{head[11:13]}:{head[13:15]}"
        )
    return ""


def duration_s(started_at: str, ended_at: str) -> int | None:
    if not started_at or not ended_at:
        return None
    try:
        a = datetime.fromisoformat(started_at)
        b = datetime.fromisoformat(ended_at)
    except (TypeError, ValueError):
        return None
    return max(0, int((b - a).total_seconds()))


def node_text(result) -> tuple[str, bool]:
    """从 node_result.result 抽出可读文本；has_sub 表示 parallel/pipeline 容器。"""
    rtext = ""
    has_sub = False
    if isinstance(result, dict):
        rtext = result.get("result", "") or result.get("answer", "") or ""
        has_sub = bool(result.get("sub_results"))
    elif isinstance(result, str):
        rtext = result
    return (str(rtext)[:5000] if rtext else ""), has_sub


def slim_event(ev: dict) -> dict:
    t = ev.get("type") or ""
    out: dict = {"type": t}
    if ev.get("ts"):
        out["ts"] = ev["ts"]
    nid = ev.get("node_id") or ""
    if nid:
        out["node_id"] = nid
    if t == "flow_end":
        out["status"] = ev.get("status") or ""
    elif t == "node_start":
        out["node_type"] = ev.get("node_type") or ""
    elif t in ("node_end", "node_result"):
        out["status"] = ev.get("status") or ""
    elif t in ("worker_spawn", "worker_ready"):
        out["waker"] = ev.get("waker") or ""
    return out


def scan_flow_jsonl(path, *, collect_detail: bool) -> dict:
    """扫一次 flow run jsonl。解析失败的行跳过。"""
    run_id = path.stem
    started_at = ""
    ended_at = ""
    status = ""
    error = ""
    returns: dict = {}
    nodes: list = []
    events: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(ev, dict):
                    continue
                t = ev.get("type") or ""
                if t == "flow_start" and not started_at:
                    started_at = str(ev.get("ts") or "")
                elif t == "flow_end":
                    ended_at = str(ev.get("ts") or ended_at)
                    status = str(ev.get("status") or status)
                    if collect_detail:
                        ret = ev.get("returns")
                        if isinstance(ret, dict) and ret:
                            returns = ret
                    if ev.get("error") and not error:
                        error = str(ev.get("error") or "")[:500]
                elif t in ("run_error", "flow_error"):
                    if not error:
                        error = str(ev.get("message") or ev.get("error") or "")[:500]
                if collect_detail:
                    if t == "node_result":
                        rtext, has_sub = node_text(ev.get("result"))
                        if rtext and not has_sub:
                            nodes.append({
                                "node_id": ev.get("node_id", ""),
                                "status": ev.get("status", ""),
                                "result": rtext,
                            })
                    if t in SIGNIFICANT_EVENT_TYPES:
                        events.append(slim_event(ev))
    except OSError:
        pass
    if not started_at:
        started_at = ts_from_run_id(run_id)
    if collect_detail and not returns and nodes:
        for n in nodes:
            returns[n["node_id"]] = n["result"]
    out = {
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "status": status,
        "error": error,
        "duration_s": duration_s(started_at, ended_at),
    }
    if collect_detail:
        out["returns"] = returns
        out["nodes"] = nodes
        out["events"] = events
    return out


def apply_live_status(rec: dict, active_ids: set[str]) -> dict:
    st = rec.get("status") or ""
    if st in ("completed", "failed", "error", "ok"):
        return rec
    if rec.get("run_id") in active_ids:
        rec["status"] = "running"
    else:
        rec["status"] = "interrupted"
    return rec


def active_run_ids(runner, user_id: str) -> set[str]:
    """收集 runner 内存表中该用户当前活跃的 run_id 集合。

    runner 未挂载 / 列举异常 → 空集（调用方按无活跃 run 处理）。
    """
    out: set[str] = set()
    if runner is None:
        return out
    try:
        for rec in runner.list_active(user_id):
            rid = rec.get("run_id") or ""
            if rid:
                out.add(rid)
    except Exception:
        pass
    return out


def read_approval_context(store: FlowStore, flow_name: str, run_id: str, ask_node_id: str) -> list:
    """读 run jsonl，提取审批节点 ask_node_id 之前的所有 node_result 摘要。

    返回 [{node_id, status, result}]，result 截断到 500 字符（避免前端渲染过大）。
    读到 ask_node_id 的 approval_required 事件就停。
    """
    if not flow_name or not run_id:
        return []
    try:
        # P3-4：run_id / flow_name 过 store 层 _safe_component（防 %5C 反斜杠
        # 在 Windows 穿越出 runs 目录），与 status 端点同一构造入口
        jp = store.run_jsonl_path(flow_name, run_id)
    except ValueError:
        return []
    if not jp.exists():
        return []
    context: list = []
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
                # 遇到审批节点本身 → 停（它的前序节点都收集完了）
                if etype == "approval_required" and ev.get("node_id") == ask_node_id:
                    break
                if etype == "node_result":
                    nid = ev.get("node_id", "")
                    status = ev.get("status", "")
                    result_obj = ev.get("result", {})
                    result_text = ""
                    if isinstance(result_obj, dict):
                        result_text = result_obj.get("result", "") or result_obj.get("answer", "") or ""
                    context.append({
                        "node_id": nid,
                        "status": status,
                        "result": str(result_text)[:500],
                    })
    except OSError:
        pass
    return context
