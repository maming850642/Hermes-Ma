"""
============================================
manage_waker 工具组 —— 聊天内管理数字员工与流程
============================================
让主 agent 在 chat 里把用户需求直接落成 waker / wakerflow：

    list_wakers        列出现有 waker + flow（查重名 / 编排前确认可引用名）
    create_waker       创建数字员工草稿（固定 enabled=False，二步式安全）
    set_waker_enabled  启用/禁用（用户明确确认后才调）
    create_wakerflow   接收 flow YAML 文本，parse_flow 校验后落盘

设计约束：
- 唯一写入口走 WakerStore / FlowStore / parse_flow——校验、api_token 生成、
  原子写、失败回滚全部复用，绝不让 LLM 经 write_file 直写 data/home
  （绕过 validate_schedule 会复活"静默永不调度"bug 类）。
- 校验失败一律返回可读错误字符串（不抛异常），LLM 下一轮自行修正，
  与 remember 工具同一模式。
- 写类工具在 YAML 声明 blocked_in: [employee]——数字员工不能造数字员工。
- 触发运行（waker invoke / flow trigger）不在本工具组：FlowRunner /
  WakerAsyncRunner 是主进程状态，worker 子进程无回调通道；引导用户去
  页面触发或等调度。

store 注入（测试用）：
模块级 holder + set_waker_store / set_flow_store，测试注入 tmp 根的
store 实例；生产路径 resolve 时现构造（paths.agent_home()）。
"""
from __future__ import annotations

import logging
import re

from src.constants import LOCAL_USER
from src.waker.models import WakerConfig, WakerConfigError
from src.waker.store import WakerStore
from src.wakerflow.models import FlowSpec, StepNode
from src.wakerflow.parser import FlowParseError, parse_flow
from src.wakerflow.store import FlowStore

logger = logging.getLogger("hermes.tools.manage_waker")

# task_prompt 疑似要写文件/落盘的特征（启发式，仅用于无人值守 + 审批模式
# 的危险组合警告，不参与校验）
_WRITEISH_RE = re.compile(
    r"写入|写出|写到|保存到|保存至|另存|\.md\b|\.txt\b|\.csv\b|\.json\b|write_file|日报|周报",
    re.IGNORECASE,
)

# store holder（与 remember._manager_holder 同款：测试注入，生产惰性构造）
_waker_store_holder: dict = {}
_flow_store_holder: dict = {}


def set_waker_store(store: WakerStore) -> None:
    """注入 WakerStore（测试指向 tmp 根；生产不调，走惰性构造）。"""
    _waker_store_holder["store"] = store


def set_flow_store(store: FlowStore) -> None:
    """注入 FlowStore（测试指向 tmp 根）。"""
    _flow_store_holder["store"] = store


def _waker_store() -> WakerStore:
    return _waker_store_holder.get("store") or WakerStore(LOCAL_USER)


def _flow_store() -> FlowStore:
    return _flow_store_holder.get("store") or FlowStore(LOCAL_USER)


# ============================================
# 内部辅助
# ============================================
def _describe_schedule(cfg: WakerConfig) -> str:
    """调度配置的一行人类描述。"""
    if cfg.schedule_type == "interval":
        return f"每 {cfg.interval_minutes} 分钟"
    if cfg.schedule_type == "daily":
        return f"每天 {cfg.daily_at}"
    return "不自动调度（仅手动触发）"


def _collect_workers(steps: list[StepNode], out: list[str]) -> None:
    """递归收集 flow 里引用的全部 waker 名。"""
    for s in steps:
        if s.worker:
            out.append(s.worker)
        if s.parallel:
            _collect_workers(s.parallel, out)
        if s.pipeline:
            _collect_workers(s.pipeline, out)


# ============================================
# list_wakers
# ============================================
def _execute_list_wakers(*, ctx=None) -> str:
    """列出现有数字员工（waker）与流程（wakerflow）。"""
    wakers = _waker_store().list()
    flows = _flow_store().list()

    lines: list[str] = []
    lines.append("## 数字员工（waker）")
    if wakers:
        for cfg in wakers:
            state = "已启用" if cfg.enabled else "未启用"
            desc = f" — {cfg.description}" if cfg.description else ""
            lines.append(
                f"- {cfg.name} [{state}] {_describe_schedule(cfg)}{desc}"
            )
    else:
        lines.append("（暂无。用户想定期自动做某事时，可用 create_waker 创建。）")

    lines.append("")
    lines.append("## 流程（wakerflow）")
    if flows:
        for name, text in flows:
            desc = ""
            try:
                spec = parse_flow(text)
                desc = f" — {spec.description}" if spec.description else ""
            except Exception:
                desc = " — （yaml 解析失败）"
            lines.append(f"- {name}{desc}")
    else:
        lines.append("（暂无。多步骤/多员工编排需求可用 create_wakerflow 创建。）")

    return "\n".join(lines)


