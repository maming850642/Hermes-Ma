"""
============================================
WakerFlow 画布转换层 —— 积木块 JSON ↔ FlowSpec
============================================
纯函数模块，连接"垂直积木块拖拽编辑器"与 FlowSpec YAML 数据模型。

## 设计核心
积木块的 DOM 顺序 = steps 数组顺序 = 执行顺序，1:1 映射。
不需要拓扑排序，不需要坐标系，转换层几乎是 DOM→list 直读。

## 两个方向
- blocks_to_flow(blocks, ...) → FlowSpec：积木块列表 → FlowSpec（前端保存时）
- flow_to_blocks(spec) → list[dict]：FlowSpec → 积木块列表（前端加载时）

## 积木块 JSON 结构（block dict）
顶层字段（所有块共有）：
    {
      "type": "worker"|"parallel"|"pipeline"|"ask_user"|"action",
      "id": "step-id",          # 必填，对应 StepNode.id
      "if": "条件表达式",        # 可选，对应 StepNode.if_cond
      ...类型特有字段
    }

类型特有字段：
- worker:    waker(str), task(str), tools(list[str]|null), permission_mode(str|null)
- parallel:  children(list[block])           # 子块，递归
- pipeline:  children(list[block])           # 子块，递归
- ask_user:  question(str), options(list[{label,value}]),
             timeout(int), default(any|null)
- action:    method(str), url(str), headers(dict), body(str|dict|null)

## 与 parser 的分工
- canvas：积木块 dict（来自前端 JSON）↔ FlowSpec 对象。不碰 YAML 文本。
- parser：YAML 文本 ↔ FlowSpec 对象。
两者都产出 FlowSpec，互不依赖。前端存盘时 canvas → FlowSpec → parser.dump 为 YAML。
"""
from __future__ import annotations

from typing import Any

from src.wakerflow.models import (
    ActionConfig,
    AskUserConfig,
    FlowSpec,
    InputField,
    StepNode,
)

# 允许的块类型（与 StepNode.node_kind() 对齐）
_BLOCK_TYPES = {"worker", "parallel", "pipeline", "ask_user", "action"}


class CanvasError(ValueError):
    """积木块 ↔ FlowSpec 转换失败。"""


# ════════════════════════════════════════════════════════════════
# blocks_to_flow：积木块列表 → FlowSpec
# ════════════════════════════════════════════════════════════════
def blocks_to_flow(
    blocks: list[dict],
    *,
    name: str = "",
    description: str = "",
    inputs: list[dict] | None = None,
    returns: dict | None = None,
    schedule: dict | None = None,
) -> FlowSpec:
    """积木块列表 → FlowSpec。

    Args:
        blocks: 积木块 dict 列表（DOM 顺序 = steps 顺序）
        name: flow 名（前端单独传，不进 blocks）
        description: 描述
        inputs: input 字段列表（每项 {name,type,required,default,enum}）
        returns: 返回值映射
        schedule: 调度配置 dict（enabled/schedule_type/interval_minutes/daily_at/
            api_enabled/api_token/max_runs/expire_at 任意子集），缺失用 FlowSpec 默认值

    Returns:
        FlowSpec（已校验，可直接存盘 / 执行）

    Raises:
        CanvasError: 块结构非法（type 缺失 / id 重复 / worker 无 waker 等）
    """
    if not isinstance(blocks, list):
        raise CanvasError(f"blocks 须为列表，得到 {type(blocks).__name__}")

    seen_ids: set[str] = set()
    steps: list[StepNode] = []
    for i, block in enumerate(blocks):
        step = _block_to_step(block, seen_ids, path=f"blocks[{i}]")
        steps.append(step)

    # input 字段转换
    input_fields = [_dict_to_input_field(d, path=f"inputs[{j}]")
                    for j, d in enumerate(inputs or [])]

    # 调度配置（过滤 None，让 FlowSpec 默认值生效）
    sched_kwargs: dict = {}
    for k, v in (schedule or {}).items():
        if v is not None:
            sched_kwargs[k] = v

    # P2-21：复用 parser._parse_schedule 的语义校验。canvas 绕过了 YAML
    # 解析路径，若不校验，脏 schedule（schedule_type:"weekly"/负 interval）
    # 会入库成功——之后每次 tick parse_flow 都失败，flow 永久哑火且无提示。
    # 校验失败在保存入口（POST/PUT /items）就被拒为 4xx。
    if sched_kwargs:
        from src.wakerflow.parser import FlowParseError, _parse_schedule
        try:
            _parse_schedule(dict(sched_kwargs))
        except FlowParseError as e:
            raise CanvasError(f"schedule 配置非法: {e}") from e

    spec = FlowSpec(
        name=name,
        description=description,
        inputs=input_fields,
        steps=steps,
        returns=returns or {},
        raw_yaml="",  # canvas 产物无原始 YAML；存盘时由 parser 重新序列化
        **sched_kwargs,
    )
    return spec


