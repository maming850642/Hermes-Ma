"""
Worker op 实现（P2 自 worker_process.py 三分抽出）。

- OP_HANDLERS：7 个轻量 op 的共享 handler 注册表（内联/主循环唯一实现入口）；
- _apply_settings_update / _apply_llm_params_cmd：内联与主循环共用的
  settings / 模型热切换执行体；
- 会话桶类 op：_op_session_load / _op_session_reset / _op_compact /
  _op_mcp_list 及其水合/序列化 helper（_hydrate_bucket /
  _get_or_hydrate_bucket / _serialize_messages_for_history 等）。

依赖方向（无环）：本模块 → worker_state（顶层）；→ worker_process 只走
函数内延迟 import（仓库惯例）——`_send`/`_cancel_event`/`_hydrate_bucket`
等 IPC 设施与运行期全局仍归 worker_process 所有，经 `wp.` 属性访问同时
保证 tests `monkeypatch.setattr(wp, "_send", ...)` 的替换语义不变。
"""
import json
import logging
import uuid
from collections.abc import Callable

from web_fastapi.ipc import make_error, make_result
from web_fastapi.worker_state import SessionBucket, WorkerState

logger = logging.getLogger("hermes.web.worker")


# settings_update 中触发 context_window lru_cache 作废的键：model_context_window
# / max_short_term_messages 直接影响窗口预算；llm_model_name / openai_base_url
# 决定自动探测（get_context_window → detect_context_window 用它们请求 /models）
# 的目标——任一变化都清缓存，下一轮按新配置重算。
_CONTEXT_WINDOW_KEYS = {"model_context_window", "max_short_term_messages",
                        "llm_model_name", "openai_base_url"}


def _apply_settings_update(state: WorkerState, cmd: dict) -> list:
    """settings_update 共享执行体（内联/主循环两路径同一语义）。

    把更新原地合并进 settings 单例（_Settings 是 dict 子类，update 后
    __getattr__ 立即读到新值；agent 等持有的旧引用与 get_settings() 是
    同一对象 → 同步生效）。worker 不走 reload_settings（那会换对象，旧
    引用保持旧值快照）——内存注入与 llm_params_set 同一信任域，持久真相
    源 config.yaml 已由主进程写盘。返回实际生效的键名。

    布尔键（shell_enabled 等）合并前经 normalize_setting_value 归一——
    主进程 API 层 updates 是 dict[str,str]，'false' 字符串恒真会让
    config_guard（resolve_tools 按 getattr(settings, key) 真值过滤 bash）
    关不掉 shell；与 config.py 读取侧 / config_service.save_yaml 写入侧
    共用同一清单与函数。
    """
    from config import get_settings, normalize_setting_value
    updates = {k: normalize_setting_value(k, v)
               for k, v in (cmd.get("updates") or {}).items() if v is not None}
    if not updates:
        return []
    get_settings().update(updates)

    # 窗口语义键 → 作废 get_context_window 的 lru_cache（缓存的是旧配置
    # 下的解析结果），下一轮调用按新配置重算
    if _CONTEXT_WINDOW_KEYS & updates.keys():
        try:
            from src.agent.context_window import get_context_window
            get_context_window.cache_clear()
        except Exception:
            logger.warning("settings_update: context_window 缓存清理失败", exc_info=True)

    # shell 开关 → 工具面重载：强制重扫 tools/*.yaml（作废 loader 缓存）+
    # registry 重绑（对齐 mcp_reload 的 rebind 路径）。resolve_tools 每
    # turn 按 config_guard 动态过滤，下一轮对话 bash 工具随开关出现/消失；
    # 失败只告警不崩（工具面维持现状，不影响本次 ack）
    if "shell_enabled" in updates:
        try:
            from src.tools.loader import load_builtin_tools
            load_builtin_tools(force=True)
            try:
                if state.agent is not None:
                    state.agent.rebind_tools()
            except Exception:
                logger.warning("settings_update: registry 重绑失败", exc_info=True)
        except Exception:
            logger.warning("settings_update: 工具面强制重载失败", exc_info=True)
    return sorted(updates)


