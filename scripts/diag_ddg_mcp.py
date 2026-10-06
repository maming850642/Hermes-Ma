"""
ddg-search MCP 连接诊断（2026-09-07；2026-09-09 随内置化改为可移植启动）

用途：定位「ddg-search 连不上（Connection closed）」的环境差异。
必须在 **启动 web 服务的同一个终端/环境** 里运行：
    python scripts/diag_ddg_mcp.py

做三件事：
  1. dump 当前环境变量到 logs/diag_env_dump.txt（供与正常环境 diff）
  2. 裸 spawn「{PYTHON} -m duckduckgo_mcp_server.server」+ 手写 MCP initialize
     握手（不经过 fastmcp）
  3. 用 hermesma 同款 fastmcp StdioTransport 连接

代理与生产同源：读 config.yaml 的 web_proxy（MCP 子进程的 HTTP(S)_PROXY 就来自它）。
"""

import json
import os
import subprocess
import sys
import time

# 2026-09-09: 不再指向独立 conda 环境的 exe，与 mcp_servers/mcp_ddg-search.json
# 的可移植形态一致（{PYTHON} 占位符 = 当前解释器）
LAUNCH = [sys.executable, "-m", "duckduckgo_mcp_server.server",
          "--search-backend", "auto"]


def _web_proxy() -> str:
    try:
        import yaml
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config.yaml"), encoding="utf-8") as f:
            return ((yaml.safe_load(f) or {}).get("web_proxy") or "").strip()
    except Exception:
        return ""


_PROXY = _web_proxy()
PROXY_ENV = {k: _PROXY for k in ("HTTP_PROXY", "HTTPS_PROXY")} if _PROXY else {}

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2024-11-05", "capabilities": {},
    "clientInfo": {"name": "diag", "version": "0.0.1"}}}


def dump_env() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "logs", "diag_env_dump.txt")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for k in sorted(os.environ):
            f.write(f"{k}={os.environ[k]}\n")
    print(f"[1/3] 环境变量已 dump -> {out}（共 {len(os.environ)} 个）")


def bare_handshake(env_mode: str) -> bool:
    """env_mode: 'merge' = 完整环境 + config.yaml 代理（hermesma 现行为）
                'none'  = 不传 env（sdk 用白名单默认环境）"""
    import asyncio
    sys.platform == "win32" and asyncio.set_event_loop_policy(
        asyncio.WindowsSelectorEventLoopPolicy())
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    if env_mode == "merge":
        env = {**os.environ, **PROXY_ENV}
    else:
        env = None

    async def run() -> bool:
        p = StdioServerParameters(command=LAUNCH[0], args=LAUNCH[1:], env=env)
        async with stdio_client(p) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=30)
                return True

    try:
        ok = asyncio.run(asyncio.wait_for(run(), timeout=45))
        print(f"[2/3] 裸 mcp sdk 握手（env_mode={env_mode}）: 成功")
        return True
    except Exception as e:
        print(f"[2/3] 裸 mcp sdk 握手（env_mode={env_mode}）: 失败 "
              f"{type(e).__name__}: {e}")
        return False


def fastmcp_connect() -> None:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport
    import fastmcp
    print(f"[3/3] fastmcp {fastmcp.__version__} 连接中（hermesma 同款）...")
    t = StdioTransport(command=LAUNCH[0], args=LAUNCH[1:],
                       env={**os.environ, **PROXY_ENV})
    c = Client(t)

    async def run():
        await asyncio.wait_for(c.__aenter__(), timeout=40)
        tools = await c.list_tools()
        print(f"[3/3] fastmcp 连接: 成功，工具 {[x.name for x in tools]}")
        await c.__aexit__(None, None, None)

    import asyncio
    try:
        asyncio.run(asyncio.wait_for(run(), timeout=50))
    except Exception as e:
        print(f"[3/3] fastmcp 连接: 失败 {type(e).__name__}: {e}")
        # 2026-09-09: 子进程现在是 python.exe，严禁按镜像名 taskkill
        #（会误杀本终端/web 服务）；生产侧 _kill_transport_children 按
        # 直接子进程 PID 清理，此处仅提示。
        print("  （fastmcp 可能残留一个 python 子进程，重启终端/服务即可回收）")


if __name__ == "__main__":
    print(f"python: {sys.executable}")
    print(f"代理（config.yaml web_proxy）: {_PROXY or '（空=直连）'}")
    dump_env()
    ok_merge = bare_handshake("merge")
    ok_none = bare_handshake("none")
    fastmcp_connect()
    print("\n=== 结论指引 ===")
    if ok_merge and ok_none:
        print("两种 env 模式都成功：本终端环境正常。"
              "若 hermesma 仍失败，差异在 web 进程内部状态——彻底重启服务/机器。")
    elif ok_none and not ok_merge:
        print("仅 merge 失败：完整环境里有变量会让子进程崩——"
              "把 logs/diag_env_dump.txt 与正常机器 diff 找元凶。")
    elif not ok_none:
        print("白名单默认环境也失败：python -m duckduckgo_mcp_server.server "
              "在本终端就起不来——依赖未装（pip install duckduckgo-mcp-server）"
              "或杀软拦截，与 hermesma 无关。")
