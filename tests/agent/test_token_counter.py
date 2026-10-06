"""
F1 回归：token 计数对 OpenAI dict 消息的死路径。

背景（2026-08-15 R1 深度 review）：
    count_tokens 用 getattr(msg, "content", "") 取内容——T7 起消息全面
    dict 化（state["messages"] 存 OpenAI dict），dict 无 content 属性 →
    恒空串 → 计数≈0 → token 阈值压缩永不触发。

约定：不 mock 计数器——真实 count_tokens（tiktoken 或字符回落），
断言 >0 且单调；阈值压缩走 HermesAgentV3._compact_threshold_hit 集成路径。
"""
from unittest.mock import patch

import pytest

from src.agent.token_counter import count_tokens, count_text_tokens


def test_dict_message_content_counted():
    """单条 dict 消息的内容必须被计入（不再只有每条 +4 的开销）。"""
    msgs = [{"role": "user", "content": "这是一段需要被计入 token 的较长文本内容，" * 10}]
    total = count_tokens(msgs)
    assert total > 50, f"dict 消息内容应计入 token，实际只有 {total}（≈4 为死路径特征）"


def test_monotonic_increase_with_messages():
    """消息数增加 → 计数单调不减（每条都有真实内容）。"""
    msgs = [
        {"role": "user", "content": "第一段有实际内容的消息，长度足以计入若干 token。"},
        {"role": "assistant", "content": "这是回复，同样包含足够长的实际内容用于计数。"},
        {"role": "user", "content": "再来一条更长的消息。" * 3},
    ]
    counts = [count_tokens(msgs[: i + 1]) for i in range(len(msgs))]
    assert counts[0] > 10, "首条消息计数应显著大于 0"
    for a, b in zip(counts, counts[1:]):
        assert b > a, f"计数应单调增加: {counts}"


def test_tool_call_arguments_counted():
    """assistant 消息的 tool_calls.function.arguments 计入 token。"""
    base_args = '{"command": "ls -la /some/very/long/path/that/should/be/counted"}'
    plain = [{"role": "assistant", "content": ""}]
    with_tc = [{
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "bash", "arguments": base_args},
        }],
    }]
    assert count_tokens(with_tc) > count_tokens(plain), \
        "tool_calls 的 arguments 应计入 token（否则工具调用多的会话被低估）"


def test_threshold_compaction_triggers_for_huge_dict_message():
    """集成：超大单条 dict 消息 → 真实计数过阈值 → _compact_threshold_hit=True。

    修复前：dict 死路径 → 计数≈4 → 永不触发 → 上下文无限膨胀。
    """
    from config import _Settings
    from src.agent.agent_v3 import HermesAgentV3

    with patch.object(HermesAgentV3, "__init__", lambda self, *a, **kw: None):
        agent = HermesAgentV3.__new__(HermesAgentV3)
    settings = _Settings()
    settings["compact_threshold_pct"] = 80
    settings["max_tokens"] = 1000
    agent.settings = settings

    huge = [{"role": "user", "content": "数据载荷 " * 200_000}]  # 远超窗口
    with patch("src.agent.context_window.get_context_window", return_value=131072):
        assert agent._compact_threshold_hit(huge, 80) is True, \
            "超大 dict 消息必须触发阈值压缩（修复前恒 False）"

    tiny = [{"role": "user", "content": "hi"}]
    with patch("src.agent.context_window.get_context_window", return_value=131072):
        assert agent._compact_threshold_hit(tiny, 80) is False


def test_str_content_still_counted():
    """str content 原有行为保持（回归对照）。"""
    text = "plain text content for counting"
    assert count_tokens([{"role": "user", "content": text}]) >= count_text_tokens(text)