def _apply_llm_params_cmd(state: WorkerState, cmd: dict) -> list:
    """llm_params_set 共享执行体（内联/主循环两路径同一语义）。

    默认（clear_model_overrides=False）：过滤 None 后调
    agent.set_llm_params——None = 该项保持现值（每轮 prefs 注入不得冲掉
    已热切换的模型）。清除模式（cmd.clear_model_overrides=True，模型切换
    专用）：四个模型键全量透传（None/空串 = 显式清除该项 override），解决
    覆盖残留（P 档案有 key/ctx 切到 Q 或默认后，P 的 api_key/context_window
    残留在 override 里继续生效）。不进 state.prefs（prefs_get 回显不得泄漏
    api_key），也不重读 config.yaml：worker 的模型参数唯一来源是本命令。
    返回实际生效的键名（清除模式下含被清除项，日志/对账用）。
    """
    # _LLM_PARAMS_KEYS 单处定义在 worker_process（test_model_switch 以
    # getsource 钉住），跨模块延迟读取
    from web_fastapi import worker_process as wp
    clear = bool(cmd.get("clear_model_overrides"))
    applied = {k: cmd[k] for k in wp._LLM_PARAMS_KEYS
               if cmd.get(k) is not None or clear}
    state.agent.set_llm_params(clear_model_overrides=clear, **applied)
    return sorted(applied)


# ----------------------------------------------------------------------
# 轻量 op 共享 handler + 注册表（P1 重构：消灭内联/主循环双套分发）
# ----------------------------------------------------------------------
# 旧实现：_handle_inline_cmd 与 handle_command 各写一份分支体（7 个 op
# 两处维护，改一处漏一处）。现在实现收敛到下方 _op_* 共享函数，注册表
# OP_HANDLERS 是唯一的 op → handler 映射，两条分发路径都经它调用；差异
# 只剩 inline 标志（日志"（内联）"标识，文案与旧行为逐字一致）。
# 签名约定：(state, req_id, cmd, inline=False) -> None，返回无值——
# 响应一律在 handler 内经 _send 发出。

Handler = Callable[[WorkerState, str, dict, bool], None]


def _inline_tag(inline: bool) -> str:
    """日志文案的内联标识：内联路径带"（内联）"，主循环路径为空串。"""
    return "（内联）" if inline else ""


def _op_chat_stop(state: WorkerState, req_id: str, cmd: dict,
                  inline: bool = False) -> None:
    """chat_stop：置软中断标志 + ack。

    _drain_stream_events 在事件间隙轮询 _cancel_event，检测到后 break
    退出 stream 消费循环，stream.close() 关闭 generator。单轮 LLM 流式
    内部（_stream_llm_with_hard_timeout）不可中断，靠 llm_timeout 间隙
    超时兜底。读取线程直通（_direct_cancel_probe）是第三处独立入口，
    零延迟置位同一标志，不走本 handler。
    """
    from web_fastapi import worker_process as wp
    wp._cancel_event.set()
    if inline:
        # 兜底路径：chat_stop 通常已被读取线程直通置位（_direct_cancel_probe），
        # 走不到这里；保留以防直通钩子异常回退入队的情形。
        logger.info("chat_stop（内联兜底）：已置取消标志")
    wp._send(make_result(req_id, ok=True))


def _op_permission_mode_set(state: WorkerState, req_id: str, cmd: dict,
                            inline: bool = False) -> None:
    """V3 权限模式切换：full_access / before_changes / plan。"""
    from web_fastapi import worker_process as wp
    mode = cmd.get("mode", "before_changes")
    if mode not in ("full_access", "before_changes", "plan"):
        wp._send(make_error(req_id, f"未知权限模式: {mode}"))
    else:
        state.agent.set_permission_mode(mode)
        state.permission_mode = mode
        logger.info(f"权限模式切换为{_inline_tag(inline)}: {mode}")
        wp._send(make_result(req_id, ok=True, permission_mode=mode))


def _op_permission_mode_get(state: WorkerState, req_id: str, cmd: dict,
                            inline: bool = False) -> None:
    from web_fastapi import worker_process as wp
    wp._send(make_result(req_id, permission_mode=state.permission_mode))


