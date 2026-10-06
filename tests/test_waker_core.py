"""
waker 核心子系统测试。

覆盖：
- schedule_parse: interval/daily/expire/max_runs 边界
- WakerStore: create/get/update/set_enabled/save_state/delete
- persona 组装
- yaml 往返
"""
from datetime import datetime, timedelta

import pytest

from src.waker import (
    WakerConfig,
    WakerStore,
    load_persona_prompt,
    compute_next_run,
    is_due,
    iter_all_wakers,
)
from src.waker.models import WakerConfigError, validate_name
from src.waker.schedule_parse import validate_schedule


# ============================================
# 辅助
# ============================================
def _store(tmp_path, uid="u1"):
    return WakerStore(uid, workspace_root=str(tmp_path))


def _cfg(name="w1", **kw):
    base = dict(name=name, enabled=True, schedule_type="interval", interval_minutes=60)
    base.update(kw)
    return WakerConfig(**base)


# ============================================
# validate_name
# ============================================
def test_validate_name_ok():
    assert validate_name("w1") == "w1"
    assert validate_name("my-waker_2") == "my-waker_2"


@pytest.mark.parametrize("bad", ["", "has space", "x" * 65, "中文", "a.b"])
def test_validate_name_bad(bad):
    with pytest.raises(WakerConfigError):
        validate_name(bad)


# ============================================
# schedule_parse: interval
# ============================================
def test_interval_first_run():
    now = datetime(2026, 7, 27, 9, 0)
    cfg = _cfg(interval_minutes=30)
    nxt = compute_next_run(cfg, now)
    assert nxt == now + timedelta(minutes=30)


def test_interval_after_run():
    now = datetime(2026, 7, 27, 9, 0)
    cfg = _cfg(interval_minutes=60, last_run_at="2026-07-27T08:00:00")
    # last+60min = 09:00，已到 → 立即跑（返回 now）
    nxt = compute_next_run(cfg, now)
    assert nxt == now


def test_interval_not_yet():
    now = datetime(2026, 7, 27, 9, 0)
    cfg = _cfg(interval_minutes=60, last_run_at="2026-07-27T08:30:00")
    nxt = compute_next_run(cfg, now)
    assert nxt == datetime(2026, 7, 27, 9, 30)


# ============================================
# schedule_parse: daily
# ============================================
def test_daily_today_not_yet():
    now = datetime(2026, 7, 27, 8, 0)
    cfg = _cfg(schedule_type="daily", daily_at="09:00")
    nxt = compute_next_run(cfg, now)
    assert nxt == datetime(2026, 7, 27, 9, 0)


def test_daily_today_already_passed_not_run():
    now = datetime(2026, 7, 27, 10, 0)
    cfg = _cfg(schedule_type="daily", daily_at="09:00")
    # 今天 09:00 已过但还没跑过 → 立即跑
    nxt = compute_next_run(cfg, now)
    assert nxt == now


def test_daily_already_run_today():
    now = datetime(2026, 7, 27, 10, 0)
    cfg = _cfg(
        schedule_type="daily", daily_at="09:00",
        last_run_at="2026-07-27T09:30:00",
    )
    nxt = compute_next_run(cfg, now)
    assert nxt == datetime(2026, 7, 28, 9, 0)


# ============================================
# schedule_parse: none / disabled
# ============================================
def test_none_schedule():
    now = datetime(2026, 7, 27, 9, 0)
    cfg = _cfg(schedule_type="none")
    assert compute_next_run(cfg, now) is None


def test_disabled():
    now = datetime(2026, 7, 27, 9, 0)
    cfg = _cfg(enabled=False)
    assert compute_next_run(cfg, now) is None


# ============================================
# schedule_parse: expire / max_runs
# ============================================
def test_expired():
    now = datetime(2026, 7, 27, 9, 0)
    cfg = _cfg(expire_at="2026-07-26T00:00:00")
    assert compute_next_run(cfg, now) is None


