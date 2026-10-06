"""
============================================
WakerFlow DSL 解析器 —— YAML 文本 → FlowSpec
============================================
纯函数模块：parse_flow(yaml_text) → FlowSpec，做完整校验。

校验规则（任一失败抛 FlowParseError）：
1. YAML 语法合法
2. name 非空、匹配 ^[a-zA-Z0-9_-]{1,64}$
3. 每个 step 必须有 id
4. step id 全局唯一（含 parallel/pipeline 内部）
5. 每个 step 恰好一种节点类型（worker/parallel/pipeline/ask_user/action 五选一）
6. worker 节点必须有 worker + task 字段
7. parallel/pipeline 至少 1 个子节点
8. ask_user 至少 1 个 option
9. action 必须有 url
10. 依赖校验：所有 {{steps.XX...}} 引用里的 step id 必须存在（不严格校验顺序）

不校验：worker 引用的 waker 名是否真的存在（运行时由 executor/store 验证）。
"""
from __future__ import annotations

import re
from typing import Any

import yaml

from src.wakerflow.models import (
    ActionConfig,
    AskUserConfig,
    FlowSpec,
    InputField,
    StepNode,
)
from src.wakerflow.template import find_references

# Flow name 同 waker name 一致：字母数字下划线短横，1-64 字符
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# 五种节点类型 key
_NODE_KEYS = ("worker", "parallel", "pipeline", "ask_user", "action")


class FlowParseError(ValueError):
    """Flow DSL 解析或校验失败。"""


# ============================================
# 主入口
# ============================================
def parse_flow(yaml_text: str) -> FlowSpec:
    """解析 YAML 文本为 FlowSpec，做完整校验。

    Args:
        yaml_text: YAML 文本

    Returns:
        FlowSpec（raw_yaml 字段保存原始文本）

    Raises:
        FlowParseError: 任何解析/校验失败
    """
    if not isinstance(yaml_text, str):
        raise FlowParseError(f"yaml_text 须为字符串，得到 {type(yaml_text).__name__}")

    # 1. YAML 语法
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise FlowParseError(f"YAML 语法错误: {e}") from e

    if data is None:
        raise FlowParseError("Flow YAML 为空")
    if not isinstance(data, dict):
        raise FlowParseError(
            f"Flow YAML 顶层须为 mapping，得到 {type(data).__name__}"
        )

    # 2. name
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise FlowParseError("Flow 缺少 name 字段（或非字符串）")
    if not _NAME_RE.match(name):
        raise FlowParseError(
            f"非法 Flow name: {name!r}（须匹配 {_NAME_RE.pattern}）"
        )

    description = data.get("description", "") or ""

    # 3. inputs
    inputs = _parse_inputs(data.get("inputs"))

    # 4. steps（递归构建 + 校验）
    raw_steps = data.get("steps", []) or []
    if not isinstance(raw_steps, list):
        raise FlowParseError("steps 须为列表")
    seen_ids: set[str] = set()
    steps = [_parse_step(s, seen_ids, path="steps") for s in raw_steps]

    # 5. returns
    returns = data.get("returns", {}) or {}
    if not isinstance(returns, dict):
        raise FlowParseError("returns 须为 mapping")

    # 6. 调度配置（与 WakerConfig 对齐字段名）
    sched = _parse_schedule(data)

    spec = FlowSpec(
        name=name,
        description=description if isinstance(description, str) else str(description),
        inputs=inputs,
        steps=steps,
        returns=returns,
        raw_yaml=yaml_text,
        **sched,
    )

    # 6. 依赖校验：所有 {{steps.XX...}} 引用里的 step id 必须存在
    _check_step_references(spec)

    return spec


# ============================================
# schedule 配置（与 WakerConfig 对齐）
# ============================================
_SCHED_TYPES = {"none", "interval", "daily"}