def _op_waker_set(state: WorkerState, req_id: str, cmd: dict,
                  inline: bool = False) -> None:
    """切换 waker 人格（空串=默认助手）。下一轮 stream_invoke 读到。

    P2-14：带 session_id 时落到该会话的桶（/api/config/waker 亲和路由
    到本槽，但本槽 current 桶不保证就是该会话）；缺省当前桶。
    """
    from web_fastapi import worker_process as wp
    name = cmd.get("name", "") or ""
    state.get_bucket(cmd.get("session_id") or None).waker = name
    logger.info(f"waker 切换为{_inline_tag(inline)}: {name or '默认助手'}")
    wp._send(make_result(req_id, ok=True, waker=name))


def _op_waker_get(state: WorkerState, req_id: str, cmd: dict,
                  inline: bool = False) -> None:
    # 按桶取值（对齐 waker_set 的桶定位）：带 session_id 读该会话的桶；
    # 缺省 get_bucket(None) 即 current 桶（旧行为不变）。
    from web_fastapi import worker_process as wp
    bucket = state.get_bucket(cmd.get("session_id") or None)
    wp._send(make_result(req_id, waker=bucket.waker))


def _op_llm_params_set(state: WorkerState, req_id: str, cmd: dict,
                       inline: bool = False) -> None:
    """模型热切换（chat 期间内联 / Web 系统配置保存后广播，同一实现）。"""
    from web_fastapi import worker_process as wp
    keys = _apply_llm_params_cmd(state, cmd)
    logger.info(f"模型参数热切换{_inline_tag(inline)}: {keys}")
    wp._send(make_result(req_id, ok=True, restart_required=False, applied=keys))


def _op_settings_update(state: WorkerState, req_id: str, cmd: dict,
                        inline: bool = False) -> None:
    """系统配置全键热更新（原地合并 settings 单例 + 特殊键副作用）。"""
    from web_fastapi import worker_process as wp
    keys = _apply_settings_update(state, cmd)
    logger.info(f"系统配置热更新{_inline_tag(inline)}: {keys}")
    wp._send(make_result(req_id, ok=True, restart_required=False, applied=keys))


# op → 共享 handler 注册表。主分发（handle_command，worker_process）与
# 内联路径（_handle_inline_cmd，worker_process）的唯一实现入口；
# _INLINE_CMDS 白名单机制保留，仍单独限定哪些 op 允许在流式间隙插队
# （性能语义）。
OP_HANDLERS: dict[str, Handler] = {
    "chat_stop": _op_chat_stop,
    "permission_mode_set": _op_permission_mode_set,
    "permission_mode_get": _op_permission_mode_get,
    "waker_set": _op_waker_set,
    "waker_get": _op_waker_get,
    "llm_params_set": _op_llm_params_set,
    "settings_update": _op_settings_update,
}


def _history_messages_for_ui(state) -> list:
    """UI 历史视图：事件流会话用 include_reasoning 投影（assistant 带
    reasoning，供前端回放推理块）；JSON 快照回退的会话本身已含 reasoning；
    两者皆不可得时退回纯投影（丢 reasoning 但不丢内容）。"""
    try:
        log = getattr(getattr(state, "agent", None), "_session_log", None)
        sid = getattr(state, "session_id", "")
        if log is not None and sid and log.events(sid):
            return log.derive_messages(sid, include_reasoning=True)
    except Exception:
        pass
    return state.session_messages


