"""
============================================
HITL 异常协议 —— 基于异常的暂停/恢复（取代旧图引擎的 interrupt/resume/checkpoint）
============================================
V3 的权限求值在 executor 之前——requireApproval 触发时 executor 还没跑。
approve 后 executor 第一次执行。不存在"执行两次"或"工具拿不到审批值"的问题
（这是旧图引擎 interrupt 语义下的痛点，V3 架构自然解之）。

核心三件套：
    InterruptSignal     工具/Registry 抛出的暂停信号
    InterruptSnapshot   一次中断的内存快照（取代 MemorySaver checkpoint）
    InterruptStore      thread_id → InterruptSnapshot 的内存存储

P3-2 公共类型下沉：InterruptSignal（暂停信号）与其 payload 约定键
COMPLETED_TOOL_MESSAGES_KEY 已搬至 src/types/interrupt.py，此处 re-export
兼容既有 import 路径；InterruptSnapshot / InterruptStore 仍在本模块
（依赖 session_log 等 agent 运行时设施，不是跨包公共类型）。

"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.types.interrupt import COMPLETED_TOOL_MESSAGES_KEY as COMPLETED_TOOL_MESSAGES_KEY
from src.types.interrupt import InterruptSignal as InterruptSignal

if TYPE_CHECKING:  # 仅类型标注，避免运行期循环 import（session_log 反向依赖本模块）
    from src.agent.session_log import SessionLog

logger = logging.getLogger("hermes.agent.hitl")

__all__ = [
    "InterruptSignal",
    "COMPLETED_TOOL_MESSAGES_KEY",
    "InterruptSnapshot",
    "InterruptStore",
]


@dataclass
class InterruptSnapshot:
    """一次中断的内存快照（取代 MemorySaver 的 checkpoint）。

    存储暂停时的完整状态，供 resume 路径恢复执行。
    内存级——进程重启即丢（对齐现有 MemorySaver 行为）。
    """

    thread_id: str
    """会话线程 ID（与 stream_invoke 的 thread_id 对齐）。"""

    messages: list = field(default_factory=list)
    """暂停时的完整 messages 列表（深拷贝，避免后续修改污染快照）。"""

    pending_args: dict[str, Any] = field(default_factory=dict)
    """触发中断的工具调用参数（resume 时用这些 args 重新调用 executor）。"""

    pending_tool_call_id: str = ""
    """触发中断的 tool_call ID（用于构造 ToolMessage）。"""

    pending_tool_name: str = ""
    """触发中断的工具名（日志/调试用）。"""

    pending_payload: dict[str, Any] = field(default_factory=dict)
    """InterruptSignal 的 payload（action/details，用于审批面板展示）。"""

    permission_mode_at_interrupt: str = "before_changes"
    """暂停时的 mode。用于检测 resume 时是否切换了 mode。
    若 mode 变了（如切到 full_access），重新求值权限可能自动放行。"""

    @classmethod
    def create(
        cls,
        thread_id: str,
        messages: list,
        pending_args: dict[str, Any],
        tool_call_id: str,
        tool_name: str,
        payload: dict[str, Any],
        permission_mode: str,
    ) -> "InterruptSnapshot":
        """构造快照（自动深拷贝 messages 防污染）。"""
        return cls(
            thread_id=thread_id,
            messages=copy.deepcopy(messages),
            pending_args=dict(pending_args),
            pending_tool_call_id=tool_call_id,
            pending_tool_name=tool_name,
            pending_payload=dict(payload),
            permission_mode_at_interrupt=permission_mode,
        )


class InterruptStore:
    """中断状态存储。thread_id → InterruptSnapshot。

    不带 session_log（默认）：纯内存 dict（等价 MemorySaver，重启即丢），
    行为与抽取前完全一致。

    带 session_log（T3）：内存 dict 仍是快路径；save() 同步写
    interrupt/requested 事件，pop() 同步写 interrupt/resolved 事件，
    使中断状态可跨进程重启恢复（worker 启动时 SessionLog.recover_into
    把 pending 快照回填进本 store）。持久化失败只降级告警，绝不阻塞
    审批主流程。

    线程安全说明：调用方（stream_invoke）通常是单线程驱动 generator，
    不会并发访问同一 thread_id。跨线程访问时调用方自行加锁。
    """

    def __init__(self, session_log: "SessionLog | None" = None) -> None:
        self._store: dict[str, InterruptSnapshot] = {}
        self._log = session_log

    def save(self, snapshot: InterruptSnapshot) -> None:
        """存快照（覆盖同 thread_id 的旧快照）。带 log 时同步持久化。"""
        self._store[snapshot.thread_id] = snapshot
        if self._log is not None:
            try:
                self._log.save_interrupt(snapshot)
            except Exception:
                logger.warning(
                    f"interrupt/requested 事件写入失败（降级为纯内存）: "
                    f"thread_id={snapshot.thread_id}",
                    exc_info=True,
                )

    def get(self, thread_id: str) -> InterruptSnapshot | None:
        """查快照（不删除）。"""
        return self._store.get(thread_id)

    def pop(self, thread_id: str, decision: str = "", reason: str = "") -> InterruptSnapshot | None:
        """取快照并删除（resume 成功后调用）。

        带 log 时同步写 interrupt/resolved 事件（decision/reason 记录审批
        结果；默认空串兼容旧调用——取不到 decision 时仅标记"已消费"）。
        thread_id 无 pending 快照时不写事件（避免为不存在的中断记 resolved）。
        """
        snapshot = self._store.pop(thread_id, None)
        if snapshot is not None and self._log is not None:
            try:
                self._log.resolve_interrupt(thread_id, decision, reason)
            except Exception:
                logger.warning(
                    f"interrupt/resolved 事件写入失败（降级为纯内存）: thread_id={thread_id}",
                    exc_info=True,
                )
        return snapshot

    def restore(self, snapshot: InterruptSnapshot) -> None:
        """恢复路径专用：只进内存，不写事件日志。

        SessionLog.recover_into 用它把 pending 快照回填，避免恢复动作
        再产生重复的 interrupt/requested 事件。
        """
        self._store[snapshot.thread_id] = snapshot

    def has_pending(self, thread_id: str) -> bool:
        """是否有待处理的审批。"""
        return thread_id in self._store

    def clear(self) -> None:
        """清空所有快照（会话重置时调用）。"""
        self._store.clear()
