"""只读文件树 API（ADR-0005 D6）行为锁定。

覆盖：懒加载单层列表、折叠目录哨兵、路径越界 400、404、
文本预览 / 截断 / 二进制 415。组装方式仿 test_workspace_api.py 的
精简 app + tmp data root 隔离。
"""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.storage import paths
from src.storage.sqlite_provider import SQLiteProvider
from src.workspace import state as workspace_state
from src.workspace.service import WorkspaceService
from web_fastapi.routers import workspace as workspace_router


@pytest.fixture
def env(tmp_path):
    paths.set_data_root(tmp_path / "data")
    provider = SQLiteProvider(db_path=tmp_path / "data" / "ws.db")
    svc = WorkspaceService(provider=provider)

    proj = tmp_path / "myproj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (proj / "README.md").write_text("# demo\n" * 5, encoding="utf-8")
    (proj / ".git").mkdir()
    (proj / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    nm = proj / "node_modules"
    nm.mkdir()
    for i in range(30):                       # 深藏大目录：不应被展开读取
        (nm / f"pkg{i}").mkdir()

    svc.mount_local(str(proj), display_name="myproj")
    workspace_state.set_service(svc)          # _get_service 的回退单例

    a = FastAPI()
    a.include_router(workspace_router.router, prefix="/api/workspace")
    yield a, proj, tmp_path
    workspace_state.set_service(None)
    provider.close()
    paths.set_data_root(None)


@pytest.fixture
def client(env):
    return TestClient(env[0])


def test_tree_root_lists_and_collapses_sentinels(client, env):
    r = client.get("/api/workspace/tree")
    assert r.status_code == 200, r.text
    j = r.json()
    names = {e["name"]: e for e in j["entries"]}
    assert names["README.md"]["type"] == "file"
    assert names["README.md"]["size"] > 0
    assert names[".git"]["collapsed"] is True           # 哨兵目录只给标记
    assert names["node_modules"]["collapsed"] is True
    # 目录在前
    types_in_order = [e["type"] for e in j["entries"]]
    assert types_in_order == sorted(types_in_order, key=lambda t: 0 if t == "dir" else 1)


def test_tree_subdir_navigation(client):
    r = client.get("/api/workspace/tree", params={"path": "src"})
    assert r.status_code == 200
    j = r.json()
    assert [e["name"] for e in j["entries"]] == ["main.py"]
    assert j["entries"][0]["type"] == "file"


def test_tree_escape_rejected_400(client):
    r = client.get("/api/workspace/tree", params={"path": "../outside.txt"})
    assert r.status_code == 400
    # 绝对路径越界同样拒绝
    r2 = client.get("/api/workspace/tree", params={"path": str(Path("C:/Windows"))})
    assert r2.status_code in (400, 404)


def test_tree_missing_dir_404(client):
    assert client.get("/api/workspace/tree", params={"path": "nope"}).status_code == 404


def test_tree_file_target_rejected_400(client):
    assert client.get("/api/workspace/tree", params={"path": "README.md"}).status_code == 400


def test_preview_text_ok(client):
    r = client.get("/api/workspace/tree/preview", params={"path": "README.md"})
    assert r.status_code == 200
    j = r.json()
    assert j["truncated"] is False
    assert j["content"].startswith("# demo")


def test_preview_truncates_large_file(client, env, monkeypatch):
    _, proj, _ = env
    big = proj / "big.log"
    big.write_text("x" * 20_000, encoding="utf-8")
    monkeypatch.setattr(workspace_router, "_preview_max_bytes", lambda: 1024)
    r = client.get("/api/workspace/tree/preview", params={"path": "big.log"})
    assert r.status_code == 200
    j = r.json()
    assert j["truncated"] is True
    assert len(j["content"]) <= 1100                    # 解码替换后的近似上限


def test_preview_binary_rejected_415(client, env):
    _, proj, _ = env
    binf = proj / "blob.bin"
    binf.write_bytes(b"AB\x00CD" * 100)
    r = client.get("/api/workspace/tree/preview", params={"path": "blob.bin"})
    assert r.status_code == 415


def test_preview_missing_file_404(client):
    r = client.get("/api/workspace/tree/preview", params={"path": "nope.txt"})
    assert r.status_code == 404


def test_preview_on_directory_rejected_400(client):
    r = client.get("/api/workspace/tree/preview", params={"path": "src"})
    assert r.status_code == 400


def test_preview_html_exempt_from_text_cap(client, env, monkeypatch):
    """.html 预览不受文本 cap 钳制（自包含 HTML 截断=废页），放宽到 8MB 硬顶。"""
    _, proj, _ = env
    big_html = proj / "big.html"
    big_html.write_text("<html>" + "x" * 6000 + "</html>", encoding="utf-8")
    monkeypatch.setattr(workspace_router, "_preview_max_bytes", lambda: 1024)
    r = client.get("/api/workspace/tree/preview", params={"path": "big.html"})
    assert r.status_code == 200
    j = r.json()
    assert j["truncated"] is False
    assert len(j["content"]) > 1024


def test_preview_other_text_still_capped(client, env, monkeypatch):
    """非 HTML 类型维持 cap 截断语义（对照用例）。"""
    _, proj, _ = env
    big_log = proj / "big2.log"
    big_log.write_text("y" * 6000, encoding="utf-8")
    monkeypatch.setattr(workspace_router, "_preview_max_bytes", lambda: 1024)
    r = client.get("/api/workspace/tree/preview", params={"path": "big2.log"})
    assert r.status_code == 200
    j = r.json()
    assert j["truncated"] is True
    assert len(j["content"]) <= 1100