def _serialize_messages_for_history(messages: list) -> list:
    """把消息（OpenAI dict）序列化为前端可用的 history 格式(含工具调用+推理)。"""
    history = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role == "user":
            content = msg.get("content", "")
            # 多模态 content（list）→ 提取文本 + 标记 has_images 供前端渲染
            if isinstance(content, list):
                text_parts, has_images = [], False
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif isinstance(block, dict) and block.get("type") == "image_url":
                        has_images = True
                history.append({
                    "role": "user",
                    "content": "\n".join(text_parts),
                    "has_images": has_images,
                })
            else:
                history.append({"role": "user", "content": content})
        elif role == "assistant":
            entry = {"role": "assistant", "content": msg.get("content") or ""}
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                # OpenAI 格式 → 前端展示用 {id, name, args}（id 供历史回放配对 tool_end）
                entry["tool_calls"] = [
                    {
                        "id": tc.get("id", "") if isinstance(tc, dict) else "",
                        "name": (tc.get("function") or {}).get("name", "")
                        or tc.get("name", ""),
                        "args": _parse_tool_args_for_history(tc),
                    }
                    for tc in tool_calls if isinstance(tc, dict)
                ]
            reasoning = msg.get("reasoning")
            if reasoning:
                entry["reasoning"] = reasoning
            if msg.get("llm_error"):
                # LLM 调用失败留底：标记透传给前端，历史按错误样式渲染
                entry["llm_error"] = True
            history.append(entry)
        elif role == "tool":
            content = msg.get("content", "")
            if isinstance(content, str):
                content = content[:500]
            history.append({
                "role": "tool", "content": content,
                "tool_call_id": msg.get("tool_call_id", ""),
            })
    return history


def _parse_tool_args_for_history(tool_call: dict) -> dict:
    """history 展示用：tool_call 的 arguments（JSON 字符串）→ dict（失败回退原串）。"""
    raw = (tool_call.get("function") or {}).get("arguments", "")
    if raw in ("", None) and "args" in tool_call:
        raw = tool_call.get("args")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {"args": parsed}
    except Exception:
        return {"args": raw}


def _hydrate_bucket(state: WorkerState, session_id: str) -> bool:
    """把 sid 的历史灌入本实例的桶（事件投影优先），返回是否非空。

    M5 多槽位共用：session_load 与 current_session(带 sid) 都走这里。
    归属回填：get_bucket 建桶时 stamp 的是"此刻激活项目"，打开旧项目 A
    会话而顶栏在 B 时桶会被错标 B——以快照 meta 的绑定归属为准回填
    （一次性绑定语义：快照有值永不漂移）；旧快照无归属保持建桶值。
    """
    from src.session_store import load_session_events_first
    log = getattr(state.agent, "_session_log", None) if state.agent is not None else None
    msgs, todos, vfs, waker = load_session_events_first(
        state.user_id, session_id, session_log=log)
    bucket = state.get_bucket(session_id)
    bucket.messages = msgs
    bucket.todos = todos
    bucket.vfs = vfs or {}
    bucket.waker = waker or ""
    try:
        from src.session_store import read_session_meta
        bound = (read_session_meta(state.user_id, session_id) or {}).get("project") or ""
        if bound:
            bucket.project = bound
    except Exception:
        logger.debug(f"水合回填归属失败（保持建桶值）: sid={session_id}", exc_info=True)
    return bool(msgs)


def _get_or_hydrate_bucket(state: WorkerState, session_id: str) -> SessionBucket:
    """取桶；冷槽（桶不存在或消息为空）先经 _hydrate_bucket 水合。

    P0/P2 共用修法（同根因：多槽架构下非 chat 路径对冷槽不水合）：
    应用重启后首条消息 /api/compact /api/sessions/save 落到刚 spawn 的
    会话槽时，桶尚未物化（get_bucket 只会新建空桶）——不水合就开跑，
    agent 收 messages=[] 失忆开局，轮末 _save_bucket 再用本轮
    [user, assistant] 覆写盘上快照（历史蒸发）；compact 则对着盘上明明
    有历史的会话回"无对话历史"。水合失败（无快照无事件）保持空桶 =
    真正的新会话，语义不变。

    桶上已有的会话级状态（waker_set 预设的 waker / todos / vfs）不被
    水合冲掉：盘上字段有值以盘为准，盘上为空（或水合无所获）时回填
    桶上原值。main 槽 current 语义不变——current 桶非空时直接复用，
    不重复读盘。
    """
    # 经 worker_process 属性取 _hydrate_bucket：tests 以
    # monkeypatch.setattr(wp, "_hydrate_bucket", ...) 替换时此处同步生效
    from web_fastapi import worker_process as wp
    buckets = getattr(state, "_buckets", None)
    bucket = buckets.get(session_id) if isinstance(buckets, dict) else None
    if bucket is not None and bucket.messages:
        return bucket
    if not isinstance(buckets, dict):
        # 测试替身（MagicMock state + get_bucket 打桩）没有真实桶表：
        # 按旧语义直接取桩桶，不碰水合路径
        return state.get_bucket(session_id)
    prev_waker = bucket.waker if bucket is not None else ""
    prev_todos = list(bucket.todos) if bucket is not None else []
    prev_vfs = dict(bucket.vfs) if bucket is not None else {}
    wp._hydrate_bucket(state, session_id)
    bucket = state.get_bucket(session_id)
    bucket.waker = bucket.waker or prev_waker
    bucket.todos = bucket.todos or prev_todos
    bucket.vfs = bucket.vfs or prev_vfs
    return bucket


