"""
============================================
McpClientManager - MCP 客户端连接管理
============================================
管理所有 MCP Server 的连接生命周期。

核心能力:
- connect(config): 连接单个 server，拉取工具列表（缓存 raw_tool）
- disconnect(name): 断开单个 server
- connect_enabled_all(): 批量连接所有 enabled 的 server
- shutdown(): 关闭所有连接

工具暴露：已连接 server 的 raw_tool 缓存在 McpConnectionState._raw_tools，
由 src/mcp/tool_factory.py 动态生成 ToolSpec（resolve_tools 组装用）。

异步同步桥接:
- Hermes 的调用链是同步的
- fastmcp 是异步 API
- 内部维护一个独立事件循环线程，同步方法通过 run_coroutine_threadsafe 桥接
"""

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.mcp.config import McpServerConfig, McpConfigManager

logger = logging.getLogger("hermes.mcp.client")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 传给异环境 stdio 子进程时必须剥掉：会覆盖目标解释器的前缀/模块搜索路径，
# 表现为子进程秒退、握手 "Connection closed"。
_UNSAFE_CHILD_ENV = ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV")


def _decode_win_console(data: bytes) -> str:
    """解码 Windows 控制台工具（wmic 等）的字节输出。

    PYTHONUTF8=1 下 text=True 按 utf-8 解，但中文 Windows 的 wmic 输出 GBK
    （首字节常是 0xbd=「节」），_readerthread 直接 UnicodeDecodeError。
    """
    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("mbcs", errors="replace")


def _expand_placeholders(value: str) -> str:
    """展开配置里的可移植占位符：{PYTHON}→当前解释器、{PROJECT_ROOT}→项目根。

    在用时（spawn 前）展开而非加载时，mcp_servers/*.json 里永远存占位符——
    换设备零手工修正，Web 设置页回存也不会把绝对路径写死进配置。
    与 waker 子进程用 sys.executable 同套路。
    """
    if not value or "{" not in value:
        return value
    out = value.replace("{PYTHON}", sys.executable)
    try:
        from config import PROJECT_ROOT
        out = out.replace("{PROJECT_ROOT}", str(PROJECT_ROOT))
    except Exception:
        pass
    return out


def _proxy_overlay() -> dict[str, str]:
    """设备级代理 → MCP 子进程环境（读 config.yaml，缺省静默跳过）。

    优先级（低→高）：web_proxy → mcp_env。httpx 按 HTTP_PROXY/HTTPS_PROXY
    出代理，DDG server 即靠此访问境外；cn 直连环境把 web_proxy 留空即可。
    """
    overlay: dict[str, str] = {}
    try:
        from config import settings
        proxy = (settings.get("web_proxy") or "").strip() if settings else ""
        if proxy:
            overlay["HTTP_PROXY"] = proxy
            overlay["HTTPS_PROXY"] = proxy
        mcp_env = settings.get("mcp_env") if settings else None
        if isinstance(mcp_env, dict):
            overlay.update({str(k): str(v) for k, v in mcp_env.items() if k})
    except Exception:
        pass
    return overlay


def _stdio_env(cfg: McpServerConfig) -> dict[str, str]:
    """父进程完整环境 + 设备级代理(web_proxy/mcp_env) + 用户 cfg.env，剥掉会毒害异解释器的变量。

    2026-09-09: 代理不再写死在 mcp_servers/*.json 的 env 里（内网 IP 不进仓库、
    换设备在 config.yaml 配 web_proxy 即可），优先级 cfg.env > mcp_env > web_proxy。
    """
    merged: dict[str, str] = {}
    for k, v in os.environ.items():
        if not k or v is None or k in _UNSAFE_CHILD_ENV:
            continue
        merged[k] = str(v)
    merged.update(_proxy_overlay())
    for k, v in (cfg.env or {}).items():
        if k:
            merged[str(k)] = str(v)
    return merged


def _stderr_log_path(name: str) -> Path:
    try:
        from config import PROJECT_ROOT
        root = PROJECT_ROOT
    except Exception:
        root = Path.cwd()
    d = Path(root) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in name if c.isalnum() or c in ("-", "_")) or "server"
    return d / f"mcp_{safe}.stderr.log"


