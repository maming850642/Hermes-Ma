"""
scheduler 插件装配 —— 统一调度服务 SchedulerService 注册为 "schedule"。

P3-3：SchedulerService 本体已下沉到 src/scheduling/service.py（storage /
memory / waker 等下层模块顶层直接 import src.scheduling，不再上探
plugins 层，消除依赖倒置）。本模块只保留 cordis 插件入口（cordis.yaml
的 "src.plugins.scheduler_plugin:apply"，键 "schedule"），并原样
re-export 旧路径符号——waker/wakerflow 与既有测试仍从旧路径 import，
保持零迁移成本；新代码请直接用 src.scheduling。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.scheduling.service import _MAX_STEP_SECONDS, SchedulerService

if TYPE_CHECKING:
    from src.cordis.context import Context

__all__ = ["SchedulerService", "_MAX_STEP_SECONDS"]


def apply(ctx: "Context", config: dict) -> None:
    """scheduler 插件入口：SchedulerService 注册为 "schedule"。"""
    ctx.register("schedule", SchedulerService())
