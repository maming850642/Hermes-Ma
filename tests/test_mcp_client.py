"""MCP 客户端：Windows 控制台解码 / stdio env / 失败清理 / connect 不抛。

回归背景（2026-09-08）：
- 设置页启用 ddg-search 时 Thread-_readerthread UnicodeDecodeError
  （byte 0xbd）：_kill_transport_children 用 text=True 读中文 Windows
  wmic 的 GBK 输出（「节」= 0xbd），PYTHONUTF8=1 下读线程炸，残留 exe
  清不掉，重试继续 Connection closed，chat /tools 看不到 MCP 工具。
- from fastmcp import Client 在 try 外：没装 fastmcp 的解释器会掀翻
  connect_enabled_all，所有 server 都不连。

2026-09-09: ddg-search MCP 内置为项目依赖——{PYTHON}/{PROJECT_ROOT}
占位符展开、代理从 config.yaml（web_proxy/mcp_env）注入子进程，
不再在 mcp_servers/*.json 里写死 exe 路径和内网代理。
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.mcp.client import (
    McpClientManager,
    McpConnectionState,
    _decode_win_console,
    _expand_placeholders,
    _stdio_env,
)
from src.mcp.config import McpServerConfig


def _cfg(**kw) -> McpServerConfig:
    base = dict(name="ddg-search", transport="stdio", command="uvx")
    base.update(kw)
    return McpServerConfig(**base)


# ============================================
# _decode_win_console
# ============================================
def test_decode_win_console_gbk_jie():
    """中文 wmic 表头「节点」：GBK 首字节 0xbd，utf-8 会炸。"""
    raw = "节点,ParentProcessId,ProcessId\n".encode("gbk")
    assert raw[0] == 0xBD
    text = _decode_win_console(raw)
    assert "ParentProcessId" in text
    assert "ProcessId" in text


def test_decode_win_console_utf8():
    assert _decode_win_console(b"Node,ParentProcessId,ProcessId\n").startswith("Node")


def test_decode_win_console_utf16_le():
    raw = "Node,ParentProcessId\n".encode("utf-16")  # 带 BOM
    assert _decode_win_console(raw).startswith("Node")


def test_decode_win_console_empty():
    assert _decode_win_console(b"") == ""


# ============================================
# _stdio_env
# ============================================
def test_stdio_env_strips_unsafe_keeps_path_applies_cfg(monkeypatch):
    monkeypatch.setenv("PYTHONHOME", "C:\\wrong")
    monkeypatch.setenv("PYTHONPATH", "C:\\also-wrong")
    monkeypatch.setenv("VIRTUAL_ENV", "C:\\venv")
    monkeypatch.setenv("PYTHONSTARTUP", "sitecustomize.py")
    monkeypatch.setenv("PATH", "C:\\Windows")
    monkeypatch.setenv("CONDA_PREFIX", "C:\\envs\\hermes_ma")
    cfg = _cfg(env={"HTTP_PROXY": "http://127.0.0.1:7890", "PATH": "C:\\mcp-bin"})
    env = _stdio_env(cfg)
    assert "PYTHONHOME" not in env
    assert "PYTHONPATH" not in env
    assert "VIRTUAL_ENV" not in env
    assert "PYTHONSTARTUP" not in env
    assert env["CONDA_PREFIX"] == "C:\\envs\\hermes_ma"
    assert env["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert env["PATH"] == "C:\\mcp-bin"  # cfg.env 覆盖


def test_stdio_env_coerces_non_string(monkeypatch):
    cfg = _cfg(env={"N": 1})  # JSON 里偶尔出现数字
    env = _stdio_env(cfg)
    assert env["N"] == "1"


# ============================================
# 占位符展开 / 代理注入（2026-09-09 ddg 内置）
# ============================================
def _fake_settings(monkeypatch, **kv):
    """桩掉 config.settings，测试不依赖开发机真实 config.yaml。"""
    from config import _Settings
    monkeypatch.setattr("config.settings", _Settings(kv))


def test_expand_placeholders_python_and_project_root():
    from config import PROJECT_ROOT
    assert _expand_placeholders("{PYTHON}") == sys.executable
    assert _expand_placeholders("{PROJECT_ROOT}/a.py") == f"{PROJECT_ROOT}/a.py"
    assert _expand_placeholders("{PYTHON} -m x") == f"{sys.executable} -m x"


def test_expand_placeholders_passthrough():
    # 无占位符原样返回（兼容旧的手写绝对路径配置）
    assert _expand_placeholders("C:\\bin\\ddg.exe") == "C:\\bin\\ddg.exe"
    assert _expand_placeholders("") == ""


def test_stdio_env_web_proxy_injected(monkeypatch):
    _fake_settings(monkeypatch, web_proxy="http://198.51.100.1:7890", mcp_env=None)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    env = _stdio_env(_cfg())  # env 未配置 → 代理来自 web_proxy
    assert env["HTTP_PROXY"] == "http://198.51.100.1:7890"
    assert env["HTTPS_PROXY"] == "http://198.51.100.1:7890"


def test_stdio_env_empty_web_proxy_no_inject(monkeypatch):
    # 直连环境：web_proxy 留空且父进程无代理变量时，不得凭空注入
    _fake_settings(monkeypatch, web_proxy="", mcp_env=None)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    env = _stdio_env(_cfg())
    assert "HTTP_PROXY" not in env
    assert "HTTPS_PROXY" not in env


def test_stdio_env_priority_cfg_over_mcp_env_over_web_proxy(monkeypatch):
    _fake_settings(
        monkeypatch,
        web_proxy="http://web:1",
        mcp_env={"HTTP_PROXY": "http://mcp:1", "NO_PROXY": "a"},
    )
    # cfg.env 最高：覆盖 mcp_env/web_proxy 的同名键
    env = _stdio_env(_cfg(env={"HTTP_PROXY": "http://cfg:9"}))
    assert env["HTTP_PROXY"] == "http://cfg:9"
    assert env["NO_PROXY"] == "a"  # mcp_env 的无冲突键照常注入
    # cfg.env 不设 → mcp_env 覆盖 web_proxy
    env2 = _stdio_env(_cfg())
    assert env2["HTTP_PROXY"] == "http://mcp:1"


# ============================================
# _kill_transport_children：GBK 输出不得抛
# ============================================
class _Proc:
    def __init__(self, stdout=b"", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def test_kill_transport_children_gbk_wmic_kills_own_child(monkeypatch):
    pid, ppid = 4242, os.getpid()
    header = "节点,ParentProcessId,ProcessId\n".encode("gbk")
    body = f"PC,{ppid},{pid}\n".encode("gbk")
    calls = []

    def fake_run(cmd, **kwargs):
        assert kwargs.get("text") not in (True,), "必须按字节读，禁止 text=True"
        calls.append(list(cmd))
        if cmd[0] == "wmic":
            return _Proc(header + body)
        return _Proc(b"", 0)

    monkeypatch.setattr("src.mcp.client.subprocess.run", fake_run)
    state = McpConnectionState(config=_cfg(command=r"D:\envs\mcp\Scripts\ddg.exe"))
    McpClientManager._kill_transport_children(state)
    assert any(c[0] == "wmic" for c in calls)
    assert any(c[0] == "taskkill" and "4242" in c for c in calls)


def test_kill_transport_children_skips_foreign_ppid(monkeypatch):
    header = "节点,ParentProcessId,ProcessId\n".encode("gbk")
    body = f"PC,{os.getpid() + 1},9999\n".encode("gbk")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0] == "wmic":
            return _Proc(header + body)
        return _Proc(b"", 0)

    monkeypatch.setattr("src.mcp.client.subprocess.run", fake_run)
    McpClientManager._kill_transport_children(
        McpConnectionState(config=_cfg(command="ddg.exe"))
    )
    assert not any(c[0] == "taskkill" for c in calls)


def test_kill_transport_children_empty_command_noop(monkeypatch):
    monkeypatch.setattr(
        "src.mcp.client.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应 spawn")),
    )
    McpClientManager._kill_transport_children(
        McpConnectionState(config=_cfg(command=""))
    )


# ============================================
# connect() 不把异常抛给 connect_enabled_all
# ============================================
def test_connect_swallows_timeout(monkeypatch, tmp_path):
    mgr = McpClientManager.__new__(McpClientManager)
    mgr._initialized = True
    mgr._configs = {"ddg-search": _cfg()}
    mgr._servers = {}

    class _Loop:
        def run_coro(self, coro, timeout=30.0):
            # 未 await 的 coro 会 ResourceWarning，显式关闭
            try:
                coro.close()
            except Exception:
                pass
            raise TimeoutError("timed out")

    monkeypatch.setattr("src.mcp.client._get_loop_thread", lambda: _Loop())
    monkeypatch.setattr(McpClientManager, "_kill_transport_children", staticmethod(lambda s: None))
    monkeypatch.setattr("src.mcp.client.time.sleep", lambda s: None)
    ok, msg = mgr.connect("ddg-search")
    assert ok is False
    assert "timed out" in msg
    assert mgr._servers["ddg-search"].connected is False


def test_connect_unknown_name():
    mgr = McpClientManager.__new__(McpClientManager)
    mgr._initialized = True
    mgr._configs = {}
    mgr._servers = {}
    ok, msg = mgr.connect("nope")
    assert ok is False
    assert "未找到" in msg
