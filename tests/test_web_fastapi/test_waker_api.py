"""Waker 管理 API（/api/waker）测试。

不启动完整 create_app（那会 fork worker 子进程），而是构造一个仅挂 waker
router 的精简 FastAPI app，用 fastapi.testclient.TestClient 打。

覆盖：list / create / get / update / set_enabled / delete / runs 列表+详情 / result / invoke。
invoke 用 FakeScheduler 记录 submit_now 调用，不真跑。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.constants import LOCAL_USER
from src.waker.store import WakerStore
from web_fastapi.routers import waker as waker_router


# ============================================
# FakeScheduler：记录 submit_now 调用，不真跑
# ============================================
class FakeScheduler:
    """替身 WakerScheduler。workspace_root 指向 tmp；submit_now 只记录。"""

    def __init__(self, workspace_root: str):
        self._workspace_root = workspace_root
        self.calls = []  # [(uid, name, prompt), ...]
        self._next_run_id = 0

    def submit_now(self, user_id, name, api_prompt=None):
        self._next_run_id += 1
        rid = f"fake-run-{self._next_run_id}"
        self.calls.append((user_id, name, api_prompt))
        return rid


# ============================================
# fixtures
# ============================================
@pytest.fixture
def app(tmp_path):
    """精简 app：只挂 waker router；state 注入 FakeScheduler 指向 tmp_path。"""
    a = FastAPI()
    a.include_router(waker_router.router, prefix="/api/waker", tags=["waker"])
    a.state.waker_scheduler = FakeScheduler(str(tmp_path))
    # 单用户：登录 cookie 是唯一鉴权来源（身份恒 LOCAL_USER）
    # worker_manager 不应被 waker router 触达（CRUD 走文件 IO），不挂
    return a


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
def _create_waker(client, auth_headers, name="w1", **extra):
    body = {"name": name}
    body.update(extra)
    r = client.post("/api/waker/items", headers=auth_headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ============================================
# create / get / list
# ============================================
def test_create_returns_plain_token(client, auth_headers):
    j = _create_waker(client, auth_headers, name="w1", description="hello",
                      identity="职责", persona="风格", bible="准则")
    assert j["ok"] is True
    w = j["waker"]
    assert w["name"] == "w1"
    assert w["description"] == "hello"
    # api_token 明文返回（仅创建这一次）
    assert w["api_token"] and len(w["api_token"]) > 8
    assert "****" not in w["api_token"]
    # 人格文本回填（store 写盘会补 \n，这里只校验内容包含）
    assert w["identity"].strip() == "职责"
    assert w["persona"].strip() == "风格"
    assert w["bible"].strip() == "准则"
    # 创建默认 enabled=False
    assert w["enabled"] is False


def test_get_masks_token(client, auth_headers):
    plain = _create_waker(client, auth_headers, name="w1")["waker"]["api_token"]
    r = client.get("/api/waker/items/w1", headers=auth_headers)
    assert r.status_code == 200
    w = r.json()["waker"]
    # 掩码：露前4后4
    assert w["api_token"] == f"{plain[:4]}****{plain[-4:]}"
    assert w["name"] == "w1"


def test_get_404(client, auth_headers):
    r = client.get("/api/waker/items/nope", headers=auth_headers)
    assert r.status_code == 404


def test_list(client, auth_headers):
    _create_waker(client, auth_headers, name="a")
    _create_waker(client, auth_headers, name="b")
    r = client.get("/api/waker/items", headers=auth_headers)
    assert r.status_code == 200
    arr = r.json()["wakers"]
    assert sorted(w["name"] for w in arr) == ["a", "b"]
    # 列表项 api_token 都被掩码
    for w in arr:
        assert "****" in w["api_token"] or w["api_token"] == "****" or len(w["api_token"]) <= 8


def test_create_duplicate_400(client, auth_headers):
    _create_waker(client, auth_headers, name="w1")
    r = client.post("/api/waker/items", headers=auth_headers, json={"name": "w1"})
    assert r.status_code == 400


def test_create_invalid_name_400(client, auth_headers):
    # 名字带空格 → 非法
    r = client.post("/api/waker/items", headers=auth_headers, json={"name": "has space"})
    assert r.status_code == 400


def test_create_invalid_schedule_400(client, auth_headers):
    r = client.post(
        "/api/waker/items", headers=auth_headers,
        json={"name": "w1", "schedule_type": "weekly"},
    )
    assert r.status_code == 400


def test_items_public_without_headers(client):
    """免认证：匿名列表可见。"""
    r = client.get("/api/waker/items")
    assert r.status_code == 200


# ============================================
# update / set_enabled / delete
# ============================================
def test_update(client, auth_headers):
    _create_waker(client, auth_headers, name="w1")
    r = client.put(
        "/api/waker/items/w1", headers=auth_headers,
        json={"description": "changed", "task_prompt": "新任务",
              "identity": "新职责", "persona": "新风格"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # 校验落盘
    got = client.get("/api/waker/items/w1", headers=auth_headers).json()["waker"]
    assert got["description"] == "changed"
    assert got["task_prompt"] == "新任务"
    assert got["identity"].strip() == "新职责"
    assert got["persona"].strip() == "新风格"


def test_update_404(client, auth_headers):
    r = client.put("/api/waker/items/nope", headers=auth_headers, json={"description": "x"})
    assert r.status_code == 404


# ============================================
# P2-25 / P1-7：update 全字段校验 + 调度字段校验
# ============================================
def test_update_invalid_schedule_type_400(client, auth_headers):
    """P2-25：PUT 此前跳过校验，schedule_type:"weekly" 静默落盘成永不调度。"""
    _create_waker(client, auth_headers, name="w1")
    r = client.put(
        "/api/waker/items/w1", headers=auth_headers,
        json={"schedule_type": "weekly"},
    )
    assert r.status_code == 400
    # 盘上未被污染
    got = client.get("/api/waker/items/w1", headers=auth_headers).json()["waker"]
    assert got["schedule_type"] == "interval"


def test_update_zero_interval_400(client, auth_headers):
    _create_waker(client, auth_headers, name="w1")
    r = client.put(
        "/api/waker/items/w1", headers=auth_headers,
        json={"schedule_type": "interval", "interval_minutes": 0},
    )
    assert r.status_code == 400


def test_update_aware_expire_at_400(client, auth_headers):
    """P1-7：带时区的 expire_at 在更新入口 400 拒绝。"""
    _create_waker(client, auth_headers, name="w1")
    r = client.put(
        "/api/waker/items/w1", headers=auth_headers,
        json={"expire_at": "2026-01-01T00:00:00+08:00"},
    )
    assert r.status_code == 400


def test_create_aware_expire_at_400(client, auth_headers):
    r = client.post(
        "/api/waker/items", headers=auth_headers,
        json={"name": "w2", "expire_at": "2026-01-01T00:00:00+08:00"},
    )
    assert r.status_code == 400


def test_update_valid_schedule_ok(client, auth_headers):
    _create_waker(client, auth_headers, name="w1")
    r = client.put(
        "/api/waker/items/w1", headers=auth_headers,
        json={"schedule_type": "daily", "daily_at": "07:30"},
    )
    assert r.status_code == 200, r.text
    got = client.get("/api/waker/items/w1", headers=auth_headers).json()["waker"]
    assert got["schedule_type"] == "daily"
    assert got["daily_at"] == "07:30"


def test_set_enabled(client, auth_headers):
    _create_waker(client, auth_headers, name="w1")
    assert client.get("/api/waker/items/w1", headers=auth_headers).json()["waker"]["enabled"] is False
    r = client.patch("/api/waker/items/w1/enabled", headers=auth_headers, json={"enabled": True})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert client.get("/api/waker/items/w1", headers=auth_headers).json()["waker"]["enabled"] is True
    # 再关
    r = client.patch("/api/waker/items/w1/enabled", headers=auth_headers, json={"enabled": False})
    assert r.status_code == 200
    assert client.get("/api/waker/items/w1", headers=auth_headers).json()["waker"]["enabled"] is False


def test_set_enabled_404(client, auth_headers):
    r = client.patch("/api/waker/items/nope/enabled", headers=auth_headers, json={"enabled": True})
    assert r.status_code == 404


def test_delete(client, auth_headers):
    _create_waker(client, auth_headers, name="w1")
    r = client.delete("/api/waker/items/w1", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # 再查 404
    assert client.get("/api/waker/items/w1", headers=auth_headers).status_code == 404
    # 二次删 404
    assert client.delete("/api/waker/items/w1", headers=auth_headers).status_code == 404


# ============================================
# runs / result
# ============================================
def _write_run(run_dir: Path, run_id: str, events: list, mtime_ns: int | None = None):
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
    _create_waker(client, auth_headers, name="w1")
    r = client.get("/api/waker/items/w1/runs", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["runs"] == []


def test_runs_lists_newest_first(client, app, auth_headers):
    _create_waker(client, auth_headers, name="w1")
    run_dir = Path(app.state.waker_scheduler._workspace_root) / "wakers" / "w1" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    import time
    now = time.time_ns()
    _write_run(run_dir, "old-run", [
        {"type": "run_start", "run_id": "old-run", "ts": "2026-09-07T08:00:00"},
        {"type": "run_end", "run_id": "old-run", "status": "ok", "ts": "2026-09-07T08:01:00"},
    ], mtime_ns=now - 2_000_000_000)
    _write_run(run_dir, "new-run", [
        {"type": "run_start", "run_id": "new-run", "ts": "2026-09-08T13:53:28"},
        {"type": "complete", "content": "交付正文"},
        {"type": "run_end", "run_id": "new-run", "status": "error", "ts": "2026-09-08T13:57:03"},
    ], mtime_ns=now)

    r = client.get("/api/waker/items/w1/runs", headers=auth_headers)
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert [x["run_id"] for x in runs] == ["new-run", "old-run"]
    assert runs[0]["status"] == "error"
    assert runs[0]["started_at"] == "2026-09-08T13:53:28"
    assert runs[0]["ended_at"] == "2026-09-08T13:57:03"
    assert runs[0]["duration_s"] == 215
    assert runs[1]["status"] == "ok"
    assert runs[1]["duration_s"] == 60


def test_runs_limit(client, app, auth_headers):
    _create_waker(client, auth_headers, name="w1")
    run_dir = Path(app.state.waker_scheduler._workspace_root) / "wakers" / "w1" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    import time
    base = time.time_ns()
    for i in range(5):
        _write_run(run_dir, f"r{i}", [
            {"type": "run_start", "ts": "2026-09-08T08:00:00"},
            {"type": "run_end", "status": "ok", "ts": "2026-09-08T08:00:01"},
        ], mtime_ns=base + i * 1_000_000)
    r = client.get("/api/waker/items/w1/runs?limit=3", headers=auth_headers)
    assert r.status_code == 200
    assert len(r.json()["runs"]) == 3


def test_runs_interrupted_when_no_end(client, app, auth_headers):
    """无 run_end 且不在 active 表 → interrupted（半态可见）。"""
    _create_waker(client, auth_headers, name="w1")
    run_dir = Path(app.state.waker_scheduler._workspace_root) / "wakers" / "w1" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_run(run_dir, "20260908T135328-abc", [
        {"type": "run_start", "run_id": "20260908T135328-abc", "ts": "2026-09-08T13:53:28"},
        {"type": "token"},
    ])
    r = client.get("/api/waker/items/w1/runs", headers=auth_headers)
    assert r.status_code == 200
    rec = r.json()["runs"][0]
    assert rec["status"] == "interrupted"
    assert rec["started_at"] == "2026-09-08T13:53:28"
    assert rec["ended_at"] == ""
    assert rec["duration_s"] is None


def test_runs_running_when_active(client, app, auth_headers):
    _create_waker(client, auth_headers, name="w1")
    run_dir = Path(app.state.waker_scheduler._workspace_root) / "wakers" / "w1" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_run(run_dir, "live-run", [
        {"type": "run_start", "run_id": "live-run", "ts": "2026-09-08T13:53:28"},
    ])
    app.state.waker_async_runner = type("AR", (), {
        "list_active": staticmethod(lambda uid: [{"waker_name": "w1", "run_id": "live-run"}]),
    })()
    r = client.get("/api/waker/items/w1/runs", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["runs"][0]["status"] == "running"


def test_runs_404_unknown_waker(client, auth_headers):
    r = client.get("/api/waker/items/nope/runs", headers=auth_headers)
    assert r.status_code == 404


def test_run_detail_extracts_result_and_skips_tokens(client, app, auth_headers):
    _create_waker(client, auth_headers, name="w1")
    run_dir = Path(app.state.waker_scheduler._workspace_root) / "wakers" / "w1" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_run(run_dir, "r-ok", [
        {"type": "run_start", "run_id": "r-ok", "ts": "2026-09-08T13:53:28"},
        {"type": "token", "d": "x"},
        {"type": "reasoning_token"},
        {"type": "tool_call", "name": "mcp-ddg"},
        {"type": "tool_start", "tool_name": "ls", "tool_args": {"path": "/papers"}},
        {"type": "tool_end", "tool_name": "ls", "result": "/papers\nllm/"},
        "not-a-json-line",
        {"type": "complete", "content": "# 交付\n正文"},
        {"type": "run_end", "run_id": "r-ok", "status": "ok", "ts": "2026-09-08T13:57:03"},
    ])
    r = client.get("/api/waker/items/w1/runs/r-ok", headers=auth_headers)
    assert r.status_code == 200
    j = r.json()
    assert j["run_id"] == "r-ok"
    assert j["status"] == "ok"
    assert j["result"] == "# 交付\n正文"
    assert j["token_count"] == 2
    assert j["tool_calls"] == ["mcp-ddg", "ls"]
    types = [e["type"] for e in j["events"]]
    assert "token" not in types
    assert "reasoning_token" not in types
    assert "tool_end" not in types  # 回包太大，事件流只留 tool_start
    assert types == ["run_start", "tool_call", "tool_start", "complete", "run_end"]
    ls_ev = next(e for e in j["events"] if e["type"] == "tool_start")
    assert ls_ev["tool_name"] == "ls"
    assert ls_ev["summary"] == "path=/papers"
    assert "content" not in next(e for e in j["events"] if e["type"] == "complete")
    assert j["duration_s"] == 215


def test_run_detail_404_unknown_run(client, auth_headers):
    _create_waker(client, auth_headers, name="w1")
    r = client.get("/api/waker/items/w1/runs/nope", headers=auth_headers)
    assert r.status_code == 404


def test_run_detail_rejects_traversal_run_id(client, auth_headers):
    _create_waker(client, auth_headers, name="w1")
    r = client.get(
        "/api/waker/items/w1/runs/..%5C..%5Cevil",
        headers=auth_headers,
    )
    assert r.status_code == 400


def test_run_detail_404_unknown_waker(client, auth_headers):
    r = client.get("/api/waker/items/nope/runs/r1", headers=auth_headers)
    assert r.status_code == 404


def test_result_empty_when_missing(client, auth_headers):
    _create_waker(client, auth_headers, name="w1")
    r = client.get("/api/waker/items/w1/result", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["result"] == ""


def test_result_reads_md(client, app, auth_headers):
    _create_waker(client, auth_headers, name="w1")
    wdir = Path(app.state.waker_scheduler._workspace_root) / "wakers" / "w1"
    (wdir / "latest_result.md").write_text("# 结果\n摘要内容", encoding="utf-8")
    r = client.get("/api/waker/items/w1/result", headers=auth_headers)
    assert r.status_code == 200
    assert "摘要内容" in r.json()["result"]


def test_result_404_unknown_waker(client, auth_headers):
    r = client.get("/api/waker/items/nope/result", headers=auth_headers)
    assert r.status_code == 404


# ============================================
# invoke
# ============================================
def test_invoke_calls_submit_now(client, auth_headers):
    _create_waker(client, auth_headers, name="w1")
    r = client.post("/api/waker/items/w1/invoke", headers=auth_headers, json={"prompt": "go"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert j["run_id"].startswith("fake-run-")
    sched = client.app.state.waker_scheduler
    assert sched.calls == [(LOCAL_USER, "w1", "go")]


def test_invoke_no_prompt(client, auth_headers):
    _create_waker(client, auth_headers, name="w1")
    r = client.post("/api/waker/items/w1/invoke", headers=auth_headers, json={})
    assert r.status_code == 200
    sched = client.app.state.waker_scheduler
    assert sched.calls[-1] == (LOCAL_USER, "w1", None)


def test_invoke_404_unknown_waker(client, auth_headers):
    r = client.post("/api/waker/items/nope/invoke", headers=auth_headers, json={})
    assert r.status_code == 404


def test_invoke_503_when_scheduler_missing(auth_headers, tmp_path):
    """scheduler / async_runner 都未挂载 → 503。

    workspace 必须走 tmp：scheduler=None 时若再回落到 agent_home，
    POST create 会把测试用的 w1 写进真实 data/home/wakers/（重启列表里就会多一张空卡片）。
    """
    a = FastAPI()
    a.include_router(waker_router.router, prefix="/api/waker", tags=["waker"])
    a.state.waker_scheduler = None
    a.state.waker_async_runner = None
    a.state.workspace_root = str(tmp_path)
    c = TestClient(a)
    r = c.post("/api/waker/items", headers=auth_headers, json={"name": "w1"})
    assert r.status_code == 200, r.text
    assert (tmp_path / "wakers" / "w1" / "waker.yaml").is_file()
    r = c.post("/api/waker/items/w1/invoke", headers=auth_headers, json={})
    assert r.status_code == 503


# ============================================
# list 逾期标记（overdue）
# ============================================
def _set_state(app, name, **fields):
    """直改 waker.yaml 的 state 节（next_run_at 等由 scheduler 写，API 不暴露）。"""
    ws = app.state.waker_scheduler._workspace_root
    store = WakerStore(LOCAL_USER, workspace_root=ws)
    cfg = store.get(name)
    for k, v in fields.items():
        setattr(cfg, k, v)
    store.save_state(cfg)


def _list_item(client, auth_headers, name):
    arr = client.get("/api/waker/items", headers=auth_headers).json()["wakers"]
    return next(w for w in arr if w["name"] == name)


def test_overdue_false_when_disabled(client, app, auth_headers):
    """未启用：即使 next_run_at 已远超容差也不标逾期。"""
    _create_waker(client, auth_headers, name="w1")
    _set_state(app, "w1", next_run_at=(datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"))
    assert _list_item(client, auth_headers, "w1")["overdue"] is False


def test_overdue_true_when_past_beyond_tolerance(client, app, auth_headers):
    """enabled 且 next_run_at 已过 1 小时（> 120s 容差）→ True。"""
    _create_waker(client, auth_headers, name="w1")
    client.patch("/api/waker/items/w1/enabled", headers=auth_headers, json={"enabled": True})
    _set_state(app, "w1", next_run_at=(datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"))
    assert _list_item(client, auth_headers, "w1")["overdue"] is True


def test_overdue_false_within_tolerance(client, app, auth_headers):
    """next_run_at 刚过 30s（< 120s 容差）：消化手动 invoke 不刷新 next_run_at → False。"""
    _create_waker(client, auth_headers, name="w1")
    client.patch("/api/waker/items/w1/enabled", headers=auth_headers, json={"enabled": True})
    _set_state(app, "w1", next_run_at=(datetime.now() - timedelta(seconds=30)).isoformat(timespec="seconds"))
    assert _list_item(client, auth_headers, "w1")["overdue"] is False


def test_overdue_false_when_running(client, app, auth_headers):
    """正在运行（active_run_id / running）：不标逾期。"""
    _create_waker(client, auth_headers, name="w1")
    client.patch("/api/waker/items/w1/enabled", headers=auth_headers, json={"enabled": True})
    _set_state(app, "w1", next_run_at=(datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"))
    app.state.waker_async_runner = type("AR", (), {
        "list_active": staticmethod(lambda uid: [{"waker_name": "w1", "run_id": "live-run"}]),
    })()
    item = _list_item(client, auth_headers, "w1")
    assert item["overdue"] is False
    assert item["last_status"] == "running"


def test_overdue_false_when_next_run_at_malformed(client, app, auth_headers):
    """next_run_at 解析失败（坏串）：不抛错、按未逾期处理。"""
    _create_waker(client, auth_headers, name="w1")
    client.patch("/api/waker/items/w1/enabled", headers=auth_headers, json={"enabled": True})
    _set_state(app, "w1", next_run_at="not-a-date")
    assert _list_item(client, auth_headers, "w1")["overdue"] is False
