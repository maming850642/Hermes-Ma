"""
skills 插件 —— SkillRegistry 单例注册为 "skills"。
"""

from __future__ import annotations

from src.cordis.context import Context


def apply(ctx: Context, config: dict) -> None:
    from src.skills import get_registry

    ctx.register("skills", get_registry())