def _block_to_step(block: Any, seen_ids: set[str], path: str) -> StepNode:
    """单个积木块 → StepNode（递归处理 parallel/pipeline children）。

    Args:
        block: 积木块 dict
        seen_ids: 已收集的 id 集合（跨层级全局唯一，与 parser 一致）
        path: 错误定位用，如 "blocks[2].children[0]"
    """
    if not isinstance(block, dict):
        raise CanvasError(f"{path} 须为 mapping，得到 {type(block).__name__}")

    btype = block.get("type")
    if not btype:
        raise CanvasError(f"{path} 缺少 type 字段")
    if btype not in _BLOCK_TYPES:
        raise CanvasError(
            f"{path} 非法 type {btype!r}（须为 {sorted(_BLOCK_TYPES)}）"
        )

    # id（必填，全局唯一）
    sid = block.get("id")
    if not isinstance(sid, str) or not sid:
        raise CanvasError(f"{path} 缺少 id 字段（或非字符串）")
    if sid in seen_ids:
        raise CanvasError(f"{path} id 重复: {sid!r}")
    seen_ids.add(sid)

    if_cond = block.get("if") or None
    if if_cond is not None and not isinstance(if_cond, str):
        raise CanvasError(f"{path} 的 if 须为字符串")

    # 按类型分发
    if btype == "worker":
        return _worker_block_to_step(block, sid, if_cond, path)
    if btype in ("parallel", "pipeline"):
        return _container_block_to_step(block, btype, sid, if_cond, seen_ids, path)
    if btype == "ask_user":
        return _ask_user_block_to_step(block, sid, if_cond, path)
    if btype == "action":
        return _action_block_to_step(block, sid, if_cond, path)

    # 不可达（_BLOCK_TYPES 已校验）
    raise CanvasError(f"{path} 未知 type {btype!r}")


def _worker_block_to_step(
    block: dict, sid: str, if_cond: str | None, path: str
) -> StepNode:
    """worker 块 → StepNode。"""
    waker = block.get("waker")
    if not isinstance(waker, str) or not waker:
        raise CanvasError(f"{path} worker 块缺少 waker（或为空）")

    task = block.get("task")
    if not isinstance(task, str) or not task:
        raise CanvasError(f"{path} worker 块缺少 task（或为空）")

    tools = block.get("tools")
    if tools is not None:
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise CanvasError(f"{path} tools 须为字符串列表或 null")

    permission_mode = block.get("permission_mode")
    if permission_mode is not None and not isinstance(permission_mode, str):
        raise CanvasError(f"{path} permission_mode 须为字符串或 null")

    return StepNode(
        id=sid,
        worker=waker,
        task=task,
        if_cond=if_cond,
        tools=tools,
        permission_mode=permission_mode,
    )


def _container_block_to_step(
    block: dict,
    btype: str,  # "parallel" | "pipeline"
    sid: str,
    if_cond: str | None,
    seen_ids: set[str],
    path: str,
) -> StepNode:
    """parallel/pipeline 块 → StepNode（递归 children）。

    children 至少 1 个（与 parser._parse_step 一致）。
    """
    children_raw = block.get("children")
    if not isinstance(children_raw, list) or len(children_raw) == 0:
        raise CanvasError(
            f"{path} {btype} 块缺少 children（或为空列表）"
        )

    sub_steps = [
        _block_to_step(c, seen_ids, path=f"{path}.children[{i}]")
        for i, c in enumerate(children_raw)
    ]

    kwargs: dict = {"id": sid, "if_cond": if_cond}
    if btype == "parallel":
        kwargs["parallel"] = sub_steps
    else:
        kwargs["pipeline"] = sub_steps
    return StepNode(**kwargs)