def _parse_schedule(data: dict) -> dict:
    """解析顶层调度配置字段，返回 kwargs dict 供 FlowSpec 构造。

    所有字段可选，缺失用 FlowSpec 默认值。校验 schedule_type / interval /
    daily_at / api 字段类型。旧 YAML（无这些字段）→ 返回空 dict，全用默认值。
    """
    out: dict = {}
    if "enabled" in data:
        out["enabled"] = bool(data["enabled"])
    if "schedule_type" in data:
        st = data["schedule_type"]
        if st not in _SCHED_TYPES:
            raise FlowParseError(
                f"非法 schedule_type: {st!r}（须为 {sorted(_SCHED_TYPES)}）"
            )
        out["schedule_type"] = st
    if "interval_minutes" in data:
        im = data["interval_minutes"]
        if isinstance(im, bool) or not isinstance(im, int):
            raise FlowParseError(f"interval_minutes 须为整数，得到 {im!r}")
        if im <= 0:
            raise FlowParseError(f"interval_minutes 须为正整数，得到 {im}")
        out["interval_minutes"] = im
    if "daily_at" in data:
        da = data["daily_at"]
        if not isinstance(da, str):
            raise FlowParseError(f"daily_at 须为字符串，得到 {da!r}")
        _check_hhmm(da)
        out["daily_at"] = da
    if "api_enabled" in data:
        out["api_enabled"] = bool(data["api_enabled"])
    if "api_token" in data:
        at = data["api_token"]
        if not isinstance(at, str):
            raise FlowParseError(f"api_token 须为字符串，得到 {at!r}")
        out["api_token"] = at
    if "max_runs" in data:
        mr = data["max_runs"]
        if isinstance(mr, bool) or not isinstance(mr, int):
            raise FlowParseError(f"max_runs 须为整数，得到 {mr!r}")
        out["max_runs"] = mr
    if "expire_at" in data:
        ea = data["expire_at"]
        if not isinstance(ea, str):
            raise FlowParseError(f"expire_at 须为字符串，得到 {ea!r}")
        out["expire_at"] = ea
    return out


def _check_hhmm(s: str) -> None:
    """校验 "HH:MM" 格式（与 WakerConfig._parse_hhmm 一致）。"""
    parts = s.split(":")
    if len(parts) != 2:
        raise FlowParseError(f"daily_at 格式错（须 'HH:MM'），得到 {s!r}")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        raise FlowParseError(f"daily_at 时分须为整数，得到 {s!r}")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise FlowParseError(f"daily_at 越界，得到 {s!r}")


# ============================================
# inputs
# ============================================
def _parse_inputs(raw: Any) -> list[InputField]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise FlowParseError("inputs 须为 mapping")

    out: list[InputField] = []
    for name, body in raw.items():
        if not isinstance(name, str) or not name:
            raise FlowParseError(f"input 名非法: {name!r}")
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise FlowParseError(
                f"input {name!r} 须为 mapping，得到 {type(body).__name__}"
            )

        f = InputField(
            name=name,
            type=body.get("type", "string"),
            required=bool(body.get("required", False)),
            default=body.get("default", None),
            enum=body.get("enum", None),
        )

        if f.type not in ("string", "number", "boolean"):
            raise FlowParseError(
                f"input {name!r} 非法 type {f.type!r}（须为 string/number/boolean）"
            )

        # default 不为 None 时做类型 / enum 一致性轻校验
        if f.default is not None:
            _check_default(name, f.type, f.default, f.enum)

        out.append(f)
    return out


def _check_default(name: str, type_: str, default: Any, enum: list | None) -> None:
    if type_ == "string" and not isinstance(default, str):
        raise FlowParseError(
            f"input {name!r} default 须为 string，得到 {type(default).__name__}"
        )
    if type_ == "number" and (
        isinstance(default, bool) or not isinstance(default, (int, float))
    ):
        raise FlowParseError(
            f"input {name!r} default 须为 number，得到 {type(default).__name__}"
        )
    if type_ == "boolean" and not isinstance(default, bool):
        raise FlowParseError(
            f"input {name!r} default 须为 boolean，得到 {type(default).__name__}"
        )
    if enum is not None and default not in enum:
        raise FlowParseError(
            f"input {name!r} default {default!r} 不在 enum {enum!r} 内"
        )