def _truncate_file(path: Path) -> None:
    try:
        path.write_bytes(b"")
    except Exception:
        pass


def _read_stderr_tail(name: str, limit: int = 2000) -> str:
    try:
        raw = _stderr_log_path(name).read_bytes()
    except Exception:
        return ""
    text = _decode_win_console(raw).strip()
    return text[-limit:] if len(text) > limit else text


def _attach_stderr(state: "McpConnectionState") -> None:
    extra = _read_stderr_tail(state.config.name)
    if extra and extra not in (state.error or ""):
        state.error = f"{state.error}; stderr: {extra}" if state.error else extra


# ============================================
# 连接状态
# ============================================

@dataclass
class McpConnectionState:
    """单个 MCP Server 的运行时状态"""
    config: McpServerConfig
    connected: bool = False
    tool_count: int = 0
    tool_names: list[str] = field(default_factory=list)
    error: str = ""
    # 内部: fastmcp Client 实例（保持长连接）
    _client: Any = None


# ============================================
# 异步事件循环线程
# ============================================

class _AsyncLoopThread:
    """
    在独立线程中运行 asyncio 事件循环。

    让同步代码能安全调用异步 fastmcp API，
    避免在 Hermes 的同步调用链中嵌套 asyncio.run() 导致冲突。
    """

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self):
        if self.loop is not None and self.loop.is_running():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="mcp-asyncio")
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run(self):
        # Windows 下用 SelectorEventLoop 避免 Proactor 的 pipe 关闭问题
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()

    def run_coro(self, coro, timeout: float = 30.0) -> Any:
        """在独立线程的事件循环中运行协程，同步等待结果"""
        if self.loop is None or not self.loop.is_running():
            self.start()
        future: Future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    def stop(self):
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=3)


# 全局单例：独立事件循环线程
_loop_thread: _AsyncLoopThread | None = None


def _get_loop_thread() -> _AsyncLoopThread:
    global _loop_thread
    if _loop_thread is None:
        _loop_thread = _AsyncLoopThread()
        _loop_thread.start()
    return _loop_thread


# ============================================
# McpClientManager
# ============================================

