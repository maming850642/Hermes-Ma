"""
============================================
manage_mcp 工具组 —— 聊天内接入 / 删除 / 列出 MCP server
============================================
让主 agent 在 chat 里把用户丢来的链接或包名落成可用 MCP：

    list_mcps    列出 enabled/transport/trust/连接状态/工具清单
    create_mcp   校验 → 落盘 mcp_servers/mcp_<name>.json → 连接
    remove_mcp   断开 + 删 json

设计约束：
- 唯一写入口走 McpConfigManager / McpClientManager——校验、原子写、
  连接、失败带 stderr 全部复用，绝不让 LLM 经 bash 直写 mcp_servers/。
- 校验失败一律返回可读错误字符串（不抛异常），与 manage_waker 同一模式。
- 写类工具 YAML 声明 blocked_in: [subagent]——task 子代理不能改 MCP 配置；
  waker（employee）允许，但 install-mcp 技能会警告无人值守审批。
- 同名已存在 → 拒绝并提示先 remove_mcp（避免静默覆盖正在用的 server）。
- connect 限时 CONNECT_TIMEOUT 秒；超时返回「已落盘、后台连接中」，
  线程继续连，下一 turn resolve_tools 能看到新工具。

manager 注入（测试用）：
模块级 holder + set_client_manager，测试注入指向 tmp 目录的 manager；
生产路径 resolve 时 get_client_manager()。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

from src.mcp.config import McpServerConfig
from src.mcp.client import McpClientManager, get_client_manager

logger = logging.getLogger("hermes.tools.manage_mcp")

# create_mcp 同步等待连接的上限。内部 connect 自带一次重试，可能长于此时限；
# 超时后连接线程继续跑，不取消。
CONNECT_TIMEOUT = 45.0

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 计划入参 transport=command 对应底层 stdio；顺带兼容 http → streamable_http
_TRANSPORT_MAP = {
    "command": "stdio",
    "stdio": "stdio",
    "sse": "sse",
    "streamable_http": "streamable_http",
    "http": "streamable_http",
}

_mgr_holder: dict = {}


def set_client_manager(mgr: McpClientManager | None) -> None:
    """注入 McpClientManager（测试指向 tmp 配置目录；生产不调）。"""
    if mgr is None:
        _mgr_holder.pop("mgr", None)
    else:
        _mgr_holder["mgr"] = mgr


def _mgr() -> McpClientManager:
    return _mgr_holder.get("mgr") or get_client_manager()


def _parse_str_list(value: Any) -> list[str] | str:
    """args：list / JSON 数组字符串 / 空白分隔 → list[str]。失败返回错误串。"""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                parsed = json.loads(s)
            except json.JSONDecodeError as e:
                return f"args 不是合法 JSON 数组: {e}"
            if not isinstance(parsed, list):
                return "args JSON 必须是数组"
            return [str(x) for x in parsed]
        return [p for p in s.split() if p]
    return [str(value)]


def _parse_str_dict(value: Any, field: str) -> dict[str, str] | str:
    """env / headers：dict / JSON 对象字符串 → dict[str,str]。失败返回错误串。"""
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items() if k}
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError as e:
            return f"{field} 不是合法 JSON 对象: {e}"
        if not isinstance(parsed, dict):
            return f"{field} JSON 必须是对象"
        return {str(k): str(v) for k, v in parsed.items() if k}
    return f"{field} 必须是对象或 JSON 字符串"


def _tool_full_names(server: str, tool_names: list[str]) -> list[str]:
    return [f"mcp__{server}__{t}" for t in tool_names]


def _connect_with_timeout(mgr: McpClientManager, name: str, timeout: float) -> tuple[str, bool]:
    """同步等 connect，超时则让后台线程继续。

    Returns:
        (message, timed_out)
        timed_out=True 时 message 是「已落盘、后台连接中」类提示，连接仍在跑。
        timed_out=False 时 message 是 connect 的成功/失败原文。
    """
    box: dict[str, tuple[bool, str] | None] = {"result": None}

    def _run() -> None:
        try:
            box["result"] = mgr.connect(name)
        except Exception as e:
            logger.warning(f"create_mcp 连接线程异常 {name}: {e}", exc_info=True)
            box["result"] = (False, str(e))

    t = threading.Thread(target=_run, daemon=True, name=f"mcp-connect-{name}")
    t.start()
    t.join(timeout)
    if t.is_alive():
        return (
            f"已落盘、后台连接中（等待超过 {int(timeout)}s）。"
            f"下一轮用 list_mcps 确认；成功后工具名为 mcp__{name}__<tool>。",
            True,
        )
    result = box["result"]
    if result is None:
        return "连接未返回结果，请用 list_mcps 查看状态。", False
    _ok, msg = result
    return msg, False


# ============================================
# list_mcps
# ============================================
def _execute_list_mcps(*, ctx=None) -> str:
    """列出所有 MCP server 的配置与连接状态。"""
    mgr = _mgr()
    states = mgr.list_servers()
    if not states:
        return (
            "（暂无 MCP server。用户丢来 GitHub 链接或包名时，"
            "先加载 install-mcp 技能，再用 create_mcp 接入。）"
        )

    lines = ["## MCP servers"]
    for st in states:
        cfg = st.config
        enabled = "已启用" if cfg.enabled else "未启用"
        trust = cfg.trust or "approval"
        head = (
            f"- {cfg.name} [{enabled}] transport={cfg.transport} "
            f"trust={trust}"
        )
        if st.connected:
            tools = ", ".join(_tool_full_names(cfg.name, st.tool_names)) or "（无工具）"
            lines.append(f"{head} 已连接 {st.tool_count} 个工具: {tools}")
        else:
            err = (st.error or "").strip() or "未连接"
            # 截断，避免把整段 stderr 灌进上下文
            if len(err) > 800:
                err = err[:800] + "…"
            lines.append(f"{head} 未连接: {err}")
    return "\n".join(lines)


# ============================================
# create_mcp
# ============================================
def _execute_create_mcp(
    *,
    name: str,
    transport: str,
    command: str = "",
    args: Any = None,
    env: Any = None,
    url: str = "",
    headers: Any = None,
    trust: str = "approval",
    ctx=None,
) -> str:
    """校验 + 落盘 + 连接。同名已存在则拒绝。enabled 固定 True。"""
    name = (name or "").strip()
    if not _NAME_RE.fullmatch(name):
        return (
            "创建失败：name 只能含字母/数字/下划线/短横线（1-64 字符），"
            f"收到 {name!r}。"
        )

    raw_transport = (transport or "").strip().lower()
    mapped = _TRANSPORT_MAP.get(raw_transport)
    if mapped is None:
        return (
            f"创建失败：不支持的 transport={transport!r}。"
            "合法值：command（stdio）、sse、streamable_http。"
        )

    parsed_args = _parse_str_list(args)
    if isinstance(parsed_args, str):
        return f"创建失败：{parsed_args}"
    parsed_env = _parse_str_dict(env, "env")
    if isinstance(parsed_env, str):
        return f"创建失败：{parsed_env}"
    parsed_headers = _parse_str_dict(headers, "headers")
    if isinstance(parsed_headers, str):
        return f"创建失败：{parsed_headers}"

    trust_norm = (trust or "approval").strip().lower() or "approval"

    cfg = McpServerConfig(
        name=name,
        enabled=True,  # 接入即启用；要关掉走设置页或先 remove
        transport=mapped,
        command=(command or "").strip(),
        args=parsed_args,
        env=parsed_env,
        url=(url or "").strip(),
        headers=parsed_headers,
        trust=trust_norm,
    )
    err = cfg.validate()
    if err:
        return (
            f"创建失败：{err}\n"
            "command 型必须给 command；sse/streamable_http 必须给 url；"
            "trust 只能是 full / approval / deny。"
        )

    mgr = _mgr()
    mgr.reload_config(force=True)
    if name in mgr._configs:
        return (
            f"创建失败：已存在同名 MCP server「{name}」。"
            "若要替换，先 remove_mcp 再 create_mcp。"
        )

    try:
        mgr.config_manager.save_single(cfg)
    except Exception as e:
        logger.warning(f"create_mcp 落盘失败 {name}: {e}", exc_info=True)
        return f"创建失败：写入配置失败: {e}"
    mgr._configs[name] = cfg

    msg, timed_out = _connect_with_timeout(mgr, name, CONNECT_TIMEOUT)
    if timed_out:
        return msg

    state = mgr._servers.get(name)
    if state is not None and state.connected:
        tools = _tool_full_names(name, state.tool_names)
        listing = ", ".join(tools) if tools else "（server 未暴露工具）"
        return (
            f"已接入 MCP server「{name}」（trust={cfg.trust}，"
            f"transport={cfg.transport}），提供 {state.tool_count} 个工具：\n"
            f"{listing}\n"
            "trust=approval 时调用这些工具仍会弹审批；用户明确信任可 recreate 为 trust=full。"
        )

    detail = msg
    if state is not None and state.error:
        detail = state.error
    if len(detail) > 1200:
        detail = detail[:1200] + "…"
    return (
        f"已落盘但连接失败（「{name}」配置已写入，可用 list_mcps 查看）：{detail}\n"
        "按 stderr / 代理（config.yaml 的 web_proxy）排查后，"
        "先 remove_mcp 再 create_mcp。"
    )


# ============================================
# remove_mcp
# ============================================
def _execute_remove_mcp(*, name: str, ctx=None) -> str:
    """断开并删除 MCP server 配置。"""
    name = (name or "").strip()
    if not name:
        return "失败：name 为空。"
    mgr = _mgr()
    mgr.reload_config(force=True)
    ok, msg = mgr.remove_server(name)
    if not ok:
        known = "、".join(mgr._configs) or "（无）"
        return f"{msg}。现有的：{known}"
    return f"已删除 MCP server「{name}」并断开连接。"