def test_max_runs_reached():
    now = datetime(2026, 7, 27, 9, 0)
    cfg = _cfg(max_runs=3, run_count=3)
    assert compute_next_run(cfg, now) is None


def test_max_runs_not_reached():
    now = datetime(2026, 7, 27, 9, 0)
    cfg = _cfg(max_runs=3, run_count=2)
    assert compute_next_run(cfg, now) is not None


# ============================================
# is_due
# ============================================
def test_is_due_true():
    now = datetime(2026, 7, 27, 10, 0)
    cfg = _cfg(next_run_at="2026-07-27T09:00:00")
    assert is_due(cfg, now) is True


def test_is_due_future():
    now = datetime(2026, 7, 27, 9, 0)
    cfg = _cfg(next_run_at="2026-07-27T10:00:00")
    assert is_due(cfg, now) is False


def test_is_due_empty():
    now = datetime(2026, 7, 27, 9, 0)
    cfg = _cfg()
    cfg.next_run_at = ""
    assert is_due(cfg, now) is False


# ============================================
# validate_schedule
# ============================================
def test_validate_schedule_ok():
    """合法配置（interval / daily / none + 合法 expire）不应抛。"""
    # interval 默认就是合法值（interval_minutes=60）
    validate_schedule(_cfg())  # 不抛即通过
    # daily 合法
    validate_schedule(_cfg(schedule_type="daily", daily_at="09:30"))
    # none 合法
    validate_schedule(_cfg(schedule_type="none"))
    # 带合法 expire
    validate_schedule(_cfg(expire_at="2026-12-31T23:59:59"))


def test_validate_schedule_bad_interval():
    cfg = _cfg(interval_minutes=0)
    with pytest.raises(WakerConfigError):
        validate_schedule(cfg)


def test_validate_schedule_bad_daily():
    cfg = _cfg(schedule_type="daily", daily_at="9am")
    with pytest.raises(WakerConfigError):
        validate_schedule(cfg)


def test_validate_schedule_bad_type():
    cfg = _cfg(schedule_type="weekly")
    with pytest.raises(WakerConfigError):
        validate_schedule(cfg)


def test_validate_schedule_bad_expire():
    cfg = _cfg(expire_at="not-a-date")
    with pytest.raises(WakerConfigError):
        validate_schedule(cfg)


def test_validate_schedule_rejects_aware_expire_at():
    """P1-7：带时区的 expire_at 在校验入口拒绝（调度计算全程 naive 本地时间，
    aware 值会让 now >= exp 抛 TypeError、停摆其后所有任务）。"""
    cfg = _cfg(expire_at="2026-01-01T00:00:00+08:00")
    with pytest.raises(WakerConfigError):
        validate_schedule(cfg)


# ============================================
# yaml 往返
# ============================================
def test_yaml_roundtrip():
    cfg = _cfg(
        description="测试 waker",
        tools=["a", "b"],
        permission_mode="plan",
        task_prompt="做某事",
        schedule_type="daily",
        daily_at="10:30",
        max_runs=5,
        expire_at="2026-12-31T23:59:59",
    )
    cfg.run_count = 2
    cfg.last_run_at = "2026-07-27T09:00:00"
    cfg.last_status = "ok"
    cfg.next_run_at = "2026-07-28T10:30:00"

    d = cfg.to_yaml_dict()
    assert set(d.keys()) == {"config", "state"}
    # config 节不含状态字段
    assert "run_count" not in d["config"]
    # state 节不含配置字段
    assert "task_prompt" not in d["state"]
    assert d["state"]["run_count"] == 2

    restored = WakerConfig.from_yaml_dict(d)
    assert restored.name == cfg.name
    assert restored.tools == cfg.tools
    assert restored.run_count == 2
    assert restored.last_status == "ok"
    assert restored.next_run_at == cfg.next_run_at


