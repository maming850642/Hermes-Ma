"""WakerFlow 管理 API（/api/wakerflow）测试。

不启动完整 create_app（那会 fork worker 子进程），而是构造一个仅挂 wakerflow
router 的精简 FastAPI app，用 TestClient 打。

覆盖：list / create（校验 ok）/ create（非法 yaml 400）/ get / update / delete /
runs / approvals list / approve / invoke（异步走 FlowRunner mock）/ status。

invoke 和 approve 走主进程 FlowRunner（不走 worker IPC），测试用 FakeFlowRunner mock。
"""
import json
import threading
import time
import urllib.request as _urlreq
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.constants import LOCAL_USER
from src.storage.sqlite_provider import SQLiteProvider
from src.wakerflow.runner import FlowRunner
from src.wakerflow.store import FlowStore

from web_fastapi.routers import wakerflow as wakerflow_router


# ============================================
# 有效 / 非法 yaml 样本
# ============================================
VALID_YAML = """\
name: demo_flow
description: 一个示例 flow
inputs:
  topic:
    type: string
    required: true
steps:
  - id: s1
    worker: assistant
    task: 写一段关于 {{inputs.topic}} 的文字
returns:
  text: "{{steps.s1.result}}"
"""

INVALID_YAML_NO_NAME = """\
description: 缺 name
steps:
  - id: s1
    worker: assistant
    task: hi
"""

INVALID_YAML_DUP_ID = """\
name: dup
steps:
  - id: s1
    worker: a
    task: t1
  - id: s1
    worker: a
    task: t2
"""


# ============================================
# FakeFlowRunner：替身 FlowRunner（不真跑 executor）
# ============================================
class FakeFlowRunner:
    """替身 FlowRunner。

    submit → 生成 run_id，立即把状态记为 completed（或测试设定的 status）。
    approve → 直接返回 True（或测试设定 False）。
    get_status / list_active / list_recent 查内存状态。
    """

    def __init__(self, workspace_root=""):
        self._workspace_root = workspace_root
        self._runs = {}
        self._counter = 0
        self._lock = threading.Lock()
        # submit 后立即设成的终态（completed/failed）；None 表示保持 running
        self.next_status = "completed"
        self.next_returns = {"text": "done"}
        self.next_error = ""
        self.approve_ok = True
        # submit 抛出的异常（模拟 parse_flow 失败等）；默认 None
        self.submit_exception = None
        # 调用记录
        self.submit_calls = []
        self.approve_calls = []

    def submit(self, user_id, flow_name, inputs=None):
        if self.submit_exception is not None:
            raise self.submit_exception
        with self._lock:
            self._counter += 1
            run_id = f"run-{self._counter:03d}"
        self.submit_calls.append({
            "user_id": user_id, "flow_name": flow_name,
            "inputs": inputs or {}, "run_id": run_id,
        })
        rec = {
            "run_id": run_id, "flow_name": flow_name, "user_id": user_id,
            "status": self.next_status, "started_at": "2026-07-27T10:00:00",
            "finished_at": "2026-07-27T10:00:01" if self.next_status in ("completed","failed","error") else "",
            "returns": self.next_returns if self.next_status == "completed" else {},
            "error": self.next_error,
        }
        self._runs[run_id] = rec
        return run_id

    def get_status(self, run_id):
        return self._runs.get(run_id)

    def list_active(self, user_id=None):
        # 对齐真实 FlowRunner.list_active 的活跃状态集合（P2-22）：
        # waiting_approval（ask_user 挂起）也属活跃——run 没结束
        return [r for r in self._runs.values()
                if r["status"] in ("pending", "running", "waiting_approval")
                and (user_id is None or r["user_id"] == user_id)]

    def list_recent(self, user_id=None, limit=20):
        out = list(self._runs.values())
        if user_id is not None:
            out = [r for r in out if r["user_id"] == user_id]
        return out[:limit]

    def approve(self, user_id, run_id, answer, answered_by=""):
        self.approve_calls.append({
            "user_id": user_id, "run_id": run_id,
            "answer": answer, "answered_by": answered_by,
        })
        return self.approve_ok

    def start(self): pass
    def shutdown(self): pass


# ============================================
# fixtures
# ============================================
@pytest.fixture
def app(tmp_path):
    """精简 app：只挂 wakerflow router；state 注入指向 tmp_path 的 scheduler
    （仅用于 _store 取 workspace_root）+ FakeFlowRunner。"""
    a = FastAPI()
    a.include_router(wakerflow_router.router, prefix="/api/wakerflow", tags=["wakerflow"])

    class _FakeSched:
        _workspace_root = str(tmp_path)

    a.state.waker_scheduler = _FakeSched()
    a.state.flow_runner = FakeFlowRunner(workspace_root=str(tmp_path))
    # 单用户：登录 cookie 是唯一鉴权来源（身份恒 LOCAL_USER）
    return a


@pytest.fixture
def fake_runner(app):
    """暴露 app 上的 FakeFlowRunner，测试可改 next_status 等。"""
    return app.state.flow_runner


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def auth_headers(app):
    """已登录会话（签名 cookie）；身份恒 LOCAL_USER（单用户坍缩）。"""
    return {}


