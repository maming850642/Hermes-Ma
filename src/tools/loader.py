"""
============================================
YAML 加载器 —— tools/*.yaml → ToolSpec
============================================
扫描 tools/ 目录的 YAML 工具定义，加载成 ToolSpec 对象。

YAML 结构：

    name: bash
    description: ...
    parameters: {JSON Schema}
    runtime:
      type: shell | python | mcp
      # shell: commandField, timeoutField, cwd
      # python: module, function
      # mcp: server, tool
    side_effects:
      destructive: true
      writes_state: false
      network_access: false
      spawns_process: true
    side_effects_evaluator:     # 可选（Layer 3 custom）
      module: src.tools.shell_safety
      function: classify
    side_effects_overrides:     # 可选（Layer 3 regex）
      - field: url
        patterns: [...]
        override: {force_deny: true}
    config_guard: shell_enabled  # 可选（配置门控）
    blocked_in: [employee]       # 可选（caller_context 黑名单，默认 []）

加载策略：
    - 顶层 import 不触发扫描（避免启动时副作用）。
    - load_builtin_tools() 显式调用，扫描 tools/ 目录，缓存结果。
    - 支持 force_reload() 清缓存（MCP 热刷新场景）。
"""

from __future__ import annotations

import logging
import importlib
from pathlib import Path
from typing import Any

import yaml

from src.tools.schema import (
    SideEffects,
    SideEffectsEvaluator,
    SideEffectsOverride,
    SideEffectsRegexOverride,
    ToolSpec,
)

logger = logging.getLogger("hermes.tools.loader")

# tools/ 目录位置（项目根/tools）
_TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "tools"


# ════════════════════════════════════════════════════════════════
# 执行器工厂：runtime.type → ToolExecutor 实例
# ════════════════════════════════════════════════════════════════

def _build_executor(runtime_cfg: dict[str, Any]) -> Any:
    """根据 runtime.type 构造执行器实例。"""
    rtype = runtime_cfg.get("type", "")
    if rtype == "shell":
        from src.tools.executors.shell import ShellExecutor
        return ShellExecutor(
            command_field=runtime_cfg.get("commandField", "command"),
            timeout_field=runtime_cfg.get("timeoutField", "timeout"),
            cwd=runtime_cfg.get("cwd", "workDir"),
        )
    elif rtype == "python":
        from src.tools.executors.python import PythonExecutor
        return PythonExecutor(
            module=runtime_cfg["module"],
            function=runtime_cfg["function"],
        )
    elif rtype == "mcp":
        from src.tools.executors.mcp import McpExecutor
        return McpExecutor(
            server=runtime_cfg["server"],
            tool=runtime_cfg["tool"],
        )
    else:
        raise ValueError(f"未知 runtime.type: {rtype!r}（合法值: shell/python/mcp）")


# ════════════════════════════════════════════════════════════════
# Layer 3 解析
# ════════════════════════════════════════════════════════════════

def _resolve_evaluator(se_eval_cfg: dict[str, Any]) -> SideEffectsEvaluator:
    """把 YAML 的 side_effects_evaluator 段解析成可调用函数。"""
    module = importlib.import_module(se_eval_cfg["module"])
    fn = getattr(module, se_eval_cfg["function"])
    return fn


def _parse_se_overrides(raw: list[dict[str, Any]]) -> list[SideEffectsRegexOverride]:
    """解析 side_effects_overrides 列表。"""
    result = []
    for item in raw:
        override_cfg = item.get("override", {})
        override = SideEffectsOverride(
            destructive=override_cfg.get("destructive"),
            force_deny=override_cfg.get("force_deny", False),
        )
        result.append(SideEffectsRegexOverride(
            field=item["field"],
            patterns=item["patterns"],
            override=override,
            reason=item.get("reason", ""),
        ))
    return result


# ════════════════════════════════════════════════════════════════
# 单文件加载
# ════════════════════════════════════════════════════════════════

def load_tool_from_yaml(path: Path) -> ToolSpec:
    """加载单个 YAML 工具定义文件 → ToolSpec。

    Raises:
        KeyError: YAML 缺少必需字段（name/description/parameters/runtime）。
        ValueError: runtime.type 不合法。
    """
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    name = data["name"]
    description = data["description"]
    parameters = data["parameters"]
    runtime_cfg = data["runtime"]

    executor = _build_executor(runtime_cfg)

    # side_effects
    se_cfg = data.get("side_effects", {})
    side_effects = SideEffects(
        destructive=se_cfg.get("destructive", False),
        writes_state=se_cfg.get("writes_state", False),
        network_access=se_cfg.get("network_access", False),
        spawns_process=se_cfg.get("spawns_process", False),
    )

    # Layer 3
    se_evaluator = None
    if "side_effects_evaluator" in data:
        se_evaluator = _resolve_evaluator(data["side_effects_evaluator"])

    se_overrides = _parse_se_overrides(data.get("side_effects_overrides", []))

    # 配置门控
    config_guard = data.get("config_guard")

    # caller_context 黑名单（默认 [] 不限制）
    blocked_in = [str(item) for item in data.get("blocked_in", []) or []]

    return ToolSpec(
        name=name,
        description=description,
        parameters=parameters,
        executor=executor,
        side_effects=side_effects,
        se_evaluator=se_evaluator,
        se_overrides=se_overrides,
        source="yaml",
        config_guard=config_guard,
        blocked_in=blocked_in,
    )


# ════════════════════════════════════════════════════════════════
# 批量加载 + 缓存
# ════════════════════════════════════════════════════════════════

_cache: dict[str, ToolSpec] | None = None


def load_builtin_tools(force: bool = False) -> dict[str, ToolSpec]:
    """扫描 tools/ 目录，加载全部 YAML 工具定义。结果缓存。

    Args:
        force: True 时清缓存重扫（MCP 热刷新场景）。

    Returns:
        {tool_name: ToolSpec} 字典。
    """
    global _cache
    if _cache is not None and not force:
        return _cache

    tools: dict[str, ToolSpec] = {}

    if not _TOOLS_DIR.is_dir():
        logger.warning(f"tools 目录不存在: {_TOOLS_DIR}")
        _cache = tools
        return tools

    for yfile in sorted(_TOOLS_DIR.glob("*.yaml")):
        try:
            spec = load_tool_from_yaml(yfile)
            tools[spec.name] = spec
            logger.debug(f"加载工具 {spec.name} ← {yfile.name}")
        except Exception as e:
            logger.error(f"加载 {yfile} 失败: {e}", exc_info=True)

    logger.info(f"工具加载完成: {len(tools)} 个内置工具")
    _cache = tools
    return tools


def get_tool(name: str) -> ToolSpec | None:
    """按名字查单个工具（从缓存）。"""
    cache = load_builtin_tools()
    return cache.get(name)
