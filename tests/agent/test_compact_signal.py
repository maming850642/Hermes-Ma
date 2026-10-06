"""P1 回归测试：V3 compact_conversation 工具信号链。

`_execute_compact` 必须返回 ToolResult(state_updates={"compact_requested": True})，
而非裸字符串 "COMPACT_REQUESTED"。否则 PythonExecutor 包装为
ToolResult(content="COMPACT_REQUESTED", state_updates={})，
agent_v3.py:490 的 state_updates.get("compact_requested") 永远为 False。
"""
from src.agent.tool_result import ToolResult
from src.tools.compact import _execute_compact


def test_execute_compact_returns_tool_result():
    """_execute_compact 返回 ToolResult（非裸字符串）。"""
    result = _execute_compact()
    assert isinstance(result, ToolResult), (
        f"_execute_compact 应返回 ToolResult，实际返回 {type(result)}"
    )


def test_execute_compact_has_compact_requested_flag():
    """返回的 ToolResult 中 state_updates 必须有 compact_requested=True。"""
    result = _execute_compact()
    assert result.state_updates.get("compact_requested") is True, (
        f"state_updates 应包含 compact_requested=True，实际: {result.state_updates}"
    )


def test_execute_compact_has_nonempty_content():
    """返回给 LLM 的 content 不为空（LLM 需要感知到压缩已触发）。"""
    result = _execute_compact()
    assert result.content and result.content.strip(), (
        "content 不应为空，LLM 需要收到压缩已触发的反馈"
    )