# ============================================
# WakerStore: create/get/update/delete
# ============================================
def test_create_and_get(tmp_path):
    store = _store(tmp_path)
    cfg = _cfg(description="hello")
    store.create(cfg, identity="核心职责", persona="风格", bible="准则")

    got = store.get("w1")
    assert got is not None
    assert got.name == "w1"
    assert got.description == "hello"
    # api_token 自动生成
    assert got.api_token != ""

    # 三个 md 存在
    wdir = store.waker_dir("w1")
    assert (wdir / "IDENTITY.md").exists()
    assert (wdir / "PERSONA.md").exists()
    assert (wdir / "BIBLE.md").exists()
    # runs 目录已建
    assert store.run_dir("w1").is_dir()
    jp = store.run_jsonl_path("w1", "20260908T135328-abc")
    assert jp.parent == store.run_dir("w1")
    assert jp.name == "20260908T135328-abc.jsonl"


def test_run_jsonl_path_rejects_traversal(tmp_path):
    store = _store(tmp_path)
    store.create(_cfg())
    with pytest.raises(ValueError):
        store.run_jsonl_path("w1", r"..\..\evil")
    with pytest.raises(ValueError):
        store.run_jsonl_path("w1", "../evil")


def test_create_duplicate(tmp_path):
    store = _store(tmp_path)
    store.create(_cfg())
    with pytest.raises(ValueError):
        store.create(_cfg())


def test_list(tmp_path):
    store = _store(tmp_path)
    store.create(_cfg("a"))
    store.create(_cfg("b", enabled=False))
    names = [c.name for c in store.list()]
    assert sorted(names) == ["a", "b"]


def test_update(tmp_path):
    store = _store(tmp_path)
    store.create(_cfg())
    cfg = store.get("w1")
    cfg.description = "changed"
    cfg.task_prompt = "新任务"
    store.update(cfg)
    got = store.get("w1")
    assert got.description == "changed"
    assert got.task_prompt == "新任务"


def test_update_preserves_state_section(tmp_path):
    """P3-11：update 写回保留盘上 state 节——调度器经 save_state 推进的
    run_count/next_run_at 不被"读改写窗口里的旧 cfg"整体覆盖回退。"""
    store = _store(tmp_path)
    store.create(_cfg(description="orig"))
    stale = store.get("w1")  # 读出旧 cfg（run_count=0、next_run_at 空）

    # 读改写窗口内，调度器推进了 state
    sched_view = store.get("w1")
    sched_view.run_count = 3
    sched_view.next_run_at = "2026-07-27T10:00:00"
    sched_view.last_status = "ok"
    store.save_state(sched_view)

    # 用户用旧 cfg 只改描述并 update
    stale.description = "changed"
    store.update(stale)

    got = store.get("w1")
    assert got.description == "changed"
    assert got.run_count == 3                     # 不回退为 0
    assert got.next_run_at == "2026-07-27T10:00:00"  # 不回退为空


def test_update_rejects_invalid_schedule(tmp_path):
    """P2-25/P1-7：store.update 写入口校验调度字段（含带时区 expire_at）。"""
    store = _store(tmp_path)
    store.create(_cfg())
    cfg = store.get("w1")
    cfg.schedule_type = "weekly"
    with pytest.raises(WakerConfigError):
        store.update(cfg)
    cfg2 = store.get("w1")
    cfg2.expire_at = "2026-01-01T00:00:00+08:00"
    with pytest.raises(WakerConfigError):
        store.update(cfg2)
    # 盘上未被污染
    assert store.get("w1").schedule_type == "interval"


def test_set_enabled(tmp_path):
    store = _store(tmp_path)
    store.create(_cfg(enabled=True))
    store.set_enabled("w1", False)
    assert store.get("w1").enabled is False
    store.set_enabled("w1", True)
    assert store.get("w1").enabled is True