# ============================================
# helpers
# ============================================
def _create_flow(client, auth_headers, name="demo_flow", yaml_text=VALID_YAML):
    r = client.post(
        "/api/wakerflow/items", headers=auth_headers,
        json={"name": name, "yaml": yaml_text},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ============================================
# create / 校验
# ============================================
def test_create_ok(client, auth_headers):
    j = _create_flow(client, auth_headers)
    assert j["ok"] is True


def test_create_invalid_yaml_no_name_400(client, auth_headers):
    r = client.post(
        "/api/wakerflow/items", headers=auth_headers,
        json={"name": "bad1", "yaml": INVALID_YAML_NO_NAME},
    )
    assert r.status_code == 400
    assert "name" in r.json()["detail"].lower()


def test_create_invalid_yaml_dup_id_400(client, auth_headers):
    r = client.post(
        "/api/wakerflow/items", headers=auth_headers,
        json={"name": "bad2", "yaml": INVALID_YAML_DUP_ID},
    )
    assert r.status_code == 400
    assert "重复" in r.json()["detail"] or "dup" in r.json()["detail"].lower()


def test_create_invalid_yaml_syntax_400(client, auth_headers):
    bad = "name: x\n  - : bad yaml :::\n\tindent mix"
    r = client.post(
        "/api/wakerflow/items", headers=auth_headers,
        json={"name": "bad3", "yaml": bad},
    )
    assert r.status_code == 400


def test_items_public_without_headers(client):
    # 免认证：匿名列表可见（wakerflow 自身无 API-token 门禁）
    r = client.get("/api/wakerflow/items")
    assert r.status_code == 200


# ============================================
# get / list
# ============================================
def test_get_returns_yaml(client, auth_headers):
    _create_flow(client, auth_headers, name="demo_flow")
    r = client.get("/api/wakerflow/items/demo_flow", headers=auth_headers)
    assert r.status_code == 200
    flow = r.json()["flow"]
    assert flow["name"] == "demo_flow"
    assert flow["description"] == "一个示例 flow"
    assert flow["steps_count"] == 1
    # yaml 文本回显（原始）
    assert "name: demo_flow" in flow["yaml"]


def test_get_404(client, auth_headers):
    r = client.get("/api/wakerflow/items/nope", headers=auth_headers)
    assert r.status_code == 404


def test_list(client, auth_headers):
    _create_flow(client, auth_headers, name="a_flow")
    _create_flow(client, auth_headers, name="b_flow")
    r = client.get("/api/wakerflow/items", headers=auth_headers)
    assert r.status_code == 200
    arr = r.json()["flows"]
    names = sorted(f["name"] for f in arr)
    assert names == ["a_flow", "b_flow"]
    # 列表项有 description / steps_count / last_run / last_status 字段
    for f in arr:
        assert "description" in f
        assert "steps_count" in f
        assert "last_run" in f
        assert "last_status" in f
    # 无 run 时 last_run 为 None / last_status 为空
    demo = next(f for f in arr if f["name"] == "a_flow")
    assert demo["last_run"] is None
    assert demo["last_status"] == ""


def test_list_includes_last_run(client, app, auth_headers):
    """手写一个 run jsonl，list 应读出 last_run / last_status。"""
    _create_flow(client, auth_headers, name="demo_flow")
    run_dir = (Path(app.state.waker_scheduler._workspace_root)
               / "wakerflows" / "demo_flow" / "runs")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "r-001.jsonl").write_text(
        json.dumps({"type": "flow_start", "run_id": "r-001"}) + "\n" +
        json.dumps({"type": "flow_end", "run_id": "r-001", "status": "completed"}) + "\n",
        encoding="utf-8",
    )
    r = client.get("/api/wakerflow/items", headers=auth_headers)
    arr = r.json()["flows"]
    demo = next(f for f in arr if f["name"] == "demo_flow")
    assert demo["last_run"] == "r-001"
    assert demo["last_status"] == "completed"


# ============================================
# update / delete
# ============================================
def test_update(client, auth_headers):
    _create_flow(client, auth_headers, name="demo_flow")
    new_yaml = VALID_YAML.replace("一个示例 flow", "改过的描述")
    r = client.put(
        "/api/wakerflow/items/demo_flow", headers=auth_headers,
        json={"yaml": new_yaml},
    )
    assert r.status_code == 200, r.text
    # 落盘
    flow = client.get("/api/wakerflow/items/demo_flow", headers=auth_headers).json()["flow"]
    assert flow["description"] == "改过的描述"


def test_update_invalid_yaml_400(client, auth_headers):
    _create_flow(client, auth_headers, name="demo_flow")
    r = client.put(
        "/api/wakerflow/items/demo_flow", headers=auth_headers,
        json={"yaml": INVALID_YAML_NO_NAME},
    )
    assert r.status_code == 400


def test_update_404(client, auth_headers):
    r = client.put(
        "/api/wakerflow/items/nope", headers=auth_headers,
        json={"yaml": VALID_YAML},
    )
    assert r.status_code == 404


