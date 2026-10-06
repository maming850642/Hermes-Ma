"""
============================================
人格组装 —— waker 的 system prompt 段
============================================
读 waker 目录下的 IDENTITY.md / PERSONA.md / BIBLE.md，组装成一段
"数字员工人格" system prompt。runner 在 fork worker 时把这段拼进 system prompt。

格式（任一 md 为空则跳过对应小节；三者全空返回 ""）：

    # 数字员工人格
    ## 核心职责（IDENTITY）
    <IDENTITY.md 内容>
    ## 工作风格（PERSONA）
    <PERSONA.md 内容>
    ## 工作准则（BIBLE）
    <BIBLE.md 内容>

接口接受 store 或 dir 两种入参（runner 已有 dir，调度器只有 store）：
- store: WakerStore 或任何带 waker_dir(name) 方法的对象
- dir:   直接给 waker 目录路径（Path 或 str）
"""
from __future__ import annotations

from pathlib import Path

from src.waker.models import PERSONA_FILES


def _resolve_waker_dir(store_or_dir, name: str | None) -> Path:
    """把 store_or_dir 解析成 waker 目录 Path。

    - Path / str：直接当目录路径（此时 name 不参与）
    - 其它：视为 store，调用其 waker_dir(name) 方法
    """
    if isinstance(store_or_dir, (str, Path)):
        return Path(store_or_dir)
    waker_dir = getattr(store_or_dir, "waker_dir", None)
    if callable(waker_dir):
        return Path(waker_dir(name))
    raise TypeError(
        f"无法从 {store_or_dir!r} 解析 waker 目录：需提供路径或带 waker_dir 方法的 store"
    )


def load_persona_prompt(store_or_dir, name: str | None = None) -> str:
    """组装 waker 的人格 system prompt 段。

    Args:
        store_or_dir: waker 目录路径（Path/str），或 WakerStore 对象
        name: 当 store_or_dir 是 store 时，waker 名；给路径时可为 None
    Returns:
        组装好的人格文本；三个 md 全空则返回 ""
    """
    waker_dir = _resolve_waker_dir(store_or_dir, name)
    sections: list[str] = []
    for fname, title in PERSONA_FILES:
        p = waker_dir / fname
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        text = text.strip()
        if not text:
            continue
        sections.append(f"## {title}\n{text}")
    if not sections:
        return ""
    return "# 数字员工人格\n\n" + "\n\n".join(sections) + "\n"