def test_save_state_preserves_config(tmp_path):
    store = _store(tmp_path)
    store.create(_cfg(description="orig"))
    cfg = store.get("w1")
    cfg.run_count = 5
    cfg.last_status = "ok"
    cfg.last_run_at = "2026-07-27T09:00:00"
    cfg.next_run_at = "2026-07-27T10:00:00"
    store.save_state(cfg)

    got = store.get("w1")
    assert got.run_count == 5
    assert got.last_status == "ok"
    # config 节保留
    assert got.description == "orig"


def test_delete(tmp_path):
    store = _store(tmp_path)
    store.create(_cfg())
    assert store.delete("w1") is True
    assert store.get("w1") is None
    assert not store.waker_dir("w1").exists()
    # 二次删返回 False
    assert store.delete("w1") is False


def test_update_persona(tmp_path):
    store = _store(tmp_path)
    store.create(_cfg(), identity="旧职责")
    store.update_persona("w1", identity="新职责", persona="新风格")
    text = load_persona_prompt(store, "w1")
    assert "新职责" in text
    assert "新风格" in text
    assert "数字员工人格" in text


# ============================================
# persona 组装
# ============================================
def test_persona_all_empty(tmp_path):
    store = _store(tmp_path)
    store.create(_cfg(), identity="", persona="", bible="")
    assert load_persona_prompt(store, "w1") == ""


def test_persona_partial(tmp_path):
    store = _store(tmp_path)
    store.create(_cfg(), identity="职责A", persona="", bible="准则C")
    text = load_persona_prompt(store, "w1")
    assert "职责A" in text
    assert "准则C" in text
    # PERSONA 小节不应出现（内容为空被跳过）
    assert "工作风格（PERSONA）" not in text


def test_persona_from_dir(tmp_path):
    store = _store(tmp_path)
    store.create(_cfg(), identity="职责", persona="风格", bible="准则")
    # 直接传目录路径
    wdir = store.waker_dir("w1")
    text = load_persona_prompt(wdir)
    assert "职责" in text and "风格" in text and "准则" in text


# ============================================
# new_run_id
# ============================================
def test_new_run_id(tmp_path):
    store = _store(tmp_path)
    rid = store.new_run_id()
    assert isinstance(rid, str) and len(rid) > 0
    rid2 = store.new_run_id()
    assert rid != rid2  # 随机后缀保证唯一


# ============================================
# iter_all_wakers（T2b-②：单用户拍平，扫 <root>/wakers/*/）
# ============================================
def test_iter_all_wakers(tmp_path):
    from src.constants import LOCAL_USER
    # 两个 waker（user_id 参数已不影响路径）
    WakerStore("u1", str(tmp_path)).create(_cfg("w1"))
    WakerStore("u2", str(tmp_path)).create(_cfg("w2", enabled=False))
    pairs = list(iter_all_wakers(str(tmp_path)))
    names = sorted(p[1].name for p in pairs)
    assert names == ["w1", "w2"]
    # 元组契约保留：user 恒为 LOCAL_USER
    assert all(p[0] == LOCAL_USER for p in pairs)


def test_waker_store_layout_flattened(tmp_path):
    """布局：<root>/wakers/<name>/（无 users/<uid> 段）。"""
    store = _store(tmp_path)
    store.create(_cfg("w1"))
    assert store.waker_dir("w1") == tmp_path / "wakers" / "w1"
    assert not (tmp_path / "users").exists()


# ============================================
# T8a：waker 工具白名单作用域化（ToolContext.allowed_tools / ToolSpec.blocked_in）
# ============================================
from src.tools.context import ToolContext
from src.tools.schema import ToolSpec


def _mk_spec(name: str, blocked_in: list | None = None) -> ToolSpec:
    """构造最小 ToolSpec（executor 用不上，None 即可）。"""
    return ToolSpec(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        executor=None,
        blocked_in=blocked_in or [],
    )


