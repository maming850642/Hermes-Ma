"""
manage_mcp 工具组测试（chat 内接入 / 删除 / 列出 MCP server）。

覆盖：
- create_mcp：落盘 + 连接成功返回工具全名、同名拒绝、非法 name/transport、
  command 型缺 command、连接失败仍落盘、连接超时返回「后台连接中」
- remove_mcp：删除文件 + 不存在
- list_mcps：空态与有数据态
- 工具声明接线：side_effects / blocked_in
- chat-only 白名单含管理面 + web_*，前缀通配 mcp__*
"""
from __future__ import annotations

import json
import time
import pytest

from src.mcp.client import McpClientManager, McpConnectionState
from src.storage import paths
from src.storage.sqlite_provider import SQLiteProvider
from src.tools import manage_mcp
from src.tools.context import ToolContext
from src.tools.loader import load_builtin_tools
from src.tools.manage_mcp import (
    _execute_create_mcp,
    _execute_list_mcps,
    _execute_remove_mcp,
    set_client_manager,
)
from src.tools.resolve import _name_in_allowlist, resolve_tools
from src.workspace import state as workspace_state
from src.workspace.service import WorkspaceService


@pytest.fixture()
def mgr(tmp_path, monkeypatch):
    """指向 tmp 配置目录的真实 manager，connect 打桩（不真拉子进程）。"""
    monkeypatch.setattr("src.mcp.config.get_mcp_config_path", lambda: tmp_path)
    import src.mcp.client as client_mod
    client_mod._client_manager = None
    m = McpClientManager()

    def fake_connect(name: str):
        cfg = m._configs[name]
        st = McpConnectionState(
            config=cfg, connected=True, tool_count=2,
            tool_names=["search", "fetch"],
        )
        st._client = object()
        m._servers[name] = st
        return True, f"已连接 {name}，提供 2 个工具"

    monkeypatch.setattr(m, "connect", fake_connect)
    set_client_manager(m)
    yield m
    set_client_manager(None)
    client_mod._client_manager = None
    manage_mcp._mgr_holder.clear()


def _json(mgr: McpClientManager, name: str) -> dict:
    p = mgr.config_manager._server_file(name)
    return json.loads(p.read_text(encoding="utf-8"))


# ============================================
# create_mcp
# ============================================
def test_create_mcp_ok_saves_and_returns_tool_names(mgr):
    msg = _execute_create_mcp(
        name="ddg", transport="command",
        command="{PYTHON}", args=["-m", "duckduckgo_mcp_server"],
    )
    assert "已接入" in msg
    assert "mcp__ddg__search" in msg
    assert "mcp__ddg__fetch" in msg
    data = _json(mgr, "ddg")
    assert data["enabled"] is True
    assert data["transport"] == "stdio"
    assert data["command"] == "{PYTHON}"
    assert data["args"] == ["-m", "duckduckgo_mcp_server"]
    assert data["trust"] == "approval"


def test_create_mcp_duplicate_rejected(mgr):
    _execute_create_mcp(name="ddg", transport="command", command="npx")
    msg = _execute_create_mcp(name="ddg", transport="command", command="npx")
    assert "已存在" in msg
    assert "remove_mcp" in msg


def test_create_mcp_invalid_name(mgr):
    msg = _execute_create_mcp(name="有空 格", transport="command", command="npx")
    assert "创建失败" in msg
    assert list(mgr.config_manager.config_path.glob("mcp_*.json")) == []


def test_create_mcp_command_requires_command(mgr):
    msg = _execute_create_mcp(name="x", transport="command")
    assert "创建失败" in msg
    assert "command" in msg.lower() or "stdio" in msg.lower()


def test_create_mcp_sse_requires_url(mgr):
    msg = _execute_create_mcp(name="remote", transport="sse")
    assert "创建失败" in msg
    assert "url" in msg.lower()


def test_create_mcp_connect_failure_keeps_file(mgr, monkeypatch):
    def fail(name):
        cfg = mgr._configs[name]
        st = McpConnectionState(
            config=cfg, connected=False, error="Connection closed; stderr: boom",
        )
        mgr._servers[name] = st
        return False, "连接失败: boom"

    monkeypatch.setattr(mgr, "connect", fail)
    msg = _execute_create_mcp(name="bad", transport="command", command="npx")
    assert "已落盘但连接失败" in msg
    assert "boom" in msg
    assert mgr.config_manager._server_file("bad").exists()