# ============================================
# step
# ============================================
def _parse_step(raw: Any, seen_ids: set[str], path: str) -> StepNode:
    """递归解析单个 step。

    Args:
        raw: 原始 dict
        seen_ids: 已收集的 id 集合（用于全局唯一校验，跨 parallel/pipeline）
        path: 错误定位用，如 "steps" / "steps[1].parallel[0]"
    """
    if not isinstance(raw, dict):
        raise FlowParseError(f"{path} 须为 mapping，得到 {type(raw).__name__}")

    # id
    sid = raw.get("id")
    if not isinstance(sid, str) or not sid:
        raise FlowParseError(f"{path} 缺少 id 字段（或非字符串）")

    if sid in seen_ids:
        raise FlowParseError(f"step id 重复: {sid!r}（在 {path}）")
    seen_ids.add(sid)

    # 五种节点类型互斥
    present_keys = [k for k in _NODE_KEYS if k in raw and raw[k] is not None]
    if len(present_keys) == 0:
        raise FlowParseError(
            f"step {sid!r} 缺少节点类型（须恰有一个: {list(_NODE_KEYS)})"
        )
    if len(present_keys) > 1:
        raise FlowParseError(
            f"step {sid!r} 出现多种节点类型 {present_keys!r}（须恰好一种）"
        )

    # YAML `if:` → if_cond
    if_cond = raw.get("if", None)
    if if_cond is not None and not isinstance(if_cond, str):
        raise FlowParseError(f"step {sid!r} 的 if 须为字符串")

    tools = raw.get("tools", None)
    if tools is not None:
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise FlowParseError(f"step {sid!r} 的 tools 须为字符串列表")

    permission_mode = raw.get("permission_mode", None)
    if permission_mode is not None and not isinstance(permission_mode, str):
        raise FlowParseError(f"step {sid!r} 的 permission_mode 须为字符串")

    node_key = present_keys[0]
    node_val = raw[node_key]

    worker = task = None
    parallel = pipeline = None
    ask_user = None
    action = None

    if node_key == "worker":
        if not isinstance(node_val, str) or not node_val:
            raise FlowParseError(f"step {sid!r} 的 worker 须为非空字符串")
        worker = node_val
        # task 字段（同级别）必填
        task_val = raw.get("task", None)
        if not isinstance(task_val, str) or not task_val:
            raise FlowParseError(f"step {sid!r} 的 worker 节点缺少 task")
        task = task_val

    elif node_key == "parallel":
        if not isinstance(node_val, list) or len(node_val) == 0:
            raise FlowParseError(f"step {sid!r} 的 parallel 至少需要 1 个子节点")
        parallel = [
            _parse_step(child, seen_ids, path=f"{path}[{sid}].parallel[{i}]")
            for i, child in enumerate(node_val)
        ]

    elif node_key == "pipeline":
        if not isinstance(node_val, list) or len(node_val) == 0:
            raise FlowParseError(f"step {sid!r} 的 pipeline 至少需要 1 个子节点")
        pipeline = [
            _parse_step(child, seen_ids, path=f"{path}[{sid}].pipeline[{i}]")
            for i, child in enumerate(node_val)
        ]

    elif node_key == "ask_user":
        ask_user = _parse_ask_user(sid, node_val)

    elif node_key == "action":
        action = _parse_action(sid, node_val)

    return StepNode(
        id=sid,
        worker=worker,
        task=task,
        parallel=parallel,
        pipeline=pipeline,
        ask_user=ask_user,
        action=action,
        if_cond=if_cond,
        tools=tools,
        permission_mode=permission_mode,
    )


