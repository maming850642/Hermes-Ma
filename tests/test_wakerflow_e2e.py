"""
M2 WakerFlow 端到端集成测试（mock LLM）。

验证整条链路：parser → executor → fork worker_node（mock LLM）→
  worker/parallel/pipeline/action/askUser 节点 → 事件 jsonl → returns。

用 HERMES_WORKER_NODE_MOCK_LLM=1 让 worker_node 子进程注入 mock LLM，
绕开真实模型依赖。这是 M2 的核心验收测试。
"""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.waker.models import WakerConfig
from src.waker.store import WakerStore
from src.wakerflow.parser import parse_flow
from src.wakerflow.executor import WakerFlowExecutor
from src.wakerflow.store import FlowStore


@pytest.fixture
def ws(tmp_path):
    """隔离 workspace。"""
    with patch("src.waker.store._resolve_workspace", return_value=tmp_path):
        yield tmp_path


@pytest.fixture
def mock_llm_env(monkeypatch):
    """开启 worker_node 的 mock LLM 模式。"""
    monkeypatch.setenv("HERMES_WORKER_NODE_MOCK_LLM", "1")


def _make_waker(store, name, identity="测试员"):
    """建一个最小 waker 供 flow 引用。"""
    store.create(WakerConfig(name=name, task_prompt="x"), identity=identity)


# ════════════════════════════════════════════════════════════════
# 1. 单 worker 节点 flow（fork 真实 worker_node + mock LLM）
# ════════════════════════════════════════════════════════════════
def test_e2e_single_worker_mock_llm(ws, mock_llm_env):
    """parse → executor → fork worker_node(mock LLM) → result。

    最关键的端到端验收：证明 worker_node 子进程能跑完整 HermesAgentV3 +
    通过 stdout NDJSON 回传结果给 executor。
    """
    wstore = WakerStore("e2euser", workspace_root=str(ws))
    _make_waker(wstore, "w1", identity="端到端测试员")

    yaml_text = """
name: single-worker
description: 单 worker 端到端
steps:
  - id: do
    worker: w1
    task: "回复：端到端成功"
returns:
  out: "{{steps.do.result}}"
"""
    flow = parse_flow(yaml_text)
    events = []
    ex = WakerFlowExecutor(
        flow, user_id="e2euser", run_id="e2e-run-1", inputs={},
        workspace_root=str(ws), on_event=events.append,
    )
    result = ex.run()

    assert result["status"] == "completed", f"flow 失败: {result}"
    # worker_node mock LLM 回复应含 "mock-LLM" + 任务片段
    out = result.get("returns", {}).get("out", "")
    assert "mock-LLM" in out, f"returns.out={out}"
    assert "端到端成功" in out, f"returns.out={out}"
    # 应有 node 事件
    assert any(e.get("type") == "node_start" for e in events)
    assert any(e.get("type") == "node_result" for e in events)


# ════════════════════════════════════════════════════════════════
# 2. pipeline 串行链（上游 result 喂下游 task）
# ════════════════════════════════════════════════════════════════
def test_e2e_pipeline_mock_llm(ws, mock_llm_env):
    """pipeline：worker A → worker B，B 的 task 引用 A 的 result。"""
    wstore = WakerStore("pipeuser", workspace_root=str(ws))
    _make_waker(wstore, "rea", identity="研究员")
    _make_waker(wstore, "wri", identity="写手")

    yaml_text = """
name: pipe-test
steps:
  - id: chain
    pipeline:
      - id: research
        worker: rea
        task: "研究主题X"
      - id: write
        worker: wri
        task: "基于 {{steps.research.result}} 写摘要"
returns:
  final: "{{steps.chain.write.result}}"
"""
    flow = parse_flow(yaml_text)
    ex = WakerFlowExecutor(
        flow, user_id="pipeuser", run_id="pipe-run-1", inputs={},
        workspace_root=str(ws),
    )
    result = ex.run()
    assert result["status"] == "completed"
    final = result.get("returns", {}).get("final", "")
    assert "mock-LLM" in final


