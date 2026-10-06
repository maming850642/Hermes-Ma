"""
============================================
ContextManager - 上下文管理模块
============================================
负责上下文消息的构建、压缩和窗口管理。
独立模块，遵循单一职责原则。

职责：
- 构建 LLM 消息列表（System Prompt + 历史 + 当前输入）
- 管理消息窗口（截断过长的历史）
- 执行上下文压缩（将早期消息摘要化，释放 token 空间）

T7 dict 迁移：消息统一用 OpenAI dict 格式（{"role": "system"|"user"|
"assistant"|"tool", "content": ...}，assistant 可带 "tool_calls"，tool 带
"tool_call_id"）。不再依赖任何消息类库。
"""

import logging
import uuid
from dataclasses import dataclass

from config import get_settings
from src.prompts import build_system_prompt

logger = logging.getLogger("hermes.agent.context")


@dataclass
class CompactResult:
    """压缩操作的结果"""
    compressed_messages: list
    """压缩后的消息列表（OpenAI dict）"""

    summary: str
    """生成的摘要文本"""

    original_count: int
    """压缩前的消息总数"""

    compacted_count: int
    """被压缩的早期消息数"""


class ContextManager:
    """
    上下文管理器：负责消息列表的构建、窗口管理和上下文压缩。

    压缩流程（统一入口）：
        compact_messages() 是压缩的唯一执行点。
        - Agent 自动触发：LLM 调用 compact_conversation 工具 → 返回信号 → 调用此方法
        - 用户手动触发：/compact 命令 → CLI 调用此方法

    Attributes:
        settings: 全局配置
    """

    def __init__(self):
        self.settings = get_settings()

    # ============================================
    # 消息构建
    # ============================================

    def build_llm_messages(
        self,
        user_id: str,
        current_input: str,
        messages: list[dict],
        memories: list[str] | None = None,
        todos: list[dict] | None = None,
        role: str | None = None,
        waker_persona: str | None = None,
    ) -> list[dict]:
        """
        构建 LLM 调用所需的完整消息列表（OpenAI dict 格式）。

        Args:
            user_id: 当前用户 ID
            current_input: 当前用户输入
            messages: 历史消息列表（OpenAI dict）
            memories: 检索到的长期记忆
            todos: 当前待办事项
            role: 角色名(M1-3)。None=默认人格;指定则切换角色卡人格。
            waker_persona: 数字员工人格段（runner 跑 waker 时注入）。None=不注入。

        Returns:
            list[dict]: 完整的 LLM 消息列表
                [{"role": "system"}, ...历史, {"role": "user"}(当前输入)]
        """
        system_prompt = build_system_prompt(
            user_id, memories or [], todos or [],
            role=role, waker_persona=waker_persona,
        )

        # llm_error 标记消息（LLM 调用失败留底，仅供 UI 历史回看）不进
        # LLM payload——P2-9 语义保持：模型永远看不到失败文本。在窗口
        # 截断前过滤（该消息无 tool_calls，不参与配对完整性判断）。
        messages = [m for m in messages if not m.get("llm_error")]

        # 窗口截断，避免历史过长超出 LLM 上下文窗口
        # P2-10：旧 config 缺 max_short_term_messages 键时 getattr 兜底 20
        # （与 agent_v3 同法；项目支持手写精简 config，缺键不该每轮
        # AttributeError 崩溃）
        max_msgs = int(getattr(self.settings, "max_short_term_messages", 20) or 20) * 2
        if len(messages) > max_msgs:
            truncated = messages[-max_msgs:]
            # 避免切断"工具调用 → 工具结果"的配对（两个方向都防）：
            # 1) 截断后首条是带 tool_calls 的 assistant，其对应的 tool 消息
            #    可能被丢弃——LLM 收到 tool_calls 但无结果；
            # 2) 截断后首条是 tool 消息，其对应的 assistant(tool_calls) 在
            #    窗口外——LLM 收到无配对 tool_calls 的 tool 消息（R2-11）。
            # 两种孤儿都向后跳过，直到首个配对完整的消息。
            while truncated:
                first = truncated[0]
                first_role = first.get("role")
                if first_role == "tool" or (
                    first_role == "assistant" and first.get("tool_calls")
                ):
                    truncated = truncated[1:]
                else:
                    break
            logger.debug(
                f"build_llm_messages: 历史 {len(messages)} 条超过上限 {max_msgs}，截断为 {len(truncated)} 条"
            )
        else:
            truncated = messages

        # 合并历史中的 system 消息（压缩摘要）到 system_prompt。
        # API 要求 system 消息只能有一个且在最开头，不能在中间出现。
        extra_system_parts = []
        conversation_msgs = []
        for msg in truncated:
            role_ = msg.get("role", "")
            if role_ == "system":
                extra_system_parts.append(msg.get("content", ""))
            elif role_ == "user":
                conversation_msgs.append({"role": "user", "content": msg.get("content", "")})
            elif role_ == "tool":
                conversation_msgs.append({
                    "role": "tool",
                    "content": msg.get("content", ""),
                    "tool_call_id": msg.get("tool_call_id", ""),
                })
            elif role_ == "assistant":
                # 只投影 LLM 可见键（reasoning 等附带键不进 payload；
                # 生成新 dict，避免与 state 共享对象被下游事件记录误改）
                d = {"role": "assistant", "content": msg.get("content", "")}
                if msg.get("tool_calls"):
                    d["tool_calls"] = msg["tool_calls"]
                conversation_msgs.append(d)

        final_system = system_prompt
        if extra_system_parts:
            final_system = system_prompt + "\n\n" + "\n\n".join(extra_system_parts)

        llm_messages: list[dict] = [{"role": "system", "content": final_system}]
        llm_messages.extend(conversation_msgs)

        # 2026-06-22: 修复 Bug 11 —— 用户消息在 ReAct 循环中被重复注入。
        # 旧逻辑用 `conversation_msgs[-1].content != current_input` 做位置判重，
        # 只在首轮（末尾恰好是本轮 user 消息）生效。一旦 LLM 发起 tool_call，
        # 后续循环迭代里末尾是 tool 消息，判重恒为 True，于是每次迭代
        # 都额外追加一条 user(current_input)。N 次工具调用 → 用户消息在
        # LLM payload 里出现 N+1 次，LLM 因此抱怨"用户重复了请求"。
        #
        # current_input 在本回合开始时已由 stream_invoke（构造 state 时）
        # 作为 user 消息进入 messages，
        # build_llm_messages 不应再补。这里只保留防御性兜底：历史中完全没有
        # user 消息时才补一条（冷启动场景，正常流程不会触发）。
        if not any(m.get("role") == "user" for m in conversation_msgs):
            llm_messages.append({"role": "user", "content": current_input})

        logger.debug(f"build_llm_messages: 消息数={len(llm_messages)}")
        return llm_messages

    # ============================================
    # 上下文压缩
    # ============================================

    def should_auto_compact(self, messages: list) -> bool:
        """
        判断是否应该自动压缩消息列表。

        基于消息数量阈值判断，当消息数超过 compact_min_messages 配置值时
        返回 True。

        Args:
            messages: 当前消息列表

        Returns:
            bool: 是否需要压缩
        """
        return len(messages) >= self.settings.compact_min_messages

    def compact_messages(
        self,
        messages: list[dict],
        keep_count: int | None = None,
    ) -> CompactResult | None:
        """
        执行上下文压缩：将早期消息生成摘要，替换原始消息。

        这是上下文压缩的统一执行入口，被以下两条路径调用：
        - Agent 自动触发：Registry 检测 compact_conversation 信号后调用
        - 用户手动触发：CLI 的 /compact 命令调用

        流程：
        1. 检查消息数量是否足够压缩
        2. 分离早期消息和近期消息
        3. 调用 generate_summary() 将早期消息转为摘要
        4. 用摘要 system 消息 + 近期消息重建列表
        5. 返回 CompactResult

        Args:
            messages: 消息列表（OpenAI dict；会被原地修改：clear + 重填摘要与近期消息）。
            注意：agent 循环的 _compact_messages 路径不依赖此原地修改（它用
            返回的 compressed_messages 重建 state），原地修改仅为 CLI /compact
            命令直接传 session_messages 的路径服务。
            keep_count: 保留最近几条消息（None 时使用配置值 compact_keep_recent）

        Returns:
            CompactResult | None: 压缩结果（消息数不足时返回 None）
        """
        if keep_count is None:
            keep_count = self.settings.compact_keep_recent

        # 消息数 ≤ keep_count 时自适应递减，而非直接放弃。
        # 解决"消息少但每条很大"（如大文件内容）时无法压缩的问题。
        while len(messages) <= keep_count and keep_count > 2:
            keep_count = max(2, keep_count // 2)

        if len(messages) <= keep_count:
            logger.debug(f"消息数 {len(messages)} <= 最小保留数 {keep_count}，跳过压缩")
            return None

        original_count = len(messages)
        old_messages = messages[:-keep_count]
        recent_messages = messages[-keep_count:]

        # 保护配对：保留区（recent）起点若落在某批工具调用结果中间，向前把
        # old 尾部对应的 assistant(tool_calls) 及其同批 tool 消息整体划入
        # recent——即保留区起点回溯到该批工具调用的 assistant 消息。
        # 覆盖两种切法（否则 LLM 收到孤立 tool 消息 → 严格端点 400，
        # durable kept_messages 同样投影坏数据）：
        # 1) old[-1] 是 assistant(tool_calls)、recent[0] 是对应 tool；
        # 2) 切点落在同批多个 tool 消息中间（old 尾部连续 tool，recent 以
        #    孤儿 tool 开头）。
        if (old_messages and recent_messages
                and recent_messages[0].get("role") == "tool"):
            i = len(old_messages)
            while i > 0 and old_messages[i - 1].get("role") == "tool":
                i -= 1
            if i > 0:
                assistant = old_messages[i - 1]
                if (assistant.get("role") == "assistant"
                        and assistant.get("tool_calls")):
                    moved = old_messages[i - 1:]
                    del old_messages[i - 1:]
                    recent_messages[:0] = moved

        compacted_count = len(old_messages)

        # 将早期消息拼接为文本
        from src.agent.multimodal import extract_text
        old_text_parts = []
        for msg in old_messages:
            role_ = msg.get("role", "")
            if role_ == "user":
                role = "用户"
            elif role_ == "assistant":
                role = "助手"
            elif role_ == "tool":
                role = "工具"
            else:
                role = "系统"
            # 多模态 content 可能是 list（含 image_url 块），extract_text 提取纯文本
            content = extract_text(msg.get("content", ""))
            if content:
                old_text_parts.append(f"{role}: {content}")

        if not old_text_parts:
            logger.debug("早期消息无可读内容，跳过压缩")
            return None

        old_text = "\n".join(old_text_parts)

        # 调用 LLM 生成摘要
        from src.tools.compact import generate_summary

        try:
            summary = generate_summary(old_text)
            # 2026-06-15: 防御空摘要——LLM 偶发返回空 content，会导致上下文全部丢失
            if not summary or not summary.strip():
                logger.error(f"生成压缩摘要为空（old_text={len(old_text)} 字符），放弃压缩")
                return None
            logger.info(f"上下文压缩完成: {compacted_count} 条早期消息 → 摘要({len(summary)} 字符)")
        except Exception as e:
            logger.error(f"生成压缩摘要失败: {e}")
            return None

        # 用摘要替换早期消息（原地修改列表）
        # 2026-06-15: 用 system 消息包装摘要，避免伪装成用户输入导致 LLM 困惑
        # 2026-07-03: 给摘要赋显式 id（附带的 "compact_id" 键），供多次 compact 后
        # 识别/剔除旧摘要，避免上下文累积膨胀。
        summary_msg = {
            "role": "system",
            "content": f"以下是对话历史的压缩摘要，请基于此背景继续对话：\n{summary}",
            "compact_id": f"compact-{uuid.uuid4().hex[:8]}",
        }
        messages.clear()
        messages.append(summary_msg)
        messages.extend(recent_messages)

        return CompactResult(
            compressed_messages=messages,
            summary=summary,
            original_count=original_count,
            compacted_count=compacted_count,
        )
