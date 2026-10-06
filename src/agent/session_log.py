"""
============================================
SessionLog —— 事件溯源会话日志（T3）
============================================
对齐 dsh 的核心设计："模型可见即可从日志重建"。

- 会话是 append-only 事件流（SQLite events 表，scope="chat"）
- 模型历史（messages）不是存储的事实，而是从事件流投影（derive_messages）
- HITL 中断快照也走同一事件流（scope="interrupt"，session_id 列存 thread_id），
  使"重启即丢"的 InterruptStore 具备恢复能力（save/pending/recover）

与旧 JSON 快照（src/session_store.py）双轨并行：过渡期两者同时生长，
JSON 仍是加载路径；事件日志是并行生长的真实来源（T6 起 agent 循环本体
负责写入，derive 成为唯一投影）。

事件类型（模块级常量）：
    TURN_START         turn/start          轮次开始（不参与投影）
    TURN_END           turn/end            轮次结束（不参与投影）
    USER_MSG           user/message        payload: content
    ASSISTANT_MSG      assistant/message   payload: content, tool_calls?, reasoning?
    TOOL_CALL          tool/call           payload: tool_call_id, name, args
    TOOL_RESULT        tool/result         payload: tool_call_id, name, content, ok?
    INTERRUPT_REQUESTED  interrupt/requested  payload: 完整 InterruptSnapshot 序列化
    INTERRUPT_RESOLVED   interrupt/resolved   payload: thread_id, decision, reason?
    COMPACT_APPLIED    compact/applied     payload: summary, original_count,
                                              compacted_count, kept_messages?
                                              （R2 起 kept_messages = 压缩后保留区
                                               消息列表，lc_to_dict 规范化；derive
                                               据此重建保留区投影）
    LLM_ERROR          llm/error           LLM 调用失败的旁路审计事件（P2-9，
                                              错误文本不进持久历史）；payload:
                                              content, error。derive 不识别、
                                              不参与投影（向前兼容忽略）

冷归档（P3）：COMPACT_APPLIED payload 内嵌 kept_messages → 最后一条
COMPACT_APPLIED 之前的事件对 derive 投影冗余。archive_compacted_events
把这段前缀导出到会话旁冷文件（{sid}.events-archive.jsonl，与 JSON 快照
同目录）后删热行（先冷后热、fsync 落定再删）；compact 及之后永远留热表。
消费方经 load_events_with_archive 读"冷+热按 id 合并"视图（事件对话框 /
fork / CLI /events），derive_messages 维持纯热表读——归档段对投影冗余，
投影逐字不变（不变量测试锚点）。归档由 StorageHousekeeping 按 tick 驱动。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from src.session_store import lc_to_dict, load_message

if TYPE_CHECKING:  # 仅类型标注用，避免运行期循环 import
    from src.agent.context import CompactResult
    from src.agent.hitl import InterruptSnapshot, InterruptStore
    from src.storage.base import EventLogProtocol

logger = logging.getLogger("hermes.agent.session_log")

# ── scope 约定 ──
SCOPE_CHAT = "chat"          # 会话事件流（session_id 列存会话 ID）
SCOPE_INTERRUPT = "interrupt"  # 中断事件流（session_id 列存 thread_id）

# R3-16 waker 会话隔离：waker:/wakerflow: 前缀的 sid 是无人值守任务的
# 私有事件流，与 chat 会话分 scope 存储（互不串扰、互不可枚举）。
# 读侧（events/derive_messages）双 scope 合并按 id 保序，兼容 R3 之前
# 写在 chat scope 的旧 waker 事件。
SCOPE_WAKER = "waker"
_WAKER_SID_PREFIXES = ("waker:", "wakerflow:")


def sid_scope(session_id: str) -> str:
    """会话 ID → 事件 scope（waker:/wakerflow: 前缀 → "waker"，否则 "chat"）。"""
    if isinstance(session_id, str) and session_id.startswith(_WAKER_SID_PREFIXES):
        return SCOPE_WAKER
    return SCOPE_CHAT

# ── 事件类型常量 ──
TURN_START = "turn/start"
TURN_END = "turn/end"
USER_MSG = "user/message"
ASSISTANT_MSG = "assistant/message"
TOOL_CALL = "tool/call"          # payload: tool_call_id, name, args
TOOL_RESULT = "tool/result"      # payload: tool_call_id, name, content, ok?
INTERRUPT_REQUESTED = "interrupt/requested"
INTERRUPT_RESOLVED = "interrupt/resolved"  # payload: thread_id, decision, reason?
COMPACT_APPLIED = "compact/applied"        # payload: summary, original_count, compacted_count, kept_messages?
LLM_ERROR = "llm/error"                    # payload: content, error —— LLM 调用失败旁路事件（仅 UI 投影还原，LLM 投影忽略）
TRUNCATED = "session/truncated"            # payload: kept_messages, original_count, truncated_count
                                           # —— 编辑重发的截断标记（2026-09-19）：derive 在此
                                           # 重置投影为 kept_messages 前缀（无 compact 的 system
                                           # 摘要行）；与 COMPACT_APPLIED 同构但语义诚实，不触发
                                           # 冷归档扫描（housekeeping 只认 COMPACT_APPLIED）

#: 占位 tool 消息内容：悬空 tool_calls（中断/取消）无结果时由投影合成
_DANGLING_PLACEHOLDER = "(中断，无结果)"

# ── 冷归档（P3 双轨收口）────────────────────────────────────────
# 依据：COMPACT_APPLIED 的 payload 内嵌 kept_messages，derive_messages 在
# 每条 COMPACT_APPLIED 处重置投影 → 最后一条 COMPACT_APPLIED 之前的事件对
# 消息重建冗余。归档策略：把最后 COMPACT_APPLIED 之前的热表行导出到会话
# 旁冷文件（JSONL，逐行带原 id 保全局序）后从热表删除；compact 事件本身
# 及之后永远留热表。三个消费方（UI 事件对话框 / fork / CLI /events）经
# load_events_with_archive 走"冷+热按 id 合并"视图，读侧无感。

#: 冷归档文件名后缀：{sid}.events-archive.jsonl，与 JSON 快照同目录
ARCHIVE_FILE_SUFFIX = ".events-archive.jsonl"

#: 文件名不安全字符（Windows 保留字符 + 路径分隔符；":" 兼顾 waker: 前缀 sid
#: 与盘符语义——这类 sid 的冷文件直接禁用，读侧退化为纯热读）
_UNSAFE_FILENAME_CHARS = set('\\/:*?"<>|')


def _archive_sid_ok(session_id: str) -> bool:
    """sid 能否直接用作冷归档文件名（文件系统安全 + 非 waker 私有流）。"""
    if not session_id or session_id.startswith(_WAKER_SID_PREFIXES):
        return False
    return not (_UNSAFE_FILENAME_CHARS & set(session_id))


def _project_kept_message(m: dict) -> dict:
    """kept_messages 里的单条消息 → 投影（只保留模型可见键）。

    与 derive_messages 的逐事件投影同构：assistant 带 tool_calls、tool 带
    tool_call_id；system（历史代摘要）按原样保留 role/content。
    """
    role = m.get("role", "")
    out: dict = {"role": role, "content": m.get("content", "")}
    if role == "assistant" and m.get("tool_calls"):
        out["tool_calls"] = m["tool_calls"]
    if role == "tool":
        out["tool_call_id"] = m.get("tool_call_id", "")
    return out


def build_compact_applied_payload(result: "CompactResult", kept_source: list) -> dict:
    """构造 COMPACT_APPLIED durable 事件的 payload（compact 三处写侧共用工厂）。

    - result: ContextManager.compact_messages() 的返回值（取 summary /
      original_count / compacted_count 三个精确计数字段）
    - kept_source: 保留区消息来源（OpenAI dict 列表）——先剥掉头部的本轮
      摘要 system 消息（仅当首元素是 role="system" 的 dict），再逐条
      lc_to_dict 规范化（剔除 compact_id 等运行期附带键）

    lc_to_dict 复用本模块顶部既有的 src.session_store import——session_log
    → session_store 单向依赖（session_store 对本模块只在函数内延迟 import，
    无环），无需再走函数内延迟 import。
    """
    kept_src = list(kept_source)
    if kept_src and isinstance(kept_src[0], dict) and kept_src[0].get("role") == "system":
        kept_src = kept_src[1:]
    return {
        "summary": result.summary,
        "original_count": result.original_count,
        "compacted_count": result.compacted_count,
        "kept_messages": [lc_to_dict(m) for m in kept_src],
    }


class SessionLog:
    """会话事件日志：append 事件 + 从事件投影 messages + HITL 持久化。

    用法：
        log = SessionLog()                      # 默认 SQLiteProvider（data/hermes.db）
        log.append(sid, USER_MSG, {"content": "hi"})
        msgs = log.derive_messages(sid)         # OpenAI 格式投影
    """

    def __init__(self, provider: "EventLogProtocol | None" = None) -> None:
        if provider is None:
            from src.storage.sqlite_provider import SQLiteProvider
            provider = SQLiteProvider()
        self.provider = provider

    # ── 会话事件（chat / waker 按 sid 前缀路由，R3-16）──

    def append(self, session_id: str, type_: str, payload: dict) -> int:
        """追加一条会话事件，返回事件 id。

        scope 路由：sid 以 waker:/wakerflow: 开头 → scope="waker"（无人值守
        任务私有流），否则 scope="chat"。写侧单一入口，agent/路由层无感知。
        """
        return self.provider.append_event(sid_scope(session_id), session_id, type_, payload)

    def events(self, session_id: str) -> list[dict]:
        """该会话的全部事件（id 升序）。每行 {id, scope, session_id, type, payload, ts}。

        双 scope 合并（R3-16 旧数据兼容）：chat + waker 同 sid 的事件都查，
        按全局自增 id 排序——R3 前写在 chat scope 的 waker 会话仍可读。
        """
        merged = (
            self.provider.iter_events(SCOPE_CHAT, session_id)
            + self.provider.iter_events(SCOPE_WAKER, session_id)
        )
        merged.sort(key=lambda e: e["id"])
        return merged

    def purge_session(self, session_id: str) -> int:
        """删除该会话的全部事件（E1：会话删除时同步清理，防止 events/fork 复活）。

        双 scope 都清（兼容 R3 前写在 chat scope 的 waker 会话数据）；
        冷归档文件一并删除（同 E1 语义——只清热表会让 fork 从冷区复活）。
        """
        purge = getattr(self.provider, "purge_events", None)
        if purge is None:
            return 0
        n = purge(SCOPE_CHAT, session_id)
        n += purge(SCOPE_WAKER, session_id)
        if _archive_sid_ok(session_id):
            try:
                self.archive_path(session_id).unlink(missing_ok=True)
            except OSError:
                logger.warning(f"冷归档文件删除失败（忽略）: sid={session_id}", exc_info=True)
        return n

    # ── 冷归档（P3）：最后 COMPACT_APPLIED 之前的事件导出冷文件并删热行 ──

    @staticmethod
    def archive_path(session_id: str) -> Path:
        """会话冷归档文件路径：{会话数据目录}/{sid}.events-archive.jsonl。

        与 JSON 快照同目录（src.session_store.SESSIONS_DIR，调用期读取，
        测试可 monkeypatch）；waker: 前缀等文件名不安全的 sid 不落冷文件。
        """
        from src.session_store import SESSIONS_DIR
        return SESSIONS_DIR / f"{session_id}{ARCHIVE_FILE_SUFFIX}"

    def _read_archive(self, session_id: str) -> list[dict]:
        """读冷归档 JSONL 为事件行列表（id 升序由写侧保证，此处不再排序）。

        容错：文件不存在 → []；损坏行（崩溃窗口的半截行等）跳过并 warn，
        不让单行毒化整个合并视图。
        """
        if not _archive_sid_ok(session_id):
            return []
        path = self.archive_path(session_id)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError:
            logger.warning(f"冷归档文件读取失败（按无冷区处理）: {path}", exc_info=True)
            return []
        out: list[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(f"冷归档文件存在损坏行（跳过）: {path}")
                continue
            if isinstance(ev, dict) and "id" in ev:
                out.append(ev)
        return out

    def load_events_with_archive(self, session_id: str) -> list[dict]:
        """冷区 + 热表合并事件视图（id 升序、按 id 去重）。

        三个消费方（UI 事件对话框 / fork 截断 / CLI /events）统一入口：
        归档只搬走"最后 COMPACT_APPLIED 之前"的行，合并后与归档前的
        events() 逐条等价（id 全局唯一，崩溃窗口的冷热并存由 id 去重兜底，
        同 id 以热表行为准）。provider 不支持归档或从未归档时等价于 events()。
        """
        hot = self.events(session_id)
        cold = self._read_archive(session_id)
        if not cold:
            return hot
        merged: dict[int, dict] = {e["id"]: e for e in cold}
        for e in hot:
            merged[e["id"]] = e
        return [merged[k] for k in sorted(merged)]

    def archive_compacted_events(self, session_id: str) -> int:
        """把最后 COMPACT_APPLIED 之前的热事件导出冷文件并从热表删除。

        返回本次归档行数（0 = 无 compact / 无可归档行 / provider 不支持 /
        sid 不适合落冷文件——四种情况都不动库不动文件，幂等可重入）。

        原子有序约定：**先冷后热**——导出行整批 append 写入 JSONL 并
        flush + fsync 落定之后，才调 delete_events_before 删热行。两步之间
        崩溃的后果是冷热并存（合并读按 id 去重，读侧仍正确），下一次归档
        会把残留热行补删（写侧先按已有冷文件 id 去重，不会写重）。导出与
        删除之间并发追加的事件必然 id > compact_id（id 全局自增单调），
        不在删除范围内，不会误删。
        """
        provider = self.provider
        find_last = getattr(provider, "last_event_id_of_type", None)
        delete_before = getattr(provider, "delete_events_before", None)
        if find_last is None or delete_before is None:
            return 0  # 自定义 provider 缺归档原语 → 归档禁用（降级安全）
        scope = sid_scope(session_id)
        compact_id = find_last(scope, session_id, COMPACT_APPLIED)
        if compact_id <= 0:
            return 0  # 无 COMPACT_APPLIED：会话不动
        if not _archive_sid_ok(session_id):
            logger.debug(f"sid 不适合落冷归档文件，跳过: {session_id!r}")
            return 0
        rows = [e for e in provider.iter_events(scope, session_id)
                if e["id"] < compact_id]
        if not rows:
            return 0  # 已归档过（幂等）：该 compact 之前无热行

        existing_ids = {e["id"] for e in self._read_archive(session_id)}
        fresh = [e for e in rows if e["id"] not in existing_ids]
        if fresh:
            path = self.archive_path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            blob = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in fresh)
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
        deleted = delete_before(scope, session_id, compact_id)
        if deleted != len(rows):
            logger.warning(
                f"冷归档删热行数与导出数不一致（并发 purge?）: sid={session_id} "
                f"exported={len(rows)} deleted={deleted}")
        logger.info(
            f"会话事件冷归档: sid={session_id} 归档 {len(rows)} 行 "
            f"(compact_id={compact_id} 之前；冷文件 {self.archive_path(session_id).name})")
        return len(rows)

    def derive_messages(self, session_id: str, include_reasoning: bool = False) -> list[dict]:
        """从事件流投影模型可见的 messages（OpenAI 格式，按事件 id 升序）。

        include_reasoning=False（默认）：assistant 只带 content/tool_calls——
        本列表喂 LLM 历史，多余键可能被严格网关拒绝，且是不变量测试的锚点。
        include_reasoning=True：assistant 额外透传 payload 里的 reasoning
        （durable 事件里有存）——仅供 UI 历史回放序列化使用，勿喂 LLM。

        投影规则：
        - user/message      → {"role": "user", "content": ...}
        - assistant/message → {"role": "assistant", "content": ...}
                              有 tool_calls 时附 "tool_calls"（OpenAI 格式透传）
        - tool/result       → {"role": "tool", "tool_call_id": ..., "content": ...}
                              （与前面的 tool/call 经 tool_call_id 配对；tool/call
                                本身不投影，调用信息由 assistant 的 tool_calls 承载）
        - compact/applied   → 丢弃其之前的全部投影，重置为
                              [system:summary] + kept_messages 投影
                              （kept_messages = 压缩保留区，R2 起写入；旧事件
                                无该字段时只留 summary，行为兼容）
        - session/truncated → 丢弃其之前的全部投影，重置为 kept_messages
                              投影（编辑重发的截断标记，2026-09-19；无
                              system 摘要行，与 compact/applied 同构）
        - llm/error         → 仅 UI 视图（include_reasoning=True）投影为
                              {"role": "assistant", "content": ..., "llm_error":
                              True}；LLM 视图忽略（模型看不到失败文本，P2-9）
        - turn/*            → 不参与投影
        - 其余未知类型      → 忽略（向前兼容）

        悬空 tool_calls 修复（R2）：中断/审批拒绝/取消路径会在事件流留下
        assistant(tool_calls) 而无配对 tool/result，直接投影会得到非法消息
        序列（LLM 400、会话永久不可聊）。投影时按 call_id 挂起跟踪：下一条
        user/assistant 消息或流末尾仍未收到结果的，合成
        {"role":"tool","tool_call_id":id,"content":"(中断，无结果)"} 占位。
        compact/applied 之前的悬空已被截断，不补。孤儿 tool/result（其
        assistant 不在投影内，如已被 compact 截断）同样丢弃不投影。
        """
        projections: list[dict] = []
        # 挂起的 tool_call id（按出现顺序）：收到对应 tool/result 移除
        pending_call_ids: list[str] = []

        def _flush_pending() -> None:
            """为仍未收到结果的挂起 call_id 合成占位 tool 消息。"""
            for call_id in pending_call_ids:
                projections.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _DANGLING_PLACEHOLDER,
                })
            pending_call_ids.clear()

        for ev in self.events(session_id):
            etype = ev.get("type", "")
            payload = ev.get("payload") or {}
            if etype == USER_MSG:
                _flush_pending()
                projections.append({"role": "user", "content": payload.get("content", "")})
            elif etype == ASSISTANT_MSG:
                _flush_pending()
                # 注意：投影保持 OpenAI 最小字段（content/tool_calls）。
                # durable payload 里的 reasoning 默认不透传（LLM 历史纯净 +
                # 不变量测试锚点）；include_reasoning=True 的 UI 视图才带上。
                msg: dict = {"role": "assistant", "content": payload.get("content", "")}
                if include_reasoning and payload.get("reasoning"):
                    msg["reasoning"] = payload["reasoning"]
                if payload.get("tool_calls"):
                    msg["tool_calls"] = payload["tool_calls"]
                    for tc in payload["tool_calls"]:
                        call_id = tc.get("id", "") if isinstance(tc, dict) else ""
                        if call_id:
                            pending_call_ids.append(call_id)
                projections.append(msg)
            elif etype == TOOL_RESULT:
                call_id = payload.get("tool_call_id", "")
                if call_id in pending_call_ids:
                    pending_call_ids.remove(call_id)
                    projections.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": payload.get("content", ""),
                    })
                # 不在挂起集（孤儿结果）→ 丢弃，避免无配对 assistant 的 tool
            elif etype == COMPACT_APPLIED:
                # 历史压缩：摘要之前的投影全部作废（悬空挂起一并截断，不补）
                projections = [{"role": "system", "content": payload.get("summary", "")}]
                kept = payload.get("kept_messages")
                if isinstance(kept, list):
                    projections.extend(
                        _project_kept_message(m) for m in kept if isinstance(m, dict)
                    )
                pending_call_ids.clear()
            elif etype == LLM_ERROR and include_reasoning:
                # LLM 调用失败留底：仅 UI 视图投影（带 llm_error 标记，前端
                # 历史按错误样式渲染、build_llm_messages 投影过滤）；LLM 视图
                # （默认）忽略——模型永远看不到失败文本（P2-9）。悬空挂起
                # 先补占位，保证消息序列合法。
                _flush_pending()
                projections.append({
                    "role": "assistant",
                    "content": payload.get("content", ""),
                    "llm_error": True,
                })
            elif etype == TRUNCATED:
                # 编辑重发的截断点（2026-09-19）：与 compact/applied 同构的
                # 「重置投影」语义，但不产生 system 摘要行——kept_messages
                # 就是截断后的全部历史。悬空挂起一并截断（被截掉的尾巴不补
                # 占位，与 compact 处之前的悬空同规则）。
                projections = [
                    _project_kept_message(m) for m in
                    (payload.get("kept_messages") or [])
                    if isinstance(m, dict)
                ]
                pending_call_ids.clear()
            # turn/* 与未知类型：忽略（向前兼容）
        _flush_pending()
        return projections

    # ── HITL 中断持久化（scope="interrupt"，session_id 列存 thread_id）──

    def save_interrupt(self, snapshot: "InterruptSnapshot") -> None:
        """把中断快照完整序列化成 interrupt/requested 事件。"""
        payload = {
            "thread_id": snapshot.thread_id,
            "messages": [lc_to_dict(m) for m in snapshot.messages],
            "permission_mode": snapshot.permission_mode_at_interrupt,
            "pending_args": snapshot.pending_args,
            "tool_call_id": snapshot.pending_tool_call_id,
            "tool_name": snapshot.pending_tool_name,
            "payload": snapshot.pending_payload,
        }
        self.provider.append_event(SCOPE_INTERRUPT, snapshot.thread_id, INTERRUPT_REQUESTED, payload)

    def resolve_interrupt(self, thread_id: str, decision: str, reason: str = "") -> None:
        """记录中断已被消费（approve/reject/auto-allow 均算 resolved）。"""
        payload = {"thread_id": thread_id, "decision": decision}
        if reason:
            payload["reason"] = reason
        self.provider.append_event(SCOPE_INTERRUPT, thread_id, INTERRUPT_RESOLVED, payload)

    def pending_interrupts(self) -> "list[InterruptSnapshot]":
        """扫 scope=interrupt 全事件，还原仍待处理的中断快照。

        判定：每个 thread_id 的最后一个事件若是 interrupt/requested → pending；
        是 interrupt/resolved（或未知）→ 已消费。requested 反序列化回
        InterruptSnapshot（messages 经 load_message 规范化为 OpenAI dict）。

        R3-16：waker:/wakerflow: 前缀的 thread 是无人值守任务的审批中断，
        不参与恢复——chat worker 启动时不把 waker 中断重导入内存 store
        （事件仍留在 interrupt scope 供审计）。
        """
        by_thread: dict[str, dict[str, "InterruptSnapshot | None"]] = {}
        for ev in self.provider.iter_events(SCOPE_INTERRUPT):
            thread_id = ev.get("session_id", "")
            if not thread_id:
                continue
            if thread_id.startswith(_WAKER_SID_PREFIXES):
                continue  # waker/flow thread 不恢复（见 docstring）
            if ev.get("type") == INTERRUPT_REQUESTED:
                by_thread[thread_id] = {"snap": self._snapshot_from_event(ev)}
            else:
                # resolved（或任何后续事件）→ 该 thread 不再 pending
                by_thread[thread_id] = {"snap": None}
        return [v["snap"] for v in by_thread.values() if v["snap"] is not None]

    def recover_into(self, store: "InterruptStore") -> int:
        """把 pending 中断恢复进 InterruptStore（内存）。

        用 store.restore()（只进内存，不回写事件日志），避免恢复动作
        本身再产生重复的 requested 事件。返回恢复条数。
        """
        from src.agent.hitl import InterruptStore  # noqa: F401 （类型确认）

        recovered = 0
        for snap in self.pending_interrupts():
            store.restore(snap)
            recovered += 1
        if recovered:
            logger.info(f"SessionLog 恢复 {recovered} 个 pending 中断进内存 store")
        return recovered

    @staticmethod
    def _snapshot_from_event(ev: dict) -> "InterruptSnapshot":
        """interrupt/requested 事件 payload → InterruptSnapshot。"""
        from src.agent.hitl import InterruptSnapshot

        payload = ev.get("payload") or {}
        messages = []
        for msg_data in payload.get("messages", []):
            msg = load_message(msg_data)
            if msg is not None:
                messages.append(msg)
        return InterruptSnapshot(
            thread_id=payload.get("thread_id", ev.get("session_id", "")),
            messages=messages,
            pending_args=payload.get("pending_args") or {},
            pending_tool_call_id=payload.get("tool_call_id", ""),
            pending_tool_name=payload.get("tool_name", ""),
            pending_payload=payload.get("payload") or {},
            permission_mode_at_interrupt=payload.get("permission_mode", "before_changes"),
        )