@pytest.fixture
def resolve_env(monkeypatch):
    """隔离 resolve_tools：假内置工具集 + 跳过 workspace 模式过滤 + 不拉 MCP。

    返回 {name: ToolSpec}，测试可断言过滤结果恰为白名单 ∩ 全量。
    """
    specs = {
        "alpha": _mk_spec("alpha"),
        "beta": _mk_spec("beta"),
        "gamma": _mk_spec("gamma"),
        "emp_only": _mk_spec("emp_only", blocked_in=["employee"]),
    }
    monkeypatch.setattr("src.tools.resolve.load_builtin_tools", lambda: dict(specs))
    monkeypatch.setattr(
        "src.tools.resolve._apply_workspace_filter", lambda visible, settings=None: visible
    )
    return specs


def _resolve(ctx: ToolContext, env) -> list[str]:
    from src.tools.resolve import resolve_tools
    return sorted(s.name for s in resolve_tools(ctx, include_mcp=False))


def test_allowed_tools_filters_to_intersection(resolve_env):
    """白名单 = 白名单 ∩ 全量（不存在的名字被忽略）。"""
    ctx = ToolContext(allowed_tools={"alpha", "gamma", "nonexistent"})
    assert _resolve(ctx, resolve_env) == ["alpha", "gamma"]


def test_allowed_tools_none_keeps_all(resolve_env):
    """allowed_tools=None（waker cfg.tools 为空时的传递值）不过滤。"""
    ctx = ToolContext(allowed_tools=None)
    assert _resolve(ctx, resolve_env) == ["alpha", "beta", "emp_only", "gamma"]


def test_allowed_tools_empty_set_filters_everything(resolve_env):
    """显式空集 → 全部过滤掉（空集是有效白名单，区别于 None）。"""
    ctx = ToolContext(allowed_tools=set())
    assert _resolve(ctx, resolve_env) == []


def test_blocked_in_hides_for_matching_caller_context(resolve_env):
    """blocked_in=["employee"] + caller_context="employee" → 该工具消失。"""
    ctx = ToolContext(caller_context="employee")
    assert _resolve(ctx, resolve_env) == ["alpha", "beta", "gamma"]
    # main 上下文不受影响
    ctx_main = ToolContext(caller_context="main")
    assert _resolve(ctx_main, resolve_env) == ["alpha", "beta", "emp_only", "gamma"]


def test_blocked_in_and_whitelist_compose(resolve_env):
    """两层尾部过滤叠加：白名单先命中 emp_only，再被 blocked_in 摘掉。"""
    ctx = ToolContext(
        caller_context="employee",
        allowed_tools={"emp_only", "alpha"},
    )
    assert _resolve(ctx, resolve_env) == ["alpha"]


def test_loader_reads_blocked_in_from_yaml(tmp_path):
    """loader 可选读 yaml 的 blocked_in（默认 []）。"""
    from src.tools.loader import load_tool_from_yaml

    yml = tmp_path / "tool_x.yaml"
    yml.write_text(
        "name: tool_x\n"
        "description: 测试\n"
        "parameters: {type: object, properties: {}}\n"
        "runtime: {type: python, module: os, function: getcwd}\n"
        "blocked_in: [employee, subagent]\n",
        encoding="utf-8",
    )
    spec = load_tool_from_yaml(yml)
    assert spec.blocked_in == ["employee", "subagent"]

    # 不写 blocked_in → 默认 []
    yml2 = tmp_path / "tool_y.yaml"
    yml2.write_text(
        "name: tool_y\n"
        "description: 测试\n"
        "parameters: {type: object, properties: {}}\n"
        "runtime: {type: python, module: os, function: getcwd}\n",
        encoding="utf-8",
    )
    assert load_tool_from_yaml(yml2).blocked_in == []