# ════════════════════════════════════════════════════════════════
# 3. action 节点（不 fork，纯 HTTP mock）
# ════════════════════════════════════════════════════════════════
def test_e2e_action_only(ws, monkeypatch):
    """纯 action flow（不依赖 LLM），mock urllib 验证。"""
    yaml_text = """
name: notify-flow
steps:
  - id: hook
    action:
      method: POST
      url: https://hooks.example.com/x
      body: {ok: true}
returns:
  done: "{{steps.hook.result}}"
"""
    flow = parse_flow(yaml_text)

    # mock urllib.request.urlopen
    import urllib.request
    fake_resp = type("R", (), {
        "status": 200,
        "getcode": lambda self: 200,
        "read": lambda self: b'{"result":"sent"}',
        "__enter__": lambda self: self,
        "__exit__": lambda *a: None,
    })()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: fake_resp)

    ex = WakerFlowExecutor(flow, user_id="auser", run_id="a-run-1",
                           inputs={}, workspace_root=str(ws))
    result = ex.run()
    assert result["status"] == "completed"
    assert "200" in result["returns"]["done"]


# ════════════════════════════════════════════════════════════════
# 4. 完整混合 flow（worker + action，证明多节点协同）
# ════════════════════════════════════════════════════════════════
def test_e2e_mixed_worker_action(ws, mock_llm_env, monkeypatch):
    """worker 节点产出 → action 节点用其 result 发 HTTP。"""
    wstore = WakerStore("mixuser", workspace_root=str(ws))
    _make_waker(wstore, "gen", identity="生成器")

    yaml_text = """
name: mixed
steps:
  - id: produce
    worker: gen
    task: "生成一段内容"
  - id: send
    action:
      method: POST
      url: https://hooks.example.com/send
      body: {content: "{{steps.produce.result}}"}
returns:
  sent: "{{steps.send.result}}"
"""
    flow = parse_flow(yaml_text)

    import urllib.request
    fake_resp = type("R", (), {
        "status": 200, "getcode": lambda self: 200, "read": lambda self: b'ok',
        "__enter__": lambda self: self, "__exit__": lambda *a: None,
    })()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: fake_resp)

    ex = WakerFlowExecutor(flow, user_id="mixuser", run_id="mix-run-1",
                           inputs={}, workspace_root=str(ws))
    result = ex.run()
    assert result["status"] == "completed", f"混合 flow 失败: {result}"
    # action 的 body 应含 worker 产出（mock-LLM）
    assert "200" in result["returns"]["sent"]


# ════════════════════════════════════════════════════════════════
# 5. FlowRunner：ask_user 挂起 → 审批 → 续跑（P2-22）+ on_done（P2-19）
# ════════════════════════════════════════════════════════════════
import json as _json
import threading as _threading
import time as _time
import urllib.request as _urlreq
from types import SimpleNamespace

from src.storage.sqlite_provider import SQLiteProvider
from src.wakerflow.runner import FlowRunner


def _wait_until(cond, timeout=10.0):
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if cond():
            return True
        _time.sleep(0.05)
    return cond()


_ASK_FLOW_YAML = """
name: ask-flow
steps:
  - id: q1
    ask_user:
      question: 发布吗？
      options:
        - {label: 是, value: "yes"}
        - {label: 否, value: "no"}
      timeout: 60
  - id: hook
    action:
      method: POST
      url: https://hooks.example.com/x
      body: {answer: "{{steps.q1.result}}"}
returns:
  done: "{{steps.hook.result}}"
"""


def _make_runner(tmp_path):
    ws = tmp_path / "ws"
    store = FlowStore("u1", workspace_root=str(ws))
    store.save("ask-flow", _ASK_FLOW_YAML)
    # RunRegistry 的 kv 用 tmp 库（绝不碰真实 data/hermes.db）
    prov = SQLiteProvider(db_path=tmp_path / "kv.db")
    runner = FlowRunner(workspace_root=str(ws), max_concurrent=2, storage=prov)
    runner.start()
    return runner, store, prov


def _fake_http(monkeypatch):
    fake_resp = type("R", (), {
        "getcode": lambda self: 200,
        "read": lambda self: b'{"ok": true}',
        "__enter__": lambda self: self,
        "__exit__": lambda *a: None,
    })()
    monkeypatch.setattr(_urlreq, "urlopen", lambda *a, **kw: fake_resp)


