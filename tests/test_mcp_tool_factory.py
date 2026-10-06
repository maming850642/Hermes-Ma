"""MCP trust 配置链路测试（P1-9）。

回归背景：tool_factory 对不存在符号 get_config_manager 的 import 恒抛
ImportError 被静默吞掉，McpServerConfig 也无 trust 字段——
mcp_servers/mcp_<name>.json 里的 "trust" 全链路失效，所有 MCP 工具
恒按默认 destructive 弹审批。

覆盖：
- McpServerConfig.trust 字段（from_dict 读取 / to_dict 回写 / validate 取值校验）
- mcp_tool_to_spec 按 trust 生成 side_effects
- generate_mcp_tool_specs 经 McpClientManager 连接状态自带的 config 读 trust
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from src.mcp.config import McpConfigManager, McpServerConfig
from src.mcp.tool_factory import _get_server_trust, generate_mcp_tool_specs, mcp_tool_to_spec


class _RawTool:
    """模拟 fastmcp 返回的工具对象。"""

    name = "do_thing"
    description = "does a thing"
    inputSchema = {"type": "object", "properties": {}}


def _cfg(**kw) -> McpServerConfig:
    base = dict(name="srv", transport="stdio", command="uvx")
    base.update(kw)
    return McpServerConfig(**base)


# ============================================
# McpServerConfig.trust 字段
# ============================================
def test_from_dict_reads_trust():
    cfg = McpServerConfig.from_dict("srv", {"transport": "stdio", "command": "x", "trust": "full"})
    assert cfg.trust == "full"


def test_from_dict_trust_default_approval():
    """未配置 trust → 默认 approval（最安全）。"""
    cfg = McpServerConfig.from_dict("srv", {"transport": "stdio", "command": "x"})
    assert cfg.trust == "approval"


def test_to_dict_roundtrips_trust():
    cfg = _cfg(trust="deny")
    d = cfg.to_dict()
    assert d["trust"] == "deny"
    assert McpServerConfig.from_dict("srv", d).trust == "deny"


def test_validate_rejects_bad_trust():
    assert _cfg(trust="bogus").validate() is not None
    assert "trust" in _cfg(trust="bogus").validate()
    # 三级合法值都通过
    for t in ("full", "approval", "deny"):
        assert _cfg(trust=t).validate() is None


def test_config_manager_persists_trust(tmp_path):
    """trust 落盘 mcp_<name>.json 并能 load 回来（用户配置真实生效的前提）。"""
    mgr = McpConfigManager(config_path=tmp_path)
    mgr.save_single(_cfg(trust="full"))
    loaded = mgr.load()
    assert loaded["srv"].trust == "full"


# ============================================
# tool_factory：trust → side_effects
# ============================================
def test_trust_full_marks_only_network_access():
    spec = mcp_tool_to_spec(_RawTool(), mcp_client=None, server_name="srv", server_config=_cfg(trust="full"))
    assert spec.side_effects.destructive is False
    assert spec.side_effects.network_access is True


def test_trust_approval_default_marks_destructive():
    spec = mcp_tool_to_spec(_RawTool(), mcp_client=None, server_name="srv", server_config=_cfg(trust="approval"))
    assert spec.side_effects.destructive is True
    assert spec.side_effects.network_access is True


def test_trust_deny_marks_destructive():
    spec = mcp_tool_to_spec(_RawTool(), mcp_client=None, server_name="srv", server_config=_cfg(trust="deny"))
    assert spec.side_effects.destructive is True


def test_get_server_trust_defaults():
    assert _get_server_trust(None) == "approval"
    assert _get_server_trust({"trust": "full"}) == "full"
    assert _get_server_trust(_cfg(trust="full")) == "full"


# ============================================
# generate_mcp_tool_specs：从连接状态自带 config 读 trust（死 import 修复）
# ============================================
@dataclass
class _FakeState:
    config: McpServerConfig
    connected: bool = True
    _client: object = None
    _raw_tools: list = field(default_factory=list)


class _FakeManager:
    def __init__(self, config):
        self._servers = {"srv": _FakeState(config=config, _client=object(), _raw_tools=[_RawTool()])}


def test_generate_specs_applies_trust_from_connection_state(monkeypatch):
    """json 配 trust=full 后，生成的 ToolSpec side_effects 反映信任级别。"""
    import src.mcp.client as mcp_client_mod

    monkeypatch.setattr(
        mcp_client_mod, "get_client_manager",
        lambda: _FakeManager(_cfg(trust="full")),
    )
    specs = generate_mcp_tool_specs()
    assert len(specs) == 1
    assert specs[0].name == "mcp__srv__do_thing"
    assert specs[0].side_effects.destructive is False


def test_generate_specs_default_trust_stays_conservative(monkeypatch):
    import src.mcp.client as mcp_client_mod

    monkeypatch.setattr(
        mcp_client_mod, "get_client_manager",
        lambda: _FakeManager(_cfg(trust="approval")),
    )
    specs = generate_mcp_tool_specs()
    assert len(specs) == 1
    assert specs[0].side_effects.destructive is True


# ============================================
# McpExecutor 执行路径（假 manager/server/client，不连真实 MCP）
# ============================================


class _FakeMcpClient:
    """模拟 fastmcp Client：记录 call_tool 入参，返回固定结果或抛错。"""

    def __init__(self, result=None, error: Exception | None = None):
        self.calls: list[tuple] = []
        self._result = result
        self._error = error

    async def call_tool(self, tool, args):
        self.calls.append((tool, args))
        if self._error is not None:
            raise self._error
        return self._result


class _FakeText:
    def __init__(self, text: str):
        self.text = text


class _FakeCallResult:
    """模拟 fastmcp CallToolResult（McpExecutor._format_result 的输入形状）。"""

    def __init__(self, text: str, is_error: bool = False):
        self.content = [_FakeText(text)]
        self.isError = is_error


class _FakeLoopThread:
    """同步桥替身：在当前线程直接跑完协程。"""

    def run_coro(self, coro, timeout: float = 30.0):
        import asyncio

        return asyncio.run(coro)


def _manager_with_client(server: str, client):
    """构造真实 McpClientManager（绕过 __init__ 的配置目录读取），
    直接注入一个已连接的连接状态——执行路径覆盖的最小真实面。"""
    from src.mcp.client import McpClientManager, McpConnectionState

    mgr = McpClientManager.__new__(McpClientManager)
    state = McpConnectionState(config=_cfg(name=server), connected=True)
    state._client = client
    mgr._servers = {server: state}
    return mgr


def test_mcp_executor_executes_via_get_client(monkeypatch):
    """execute 走 get_client → call_tool → 格式化文本，入参原样到达 client。

    回归：executor 此前引用不存在的 manager._clients，AttributeError 被
    except 吞成"MCP 工具调用失败"，所有 MCP 工具执行 100% 失败（第一轮
    trust 修复只接了发现半边 tool_factory，执行半边仍是断的）。
    """
    import src.mcp.client as mcp_client_mod
    from src.tools.executors.mcp import McpExecutor

    client = _FakeMcpClient(result=_FakeCallResult("pong"))
    monkeypatch.setattr(
        mcp_client_mod, "get_client_manager", lambda: _manager_with_client("srv", client)
    )
    monkeypatch.setattr(mcp_client_mod, "_get_loop_thread", lambda: _FakeLoopThread())

    result = McpExecutor(server="srv", tool="do_thing").execute({"x": 1}, ctx=None)

    assert result.content == "pong"
    assert client.calls == [("do_thing", {"x": 1})]


def test_mcp_executor_unknown_server_structured_error(monkeypatch):
    """server 不存在 → 结构化"未连接"错误，而非异常兜底的"工具调用失败"。"""
    import src.mcp.client as mcp_client_mod
    from src.tools.executors.mcp import McpExecutor

    monkeypatch.setattr(
        mcp_client_mod,
        "get_client_manager",
        lambda: _manager_with_client("srv", _FakeMcpClient(result=_FakeCallResult("pong"))),
    )
    monkeypatch.setattr(mcp_client_mod, "_get_loop_thread", lambda: _FakeLoopThread())

    result = McpExecutor(server="nope", tool="do_thing").execute({}, ctx=None)

    assert "nope" in result.content
    assert "未连接" in result.content
    assert "MCP 工具调用失败" not in result.content


def test_get_client_read_interface():
    """get_client 正式读接口：已连接返回 client，未连接/不存在返回 None。"""
    from src.mcp.client import McpClientManager, McpConnectionState

    mgr = McpClientManager.__new__(McpClientManager)
    down = McpConnectionState(config=_cfg(name="down"), connected=False)
    mgr._servers = {"down": down}
    assert mgr.get_client("missing") is None
    assert mgr.get_client("down") is None

    ok = McpConnectionState(config=_cfg(name="ok"), connected=True)
    ok._client = object()
    mgr._servers["ok"] = ok
    assert mgr.get_client("ok") is ok._client


def test_mcp_executor_reconnects_on_connection_closed(monkeypatch):
    """stdio 子进程挂掉（Connection closed）→ 重连后再打一次。"""
    import src.mcp.client as mcp_client_mod
    from src.tools.executors.mcp import McpExecutor

    class Flaky(_FakeMcpClient):
        def __init__(self):
            super().__init__(result=_FakeCallResult("recovered"))
            self.n = 0

        async def call_tool(self, tool, args):
            self.n += 1
            self.calls.append((tool, args))
            if self.n == 1:
                raise ConnectionError("Connection closed")
            return self._result

    client = Flaky()
    mgr = _manager_with_client("srv", client)
    mgr.connect = lambda name: (True, "reconnected")
    monkeypatch.setattr(mcp_client_mod, "get_client_manager", lambda: mgr)
    monkeypatch.setattr(mcp_client_mod, "_get_loop_thread", lambda: _FakeLoopThread())

    result = McpExecutor(server="srv", tool="do_thing").execute({"x": 1}, ctx=None)
    assert result.content == "recovered"
    assert client.n == 2


def test_mcp_executor_closed_includes_stderr(monkeypatch):
    """重连仍失败时，把子进程 stderr 拼进错误，避免只剩空的 Connection closed。"""
    import src.mcp.client as mcp_client_mod
    from src.tools.executors.mcp import McpExecutor

    client = _FakeMcpClient(error=ConnectionError("Connection closed"))
    mgr = _manager_with_client("srv", client)
    mgr.connect = lambda name: (False, "still dead")
    monkeypatch.setattr(mcp_client_mod, "get_client_manager", lambda: mgr)
    monkeypatch.setattr(mcp_client_mod, "_get_loop_thread", lambda: _FakeLoopThread())
    monkeypatch.setattr(
        mcp_client_mod, "_read_stderr_tail", lambda name: "UnicodeEncodeError: surrogates"
    )

    result = McpExecutor(server="srv", tool="do_thing").execute({}, ctx=None)
    assert "Connection closed" in result.content
    assert "UnicodeEncodeError" in result.content
