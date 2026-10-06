"""git clone 任务 + /api/runs 账本（ADR-0003）行为锁定。

克隆命令经 project_tasks.build_clone_cmd 注入替身（python 子进程），
不依赖真实 git；覆盖成功 / 失败 / 超时 / 非法 URL 四路径与账本可见性。
"""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.storage import paths
from src.storage.projects_store import ProjectStore
from src.storage.run_registry import RunRegistry
from src.storage.sqlite_provider import SQLiteProvider
from web_fastapi import project_tasks
from web_fastapi.dependencies import get_current_user_id
from web_fastapi.routers import projects as projects_router
from web_fastapi.routers import runs as runs_router


class _StubCtx:
    def __init__(self, storage):
        self._s = storage

    def try_get(self, name):
        return self._s if name == "storage" else None


@pytest.fixture
def api(tmp_path):
    paths.set_data_root(tmp_path / "data")
    provider = SQLiteProvider(db_path=tmp_path / "data" / "p.db")
    spaces = paths.data_dir("projects", "spaces")
    spaces.mkdir(parents=True, exist_ok=True)

    a = FastAPI()
    a.state.cordis_ctx = _StubCtx(provider)
    a.include_router(projects_router.router, prefix="/api/projects")
    a.include_router(runs_router.router, prefix="/api/runs")
    a.dependency_overrides[get_current_user_id] = lambda: "local"

    yield {
        "client": TestClient(a),
        "provider": provider,
        "spaces": spaces,
        "store": ProjectStore(provider),
        "reg": RunRegistry("tasks", provider),
    }
    provider.close()
    paths.set_data_root(None)


def _fake_cmd(script_body: str):
    """生成指向 tmp 目标目录的 python 子进程命令（注入 build_clone_cmd）。"""
    def _build(url: str, dest: Path):
        return [sys.executable, "-c",
                f"from pathlib import Path; d=Path({str(dest)!r}); "
                f"d.mkdir(parents=True, exist_ok=True); {script_body}"]
    return _build


def test_clone_success_creates_project_and_done_run(api, monkeypatch):
    c = api["client"]
    marker = 'Path(d / "CLONED").write_text("ok")'
    monkeypatch.setattr(project_tasks, "build_clone_cmd", _fake_cmd(marker))

    r = c.post("/api/projects/clone",
               json={"url": "https://github.com/x/demo.git", "name": "Demo"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["slug"] == "demo" and j["status"] == "queued"

    run = api["reg"].get(j["run_id"])
    assert run["status"] == "done"
    assert run["kind"] == "git_clone"

    record = api["store"].get("demo")
    assert record is not None and record["type"] == "hosted"
    final = Path(record["path"])
    assert (final / "CLONED").exists()
    # 无临时目录残留
    assert [p.name for p in api["spaces"].iterdir() if p.name.startswith(".clone-")] == []

    # 只读账本可见
    runs = c.get("/api/runs").json()["runs"]
    assert any(x["run_id"] == j["run_id"] and x["status"] == "done" for x in runs)
    # 项目列表可见且目录存在
    cards = c.get("/api/projects").json()["projects"]
    card = next(p for p in cards if p["slug"] == "demo")
    assert card["path_exists"] is True


def test_clone_failure_marks_failed_without_artifacts(api, monkeypatch):
    c = api["client"]
    monkeypatch.setattr(project_tasks, "build_clone_cmd", _fake_cmd(
        "import sys; sys.stderr.write('fatal: repository not found'); sys.exit(3)"))

    r = c.post("/api/projects/clone", json={"url": "https://github.com/x/bad-repo.git"})
    assert r.status_code == 200
    j = r.json()

    run = api["reg"].get(j["run_id"])
    assert run["status"] == "failed"
    assert "fatal: repository not found" in run["error"]

    assert api["store"].get("bad-repo") is None          # 不留幽灵卡片
    leftovers = [p.name for p in api["spaces"].iterdir()]
    assert leftovers == []                               # 半成品已清理


def test_clone_timeout_marks_failed_and_cleans(api, monkeypatch):
    c = api["client"]
    monkeypatch.setattr(project_tasks, "build_clone_cmd", _fake_cmd(
        "import time; time.sleep(5)"))
    _real_run = project_tasks.run_git_clone   # 先捕获原函数，避免自递归
    monkeypatch.setattr(project_tasks, "run_git_clone",
                        lambda cmd, *a, **kw: _real_run(cmd, timeout_s=1))
    r = c.post("/api/projects/clone", json={"url": "https://github.com/x/slow.git"})
    assert r.status_code == 200
    run = api["reg"].get(r.json()["run_id"])
    assert run["status"] == "failed"
    assert "超时" in run["error"]
    assert list(api["spaces"].iterdir()) == []


def test_clone_invalid_url_rejected_before_ledger(api):
    r = api["client"].post("/api/projects/clone", json={"url": "http://insecure/repo.git"})
    assert r.status_code == 400
    r2 = api["client"].post("/api/projects/clone", json={"url": "https://"})
    assert r2.status_code == 400
    assert api["reg"].list() == []                       # 未入账
