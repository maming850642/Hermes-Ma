"""
============================================
核心服务插件（T4）
============================================
把 agent 的核心服务（config/storage/sessions/memory/llm/tools/mcp/skills/
schedule）按 Cordis 插件形态挂到 Context 上，组合根（worker_process /
worker_node / web 主进程）经 boot_context() 一次性启动。

每个插件一个文件，apply(ctx, config) 签名；cordis.yaml（仓库根）是
默认 profile。行为不变原则：插件只做「构造 + 注册」，不改变各服务
自身的语义（tools 的权限决策改为 tools/pre-execute waterfall 事件分发，
等价性由 tests/plugins/test_tools_event.py 保证）。
"""

from __future__ import annotations

from src.plugins.boot import boot_context

__all__ = ["boot_context"]
