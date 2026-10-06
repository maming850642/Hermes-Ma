"""Waker 服务层：Waker 路由的非路由辅助逻辑。

从 web_fastapi/routers/waker.py 下沉（瘦身，零行为变化），与
flow_service.py（wakerflow 路由 P2 下沉）同一模式：
- api_token 掩码 / 列表与详情 dict 组装（mask_token / masked_dict / detail_dict）
- 人格三文件读取（persona_texts）
- 请求体 → WakerConfig 转换（cfg_from_create_body / apply_update_body）
- run jsonl 扫描与事件裁剪（scan_jsonl / slim_event / args_summary）
- run_id 时间戳 / 时长换算（ts_from_run_id / duration_s）
- 活跃 run 收集 / 状态修正（active_run_ids / active_runs_by_name / apply_live_status）
- latest_result 读取（read_latest_result）/ 手动运行的 run 计数即时落库
  （persist_manual_run_state）

纯逻辑 + 文件 IO，不依赖 FastAPI Request——路由层只做薄封装
（Request/app.state 的取用留在路由侧）。
"""
import json
import logging
from datetime import datetime

from src.waker.models import WakerConfig
from src.waker.store import WakerStore

logger = logging.getLogger("hermes.web.waker")


# ============================================
# 掩码 / 详情组装
# ============================================
def mask_token(token: str) -> str:
    """api_token 掩码：露前4后4，中间 ****。不足8位全掩。"""
    if not token:
        return ""
    if len(token) <= 8:
        return "****"
    return f"{token[:4]}****{token[-4:]}"


def masked_dict(cfg: WakerConfig) -> dict:
    """cfg.to_dict() 但 api_token 做掩码（列表/详情接口用）。"""
    d = cfg.to_dict()
    d["api_token"] = mask_token(d.get("api_token", ""))
    return d


def persona_texts(store: WakerStore, name: str) -> dict:
    """读 IDENTITY.md / PERSONA.md / BIBLE.md 文本，供详情接口回填表单。

    文件不存在/读失败返回空串。键名与前端 waker.html 一致：
    identity / persona / bible。
    """
    wdir = store.waker_dir(name)
    out = {"identity": "", "persona": "", "bible": ""}
    for key, fname in (("identity", "IDENTITY.md"), ("persona", "PERSONA.md"), ("bible", "BIBLE.md")):
        p = wdir / fname
        if p.exists():
            try:
                out[key] = p.read_text(encoding="utf-8")
            except OSError:
                pass
    return out


def detail_dict(store: WakerStore, cfg: WakerConfig) -> dict:
    """详情 dict：masked 配置 + 人格三文件文本（供编辑表单回填）。"""
    d = masked_dict(cfg)
    d.update(persona_texts(store, cfg.name))
    return d


# ============================================
# 请求体 → 配置
# ============================================
def cfg_from_create_body(body) -> WakerConfig:
    """新建请求体 → WakerConfig（不落盘、不校验，校验由调用方负责）。

    把 body 里非 None 的字段灌进 cfg（identity / persona / bible 除外，
    人格三文件单独走 store.create 的关键字参数）；默认 enabled=False
    （即使前端没传）。
    """
    cfg = WakerConfig(name=body.name)
    data = body.model_dump(exclude={"identity", "persona", "bible"}, exclude_none=True)
    for k, v in data.items():
        if k == "name":
            continue
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    # 创建默认 enabled=False（即使前端没传）
    cfg.enabled = False
    return cfg


def apply_update_body(cfg: WakerConfig, body) -> None:
    """更新请求体的非 None 字段就地灌进 cfg（identity/persona/bible 除外）。

    只改值不校验——cfg.validate / validate_schedule 由调用方负责。
    """
    data = body.model_dump(exclude={"identity", "persona", "bible"}, exclude_none=True)
    for k, v in data.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)


# ============================================
# 运行记录 / 结果
# ============================================
# 详情页默认只回这些事件（token 流太噪，单独记 token_count）。
# runner 实际落盘的是 tool_start / tool_end，不是 tool_call / tool_result。
SIGNIFICANT_EVENT_TYPES = frozenset({
    "run_start", "run_end", "run_error",
    "tool_start", "tool_call",
    "todos_update",
    "complete",
    "approval_auto_rejected", "human_approval_request",
})
TOOL_START_TYPES = frozenset({"tool_start", "tool_call"})


def args_summary(args) -> str:
    """从工具参数里抽出一行摘要（path/url/query），避免把 write_file 全文塞进事件流。"""
    if isinstance(args, str):
        args = args.strip()
        if args.startswith("{") and len(args) < 400:
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, ValueError):
                return args[:120]
        else:
            return args[:120]
    if not isinstance(args, dict):
        return ""
    for key in ("path", "url", "query", "command", "file", "name"):
        val = args.get(key)
        if val:
            return f"{key}={str(val)[:120]}"
    return ""