class McpClientManager:
    """
    MCP 客户端管理器（单例）。

    管理 mcp_servers/ 目录中所有 server 配置（每 server 一个 mcp_<name>.json）的连接状态，
    提供同步 API 供 agent/worker 调用。

    使用方式:
        mgr = McpClientManager()
        mgr.connect_enabled_all()           # 启动时连接所有 enabled server
        specs = generate_mcp_tool_specs()   # 经 tool_factory 生成 ToolSpec
        mgr.shutdown()                      # 退出时清理
    """

    def __init__(self):
        from src.mcp.config import get_mcp_config_path
        self.config_manager = McpConfigManager(get_mcp_config_path())
        self._servers: dict[str, McpConnectionState] = {}
        self._configs: dict[str, McpServerConfig] = {}
        self._initialized = False

    # ---- 配置加载 ----

    def reload_config(self, force: bool = False) -> None:
        """重新加载配置文件夹(不断开已连接的 server)。

        force=True 时无视 _initialized 守卫,允许热加载新加的 server。
        """
        if self._initialized and not force:
            return
        self._configs = self.config_manager.load()
        self._initialized = True
        for name in list(self._servers.keys()):
            if name not in self._configs:
                self.disconnect(name)

    def _ensure_config(self) -> None:
        if not self._initialized:
            self.reload_config()

    # ---- 连接管理 ----

    def _build_transport(self, cfg: McpServerConfig) -> Any:
        """根据配置构造 fastmcp transport 对象"""
        from fastmcp.client.transports import (
            StdioTransport, SSETransport, StreamableHttpTransport,
        )

        if cfg.transport == "stdio":
            # 2026-06-19: 修复 Bug 8 —— 删除 NpxStdioTransport/UvStdioTransport 死分支。
            # 原条件 `cmd in ("npx") and not cfg.args` 要求 args 为空，
            # 但标准 npx/uvx MCP server 至少需要 args=[包名]，所以分支恒不命中。
            # 通用 StdioTransport 已完整支持 command + args，无需特化。
            # 2026-09-07: env 与父进程完整环境合并后传给子进程。mcp sdk 的
            # 默认行为是只继承白名单变量（get_default_environment），
            # pyinstaller 单文件 exe 等对 PATH 之外的变量（CONDA_PREFIX、
            # 语言区域等）敏感时会在白名单环境下启动假死/秒退（表现：
            # "Connection closed"）。合并完整环境 + 用户配置覆盖，对齐
            # zcode/claude code 等 MCP host 的 stdio spawn 行为。
            # 2026-09-08: 剥 PYTHONHOME/PYTHONPATH/VIRTUAL_ENV（异 conda 环境
            # 的 Scripts\\*.exe 会被这些变量绑到父解释器上秒退）；stderr 落到
            # logs/mcp_<name>.stderr.log，握手失败不再只剩空的 Connection closed。
            # 2026-09-09: command/args 支持 {PYTHON}/{PROJECT_ROOT} 占位符，
            # ddg-search 等随项目环境安装的 server 用 -m 方式启动，无需绝对路径。
            err_log = _stderr_log_path(cfg.name)
            _truncate_file(err_log)
            return StdioTransport(
                command=_expand_placeholders(cfg.command),
                args=[_expand_placeholders(a) for a in cfg.args],
                env=_stdio_env(cfg),
                log_file=err_log,
            )
        elif cfg.transport == "sse":
            return SSETransport(cfg.url, headers=cfg.headers or None)
        elif cfg.transport == "streamable_http":
            return StreamableHttpTransport(cfg.url, headers=cfg.headers or None)
        else:
            raise ValueError(f"不支持的传输类型: {cfg.transport}")

    async def _connect_async(self, cfg: McpServerConfig) -> McpConnectionState:
        """异步连接单个 server 并拉取工具列表"""
        state = McpConnectionState(config=cfg)
        # 2026-06-19: 修复 Bug 5 —— 用标志位追踪 client 是否已 __aenter__，
        # 异常时对已进入的 client 调 __aexit__，避免 stdio 子进程/连接泄漏。
        # 2026-09-08: from fastmcp 必须在 try 内——worker 若被没装 fastmcp 的
        # 解释器拉起，ImportError 会掀翻 connect_enabled_all，其它 server 也不连。
        client = None
        entered = False
        try:
            from fastmcp import Client
            transport = self._build_transport(cfg)
            client = Client(transport)
            # 保持长连接: 手动 initialize 而不用 async with
            await client.__aenter__()
            entered = True
            tools = await client.list_tools()
            state._client = client
            state.connected = True
            state.tool_count = len(tools)
            state.tool_names = [t.name for t in tools]
            # 缓存原始工具对象（供 tool_factory 生成 ToolSpec）
            state._raw_tools = tools
            logger.info(
                f"MCP server '{cfg.name}' 连接成功: {len(tools)} 个工具 "
                f"({', '.join(state.tool_names[:5])}{'...' if len(state.tool_names) > 5 else ''})"
            )
        except Exception as e:
            state.connected = False
            state.error = str(e)
            _attach_stderr(state)
            logger.warning(f"MCP server '{cfg.name}' 连接失败: {state.error}")
            # 已 __aenter__ 但后续抛异常 → 必须 __aexit__ 释放资源
            if entered and client is not None:
                try:
                    await client.__aexit__(None, None, None)
                except Exception as cleanup_err:
                    logger.debug(f"MCP server '{cfg.name}' 清理连接时异常: {cleanup_err}")
        return state

    def connect(self, name: str) -> tuple[bool, str]:
        """
        连接单个 server（同步）。

        2026-09-07 加固：失败自动重试 1 次（首次运行被杀软实时扫描拦截
        的 stdio exe 会秒退，表现 "Connection closed"，稍后重试通常即
        成功）+ 失败路径强制清理残留子进程（fastmcp keep_alive/握手
        失败路径不 kill，每次失败泄漏一个 exe 进程）。

        Returns:
            (success, message)
        """
        self._ensure_config()
        if name not in self._configs:
            return False, f"未找到 server 配置: {name}"

        cfg = self._configs[name]
        # 先断开旧连接（注：重连窗口期工具列表会短暂少此 server 的工具，
        # 单线程 CLI 场景无碍，并发路径理论上有微小竞态，当前可接受）
        if name in self._servers and self._servers[name].connected:
            self.disconnect(name)

        loop = _get_loop_thread()
        state: McpConnectionState | None = None
        for attempt in (1, 2):   # 首次 + 失败重试 1 次
            if attempt == 2:
                time.sleep(3.0)
            try:
                state = loop.run_coro(self._connect_async(cfg), timeout=30.0)
            except Exception as e:
                # TimeoutError / 协程未捕获异常不得掀翻 connect_enabled_all
                state = McpConnectionState(config=cfg, connected=False, error=str(e))
                _attach_stderr(state)
            if state.connected:
                break
            # 失败：清理本 transport 可能残留的子进程后再试
            self._kill_transport_children(state)

        if state is None:
            state = McpConnectionState(config=cfg, connected=False, error="未知错误")
        self._servers[name] = state

        if state.connected:
            return True, f"已连接 {name}，提供 {state.tool_count} 个工具"
        else:
            return False, f"连接失败: {state.error}"

    @staticmethod
    def _kill_transport_children(state: "McpConnectionState") -> None:
        """连接失败后按命令行匹配清理残留的 stdio 子进程。

        Windows 实测：fastmcp（keep_alive）/握手失败路径不会 kill 已
        spawn 的子进程，每次失败泄漏一个 exe。按 config.command 匹配
        进程命令行后精确 kill；非 Windows / 无 wmic / 无匹配时静默跳过。
        """
        cmd = (state.config.command or "").strip()
        if not cmd:
            return
        # WMI LIKE 的 % _ 是通配符；路径里的 \ 原样保留（wmic 当字面量）
        like = cmd[:80].replace("'", "\\'").replace("%", "[%]").replace("_", "[_]")
        try:
            # 禁止 text=True：中文 Windows wmic 输出 GBK，「节」= 0xbd，
            # PYTHONUTF8=1 下 _readerthread 会 UnicodeDecodeError，残留 exe 清不掉。
            out = subprocess.run(
                ["wmic", "process", "where",
                 f"commandline like '%{like}%'",
                 "get", "processid,parentprocessid", "/format:csv"],
                capture_output=True, timeout=10,
                creationflags=_NO_WINDOW)
            text = _decode_win_console(out.stdout or b"")
            killed = 0
            for line in text.splitlines():
                cols = line.strip().rstrip(",").split(",")
                # CSV 尾两列：Node,ParentProcessId,ProcessId（顺序随版本，
                # 取最后一列 = ProcessId，倒数第二列 = ParentProcessId）
                if len(cols) < 2:
                    continue
                pid, ppid = cols[-1], cols[-2]
                # 只杀本进程的直接子进程：命令行里含同路径字符串的无关
                # 进程（诊断脚本/其他 host 拉起的实例）不误伤
                if not (pid.isdigit() and ppid.isdigit()):
                    continue
                if int(ppid) != os.getpid():
                    continue
                r = subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True, timeout=10,
                                   creationflags=_NO_WINDOW)
                if r.returncode == 0:
                    killed += 1
            if killed:
                logger.warning(
                    f"MCP server '{state.config.name}' 连接失败，"
                    f"已清理残留子进程 {killed} 个")
        except Exception as e:
            logger.debug(f"清理 MCP 残留子进程跳过/失败: {e}")

    def disconnect(self, name: str) -> None:
        """断开单个 server（同步）"""
        state = self._servers.get(name)
        if state is None or not state.connected:
            self._servers.pop(name, None)
            return

        async def _close():
            try:
                if state._client is not None:
                    await state._client.__aexit__(None, None, None)
            except Exception as e:
                logger.debug(f"关闭 MCP server '{name}' 时异常: {e}")

        try:
            loop = _get_loop_thread()
            loop.run_coro(_close(), timeout=5.0)
        except Exception:
            pass

        state.connected = False
        state._client = None
        self._servers.pop(name, None)
        logger.info(f"MCP server '{name}' 已断开")

    def connect_enabled_all(self) -> dict[str, tuple[bool, str]]:
        """
        批量连接所有 enabled=True 的 server。

        Returns:
            dict[name, (success, message)]
        """
        self._ensure_config()
        results: dict[str, tuple[bool, str]] = {}
        for name, cfg in self._configs.items():
            if cfg.enabled:
                results[name] = self.connect(name)
            else:
                # 记录未启用的（用于 /mcp 显示）
                if name not in self._servers:
                    self._servers[name] = McpConnectionState(config=cfg, connected=False)
        return results

    # ---- 工具获取 ----

    def get_connected_servers(self) -> dict[str, McpConnectionState]:
        """返回所有已连接 server 的状态（raw_tool 缓存供 tool_factory 生成 ToolSpec）。

        工具命名由 tool_factory 决定: mcp__<server>__<tool>，避免与内置工具冲突。
        """
        return {
            name: state for name, state in self._servers.items()
            if state.connected and state._client is not None
        }

    def get_client(self, name: str) -> Any | None:
        """返回已连接 server 的 fastmcp Client（不存在/未连接返回 None）。

        McpExecutor 执行路径的正式读接口：此前 executor 引用不存在的
        manager._clients，AttributeError 被吞成"工具调用失败"，所有 MCP
        工具执行 100% 失败（tool_factory 的发现半边走 _servers/_client，
        两边引用不一致）。收敛到这里，执行方不再耦合私有属性。
        """
        state = self._servers.get(name)
        if state is None or not state.connected or state._client is None:
            return None
        return state._client

    # ---- 状态查询 ----

    def list_servers(self) -> list[McpConnectionState]:
        """返回所有 server 的状态（含未连接的）"""
        self._ensure_config()
        # 确保所有配置项都有状态记录
        for name, cfg in self._configs.items():
            if name not in self._servers:
                self._servers[name] = McpConnectionState(config=cfg)
        return [self._servers[name] for name in self._configs]

    def set_enabled(self, name: str, enabled: bool) -> tuple[bool, str]:
        """
        启用/禁用某个 server（修改配置 + 保存 + 连接/断开）。

        Returns:
            (success, message)
        """
        self._ensure_config()
        if name not in self._configs:
            return False, f"未找到 server: {name}"

        self._configs[name].enabled = enabled
        self.config_manager.save_single(self._configs[name])

        if enabled:
            return self.connect(name)
        else:
            self.disconnect(name)
            return True, f"已禁用 {name}"

    def remove_server(self, name: str) -> tuple[bool, str]:
        """
        删除某个 server 配置(断开 + 删 json 文件)。
        """
        self._ensure_config()
        if name not in self._configs:
            return False, f"未找到 server: {name}"

        self.disconnect(name)
        del self._configs[name]
        self.config_manager.delete_single(name)
        return True, f"已删除 {name}"

    def add_server(self, config: "McpServerConfig") -> tuple[bool, str]:
        """
        添加新 server(校验 + 存 json 文件 + 连接 + rebind)。

        Returns:
            (success, message)
        """
        err = config.validate()
        if err:
            return False, f"配置无效: {err}"
        self.config_manager.save_single(config)
        self._configs[config.name] = config
        if config.enabled:
            return self.connect(config.name)
        return True, f"已添加 {config.name}(未启用)"

    # ---- 生命周期 ----

    def shutdown(self) -> None:
        """关闭所有连接（程序退出时调用）。

        注：shutdown 后 _loop_thread 被置 None、_servers 被清空，本单例不可复用。
        若需重新使用 MCP，应重新 get_client_manager()（会新建单例）+ connect_enabled_all()。
        正常流程（agent 的 shutdown_mcp 在程序退出时调）不会踩到此限制。
        """
        for name in list(self._servers.keys()):
            self.disconnect(name)
        # 停止事件循环线程
        global _loop_thread
        if _loop_thread is not None:
            _loop_thread.stop()
            _loop_thread = None


# ============================================
# 全局单例
# ============================================

_client_manager: McpClientManager | None = None


def get_client_manager() -> McpClientManager:
    """获取全局 McpClientManager 单例"""
    global _client_manager
    if _client_manager is None:
        _client_manager = McpClientManager()
    return _client_manager