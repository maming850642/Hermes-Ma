"""
============================================
use_skill 工具 - 技能加载器
============================================
让 LLM 自主加载某个技能的详细操作指令（方案①工具化路径）。

设计：这是"模型自选"的入口。system prompt 末尾常驻「技能目录」
（仅 name + description 摘要），LLM 判断当前任务匹配某技能时调用本工具，
获取该技能的完整指令 Markdown，随后按指令执行。

这与 Claude Code Skills 的机制完全一致：skill 全文按需加载，不占常驻 token。

2026-06-18: v2 增强，返回值附带技能附属资源清单，LLM 可按需用 bash cat 读取
2026-06-18: 初始实现
"""

import logging


from src.skills import get_registry

logger = logging.getLogger("hermes.tools.use_skill")


def use_skill(skill_name: str) -> str:
    """
    加载并应用某个技能的详细操作指令。

    当你判断当前任务需要专业技能时，先调用此工具获取完整指令，
    然后严格按照指令执行任务。可用技能见系统提示的「技能目录」。

    如果该技能附带附属资源（脚本、模板、参考文档等），返回值末尾会列出
    资源清单及读取路径，你可以用 bash 的 cat 按需读取它们。

    Args:
        skill_name: 技能名称（必须与技能目录中的名称完全一致）

    Returns:
        str: 该技能的完整操作指令（Markdown）；未找到时返回可用列表
    """
    registry = get_registry()
    skill = registry.get(skill_name)

    if not skill:
        available = registry.list_names()
        hint = ", ".join(available) if available else "（无可用技能）"
        logger.info(f"use_skill: 未找到技能 '{skill_name}'，提示可用列表")
        return f"未找到技能 '{skill_name}'。当前可用技能: {hint}"

    logger.info(
        f"use_skill: LLM 加载技能 '{skill_name}' "
        f"(来源: {skill.source}, resources: {len(skill.resources)})"
    )

    parts = [
        f"# 已加载技能：{skill.name}\n",
        f"> {skill.description}\n",
        skill.content,
    ]

    # 2026-06-18: 追加附属资源清单，引导 LLM 按需读取
    if skill.resources and skill.dir_path:
        resource_lines = [f"- {rel}" for rel in skill.resources]
        parts.append(
            "\n\n## 📎 技能附属资源\n"
            f"此技能位于目录 `{skill.dir_path}`，附带以下文件。"
            "需要时可用 `bash` 的 `cat` 读取（请将相对路径与此目录拼接成完整路径）：\n\n"
            + "\n".join(resource_lines)
        )

    return "\n".join(parts)


# ════════════════════════════════════════════════════════════════
# V3 PythonExecutor 入口
# ════════════════════════════════════════════════════════════════

def _execute_use_skill(skill_name: str, *, ctx=None) -> str:
    """V3 PythonExecutor 入口。"""
    return use_skill(skill_name)