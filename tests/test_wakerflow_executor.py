"""
M2.2b WakerFlowExecutor + FlowStore 测试。

验证：
1. FlowStore CRUD（list/get/save/delete + iter_all_flows + 路径辅助）
2. executor 跑纯 action 节点 flow（monkeypatch urllib，不依赖网络/LLM/子进程）
3. executor 跑 pipeline（mock _run_worker 返回固定文本，验证上游 result 喂下游）
4. executor 跑 parallel（mock _run_worker，验证并发 + sub_results 汇总）
5. if_cond 跳过
6. askUser 占位返回 skipped
7. validate_inputs 失败 → flow failed
8. returns 模板渲染

注意：parser 还没实现 FlowSpec/StepNode，这里用本地 mock dataclass 模拟
其接口（id/worker/task/parallel/pipeline/ask_user/action/if_cond/...），
保证 executor 不依赖 parser 也能跑（联调时换真 dataclass 应无感）。
"""
from __future__ import annotations

import io
import json
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.wakerflow.store import FlowStore, iter_all_flows
from src.wakerflow.executor import (
    FlowSuspended,
    NodeResult,
    WakerFlowExecutor,
    _ApprovalWatch,
    render,
    WAKER_NODE_TIMEOUT,
)


# ════════════════════════════════════════════════════════════════
# Mock FlowSpec / StepNode（模拟 parser 将提供的接口）
# ════════════════════════════════════════════════════════════════
@dataclass
class MockAskUser:
    question: str = "选哪个？"
    options: list = field(default_factory=list)
    timeout: int = 86400
    default: object = None


@dataclass
class MockAction:
    method: str = "POST"
    url: str = ""
    headers: dict = field(default_factory=dict)
    body: object = None


@dataclass
class MockStep:
    """模拟 parser 的 StepNode 接口。"""
    id: str
    worker: str | None = None
    task: str | None = None
    parallel: list | None = None
    pipeline: list | None = None
    ask_user: MockAskUser | None = None
    action: MockAction | None = None
    if_cond: str | None = None
    tools: list | None = None
    permission_mode: str | None = None


@dataclass
class MockInputField:
    """模拟 parser 的 InputField（最小：name + required + default）。"""
    name: str
    required: bool = False
    default: object = None


@dataclass
class MockFlowSpec:
    """模拟 parser 的 FlowSpec 接口。"""
    name: str = "test-flow"
    description: str = ""
    inputs: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    returns: dict = field(default_factory=dict)
    raw_yaml: str = ""

    def validate_inputs(self, provided: dict) -> dict:
        """简易实现：required 必须给，缺则用 default，未知键保留。"""
        out = {}
        for f in self.inputs:
            if f.name in provided:
                out[f.name] = provided[f.name]
            elif f.required:
                raise ValueError(f"缺少必填输入: {f.name}")
            else:
                out[f.name] = f.default
        # 透传 provided 中额外的键
        for k, v in (provided or {}).items():
            out.setdefault(k, v)
        return out


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    """隔离 workspace 到 tmp_path。"""
    with patch("src.wakerflow.store._resolve_workspace", return_value=tmp_path):
        # 同时 patch waker.store._resolve_workspace（FlowStore 复用它）
        # 已经在 store 模块 import 进来，patch 它即可
        yield tmp_path


