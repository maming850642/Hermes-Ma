"""配置 API：per-user 偏好（热更新）。

M5 多槽位：prefs/permission_mode 的"用户级真相"由 WorkerManager 镜像
持有并广播到全部存活实例（写入路径）；读取走默认槽 main。
系统配置（读写 config.yaml）：模型三项（llm_model_name / openai_base_url /
openai_api_key）保存后 reload_settings + 广播 llm_params_set 即时生效，
worker 不重启；其余系统键（shell_* 等）仍需重启。"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Request

from config import reload_settings
from src.constants import LOCAL_USER
from web_fastapi.services.config_service import load_yaml, save_yaml, mask_secret, classify_keys
from web_fastapi.dependencies import get_current_user_id, get_worker
from web_fastapi.routers.chat import _worker_for as chat_gate_worker
from web_fastapi.worker_manager import SlotsFullError, WorkerProcess
from web_fastapi.models import SystemConfigUpdate, PrefsUpdate, PermissionModeUpdate, WakerSelectBody, ModelSwitchBody

logger = logging.getLogger("hermes.web.config")
router = APIRouter()

_SECRET_KEYS = {"openai_api_key"}

# 模型键：保存后经 reload_settings + broadcast_llm_params 即时生效
# （不再需要重启）；其余系统键维持"重启生效"语义
_HOT_MODEL_KEYS = {"llm_model_name", "openai_base_url", "openai_api_key"}

# 真正无法热生效的键：workspace_root（挂载语义）、web_proxy（LLM 客户端
# 构造期固化）。其余全部经 settings_update 广播到 worker 内存态即时生效
_RESTART_KEYS = {"workspace_root", "web_proxy"}


def _session_exists(request: Request, sid: str) -> bool:
    """会话是否已落盘（快照或事件流）。纯主进程读，零 worker IPC。"""
    from src.session_store import read_session_meta
    if read_session_meta(LOCAL_USER, sid) is not None:
        return True
    try:
        from web_fastapi.routers.sessions import _sessions_log
        if _sessions_log(request).events(sid):
            return True
    except Exception:
        logger.warning(f"会话事件流校验失败（按不存在处理）: sid={sid}",
                       exc_info=True)
    return False


def _require_session(request: Request, sid: str) -> None:
    """已废弃的旧校验入口（保留占位避免旧测试直接 AttributeError）。

    PUT /model 已改为 _session_exists 直判 + 草稿降级全局（2026-09-10）：
    首条消息前的草稿会话 sid 尚未落盘，硬 404 会拒掉合法的"先选模型再
    开聊"流程。伪造 sid 的防护不变——未知 sid 只降级全局广播，绝不
    spawn 专属槽，不占并发名额。
    """
    if _session_exists(request, sid):
        return
    raise HTTPException(status_code=404, detail=f"会话不存在: {sid}")


@router.get("/system")
async def get_system_config(user_id: str = Depends(get_current_user_id)):
    cfg = load_yaml()
    masked = {}
    for k, v in cfg.items():
        if k in _SECRET_KEYS and isinstance(v, str):
            masked[k] = mask_secret(v)
        else:
            masked[k] = v
    return {"config": masked}


@router.put("/system")
async def put_system_config(body: SystemConfigUpdate,
                            request: Request,
                            user_id: str = Depends(get_current_user_id)):
    system_keys, _ = classify_keys()
    to_write = {k: v for k, v in body.updates.items() if k in system_keys}
    rejected = {k for k in body.updates if k not in system_keys}
    # 掩码回写防御：GET /system 返回的密钥是掩码值（含 *），
    # 前端整表提交时若原样带回，会把掩码串写进 config.yaml 毁掉真实密钥。
    # 含 * 的密钥提交值一律跳过（真实密钥不会含 *）。
    skipped_masked = [k for k in _SECRET_KEYS if k in to_write and isinstance(to_write[k], str) and "*" in to_write[k]]
    for k in skipped_masked:
        del to_write[k]
    # 保存前快照（api_key 变化判断基线：掩码跳过/同值重存都不算变化）
    old_cfg = load_yaml() if to_write else {}
    if to_write:
        save_yaml(to_write)
    # 保存成功即重读主进程 settings（全部键），模型三项另走专用广播
    hot_written = [k for k in to_write if k in _HOT_MODEL_KEYS]
    if to_write:
        fresh = reload_settings()
    if hot_written:
        params: dict = {"model": fresh.get("llm_model_name", ""),
                        "base_url": fresh.get("openai_base_url", ""),
                        # 清除 context_window override：全局配置是新的真相源，
                        # 档案切换残留的窗口覆盖不得遮蔽 config.yaml 的
                        # model_context_window / 自动探测（None+clear=清除）
                        "context_window": None}
        # api_key 只在真实变化时下发（不打扰 worker、不作废其客户端缓存）；
        # clear 模式下未随载荷下发 = 清除 override，回退刚写盘的 config 值
        new_key = to_write.get("openai_api_key")
        if new_key is not None and new_key != old_cfg.get("openai_api_key"):
            params["api_key"] = new_key
        request.app.state.worker_manager.broadcast_llm_params(
            clear_model_overrides=True, **params)
    # 全键热同步：除真正需重启的键外，写盘成功即把变更广播到全部存活
    # worker 的内存 settings（worker 侧原地生效；模型键另有 llm_params_set
    # 专用通道，这里仍同步字典保持两端一致）
    sync_updates = {k: v for k, v in to_write.items() if k not in _RESTART_KEYS}
    if sync_updates:
        request.app.state.worker_manager.broadcast_settings_updates(sync_updates)
    # restart 语义（做准到键）：仅 workspace_root / web_proxy 需重启
    restart_keys = sorted(k for k in to_write if k in _RESTART_KEYS)
    return {
        "ok": True,
        "written": list(to_write.keys()),
        "rejected": list(rejected),
        "skipped_masked": skipped_masked,
        "restart_required": bool(restart_keys),
        "restart_required_keys": restart_keys,
        "applied_immediately": sorted(k for k in to_write if k not in _RESTART_KEYS),
    }


@router.get("/prefs")
def get_prefs(worker: WorkerProcess = Depends(get_worker)):
    # 同步 def：worker.send 阻塞等待须发生在线程池，不能卡事件循环
    events = worker.send("prefs_get")
    return events[0]["data"] if events else {"prefs": {}}


@router.put("/prefs")
def put_prefs(body: PrefsUpdate,
              request: Request,
              worker: WorkerProcess = Depends(get_worker)):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    # M5：Manager 镜像 + 广播到全部存活实例（新槽 spawn 时也会重放）
    request.app.state.worker_manager.remember_prefs(updates)
    return {"ok": True, "prefs": updates}


# ---- V3 权限模式（full_access / before_changes / plan）----

@router.get("/permission-mode")
def get_permission_mode(request: Request):
    """读取当前权限模式。

    直接读 Manager 镜像（落盘持久化，零 IPC）——历史版本走 worker.send，
    chat 进行中拿不到锁导致读取失败被回落成默认值，前端就会把用户设置的
    模式显示/覆盖回 before_changes。
    """
    return {"permission_mode": request.app.state.worker_manager.get_permission_mode()}


@router.put("/permission-mode")
async def set_permission_mode(body: PermissionModeUpdate,
                              request: Request):
    """切换权限模式（fire-and-forget，chat 进行中也能即时生效）。

    Manager 镜像（落盘）+ 广播到全部存活实例，任何标签页的下一轮
    chat 都拿到同一模式；worker 未启动时不为此 spawn（spawn 时会
    从镜像重放）。

    full_access     — 完全访问（所有工具直接执行）
    before_changes  — 变更前访问（destructive 工具需审批）
    plan            — 计划模式（destructive 工具被拒绝）
    """
    request.app.state.worker_manager.remember_permission_mode(body.mode)
    return {"ok": True, "permission_mode": body.mode}


# ---- 模型档案热切换（对接 /api/models 注册表） ----

@router.put("/model")
def put_model(body: ModelSwitchBody, request: Request):
    """按模型档案热切换模型（不重启、下一轮 chat 生效）。

    四键（model/base_url/api_key/context_window）全量下发 + 显式清除语义
    （clear_model_overrides=True）：档案/默认缺哪项就清哪项——切档案 P
    （有 key+ctx）→ Q（无 key）或回默认后，P 的 api_key/context_window
    不得残留在 override 里继续生效（key 残留会把 P 的凭据发给 Q 的服务端）。
    session_id 提供且会话已落盘时按 chat 闸门亲和路由到该会话的 worker 槽
    （单会话切换）；缺省或**草稿会话**（首条消息前 sid 尚未落盘）降级为
    全局切换——广播存活 worker，零存活时经 manager 内存挂账由下一个
    spawn 重放（正是首条消息的会话 worker），草稿期切模型不再被 404 拒绝
    （2026-09-10）。伪造/未知 sid 同样只走全局分支，绝不 spawn 专属槽
    （不占并发名额，DoS 防护不变）。会话分支与全局分支同为即发即忘：
    llm_params_set 是幂等设置操作、无返回值依赖，send 抢 worker 锁在
    chat 流式期间（最长 300s 持锁，5s 等锁超时）必然失败。api_key 经
    IPC stdin 管道注入（与 boot 同信任域），不进 prefs、不落盘、不在
    响应回显。
    """
    params: dict
    display = "默认（config.yaml）"
    if body.profile_id:
        from src.model_registry import resolve_profile
        profile = resolve_profile(body.profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail=f"模型档案不存在: {body.profile_id}")
        params = {
            "model": profile["model"],
            "base_url": profile["base_url"],
            # Q 档案无 key → 空串显式清除 P 残留；无 ctx → None 显式清除
            "api_key": profile.get("api_key") or "",
            "context_window": (int(profile["context_window"])
                               if profile.get("context_window") else None),
        }
        display = profile.get("display") or body.profile_id
    else:
        from config import get_settings
        s = get_settings()
        params = {
            "model": s.get("llm_model_name"),
            "base_url": s.get("openai_base_url"),
            # config 无 key → 空串显式清除；context_window 一律 None 清除，
            # 回落 settings.model_context_window / 自动探测（get_context_window）
            "api_key": s.get("openai_api_key") or "",
            "context_window": None,
        }

    sid = (body.session_id or "").strip()
    applied: list[str] = []
    send_ok = True
    session_routed = bool(sid) and _session_exists(request, sid)
    try:
        if session_routed:
            worker = chat_gate_worker(request, sid)
            send_ok = worker.send_fire_and_forget(
                "llm_params_set", session_id=sid,
                clear_model_overrides=True, **params)
            applied = sorted(params) if send_ok else []
        else:
            # 草稿会话（首条消息前 sid 未落盘）/ 空 sid / 未知 sid：全局语义
            request.app.state.worker_manager.broadcast_llm_params(
                clear_model_overrides=True, **params)
            applied = sorted(params)
    except SlotsFullError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    return {
        "ok": send_ok,
        "profile_id": body.profile_id,
        "display": display,
        "session_id": sid,
        "scope": "session" if session_routed else "global",
        "applied": applied,
        "restart_required": False,
        **({} if send_ok else {"error": "worker 暂不可用"}),
    }


# ---- 会话 waker 人格切换（chat 选择器用）----

@router.get("/waker")
def get_session_waker(request: Request, session_id: str = ""):
    """读取会话绑定的 waker 名（空=默认助手）。

    P2-14 同源：带 session_id 时按 chat 闸门亲和路由到该会话的 worker 槽
    （与 PUT /waker 对称，否则专属槽会话读回的是 main 的值）；无参数维持
    main 槽旧行为。保持同步 def——worker.send 阻塞抢 per-worker 锁，
    _worker_for 还可能同步 spawn，都须在线程池跑（memory.py:8-10 规则）。
    """
    sid = (session_id or "").strip()
    try:
        if sid:
            worker = chat_gate_worker(request, sid)
        else:
            worker = request.app.state.worker_manager.get_or_create(LOCAL_USER)
    except SlotsFullError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    try:
        # session_id 透传进 op：worker 侧按会话桶取值（对齐 waker_set 的
        # 桶定位——槽 worker 的 current 桶不保证就是该会话）；无参数时
        # worker 按 current 桶返回（main 槽旧行为不变）。
        events = worker.send("waker_get", session_id=sid)
        return events[0]["data"] if events else {"waker": ""}
    except Exception:
        return {"waker": ""}


@router.put("/waker")
def set_session_waker(body: WakerSelectBody, request: Request):
    """切换会话的 waker 人格（fire-and-forget，chat 进行中也能即时生效）。

    name 空串=默认助手；非空=注入对应 waker 的 IDENTITY/PERSONA/BIBLE 人格。

    P2-14：已落盘会话按 chat 闸门亲和路由到该会话的 worker 槽；草稿/
    未知 sid 落到 main 槽（不 spawn 专属槽，防伪造 sid 占并发名额）。
    人格仍随下一条 chat 的 waker 字段生效。无 session_id 维持 main 槽旧行为。
    """
    sid = (body.session_id or "").strip()
    try:
        if sid and _session_exists(request, sid):
            worker = chat_gate_worker(request, sid)
        else:
            worker = request.app.state.worker_manager.get_or_create(LOCAL_USER)
    except SlotsFullError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    ok = worker.send_fire_and_forget("waker_set", name=body.name, session_id=sid)
    if ok:
        return {"ok": True, "waker": body.name}
    return {"ok": False, "waker": body.name, "error": "worker 暂不可用"}