def test_run_waker_passes_tool_whitelist_via_context(tmp_path, monkeypatch):
    """run_waker：cfg.tools 非空 → stream_invoke 收到 allowed_tools=白名单集。"""
    from src.waker.runner import run_waker

    monkeypatch.setattr("src.waker.store._resolve_workspace", lambda ws="": tmp_path)

    cfg = _cfg("w1", task_prompt="做某事", tools=["alpha", "beta"])
    store = _store(tmp_path)
    store.create(cfg)

    captured = {}

    class _CapAgent:
        def get_permission_mode(self):
            return "before_changes"

        def set_permission_mode(self, mode):
            pass

        def stream_invoke(self, user_id, task_input, **kw):
            captured["allowed_tools"] = kw.get("allowed_tools")
            yield {"type": "complete", "content": "ok"}

    class _CapState:
        user_id = "u1"
        agent = _CapAgent()
        permission_mode = "before_changes"

    result = run_waker(_CapState(), name="w1", run_id="r-wl")
    assert result["status"] == "ok"
    assert captured["allowed_tools"] == {"alpha", "beta"}


def test_run_waker_empty_tools_means_no_whitelist(tmp_path, monkeypatch):
    """run_waker：cfg.tools 空 → allowed_tools=None（不限，取代空 monkey-patch）。"""
    from src.waker.runner import run_waker

    monkeypatch.setattr("src.waker.store._resolve_workspace", lambda ws="": tmp_path)

    cfg = _cfg("w1", task_prompt="做某事", tools=[])
    store = _store(tmp_path)
    store.create(cfg)

    captured = {}

    class _CapAgent:
        def get_permission_mode(self):
            return "before_changes"

        def set_permission_mode(self, mode):
            pass

        def stream_invoke(self, user_id, task_input, **kw):
            captured["allowed_tools"] = kw.get("allowed_tools")
            yield {"type": "complete", "content": "ok"}

    class _CapState:
        user_id = "u1"
        agent = _CapAgent()
        permission_mode = "before_changes"

    result = run_waker(_CapState(), name="w1", run_id="r-wl2")
    assert result["status"] == "ok"
    assert captured["allowed_tools"] is None


def test_worker_node_run_stream_passes_whitelist(capsys):
    """wakerflow worker_node._run_stream：白名单经 allowed_tools 透传（无 patch）。"""
    from src.wakerflow.worker_node import _run_stream

    captured = {}

    class _CapAgent:
        def stream_invoke(self, user_id, task, **kw):
            captured["allowed_tools"] = kw.get("allowed_tools")
            yield {"type": "complete", "content": "done"}

    text = _run_stream(_CapAgent(), "u1", "任务", None, "w1", "run-1", {"alpha"})
    assert text == "done"
    assert captured["allowed_tools"] == {"alpha"}

    # 无白名单 → None
    text2 = _run_stream(_CapAgent(), "u1", "任务", None, "w1", "run-2", None)
    assert text2 == "done"
    assert captured["allowed_tools"] is None


# ============================================
# WakerAsyncRunner：任务文本经 --task-stdin 传入（Windows 32k argv 上限）
# ============================================
def test_async_runner_passes_task_via_stdin(tmp_path, monkeypatch):
    """回归：_run 不再把任务文本放 argv（--task），改用 --task-stdin +
    stdin PIPE 写入——api_prompt 拼接后可达数万字符，Windows 命令行
    32k 上限会让 spawn 直接 WinError 206（与 executor._run_worker 对齐）。"""
    import json as _json
    from src.waker.async_runner import WakerAsyncRunner

    monkeypatch.setattr("src.waker.store._resolve_workspace",
                        lambda ws="": tmp_path)

    captured = {"cmds": [], "kwargs": [], "stdin": [], "stdin_closed": []}

    class _FakeStdin:
        def write(self, s):
            captured["stdin"].append(s)

        def close(self):
            captured["stdin_closed"].append(True)

    class _FakeProc:
        def __init__(self):
            self.stdin = _FakeStdin()
            result = _json.dumps({
                "type": "result", "status": "ok",
                "content": "完成", "run_id": "r-x", "waker": "w1",
            }, ensure_ascii=False)
            self.stdout = iter([result + "\n"])

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def _fake_popen(cmd, **kwargs):
        captured["cmds"].append(list(cmd))
        captured["kwargs"].append(kwargs)
        return _FakeProc()

    monkeypatch.setattr("src.waker.async_runner.subprocess.Popen", _fake_popen)

    runner = WakerAsyncRunner(workspace_root=str(tmp_path))
    big_task = "做某事 " + "x" * 40_000  # 远超 Windows 32k argv 上限
    status, text = runner._run("u1", "w1", "r-x", big_task)

    assert status == "ok"
    assert text == "完成"
    cmd = captured["cmds"][0]
    assert "--task-stdin" in cmd
    assert "--task" not in cmd
    assert big_task not in cmd                      # 任务文本不再进 argv
    assert captured["stdin"] == [big_task]          # 全文经 stdin 传入
    assert captured["stdin_closed"] == [True]       # 写完即关闭
    assert captured["kwargs"][0]["stdin"] is not None  # stdin=PIPE


