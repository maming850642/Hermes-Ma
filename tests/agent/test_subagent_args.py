"""
子 agent 参数类型转换测试。

背景（2026-06-23）：LLM tool_call 的数字参数可能是字符串（如 "60" 而非 60）。
_execute_subagent_tool 这条路径绕过了 @tool 的 pydantic 校验，直接 .get() 取值，
导致字符串 timeout 传到 StreamingSubAgent.__init__，在 `self._timeout > 0` 抛
TypeError: '>' not supported between instances of 'str' and 'int'。

本测试验证两层防御：
1. _execute_subagent_tool 解析时强转（主要修复）
2. StreamingSubAgent.__init__ 防御性强转（纵深防御）
"""
from unittest.mock import patch, MagicMock

import pytest


def test_streaming_subagent_init_accepts_str_timeout():
    """StreamingSubAgent.__init__ 防御层：字符串 timeout 不应抛 TypeError。

    修复前：__init__ 把 str 存进 self._timeout，后续 self._timeout > 0 抛 TypeError
    修复后：__init__ 强转 int
    """
    from src.tools.sub_agent import StreamingSubAgent

    # 用 mock 避免真实 LLM 初始化
    with patch("src.tools.sub_agent.LLMClient"):
        # 传字符串 "60"（模拟 LLM tool_call 的典型错误）
        agent = StreamingSubAgent(name="test", timeout="60", max_tokens="3000")
        assert agent._timeout == 60, f"timeout 应被强转为 int，实际 {type(agent._timeout)}"
        assert agent._max_tokens == 3000, f"max_tokens 应被强转为 int，实际 {type(agent._max_tokens)}"


def test_streaming_subagent_init_accepts_int():
    """正常 int 参数仍能工作。"""
    from src.tools.sub_agent import StreamingSubAgent

    with patch("src.tools.sub_agent.LLMClient"):
        agent = StreamingSubAgent(name="test", timeout=60, max_tokens=3000)
        assert agent._timeout == 60
        assert agent._max_tokens == 3000


def test_streaming_subagent_init_none_falls_back_to_config():
    """None 参数回退到配置默认值。"""
    from src.tools.sub_agent import StreamingSubAgent

    with patch("src.tools.sub_agent.LLMClient"):
        agent = StreamingSubAgent(name="test", timeout=None, max_tokens=None)
        # 应该用了配置默认值（非 None）
        assert agent._timeout is not None
        assert agent._max_tokens is not None


def test_execute_subagent_tool_parses_str_args():
    """V3 解析层：registry_v3.coerce_args 按 JSON Schema 强转字符串参数。

    模拟 LLM tool_call 传 {"timeout": "60", "max_tokens": "3000", "inherit_tools": "true"}
    验证强转后传给 StreamingSubAgent 的是 int/bool。
    """
    from src.agent.registry_v3 import coerce_args
    from src.tools.sub_agent import _execute_task
    from src.tools.loader import get_tool

    spec = get_tool("task")
    assert spec is not None, "task 工具应可加载"

    raw_args = {
        "instruction": "测试任务",
        "timeout": "60",         # LLM 传了字符串
        "max_tokens": "3000",    # LLM 传了字符串
        "inherit_tools": "true", # LLM 传了字符串
    }
    coerced = coerce_args(raw_args, spec.parameters)
    assert coerced["timeout"] == 60
    assert coerced["max_tokens"] == 3000
    assert coerced["inherit_tools"] is True

    # 强转后的参数经 _execute_task → StreamingSubAgent 构造
    captured_kwargs = {}

    class FakeSubAgent:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)
        def stream(self, instruction):
            yield "done"
        def run(self, instruction):
            return MagicMock(success=True, result="done", error_type=None)

    with patch("src.tools.sub_agent.LLMClient"), \
         patch("src.tools.sub_agent._get_current_depth", return_value=0), \
         patch("src.tools.sub_agent._set_current_depth"), \
         patch("src.tools.sub_agent.StreamingSubAgent", FakeSubAgent):
        try:
            _execute_task(
                instruction=coerced["instruction"],
                timeout=coerced["timeout"],
                max_tokens=coerced["max_tokens"],
                inherit_tools=coerced["inherit_tools"],
            )
        except Exception:
            # 可能因 mock 不完整而抛其他异常，但我们只关心参数解析
            pass

    assert captured_kwargs.get("timeout") == 60, \
        f"timeout 应被强转为 int 60，实际 {captured_kwargs.get('timeout')!r}"
    assert captured_kwargs.get("max_tokens") == 3000, \
        f"max_tokens 应被强转为 int 3000，实际 {captured_kwargs.get('max_tokens')!r}"
    # inherit_tools="true" 解析为 True 后，会注入 tools（tools 非空）
    assert captured_kwargs.get("tools") is not None, \
        "inherit_tools='true' 应触发 tools 注入"


def test_streaming_subagent_extra_body_empty():
    """子代理不强制 enable_thinking:False，对齐主 agent thinking=False 的空 extra_body。"""
    from src.tools.sub_agent import StreamingSubAgent

    with patch("src.tools.sub_agent.LLMClient") as mock_cls:
        StreamingSubAgent(name="test")
    kwargs = mock_cls.call_args.kwargs
    assert kwargs.get("extra_body") == {}, (
        f"extra_body 应为空 dict，实际 {kwargs.get('extra_body')!r}"
    )


def test_streaming_subagent_inherits_llm_overrides():
    """主 agent 热切换的 model/base_url/api_key 经 contextvar 传到子代理。"""
    from src.llm.client import set_current_llm_overrides
    from src.tools.sub_agent import StreamingSubAgent

    set_current_llm_overrides({
        "model": "hot-model",
        "base_url": "http://hot.test/v1",
        "api_key": "sk-hot",
    })
    try:
        with patch("src.tools.sub_agent.LLMClient") as mock_cls:
            StreamingSubAgent(name="test")
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["model"] == "hot-model"
        assert kwargs["base_url"] == "http://hot.test/v1"
        assert kwargs["api_key"] == "sk-hot"
    finally:
        set_current_llm_overrides(None)


def test_timeout_str_no_longer_raises_typeerror():
    """端到端验证：字符串 timeout 不再抛 TypeError。

    这是修复前用户实际遇到的错误：
    'ERROR: 子智能体执行失败: '>' not supported between instances of 'str' and 'int''
    """
    from src.tools.sub_agent import StreamingSubAgent

    with patch("src.tools.sub_agent.LLMClient"):
        # 修复前这一行会成功（构造时不报错），但后续 self._timeout > 0 爆炸
        # 修复后构造时就强转，后续比较安全
        agent = StreamingSubAgent(name="test", timeout="45")
        # 模拟 sub_agent.py:169 的比较
        if agent._timeout and agent._timeout > 0:
            pass  # 不应抛 TypeError
