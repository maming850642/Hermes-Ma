"""
============================================
parse_partial_json —— 容错 JSON 解析（自上游 MIT 库抄录）
============================================
解析可能不完整的 JSON 字符串（如流式 tool_call arguments 的累积片段）。

能修复的瑕疵：
- 未闭合的 { } 或 [ ]
- 字符串内未转义的换行（\n → \\n）
- 尾随逗号（通过尝试逐字符回退）
- 未闭合的字符串（自动补 "）

来源：上游 MIT 库 utils/json.py（License: MIT）
原始作者：https://github.com/KillianLucas/open-interpreter/blob/5b6080fae1f8c68938a1e4fa8667e3744084ee21/interpreter/utils/parse_partial_json.py

为什么需要它：
    Ollama 上 Qwen/Gemma 的流式 tool_call arguments 经常带瑕疵——
    流式时最后一个 chunk 还没收完（{ "path":"/x" 没闭合），
    或字符串里有裸换行、尾随逗号。
    上游实现用 parse_partial_json 全吃下来，
    我们的 LLMClient 也必须用同等容错，否则 json.loads 直接抛异常。
"""

from __future__ import annotations

import json
from typing import Any


def parse_partial_json(s: str, *, strict: bool = False) -> Any:
    """解析可能不完整的 JSON 字符串。

    Args:
        s: 可能不完整的 JSON 字符串。
        strict: 是否严格模式（json.loads 的 strict 参数）。

    Returns:
        解析后的 Python 对象。无法解析时抛 json.JSONDecodeError（原始字符串的）。

    Raises:
        json.JSONDecodeError: 如果连容错修复后仍无法解析。
    """
    # 先试原样解析
    try:
        return json.loads(s, strict=strict)
    except json.JSONDecodeError:
        pass

    new_chars: list[str] = []
    stack: list[str] = []
    is_inside_string = False
    escaped = False

    # 逐字符处理
    for char in s:
        new_char = char
        if is_inside_string:
            if char == '"' and not escaped:
                is_inside_string = False
            elif char == "\n" and not escaped:
                new_char = "\\n"  # 字符串内裸换行 → 转义
            elif char == "\\":
                escaped = not escaped
            else:
                escaped = False
        elif char == '"':
            is_inside_string = True
            escaped = False
        elif char == "{":
            stack.append("}")
        elif char == "[":
            stack.append("]")
        elif char in {"}", "]"}:
            if stack and stack[-1] == char:
                stack.pop()
            else:
                # 不匹配的闭合符——输入已损坏
                return None

        new_chars.append(new_char)

    # 如果结束时仍在字符串内，自动闭合
    if is_inside_string:
        if escaped:  # 移除未终结的转义符
            new_chars.pop()
        new_chars.append('"')

    # 反转栈，得到需要补的闭合符
    stack.reverse()

    # 逐字符回退尝试解析
    while new_chars:
        try:
            return json.loads("".join(new_chars + stack), strict=strict)
        except json.JSONDecodeError:
            new_chars.pop()

    # 回退到原始字符串解析（会抛 JSONDecodeError）
    return json.loads(s, strict=strict)


def safe_parse_tool_args(args_str: str | None) -> dict:
    """容错解析 tool_call arguments。

    对齐上游 LLM 库的 init_tool_calls 行为：
    1. 先用 parse_partial_json（能修复不完整 JSON）
    2. 失败则用 json.loads 严格解析
    3. 都失败则返回空 dict（不抛异常，让工具以空参数执行）

    Args:
        args_str: LLM 返回的 arguments 字符串（可能不完整/带瑕疵）。

    Returns:
        解析后的 dict。解析失败返回 {}。
    """
    if not args_str or not args_str.strip():
        return {}
    try:
        result = parse_partial_json(args_str)
        if isinstance(result, dict):
            return result
        if isinstance(result, list):
            # 极少数模型返回 list 形式的 args
            return {"_args": result}
        return {"_value": result}
    except Exception:
        pass
    try:
        result = json.loads(args_str)
        if isinstance(result, dict):
            return result
        return {"_value": result}
    except Exception:
        # 全部失败——返回空 dict，工具以默认参数执行（不崩）
        return {}
