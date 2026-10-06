"""
Worker 子进程入口：每用户一个独立进程，跑完整 MemoryManager + HermesAgentV3。

启动方式（由 WorkerManager fork）：
    python -m web_fastapi.worker_process <user_id>

通信协议：stdin/stdout NDJSON（见 ipc.py）。
- stdin：前端发来的命令（每行一个 JSON）
- stdout：事件流/结果/完成/错误（每行一个 JSON）
- stderr：日志（logging_config 配到 stderr + 文件）

stdout 专用 IPC，绝不写其他内容到 stdout。
session 状态（messages/todos/vfs/session_id）在 worker 内存里，
每轮对话后 worker 自己调 save_session 落盘（贴 CLI 语义）。

P2 三分（零行为变化，子进程入口路径不变）：
- web_fastapi/worker_state.py：SessionBucket / WorkerState（状态与持久化）
- web_fastapi/worker_ops.py：op 实现（OP_HANDLERS 注册表 + 各 _op_* 与
  水合/序列化 helper）
- 本模块保留 stdio 入口/main、IPC 设施（_writer/_send/stdin 读取线程/
  运行期全局标志）、handle_command 分发与 chat 流式执行单元
  （_op_chat/_op_chat_approve/_drain_stream_events——test_signature_guards
  以 AST 钉住本文件内的 stream_invoke 调用点）。移动的名字全部在下方
  re-export：`wp.X` 引用与 monkeypatch.setattr(wp, ...) 语义不变；
  子模块回调本模块的 _send/_cancel_event 等走函数内延迟 import（属性
  访问发生在调用期，monkeypatch 替换可见）。
"""
import sys
import queue as _q
import logging
import threading

# `-m` 启动时本模块以 __main__ 执行，包名 web_fastapi.worker_process 下没有
# 实例——子模块（worker_ops）函数内的延迟 import 会二次执行本模块，得到
# 第二份全局状态（未启动的 StdoutWriter / 独立的 _cancel_event），IPC 从此
# 哑掉。这里把 __main__ 登记为包内模块，保证延迟 import 拿到同一份运行
# 实例（正常 import 路径 __name__ 已是包内名，不受影响）。
if __name__ == "__main__":
    sys.modules["web_fastapi.worker_process"] = sys.modules["__main__"]

# T8b：子进程 stdio 样板（Windows UTF-8 重配 + SSL_CERT_FILE 清洗）收敛到 src/ipc.py
from src.ipc import StdoutWriter, configure_subprocess_stdio, read_stdin_lines

configure_subprocess_stdio()

import config  # noqa: F401
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*LangChainPendingDeprecationWarning.*")

# 日志走 stderr + 文件（绝不碰 stdout）
from src.logging_config import setup_logging
_settings = config.get_settings()
setup_logging(
    debug="--debug" in sys.argv,
    log_dir=_settings.get("log_dir", "logs"),
    log_to_file=_settings.get("log_to_file", True),
)

from web_fastapi.ipc import (
    make_event, make_result, make_done, make_error,
)

# ── P2 三分 re-export：状态/持久化 + op 实现 ──
# 保持 web_fastapi.worker_process 的既有导入面（tests 经 wp.WorkerState /
# wp.SessionBucket 引用，并对 wp.WorkerState 的类属性做 monkeypatch——
# 必须与子模块里的是同一类对象）。
from web_fastapi.worker_state import SessionBucket, WorkerState

from web_fastapi.worker_ops import (
    OP_HANDLERS,
    Handler,
    _CONTEXT_WINDOW_KEYS,
    _apply_llm_params_cmd,
    _apply_settings_update,
    _get_or_hydrate_bucket,
    _history_messages_for_ui,
    _hydrate_bucket,
    _inline_tag,
    _op_chat_stop,
    _op_compact,
    _op_llm_params_set,
    _op_mcp_list,
    _op_permission_mode_get,
    _op_permission_mode_set,
    _op_session_load,
    _op_session_reset,
    _op_session_truncate,
    _op_settings_update,
    _op_waker_get,
    _op_waker_set,
    _parse_tool_args_for_history,
    _serialize_messages_for_history,
)

logger = logging.getLogger("hermes.web.worker")

# ----------------------------------------------------------------------
# stdout 写设施 + stdin 读线程（T8b 收敛到 src/ipc.py）
# ----------------------------------------------------------------------
# 背景：前端断开 SSE（切会话/关标签页）后 worker 无感知，继续往 stdout
# 写事件，管道 buffer（~64KB）写满后 sys.stdout.write 永久阻塞，导致
# worker 对所有后续请求无响应，而父进程还复用这个"活着但卡死"的 worker。
#
# 方案：_send 不再直接写 stdout，而是 put 到无界队列；一个常驻 daemon
# 线程从队列取消息写 stdout。管道满/关闭时，阻塞的只是后台写线程，
# 不再卡住处理命令的主循环——主循环继续读 stdin、继续工作。
_writer = StdoutWriter()