# ============================================
# web invoke 端点：手动运行即时落库（UX 回归）
# ============================================
def test_invoke_via_async_runner_persists_run_state_immediately(tmp_path):
    """手动"立即运行"提交成功后 run_count/last_run_at 必须即时落库。

    回归：async_runner 路径跑完只写 jsonl / latest_result，不回写
    waker.yaml 的 state——卡片（/api/waker/items → store.get）一直显示
    旧值（运行次数：0 / 上次运行：—）。修后 invoke 端点在 submit 成功后
    立即 save_state 推进 run_count / last_run_at。
    """
    import asyncio
    from types import SimpleNamespace

    from web_fastapi.models import WakerInvokeBody
    from web_fastapi.routers.waker import invoke_item

    store = _store(tmp_path)
    store.create(_cfg())

    submitted = []

    class _FakeRunner:
        def submit(self, user_id, name, run_id, prompt=None):
            submitted.append((user_id, name, run_id, prompt))
            return run_id

    app_state = SimpleNamespace(
        waker_async_runner=_FakeRunner(),
        # 路由的 _store 用 scheduler._workspace_root 解析存储根
        waker_scheduler=SimpleNamespace(_workspace_root=str(tmp_path)),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=app_state))

    resp = asyncio.run(
        invoke_item("w1", WakerInvokeBody(prompt="做一件事"),
                    request, user_id="u1")
    )
    assert resp["ok"] is True
    assert submitted and submitted[0][1] == "w1"    # 运行确已提交

    got = store.get("w1")
    assert got.run_count == 1, f"run_count={got.run_count}"
    assert got.last_run_at != "", "last_run_at 未落库"


def test_invoke_submit_failure_does_not_count_run(tmp_path):
    """对照组：提交失败（task_prompt 为空 → 400）不得推进 run 计数。"""
    import asyncio
    from types import SimpleNamespace

    import pytest as _pytest
    from fastapi import HTTPException

    from web_fastapi.models import WakerInvokeBody
    from web_fastapi.routers.waker import invoke_item

    store = _store(tmp_path)
    store.create(_cfg(task_prompt=""))              # 无 task_prompt → submit 拒绝

    class _FakeRunner:
        def submit(self, user_id, name, run_id, prompt=None):
            raise ValueError("waker w1 task_prompt 为空，无任务可执行")

    app_state = SimpleNamespace(
        waker_async_runner=_FakeRunner(),
        waker_scheduler=SimpleNamespace(_workspace_root=str(tmp_path)),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=app_state))

    with _pytest.raises(HTTPException) as ei:
        asyncio.run(
            invoke_item("w1", WakerInvokeBody(prompt=None),
                        request, user_id="u1")
        )
    assert ei.value.status_code == 400
    got = store.get("w1")
    assert got.run_count == 0, f"run_count={got.run_count}"
    assert got.last_run_at == ""
