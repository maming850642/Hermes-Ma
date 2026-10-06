"""
============================================
任务规划工具
============================================
允许 LLM 创建、更新、标记完成子任务。

2026-06-11: 初始实现
2026-07-09: 重复检测——当传入的 todos 与上次完全相同时，返回"已规划，继续执行"
            而非相同的 summary，给模型明确的前进信号，防止 write_todos 死循环。
2026-09-05: 重复检测签名改 per-session 隔离——_last_todos_sig 此前是进程级
            单值，跨会话泄漏：A 会话写的规划会让 B 会话（如 waker 定时任务）
            首次写入相同结构的 todos 就吃到"与上次完全相同"的假前进信号。
            ToolContext 无会话标识字段，按 remember.py 同款 contextvar
            user_id 维度隔离（stream_invoke 每轮 set，waker/子代理各自
            set 自己的 user_id），并发线程经 copy_context 天然继承。
"""

import logging
logger = logging.getLogger("hermes.tools.write_todos")

# per-session 快照：上次 write_todos 写入的完整列表（用于重复检测），
# 按 user_id 分桶（进程内 dict；user 数有限，无淘汰也不会无界增长到
# 有感知的程度）。用 (id, status, content) 三元组的排序列表做签名比较，
# 避免 dict 顺序差异。
_last_todos_sig_by_user: dict[str, tuple] = {}


def _todos_signature(todos: list[dict]) -> tuple:
    """生成 todos 签名用于重复检测（排序后取 id+status+content）。"""
    return tuple(sorted(
        (t.get("id", ""), t.get("status", "pending"), t.get("content", ""))
        for t in todos
    ))


def _session_sig_key(ctx=None) -> str:
    """重复检测签名的会话隔离键。

    ToolContext（src/tools/context.py）只有 permission_mode / caller_context /
    allowed_tools / progress_cb / should_cancel，无会话或用户标识——
    取 remember.py 的 user_id contextvar（stream_invoke 每轮 set）；
    取不到（裸调用/测试）回落 "anonymous"，与旧进程级单值相比仍隔离了
    绝大多数真实并发场景。
    """
    from src.tools.remember import get_current_user_id
    return get_current_user_id() or "anonymous"


def write_todos(todos: list[dict]) -> dict:
    """
    创建或更新待办事项列表。传入完整的待办列表（全量替换，非增量）。

    每个待办项包含:
    - id: 唯一标识符（字符串）
    - content: 任务内容描述
    - status: 任务状态（pending/in_progress/completed/cancelled）

    Args:
        todos: 完整的待办事项列表

    Returns:
        更新后的待办列表状态摘要
    """
    # 校验状态值
    valid_statuses = {"pending", "in_progress", "completed", "cancelled"}
    for item in todos:
        status = item.get("status", "pending")
        if status not in valid_statuses:
            item["status"] = "pending"

    # 2026-06-11: 校验 in_progress 只能有一个
    in_progress_count = sum(1 for t in todos if t.get("status") == "in_progress")
    if in_progress_count > 1:
        # 只保留第一个为 in_progress，其余改为 pending
        first_found = False
        for t in todos:
            if t.get("status") == "in_progress":
                if first_found:
                    t["status"] = "pending"
                first_found = True

    logger.debug(f"[DEBUG] write_todos: 更新 {len(todos)} 个待办项")

    # 返回格式：包含 state 更新指令和文本摘要
    pending = sum(1 for t in todos if t.get("status") == "pending")
    in_progress = sum(1 for t in todos if t.get("status") == "in_progress")
    completed = sum(1 for t in todos if t.get("status") == "completed")
    cancelled = sum(1 for t in todos if t.get("status") == "cancelled")

    summary = f"待办列表已更新：共 {len(todos)} 项（进行中: {in_progress}, 待处理: {pending}, 已完成: {completed}, 已取消: {cancelled}）"

    return {
        "todos": todos,
        "summary": summary,
    }


# ════════════════════════════════════════════════════════════════
# V3 PythonExecutor 入口
# ════════════════════════════════════════════════════════════════

def _execute_write_todos(todos: list[dict], *, ctx=None):
    """V3 PythonExecutor 入口。返回 ToolResult（含 state_updates）。

    重复检测：如果传入的 todos 与本会话上次完全相同（id+status+content），
    返回"已规划，继续执行"提示而非相同的 summary——给模型明确的前进信号，
    防止小模型（如 Ornith-9B）困在"重复规划"的吸引子里。
    签名按 user 维度分桶（见模块 docstring：进程级单值跨会话泄漏）。
    """
    from src.types import ToolResult

    result = write_todos(todos)
    new_sig = _todos_signature(todos)
    key = _session_sig_key(ctx)
    last_sig = _last_todos_sig_by_user.get(key)

    if last_sig is not None and new_sig == last_sig:
        # 完全相同的重复调用 → 给前进信号
        # 找到 in_progress 的任务，提示模型去执行它
        in_progress_items = [t for t in todos if t.get("status") == "in_progress"]
        if in_progress_items:
            hint = f'当前进行中的任务是「{in_progress_items[0].get("content", "")}」，请开始执行它。'
        else:
            first_pending = next((t for t in todos if t.get("status") == "pending"), None)
            if first_pending:
                hint = f'请开始执行「{first_pending.get("content", "")}」，并用 write_todos 把它标记为 in_progress。'
            else:
                hint = "所有任务已完成，请总结结果。"

        _last_todos_sig_by_user[key] = new_sig
        return ToolResult(
            content=f"⚠️ 待办列表与上次完全相同，规划已完成，不要重复规划。{hint}",
            state_updates={"todos": result["todos"]},
        )

    _last_todos_sig_by_user[key] = new_sig
    return ToolResult(
        content=result["summary"],
        state_updates={"todos": result["todos"]},
    )