def test_delete(client, auth_headers):
    _create_flow(client, auth_headers, name="demo_flow")
    r = client.delete("/api/wakerflow/items/demo_flow", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # 再查 404
    assert client.get("/api/wakerflow/items/demo_flow", headers=auth_headers).status_code == 404


def test_delete_404(client, auth_headers):
    r = client.delete("/api/wakerflow/items/nope", headers=auth_headers)
    assert r.status_code == 404


# ============================================
# runs
# ============================================
def _write_flow_run(run_dir: Path, run_id: str, events: list, mtime_ns: int | None = None):
    p = run_dir / f"{run_id}.jsonl"
    chunks = []
    for e in events:
        if isinstance(e, str):
            chunks.append(e if e.endswith("\n") else e + "\n")
        else:
            chunks.append(json.dumps(e, ensure_ascii=False) + "\n")
    p.write_text("".join(chunks), encoding="utf-8")
    if mtime_ns is not None:
        import os
        os.utime(p, ns=(mtime_ns, mtime_ns))
    return p


def test_runs_empty_when_no_run(client, auth_headers):
    _create_flow(client, auth_headers, name="demo_flow")
    r = client.get("/api/wakerflow/items/demo_flow/runs", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["runs"] == []


def test_runs_lists_newest_first(client, app, auth_headers):
    _create_flow(client, auth_headers, name="demo_flow")
    run_dir = (Path(app.state.waker_scheduler._workspace_root)
               / "wakerflows" / "demo_flow" / "runs")
    run_dir.mkdir(parents=True, exist_ok=True)
    import time
    now = time.time_ns()
    _write_flow_run(run_dir, "old-run", [
        {"type": "flow_start", "run_id": "old-run", "ts": "2026-09-07T08:00:00"},
        {"type": "flow_end", "run_id": "old-run", "status": "completed", "ts": "2026-09-07T08:01:00"},
    ], mtime_ns=now - 2_000_000_000)
    _write_flow_run(run_dir, "new-run", [
        {"type": "flow_start", "run_id": "new-run", "ts": "2026-09-08T13:53:28"},
        {"type": "flow_end", "run_id": "new-run", "status": "failed", "ts": "2026-09-08T13:57:03"},
    ], mtime_ns=now)
    r = client.get("/api/wakerflow/items/demo_flow/runs", headers=auth_headers)
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert [x["run_id"] for x in runs] == ["new-run", "old-run"]
    assert runs[0]["status"] == "failed"
    assert runs[0]["duration_s"] == 215
    assert runs[1]["status"] == "completed"
    assert runs[1]["duration_s"] == 60


def test_runs_limit(client, app, auth_headers):
    _create_flow(client, auth_headers, name="demo_flow")
    run_dir = (Path(app.state.waker_scheduler._workspace_root)
               / "wakerflows" / "demo_flow" / "runs")
    run_dir.mkdir(parents=True, exist_ok=True)
    import time
    base = time.time_ns()
    for i in range(5):
        _write_flow_run(run_dir, f"r{i}", [
            {"type": "flow_start", "ts": "2026-09-08T08:00:00"},
            {"type": "flow_end", "status": "completed", "ts": "2026-09-08T08:00:01"},
        ], mtime_ns=base + i * 1_000_000)
    r = client.get("/api/wakerflow/items/demo_flow/runs?limit=3", headers=auth_headers)
    assert r.status_code == 200
    assert len(r.json()["runs"]) == 3


def test_runs_interrupted_when_no_end(client, app, auth_headers):
    _create_flow(client, auth_headers, name="demo_flow")
    run_dir = (Path(app.state.waker_scheduler._workspace_root)
               / "wakerflows" / "demo_flow" / "runs")
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_flow_run(run_dir, "20260908T135328-abc", [
        {"type": "flow_start", "run_id": "20260908T135328-abc", "ts": "2026-09-08T13:53:28"},
    ])
    r = client.get("/api/wakerflow/items/demo_flow/runs", headers=auth_headers)
    rec = r.json()["runs"][0]
    assert rec["status"] == "interrupted"
    assert rec["duration_s"] is None


def test_runs_404_unknown_flow(client, auth_headers):
    r = client.get("/api/wakerflow/items/nope/runs", headers=auth_headers)
    assert r.status_code == 404


def test_run_detail_extracts_returns_and_skips_worker_events(client, app, auth_headers):
    _create_flow(client, auth_headers, name="demo_flow")
    run_dir = (Path(app.state.waker_scheduler._workspace_root)
               / "wakerflows" / "demo_flow" / "runs")
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_flow_run(run_dir, "r-ok", [
        {"type": "flow_start", "run_id": "r-ok", "ts": "2026-09-08T13:53:28"},
        {"type": "node_start", "node_id": "brief", "node_type": "worker", "ts": "2026-09-08T13:53:29"},
        {"type": "worker_event", "node_id": "brief", "event": {"type": "token", "d": "x"}},
        {"type": "node_result", "node_id": "brief", "status": "ok",
         "result": {"result": "# 日报\n正文"}},
        {"type": "node_end", "node_id": "brief", "status": "ok", "ts": "2026-09-08T13:57:00"},
        {"type": "flow_end", "run_id": "r-ok", "status": "completed",
         "returns": {"brief": "# 日报\n正文"}, "ts": "2026-09-08T13:57:03"},
    ])
    r = client.get("/api/wakerflow/items/demo_flow/runs/r-ok", headers=auth_headers)
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "completed"
    assert j["returns"]["brief"] == "# 日报\n正文"
    assert j["nodes"][0]["node_id"] == "brief"
    types = [e["type"] for e in j["events"]]
    assert "worker_event" not in types
    assert types == ["flow_start", "node_start", "node_result", "node_end", "flow_end"]
    assert j["duration_s"] == 215


def test_run_detail_404_unknown_run(client, auth_headers):
    _create_flow(client, auth_headers, name="demo_flow")
    r = client.get("/api/wakerflow/items/demo_flow/runs/nope", headers=auth_headers)
    assert r.status_code == 404


def test_run_detail_rejects_traversal_run_id(client, auth_headers):
    _create_flow(client, auth_headers, name="demo_flow")
    r = client.get(
        "/api/wakerflow/items/demo_flow/runs/..%5C..%5Cevil",
        headers=auth_headers,
    )
    assert r.status_code == 400


# ============================================
# invoke（异步：走 FlowRunner，立即返回 running + run_id）
# ============================================
def test_invoke_returns_running_and_run_id(client, auth_headers, fake_runner):
    _create_flow(client, auth_headers, name="demo_flow")
    r = client.post(
        "/api/wakerflow/items/demo_flow/invoke", headers=auth_headers,
        json={"inputs": {"topic": "cats"}},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["run_id"]  # 生成了 run_id
    assert j["status"] == "running"  # 异步：立即返回 running
    assert j["ok"] is True
    # FlowRunner.submit 收到正确参数
    assert len(fake_runner.submit_calls) == 1
    call = fake_runner.submit_calls[0]
    assert call["flow_name"] == "demo_flow"
    assert call["user_id"] == LOCAL_USER
    assert call["inputs"] == {"topic": "cats"}
    assert call["run_id"] == j["run_id"]


def test_invoke_default_inputs(client, auth_headers, fake_runner):
    _create_flow(client, auth_headers, name="demo_flow")
    r = client.post(
        "/api/wakerflow/items/demo_flow/invoke", headers=auth_headers,
        json={},
    )
    assert r.status_code == 200
    assert fake_runner.submit_calls[0]["inputs"] == {}


def test_invoke_404_unknown_flow(client, auth_headers):
    r = client.post("/api/wakerflow/items/nope/invoke", headers=auth_headers, json={})
    assert r.status_code == 404


def test_invoke_503_when_runner_missing(app, auth_headers):
    """FlowRunner 未挂载（启动失败）→ 503。"""
    app.state.flow_runner = None
    c = TestClient(app)
    # 先建 flow（CRUD 不需要 runner）
    c.post("/api/wakerflow/items", headers=auth_headers,
           json={"name": "demo_flow", "yaml": VALID_YAML})
    r = c.post("/api/wakerflow/items/demo_flow/invoke", headers=auth_headers, json={})
    assert r.status_code == 503


def test_invoke_400_on_parse_error(client, auth_headers, fake_runner):
    """flow 定义坏掉（YAML 解析失败）→ 400。

    submit 调 parse_flow，坏的 YAML 抛 FlowParseError → 路由转 400。
    用 fake_runner.submit_exception 模拟（FakeFlowRunner 不真跑 parse_flow）。
    """
    from src.wakerflow.parser import FlowParseError
    fake_runner.submit_exception = FlowParseError("YAML 语法错误")
    _create_flow(client, auth_headers, name="demo_flow")
    r = client.post(
        "/api/wakerflow/items/demo_flow/invoke", headers=auth_headers, json={},
    )
    assert r.status_code == 400


# ============================================
# status（轮询 run 状态）
# ============================================
def test_status_completed(client, auth_headers, fake_runner):
    _create_flow(client, auth_headers, name="demo_flow")
    # 先 invoke 一次（fake_runner 默认 next_status=completed）
    r = client.post(
        "/api/wakerflow/items/demo_flow/invoke", headers=auth_headers, json={},
    )
    run_id = r.json()["run_id"]
    # 查 status（进度轮询端点——契约不含 returns，returns 在 /result 端点）
    r = client.get(
        f"/api/wakerflow/items/demo_flow/runs/{run_id}/status", headers=auth_headers,
    )
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "completed"
    assert j["run_id"] == run_id
    # returns 在 runner 记录里（/result 端点从 jsonl 读取，fake runner 不落盘）
    rec = fake_runner.get_status(run_id)
    assert rec["returns"] == {"text": "done"}


def test_status_running(client, auth_headers, fake_runner):
    _create_flow(client, auth_headers, name="demo_flow")
    fake_runner.next_status = "running"  # 不立即完成
    r = client.post(
        "/api/wakerflow/items/demo_flow/invoke", headers=auth_headers, json={},
    )
    run_id = r.json()["run_id"]
    r = client.get(
        f"/api/wakerflow/items/demo_flow/runs/{run_id}/status", headers=auth_headers,
    )
    assert r.json()["status"] == "running"


def test_status_fallback_to_jsonl(client, app, auth_headers):
    """runner 内存查不到（重启后）→ 回退读 jsonl 的 flow_end。"""
    _create_flow(client, auth_headers, name="demo_flow")
    run_dir = (Path(app.state.waker_scheduler._workspace_root)
               / "wakerflows" / "demo_flow" / "runs")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "r-old.jsonl").write_text(
        json.dumps({"type": "flow_start", "run_id": "r-old"}) + "\n" +
        json.dumps({"type": "flow_end", "run_id": "r-old", "status": "failed"}) + "\n",
        encoding="utf-8",
    )
    r = client.get(
        "/api/wakerflow/items/demo_flow/runs/r-old/status", headers=auth_headers,
    )
    assert r.json()["status"] == "failed"


# ============================================
# approvals
# ============================================
def test_approvals_empty(client, auth_headers):
    r = client.get("/api/wakerflow/approvals", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["approvals"] == []


def test_approvals_list_pending(client, app, auth_headers):
    """手写两个审批文件（pending + answered），只应列 pending。"""
    adir = (Path(app.state.waker_scheduler._workspace_root)
            / "wakerflows" / "_approvals")
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "run-a.json").write_text(json.dumps({
        "run_id": "run-a", "flow_name": "demo_flow", "node_id": "ask1",
        "question": "继续吗？", "options": [{"label": "是", "value": "yes"}],
        "status": "pending", "created_ts": "2026-07-27T10:00:00",
    }), encoding="utf-8")
    (adir / "run-b.json").write_text(json.dumps({
        "run_id": "run-b", "flow_name": "demo_flow", "node_id": "ask2",
        "question": "旧问题", "options": [], "status": "answered",
        "answer": "yes", "created_ts": "2026-07-27T09:00:00",
    }), encoding="utf-8")
    r = client.get("/api/wakerflow/approvals", headers=auth_headers)
    assert r.status_code == 200
    arr = r.json()["approvals"]
    assert len(arr) == 1
    a = arr[0]
    assert a["run_id"] == "run-a"
    assert a["question"] == "继续吗？"
    assert a["flow_name"] == "demo_flow"
    assert a["node_id"] == "ask1"
    assert a["options"] == [{"label": "是", "value": "yes"}]
    assert a["created_ts"] == "2026-07-27T10:00:00"


def test_approvals_skips_bad_json(client, app, auth_headers):
    """坏 json 文件应被跳过，不爆 500。"""
    adir = (Path(app.state.waker_scheduler._workspace_root)
            / "wakerflows" / "_approvals")
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "bad.json").write_text("not a json {{{", encoding="utf-8")
    r = client.get("/api/wakerflow/approvals", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["approvals"] == []


def test_approve_calls_runner(client, auth_headers, fake_runner):
    """approve → FlowRunner.approve，记录 answered_by=user_id。"""
    r = client.post(
        "/api/wakerflow/approvals/run-a", headers=auth_headers,
        json={"answer": "yes"},
    )
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["run_id"] == "run-a"
    assert j["answer"] == "yes"
    assert len(fake_runner.approve_calls) == 1
    call = fake_runner.approve_calls[0]
    assert call["run_id"] == "run-a"
    assert call["answer"] == "yes"
    assert call["answered_by"] == LOCAL_USER


def test_approve_404_when_no_pending(client, auth_headers, fake_runner):
    """审批文件不存在（runner.approve 返回 False）→ 404。"""
    fake_runner.approve_ok = False
    r = client.post(
        "/api/wakerflow/approvals/run-x", headers=auth_headers,
        json={"answer": "yes"},
    )
    assert r.status_code == 404


def test_approve_503_when_runner_missing(app, auth_headers):
    """FlowRunner 未挂载 → 503。"""
    app.state.flow_runner = None
    c = TestClient(app)
    r = c.post(
        "/api/wakerflow/approvals/run-a", headers=auth_headers,
        json={"answer": "yes"},
    )
    assert r.status_code == 503


# ============================================
# enabled / trigger（M3.2 调度）
# ============================================
def test_set_enabled(client, auth_headers):
    """PATCH enabled 修改 flow 的 enabled 字段并落盘。"""
    _create_flow(client, auth_headers, name="demo_flow")
    r = client.patch(
        "/api/wakerflow/items/demo_flow/enabled", headers=auth_headers,
        json={"enabled": False},
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    # 落盘确认：重新 GET 看 schedule.enabled
    flow = client.get("/api/wakerflow/items/demo_flow", headers=auth_headers).json()["flow"]
    assert flow["schedule"]["enabled"] is False


def test_set_enabled_404(client, auth_headers):
    r = client.patch(
        "/api/wakerflow/items/nope/enabled", headers=auth_headers,
        json={"enabled": True},
    )
    assert r.status_code == 404


def test_trigger_no_token_401(client, auth_headers, fake_runner):
    """trigger 无 token → 401。"""
    # 先建一个 api_enabled=true 的 flow（直接写 yaml）
    yaml_with_token = VALID_YAML.replace(
        "steps:", "api_enabled: true\napi_token: secret-xyz\nsteps:")
    client.post("/api/wakerflow/items", headers=auth_headers,
                json={"name": "triggerable", "yaml": yaml_with_token})
    r = client.post("/api/wakerflow/items/triggerable/trigger", headers=auth_headers)
    assert r.status_code == 401


def test_trigger_wrong_token_401(client, auth_headers, fake_runner):
    yaml_with_token = VALID_YAML.replace(
        "steps:", "api_enabled: true\napi_token: secret-xyz\nsteps:")
    client.post("/api/wakerflow/items", headers=auth_headers,
                json={"name": "triggerable", "yaml": yaml_with_token})
    r = client.post("/api/wakerflow/items/triggerable/trigger",
                    headers={**auth_headers, "X-Flow-Token": "wrong"})
    assert r.status_code == 401


def test_trigger_api_disabled_403(client, auth_headers, fake_runner):
    """api_enabled=false → 403。"""
    _create_flow(client, auth_headers, name="demo_flow")  # 默认 api_enabled=false
    r = client.post("/api/wakerflow/items/demo_flow/trigger",
                    headers={**auth_headers, "X-Flow-Token": "any"})
    assert r.status_code == 403


def test_trigger_ok(client, auth_headers, fake_runner):
    """正确 token + api_enabled → 异步提交，返回 run_id。"""
    yaml_with_token = VALID_YAML.replace(
        "steps:", "api_enabled: true\napi_token: secret-xyz\nsteps:")
    client.post("/api/wakerflow/items", headers=auth_headers,
                json={"name": "triggerable", "yaml": yaml_with_token})
    r = client.post("/api/wakerflow/items/triggerable/trigger",
                    headers={**auth_headers, "X-Flow-Token": "secret-xyz"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert j["run_id"]
    assert j["status"] == "running"


def test_list_includes_schedule_fields(client, auth_headers):
    """列表项含 enabled / schedule_type / next_run_at 字段。"""
    _create_flow(client, auth_headers, name="demo_flow")
    r = client.get("/api/wakerflow/items", headers=auth_headers)
    f = next(x for x in r.json()["flows"] if x["name"] == "demo_flow")
    assert "enabled" in f
    assert "schedule_type" in f
    assert "next_run_at" in f
    assert f["schedule_type"] == "none"  # 默认


def test_get_returns_schedule_and_state(client, auth_headers):
    """详情接口返回 schedule + state 字段。"""
    _create_flow(client, auth_headers, name="demo_flow")
    r = client.get("/api/wakerflow/items/demo_flow", headers=auth_headers)
    flow = r.json()["flow"]
    assert "schedule" in flow
    assert "state" in flow
    assert flow["schedule"]["schedule_type"] == "none"
    assert "next_run_at" in flow["state"]


# ============================================
# W4/H1: preview 非持久化转换（blocks⇄yaml，取代 __tmp__ 死路）
# ============================================
def test_preview_blocks_to_yaml(client, auth_headers):
    """blocks → yaml（导出路径），不落盘。"""
    blocks = [{
        "id": "s1", "type": "worker", "waker": "w1",
        "task": "hi",
    }]
    r = client.post("/api/wakerflow/preview", headers=auth_headers,
                    json={"name": "pv1", "blocks": blocks, "description": "d"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert "steps:" in j["yaml"] and "pv1" in j["yaml"]
    assert j["name"] == "pv1" and j["description"] == "d"
    assert isinstance(j["blocks"], list) and j["blocks"]
    # 不落盘：items 列表里没有 pv1
    r2 = client.get("/api/wakerflow/items", headers=auth_headers)
    names = [f["name"] for f in r2.json().get("flows", [])]
    assert "pv1" not in names


def test_preview_yaml_to_blocks(client, auth_headers):
    """yaml → blocks（导入路径），不落盘。"""
    yaml_text = (
        "name: pv2\ndescription: from yaml\nsteps:\n"
        "  - id: s1\n    worker: w1\n    task: hi\n"
    )
    r = client.post("/api/wakerflow/preview", headers=auth_headers,
                    json={"name": "whatever", "yaml": yaml_text})
    assert r.status_code == 200
    j = r.json()
    assert j["name"] == "pv2"           # 名字来自 yaml，不是 body
    assert any(b.get("type") == "worker" for b in j["blocks"])
    assert "pv2" in j["yaml"]


def test_preview_invalid_blocks_400(client, auth_headers):
    r = client.post("/api/wakerflow/preview", headers=auth_headers,
                    json={"name": "x", "blocks": [{"id": "s1", "type": "???"}]})
    assert r.status_code == 400


def test_preview_public(client):
    r = client.post("/api/wakerflow/preview", json={"name": "x", "yaml": "name: x\nsteps: []\n"})
    assert r.status_code == 200


# ============================================
# P2-21：canvas 建块的脏 schedule 在保存入口 4xx
# ============================================
def test_create_blocks_dirty_schedule_400(client, auth_headers):
    """schedule_type:"weekly" 此前原样入库 → tick 每次 parse 失败，flow 永久哑火。"""
    r = client.post(
        "/api/wakerflow/items", headers=auth_headers,
        json={"name": "dirty_sched", "blocks": [], "schedule_type": "weekly"},
    )
    assert r.status_code == 400
    assert "schedule" in r.json()["detail"]


def test_create_blocks_negative_interval_400(client, auth_headers):
    r = client.post(
        "/api/wakerflow/items", headers=auth_headers,
        json={"name": "dirty_int", "blocks": [],
              "schedule_type": "interval", "interval_minutes": -5},
    )
    assert r.status_code == 400


def test_update_blocks_dirty_schedule_400(client, auth_headers):
    _create_flow(client, auth_headers, name="demo_flow")
    r = client.put(
        "/api/wakerflow/items/demo_flow", headers=auth_headers,
        json={"blocks": [], "schedule_type": "weekly"},
    )
    assert r.status_code == 400


def test_create_blocks_valid_schedule_ok(client, auth_headers):
    r = client.post(
        "/api/wakerflow/items", headers=auth_headers,
        json={"name": "sched_ok", "blocks": [],
              "schedule_type": "interval", "interval_minutes": 30},
    )
    assert r.status_code == 200, r.text


# ============================================
# P3-4：run_id 路径校验（status 端点走 store.run_jsonl_path）
# ============================================
def test_run_status_rejects_traversal_run_id(client, auth_headers):
    """run_id 带反斜杠/点穿越字符 → 400，不再用其拼接 jsonl 路径。"""
    _create_flow(client, auth_headers, name="demo_flow")
    r = client.get(
        "/api/wakerflow/items/demo_flow/runs/..%5C..%5Cevil/status",
        headers=auth_headers,
    )
    assert r.status_code == 400


# ============================================
# 前端审查(高)：markdown 渲染消毒（mdSafe）前端 JS 回归
# （内联 JS 已外置到 static/js/wakerflow.js，守卫随之迁移）
# ============================================
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_JS = _PROJECT_ROOT / "web_fastapi" / "static" / "js" / "wakerflow.js"


def _frontend_js_text() -> str:
    return _FRONTEND_JS.read_text(encoding="utf-8")


def test_wakerflow_js_md_render_all_through_mdsafe():
    """三处 markdown 渲染点（审批上下文/returns/节点产出）全部过 mdSafe；
    全部前端 JS 里 marked.parse 只允许出现在 mdSafe 函数体内——绕过消毒
    管线的裸 marked.parse(esc(...)) 是 XSS 回归（markdown 链接语法可生成
    javascript: URL，esc 拦不住括号内的 scheme 文本）。"""
    tpl = _frontend_js_text()
    # 唯一的 marked.parse 调用必须在 mdSafe 内部
    assert tpl.count("marked.parse(") == 1, "marked.parse 必须收敛在 mdSafe 内"
    md_safe_start = tpl.index("function mdSafe(")
    md_safe_end = tpl.index("function showValidation", md_safe_start)
    md_safe_body = tpl[md_safe_start:md_safe_end]
    assert "marked.parse(" in md_safe_body, "marked.parse 应在 mdSafe 内"
    # 三处渲染点：审批上下文 c.result / returns 值 v / 节点产出 n.result
    assert "${mdSafe(c.result)}" in tpl
    assert "${mdSafe(v)}" in tpl
    assert "${mdSafe(n.result)}" in tpl
    # 旧写法（marked 结果直插模板字符串）不得复活——渲染调用只能经 mdSafe
    assert "${marked.parse" not in tpl
    assert "${typeof marked" not in tpl
    # 消毒谓词是 http/https/mailto 白名单 + new URL 协议判定（DOM 方案）
    assert "function mdLinkAllowed(" in tpl
    assert "mailto:" in tpl
    assert "new URL(" in tpl
    # 渲染后挂 detached div 遍历 a[href]，危险目标中和为 href="#"
    assert "document.createElement('div')" in tpl
    assert "querySelectorAll('a[href]')" in tpl
    assert 'setAttribute(\'href\', \'#\')' in tpl


def _node_available() -> bool:
    import shutil
    return shutil.which("node") is not None


# node 行为测试脚本：从前端 JS（static/js/wakerflow.js）提取
# esc/mdLinkAllowed/mdSafe 实测
# ① 谓词白名单（危险 scheme 全拒 / 合法与相对链接放行）
# ② mdSafe 对 marked 桩输出的中和行为（含 jav&#x09;ascript: 实体混淆）
# ③ marked 未加载时的 <pre> 转义回退
_NODE_MDSAFE_SCRIPT = r'''
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf-8');
function extractFn(name) {
  const marker = 'function ' + name + '(';
  const start = src.indexOf(marker);
  if (start < 0) throw new Error('function ' + name + ' not found');
  let i = src.indexOf('{', start), depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) break; }
  }
  return src.slice(start, i + 1);
}
globalThis.location = { href: 'https://hermes.local/wakerflow' };
eval(extractFn('esc'));
eval(extractFn('mdLinkAllowed'));
eval(extractFn('mdSafe'));
const mustBlock = [
  'javascript:alert(1)', 'JavaScript:alert(1)',
  'jav\tascript:alert(1)', 'jav\nascript:alert(1)', ' \tjavascript:alert(1)',
  'vbscript:msgbox(1)',
  'data:text/html;base64,PHNjcmlwdD4=', 'DATA:text/html,<b>',
  'file:///C:/x', 'ftp://e.com/x', 'chrome://settings',
];
for (const href of mustBlock)
  if (mdLinkAllowed(href)) { console.error('should-block ' + JSON.stringify(href)); process.exit(1); }
const mustAllow = [
  'https://example.com/a', 'http://example.com/', 'HTTPS://example.com/',
  'mailto:a@b.com', '/rel/path', 'other', '#anchor', '//host.com/p',
];
for (const href of mustAllow)
  if (!mdLinkAllowed(href)) { console.error('should-allow ' + JSON.stringify(href)); process.exit(1); }
function decodeEntities(s) {
  return s.replace(/&#x([0-9a-fA-F]+);/g, (_, h) => String.fromCodePoint(parseInt(h, 16)))
          .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(parseInt(d, 10)))
          .replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
          .replace(/&#39;/g, "'").replace(/&amp;/g, '&');
}
function shimElement() {
  const el = { _html: '', _anchors: [] };
  Object.defineProperty(el, 'innerHTML', {
    set(v) {
      el._html = String(v); el._anchors = [];
      const re = /<a\b([^>]*)>/g; let m;
      while ((m = re.exec(el._html)) !== null) {
        const attrs = {}, order = [];
        const are = /([a-zA-Z:-]+)\s*=\s*"([^"]*)"/g; let am;
        while ((am = are.exec(m[1])) !== null) {
          const n = am[1].toLowerCase();
          if (!(n in attrs)) order.push(n);
          attrs[n] = am[2];
        }
        el._anchors.push({ span: [m.index, m.index + m[0].length], attrs, order });
      }
    },
    get() {
      let out = el._html;
      for (const a of [...el._anchors].reverse()) {
        const tag = '<a ' + a.order.map(n => n + '="' + a.attrs[n] + '"').join(' ') + '>';
        out = out.slice(0, a.span[0]) + tag + out.slice(a.span[1]);
      }
      return out;
    },
  });
  el.querySelectorAll = sel => {
    if (sel !== 'a[href]') return [];
    return el._anchors.map(a => ({
      getAttribute(n) { n = n.toLowerCase(); return n in a.attrs ? decodeEntities(a.attrs[n]) : null; },
      setAttribute(n, v) { n = n.toLowerCase(); if (!(n in a.attrs)) a.order.push(n); a.attrs[n] = String(v).replace(/"/g, '&quot;'); },
      removeAttribute(n) { n = n.toLowerCase(); delete a.attrs[n]; a.order = a.order.filter(x => x !== n); },
    }));
  };
  return el;
}
globalThis.document = { createElement: () => shimElement() };
globalThis.marked = {
  parse: t => t === '__PAYLOAD__'
    ? '<p><a href="javascript:alert(1)">a</a> <a href="https://ok.e/x">b</a>'
      + ' <a href="jav&#x09;ascript:alert(2)">c</a> <a href="mailto:a@b.com">d</a></p>'
    : '<p>plain</p>',
};
const out = mdSafe('__PAYLOAD__');
if ((out.match(/href="#"/g) || []).length !== 2) { console.error('neutralized=' + out); process.exit(1); }
if (!out.includes('href="https://ok.e/x"') || !out.includes('href="mailto:a@b.com"')) {
  console.error('legal link clobbered: ' + out); process.exit(1);
}
globalThis.marked = undefined;
const fb = mdSafe('<script>x</script> & [a](javascript:1)');
if (!fb.includes('&lt;script&gt;') || fb.includes('<a ')) { console.error('fallback: ' + fb); process.exit(1); }
console.log('MDSAFE_NODE_ALL_PASS');
'''


@pytest.mark.skipif(not _node_available(), reason="node 不可用")
def test_wakerflow_js_mdsafe_behavior_via_node(tmp_path):
    """node 实测前端 JS 里 mdSafe/mdLinkAllowed：危险 scheme（含实体混淆
    jav&#x09;ascript:、大小写、\t\r\n 混入）全中和为 href="#"，合法
    http/https/mailto/相对链接原样保留；marked 未加载时走 <pre> 转义回退。"""
    import subprocess
    script_path = tmp_path / "mdsafe_check.cjs"  # .cjs：免受临时目录上层 package.json 的 ESM 声明影响
    script_path.write_text(_NODE_MDSAFE_SCRIPT, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script_path), str(_FRONTEND_JS)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"node 校验失败:\n{proc.stdout}\n{proc.stderr}"
    assert "MDSAFE_NODE_ALL_PASS" in proc.stdout


@pytest.mark.skipif(not _node_available(), reason="node 不可用")
def test_wakerflow_js_node_syntax_check():
    """外置 JS 整文件过 node --check：编辑后仍是无语法错误的合法 JS。"""
    import subprocess
    proc = subprocess.run(
        ["node", "--check", str(_FRONTEND_JS)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"node --check 失败:\n{proc.stderr}"


# ============================================
# 审批取消 + flow name 校验收口 + 审批卡 XSS 守卫
# （自 tests/test_wakerflow_api.py 并入：真 FlowRunner + tmp workspace 的
# env/client fixtures 与上方 FakeFlowRunner 版本不兼容，故此组自带
# cancel_env / cancel_client，命名区分两套替身。）
# ============================================
_APPROVAL_FLOW_YAML = """
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


def _wait_until(cond, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def _fake_http(monkeypatch):
    fake_resp = type("R", (), {
        "getcode": lambda self: 200,
        "read": lambda self: b'{"ok": true}',
        "__enter__": lambda self: self,
        "__exit__": lambda self, *a: None,
    })()
    monkeypatch.setattr(_urlreq, "urlopen", lambda *a, **kw: fake_resp)


@pytest.fixture
def cancel_env(tmp_path):
    """真实 FlowRunner + 挂 wakerflow router 的精简 app（workspace 全在 tmp）。"""
    ws = tmp_path / "ws"
    store = FlowStore("u1", workspace_root=str(ws))
    store.save("ask-flow", _APPROVAL_FLOW_YAML)
    prov = SQLiteProvider(db_path=tmp_path / "kv.db")
    runner = FlowRunner(workspace_root=str(ws), max_concurrent=2, storage=prov)
    runner.start()

    a = FastAPI()
    a.include_router(wakerflow_router.router, prefix="/api/wakerflow")
    a.state.flow_runner = runner
    # router._store 优先读 waker_scheduler._workspace_root
    a.state.waker_scheduler = SimpleNamespace(_workspace_root=str(ws))
    yield SimpleNamespace(app=a, store=store, runner=runner, ws=ws, prov=prov)
    runner.shutdown()
    prov.close()


@pytest.fixture
def cancel_client(cancel_env):
    return TestClient(cancel_env.app)


# --------------------------------------------
# POST /approvals/{run_id}/cancel（第二轮审查·缺陷回归）
# --------------------------------------------
def test_cancel_endpoint_converges_suspended_flow(cancel_env, cancel_client, monkeypatch):
    """API 取消：审批文件被写 cancelled → 看护提前唤醒 → 挂起 flow 收敛为
    取消终态（failed + finished_at），on_done 触发（防重入键随终态释放，
    该 flow 不再被占键卡到 24h 超时）。"""
    _fake_http(monkeypatch)
    done = threading.Event()
    run_id = cancel_env.runner.submit("u1", "ask-flow", {}, on_done=done.set)

    # ① 挂起：waiting_approval + pending 审批文件
    assert _wait_until(
        lambda: (cancel_env.runner.get_status(run_id) or {}).get("status") == "waiting_approval"
    ), cancel_env.runner.get_status(run_id)
    apath = cancel_env.store.approval_path(run_id)
    assert apath.exists()
    assert json.loads(apath.read_text(encoding="utf-8"))["status"] == "pending"
    assert not done.is_set()

    # ② 走 API 取消
    r = cancel_client.post(f"/api/wakerflow/approvals/{run_id}/cancel")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "run_id": run_id, "status": "cancelled"}
    assert json.loads(apath.read_text(encoding="utf-8"))["status"] == "cancelled"

    # ③ 收敛为取消终态：failed + finished_at，on_done 触发，审批文件已消费
    assert _wait_until(
        lambda: (cancel_env.runner.get_status(run_id) or {}).get("status") == "failed"
    ), cancel_env.runner.get_status(run_id)
    rec = cancel_env.runner.get_status(run_id)
    assert rec["finished_at"], rec
    assert done.wait(timeout=5), "取消终态后 on_done（防重入键释放）未触发"
    assert not apath.exists()


def test_cancel_unknown_run_returns_404(cancel_client):
    """无 pending 审批文件 → 404。"""
    r = cancel_client.post("/api/wakerflow/approvals/no-such-run/cancel")
    assert r.status_code == 404


def test_cancel_non_pending_approval_returns_404(cancel_env, cancel_client):
    """审批文件已 answered（非 pending）→ 404，不重复改写。"""
    apath = cancel_env.store.approval_path("runX")
    apath.parent.mkdir(parents=True, exist_ok=True)
    apath.write_text(json.dumps({"status": "answered"}), encoding="utf-8")
    r = cancel_client.post("/api/wakerflow/approvals/runX/cancel")
    assert r.status_code == 404
    assert json.loads(apath.read_text(encoding="utf-8"))["status"] == "answered"


def test_cancel_without_runner_returns_503():
    """flow_runner 未挂载（启动失败）→ 503。"""
    a = FastAPI()
    a.include_router(wakerflow_router.router, prefix="/api/wakerflow")
    c = TestClient(a)
    assert c.post("/api/wakerflow/approvals/abc/cancel").status_code == 503


# --------------------------------------------
# flow name 字符集校验收敛在 FlowStore.save（创建路径回归）
# --------------------------------------------
def test_create_blocks_injection_name_rejected(cancel_client):
    """blocks 模式带注入名 → 400。此前 canvas 绕过 parser 直接入库，
    前端卡片按钮 onclick 拼接的注入可达；现收口在 FlowStore.save。"""
    r = cancel_client.post("/api/wakerflow/items", json={
        "name": "x');alert(1)#",
        "blocks": [{"type": "worker", "id": "s1", "waker": "w", "task": "t"}],
    })
    assert r.status_code == 400
    assert "非法 flow name" in r.json()["detail"]


def test_create_yaml_top_level_name_mismatch_rejected(cancel_client):
    """yaml 内层 name 合法但顶层 body.name 非法 → 400（parse_flow 只
    校验 yaml 内层 name，顶层 name 的唯一闸门是 store.save）。"""
    r = cancel_client.post("/api/wakerflow/items", json={
        "name": "bad`name",
        "yaml": "name: inner-ok\nsteps: []\n",
    })
    assert r.status_code == 400
    assert "非法 flow name" in r.json()["detail"]


def test_create_legal_names_still_ok(cancel_client, cancel_env):
    """合法名称：blocks 创建照常 200 且落盘。"""
    r = cancel_client.post("/api/wakerflow/items", json={
        "name": "blocks-made",
        "blocks": [{"type": "worker", "id": "s1", "waker": "w", "task": "t"}],
    })
    assert r.status_code == 200, r.text
    assert cancel_env.store.get("blocks-made") is not None


# --------------------------------------------
# 前端守卫回归自证（审批弹窗：data-* + 事件委托，禁 onclick 拼接）
# （内联 JS 已外置到 static/js/wakerflow.js，守卫随之迁移）
# --------------------------------------------
def _approval_card_region(tpl: str) -> str:
    """审批卡片渲染区（showApprovals 定义 → closeApproval 定义）。"""
    start = tpl.index("async function showApprovals()")
    end = tpl.index("function closeApproval()", start)
    return tpl[start:end]


def test_template_approval_cards_use_data_attrs_no_onclick():
    """审批卡片区域禁用 onclick（含字符串拼接）；run_id/answer 走 data-*，
    取消按钮由既有事件委托统一分发（参照 P2-23 安全写法，无拼接回归）。
    （审批区 JS 随 P2-5 外置到 static/js/wakerflow.js，此处锁外置文件。）"""
    tpl = _frontend_js_text()
    region = _approval_card_region(tpl)
    assert "onclick" not in region, "审批卡片渲染区出现 onclick（拼接回归）"
    # 取消按钮：run_id 走 data-cancel-run 属性（纯数据，不进 JS 编译器）
    assert 'data-cancel-run="${esc(a.run_id)}"' in region
    # 既有审批按钮的 data-* 写法未被破坏
    assert 'data-run="${esc(a.run_id)}"' in region
    assert 'data-answer="${esc(o.value)}"' in region
    # 取消走同一个一次性注册的事件委托（#approval-list 在静态 DOM）
    assert "document.getElementById('approval-list').addEventListener('click'" in tpl
    assert "cancelApproval(cancelBtn.dataset.cancelRun)" in tpl
    # cancelApproval 调取消端点（与路由同语义路径）
    assert "encodeURIComponent(runId) + '/cancel'" in tpl