def slim_event(ev: dict) -> dict:
    """事件流只保留可读字段，丢掉 token / 工具全文。"""
    t = ev.get("type") or ""
    out: dict = {"type": t}
    if ev.get("ts"):
        out["ts"] = ev["ts"]
    if t in TOOL_START_TYPES:
        out["tool_name"] = ev.get("tool_name") or ev.get("name") or ev.get("tool") or ""
        out["summary"] = args_summary(ev.get("tool_args") or ev.get("args") or ev.get("input"))
    elif t == "run_end":
        out["status"] = ev.get("status") or ""
    elif t == "run_error":
        out["message"] = str(ev.get("message") or "")[:300]
    elif t == "todos_update":
        todos = ev.get("todos")
        if isinstance(todos, list):
            out["summary"] = f"{len(todos)} 项"
        elif isinstance(todos, str) and todos:
            out["summary"] = todos[:80]
    elif t == "human_approval_request":
        out["summary"] = str(ev.get("question") or ev.get("message") or "")[:120]
    elif t == "approval_auto_rejected":
        out["summary"] = str(ev.get("reason") or "")[:120]
    return out


def ts_from_run_id(run_id: str) -> str:
    """run_id 前缀 YYYYMMDDTHHMMSS → naive ISO。对不上返回空串。"""
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


def scan_jsonl(path, *, collect_detail: bool) -> dict:
    """扫一个 run jsonl。解析失败的行跳过；读失败仍返回 run_id 骨架。

    collect_detail=False 只取列表需要的开始/结束/状态/错误。
    collect_detail=True 再提取 result / token_count / tool_calls / 关键事件。
    """
    run_id = path.stem
    started_at = ""
    ended_at = ""
    status = ""
    error = ""
    result = ""
    token_count = 0
    tool_calls: list[str] = []
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
                if t == "run_start" and not started_at:
                    started_at = str(ev.get("ts") or "")
                elif t == "run_end":
                    ended_at = str(ev.get("ts") or "")
                    status = str(ev.get("status") or status)
                elif t == "run_error":
                    if not error:
                        error = str(ev.get("message") or "")[:500]
                if collect_detail:
                    if t == "complete":
                        content = ev.get("content")
                        if isinstance(content, str) and content:
                            result = content
                    elif t in ("token", "reasoning_token"):
                        token_count += 1
                    elif t in TOOL_START_TYPES:
                        tname = ev.get("tool_name") or ev.get("name") or ev.get("tool") or ""
                        if tname:
                            tool_calls.append(str(tname))
                    if t in SIGNIFICANT_EVENT_TYPES:
                        events.append(slim_event(ev))
    except OSError:
        pass
    if not started_at:
        started_at = ts_from_run_id(run_id)
    out = {
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "status": status,
        "error": error,
        "duration_s": duration_s(started_at, ended_at),
    }
    if collect_detail:
        out["result"] = result
        out["token_count"] = token_count
        out["tool_calls"] = tool_calls
        out["events"] = events
    return out


def apply_live_status(rec: dict, active_ids: set[str]) -> dict:
    """无 run_end 的半态必须可见：在跑 → running；否则 → interrupted。"""
    st = rec.get("status") or ""
    if st in ("ok", "error", "failed"):
        return rec
    if rec.get("run_id") in active_ids:
        rec["status"] = "running"
    else:
        rec["status"] = "interrupted"
    return rec


def active_run_ids(runner, user_id: str) -> set[str]:
    """收集 runner（WakerAsyncRunner）内存表中该用户活跃的 run_id 集合。

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


def active_runs_by_name(runner, user_id: str) -> dict:
    """收集 runner 内存表中该用户活跃 run 的 waker_name → run_id 映射。

    同名取首个；runner 未挂载 / 列举异常 → 空 dict。
    """
    active: dict = {}  # waker_name → run_id
    if runner is None:
        return active
    try:
        for rec in runner.list_active(user_id):
            wn = rec.get("waker_name", "")
            if wn and wn not in active:
                active[wn] = rec.get("run_id", "")
    except Exception:
        pass
    return active


def read_latest_result(store: WakerStore, name: str) -> str:
    """读 latest_result.md。不存在/读失败返回空串。"""
    path = store.latest_result_path(name)
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def persist_manual_run_state(store: WakerStore, user_id: str, name: str) -> None:
    """手动运行提交成功后即时推进 run_count / last_run_at（异步路径专用）。

    async_runner 跑完只写 jsonl / latest_result，不回写 waker.yaml 的
    state——卡片的 run_count/last_run_at 一直是旧值（运行次数：0 /
    上次运行：—，刷新页面也不变）。提交成功即推进（scheduler.submit_now
    回退路径由 _run_manual 跑完后回写，不在此重复计数；同其语义：
    不动 last_status / next_run_at）。落库失败只记日志，不影响已提交的 run。
    """
    try:
        cfg = store.get(name)
        if cfg is not None:
            cfg.run_count += 1
            cfg.last_run_at = datetime.now().isoformat(timespec="seconds")
            store.save_state(cfg)
    except Exception:
        logger.exception(f"waker 状态即时落库失败: {user_id}/{name}")
