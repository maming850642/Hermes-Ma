"""
manage_waker 工具组测试（chat 内创建 waker / wakerflow）。

覆盖：
- create_waker：成功落盘且 enabled=False、重名拒绝、非法 name/schedule
  返回错误字符串且不留半成品、人格三文件写入
- set_waker_enabled：启用/禁用/不存在/幂等
- list_wakers：空态与有数据态
- create_wakerflow：校验通过落盘、DSL 错误返回错误串、重名保护与
  overwrite、缺失 waker 引用警告
- 工具声明接线：四个新工具能被 loader 加载，side_effects/blocked_in 正确
"""
import pytest

from src.tools import manage_waker
from src.tools.manage_waker import (
    _execute_create_waker,
    _execute_create_wakerflow,
    _execute_list_wakers,
    _execute_set_waker_enabled,
    set_flow_store,
    set_waker_store,
)
from src.waker import WakerStore
from src.wakerflow.store import FlowStore


VALID_FLOW_YAML = """\
name: demo-flow
description: 测试流程
steps:
- id: brief
  worker: daily-patrol
  task: 写一份简短日报
returns:
  brief: '{{steps.brief.result}}'
"""


# ============================================
# 辅助
# ============================================
@pytest.fixture()
def stores(tmp_path):
    """注入 tmp 根的双 store，用完清理 holder（防跨测试泄漏）。"""
    ws = WakerStore("local", workspace_root=str(tmp_path))
    fs = FlowStore("local", workspace_root=str(tmp_path))
    set_waker_store(ws)
    set_flow_store(fs)
    yield ws, fs
    manage_waker._waker_store_holder.clear()
    manage_waker._flow_store_holder.clear()


def _waker(**kw):
    base = dict(
        name="daily-patrol",
        description="巡查",
        task_prompt="读 README，写日报到 daily_report.md",
        schedule_type="daily",
        daily_at="09:00",
    )
    base.update(kw)
    return _execute_create_waker(**base)


# ============================================
# create_waker
# ============================================
def test_create_waker_ok_disabled_by_default(stores):
    ws, _ = stores
    msg = _waker(identity="日报员")
    assert "daily-patrol" in msg
    assert "未启用" in msg

    cfg = ws.get("daily-patrol")
    assert cfg is not None and cfg.enabled is False
    assert cfg.daily_at == "09:00"
    # 人格三文件已写入
    assert "日报员" in (ws.waker_dir("daily-patrol") / "IDENTITY.md").read_text(encoding="utf-8")


def test_create_waker_duplicate_rejected(stores):
    _waker()
    msg = _waker()
    assert "失败" in msg


@pytest.mark.parametrize("bad", [{"name": "中文名"}, {"schedule_type": "weekly"},
                                 {"daily_at": "25:00"}])
def test_create_waker_invalid_no_leftover(stores, bad):
    ws, _ = stores
    kw = {**{"name": "x1", "description": "d", "task_prompt": "t",
             "schedule_type": "daily", "daily_at": "09:00"}, **bad}
    msg = _execute_create_waker(**kw)
    assert "失败" in msg
    # 失败不落半成品（store.create 内部回滚）
    assert ws.get(kw["name"]) is None


def test_create_waker_default_schedule_none(stores):
    # 执行器层 schedule_type 缺省为 none（schema 层才强制必填）
    msg = _execute_create_waker(name="n1", description="d", task_prompt="t")
    assert "未启用" in msg and "仅手动触发" in msg


# ============================================
# permission_mode / 危险组合警告
# ============================================
def test_create_waker_permission_mode_persisted(stores):
    ws, _ = stores
    _execute_create_waker(
        name="p1", description="d", task_prompt="只读巡检并汇报",
        schedule_type="daily", daily_at="09:00", permission_mode="full_access",
    )
    assert ws.get("p1").permission_mode == "full_access"


def test_create_waker_write_task_with_approval_mode_warns(stores):
    ws, _ = stores
    msg = _waker()  # daily + 默认 before_changes + task_prompt 含"写入 daily_report.md"
    assert "警告" in msg and "full_access" in msg
    # 警告不阻断创建
    assert ws.get("daily-patrol") is not None


def test_create_waker_readonly_task_no_warning(stores):
    msg = _execute_create_waker(
        name="ro1", description="d", task_prompt="搜索 AI 资讯并汇报要点",
        schedule_type="interval", interval_minutes=60,
    )
    assert "警告" not in msg