def _now_iso() -> str:
    """当前时间 ISO 字符串（jsonl 事件 ts 用）。"""
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def _send(msg: dict, *, sync: bool = False) -> None:
    """写一条 NDJSON 消息到 stdout（专用 IPC 通道）。

    默认经 StdoutWriter 队列立即返回，永不阻塞调用方——用于运行期事件。

    sync=True 时锁守直写 stdout：仅供启动期同步握手信号（ready / init
    error）使用。这些信号是父子进程的同步点，必须立即送达——走异步队列时
    daemon 写线程可能被 OS 延迟调度，导致父进程 get_or_create 等满一个
    10s 轮才收到 ready（实测启动从亚秒级退化到 10s+ 甚至间歇性超时）。
    """
    if sync:
        try:
            _writer.send_sync(msg)
        except Exception:
            logger.error("worker stdout 同步写入失败", exc_info=True)
    else:
        _writer.send(msg)


# ----------------------------------------------------------------------
# 会话总结改为按需触发（2026-09-08 用户决策）：不再有任何自动总结路径
# （reset/退出均不总结），只有用户显式请求（侧栏「⋯ → 生成总结」→
# POST /api/sessions/{sid}/summary → session_summary op）才跑。
# 旧的后台总结线程池/幂等 memo/退出排空已随自动路径一并移除。
# ----------------------------------------------------------------------

# 软中断标志：chat_stop（读取线程直通）置位，agent 的协作式取消探针
# （stream_invoke(should_cancel=...)）在轮间/chunk/工具间轮询，干净收尾。
# 下一次 chat 启动时 clear。
_cancel_event = threading.Event()

# R3-18 统一关停标志：exit op 只设标志 + 回 ack，不再直接 sys.exit——
# 主循环检测到后 break，走与 stdin 关闭相同的统一收尾
# （save_all_buckets → shutdown_mcp → shutdown_context）
_should_exit = threading.Event()

# stdin 读取线程 + 命令队列：让 chat 执行期间也能收到轻量命令
# （permission_mode_set）。
# 旧架构：for line in sys.stdin 阻塞——chat 运行时读不到 stdin，导致
# 权限切换等命令拿不到 worker 锁、超时失败。
# 新架构：常驻 daemon 线程逐行读 stdin → _cmd_queue；主循环和
# _drain_stream_events 都从队列取命令，互不阻塞。
# （线程体与 NDJSON 解码由 src/ipc.read_stdin_lines 提供）
_cmd_queue: "_q.Queue[dict | None]" = _q.Queue()


# chat 执行期间可内联处理的轻量命令（不经过 handle_command，不产生重入）。
# 这些命令只改内存状态，立即响应，下一轮 stream_invoke 自然读到新值。
# 实现唯一入口是 OP_HANDLERS 注册表（web_fastapi/worker_ops.py，上方
# re-export；内联/主循环共用同一 handler）；
# 本白名单只限定哪些 op 允许在流式间隙插队，属性能语义。
_INLINE_CMDS = {"permission_mode_set", "permission_mode_get", "waker_set", "waker_get",
                "chat_stop", "llm_params_set", "settings_update"}

# llm_params_set 接受的模型热切换键（clear_model_overrides=True 时 None/空串
# = 显式清除该项 override；否则 None = 保持现值）。temperature/max_tokens
# 不在本清单（走 prefs 路径，None=默认）
# （单处定义钉在本文件：test_model_switch 以 getsource 断言本赋值在文件
#  内恰出现一次；worker_ops 跨模块延迟读取。）
_LLM_PARAMS_KEYS = ("model", "base_url", "api_key", "context_window")


def _try_inline_command(state: "WorkerState") -> bool:
    """非阻塞 peek 命令队列，处理轻量命令。返回是否处理了任何命令。

    在 _drain_stream_events 的事件间隙调用：permission_mode_set 等命令
    不需要等 chat 结束就能即时生效。
    """
    handled = False
    while True:
        try:
            cmd = _cmd_queue.get_nowait()
        except _q.Empty:
            break
        if cmd is None:
            # stdin 关闭信号 —— 放回队列让主循环处理
            _cmd_queue.put(None)
            break
        op = cmd.get("op", "")
        req_id = cmd.get("id", "")
        if op in _INLINE_CMDS:
            _handle_inline_cmd(state, op, req_id, cmd)
            handled = True
        else:
            # 非轻量命令 —— 放回队列，让主循环在 chat 结束后处理
            _cmd_queue.put(cmd)
            break
    return handled


