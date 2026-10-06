"""
============================================
WakerFlow 模板插值 —— {{...}} → 实际值
============================================
纯函数模块：把 {{inputs.x}} / {{steps.y.result}} 这类占位符
按 context 替换成字符串。

设计要点：
- 只做字符串替换，不执行任意代码（安全）
- 缺失键抛 KeyError，错误消息带上具体占位符
- 多级点号路径（{{steps.analyze.research.result}}）逐级 resolve
- 非字符串值（dict/list/数字）转 JSON 字符串（便于嵌进 prompt/body）
- 一个模板里同一占位符多次出现时全部替换

context 结构示例：
    {
        "inputs": {"repo_path": "...", "mode": "quick"},
        "steps": {
            "scan": {"result": "..."},
            "analyze": {"research": {"result": "..."}, "critique": {"result": "..."}},
        },
    }
"""
from __future__ import annotations

import json
import re

# 匹配 {{xxx}} / {{ xxx }} / {{ a.b.c }}，捕获点号路径
# 只允许字母数字下划线和点（防止 {{ 1+1 }} 这类表达式逃逸）
_TEMPLATE_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


class TemplateError(KeyError):
    """模板插值失败（含原始占位符消息）。继承 KeyError 保持向后兼容。"""


def render(template: str, context: dict) -> str:
    """把 template 中的所有 {{...}} 占位符按 context 替换。

    Args:
        template: 含占位符的字符串
        context: 上下文 dict，至少包含 inputs / steps 等 key

    Returns:
        替换后的字符串

    Raises:
        TemplateError: 任何占位符在 context 里无法 resolve
    """
    if not isinstance(template, str):
        # 非字符串（dict/list/数字等）：直接转 JSON。允许 caller 把整个字段当模板传进来。
        return template

    def _repl(m: re.Match) -> str:
        path = m.group(1)
        try:
            value = _resolve_path(context, path)
        except TemplateError:
            raise
        if isinstance(value, str):
            return value
        if value is None:
            return ""  # 便于 prompt 拼接：None → 空串
        # dict / list / number / bool → JSON 字符串
        return json.dumps(value, ensure_ascii=False, default=str)

    return _TEMPLATE_RE.sub(_repl, template)


def _resolve_path(context: dict, path: str) -> Any:
    """从 context 里按点号路径取值。

    例：path="steps.analyze.research.result"
        context={"steps": {"analyze": {"research": {"result": "ok"}}}}
        → "ok"
    """
    parts = path.split(".")
    cur: Any = context
    walked: list[str] = []
    for p in parts:
        walked.append(p)
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        elif isinstance(cur, list):
            # 支持 {{steps.x.0.result}} 数字索引（防御性，DSL 一般不用）
            try:
                idx = int(p)
            except ValueError:
                raise TemplateError(
                    f"模板占位符 {{{{{path}}}}} 解析失败：在 {cur!r} 上无法用 {p!r} 索引"
                )
            if idx < 0 or idx >= len(cur):
                raise TemplateError(
                    f"模板占位符 {{{{{path}}}}} 解析失败：索引 {idx} 越界"
                )
            cur = cur[idx]
        else:
            raise TemplateError(
                f"模板占位符 {{{{{path}}}}} 解析失败：在 {'.'.join(walked)} 处找不到 {p!r}"
            )
    return cur


def find_references(template: str) -> list[str]:
    """提取 template 里所有 {{...}} 占位符的路径，去重保序。

    供 parser 做"依赖 step id 是否存在"的静态校验。
    """
    seen: list[str] = []
    out: list[str] = []
    for m in _TEMPLATE_RE.finditer(template or ""):
        p = m.group(1)
        if p not in seen:
            seen.append(p)
            out.append(p)
    return out