def test_create_mcp_connect_timeout_returns_background(mgr, monkeypatch):
    monkeypatch.setattr(manage_mcp, "CONNECT_TIMEOUT", 0.05)

    def slow(name):
        time.sleep(0.4)
        cfg = mgr._configs[name]
        st = McpConnectionState(config=cfg, connected=True, tool_count=0)
        st._client = object()
        mgr._servers[name] = st
        return True, "ok"

    monkeypatch.setattr(mgr, "connect", slow)
    msg = _execute_create_mcp(name="slow", transport="command", command="npx")
    assert "后台连接中" in msg
    assert mgr.config_manager._server_file("slow").exists()


def test_create_mcp_args_json_string(mgr):
    msg = _execute_create_mcp(
        name="pkg", transport="command", command="npx",
        args='["-y", "@org/server"]',
    )
    assert "已接入" in msg
    assert _json(mgr, "pkg")["args"] == ["-y", "@org/server"]


# ============================================
# remove / list
# ============================================
def test_remove_mcp_ok(mgr):
    _execute_create_mcp(name="ddg", transport="command", command="npx")
    msg = _execute_remove_mcp(name="ddg")
    assert "已删除" in msg
    assert not mgr.config_manager._server_file("ddg").exists()


def test_remove_mcp_missing(mgr):
    msg = _execute_remove_mcp(name="nope")
    assert "未找到" in msg or "不存在" in msg or "失败" in msg


def test_list_mcps_empty(mgr):
    out = _execute_list_mcps()
    assert "暂无" in out


def test_list_mcps_with_data(mgr):
    _execute_create_mcp(name="ddg", transport="command", command="npx")
    out = _execute_list_mcps()
    assert "ddg" in out
    assert "已连接" in out
    assert "mcp__ddg__search" in out


# ============================================
# YAML 接线
# ============================================
def test_manage_mcp_yaml_wiring():
    tools = load_builtin_tools(force=True)
    for name in ("list_mcps", "create_mcp", "remove_mcp"):
        assert name in tools
    create = tools["create_mcp"]
    assert create.side_effects.destructive is True
    assert create.blocked_in == ["subagent"]
    remove = tools["remove_mcp"]
    assert remove.side_effects.destructive is True
    assert "subagent" in remove.blocked_in
    listing = tools["list_mcps"]
    assert listing.side_effects.destructive is False
    assert listing.blocked_in == []


# ============================================
# chat-only 白名单 + 前缀通配
# ============================================
def test_prefix_allowlist():
    assert _name_in_allowlist("mcp__ddg__search", {"mcp__*"})
    assert _name_in_allowlist("web_search", {"web_search", "mcp__*"})
    assert not _name_in_allowlist("bash", {"mcp__*"})
    assert not _name_in_allowlist("bash", {"*"})  # 裸 * 不打穿
    assert _name_in_allowlist("bash", {"bash"})


@pytest.fixture()
def ws(tmp_path):
    paths.set_data_root(tmp_path / "data")
    provider = SQLiteProvider(db_path=tmp_path / "data" / "ws.db")
    svc = WorkspaceService(provider=provider)
    workspace_state.set_service(svc)
    yield svc
    workspace_state.set_service(None)
    provider.close()
    paths.set_data_root(None)


def _settings():
    s = type("S", (), {})()
    s.shell_enabled = True
    s.workspace_chat_only_tools = ""
    return s


def test_chat_only_keeps_mcp_manage_and_web(ws):
    """收件箱开箱即用：MCP 管理面 + web_* 可见，bash 仍隐藏。"""
    ws.choose_chat_only()
    specs = resolve_tools(ToolContext(caller_context="main"), _settings())
    names = {s.name for s in specs}
    assert "create_mcp" in names
    assert "remove_mcp" in names
    assert "list_mcps" in names
    assert "web_search" in names
    assert "web_fetch" in names
    assert "use_skill" in names
    assert "bash" not in names


def test_chat_only_hides_create_mcp_from_subagent(ws):
    ws.choose_chat_only()
    specs = resolve_tools(ToolContext(caller_context="subagent"), _settings())
    names = {s.name for s in specs}
    assert "create_mcp" not in names
    assert "remove_mcp" not in names
    assert "list_mcps" in names