def _handle_inline_cmd(state: "WorkerState", op: str, req_id: str, cmd: dict) -> None:
    """处理轻量命令（chat 进行中也能即时生效）。

    P1 注册表化：直接调 OP_HANDLERS 的共享 handler（与主循环同一实现），
    inline=True 让日志带"（内联）"标识。调用方（_try_inline_command）已按
    _INLINE_CMDS 白名单过滤；.get 兜底保证白名单与注册表键集万一失配时
    静默忽略，与旧代码"缺分支则无操作"的行为一致。
    """
    handler = OP_HANDLERS.get(op)
    if handler is not None:
        handler(state, req_id, cmd, inline=True)


def _direct_cancel_probe(cmd: dict) -> bool:
    """stdin 读取线程直通钩子：chat_stop 零延迟置位取消标志。

    历史路径是入队 → _drain_stream_events 事件间隙才处理——工具执行期
    agent 零事件，取消要等当前工具跑完才生效，期间 worker 锁被占、用户
    重发消息报"worker 忙"。现在读取线程直接置位（Event.set 线程安全），
    agent 的协作式取消探针即刻可见。返回 True = 已处理，不再入队。
    ack 经 StdoutWriter 写线程发出（线程安全）；调用方均 fire-and-forget，
    无人在等这个响应，仅作对账。
    """
    if cmd.get("op") != "chat_stop":
        return False
    _cancel_event.set()
    logger.info("chat_stop（读取线程直通）：已置取消标志")
    try:
        _send(make_result(cmd.get("id", ""), ok=True))
    except Exception:
        pass
    return True


