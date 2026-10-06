"""
boot_context —— 组合根入口：按 profile（缺省仓库根 cordis.yaml）
启动全部核心服务插件，返回挂载好的 Context。

用法：
    from src.plugins import boot_context
    ctx = boot_context()          # data/hermes.db + 仓库默认配置
    ctx = boot_context("x.yaml")  # 自定义 profile（如测试指向 tmp 库）
    ...
    ctx.teardown()                # 进程/作用域退出时拆卸
"""

from __future__ import annotations

from pathlib import Path

from src.cordis.context import Context
from src.cordis.loader import boot_file

#: 仓库根的默认 profile：src/plugins/boot.py → 上溯两级
DEFAULT_PROFILE = Path(__file__).resolve().parents[2] / "cordis.yaml"


def boot_context(profile: "str | Path | None" = None) -> Context:
    """启动组合根上下文。

    Args:
        profile: 插件 profile 文件路径；None 时用仓库根 cordis.yaml。

    Returns:
        挂载好的根 Context（ctx.config/ctx.storage/ctx.sessions/ctx.memory/
        ctx.llm/ctx.tools/ctx.skills/ctx.mcp/ctx.schedule 稳定键可取）。
    """
    path = Path(profile) if profile is not None else DEFAULT_PROFILE
    return boot_file(path)