def _ask_user_block_to_step(
    block: dict, sid: str, if_cond: str | None, path: str
) -> StepNode:
    """ask_user 块 → StepNode。"""
    question = block.get("question")
    if not isinstance(question, str) or not question:
        raise CanvasError(f"{path} ask_user 块缺少 question")

    options_raw = block.get("options")
    if not isinstance(options_raw, list) or len(options_raw) == 0:
        raise CanvasError(f"{path} ask_user 块 options 须为非空列表")

    options: list[dict] = []
    for i, o in enumerate(options_raw):
        if not isinstance(o, dict) or "label" not in o or "value" not in o:
            raise CanvasError(
                f"{path} ask_user.options[{i}] 须含 label 和 value"
            )
        options.append({"label": o["label"], "value": o["value"]})

    timeout = block.get("timeout", 86400)
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise CanvasError(f"{path} ask_user.timeout 须为整数")

    default = block.get("default")
    if default is not None and not any(o["value"] == default for o in options):
        raise CanvasError(
            f"{path} ask_user.default {default!r} 不在 options 里"
        )

    return StepNode(
        id=sid,
        if_cond=if_cond,
        ask_user=AskUserConfig(
            question=question,
            options=options,
            timeout=timeout,
            default=default,
        ),
    )


def _action_block_to_step(
    block: dict, sid: str, if_cond: str | None, path: str
) -> StepNode:
    """action 块 → StepNode。"""
    url = block.get("url")
    if not isinstance(url, str) or not url:
        raise CanvasError(f"{path} action 块缺少 url")

    method = block.get("method", "POST")
    if not isinstance(method, str) or not method:
        raise CanvasError(f"{path} action.method 须为字符串")

    headers = block.get("headers", {}) or {}
    if not isinstance(headers, dict):
        raise CanvasError(f"{path} action.headers 须为 mapping")

    body = block.get("body", None)

    return StepNode(
        id=sid,
        if_cond=if_cond,
        action=ActionConfig(method=method, url=url, headers=headers, body=body),
    )


def _dict_to_input_field(d: Any, path: str) -> InputField:
    """input dict → InputField（与 parser._parse_inputs 对齐）。"""
    if not isinstance(d, dict):
        raise CanvasError(f"{path} 须为 mapping，得到 {type(d).__name__}")

    name = d.get("name")
    if not isinstance(name, str) or not name:
        raise CanvasError(f"{path} input 缺少 name")

    type_ = d.get("type", "string")
    if type_ not in ("string", "number", "boolean"):
        raise CanvasError(
            f"{path} input {name!r} 非法 type {type_!r}（须为 string/number/boolean）"
        )

    return InputField(
        name=name,
        type=type_,
        required=bool(d.get("required", False)),
        default=d.get("default", None),
        enum=d.get("enum", None),
    )


# ════════════════════════════════════════════════════════════════
# flow_to_blocks：FlowSpec → 积木块列表
# ════════════════════════════════════════════════════════════════
def flow_to_blocks(spec: FlowSpec) -> list[dict]:
    """FlowSpec → 积木块列表（前端渲染用）。

    与 blocks_to_flow 互逆。保留所有字段，让前端能完整还原编辑。
    children 递归转换。
    """
    if not isinstance(spec, FlowSpec):
        raise CanvasError(f"spec 须为 FlowSpec，得到 {type(spec).__name__}")

    return [_step_to_block(s) for s in (spec.steps or [])]


def _step_to_block(step: StepNode) -> dict:
    """单个 StepNode → 积木块 dict（递归 parallel/pipeline）。"""
    kind = step.node_kind()
    block: dict = {"type": kind, "id": step.id}

    if step.if_cond:
        block["if"] = step.if_cond

    if kind == "worker":
        block["waker"] = step.worker or ""
        block["task"] = step.task or ""
        block["tools"] = step.tools  # 可能 None，前端按 null 处理
        block["permission_mode"] = step.permission_mode

    elif kind in ("parallel", "pipeline"):
        sub_steps = step.parallel if kind == "parallel" else step.pipeline
        block["children"] = [_step_to_block(s) for s in (sub_steps or [])]

    elif kind == "ask_user":
        cfg = step.ask_user
        block["question"] = cfg.question if cfg else ""
        block["options"] = (
            [{"label": o["label"], "value": o["value"]} for o in cfg.options]
            if cfg else []
        )
        block["timeout"] = cfg.timeout if cfg else 86400
        block["default"] = cfg.default if cfg else None

    elif kind == "action":
        cfg = step.action
        block["method"] = cfg.method if cfg else "POST"
        block["url"] = cfg.url if cfg else ""
        block["headers"] = dict(cfg.headers) if cfg else {}
        block["body"] = cfg.body if cfg else None

    return block


