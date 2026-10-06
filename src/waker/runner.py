"""
============================================
run_waker —— worker 进程内的 waker 执行体
============================================
worker_process.py 收到 waker_run op 时调用本模块的 run_waker。

职责（在 per-user worker 子进程里）：
1. 读 waker 配置 + 人格
2. 组装任务输入（task_prompt + 可选 api_prompt）
3. 工具白名单过滤（cfg.tools 非空时，经 ToolContext.allowed_tools 生效）
4. permission_mode 临时覆盖（跑完恢复）
5. 调 agent.stream_invoke(waker_persona=...)，把事件流原子追加到 jsonl
6. 遇到 human_approval_request：无人值守自动拒绝（记 jsonl）
7. 取最终 assistant 文本写 latest_result.md
8. 返回 {"status": "ok"|"error", ...}，绝不抛异常（worker 不崩）

state 是 worker_process.WorkerState（含 user_id / agent / memory_manager /
permission_mode）。本模块不重新构造 agent，直接复用 worker 的单例 agent。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from src.waker.store import WakerStore
from src.waker.persona import load_persona_prompt

logger = logging.getLogger("hermes.waker.runner")

# permission_mode 合法值（与 src/tools/context.py 对齐）
_VALID_PERMISSION_MODES = ("full_access", "before_changes", "plan")


def run_waker(
    state: Any,
    name: str,
    run_id: str,
    api_prompt: str | None = None,
) -> dict:
    """在 worker 进程里跑一轮 waker 任务。

    Args:
        state: worker_process.WorkerState 实例（含 agent / user_id）
        name: waker 名（目录名）
        run_id: 调度器/触发方生成的运行 ID（写 jsonl 文件名）
        api_prompt: API 触发时附加的指令（非空时拼到 task_prompt 后）

    Returns:
        {"status": "ok"|"error", "run_id", "name", "message"?, "waker"?}
        绝不抛异常——所有异常都被捕获转成 status=error。
    """
    try:
        return _run_waker_impl(state, name, run_id, api_prompt)
    except Exception as e:
        logger.exception(f"run_waker 未捕获异常: {name}/{run_id}")
        return {
            "status": "error",
            "run_id": run_id,
            "name": name,
            "message": f"runner 异常: {e}",
        }


def _run_waker_impl(state: Any, name: str, run_id: str, api_prompt: str | None) -> dict:
    """run_waker 的真正实现（可能抛异常，由 run_waker 兜底）。"""
    user_id = state.user_id
    agent = state.agent

    # waker 尚无项目归属（后续单独立项）：显式复位会话项目上下文，
    # 防止 worker 单线程里残留上一轮 chat 会话的 project contextvar
    # （否则 waker 记忆会错标进"最近聊过的会话"的项目）。
    from src.tools.remember import set_current_project
    set_current_project("")

    # 1. 读配置 + 人格
    store = WakerStore(user_id)
    cfg = store.get(name)
    if cfg is None:
        return {
            "status": "error",
            "run_id": run_id,
            "name": name,
            "message": f"waker 不存在: {name}",
        }

    persona = load_persona_prompt(store, name)

    # 2. 组装任务输入
    task_input = cfg.task_prompt or ""
    if api_prompt:
        task_input = (task_input + "\n\n[API 触发附加指令]\n" + api_prompt).strip()
    if not task_input:
        return {
            "status": "error",
            "run_id": run_id,
            "name": name,
            "message": "task_prompt 为空，无任务可执行",
        }

    # 3. 准备 jsonl 日志（原子追加）
    run_dir = store.run_dir(name)
    run_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = run_dir / f"{run_id}.jsonl"

    def _append_event(event: dict) -> None:
        """原子追加一条事件到 jsonl（每行一个 json，写完 flush+fsync）。"""
        line = json.dumps(event, ensure_ascii=False, default=str)
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # 某些文件系统不支持 fsync

    # 起始标记
    _append_event({
        "type": "run_start",
        "run_id": run_id,
        "name": name,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "api_prompt": bool(api_prompt),
    })

    # 4. permission_mode 临时覆盖
    # worker 单例 agent 的 _permission_mode 在 stream_invoke 内被读进 ToolContext；
    # 通过 agent.set_permission_mode 临时改，运行后恢复。
    # 注意：这与 worker 的 permission_mode_set op 共享同一字段，但 waker_run 是
    # 同步串行的（send 持 worker 锁期间跑完），不会有并发 permission_mode_set 竞争。
    target_mode = cfg.permission_mode if cfg.permission_mode in _VALID_PERMISSION_MODES else "before_changes"
    saved_mode = agent.get_permission_mode()
    saved_state_mode = getattr(state, "permission_mode", saved_mode)
    if saved_mode != target_mode:
        agent.set_permission_mode(target_mode)
        state.permission_mode = target_mode
        logger.info(f"waker {name} 临时切换 permission_mode: {saved_mode} → {target_mode}")

    final_status = "ok"
    final_text = ""
    # 工具白名单：cfg.tools 非空时只保留白名单内工具（含 mcp 工具按名匹配）。
    whitelist = set(cfg.tools) if cfg.tools else None
    try:
        # 5+6. 构造 stream + 消费事件流（白名单经 ToolContext 全程生效）
        final_text, hit_approval = _run_stream(
            agent, user_id, task_input, persona, run_id, name,
            whitelist, _append_event,
        )

        # approval 自动拒绝：见 _consume_stream 内的 TODO 注释。
        # 这里只记录最终状态：如果中途因 approval 中断，final_text 可能很短，
        # status 仍记 ok（任务本身跑完了，只是被规则拦下）。
        if hit_approval:
            logger.info(f"waker {name}/{run_id} 遇到审批请求，已按无人值守自动拒绝")

    except Exception as e:
        logger.exception(f"waker {name}/{run_id} stream_invoke 失败")
        final_status = "error"
        final_text = f"运行失败: {e}"
        _append_event({
            "type": "run_error",
            "run_id": run_id,
            "name": name,
            "message": str(e),
        })
    finally:
        # 7. 恢复 permission_mode
        if saved_mode != target_mode:
            try:
                agent.set_permission_mode(saved_mode)
                state.permission_mode = saved_state_mode
            except Exception:
                logger.warning(f"waker {name} 恢复 permission_mode 失败", exc_info=True)

    # 8. 写 latest_result.md（覆盖）
    try:
        result_path = store.latest_result_path(name)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = result_path.with_suffix(result_path.suffix + ".tmp")
        tmp.write_text(
            f"# waker: {name}\n\n**run_id**: {run_id}\n**status**: {final_status}\n"
            f"**ts**: {datetime.now().isoformat(timespec='seconds')}\n\n---\n\n{final_text}\n",
            encoding="utf-8",
        )
        os.replace(tmp, result_path)
    except Exception:
        logger.exception(f"waker {name}/{run_id} 写 latest_result 失败")

    _append_event({
        "type": "run_end",
        "run_id": run_id,
        "name": name,
        "status": final_status,
        "ts": datetime.now().isoformat(timespec="seconds"),
    })

    return {
        "status": final_status,
        "run_id": run_id,
        "name": name,
        "waker": name,
    }


# ════════════════════════════════════════════════════════════════
# 辅助：stream 消费（工具白名单经 ToolContext.allowed_tools 生效）
# ════════════════════════════════════════════════════════════════
def _run_stream(agent, user_id, task_input, persona, run_id, name,
                whitelist, append_fn) -> tuple[str, bool]:
    """构造 stream_invoke generator + 消费事件，返回 (final_text, hit_approval)。

    工具白名单过滤（T8a 作用域化）：whitelist 非空时经 stream_invoke 的
    allowed_tools 参数传给 ToolContext，resolve_tools 尾部按名过滤（含 MCP
    工具）。相比旧的 resolve_tools monkey-patch：作用域天然覆盖"构造
    generator + 迭代消费"整个区间（ctx 在每轮工具组装时都被使用），
    无需 try/finally 还原，也无进程级补丁窗口。
    """
    stream = agent.stream_invoke(
        user_id, task_input,
        session_id=f"waker:{name}:{run_id}",
        thread_id=f"waker:{name}",
        role=None,
        waker_persona=persona or None,
        allowed_tools=whitelist,
        caller_context="employee",  # 数字员工运行上下文：blocked_in=[employee] 的管理面工具（create_waker 等）对其隐藏
    )
    # R3-16：把 agent 的 interrupt_store 传进消费循环——无人值守遇到审批
    # 请求时 pop 掉 pending（写 interrupt/resolved），否则 worker 重启时
    # pending_interrupts 恢复扫描会把 waker 的死审批重导进 chat worker。
    interrupt_store = getattr(agent, "interrupt_store", None)
    return _consume_stream(
        stream, append_fn, name, run_id,
        interrupt_store=interrupt_store, thread_id=f"waker:{name}",
    )


def _consume_stream(stream, append_fn, name: str, run_id: str,
                    interrupt_store=None, thread_id: str = "") -> tuple[str, bool]:
    """消费 stream_invoke 事件流：每条 append 到 jsonl，提取最终文本。

    Args:
        interrupt_store / thread_id: R3-16——遇 human_approval_request 时调
            pop(thread_id, "reject", "waker_auto") 清 pending 并落
            interrupt/resolved 事件（agent 持有 store，runner 透传）。

    Returns:
        (final_text, hit_approval)

    遇到 human_approval_request 事件时记录"自动拒绝"，但 generator 已结束
    （stream_invoke 在 yield human_approval_request 后 return），无法继续。
    TODO: 真正的"自动拒绝并继续执行"需要 agent 支持 resume_payload=reject
    语义自动续跑。本任务只确保 permission_mode 语义正确——在 plan 模式下
    destructive 工具本就被 deny（不触发 approval）；before_changes 才会触发
    approval，此时无人值守下任务无法继续是预期行为。
    """
    final_text = ""
    hit_approval = False
    for event in stream:
        etype = event.get("type", "")

        # 记录到 jsonl（除了 turn_messages/messages_snapshot 这类大对象
        # 也照记，便于回放；default=str 防序列化失败）
        _safe_append(append_fn, event)

        if etype == "complete":
            final_text = event.get("content", "") or ""
        elif etype == "human_approval_request":
            hit_approval = True
            # R3-16：auto-reject 补 resolved——pop 写 interrupt/resolved，
            # 中断事件流不再残留 pending（重启恢复扫描也不会重导入）
            if interrupt_store is not None and thread_id:
                try:
                    interrupt_store.pop(thread_id, "reject", "waker_auto")
                except Exception:
                    logger.warning(
                        f"waker {name}/{run_id} auto-reject pop 中断失败（忽略）",
                        exc_info=True,
                    )
            append_fn({
                "type": "approval_auto_rejected",
                "run_id": run_id,
                "name": name,
                "reason": "无人值守运行，自动拒绝审批请求",
                "ts": datetime.now().isoformat(timespec="seconds"),
            })

    # stream 正常结束但无 complete 事件：兜底取空文本
    return final_text, hit_approval


def _safe_append(append_fn, event: dict) -> None:
    """append 事件，序列化失败时降级存 type + error 信息。"""
    try:
        append_fn(event)
    except Exception:
        try:
            append_fn({
                "type": event.get("type", "unknown"),
                "_serialize_error": True,
            })
        except Exception:
            pass  # 日志写不进去也不能让任务挂
