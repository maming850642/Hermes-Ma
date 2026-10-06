"""
============================================
loader 测试用插件夹具
============================================
提供可经 "tests.cordis.fixture_plugins:属性" 引用的插件函数，
用模块级 APPLY_ORDER/SEEN_TOOLS 记录挂载顺序与依赖可见性，
每个测试前由 autouse fixture 调 reset() 清空。
"""

from __future__ import annotations

from typing import Any

from src.cordis.context import Context

#: 插件 apply 顺序记录
APPLY_ORDER: list[str] = []
#: 依赖方插件实际取到的 "tools" 服务
SEEN_TOOLS: list[Any] = []


def reset() -> None:
    """清空夹具状态（测试隔离用）。"""
    APPLY_ORDER.clear()
    SEEN_TOOLS.clear()


def tools_plugin(ctx: Context, config: dict) -> None:
    """提供 tools 服务的插件。"""
    APPLY_ORDER.append("tools")
    ctx.register("tools", {"src": "fixture", **config})


def consumer_plugin(ctx: Context, config: dict) -> None:
    """依赖 tools 服务的消费方插件。"""
    APPLY_ORDER.append("consumer")
    SEEN_TOOLS.append(ctx.get("tools"))


def plain_plugin(ctx: Context, config: dict) -> None:
    """无依赖的普通插件。"""
    APPLY_ORDER.append("plain")


def llm_plugin(ctx: Context, config: dict) -> None:
    """提供 llm 服务的插件。"""
    APPLY_ORDER.append("llm")
    ctx.register("llm", {"src": "fixture-llm"})
