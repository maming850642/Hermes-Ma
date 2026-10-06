"""
workspace 插件 —— WorkspaceService 注册为 "workspace"。

T5：挂载状态机接入组合根。boot 时：
    svc = WorkspaceService(provider=ctx.get("storage"))
    ctx.register("workspace", svc)
    state.set_service(svc)          # 进程内访问器（resolve_tools / fs 根切换用）

teardown 时 state.set_service(None)（可逆，防跨测试泄漏）。

worker 子进程与 Web 主进程都 boot_context → 各自持有一个 service
（同一 SQLite 库，跨进程共享挂载状态）。
"""
from __future__ import annotations

import logging

from src.cordis.context import Context

logger = logging.getLogger("hermes.plugins.workspace")


def apply(ctx: Context, config: dict) -> None:
    from src.workspace import state as workspace_state
    from src.workspace.service import WorkspaceService

    provider = ctx.get("storage")
    svc = WorkspaceService(provider=provider)

    ctx.register("workspace", svc)
    workspace_state.set_service(svc)

    def _dispose() -> None:
        workspace_state.set_service(None)

    ctx.effect(lambda: _dispose)
    logger.debug("WorkspaceService 就绪（state.set_service 已接线）")