# ============================================
# create_waker
# ============================================
def _execute_create_waker(
    *,
    name: str,
    description: str,
    task_prompt: str,
    schedule_type: str = "none",
    interval_minutes: int | None = None,
    daily_at: str | None = None,
    identity: str = "",
    persona: str = "",
    bible: str = "",
    working_dir: str = "",
    permission_mode: str = "before_changes",
    max_runs: int = 0,
    expire_at: str = "",
    ctx=None,
) -> str:
    """创建数字员工草稿（固定 enabled=False，须 set_waker_enabled 二次确认）。"""
    store = _waker_store()

    kwargs: dict = {}
    if interval_minutes is not None:
        kwargs["interval_minutes"] = interval_minutes
    if daily_at:
        kwargs["daily_at"] = daily_at

    try:
        cfg = WakerConfig(
            name=name,
            description=description,
            enabled=False,  # 二步式安全：创建一律未启用
            working_dir=working_dir,
            permission_mode=permission_mode,
            task_prompt=task_prompt,
            schedule_type=schedule_type,
            max_runs=max_runs,
            expire_at=expire_at,
            **kwargs,
        )
        store.create(cfg, identity=identity, persona=persona, bible=bible)
    except (WakerConfigError, ValueError) as e:
        logger.info(f"create_waker 被拒: {name}: {e}")
        return (
            f"创建失败：{e}\n"
            "请修正参数后重试。name 只能用字母/数字/下划线/短横线"
            "（1-64 字符）；schedule_type 只能是 interval/daily/none；"
            "permission_mode 只能是 full_access/before_changes/plan。"
        )

    msg = (
        f"已创建数字员工「{name}」（当前未启用）：{_describe_schedule(cfg)}。\n"
        "向用户确认后，用 set_waker_enabled(name, enabled=true) 启用；"
        "也可让用户到数字员工页面手动开启或调整。"
    )

    # 无人值守 + 审批模式 + 疑似写任务：每次运行必然卡审批而失败的组合，
    # 创建即警告（勿等到运行 error 才暴露）。删除后以 full_access 重建。
    if (
        cfg.schedule_type != "none"
        and cfg.permission_mode != "full_access"
        and _WRITEISH_RE.search(task_prompt or "")
    ):
        msg += (
            f"\n⚠️ 警告：该 waker 的任务是自动运行的，permission_mode 却是"
            f"「{cfg.permission_mode}」——无人值守时写文件等变更操作会卡在人工审批上，"
            "导致每次运行以 error 结束。请与用户确认后删除此 waker，"
            "并以 permission_mode=full_access 重建（或在数字员工页面把权限改为完全访问）。"
        )
    return msg


# ============================================
# set_waker_enabled
# ============================================
def _execute_set_waker_enabled(*, name: str, enabled: bool, ctx=None) -> str:
    """启用/禁用数字员工（启用即进入调度，须用户明确确认）。"""
    store = _waker_store()
    cfg = store.get(name)
    if cfg is None:
        known = "、".join(w.name for w in store.list()) or "（无）"
        return f"失败：数字员工「{name}」不存在。现有的：{known}"

    before = cfg.enabled
    try:
        store.set_enabled(name, enabled)
    except (WakerConfigError, ValueError) as e:
        return f"失败：{e}"

    if enabled and not before:
        return (
            f"已启用「{name}」：{_describe_schedule(cfg)}。"
            "调度器下一轮扫描会安排首次运行。"
        )
    if not enabled and before:
        return f"已禁用「{name}」，不再自动调度。"
    return f"「{name}」本来就处于{'启用' if enabled else '禁用'}状态，未做改动。"


# ============================================
# create_wakerflow
# ============================================
def _execute_create_wakerflow(
    *, yaml_text: str, overwrite: bool = False, ctx=None
) -> str:
    """保存 wakerflow 定义（parse_flow 全量校验后落盘）。

    只认 yaml_text 里的顶层 name 作为目录名——不存在 body.name 与
    yaml 内层 name 两个来源（web 路由的历史坑，工具入口直接掐掉）。
    """
    store = _flow_store()

    try:
        spec: FlowSpec = parse_flow(yaml_text)
    except FlowParseError as e:
        return f"flow YAML 校验失败：{e}\n请修正后重试（可加载 create-waker 技能查 DSL 语法）。"

    existing = store.get(spec.name)
    if existing is not None and not overwrite:
        return (
            f"已存在同名流程「{spec.name}」。向用户确认覆盖后，"
            "再以 overwrite=true 重新调用；否则换一个名字。"
        )

    try:
        store.save(spec.name, yaml_text)
    except ValueError as e:
        return f"保存失败：{e}"

    # 引用完整性提示（允许先写 flow 后补 waker，故只警告不拒绝）
    workers: list[str] = []
    _collect_workers(spec.steps, workers)
    known = {w.name for w in _waker_store().list()}
    missing = sorted({w for w in workers if w not in known})

    sched = ""
    if spec.schedule_type != "none":
        sched = f"调度：{'每 ' + str(spec.interval_minutes) + ' 分钟' if spec.schedule_type == 'interval' else '每天 ' + spec.daily_at}"
    else:
        sched = "无自动调度（页面手动触发）"

    parts = [
        f"已保存流程「{spec.name}」（{len(spec.steps)} 个顶层步骤，{sched}）。",
        f"步骤引用的数字员工：{('、'.join(sorted(set(workers)))) if workers else '（无）'}。",
    ]
    if missing:
        parts.append(
            f"注意：以下被引用的数字员工还不存在：{'、'.join(missing)}。"
            "运行到对应步骤会失败，可用 create_waker 先补齐。"
        )
    if existing is not None:
        parts.append("（已覆盖原有同名流程。）")
    return "\n".join(parts)
