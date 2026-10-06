"""
============================================
Cordis 风格插件内核
============================================
按 DeepSeek Harness 底层 Cordis 框架（TypeScript）的架构模式实现的
Python 小型插件内核（同步代码库，无 asyncio）。五理念对应：

- 插件  = 带 apply(ctx, config) 的函数或 Service 子类
- Context = 服务仓库（ctx.tools/ctx.llm 稳定键），见 context.Context
- inject = 声明依赖（服务就绪才激活），见 loader.boot 拓扑排序
- 类型化事件 = emit/waterfall/parallel/serial 四种分发模式，
  模式是事件契约，见 events.EventTable
- 注册皆可逆 = effect/on/register 登记清理动作，卸载 LIFO 回滚

后续 agent 的各模块将重组成插件挂到 Context 上。
"""

from __future__ import annotations

from src.cordis.config_rows import validate_rows
from src.cordis.context import Context
from src.cordis.events import declare, declare_events, mode_of, require
from src.cordis.loader import boot, boot_file
from src.cordis.service import Service

__all__ = [
    "Context",
    "Service",
    "declare",
    "declare_events",
    "mode_of",
    "require",
    "boot",
    "boot_file",
    "validate_rows",
]