def handle_command(state: WorkerState, cmd: dict) -> None:
    """处理一条命令，输出 NDJSON 事件/结果到 stdout。"""
    req_id = cmd.get("id", "")
    op = cmd.get("op", "")

    try:
        if op == "chat":
            _op_chat(state, req_id, cmd)
        elif op == "chat_approve":
            _op_chat_approve(state, req_id, cmd)
        elif op == "memory_get":
            memories = state.memory_manager.get_all(state.user_id)
            _send(make_result(req_id, memories=memories, count=len(memories)))
        elif op == "memory_clear":
            ok = state.memory_manager.delete_all(state.user_id)
            _send(make_result(req_id, ok=ok))
        elif op == "sessions_list":
            from src.session_store import list_sessions
            _send(make_result(req_id, sessions=list_sessions(
                state.user_id, project=cmd.get("project"))))
        elif op == "current_session":
            # M5 多槽位：cmd 可带 session_id 定位本槽应服务的会话（标签页恢复用）。
            # 给定时切到该桶并懒加载水合（事件投影优先），历史为空的新会话保持空态。
            target = cmd.get("session_id", "")
            if target and target != state.current_sid:
                _hydrate_bucket(state, target)
                state.set_current(target)
            # 快照归属（ADR-0005）：前端恢复会话前校验"这条会话是否属于当前
            # 激活项目"用；新会话尚无快照 → ""（与 inbox 同义）。
            try:
                from src.session_store import read_session_meta
                meta = read_session_meta(state.user_id, state.session_id)
                bound_project = (meta or {}).get("project", "")
            except Exception:
                bound_project = ""
            _send(make_result(req_id, session_id=state.session_id,
                              message_count=len(state.session_messages),
                              waker=state.current_bucket.waker,
                              project=bound_project,
                              history=_serialize_messages_for_history(
                                  _history_messages_for_ui(state))))
        elif op == "session_load":
            _op_session_load(state, req_id, cmd)
        elif op == "session_save":
            # M5 亲和（P2-14 同款，对齐 _op_compact）：/api/sessions/save 带
            # session_id 时已亲和路由到本槽，但槽的 current 桶不保证是该会话
            # ——按 cmd.session_id 定位桶保存；缺省回退当前桶（main 槽旧行为）。
            # P2 冷槽水合：刚 spawn 的槽拿到空桶就回 ok=True message_count=0
            # 是误导（盘上可能 50 条）——先水合再回报；真无会话时水合无所获，
            # 空桶不落盘（幽灵治理）+ message_count=0 的诚实语义保持。
            sid = cmd.get("session_id", "") or state.current_sid
            bucket = _get_or_hydrate_bucket(state, sid)
            state._save_bucket(bucket)
            _send(make_result(req_id, ok=True, session_id=bucket.session_id,
                              message_count=len(bucket.messages)))
        elif op == "session_reset":
            _op_session_reset(state, req_id, cmd)
        elif op == "session_rename":
            # 修复：改的是目标会话的 name，不能把当前会话的 messages 存过去（会覆盖历史）
            from src.session_store import save_session, load_session
            target_id = cmd.get("session_id", state.session_id)
            msgs, t, v, wk = load_session(state.user_id, target_id)
            save_session(state.user_id, msgs, target_id, todos=t, virtual_fs=v,
                         name=cmd.get("name", ""), waker=wk)
            _send(make_result(req_id, ok=True, name=cmd.get("name", "")))
        elif op == "session_delete":
            # 删除会话文件（不删当前会话）+ 清理事件流（E1：防止 events/fork 复活已删会话）
            from src.session_store import _get_session_file
            import os
            target_id = cmd.get("session_id", "")
            fpath = _get_session_file(state.user_id, target_id)
            purged = 0
            try:
                log = getattr(state.agent, "_session_log", None)
                if log is not None and target_id:
                    purged = log.purge_session(target_id)
            except Exception:
                logger.warning("会话事件清理失败（不阻断删除）", exc_info=True)
            # P3 kv 权威联动：状态行（todos/vfs/waker）一并清理——只删 JSON
            # 缓存不删 kv 行，会话会以"仅状态"的残影在事件/fork 里复活
            if target_id:
                try:
                    from src.storage.session_state_store import delete_state
                    delete_state(target_id)
                except Exception:
                    logger.warning("会话状态 kv 行清理失败（不阻断删除）",
                                   exc_info=True)
            # 幽灵治理（ADR-0004 附带修复）：同步弹出内存桶，防止退出时
            # save_all_buckets 把已删除会话重写回盘（删档复活路径）
            state.drop_bucket(target_id)
            if fpath.exists():
                fpath.unlink()
                _send(make_result(req_id, ok=True, events_purged=purged))
            elif purged:
                _send(make_result(req_id, ok=True, events_purged=purged))
            else:
                _send(make_error(req_id, "会话文件不存在"))
        elif op == "session_summary":
            # 按需总结（2026-09-08）：这是 Web 侧唯一总结入口，仅由用户经
            # 侧栏「⋯ → 生成总结」显式触发；reset/退出不再自动总结。
            # P3-3：先水合目标会话再总结——旧实现拿"当前桶的消息"配 cmd 里
            # 的 session_id，A 会话的消息会被总结记到 B 名下（跨会话污染）。
            from src.agent.session_lifecycle import on_session_end
            summary_sid = cmd.get("session_id", "") or state.session_id
            if summary_sid != state.current_sid:
                _hydrate_bucket(state, summary_sid)
            summary_bucket = state.get_bucket(summary_sid)
            result = on_session_end(state.memory_manager, state.user_id,
                                    summary_bucket.messages, summary_sid)
            _send(make_result(req_id, **result))
        elif op == "tools_list":
            # 与 resolve_tools（waker 白名单过滤用）同一来源，保证名字一致。
            # 旧实现读 Python 注册表工具（名字是 run_shell 等），
            # 但 resolve_tools 读 yaml（名字是 bash 等）——两套不一致会让
            # waker 白名单填的工具名匹配不上。统一用 resolve_tools 的 spec.name。
            from src.tools.resolve import resolve_tools
            from src.tools.context import ToolContext
            from config import get_settings
            ctx = ToolContext(permission_mode="full_access")
            specs = resolve_tools(ctx, get_settings())
            tools = [{"name": s.name, "description": (s.description or "").split("\n")[0]} for s in specs]
            _send(make_result(req_id, tools=tools, count=len(tools)))
        elif op == "skills_list":
            from src.skills import get_registry
            skills = [{
                "name": s.name, "description": s.description,
                "source_icon": s.source_icon, "source": s.source,
                "resources_count": len(s.resources) if s.resources else 0,
            } for s in get_registry().list_all()]
            _send(make_result(req_id, skills=skills, count=len(skills)))
        elif op == "compact":
            _op_compact(state, req_id, cmd)
        elif op == "session_truncate":
            _op_session_truncate(state, req_id, cmd)
        elif op == "health":
            from src.health import run_health_check
            ok = run_health_check(silent=True)
            _send(make_result(req_id, ok=bool(ok)))
        elif op == "mcp_list":
            _op_mcp_list(state, req_id)
        elif op == "mcp_set_enabled":
            from src.mcp.client import get_client_manager
            ok, msg = get_client_manager().set_enabled(cmd.get("name", ""), cmd.get("enabled", True))
            try:
                state.agent.rebind_tools()
            except Exception:
                pass
            _send(make_result(req_id, ok=ok, message=msg))
        elif op == "mcp_remove":
            from src.mcp.client import get_client_manager
            ok, msg = get_client_manager().remove_server(cmd.get("name", ""))
            try:
                state.agent.rebind_tools()
            except Exception:
                pass
            _send(make_result(req_id, ok=ok, message=msg))
        elif op == "mcp_add":
            from src.mcp.client import get_client_manager
            from src.mcp.config import McpServerConfig
            config_data = cmd.get("config", {})
            try:
                cfg = McpServerConfig.from_dict(config_data.get("name", ""), config_data)
                ok, msg = get_client_manager().add_server(cfg)
                state.agent.rebind_tools()
            except Exception as e:
                ok, msg = False, f"添加失败: {e}"
            _send(make_result(req_id, ok=ok, message=msg))
        elif op == "mcp_reload":
            from src.mcp.client import get_client_manager
            mgr = get_client_manager()
            mgr.reload_config(force=True)
            results = mgr.connect_enabled_all()
            for name, (mok, mmsg) in results.items():
                if mok:
                    logger.info(f"MCP reload {name}: {mmsg}")
                else:
                    logger.warning(f"MCP reload {name}: {mmsg}")
            try:
                state.agent.rebind_tools()
            except Exception:
                pass
            _send(make_result(req_id, ok=True, reloaded=list(results.keys())))
        elif op == "prefs_get":
            _send(make_result(req_id, prefs=state.prefs))
        elif op == "prefs_set":
            updates = cmd.get("prefs", {})
            for k, v in updates.items():
                if k in state.prefs:
                    state.prefs[k] = v
            _send(make_result(req_id, prefs=state.prefs, restart_required=False))
        elif op in OP_HANDLERS:
            # P1 注册表化：7 个轻量 op（chat_stop / permission_mode_* /
            # waker_* / llm_params_set / settings_update）与 chat 期间的内联
            # 路径共用 OP_HANDLERS 的同一 handler——旧实现两套分支体逐行
            # 重复，改一处漏一处。inline=False：日志不带"（内联）"标识，
            # 文案与旧主循环分支逐字一致。其余 op 保持上方 if/elif 原样。
            OP_HANDLERS[op](state, req_id, cmd, inline=False)
        elif op == "waker_run":
            # 数字员工后台任务：在 worker 进程内跑一轮 waker。
            # runner 全程捕获异常，返回 {status, run_id, name, ...}。
            # send 的 timeout 由调度器侧传（WAKER_RUN_TIMEOUT=600s），这里不另设。
            from src.waker.runner import run_waker
            result = run_waker(
                state,
                name=cmd.get("name", ""),
                run_id=cmd.get("run_id", ""),
                api_prompt=cmd.get("api_prompt"),
            )
            _send(make_result(req_id, **result))
        elif op == "workspace_changed":
            # T5：Web 主进程的 fire-and-forget 挂载状态变化通知。
            # 工具列表每 turn 经 resolve_tools 动态组装（chat-only 过滤
            # 直读 kv 里的挂载状态），本 op 只做即时同步 + ack，无需做事。
            _send(make_result(req_id, ok=True))
        elif op == "exit":
            # R3-18：不再直接 sys.exit(0)——那会跳过统一收尾（落盘桶/
            # MCP 关闭/总结排空/上下文拆卸）。这里只设标志 + 回 ack，
            # 主循环 break 后与 stdin 关闭走同一条收尾路径。
            _should_exit.set()
            _send(make_result(req_id, ok=True))
        else:
            _send(make_error(req_id, f"未知操作: {op}"))
    except Exception as e:
        logger.error(f"worker 处理命令异常 op={op}: {e}", exc_info=True)
        _send(make_error(req_id, str(e)))