# ════════════════════════════════════════════════════════════════
# FlowStore 测试
# ════════════════════════════════════════════════════════════════
class TestFlowStore:
    def test_save_and_get(self, isolated_workspace):
        store = FlowStore("u1", workspace_root=str(isolated_workspace))
        yaml_text = "name: my-flow\ndescription: 测试\n"
        store.save("my-flow", yaml_text)

        # get 返回原文
        got = store.get("my-flow")
        assert got == yaml_text

        # 目录结构正确
        assert store.flow_dir("my-flow").exists()
        assert store._yaml_path("my-flow").exists()
        # save 自动建 runs/ 目录
        assert store.run_dir("my-flow").exists()

    def test_get_nonexistent(self, isolated_workspace):
        store = FlowStore("u1", workspace_root=str(isolated_workspace))
        assert store.get("nope") is None

    def test_list(self, isolated_workspace):
        store = FlowStore("u1", workspace_root=str(isolated_workspace))
        store.save("b-flow", "name: b")
        store.save("a-flow", "name: a")

        items = store.list()
        # 按 name ASCII 排序
        names = [n for n, _ in items]
        assert names == ["a-flow", "b-flow"]
        # 文本完整
        d = dict(items)
        assert d["a-flow"] == "name: a"

    def test_list_empty(self, isolated_workspace):
        store = FlowStore("u1", workspace_root=str(isolated_workspace))
        assert store.list() == []

    def test_delete(self, isolated_workspace):
        store = FlowStore("u1", workspace_root=str(isolated_workspace))
        store.save("to-delete", "name: x")
        assert store.delete("to-delete") is True
        assert store.get("to-delete") is None
        assert not store.flow_dir("to-delete").exists()
        # 再次删除返回 False
        assert store.delete("to-delete") is False

    def test_overwrite(self, isolated_workspace):
        store = FlowStore("u1", workspace_root=str(isolated_workspace))
        store.save("flow", "v1")
        store.save("flow", "v2-updated")
        assert store.get("flow") == "v2-updated"

    def test_invalid_name_rejected(self, isolated_workspace):
        store = FlowStore("u1", workspace_root=str(isolated_workspace))
        with pytest.raises(ValueError):
            store.save("_approvals", "x")  # 下划线前缀保留
        with pytest.raises(ValueError):
            store.save("../escape", "x")  # 目录穿越
        with pytest.raises(ValueError):
            store.save("", "x")  # 空

    def test_approvals_dir_path(self, isolated_workspace):
        store = FlowStore("u1", workspace_root=str(isolated_workspace))
        ap = store.approvals_dir()
        assert ap.name == "_approvals"
        assert ap.parent == store.flows_root
        # approval_path 拼接
        assert store.approval_path("run-1").name == "run-1.json"

    def test_approval_path_rejects_traversal_run_id(self, isolated_workspace):
        """P3-4：run_id 过 _safe_component（对齐 run_jsonl_path；%5C 反斜杠
        在 Windows 可穿越出 approvals 目录）。"""
        store = FlowStore("u1", workspace_root=str(isolated_workspace))
        with pytest.raises(ValueError):
            store.approval_path("..\\..\\evil")
        with pytest.raises(ValueError):
            store.approval_path("a/b")
        with pytest.raises(ValueError):
            store.approval_path("")

    def test_new_run_id_unique(self, isolated_workspace):
        store = FlowStore("u1", workspace_root=str(isolated_workspace))
        ids = {store.new_run_id() for _ in range(20)}
        assert len(ids) == 20  # 基本唯一性
        # 格式：YYYYmmddTHHMMSS-xxxxxx
        assert "-" in next(iter(ids))

    def test_run_jsonl_path(self, isolated_workspace):
        store = FlowStore("u1", workspace_root=str(isolated_workspace))
        p = store.run_jsonl_path("f", "run-1")
        assert p.name == "run-1.jsonl"
        assert p.parent.name == "runs"

    def test_list_skips_approvals_dir(self, isolated_workspace):
        """list() 不应把 _approvals 当 flow 列出。"""
        store = FlowStore("u1", workspace_root=str(isolated_workspace))
        store.save("real-flow", "name: real")
        # 手动建 _approvals 目录 + 假 json
        store.approvals_dir().mkdir(parents=True, exist_ok=True)
        store.approval_path("run-1").write_text('{"pending": true}')
        items = store.list()
        names = [n for n, _ in items]
        assert names == ["real-flow"]
        assert "_approvals" not in names


class TestIterAllFlows:
    def test_iter_all_flows(self, isolated_workspace):
        # 单用户拍平：user_id 参数不影响路径，全部落在 <root>/wakerflows/
        from src.constants import LOCAL_USER
        FlowStore("u1", workspace_root=str(isolated_workspace)).save("f1", "name: f1")
        FlowStore("u2", workspace_root=str(isolated_workspace)).save("f2", "name: f2")
        FlowStore("u2", workspace_root=str(isolated_workspace)).save("f3", "name: f3")

        result = list(iter_all_flows(str(isolated_workspace)))
        # (uid, name, yaml_text)：uid 恒为 LOCAL_USER（保元组契约）
        uids_names = sorted((uid, name) for uid, name, _ in result)
        assert uids_names == [
            (LOCAL_USER, "f1"), (LOCAL_USER, "f2"), (LOCAL_USER, "f3"),
        ]
        # yaml 文本对得上
        d = {(uid, name): text for uid, name, text in result}
        assert d[(LOCAL_USER, "f1")] == "name: f1"

    def test_iter_all_flows_empty(self, isolated_workspace):
        assert list(iter_all_flows(str(isolated_workspace))) == []


