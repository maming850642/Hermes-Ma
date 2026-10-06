"""
============================================
上下文压缩工具
============================================
允许 Agent 主动压缩消息历史，用摘要替换早期对话，释放 token 空间。

架构说明（压缩流程）：

    本模块是上下文压缩系统的最底层，提供两个核心能力：
    1. compact_conversation 工具：作为 LLM 可调用的工具，发出"压缩请求"信号
    2. generate_summary 函数：调用 LLM 将消息文本转为摘要

    压缩的完整流程涉及三个层次：

    ┌────────────────────────────────────────────────────────────────┐
    │ 触发路径                                                       │
    ├────────────────────────────────────────────────────────────────┤
    │ 路径 A（Agent 自动触发）：                                      │
    │   LLM 调用 compact_conversation 工具                            │
    │   → ToolRegistry 返回 compact_requested 信号                   │
    │   → agent 循环检测信号路由到压缩执行点                          │
    │   → ContextManager.compact_messages() 执行实际压缩              │
    │                                                                │
    │ 路径 B（用户手动触发）：                                         │
    │   用户输入 /compact 命令                                        │
    │   → CLI.compact_session() 调用 ContextManager.compact_messages()│
    │   → 直接压缩 session_messages                                  │
    └────────────────────────────────────────────────────────────────┘

    关键设计决策：
    - compact_conversation 工具本身不执行压缩，仅作为 LLM 的"信号"
    - 实际压缩逻辑统一收归在 ContextManager.compact_messages() 中
    - generate_summary() 是底层辅助函数，被 ContextManager 调用
"""

import logging
from src.llm.client import LLMClient
from src.types import ToolResult

from config import get_settings

logger = logging.getLogger("hermes.tools.compact")


def _execute_compact(*, ctx=None) -> ToolResult:
    """V3 PythonExecutor 入口：发出压缩请求信号。

    工具描述见 tools/compact_conversation.yaml。
    实际压缩由 ContextManager.compact_messages() 在 agent 层完成。
    """
    logger.debug("compact_conversation 被调用（发出压缩请求信号）")
    # 2026-09-06 死循环修复：返回文本必须是完成时态。压缩由 agent 消费
    # compact_requested 后同步执行（agent_v3._compact_messages），工具结果
    # 回到模型面前时压缩已经完成——此前"将在本轮压缩"的将来时让模型以为
    # 尚未压缩，实测每轮重说一句开场白 + 重调本工具，循环十几次。
    return ToolResult(
        content="上下文压缩已完成：早期对话已替换为摘要。请直接继续当前任务，不要再次调用本工具。",
        state_updates={"compact_requested": True},
    )


def generate_summary(messages_text: str, max_tokens: int | None = None) -> str:
    """
    调用 LLM 生成对话摘要

    这是压缩系统的底层辅助函数，由 ContextManager.compact_messages() 调用。

    Args:
        messages_text: 拼接后的早期消息文本
        max_tokens: 摘要生成的最大 token 数（None 时使用配置值）

    Returns:
        摘要文本
    """
    settings = get_settings()

    if max_tokens is None:
        max_tokens = settings.compact_summary_max_tokens

    # 2026-06-15: Qwen3 思考模型关闭思考模式，避免 token 被 <think> 消耗导致摘要为空
    summary_llm = LLMClient(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.llm_model_name,
        temperature=0.1,
        max_tokens=max_tokens,
        request_timeout=60,
        max_retries=0,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )

    system_prompt = (
        "你是对话摘要助手。把对话浓缩为高信息密度的一段中文摘要,服务于后续对话的上下文召回。"
        "客观精炼,陈述句为主,纯文本段落输出(不要分点列表/Markdown 标题)。"
        "忠于事实,对话里没出现的结论不要编造;全是寒暄或无结论时直接说明,不要硬凑。"
    )

    prompt = f"""请将以下对话历史压缩为一段简洁的摘要。

提炼要点(在心中按此逻辑组织,但输出为连贯段落):
1. 用户想解决什么问题/达成什么目标
2. 过程中的关键信息、方案或探索
3. 达成的共识、决定或结果
4. 遗留问题或下一步

对话历史:
{messages_text}

摘要:"""

    return summary_llm.invoke_simple(prompt, system=system_prompt, max_tokens=max_tokens)