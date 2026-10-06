"""
config 插件 —— 把全局配置注册为 "config" 服务。

get_settings() 是 lru_cache 单例，重复 boot 拿到同一对象（配置本就
进程级唯一）。下游插件（llm/tools 等）经 inject: [config] 依赖它。
"""

from __future__ import annotations

from src.cordis.context import Context


def apply(ctx: Context, config: dict) -> None:
    from config import get_settings

    ctx.register("config", get_settings())