def _drain_stream_events(state: WorkerState, req_id: str, stream,
                         bucket: SessionBucket) -> None:
    """消费 stream_invoke 的事件 generator，转 IPC 事件。
    turn_messages / messages_snapshot 在内部消化（回写到指定 bucket），不转发。

    T6 起 durable 会话事件（turn/start、user/message、assistant/message、
    tool/call、tool/result、compact/applied、turn/end）由 agent 循环本体直接
    写 SessionLog（T3 时代的 shim 已移除），本函数只做 UI 事件转发 + 桶回写。
    """
    last_turn_count = 0
    for event in stream:
        # 取消语义（协作式）：chat_stop 由 stdin 读取线程直通置位
        # _cancel_event，agent 在轮间/chunk/工具间轮询并干净收尾——
        # cancelled 轮的 turn_messages/complete 仍会正常流过来，必须
        # 消费（否则部分答复落不进桶）。此处不再 break。

        # 轻量命令内联处理：permission_mode_set 等在 chat 期间即时生效
        _try_inline_command(state)

        etype = event.get("type")

        # 压缩全量快照：整体替换 bucket.messages（修复压缩不落盘 bug）。
        # F2 契约：turn_messages 一律增量、messages_snapshot 一律全量。
        # 压缩后 agent 侧增量基线重置 0——下一个 turn_messages 会从压缩后
        # 状态起算重放，因此这里把 last_turn_count 记为 len(snapshot)
        # （而非清零），让 del[-N:]+extend 用重放内容替换快照，避免前缀
        # 二次叠加（旧实现清零导致压缩前缀逐轮腐化）。
        if etype == "messages_snapshot":
            snapshot_msgs = event.get("messages", [])
            bucket.messages = list(snapshot_msgs)
            last_turn_count = len(snapshot_msgs)
            logger.info(
                f"messages_snapshot: bucket(sid={bucket.session_id}) 整体替换为压缩快照 "
                f"({len(snapshot_msgs)} 条)"
            )
            continue  # 不转发

        if etype == "turn_messages":
            turn_msgs = event.get("messages", [])
            if last_turn_count > 0:
                del bucket.messages[-last_turn_count:]
            bucket.messages.extend(turn_msgs)
            last_turn_count = len(turn_msgs)
            continue  # 不转发

        # todos 从事件流捕获进桶（R2-10：get_todos() 是恒 [] 的 stub，
        # 旧的轮末同步会把桶里 todos 每轮清空；todos_update 照常转发前端）
        if etype == "todos_update":
            bucket.todos = list(event.get("todos") or [])

        # 其余事件转发（去掉 type 字段，用 event 名）
        data = {k: v for k, v in event.items() if k != "type"}
        _send(make_event(req_id, etype, **data))

        if etype in ("complete", "human_approval_request"):
            break