def _op_session_load(state: WorkerState, req_id: str, cmd: dict) -> None:
    # T9 事件优先加载：SessionLog 有事件时消息以事件流投影为准（bucket
    # 消息即事件投影），无事件回退原 JSON 路径（旧会话兼容）。worker 的
    # SessionLog 与组合根同源（agent._session_log），复用免重建。
    from web_fastapi import worker_process as wp
    session_id = cmd.get("session_id", "")
    if not wp._hydrate_bucket(state, session_id):
        wp._send(make_error(req_id, "会话不存在或为空"))
        return
    bucket = state.get_bucket(session_id)
    msgs = bucket.messages
    waker = bucket.waker
    state.set_current(session_id)
    wp._send(make_result(req_id, ok=True, session_id=session_id, message_count=len(msgs), waker=waker or ""))


def _op_session_reset(state: WorkerState, req_id: str, cmd: dict) -> None:
    # 立即落盘旧桶 + 创建新桶 + 切换 + 返回新 session_id（用户零等待）。
    # M5：新 sid 由主进程统一生成下发（cmd.new_sid），避免"生成实例"与
    # "后续 chat 路由实例"错位；未传时保留 worker 本地生成兜底。
    # 会话总结按需触发（2026-09-08）：reset 不再自动总结——只有用户经
    # 侧栏「⋯ → 生成总结」显式请求才跑（session_summary op）。
    from web_fastapi import worker_process as wp
    old_bucket = state.current_bucket
    state._save_bucket(old_bucket)
    new_sid = cmd.get("new_sid") or str(uuid.uuid4())[:8]
    state.set_current(new_sid)
    wp._send(make_result(req_id, ok=True, session_id=new_sid))


def _op_compact(state: WorkerState, req_id: str, cmd: dict) -> None:
    # P2-14：优先压缩 cmd 指定的会话桶（/api/compact 带 session_id 亲和
    # 路由到本槽时，压缩的是那个会话）；缺省回退当前桶（main 槽旧行为）。
    # P2 冷槽水合：刚 spawn 的槽里桶尚未物化——不水合会对着盘上明明有
    # 历史的会话回"无对话历史"（空桶误导）。
    from web_fastapi import worker_process as wp
    sid = cmd.get("session_id", "") or state.current_sid
    bucket = _get_or_hydrate_bucket(state, sid)
    if not bucket.messages:
        wp._send(make_result(req_id, ok=False, message="无对话历史"))
        return
    from src.agent.context import ContextManager
    ctx = ContextManager()
    result = ctx.compact_messages(bucket.messages)
    if result is None:
        wp._send(make_result(req_id, ok=False, message="消息过少，无需压缩"))
        return
    # P1 durable：compact/applied（对齐 CLI compact_session 与 agent
    # _compact_messages 的同型 payload：summary/original_count/compacted_count/
    # kept_messages，lc_to_dict 规范化）。不写的话事件投影重载/fork 复制时
    # 会复活压缩前的全量历史——derive 只认事件流，不知道 JSON 快照已压缩。
    log = getattr(state.agent, "_session_log", None) if state.agent is not None else None
    if log is not None and sid:
        try:
            from src.agent.session_log import COMPACT_APPLIED, build_compact_applied_payload
            log.append(sid, COMPACT_APPLIED,
                       build_compact_applied_payload(result, bucket.messages))
        except Exception:
            logger.warning("compact/applied 事件写入失败（忽略）", exc_info=True)
    state._save_bucket(bucket)
    wp._send(make_result(req_id, ok=True, compacted_count=result.compacted_count,
                         history=_serialize_messages_for_history(bucket.messages)))


