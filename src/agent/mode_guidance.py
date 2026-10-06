"""
============================================
权限模式注入 system prompt
============================================
LLM 不知道自己在 plan 模式会照常调 write_file → 被 deny → 困惑、重试、浪费轮次。
所以 mode 必须注入 prompt。

三种 mode 的 guidance：
    full_access     所有工具可直接调用，无需顾虑。
    before_changes  修改/删除/运行有副作用的命令前系统会暂停等用户确认。
    plan            不能执行破坏性操作，但可以读取/搜索/规划/记住事实/管理待办。

plan 模式的 guidance 显式点名 remember/write_todos "可以"——打消模型过度泛化
（把 remember 也当成"修改文件"而拒绝）。

两层兜底：prompt 减少无谓调用，执行层保证即使模型真的在 plan 下调了
remember 也不误伤（执行层 mode 决策表放行非 destructive 工具）。

"""

from __future__ import annotations

from src.tools.context import (
    PERMISSION_MODE_BEFORE,
    PERMISSION_MODE_FULL,
    PERMISSION_MODE_PLAN,
)

# 模式显示名（中文，给用户看）
MODE_DISPLAY_NAMES = {
    PERMISSION_MODE_FULL: "完全访问",
    PERMISSION_MODE_BEFORE: "变更前访问",
    PERMISSION_MODE_PLAN: "计划模式",
}

# 模式 guidance（注入 system prompt 的文本）
_MODE_GUIDANCE: dict[str, str] = {
    PERMISSION_MODE_FULL: (
        "所有工具可直接调用，无需顾虑。用户已选择完全信任。"
    ),
    PERMISSION_MODE_BEFORE: (
        "修改/删除文件、运行有副作用的 shell 命令前，系统会暂停等用户确认。"
        "你可以正常调用这些工具，审批由系统处理——调用时不必犹豫。"
    ),
    PERMISSION_MODE_PLAN: (
        "不能执行破坏性操作（写入/删除/修改文件、运行有副作用的 shell 命令）。"
        "可以：读取信息、搜索、规划、记住事实（remember）、管理待办（write_todos）、"
        "压缩上下文（compact_conversation）。"
        "遇到需要修改的外部操作时，向用户说明计划，等用户切换模式后再执行。"
    ),
}


def get_mode_display_name(mode: str) -> str:
    """模式的中文显示名（给 UI 用）。"""
    return MODE_DISPLAY_NAMES.get(mode, mode)


def get_mode_guidance(mode: str) -> str:
    """模式的 guidance 文本（注入 system prompt）。"""
    return _MODE_GUIDANCE.get(mode, _MODE_GUIDANCE[PERMISSION_MODE_BEFORE])


def build_mode_prompt_section(mode: str) -> str:
    """构造注入 system prompt 的 mode 段落。

    返回形如：
        ## 当前权限模式：计划模式
        不能执行破坏性操作（写入/删除/修改文件...）。可以：读取信息、搜索...

    空字符串表示无需注入（理论上不会发生，所有 mode 都有 guidance）。
    """
    guidance = get_mode_guidance(mode)
    display = get_mode_display_name(mode)
    return (
        f"## 当前权限模式：{display}\n"
        f"{guidance}"
    )