def test_runner_ask_user_suspends_then_completes(tmp_path, monkeypatch):
    """P2-22 集成：ask_user 挂起（waiting_approval，池线程已让出）→
    审批文件写 answered → 看护唤醒续跑至 completed。
    P2-19：on_done 在运行真正结束（含续跑）才触发。"""
    _fake_http(monkeypatch)
    runner, store, prov = _make_runner(tmp_path)
    try:
        done = _threading.Event()
        run_id = runner.submit("u1", "ask-flow", {}, on_done=done.set)

        # 挂起：状态 waiting_approval，on_done 未触发
        assert _wait_until(
            lambda: (runner.get_status(run_id) or {}).get("status") == "waiting_approval"
        ), runner.get_status(run_id)
        assert not done.is_set()
        apath = store.approval_path(run_id)
        assert apath.exists()
        assert _json.loads(apath.read_text(encoding="utf-8"))["status"] == "pending"

        # 写审批 → 看护唤醒续跑 → 完成
        data = _json.loads(apath.read_text(encoding="utf-8"))
        data["status"] = "answered"
        data["answer"] = "yes"
        apath.write_text(_json.dumps(data), encoding="utf-8")

        assert done.wait(timeout=10), "on_done 未在审批后续跑完成时触发"
        rec = runner.get_status(run_id)
        assert rec["status"] == "completed"
        assert rec["returns"]["done"].startswith("200")
        assert not apath.exists()  # 审批文件已消费

        # 事件卫生回归：挂起 + 续跑全程 jsonl 恰好一条 flow_end
        # （executor._finish 发；runner._resume 的重复补发已删）
        jpath = store.run_jsonl_path("ask-flow", run_id)
        events = [
            _json.loads(line)
            for line in jpath.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        types = [e.get("type") for e in events]
        assert types.count("flow_end") == 1, types
        assert types[-1] == "flow_end"          # 收尾事件即语义完整的 flow_end
        assert events[-1]["status"] == "completed"
        assert events[-1]["returns"]["done"].startswith("200")
    finally:
        runner.shutdown()
        prov.close()


_PLAIN_ACTION_FLOW_YAML = """
name: plain-action-flow
steps:
  - id: hook
    action:
      method: POST
      url: https://hooks.example.com/x
      body: {ok: true}
returns:
  done: "{{steps.hook.result}}"
"""


def test_runner_jsonl_flow_end_emitted_once(tmp_path, monkeypatch):
    """事件卫生回归：每个 run 的 jsonl 恰好一条 flow_end。

    修复前 runner._run 与 executor._finish 各 emit 一次 → 每个 run 落两条
    flow_end。保留语义完整的那条（executor._finish：带 flow_name /
    node_count / returns——routers/wakerflow 的 last_status / returns 提取
    都读它），删掉 runner 侧只有 status 的重复补发。jsonl 消费方取"最后
    一条 flow_end"，run 状态机走 RunRegistry（不经 jsonl），均不受影响。
    """
    _fake_http(monkeypatch)
    ws = tmp_path / "ws2"
    store = FlowStore("u1", workspace_root=str(ws))
    store.save("plain-action-flow", _PLAIN_ACTION_FLOW_YAML)
    prov = SQLiteProvider(db_path=tmp_path / "kv3.db")
    runner = FlowRunner(workspace_root=str(ws), max_concurrent=2, storage=prov)
    runner.start()
    try:
        done = _threading.Event()
        run_id = runner.submit("u1", "plain-action-flow", {}, on_done=done.set)
        assert done.wait(timeout=10), "flow 未完成"
        assert (runner.get_status(run_id) or {}).get("status") == "completed"

        jpath = store.run_jsonl_path("plain-action-flow", run_id)
        events = [
            _json.loads(line)
            for line in jpath.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        types = [e.get("type") for e in events]
        assert types.count("flow_end") == 1, types
        # 保留的是语义完整版：status + returns 都在
        assert events[-1]["type"] == "flow_end"
        assert events[-1]["status"] == "completed"
        assert events[-1]["returns"]["done"].startswith("200")
    finally:
        runner.shutdown()
        prov.close()


def test_runner_cancel_terminates_suspended_wait(tmp_path, monkeypatch):
    """P2-22：审批文件写 cancelled（runner.cancel）→ 提前终止等待，flow 失败。"""
    _fake_http(monkeypatch)
    runner, store, prov = _make_runner(tmp_path)
    try:
        run_id = runner.submit("u1", "ask-flow", {})
        assert _wait_until(
            lambda: (runner.get_status(run_id) or {}).get("status") == "waiting_approval"
        )

        assert runner.cancel("u1", run_id) is True
        assert _wait_until(
            lambda: (runner.get_status(run_id) or {}).get("status") == "failed",
            timeout=15,
        ), runner.get_status(run_id)
    finally:
        runner.shutdown()
        prov.close()


def test_runner_on_done_not_fired_while_waiting(tmp_path, monkeypatch):
    """P2-19：挂起等待期（run 未结束）on_done 保持未触发——防重入键
    覆盖整个运行期。"""
    _fake_http(monkeypatch)
    runner, store, prov = _make_runner(tmp_path)
    try:
        done = _threading.Event()
        run_id = runner.submit("u1", "ask-flow", {}, on_done=done.set)
        assert _wait_until(
            lambda: (runner.get_status(run_id) or {}).get("status") == "waiting_approval"
        )
        # 等待期 on_done 不触发
        assert not done.wait(timeout=0.5)
    finally:
        runner.shutdown()
        prov.close()


_DOUBLE_ASK_FLOW_YAML = """
name: double-ask-flow
steps:
  - id: q1
    ask_user:
      question: 第一步确认？
      options:
        - {label: 是, value: "yes"}
      timeout: 60
  - id: q2
    ask_user:
      question: 第二步确认？
      options:
        - {label: 去, value: "go"}
      timeout: 60
returns:
  a: "{{steps.q1.result}}"
  b: "{{steps.q2.result}}"
"""


def test_runner_second_ask_after_resume_resuspends(tmp_path, monkeypatch):
    """续跑遇第二个顶层 ask_user：FlowSuspended 不得逃逸（H1 回归）。

    _resume_safe 旧实现只有 except Exception，而 FlowSuspended 继承
    BaseException——两个顺序顶层 ask_user 的 flow 续跑到第二个 ask 时
    异常穿透：审批文件已写但 watch 未创建（永无响应）、run 永久卡
    running、on_done 提前放掉防重入键。修后重新登记挂起（watch 挂到
    新 suspension），第二次审批后正常完成。

    全链路：第一次审批 → 续跑 → 第二次挂起（waiting_approval）→
    第二次审批 → completed；on_done 全程只触发一次。
    """
    ws = tmp_path / "ws"
    store = FlowStore("u1", workspace_root=str(ws))
    store.save("double-ask-flow", _DOUBLE_ASK_FLOW_YAML)
    prov = SQLiteProvider(db_path=tmp_path / "kv2.db")
    runner = FlowRunner(workspace_root=str(ws), max_concurrent=2, storage=prov)
    runner.start()
    try:
        done_count = []

        def _on_done():
            done_count.append(1)

        run_id = runner.submit("u1", "double-ask-flow", {}, on_done=_on_done)
        apath = store.approval_path(run_id)

        # ① 第一次挂起：pending 审批文件属于 q1，on_done 未触发
        assert _wait_until(
            lambda: (runner.get_status(run_id) or {}).get("status") == "waiting_approval"
        ), runner.get_status(run_id)
        assert _json.loads(apath.read_text(encoding="utf-8"))["node_id"] == "q1"
        assert not done_count

        # ② 审批 q1 → 续跑撞上 q2 → 再次挂起（而非异常逃逸卡 running）
        data = _json.loads(apath.read_text(encoding="utf-8"))
        data["status"] = "answered"
        data["answer"] = "yes"
        apath.write_text(_json.dumps(data), encoding="utf-8")

        assert _wait_until(
            lambda: (runner.get_status(run_id) or {}).get("status") == "waiting_approval",
            timeout=15,
        ), runner.get_status(run_id)  # 回归时此处卡 running / 或 error
        # 新审批文件已由 _suspend_on_ask 落盘且指向 q2（watch 已挂上）
        assert _wait_until(
            lambda: apath.exists()
            and _json.loads(apath.read_text(encoding="utf-8")).get("node_id") == "q2"
        ), "第二次挂起的审批文件未创建"
        assert _json.loads(apath.read_text(encoding="utf-8"))["status"] == "pending"
        # run 未结束：on_done 不触发（防重入键不放）
        assert not done_count

        # ③ 审批 q2 → 续跑至 completed
        data = _json.loads(apath.read_text(encoding="utf-8"))
        data["status"] = "answered"
        data["answer"] = "go"
        apath.write_text(_json.dumps(data), encoding="utf-8")

        deadline = _time.monotonic() + 15
        while _time.monotonic() < deadline and not done_count:
            _time.sleep(0.05)
        assert done_count == [1], f"on_done 应恰好触发一次: {done_count}"
        rec = runner.get_status(run_id)
        assert rec["status"] == "completed", rec
        assert rec["returns"]["a"] == "yes"
        assert rec["returns"]["b"] == "go"
        assert not apath.exists()  # 审批文件已消费
    finally:
        runner.shutdown()
        prov.close()


# ════════════════════════════════════════════════════════════════
# 6. 第二轮审查回归：挂起时序 + submit 关停竞态
# ════════════════════════════════════════════════════════════════
def test_suspend_run_sets_status_before_creating_watch(tmp_path, monkeypatch):
    """_suspend_run 必须先置 waiting_approval 再建 _ApprovalWatch。

    watch 线程构造后立即首轮轮询；若先建 watch，极窄窗口内审批落盘会
    唤醒续跑（终态回写先行），随后迟到的 waiting_approval 回写把终态
    覆盖回挂起态——run 永久卡 waiting_approval、防重入键不放。
    用 monkeypatch 记录两类调用的先后顺序做断言。
    """
    calls = []

    class _FakeWatch:
        def __init__(self, path, timeout, on_done, poll_interval=2.0):
            calls.append("watch")

    runner, store, prov = _make_runner(tmp_path)
    try:
        monkeypatch.setattr(
            runner, "_set_status",
            lambda rid, status, **kw: calls.append(("status", status)),
        )
        monkeypatch.setattr("src.wakerflow.runner._ApprovalWatch", _FakeWatch)
        susp = SimpleNamespace(
            approval_path=store.approval_path("rX"), timeout=60, node_id="q1",
        )
        runner._suspend_run("rX", "u1", "ask-flow", susp)
        assert calls == [("status", "waiting_approval"), "watch"], calls
    finally:
        runner.shutdown()
        prov.close()


def test_submit_after_shutdown_rejects_and_registers_nothing(tmp_path):
    """关停后 submit：立即明确 RuntimeError，不登记 run、不登记 on_done。"""
    runner, store, prov = _make_runner(tmp_path)
    try:
        runner.shutdown()
        with pytest.raises(RuntimeError, match="未启动或已关停"):
            runner.submit("u1", "ask-flow", {}, on_done=lambda: None)
        assert runner.list_recent("u1") == []
        assert runner._on_done == {}
    finally:
        prov.close()


def test_submit_race_with_shutdown_lands_error_terminal(tmp_path, monkeypatch):
    """submit 与 shutdown 竞态：登记 on_done 后、executor.submit 前线程池
    被关停（旧实现里 cancel_futures 后的池拒绝新任务抛 RuntimeError，
    on_done 泄漏 + run 永远 pending）→ 修后 run 落明确 error 终态
    （finished_at 有值）、on_done 被清理（防重入键不泄漏不误触发）、
    异常原样抛给调用方。
    """
    runner, store, prov = _make_runner(tmp_path)
    try:
        fired = []
        real_submit = runner._executor.submit

        def _racy_submit(*a, **kw):
            # 竞态另一侧：登记完成后、提交瞬间 shutdown
            runner.shutdown()
            return real_submit(*a, **kw)  # 关停后的池 → RuntimeError

        monkeypatch.setattr(runner._executor, "submit", _racy_submit)
        with pytest.raises(RuntimeError):
            runner.submit("u1", "ask-flow", {}, on_done=lambda: fired.append(1))

        recs = runner.list_recent("u1")
        assert len(recs) == 1, recs
        rec = recs[0]
        assert rec["status"] == "error", rec
        assert "已关停" in rec["error"], rec
        assert rec["finished_at"], rec  # 明确终态（非永久 pending）
        assert not fired                # on_done 被清理，未触发
        assert runner._on_done == {}    # 登记已回收，无泄漏
    finally:
        runner.shutdown()
        prov.close()
