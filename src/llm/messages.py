"""
============================================
自建消息类型（轻量 OpenAI dict 适配层）
============================================
轻量 dataclass：SystemMsg / HumanMessage /
AIMessage / ToolMessage。

设计原则：
- 纯数据容器，无行为（不做 Runnable 那样的运行时抽象）
- to_dict() 直接产出 OpenAI chat API 的消息格式
- AIMsg.tool_calls 用 list[dict]（对齐 OpenAI 的 tool_calls JSON 结构）
- AIMsg.reasoning 保留 vLLM 的思考链字段

"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SystemMsg:
    """系统消息。注入角色/规则/mode guidance。"""
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {"role": "system", "content": self.content}


@dataclass
class HumanMsg:
    """用户消息。"""
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {"role": "user", "content": self.content}


@dataclass
class AIMsg:
    """AI 回复消息。可能含 tool_calls（触发工具调用）。"""
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    """OpenAI 格式的 tool_calls：[{"id", "type":"function", "function":{"name","arguments"}}]"""

    reasoning: str = ""
    """vLLM 的思考链（delta.reasoning / delta.reasoning_content）。
    openai SDK 直接暴露。"""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": "assistant"}
        if self.content:
            d["content"] = self.content
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        return d

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class ToolMsg:
    """工具执行结果消息。回传给 LLM 作为 tool_call 的响应。"""
    content: str
    tool_call_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": "tool",
            "content": self.content,
            "tool_call_id": self.tool_call_id,
        }


# ════════════════════════════════════════════════════════════════
# 流式 chunk
# ════════════════════════════════════════════════════════════════

@dataclass
class Chunk:
    """流式输出的单个 chunk。字段都是增量的（可能为空）。"""
    content_delta: str = ""
    reasoning_delta: str = ""
    tool_call_deltas: list[dict[str, Any]] = field(default_factory=list)
    """增量 tool_call 片段：[{"index", "id"?, "function":{"name"?, "arguments"?}}]"""