def test_create_waker_manual_schedule_no_warning(stores):
    # schedule_type=none 只手动触发，有人看着，审批模式不构成死锁
    msg = _execute_create_waker(
        name="m1", description="d", task_prompt="写入 daily_report.md",
        schedule_type="none",
    )
    assert "警告" not in msg


# ============================================
# set_waker_enabled
# ============================================
def test_set_enabled_flow(stores):
    ws, _ = stores
    _waker()
    assert "已启用" in _execute_set_waker_enabled(name="daily-patrol", enabled=True)
    assert ws.get("daily-patrol").enabled is True
    assert "已禁用" in _execute_set_waker_enabled(name="daily-patrol", enabled=False)
    assert ws.get("daily-patrol").enabled is False


def test_set_enabled_missing_and_idempotent(stores):
    _waker()
    assert "不存在" in _execute_set_waker_enabled(name="ghost", enabled=True)
    # 幂等：重复启用提示未做改动
    _execute_set_waker_enabled(name="daily-patrol", enabled=True)
    assert "未做改动" in _execute_set_waker_enabled(name="daily-patrol", enabled=True)


# ============================================
# list_wakers
# ============================================
def test_list_empty(stores):
    out = _execute_list_wakers()
    assert "暂无" in out


def test_list_with_data(stores):
    _, fs = stores
    _waker()
    fs.save("demo-flow", VALID_FLOW_YAML)
    out = _execute_list_wakers()
    assert "daily-patrol" in out
    assert "demo-flow" in out
    assert "未启用" in out


# ============================================
# create_wakerflow
# ============================================
def test_create_flow_ok_with_missing_waker_warning(stores):
    _, fs = stores
    msg = _execute_create_wakerflow(yaml_text=VALID_FLOW_YAML)
    assert "demo-flow" in msg
    # 引用的 waker 不存在 → 警告但不拒绝
    assert "daily-patrol" in msg and "不存在" in msg
    assert fs.get("demo-flow") == VALID_FLOW_YAML


def test_create_flow_parse_error_returns_string(stores):
    bad = "name: bad-flow\nsteps:\n- id: s\n  task: 没有节点类型\n"
    msg = _execute_create_wakerflow(yaml_text=bad)
    assert "校验失败" in msg


def test_create_flow_duplicate_needs_overwrite(stores):
    _, fs = stores
    _execute_create_wakerflow(yaml_text=VALID_FLOW_YAML)
    msg = _execute_create_wakerflow(yaml_text=VALID_FLOW_YAML)
    assert "overwrite" in msg
    msg2 = _execute_create_wakerflow(yaml_text=VALID_FLOW_YAML, overwrite=True)
    assert "覆盖" in msg2
    assert fs.get("demo-flow") == VALID_FLOW_YAML


def test_create_flow_no_missing_warning_when_waker_exists(stores):
    _waker()
    msg = _execute_create_wakerflow(yaml_text=VALID_FLOW_YAML)
    assert "不存在" not in msg


# ============================================
# 工具声明接线
# ============================================
def test_tool_specs_registered():
    from src.tools.loader import load_builtin_tools

    tools = load_builtin_tools(force=True)
    for name in ("list_wakers", "create_waker", "set_waker_enabled", "create_wakerflow"):
        assert name in tools, f"工具未注册: {name}"

    # 写类工具：destructive + employee/subagent 禁入；读类工具：无破坏
    for name in ("create_waker", "set_waker_enabled", "create_wakerflow"):
        spec = tools[name]
        assert spec.side_effects.destructive is True
        assert "employee" in spec.blocked_in
        assert "subagent" in spec.blocked_in
    read_spec = tools["list_wakers"]
    assert read_spec.side_effects.destructive is False
    assert read_spec.blocked_in == []


def test_employee_context_hides_write_tools(stores, tmp_path):
    """数字员工运行上下文（caller_context=employee）看不到管理面写工具。"""
    from src.tools.context import ToolContext
    from src.tools.resolve import resolve_tools

    ctx = ToolContext(caller_context="employee")
    names = {s.name for s in resolve_tools(ctx, include_mcp=False)}
    assert "create_waker" not in names
    assert "set_waker_enabled" not in names
    assert "create_wakerflow" not in names
    # 只读盘点不设限
    assert "list_wakers" in names
