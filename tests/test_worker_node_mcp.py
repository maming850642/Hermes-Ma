"""
worker_node MCP 连接步骤单测（_connect_mcp_tools）。

背景：waker「立即运行」fork 的 worker_node 子进程此前从不连接 MCP server，
任务提示词指定的 mcp 工具在 waker 运行里不存在（chat 正常、waker 打转的
根因）。本文件锁定新步骤的契约：
- 有任一 server 连接成功 → agent.rebind_tools() 被调
- 全部失败 / connect_enabled_all 抛异常 → 不 rebind、不抛（降级为无 MCP）
- rebind 自身失败 → 吞掉不抛
"""
from unittest.mock import MagicMock

import src.mcp.client as mcp_client_module
from src.wakerflow.worker_node import _connect_mcp_tools


def _patch_manager(monkeypatch, results=None, side_effect=None):
    mgr = MagicMock()
    mgr.connect_enabled_all.return_value = results or {}
    if side_effect is not None:
        mgr.connect_enabled_all.side_effect = side_effect
    monkeypatch.setattr(mcp_client_module, "get_client_manager", lambda: mgr)
    return mgr


def test_connect_success_rebinds(monkeypatch):
    agent = MagicMock()
    mgr = _patch_manager(monkeypatch, results={
        "ddg-search": (True, "已连接 ddg-search，提供 2 个工具"),
        "seq": (False, "连接失败: boom"),
    })
    _connect_mcp_tools(agent)
    mgr.connect_enabled_all.assert_called_once()
    agent.rebind_tools.assert_called_once()


def test_connect_all_failed_no_rebind(monkeypatch):
    agent = MagicMock()
    _patch_manager(monkeypatch, results={"ddg-search": (False, "连接失败: x")})
    _connect_mcp_tools(agent)
    agent.rebind_tools.assert_not_called()


def test_connect_exception_degrades(monkeypatch):
    """测试环境没装 fastmcp 时走的就是这条路径——不能阻断 waker 运行。"""
    agent = MagicMock()
    _patch_manager(
        monkeypatch,
        side_effect=ModuleNotFoundError("No module named 'fastmcp'"),
    )
    _connect_mcp_tools(agent)
    agent.rebind_tools.assert_not_called()


def test_rebind_failure_swallowed(monkeypatch):
    agent = MagicMock()
    agent.rebind_tools.side_effect = RuntimeError("boom")
    _patch_manager(monkeypatch, results={"ddg-search": (True, "ok")})
    _connect_mcp_tools(agent)
    agent.rebind_tools.assert_called_once()


def test_connect_over_budget_no_rebind_no_raise(monkeypatch):
    """连接超过预算：先跑任务（不 rebind、不抛）；resolve 每 turn 动态拉取，
    连接完成后下一轮可见。"""
    import time
    agent = MagicMock()

    def slow_connect():
        time.sleep(1.0)
        return {"ddg-search": (True, "ok")}

    mgr = MagicMock()
    mgr.connect_enabled_all.side_effect = slow_connect
    monkeypatch.setattr(mcp_client_module, "get_client_manager", lambda: mgr)
    _connect_mcp_tools(agent, budget_s=0.05)
    agent.rebind_tools.assert_not_called()


def test_skip_env_guard(monkeypatch):
    agent = MagicMock()
    mgr = _patch_manager(monkeypatch, results={"ddg-search": (True, "ok")})
    monkeypatch.setenv("HERMES_WORKER_NODE_SKIP_MCP", "1")
    _connect_mcp_tools(agent)
    mgr.connect_enabled_all.assert_not_called()
    agent.rebind_tools.assert_not_called()