# ════════════════════════════════════════════════════════════════
# Executor: 纯 action 节点（monkeypatch urllib）
# ════════════════════════════════════════════════════════════════
class TestActionNode:
    def test_action_post_success(self, isolated_workspace):
        """action POST 2xx → status=ok，result 含 status_code + body。"""
        flow = MockFlowSpec(
            steps=[MockStep(
                id="act1",
                action=MockAction(
                    method="POST",
                    url="https://example.com/api",
                    headers={"X-Token": "secret-{{ inputs.tok }}"},
                    body={"msg": "hello {{ inputs.who }}"},
                ),
            )],
            inputs=[MockInputField("tok", default="defaulttok"),
                    MockInputField("who", default="world")],
        )

        events = []
        # mock urllib.request.urlopen
        fake_resp = MagicMock()
        fake_resp.getcode.return_value = 200
        fake_resp.read.return_value = b'{"ok": true}'
        fake_resp.__enter__ = lambda self: fake_resp
        fake_resp.__exit__ = lambda *a: False

        with patch("src.wakerflow.executor.urllib.request.urlopen",
                   return_value=fake_resp) as mock_urlopen:
            ex = WakerFlowExecutor(
                flow, "u1", "run-1", {"tok": "T", "who": "Z"},
                workspace_root=str(isolated_workspace),
                on_event=events.append,
            )
            result = ex.run()

        assert result["status"] == "completed"
        # 校验 urlopen 收到的 Request：url 已渲染、body 已渲染、header 已渲染
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://example.com/api"
        assert req.method == "POST"
        assert req.data == json.dumps({"msg": "hello Z"}).encode("utf-8")
        assert req.headers.get("X-token") == "secret-T"  # http 头不区分大小写
        # 事件流：flow_start / node_start / worker 无 / node_end / node_result / flow_end
        types = [e["type"] for e in events]
        assert "flow_start" in types
        assert "node_start" in types
        assert "node_end" in types
        assert "flow_end" in types
        # result 含 status_code
        nr = next(e for e in events if e["type"] == "node_result")
        assert nr["status"] == "ok"
        assert "200" in nr["result"]["result"]
        assert '{"ok": true}' in nr["result"]["result"]

    def test_action_http_error(self, isolated_workspace):
        """action 返回 4xx/5xx → status=error。"""
        flow = MockFlowSpec(
            steps=[MockStep(
                id="act1",
                action=MockAction(method="GET", url="https://x/y"),
            )],
        )
        err = urllib.error.HTTPError(
            url="https://x/y", code=404, msg="Not Found",
            hdrs=None, fp=io.BytesIO(b'{"err":"missing"}'),
        )
        with patch("src.wakerflow.executor.urllib.request.urlopen",
                   side_effect=err):
            ex = WakerFlowExecutor(
                flow, "u1", "run-1", {},
                workspace_root=str(isolated_workspace),
            )
            result = ex.run()
        assert result["status"] == "failed"  # action error → flow failed

    def test_action_url_empty(self, isolated_workspace):
        """url 渲染后为空 → error。"""
        flow = MockFlowSpec(
            steps=[MockStep(id="act1", action=MockAction(method="POST", url=""))],
        )
        ex = WakerFlowExecutor(
            flow, "u1", "run-1", {},
            workspace_root=str(isolated_workspace),
        )
        result = ex.run()
        assert result["status"] == "failed"

    def test_action_network_error(self, isolated_workspace):
        """urlopen 抛 URLError → error。"""
        flow = MockFlowSpec(
            steps=[MockStep(id="act1",
                action=MockAction(method="GET", url="https://x"))],
        )
        with patch("src.wakerflow.executor.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("conn refused")):
            ex = WakerFlowExecutor(
                flow, "u1", "run-1", {},
                workspace_root=str(isolated_workspace),
            )
            result = ex.run()
        assert result["status"] == "failed"


# ════════════════════════════════════════════════════════════════
# Executor: pipeline（上游 result 喂下游）
# ════════════════════════════════════════════════════════════════
class TestPipeline:
    def test_pipeline_chains_results(self, isolated_workspace):
        """pipeline：上游 result 自动喂给下游 task 模板。"""
        flow = MockFlowSpec(
            steps=[MockStep(
                id="pipe1",
                pipeline=[
                    MockStep(id="s1", worker="w1", task="问 A"),
                    MockStep(id="s2", worker="w2", task="基于 {{ steps.s1.result }} 回答"),
                ],
            )],
        )

        captured = {}

        def fake_run_worker(self, step, context):
            # 记录下游收到的 task（验证上游 result 已注入）
            rendered_task = render(step.task or "", self._render_context(context))
            captured[step.id] = rendered_task
            return NodeResult(
                node_id=step.id, status="ok",
                result=f"ans-{step.id}", ts="t",
            )

        with patch.object(WakerFlowExecutor, "_run_worker", fake_run_worker):
            ex = WakerFlowExecutor(
                flow, "u1", "run-1", {},
                workspace_root=str(isolated_workspace),
            )
            result = ex.run()

        assert result["status"] == "completed"
        # s1 的 task 原样
        assert captured["s1"] == "问 A"
        # s2 的 task 已注入 s1 的 result
        assert captured["s2"] == "基于 ans-s1 回答"
        # pipeline 节点 sub_results 含两个子结果
        # 通过 events 取 node_result
        # （run 返回的 dict 没有 step 结果，需要从 context 验证——这里用回调）

    def test_pipeline_error_stops_chain(self, isolated_workspace):
        """pipeline 中游 error → 后续不跑，pipeline error。"""
        flow = MockFlowSpec(
            steps=[MockStep(
                id="pipe1",
                pipeline=[
                    MockStep(id="s1", worker="w1"),
                    MockStep(id="s2", worker="w2"),  # 不应执行
                    MockStep(id="s3", worker="w3"),  # 不应执行
                ],
            )],
        )
        call_log = []

        def fake_run_worker(self, step, context):
            call_log.append(step.id)
            if step.id == "s1":
                return NodeResult(node_id=step.id, status="error",
                                  error="boom", ts="t")
            return NodeResult(node_id=step.id, status="ok", result="ok", ts="t")

        events = []
        with patch.object(WakerFlowExecutor, "_run_worker", fake_run_worker):
            ex = WakerFlowExecutor(
                flow, "u1", "run-1", {},
                workspace_root=str(isolated_workspace),
                on_event=events.append,
            )
            result = ex.run()

        assert result["status"] == "failed"
        assert call_log == ["s1"]  # s2/s3 未执行
        # pipeline 节点本身 node_result 应是 error
        pipe_ev = next(e for e in events
                       if e["type"] == "node_result" and e["node_id"] == "pipe1")
        assert pipe_ev["status"] == "error"


# ════════════════════════════════════════════════════════════════
# Executor: parallel（并发 + sub_results 汇总）
# ════════════════════════════════════════════════════════════════
class TestParallel:
    def test_parallel_aggregates_subresults(self, isolated_workspace):
        """parallel：各子 step 并发，sub_results 汇总，全 ok → parallel ok。"""
        flow = MockFlowSpec(
            steps=[MockStep(
                id="par1",
                parallel=[
                    MockStep(id="p1", worker="w1", task="t1"),
                    MockStep(id="p2", worker="w2", task="t2"),
                    MockStep(id="p3", worker="w3", task="t3"),
                ],
            )],
        )

        def fake_run_worker(self, step, context):
            return NodeResult(
                node_id=step.id, status="ok",
                result=f"r-{step.id}", ts="t",
            )

        events = []
        with patch.object(WakerFlowExecutor, "_run_worker", fake_run_worker):
            ex = WakerFlowExecutor(
                flow, "u1", "run-1", {},
                workspace_root=str(isolated_workspace),
                on_event=events.append,
            )
            result = ex.run()

        assert result["status"] == "completed"
        # parallel 节点 node_result：sub_results 含 3 个 ok
        par_ev = next(e for e in events
                      if e["type"] == "node_result" and e["node_id"] == "par1")
        assert par_ev["status"] == "ok"
        sub_ids = sorted(par_ev["result"]["sub_results"].keys())
        assert sub_ids == ["p1", "p2", "p3"]
        for sid in sub_ids:
            sub = par_ev["result"]["sub_results"][sid]
            assert sub["status"] == "ok"
            assert sub["result"] == f"r-{sid}"

    def test_parallel_partial_error(self, isolated_workspace):
        """parallel：一个子 error → parallel 整体 error（其余仍跑）。"""
        flow = MockFlowSpec(
            steps=[MockStep(
                id="par1",
                parallel=[
                    MockStep(id="p1", worker="w1"),
                    MockStep(id="p2", worker="w2"),
                    MockStep(id="p3", worker="w3"),
                ],
            )],
        )
        call_log = []

        def fake_run_worker(self, step, context):
            call_log.append(step.id)
            if step.id == "p2":
                return NodeResult(node_id=step.id, status="error",
                                  error="p2 fail", ts="t")
            return NodeResult(node_id=step.id, status="ok", result="ok", ts="t")

        events = []
        with patch.object(WakerFlowExecutor, "_run_worker", fake_run_worker):
            ex = WakerFlowExecutor(
                flow, "u1", "run-1", {},
                workspace_root=str(isolated_workspace),
                on_event=events.append,
            )
            result = ex.run()

        # p2 error → parallel error → flow failed
        assert result["status"] == "failed"
        # 全部 3 个子都跑了（并发不因一个失败而中止）
        assert sorted(call_log) == ["p1", "p2", "p3"]
        par_ev = next(e for e in events
                      if e["type"] == "node_result" and e["node_id"] == "par1")
        assert par_ev["status"] == "error"

    def test_parallel_empty(self, isolated_workspace):
        """parallel 无子 step → skipped。"""
        flow = MockFlowSpec(
            steps=[MockStep(id="par1", parallel=[])],
        )
        ex = WakerFlowExecutor(
            flow, "u1", "run-1", {},
            workspace_root=str(isolated_workspace),
        )
        result = ex.run()
        # skipped 不算失败 → flow completed
        assert result["status"] == "completed"


# ════════════════════════════════════════════════════════════════
# Executor: if_cond 跳过
# ════════════════════════════════════════════════════════════════
class TestIfCond:
    def test_if_false_skips(self, isolated_workspace):
        """if_cond 渲染为 false → 节点 skipped，_run_worker 不被调。

        语义（GitHub Actions if: 风格）：裸标识符当字符串字面量。
        inputs.mode == run 且 mode=skip → "skip" == "run" → False → 跳过。
        """
        flow = MockFlowSpec(
            inputs=[MockInputField("mode")],
            steps=[
                MockStep(id="s1", worker="w1", if_cond="{{ inputs.mode }} == run"),
            ],
        )
        call_count = {"n": 0}

        def fake_run_worker(self, step, context):
            call_count["n"] += 1
            return NodeResult(node_id=step.id, status="ok", result="x", ts="t")

        events = []
        with patch.object(WakerFlowExecutor, "_run_worker", fake_run_worker):
            ex = WakerFlowExecutor(
                flow, "u1", "run-1", {"mode": "skip"},
                workspace_root=str(isolated_workspace),
                on_event=events.append,
            )
            result = ex.run()

        assert result["status"] == "completed"
        assert call_count["n"] == 0  # worker 未执行
        nr = next(e for e in events if e["type"] == "node_result")
        assert nr["status"] == "skipped"

    def test_if_true_executes(self, isolated_workspace):
        """if_cond 渲染为 true → 正常执行。"""
        flow = MockFlowSpec(
            inputs=[MockInputField("mode")],
            steps=[
                MockStep(id="s1", worker="w1",
                         if_cond="{{ inputs.mode }} == go"),
            ],
        )
        called = {"n": 0}

        def fake_run_worker(self, step, context):
            called["n"] += 1
            return NodeResult(node_id=step.id, status="ok", result="r", ts="t")

        with patch.object(WakerFlowExecutor, "_run_worker", fake_run_worker):
            ex = WakerFlowExecutor(
                flow, "u1", "run-1", {"mode": "go"},
                workspace_root=str(isolated_workspace),
            )
            result = ex.run()

        assert result["status"] == "completed"
        assert called["n"] == 1

    def test_if_invalid_defaults_true(self, isolated_workspace):
        """if_cond 求值失败 → 默认 True（保守执行）。"""
        flow = MockFlowSpec(
            steps=[
                MockStep(id="s1", worker="w1",
                         if_cond="some_undefined_func()"),  # 非法语法（call 不在白名单）
            ],
        )
        called = {"n": 0}

        def fake_run_worker(self, step, context):
            called["n"] += 1
            return NodeResult(node_id=step.id, status="ok", result="r", ts="t")

        with patch.object(WakerFlowExecutor, "_run_worker", fake_run_worker):
            ex = WakerFlowExecutor(
                flow, "u1", "run-1", {},
                workspace_root=str(isolated_workspace),
            )
            result = ex.run()

        assert result["status"] == "completed"
        assert called["n"] == 1  # 默认执行了

    def test_if_in_list(self, isolated_workspace):
        """支持 X in [a, b] 形式。"""
        flow = MockFlowSpec(
            inputs=[MockInputField("role")],
            steps=[
                MockStep(id="s1", worker="w1",
                         if_cond="{{ inputs.role }} in [admin, super]"),
            ],
        )

        # role=admin → True → 执行
        called = {"n": 0}

        def fake_run_worker(self, step, context):
            called["n"] += 1
            return NodeResult(node_id=step.id, status="ok", result="r", ts="t")

        with patch.object(WakerFlowExecutor, "_run_worker", fake_run_worker):
            ex = WakerFlowExecutor(
                flow, "u1", "run-1", {"role": "admin"},
                workspace_root=str(isolated_workspace),
            )
            result = ex.run()
        assert result["status"] == "completed"
        assert called["n"] == 1

        # role=guest → False → 跳过
        called["n"] = 0
        with patch.object(WakerFlowExecutor, "_run_worker", fake_run_worker):
            ex = WakerFlowExecutor(
                flow, "u1", "run-2", {"role": "guest"},
                workspace_root=str(isolated_workspace),
            )
            result = ex.run()
        assert result["status"] == "completed"
        assert called["n"] == 0


# ════════════════════════════════════════════════════════════════
# Executor: askUser 占位
# ════════════════════════════════════════════════════════════════
class TestAskUser:
    def test_ask_user_timeout_with_default(self, isolated_workspace, monkeypatch):
        """超时且有 default → 用 default 继续，flow completed。"""
        # 缩短轮询：让 timeout=1，poll_interval 也压到很小
        monkeypatch.setattr("src.wakerflow.executor.time.sleep", lambda s: None)
        flow = MockFlowSpec(
            steps=[MockStep(
                id="q1",
                ask_user=MockAskUser(question="选 A 还是 B？", timeout=0, default="B"),
            )],
        )
        events = []
        ex = WakerFlowExecutor(
            flow, "u1", "run-to1", {},
            workspace_root=str(isolated_workspace),
            on_event=events.append,
        )
        result = ex.run()
        assert result["status"] == "completed"
        nr = next(e for e in events if e["type"] == "node_result")
        assert nr["status"] == "ok"
        assert nr["result"]["answer"] == "B"

    def test_ask_user_timeout_no_default_fails(self, isolated_workspace, monkeypatch):
        """超时且无 default → node error → flow failed。"""
        monkeypatch.setattr("src.wakerflow.executor.time.sleep", lambda s: None)
        flow = MockFlowSpec(
            steps=[MockStep(
                id="q2",
                ask_user=MockAskUser(question="必须回答", timeout=0, default=None),
            )],
        )
        ex = WakerFlowExecutor(
            flow, "u1", "run-to2", {},
            workspace_root=str(isolated_workspace),
        )
        result = ex.run()
        assert result["status"] == "failed"

    def test_ask_user_answered_via_file(self, isolated_workspace, monkeypatch):
        """写 approval 文件（status=answered）→ executor 读到 answer，不阻塞。

        轮询逻辑是"先检查文件 → 再 sleep"。我们在 sleep 的 patch 里写 answered
        文件，这样下一次循环检查时就能读到。
        """
        import json as _json
        from src.wakerflow.store import FlowStore

        flow = MockFlowSpec(
            name="af",
            steps=[MockStep(
                id="q3",
                ask_user=MockAskUser(
                    question="发布吗？",
                    options=[{"label": "是", "value": "yes"}, {"label": "否", "value": "no"}],
                    timeout=10,
                    default="no",
                ),
            )],
        )
        store = FlowStore("u1", workspace_root=str(isolated_workspace))
        run_id = "run-ans"
        written = {"done": False}

        def _fake_sleep(s):
            # 首次 sleep 前：executor 已写过 pending 文件，我们把它改成 answered
            if not written["done"]:
                apath = store.approval_path(run_id)
                if apath.exists():
                    data = _json.loads(apath.read_text(encoding="utf-8"))
                    data["status"] = "answered"
                    data["answer"] = "yes"
                    apath.write_text(_json.dumps(data), encoding="utf-8")
                    written["done"] = True

        monkeypatch.setattr("src.wakerflow.executor.time.sleep", _fake_sleep)

        events = []
        ex = WakerFlowExecutor(
            flow, "u1", run_id, {},
            workspace_root=str(isolated_workspace),
            on_event=events.append,
        )
        result = ex.run()
        assert result["status"] == "completed", f"flow 状态: {result}"
        nr = next(e for e in events if e["type"] == "node_result" and e["node_id"] == "q3")
        assert nr["status"] == "ok"
        assert nr["result"]["answer"] == "yes"
        # approval 文件应被消费删除
        assert not store.approval_path(run_id).exists()


# ════════════════════════════════════════════════════════════════
# Executor: askUser 挂起等待（P2-22：不占池线程的独立等待结构）
# ════════════════════════════════════════════════════════════════
class TestAskUserSuspend:
    def _flow(self):
        return MockFlowSpec(
            steps=[
                MockStep(
                    id="q1",
                    ask_user=MockAskUser(
                        question="发布吗？",
                        options=[{"label": "是", "value": "yes"},
                                 {"label": "否", "value": "no"}],
                        timeout=3600,
                        default=None,
                    ),
                ),
                MockStep(id="s2", worker="w1", task="基于 {{ steps.q1.result }} 行动"),
            ],
            returns={"out": "{{ steps.s2.result }}"},
        )

    def test_top_level_ask_suspends_then_resumes(self, isolated_workspace):
        """suspend_ask=True：顶层 ask_user 写审批文件、发事件后抛
        FlowSuspended（不阻塞）；answered 后 resume 续跑到完成。"""
        store = FlowStore("u1", workspace_root=str(isolated_workspace))
        events = []
        ex = WakerFlowExecutor(
            self._flow(), "u1", "run-susp", {},
            workspace_root=str(isolated_workspace),
            on_event=events.append, suspend_ask=True,
        )

        def fake_worker(self, step, context):
            q = self._render_context(context)["steps"]["q1"]
            return NodeResult(node_id=step.id, status="ok",
                              result=f"acted:{q['answer']}", ts="t")

        with patch.object(WakerFlowExecutor, "_run_worker", fake_worker):
            with pytest.raises(FlowSuspended) as ei:
                ex.run()

        susp = ei.value.suspension
        assert susp.node_id == "q1"
        # pending 审批文件已写，事件已发，flow 未结束
        apath = store.approval_path("run-susp")
        assert apath.exists()
        data = json.loads(apath.read_text(encoding="utf-8"))
        assert data["status"] == "pending"
        types = [e["type"] for e in events]
        assert "node_start" in types and "approval_required" in types
        assert "flow_end" not in types

        # 模拟审批写入 + 看护线程（独立等待结构，poll 压小）
        data["status"] = "answered"
        data["answer"] = "yes"
        apath.write_text(json.dumps(data), encoding="utf-8")
        import threading as _th
        done = _th.Event()
        susp.watch = _ApprovalWatch(apath, 60, done.set, poll_interval=0.01)
        assert done.wait(timeout=5)
        assert susp.watch.status == "answered"

        # resume：续跑后续 step 并收尾
        with patch.object(WakerFlowExecutor, "_run_worker", fake_worker):
            result = ex.resume(susp)

        assert result["status"] == "completed"
        assert result["returns"]["out"] == "acted:yes"
        # 审批文件已消费；flow_end 恰好一次（resume 收尾发）
        assert not apath.exists()
        assert [e["type"] for e in events].count("flow_end") == 1

    def test_suspend_cancelled_terminates_wait_and_fails(self, isolated_workspace):
        """审批文件被写 cancelled → 看护提前终止等待，flow 以失败收尾。"""
        store = FlowStore("u1", workspace_root=str(isolated_workspace))
        ex = WakerFlowExecutor(
            self._flow(), "u1", "run-cancel", {},
            workspace_root=str(isolated_workspace), suspend_ask=True,
        )
        with pytest.raises(FlowSuspended) as ei:
            ex.run()
        susp = ei.value.suspension

        apath = store.approval_path("run-cancel")
        data = json.loads(apath.read_text(encoding="utf-8"))
        data["status"] = "cancelled"
        apath.write_text(json.dumps(data), encoding="utf-8")

        import threading as _th
        done = _th.Event()
        susp.watch = _ApprovalWatch(apath, 3600, done.set, poll_interval=0.01)
        assert done.wait(timeout=5), "cancelled 未提前终止等待"
        assert susp.watch.status == "cancelled"

        result = ex.resume(susp)
        assert result["status"] == "failed"
        assert not apath.exists()

    def test_suspend_timeout_uses_default(self, isolated_workspace):
        """挂起后无人响应且配了 default → 超时走 default 继续。"""
        flow = MockFlowSpec(
            steps=[MockStep(
                id="q1",
                ask_user=MockAskUser(
                    question="选？",
                    options=[{"label": "A", "value": "a"}],
                    timeout=0, default="a",
                ),
            )],
        )
        ex = WakerFlowExecutor(
            flow, "u1", "run-to", {},
            workspace_root=str(isolated_workspace), suspend_ask=True,
        )
        with pytest.raises(FlowSuspended) as ei:
            ex.run()
        susp = ei.value.suspension
        import threading as _th
        done = _th.Event()
        susp.watch = _ApprovalWatch(
            susp.approval_path, 0, done.set, poll_interval=0.01,
        )
        assert done.wait(timeout=5)
        assert susp.watch.status == "timeout"
        result = ex.resume(susp)
        assert result["status"] == "completed"
        assert result["returns"] == {}

    def test_nested_ask_user_keeps_blocking_semantics(self, isolated_workspace, monkeypatch):
        """嵌套（parallel 内）ask_user 不走挂起，保持阻塞轮询语义（走 default）。"""
        monkeypatch.setattr("src.wakerflow.executor.time.sleep", lambda s: None)
        flow = MockFlowSpec(
            steps=[MockStep(
                id="par1",
                parallel=[MockStep(
                    id="nested",
                    ask_user=MockAskUser(question="？", timeout=0, default="d"),
                )],
            )],
        )
        ex = WakerFlowExecutor(
            flow, "u1", "run-nested", {},
            workspace_root=str(isolated_workspace), suspend_ask=True,
        )
        result = ex.run()  # 不抛 FlowSuspended
        assert result["status"] == "completed"

    def test_top_level_ask_with_false_if_is_skipped_not_suspended(self, isolated_workspace):
        """顶层 ask_user 带 if_cond=false → skipped，不写审批文件、不挂起。"""
        flow = MockFlowSpec(
            inputs=[MockInputField("mode")],
            steps=[MockStep(
                id="q1",
                if_cond="{{ inputs.mode }} == ask",
                ask_user=MockAskUser(question="？", timeout=3600, default=None),
            )],
        )
        store = FlowStore("u1", workspace_root=str(isolated_workspace))
        events = []
        ex = WakerFlowExecutor(
            flow, "u1", "run-skip", {"mode": "auto"},
            workspace_root=str(isolated_workspace),
            on_event=events.append, suspend_ask=True,
        )
        result = ex.run()  # 不抛 FlowSuspended
        assert result["status"] == "completed"
        nr = next(e for e in events if e["type"] == "node_result")
        assert nr["status"] == "skipped"
        assert not store.approval_path("run-skip").exists()


# ════════════════════════════════════════════════════════════════
# Executor: validate_inputs + returns
# ════════════════════════════════════════════════════════════════
class TestInputsAndReturns:
    def test_validate_inputs_failure(self, isolated_workspace):
        """required 输入缺失 → flow failed，error 含说明。"""
        flow = MockFlowSpec(
            inputs=[MockInputField("must_have", required=True)],
            steps=[MockStep(id="s1", worker="w1")],
        )
        ex = WakerFlowExecutor(
            flow, "u1", "run-1", {},  # 缺 must_have
            workspace_root=str(isolated_workspace),
        )
        result = ex.run()
        assert result["status"] == "failed"
        assert "must_have" in result.get("error", "")

    def test_returns_rendered(self, isolated_workspace):
        """returns 用最终 context 模板渲染。"""
        flow = MockFlowSpec(
            inputs=[MockInputField("x")],
            steps=[MockStep(id="s1", worker="w1")],
            returns={
                "summary": "结果: {{ steps.s1.result }}",
                "echo": "输入是 {{ inputs.x }}",
            },
        )

        def fake_run_worker(self, step, context):
            return NodeResult(node_id=step.id, status="ok",
                              result="DONE", ts="t")

        with patch.object(WakerFlowExecutor, "_run_worker", fake_run_worker):
            ex = WakerFlowExecutor(
                flow, "u1", "run-1", {"x": "HELLO"},
                workspace_root=str(isolated_workspace),
            )
            result = ex.run()

        assert result["status"] == "completed"
        assert result["returns"]["summary"] == "结果: DONE"
        assert result["returns"]["echo"] == "输入是 HELLO"

    def test_inputs_default_filled(self, isolated_workspace):
        """非 required 缺失 → 用 default 填充，可被模板引用。"""
        flow = MockFlowSpec(
            inputs=[MockInputField("lang", default="zh")],
            steps=[MockStep(id="s1", worker="w1", task="lang={{ inputs.lang }}")],
            returns={},
        )
        captured = {}

        def fake_run_worker(self, step, context):
            captured["task"] = render(step.task, self._render_context(context))
            return NodeResult(node_id=step.id, status="ok", result="ok", ts="t")

        with patch.object(WakerFlowExecutor, "_run_worker", fake_run_worker):
            ex = WakerFlowExecutor(
                flow, "u1", "run-1", {},  # 不传 lang → 用 default
                workspace_root=str(isolated_workspace),
            )
            result = ex.run()

        assert result["status"] == "completed"
        assert captured["task"] == "lang=zh"


# ════════════════════════════════════════════════════════════════
# Executor: worker 节点 fork（用 monkeypatch subprocess 验证命令构造，
# 不真实 fork——避免依赖 LLM）
# ════════════════════════════════════════════════════════════════
class TestWorkerSpawnCommand:
    class RecordingStdin(io.StringIO):
        """记录写入内容后照常关闭（StringIO close 后 getvalue 会抛）。"""

        def __init__(self):
            super().__init__()
            self.final = ""

        def close(self):
            self.final = self.getvalue()
            super().close()

    def test_worker_spawn_command_constructed(self, isolated_workspace):
        """_run_worker 构造的子进程命令正确，任务文本经 stdin 传递（P2-20）。"""
        flow = MockFlowSpec(
            inputs=[MockInputField("q")],
            steps=[MockStep(
                id="w1", worker="my-waker",
                task="回答: {{ inputs.q }}",
            )],
        )

        # fake Popen：模拟 worker_node stdout NDJSON 流
        class FakeProc:
            def __init__(self, lines):
                self.stdin = TestWorkerSpawnCommand.RecordingStdin()
                self.stdout = io.StringIO("".join(l + "\n" for l in lines))
                self.stderr = io.StringIO("")
                self._poll = None
            def wait(self, timeout=None):
                return 0
            def poll(self):
                return 0
            def kill(self):
                pass

        ndjson_lines = [
            json.dumps({"type": "ready", "waker": "my-waker",
                        "node_run_id": "run-1/w1"}),
            json.dumps({"type": "node_event", "event": {"type": "token",
                       "content": "hi"}}),
            json.dumps({"type": "result", "status": "ok",
                       "content": "最终回答", "run_id": "run-1/w1",
                       "waker": "my-waker"}),
        ]
        fake_proc = FakeProc(ndjson_lines)

        with patch("src.wakerflow.executor.subprocess.Popen",
                   return_value=fake_proc) as mock_popen:
            ex = WakerFlowExecutor(
                flow, "u1", "run-1", {"q": "你好"},
                workspace_root=str(isolated_workspace),
            )
            result = ex.run()

        assert result["status"] == "completed"
        # 校验 Popen 调用参数
        call = mock_popen.call_args
        cmd = call[0][0]
        assert "-m" in cmd
        assert "src.wakerflow.worker_node" in cmd
        assert "--user-id" in cmd
        # P2-20：任务文本不走 argv（Windows 32k 上限 → WinError 206），
        # 改经 stdin 传递
        assert "--task" not in cmd
        assert "--task-stdin" in cmd
        # 任务已渲染并写入 stdin
        assert fake_proc.stdin.final == "回答: 你好"
        # waker-name
        wn_idx = cmd.index("--waker-name")
        assert cmd[wn_idx + 1] == "my-waker"
        # node-run-id 拼接 run_id/step.id
        nrid_idx = cmd.index("--node-run-id")
        assert cmd[nrid_idx + 1] == "run-1/w1"

    def test_worker_huge_task_not_in_argv(self, isolated_workspace):
        """数万字符的渲染任务不再进 argv（P2-20 触发场景本身）。"""
        flow = MockFlowSpec(
            steps=[MockStep(id="w1", worker="w", task="x")],
        )
        captured = {}

        class FakeProc:
            def __init__(self):
                self.stdin = TestWorkerSpawnCommand.RecordingStdin()
                self.stdout = io.StringIO(
                    json.dumps({"type": "result", "status": "ok", "content": "done"}) + "\n"
                )
                self.stderr = io.StringIO("")
            def wait(self, timeout=None): return 0
            def poll(self): return 0
            def kill(self): pass

        def fake_popen(cmd, **kw):
            captured["cmd"] = cmd
            captured["proc"] = FakeProc()
            return captured["proc"]

        rendered = "长" * 40000  # > Windows 32k argv 上限
        with patch("src.wakerflow.executor.subprocess.Popen", side_effect=fake_popen), \
             patch("src.wakerflow.executor.render", return_value=rendered):
            ex = WakerFlowExecutor(
                flow, "u1", "run-1", {},
                workspace_root=str(isolated_workspace),
            )
            result = ex.run()

        assert result["status"] == "completed"
        assert "--task" not in captured["cmd"]
        assert len(captured["proc"].stdin.final) == 40000

    def test_worker_no_result_event(self, isolated_workspace):
        """worker_node 没输出 result 事件 → error。"""
        flow = MockFlowSpec(
            steps=[MockStep(id="w1", worker="w", task="x")],
        )

        class FakeProc:
            stdout = io.StringIO(
                json.dumps({"type": "ready"}) + "\n"
                + json.dumps({"type": "log", "message": "doing"}) + "\n"
            )
            stderr = io.StringIO("some traceback")
            def wait(self, timeout=None): return 0
            def poll(self): return 0
            def kill(self): pass

        with patch("src.wakerflow.executor.subprocess.Popen",
                   return_value=FakeProc()):
            ex = WakerFlowExecutor(
                flow, "u1", "run-1", {},
                workspace_root=str(isolated_workspace),
            )
            result = ex.run()

        assert result["status"] == "failed"

    def test_worker_spawn_exception(self, isolated_workspace):
        """Popen 抛异常 → error，flow failed。"""
        flow = MockFlowSpec(
            steps=[MockStep(id="w1", worker="w", task="x")],
        )
        with patch("src.wakerflow.executor.subprocess.Popen",
                   side_effect=OSError("spawn 失败")):
            ex = WakerFlowExecutor(
                flow, "u1", "run-1", {},
                workspace_root=str(isolated_workspace),
            )
            result = ex.run()
        assert result["status"] == "failed"


# ════════════════════════════════════════════════════════════════
# render 函数单元测试
# ════════════════════════════════════════════════════════════════
class TestRender:
    """render 函数行为测试。

    注意：template 模块（parser 子任务）若已实现则用其语义：
    - 缺失键抛 TemplateError（KeyError 子类）
    - 非字符串模板原样返回
    若 template 模块未实现，executor 内置兜底（缺失键→空串）。
    本测试类用 try import 探测，两种模式下断言不同。
    """

    def test_basic_interpolation(self):
        assert render("hi {{ name }}", {"name": "Z"}) == "hi Z"

    def test_nested_path(self):
        ctx = {"inputs": {"x": {"y": "deep"}}}
        assert render("{{ inputs.x.y }}", ctx) == "deep"

    def test_list_index(self):
        ctx = {"items": ["a", "b", "c"]}
        assert render("{{ items.0 }}-{{ items.2 }}", ctx) == "a-c"

    def test_no_braces(self):
        assert render("plain text", {}) == "plain text"

    def test_non_string_template_passthrough(self):
        """非 str 模板原样返回（template 模块语义）或转 str（兜底语义）。"""
        # 两种实现都接受：None 要么返回 None 要么返回 ""，都"假值等价"
        assert not render(None, {})
        # 数字：template 模块原样返回 123，兜底返回 "123"；都"真值"
        assert render(123, {})

    def test_missing_key_behavior(self):
        """缺失键：template 模块抛 TemplateError；兜底返回空串。

        两种语义都"不产生有效内容"——用 try 探测。
        """
        try:
            from src.wakerflow.template import TemplateError  # type: ignore
            has_template = True
        except Exception:
            has_template = False

        if has_template:
            # template 模块语义：抛异常
            with pytest.raises(KeyError):
                render("[{{ nope }}]", {})
        else:
            # 兜底语义：空串
            assert render("[{{ nope }}]", {}) == "[]"
