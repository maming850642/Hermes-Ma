"""
============================================
WakerFlow 数据模型 —— FlowSpec / StepNode / ...
============================================
WakerFlow DSL 的纯数据结构层：解析后的 Flow 在内存里的表达。

设计要点（对齐 src/waker/models.py 风格）：
- 纯 dataclass，不做任何 IO
- 校验逻辑放在 dataclass 方法里（validate_inputs / step_ids / find_step）
- 五种 step 节点类型互斥（worker / parallel / pipeline / ask_user / action），
  由 parser 层强制保证，这里只提供数据容器
- 不引用 worker_node / executor（避免反向依赖）

YAML 字段名 → dataclass 字段名映射：
- `if:` 保留字 → StepNode.if_cond（parser 层做映射）
- `tools` / `permission_mode` 覆盖项可选
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# input 允许的字段类型
_INPUT_TYPES = {"string", "number", "boolean"}


@dataclass
class InputField:
    """Flow 的一个输入参数声明。

    Attributes:
        name: 参数名（点号路径里的 key）
        type: string / number / boolean
        required: 是否必填
        default: 默认值（required=False 时，未提供则用此值）
        enum: 可选枚举，提供值必须在列表内
    """
    name: str
    type: str = "string"
    required: bool = False
    default: Any = None
    enum: list | None = None


@dataclass
class AskUserConfig:
    """ask_user 节点的配置。"""
    question: str
    options: list[dict]          # [{"label":..., "value":...}]
    timeout: int = 86400
    default: Any = None


@dataclass
class ActionConfig:
    """action 节点的配置（HTTP 调用）。"""
    method: str = "POST"
    url: str = ""
    headers: dict = field(default_factory=dict)
    body: Any = None


@dataclass
class StepNode:
    """一个 step。五种节点类型恰好一种非 None（由 parser 强制）。

    节点类型：
    - worker:    worker + task 字段（引用一个 waker 名 + 任务）
    - parallel:  parallel 字段（子 StepNode 列表，并行执行）
    - pipeline:  pipeline 字段（子 StepNode 列表，串行链）
    - ask_user:  ask_user 字段（AskUserConfig）
    - action:    action 字段（ActionConfig，HTTP 调用）

    所有节点都可选附加：
    - if_cond:   条件表达式（YAML `if:`，为真才执行）
    - tools:     覆盖 waker 工具白名单（仅 worker/pipeline 子节点生效）
    - permission_mode: 覆盖 waker permission_mode
    """
    id: str
    worker: str | None = None
    task: str | None = None
    parallel: list["StepNode"] | None = None
    pipeline: list["StepNode"] | None = None
    ask_user: AskUserConfig | None = None
    action: ActionConfig | None = None
    if_cond: str | None = None
    tools: list[str] | None = None
    permission_mode: str | None = None

    def node_kind(self) -> str:
        """返回节点类型名（worker/parallel/pipeline/ask_user/action）。
        parser 已保证恰好一种非 None；这里做兜底判定。"""
        if self.worker is not None:
            return "worker"
        if self.parallel is not None:
            return "parallel"
        if self.pipeline is not None:
            return "pipeline"
        if self.ask_user is not None:
            return "ask_user"
        if self.action is not None:
            return "action"
        return "unknown"


@dataclass
class FlowSpec:
    """一个完整的 WakerFlow 定义。

    Attributes:
        name: Flow 名（须匹配 ^[a-zA-Z0-9_-]{1,64}$，parser 校验）
        description: 人类可读描述
        inputs: 输入参数声明列表
        steps: 顶层 step 列表（顺序执行，parallel/pipeline 内部嵌套）
        returns: 返回值映射（值为模板字符串）
        raw_yaml: 原始 YAML 文本（编辑回显用）

        调度配置（与 WakerConfig 对齐，让 schedule_parse.Schedulable Protocol 复用）：
        enabled: 是否启用调度（False 时 FlowScheduler 跳过）
        schedule_type: 调度类型（none / interval / daily）
        interval_minutes: interval 模式的间隔分钟数
        daily_at: daily 模式的触发时刻 "HH:MM"
        api_enabled: 是否允许 API token 触发
        api_token: API 触发 token（创建时自动生成）
        max_runs: 最大运行次数（0=不限）
        expire_at: ISO 时间，过期后不再调度（空=无截止）
    """
    name: str
    description: str = ""
    inputs: list[InputField] = field(default_factory=list)
    steps: list[StepNode] = field(default_factory=list)
    returns: dict = field(default_factory=dict)
    raw_yaml: str = ""
    # 调度配置（与 WakerConfig 字段名一致，供 schedule_parse 复用）
    enabled: bool = True
    schedule_type: str = "none"
    interval_minutes: int = 60
    daily_at: str = "09:00"
    api_enabled: bool = False
    api_token: str = ""
    max_runs: int = 0
    expire_at: str = ""

    # ---------- input 校验 ----------
    def validate_inputs(self, provided: dict) -> dict:
        """校验并填充 input 默认值。

        规则：
        - required=True 且未提供 → 抛 ValueError
        - 未提供但有 default → 填入 default
        - 提供/填入的值若不在 enum 内 → 抛 ValueError
        - type 不符 → 抛 ValueError（string/number/boolean）

        Returns:
            完整 inputs dict（包含所有声明的字段，未提供的按 default 填充）
        """
        provided = provided or {}
        out: dict[str, Any] = {}
        for f in self.inputs:
            if f.name in provided:
                value = provided[f.name]
            elif f.required:
                raise ValueError(
                    f"缺少必填 input: {f.name!r}"
                )
            else:
                value = f.default

            # type 校验（None 跳过，允许空）
            if value is not None:
                _check_input_type(f.name, f.type, value)

            # enum 校验
            if f.enum is not None and value is not None:
                if value not in f.enum:
                    raise ValueError(
                        f"input {f.name!r} 值 {value!r} 不在 enum {f.enum!r} 内"
                    )

            out[f.name] = value

        # 拒绝未声明的额外 key（防止拼写错误静默丢失）
        declared = {f.name for f in self.inputs}
        extra = set(provided.keys()) - declared
        if extra:
            raise ValueError(
                f"未声明的 input: {sorted(extra)!r}（已知：{sorted(declared)!r}）"
            )
        return out

    # ---------- step 查询 ----------
    def step_ids(self) -> list[str]:
        """收集所有 step 的 id（含 parallel/pipeline 内部的，按出现顺序）。"""
        ids: list[str] = []
        _collect_step_ids(self.steps, ids)
        return ids

    def find_step(self, step_id: str) -> StepNode | None:
        """按 id 递归查找 step（含 parallel/pipeline 内部）。"""
        return _find_step_in(self.steps, step_id)


# ============================================
# 内部辅助
# ============================================
def _collect_step_ids(steps: list[StepNode], out: list[str]) -> None:
    for s in steps:
        out.append(s.id)
        if s.parallel:
            _collect_step_ids(s.parallel, out)
        if s.pipeline:
            _collect_step_ids(s.pipeline, out)


def _find_step_in(steps: list[StepNode], step_id: str) -> StepNode | None:
    for s in steps:
        if s.id == step_id:
            return s
        if s.parallel:
            hit = _find_step_in(s.parallel, step_id)
            if hit is not None:
                return hit
        if s.pipeline:
            hit = _find_step_in(s.pipeline, step_id)
            if hit is not None:
                return hit
    return None


def _check_input_type(name: str, type_: str, value: Any) -> None:
    """按声明的 type 校验 value。"""
    if type_ == "string":
        if not isinstance(value, str):
            raise ValueError(
                f"input {name!r} 须为 string，得到 {type(value).__name__}"
            )
    elif type_ == "number":
        # bool 是 int 子类，要排除
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"input {name!r} 须为 number，得到 {type(value).__name__}"
            )
    elif type_ == "boolean":
        if not isinstance(value, bool):
            raise ValueError(
                f"input {name!r} 须为 boolean，得到 {type(value).__name__}"
            )
    else:
        raise ValueError(
            f"input {name!r} 声明了未知 type {type_!r}（须为 {sorted(_INPUT_TYPES)!r}）"
        )


# ============================================
# FlowState —— 运行时状态（单独存 state.json，不进 flow.yaml）
# ============================================
@dataclass
class FlowState:
    """Flow 的运行时状态（与 WakerConfig 的 state 节字段对齐）。

    与 FlowSpec 分离：FlowSpec 是用户定义（持久化 flow.yaml），FlowState 是
    运行时累积（持久化 state.json）。这样用户改 flow 定义不会覆盖运行历史，
    调度器更新状态也不会污染 flow.yaml。

    字段名与 WakerConfig 的状态字段一致（run_count/last_run_at/last_status/
    next_run_at），让 schedule_parse.Schedulable Protocol 能用
    FlowSpec+FlowState 组合对象（适配器合并两者属性）。

    Attributes:
        run_count: 已运行次数
        last_run_at: 上次运行的 ISO 时间
        last_status: 上次运行状态（"completed"/"failed"/""）
        next_run_at: 下次计划运行的 ISO 时间
    """
    run_count: int = 0
    last_run_at: str = ""
    last_status: str = ""
    next_run_at: str = ""