def _user_text_of(msg: dict) -> str:
    """user 消息的纯文本（多模态 content 取 text parts 拼接）——截断的
    expect_text 乐观校验与前端序列化口径一致。"""
    content = msg.get("content", "")
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text")
    return content if isinstance(content, str) else str(content)


def _op_session_truncate(state: WorkerState, req_id: str, cmd: dict) -> None:
    # 编辑重发的前半程（2026-09-19）：把会话截断到「第 user_ordinal 条
    # user 消息」之前（0-based；该消息及其后全部丢弃，修改后的新文本随后
    # 走普通 chat 重发，prelog 链路零改动）。照 _op_compact 四步骨架：
    # 水合桶 → 改 messages → 写 durable 事件 → 落盘 + 返回序列化 history。
    # 必须经 worker op 落地：主进程直写事件的话活跃 worker 的内存桶不会
    # 更新，下一轮会把旧尾巴喂 LLM 并在轮末 _save_bucket 覆盖存盘。
    # 寻址用 user 序数而非消息绝对索引：compact 摘要 system（桶里有、前端
    # 不显示）/llm_error 标记/悬空 tool 占位都会让绝对索引错位，user 计数
    # 在前后端两侧稳定一致。
    from web_fastapi import worker_process as wp
    sid = cmd.get("session_id", "") or state.current_sid
    try:
        user_ordinal = int(cmd.get("user_ordinal", -1))
    except (TypeError, ValueError):
        user_ordinal = -1
    if user_ordinal < 0:
        wp._send(make_result(req_id, ok=False, message="user_ordinal 非法"))
        return
    expect_text = (cmd.get("expect_text") or "").strip()
    bucket = _get_or_hydrate_bucket(state, sid)

    seen = -1
    cut = -1
    for idx, msg in enumerate(bucket.messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            seen += 1
            if seen == user_ordinal:
                cut = idx
                break
    if cut < 0:
        wp._send(make_result(
            req_id, ok=False,
            message=f"会话里没有第 {user_ordinal + 1} 条用户消息（可能已被其他窗口修改）"))
        return
    if expect_text and _user_text_of(bucket.messages[cut]).strip() != expect_text:
        wp._send(make_result(
            req_id, ok=False,
            message="消息内容已变化，请刷新会话后重试"))
        return

    kept = bucket.messages[:cut]
    # durable：session/truncated（derive 在此重置投影；不写的话事件投影
    # 重载/fork 复制会复活被截掉的历史——derive 只认事件流，同 _op_compact）
    log = getattr(state.agent, "_session_log", None) if state.agent is not None else None
    if log is not None and sid:
        try:
            from src.agent.session_log import TRUNCATED
            from src.session_store import lc_to_dict
            log.append(sid, TRUNCATED, {
                "kept_messages": [lc_to_dict(m) for m in kept],
                "original_count": len(bucket.messages),
                "truncated_count": len(bucket.messages) - cut,
            })
        except Exception:
            logger.warning("session/truncated 事件写入失败（忽略）", exc_info=True)
    bucket.messages = kept
    state._save_bucket(bucket)   # 空桶（编辑第一条消息）内部拒写，属既有幽灵治理语义
    wp._send(make_result(
        req_id, ok=True, truncated_count=len(bucket.messages),
        history=_serialize_messages_for_history(bucket.messages)))


def _op_mcp_list(state: WorkerState, req_id: str) -> None:
    from web_fastapi import worker_process as wp
    from src.mcp.client import get_client_manager
    servers = []
    for s in get_client_manager().list_servers():
        cfg = s.config
        if not cfg.enabled:
            status = "disabled"
        elif s.connected:
            status = "connected"
        elif s.error:
            status = "failed"
        else:
            status = "connecting"
        servers.append({
            "name": cfg.name, "transport": cfg.transport, "command": cfg.command,
            "enabled": cfg.enabled, "connected": s.connected,
            "tool_count": s.tool_count, "error": s.error, "status": status,
        })
    connected = sum(1 for s in servers if s["connected"])
    total_tools = sum(s["tool_count"] for s in servers if s["connected"])
    wp._send(make_result(req_id, servers=servers, connected=connected, total_tools=total_tools))