def _pop_prelogged_user(bucket: SessionBucket, message: str) -> None:
    """剥掉水合进来的本轮预写 user，恢复「历史不含当前输入」契约。

    prelog 写入的 content 是纯文本 cmd.message；多模态 content（list）
    与 str 不等，不会误剥——附图路径由 stream_invoke 再 append 带图消息。
    """
    msgs = bucket.messages
    if not msgs:
        return
    last = msgs[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return
    if last.get("content") == (message or ""):
        msgs.pop()


def _op_chat(state: WorkerState, req_id: str, cmd: dict) -> None:
    message = cmd.get("message", "")
    images = cmd.get("images", [])  # image id 列表（多模态）
    thinking = bool(cmd.get("thinking", False))

    # 会话分桶：前端带 session_id 时用指定桶，否则用当前桶。
    # P0 冷槽水合：重启后首条消息落到刚 spawn 的会话槽——不水合的话
    # agent 收 messages=[] 失忆开局，轮末 _save_bucket 还会用本轮
    # [user, assistant] 覆写盘上快照（历史蒸发）。
    sid = cmd.get("session_id", "") or state.current_sid
    bucket = _get_or_hydrate_bucket(state, sid)
    # 发送即持久化：主进程已把本轮 user/message 写入事件流/stub。
    # 冷槽水合会把它灌进桶；不剥掉的话 stream_invoke 再 append，LLM
    # 看到两条一模一样的 user，drain 的 turn_messages 还会把桶再叠一条。
    if cmd.get("prelogged"):
        _pop_prelogged_user(bucket, message)

    # 清除上一轮残留的软中断标志，避免历史 cancel 污染本轮
    _cancel_event.clear()

    # LLM 偏好（prefs 热更新）：经 set_llm_params 注入（T6 取代旧
    # llm_with_tools.bind 的 llm_override mock 路径，prefs 真正生效）
    try:
        state.agent.set_llm_params(
            temperature=state.prefs.get("temperature"),
            max_tokens=state.prefs.get("max_tokens"),
        )
    except Exception:
        pass

    from src.tools.remember import set_current_user_id, set_current_project
    from src.tools.virtual_fs import set_current_vfs
    set_current_user_id(state.user_id)
    set_current_project(bucket.project or "")
    set_current_vfs(bucket.vfs)

    # waker 人格：优先用本次请求显式传入的 waker（前端选择器即时切换），
    # 否则用会话桶记忆的 waker。空串=默认助手（不注入人格）。
    waker_name = cmd.get("waker", "") or bucket.waker
    if waker_name:
        # 同步到桶（即时切换落到会话记忆）
        bucket.waker = waker_name
    waker_persona = None
    if waker_name:
        try:
            from src.waker.store import WakerStore
            from src.waker.persona import load_persona_prompt
            _store = WakerStore(state.user_id)
            waker_persona = load_persona_prompt(_store, waker_name) or None
        except Exception:
            logger.warning(f"加载 waker 人格失败: {waker_name}（降级为默认助手）", exc_info=True)
            waker_persona = None

    # 开轮 stub（生成中可发现性）：快照列表靠轮末 _save_bucket 落盘，此前
    # 生成中的新会话在 /api/sessions 列表里不存在——切页回来的零锁快路径
    # 对不上 saved sid、侧栏也看不见本会话。这里在 agent 写 turn/start 前
    # 用 ensure_session_stub 建最小快照：消息 = 既有历史 + 本条 user 消息
    # （与事件投影语义一致，message_count 是诚实的 len+1；不伪造 assistant
    # 占位，防轮次中途崩溃留下幽灵回复；也不传空消息——list_sessions 默认
    # 滤掉 messages 为空的快照，空 stub 在侧栏/列表里仍不可见）。
    # 与轮末写入不冲突（ADR-0004-D2）：ensure_session_stub 仅在快照文件
    # 不存在时创建、绝不覆盖；轮末 _save_bucket 始终是权威整文件覆盖写——
    # 首轮：stub 从无到有 → 轮末覆盖为完整快照；后续轮：文件已在，stub
    # 调用是 no-op。失败只降级可发现性，不阻断本轮对话。
    try:
        from src.session_store import ensure_session_stub
        ensure_session_stub(
            state.user_id,
            list(bucket.messages) + [{"role": "user", "content": message}],
            sid, todos=bucket.todos, virtual_fs=bucket.vfs, waker=bucket.waker,
            project=bucket.project or state._get_active_project())
    except Exception:
        logger.warning("开轮会话 stub 创建失败（不阻断本轮）: sid=%s", sid, exc_info=True)

    # durable 事件（turn/start 含 workspace_mode、user/message、tool/*、
    # assistant/message、turn/end 等）由 agent 循环本体写 SessionLog（T6）。
    # prelogged：主进程 POST handler 已预写本轮 turn/start+user/message
    # （spawn 窗口可见性），agent 跳过这两条防重复。
    stream = state.agent.stream_invoke(
        state.user_id, message, bucket.messages,
        session_id=sid,
        todos=bucket.todos, virtual_fs=bucket.vfs,
        thread_id=sid,
        role=None,
        thinking=thinking,
        prelogged_turn=bool(cmd.get("prelogged")),
        images=images,
        compact_threshold_pct=state.prefs.get("compact_threshold_pct"),
        waker_persona=waker_persona,
        # 协作式取消：读取线程已把 chat_stop 直通置位 _cancel_event，
        # agent 在轮间/chunk/工具间轮询此探针（工具执行期零事件的死区补齐）
        should_cancel=_cancel_event.is_set,
    )
    try:
        _drain_stream_events(state, req_id, stream, bucket)
    finally:
        # cancel 时确保 generator 被关闭，释放内部状态
        stream.close()

    # todos 已在 _drain_stream_events 里从 todos_update 事件捕获进桶
    # （R2-10：不再调 agent.get_todos()——stub 恒 []，会把桶里 todos 清空）
    state._save_bucket(bucket)
    _send(make_done(req_id))


def _op_chat_approve(state: WorkerState, req_id: str, cmd: dict) -> None:
    thread_id = cmd.get("thread_id", state.current_sid)
    decision = cmd.get("decision", "approve")
    reason = cmd.get("reason", "")
    resume_payload = decision if decision == "approve" else f"{decision}:{reason}"

    # 审批恢复也走对应会话桶
    bucket = state.get_bucket(thread_id)

    # 清除上一轮残留的软中断标志（与 _op_chat 一致）
    _cancel_event.clear()

    from src.tools.remember import set_current_user_id, set_current_project
    from src.tools.virtual_fs import set_current_vfs
    set_current_user_id(state.user_id)
    set_current_project(bucket.project or "")
    set_current_vfs(bucket.vfs)

    # P2-1：审批恢复轮必须与 _op_chat 同等的会话上下文——此前只传
    # messages，waker 会话恢复后静默变默认人格、todos 清零、压缩偏好丢失
    waker_persona = None
    if bucket.waker:
        try:
            from src.waker.store import WakerStore
            from src.waker.persona import load_persona_prompt
            waker_persona = load_persona_prompt(WakerStore(state.user_id), bucket.waker) or None
        except Exception:
            logger.warning(f"加载 waker 人格失败: {bucket.waker}（降级为默认助手）", exc_info=True)
            waker_persona = None

    stream = state.agent.stream_invoke(
        state.user_id, "(resume)", bucket.messages,
        session_id=thread_id, thread_id=thread_id,
        todos=bucket.todos, virtual_fs=bucket.vfs,
        compact_threshold_pct=state.prefs.get("compact_threshold_pct"),
        waker_persona=waker_persona,
        resume_payload=resume_payload,
        should_cancel=_cancel_event.is_set,
    )
    # durable 事件（turn/start{input:"(resume)",resume} 等）由 agent 写（T6）
    try:
        _drain_stream_events(state, req_id, stream, bucket)
    finally:
        stream.close()
    state._save_bucket(bucket)
    _send(make_done(req_id))


def _shutdown_worker(state: WorkerState) -> None:
    """统一收尾（stdin 关闭与 exit op 共用，R3-18 抽取）。

    顺序（保持既有语义）：save_all_buckets → shutdown_mcp → 排空 summary
    → shutdown_context。旧 exit op 直接 sys.exit 会跳过前两步（桶不落盘、
    MCP 连接泄漏），现在所有退出路径都经过这里。
    """
    # 保存所有会话桶（多会话分桶）
    try:
        state.save_all_buckets()
    except Exception:
        pass
    # 关闭 MCP
    try:
        state.agent.shutdown_mcp()
    except Exception:
        pass
    # T4：组合根上下文随进程退出拆卸（幂等）。
    state.shutdown_context()


def main():
    """worker 进程主入口。"""
    if len(sys.argv) < 2:
        sys.stderr.write("用法: python -m web_fastapi.worker_process <user_id>\n")
        sys.exit(1)
    user_id = sys.argv[1]
    slot = sys.argv[2] if len(sys.argv) > 2 else "main"

    # 先启动常驻 stdout 写线程：后续所有 _send（含 init 失败的 error）
    # 都走队列 → 管道满/父进程断开时不会卡死主循环（问题2 卡死点 B）。
    _writer.start()

    # 初始化 agent（stderr 日志，stdout 绝不碰）
    state = WorkerState(user_id, slot=slot)
    try:
        state.init_agent()
    except Exception as e:
        # 关键：完整 traceback 必须落 stderr（=父进程控制台）+ error.log。
        # 旧实现只把 str(e) 丢上 IPC，traceback 直接丢弃，导致登录 500 时
        # 日志全空、根因不可观测。
        logger.error(f"worker init_agent 失败: user={user_id}", exc_info=True)
        _send(make_error("init", f"agent 初始化失败: {e}"), sync=True)
        sys.exit(1)

    # 自进化 respawn 接线（src/tools/self_evolve.py）：respawn_self 工具在
    # 防跳步/指纹校验通过后置位 _should_exit，主循环在当前 chat 轮完整
    # 结束后走 R3-18 统一收尾退出——与 exit op 同一路径，manager 下次
    # 请求自动拉起新进程加载新代码。CLI 不注册 → 工具降级为提示。
    from src.tools import self_evolve as _self_evolve
    _self_evolve.set_respawn_hook(_should_exit.set)

    # 发送 ready 信号（sync=True：启动握手必须立即送达，不走异步队列）
    _send({"type": "ready", "user_id": user_id, "slot": slot}, sync=True)

    # 启动常驻 stdin 读取线程（chat 执行期间也能收到轻量命令）。
    # chat_stop 直通：工具执行期 agent 零事件，排队等事件间隙会迟到
    # 几十秒——读取线程立即置位取消标志（Event.set 线程安全）。
    read_stdin_lines(_cmd_queue, on_cmd=_direct_cancel_probe)

    # 命令循环：从 stdin 读取线程的队列取命令，逐条处理
    try:
        while True:
            cmd = _cmd_queue.get()
            if cmd is None:
                # stdin 关闭（父进程断开）→ 退出
                logger.info(f"worker stdin 关闭，退出: user={user_id}")
                break
            handle_command(state, cmd)
            if _should_exit.is_set():
                # R3-18：exit op → 统一收尾（不再 sys.exit 跳过落盘/拆卸）
                logger.info(f"worker 收到 exit op，走统一收尾: user={user_id}")
                break

        _shutdown_worker(state)
    finally:
        # 兜底：_shutdown_worker 内部抛异常时也保证上下文拆卸（幂等）
        state.shutdown_context()

    # 排空写队列并关闭写线程：发 None 终止信号，等它把残留消息写完。
    _writer.close(timeout=5)


if __name__ == "__main__":
    main()