# ════════════════════════════════════════════════════════════════
# FlowSpec → YAML 文本（存盘用，不依赖 parser 的反方向）
# ════════════════════════════════════════════════════════════════
def flow_to_yaml(spec: FlowSpec) -> str:
    """FlowSpec → YAML 文本（存盘格式，与 parser.parse_flow 互逆）。

    手写序列化（不引 pyyaml 的 dumper，保持输出风格与手写 YAML 一致，
    便于人类阅读和 git diff）。输出的 YAML 能被 parse_flow 重新解析。

    用于：积木块编辑 → blocks_to_flow → flow_to_yaml → FlowStore.save。
    """
    import yaml  # 局部 import，canvas 模块本身不强依赖 pyyaml

    data = _spec_to_yaml_dict(spec)
    return yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=10000,  # 不换行长字符串
    )


def _spec_to_yaml_dict(spec: FlowSpec) -> dict:
    """FlowSpec → 可被 yaml.safe_dump 的 dict（结构对齐 parser 期望）。"""
    data: dict = {"name": spec.name}
    if spec.description:
        data["description"] = spec.description

    # inputs
    if spec.inputs:
        inputs_dict = {}
        for f in spec.inputs:
            item: dict = {"type": f.type}
            if f.required:
                item["required"] = True
            if f.default is not None:
                item["default"] = f.default
            if f.enum is not None:
                item["enum"] = list(f.enum)
            inputs_dict[f.name] = item
        data["inputs"] = inputs_dict

    # steps
    if spec.steps:
        data["steps"] = [_step_to_yaml_dict(s) for s in spec.steps]

    # returns
    if spec.returns:
        data["returns"] = dict(spec.returns)

    # 调度配置（仅输出非默认值，保持 YAML 简洁）
    if not spec.enabled:
        data["enabled"] = False
    if spec.schedule_type != "none":
        data["schedule_type"] = spec.schedule_type
        if spec.schedule_type == "interval":
            data["interval_minutes"] = spec.interval_minutes
        elif spec.schedule_type == "daily":
            data["daily_at"] = spec.daily_at
    if spec.api_enabled:
        data["api_enabled"] = True
        if spec.api_token:
            data["api_token"] = spec.api_token
    if spec.max_runs > 0:
        data["max_runs"] = spec.max_runs
    if spec.expire_at:
        data["expire_at"] = spec.expire_at

    return data


def _step_to_yaml_dict(step: StepNode) -> dict:
    """StepNode → YAML dict（结构对齐 parser._parse_step 期望）。"""
    kind = step.node_kind()
    out: dict = {"id": step.id}

    if step.if_cond:
        out["if"] = step.if_cond

    if kind == "worker":
        out["worker"] = step.worker
        out["task"] = step.task
        if step.tools is not None:
            out["tools"] = list(step.tools)
        if step.permission_mode is not None:
            out["permission_mode"] = step.permission_mode

    elif kind in ("parallel", "pipeline"):
        sub_steps = step.parallel if kind == "parallel" else step.pipeline
        out[kind] = [_step_to_yaml_dict(s) for s in (sub_steps or [])]

    elif kind == "ask_user":
        cfg = step.ask_user
        au: dict = {"question": cfg.question, "options": list(cfg.options)}
        if cfg.timeout != 86400:
            au["timeout"] = cfg.timeout
        if cfg.default is not None:
            au["default"] = cfg.default
        out["ask_user"] = au

    elif kind == "action":
        cfg = step.action
        act: dict = {"url": cfg.url, "method": cfg.method}
        if cfg.headers:
            act["headers"] = dict(cfg.headers)
        if cfg.body is not None:
            act["body"] = cfg.body
        out["action"] = act

    return out