def _parse_ask_user(sid: str, raw: Any) -> AskUserConfig:
    if not isinstance(raw, dict):
        raise FlowParseError(
            f"step {sid!r} 的 ask_user 须为 mapping，得到 {type(raw).__name__}"
        )

    question = raw.get("question", "")
    if not isinstance(question, str) or not question:
        raise FlowParseError(f"step {sid!r} 的 ask_user 缺少 question")

    options = raw.get("options", [])
    if not isinstance(options, list) or len(options) == 0:
        raise FlowParseError(f"step {sid!r} 的 ask_user 至少需要 1 个 option")
    norm_opts: list[dict] = []
    for i, o in enumerate(options):
        if not isinstance(o, dict) or "label" not in o or "value" not in o:
            raise FlowParseError(
                f"step {sid!r} 的 ask_user.options[{i}] 须含 label 和 value"
            )
        norm_opts.append({"label": o["label"], "value": o["value"]})

    timeout = raw.get("timeout", 86400)
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise FlowParseError(f"step {sid!r} 的 ask_user.timeout 须为整数")

    default = raw.get("default", None)
    if default is not None and not any(o["value"] == default for o in norm_opts):
        raise FlowParseError(
            f"step {sid!r} 的 ask_user.default {default!r} 不在 options 里"
        )

    return AskUserConfig(
        question=question,
        options=norm_opts,
        timeout=timeout,
        default=default,
    )


def _parse_action(sid: str, raw: Any) -> ActionConfig:
    if not isinstance(raw, dict):
        raise FlowParseError(
            f"step {sid!r} 的 action 须为 mapping，得到 {type(raw).__name__}"
        )

    url = raw.get("url", "")
    if not isinstance(url, str) or not url:
        raise FlowParseError(f"step {sid!r} 的 action 缺少 url")

    method = raw.get("method", "POST")
    if not isinstance(method, str) or not method:
        raise FlowParseError(f"step {sid!r} 的 action.method 须为字符串")

    headers = raw.get("headers", {}) or {}
    if not isinstance(headers, dict):
        raise FlowParseError(f"step {sid!r} 的 action.headers 须为 mapping")

    body = raw.get("body", None)

    return ActionConfig(method=method, url=url, headers=headers, body=body)


# ============================================
# 依赖校验
# ============================================
def _check_step_references(spec: FlowSpec) -> None:
    """扫描所有 task / if / returns / action.body 里的 {{steps.XX...}}，
    被引用的 step id 必须在 spec 里存在。

    本阶段不严格校验拓扑顺序（executor 会按依赖跑）。
    """
    valid_ids = set(spec.step_ids())

    bad: list[str] = []

    def _check_template_str(text: str, where: str) -> None:
        for ref in find_references(text or ""):
            # 只关心 steps.* 开头的引用
            if not ref.startswith("steps."):
                continue
            parts = ref.split(".")
            if len(parts) < 2:
                continue
            ref_id = parts[1]
            if ref_id not in valid_ids:
                bad.append(f"{where}: 引用了不存在的 step id {ref_id!r}（占位符 {{{{{ref}}}}}）")

    for s in spec.steps:
        _walk_step_templates(s, _check_template_str)

    for k, v in spec.returns.items():
        if isinstance(v, str):
            _check_template_str(v, f"returns.{k}")

    if bad:
        raise FlowParseError(
            "模板引用校验失败:\n  " + "\n  ".join(bad)
        )


def _walk_step_templates(step: StepNode, visit) -> None:
    """深度遍历 step 树，对每个含 {{}} 的字符串字段调用 visit(text, where)。"""
    where = f"steps[{step.id}]"
    if step.task:
        visit(step.task, f"{where}.task")
    if step.if_cond:
        visit(step.if_cond, f"{where}.if")
    if step.action is not None and isinstance(step.action.body, str):
        visit(step.action.body, f"{where}.action.body")
    if step.parallel:
        for c in step.parallel:
            _walk_step_templates(c, visit)
    if step.pipeline:
        for c in step.pipeline:
            _walk_step_templates(c, visit)
