"""
Workspace 挂载 API（/api/workspace）测试。

不启动完整 create_app（那会 boot 主进程组合根 + fork worker），仿
test_waker_api.py：仅挂 workspace router 的精简 FastAPI app +
TestClient。service 用真实 WorkspaceService（SQLiteProvider 指向
tmp 库 + set_data_root(tmp) 隔离），经 workspace_state.set_service 注入。

覆盖：未登录 401 / status / mount_local / unmount / choose_chat_only /
mount_upload（zip 往返 + 非 zip 415）/ history / 服务未 boot 503。
"""
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.constants import LOCAL_USER
from src.storage import paths
from src.storage.sqlite_provider import SQLiteProvider
from src.workspace import state as workspace_state
from src.workspace.service import WorkspaceService
from web_fastapi.routers import workspace as workspace_router


@pytest.fixture
def app(tmp_path):
    """精简 app：只挂 workspace router；service 走 tmp 库。"""
    paths.set_data_root(tmp_path / "data")
    provider = SQLiteProvider(db_path=tmp_path / "data" / "ws.db")
    svc = WorkspaceService(provider=provider)
    workspace_state.set_service(svc)

    a = FastAPI()
    a.include_router(workspace_router.router, prefix="/api/workspace", tags=["workspace"])
    # 不挂 worker_manager：BackgroundTasks 的 _notify_worker_changed 会
    # getattr(app.state, "worker_manager", None) → None → 直接返回（不炸）
    yield a
    workspace_state.set_service(None)
    provider.close()
    paths.set_data_root(None)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def auth_headers(app):
    return {}


# ============================================
# 鉴权
# ============================================


def test_endpoints_public_no_headers(client):
    """免认证：匿名即可访问；缺表单字段走 422 校验而非 401 鉴权。"""
    assert client.get("/api/workspace").status_code == 200
    assert client.post("/api/workspace/unmount").status_code in (200, 400)
    assert client.post("/api/workspace/choose_chat_only").status_code == 200
    assert client.get("/api/workspace/history").status_code == 200
    assert client.post("/api/workspace/mount_local").status_code == 422


def test_service_missing_503():
    """service 未 boot（无 app.state.cordis_ctx 且无进程内单例）→ 503。"""
    workspace_state.set_service(None)
    a = FastAPI()
    a.include_router(workspace_router.router, prefix="/api/workspace", tags=["workspace"])
    c = TestClient(a)
    headers = {}
    assert c.get("/api/workspace", headers=headers).status_code == 503


# ============================================
# status 往返
# ============================================


def test_status_unconfigured(client, auth_headers):
    r = client.get("/api/workspace", headers=auth_headers)
    assert r.status_code == 200
    j = r.json()
    assert j["configured"] is False
    assert j["mode"] is None


def test_mount_local_roundtrip(client, auth_headers, tmp_path):
    mnt = tmp_path / "proj"
    mnt.mkdir()
    r = client.post(
        "/api/workspace/mount_local",
        headers=auth_headers,
        data={"path": str(mnt), "display_name": "项目甲"},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert j["mount"]["configured"] is True
    assert j["mount"]["mode"] == "local"
    assert j["mount"]["display_name"] == "项目甲"
    assert j["mount"]["path"] == str(mnt.resolve())

    # 状态可再查
    j2 = client.get("/api/workspace", headers=auth_headers).json()
    assert j2 == j["mount"]


def test_mount_local_invalid_400(client, auth_headers, tmp_path):
    r = client.post(
        "/api/workspace/mount_local",
        headers=auth_headers,
        data={"path": str(tmp_path / "nope")},
    )
    assert r.status_code == 400
    assert "不存在" in r.json()["detail"]


def test_mount_local_empty_path_422(client, auth_headers):
    """缺 path 字段 → FastAPI 校验 422。"""
    r = client.post("/api/workspace/mount_local", headers=auth_headers, data={})
    assert r.status_code == 422


def test_unmount_roundtrip(client, auth_headers, tmp_path):
    mnt = tmp_path / "u"
    mnt.mkdir()
    client.post("/api/workspace/mount_local", headers=auth_headers, data={"path": str(mnt)})
    r = client.post("/api/workspace/unmount", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["mount"]["configured"] is False
    # 二次卸载 → 400
    assert client.post("/api/workspace/unmount", headers=auth_headers).status_code == 400


def test_choose_chat_only_roundtrip(client, auth_headers):
    r = client.post("/api/workspace/choose_chat_only", headers=auth_headers)
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["mount"]["configured"] is True
    assert j["mount"]["mode"] == "none"


# ============================================
# mount_upload
# ============================================


def test_mount_upload_roundtrip(client, auth_headers, tmp_path, app):
    zp = tmp_path / "pack.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("a.txt", "A")
        zf.writestr("sub/b.txt", "B")
    with open(zp, "rb") as f:
        r = client.post(
            "/api/workspace/mount_upload",
            headers=auth_headers,
            files={"file": ("pack.zip", f, "application/zip")},
        )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert j["mount"]["mode"] == "upload"
    assert j["mount"]["display_name"] == "pack"
    root = Path(j["mount"]["path"])
    assert (root / "a.txt").read_text(encoding="utf-8") == "A"
    assert (root / "sub" / "b.txt").read_text(encoding="utf-8") == "B"


def test_mount_upload_rejects_non_zip(client, auth_headers):
    r = client.post(
        "/api/workspace/mount_upload",
        headers=auth_headers,
        files={"file": ("pack.tar", b"xxx", "application/octet-stream")},
    )
    assert r.status_code == 415


def test_mount_upload_zip_slip_400(client, auth_headers, tmp_path):
    zp = tmp_path / "evil.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("../evil.txt", "x")
    with open(zp, "rb") as f:
        r = client.post(
            "/api/workspace/mount_upload",
            headers=auth_headers,
            files={"file": ("evil.zip", f, "application/zip")},
        )
    assert r.status_code == 400
    assert not (tmp_path / "evil.txt").exists()


# ============================================
# history
# ============================================


def test_history_roundtrip(client, auth_headers, tmp_path):
    for name in ("h1", "h2"):
        d = tmp_path / name
        d.mkdir()
        client.post(
            "/api/workspace/mount_local",
            headers=auth_headers,
            data={"path": str(d), "display_name": name},
        )
    r = client.get("/api/workspace/history", headers=auth_headers)
    assert r.status_code == 200
    hist = r.json()["history"]
    assert len(hist) == 1  # 只有 h1 入史（h2 是当前态）
    assert hist[0]["display_name"] == "h1"
    assert "unmounted_at" in hist[0]


# ============================================
# service 取自 app.state.cordis_ctx（接线方式）
# ============================================


class _FakeCtx:
    """替身组合根：try_get("workspace") 返回注入的 service。"""

    def __init__(self, svc):
        self._svc = svc

    def try_get(self, key):
        return self._svc if key == "workspace" else None


def test_service_via_app_state_cordis_ctx(tmp_path):
    """app.state.cordis_ctx 路径优先：ctx 上挂 service 时直接取用。"""
    paths.set_data_root(tmp_path / "data")
    provider = SQLiteProvider(db_path=tmp_path / "data" / "ws2.db")
    svc = WorkspaceService(provider=provider)
    try:
        a = FastAPI()
        a.include_router(workspace_router.router, prefix="/api/workspace", tags=["workspace"])
        a.state.cordis_ctx = _FakeCtx(svc)
        c = TestClient(a)
        headers = {}
        # 进程内单例故意不设——只走 app.state.cordis_ctx 分支
        j = c.get("/api/workspace", headers=headers).json()
        assert j["configured"] is False
        mnt = tmp_path / "viactx"
        mnt.mkdir()
        r = c.post(
            "/api/workspace/mount_local", headers=headers, data={"path": str(mnt)}
        )
        assert r.status_code == 200
        assert r.json()["mount"]["mode"] == "local"
    finally:
        workspace_state.set_service(None)
        provider.close()
        paths.set_data_root(None)


# ============================================
# P1-5：用户数据根敏感子树封禁（.ssh/.aws/.kube/.gnupg/AppData）
# ============================================


class TestMountSensitiveSubtrees:
    """挂载黑名单从「用户数据根只封根本身」收紧为「根 + 敏感子树」。

    免认证形态下匿名 CSRF 可直达 mount_local，只封根本身挡不住
    ~/.ssh（密钥）、Chrome User Data 等凭据存放地。
    """

    def _isolate_home(self, monkeypatch, home):
        monkeypatch.setenv("USERPROFILE", str(home))
        for env in ("APPDATA", "LOCALAPPDATA", "ProgramData", "ALLUSERSPROFILE"):
            monkeypatch.delenv(env, raising=False)

    def test_sensitive_subtrees_rejected(self, client, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        self._isolate_home(monkeypatch, home)
        for name in (".ssh", ".aws", ".kube", ".gnupg", "AppData"):
            d = home / name
            d.mkdir(exist_ok=True)
            r = client.post("/api/workspace/mount_local", data={"path": str(d)})
            assert r.status_code == 400, name
            assert "系统目录" in r.json()["detail"]

    def test_deep_under_sensitive_subtree_rejected(self, client, tmp_path, monkeypatch):
        """Chrome User Data 这类深层凭据目录同样拒载（AppData 子树封禁）。"""
        home = tmp_path / "home"
        chrome = home / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
        chrome.mkdir(parents=True)
        self._isolate_home(monkeypatch, home)
        r = client.post("/api/workspace/mount_local", data={"path": str(chrome)})
        assert r.status_code == 400

    def test_root_still_rejected(self, client, tmp_path, monkeypatch):
        """根本身照旧拒载（原 R3-17 语义保持）。"""
        home = tmp_path / "home"
        home.mkdir()
        self._isolate_home(monkeypatch, home)
        r = client.post("/api/workspace/mount_local", data={"path": str(home)})
        assert r.status_code == 400

    def test_normal_project_subdir_unaffected(self, client, tmp_path, monkeypatch):
        """敏感子树之外的用户目录（Documents 下的工程）不受影响。"""
        home = tmp_path / "home"
        proj = home / "Documents" / "my-project"
        proj.mkdir(parents=True)
        self._isolate_home(monkeypatch, home)
        r = client.post("/api/workspace/mount_local", data={"path": str(proj)})
        assert r.status_code == 200, r.text
        assert r.json()["mount"]["mode"] == "local"

    def test_temp_dir_inside_appdata_still_mountable(self, client, tmp_path, monkeypatch):
        """Windows %TEMP% 布局归属 %LOCALAPPDATA%\\Temp：临时目录之下的
        工程不因 AppData 封禁误杀（pytest tmp_path / 解压草稿所在）。"""
        import tempfile as _tempfile
        home = tmp_path / "home"
        temp_root = home / "AppData" / "Local" / "Temp"
        proj = temp_root / "pytest-of-x" / "proj999"
        proj.mkdir(parents=True)
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
        monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(temp_root))
        r = client.post("/api/workspace/mount_local", data={"path": str(proj)})
        assert r.status_code == 200, r.text
        assert r.json()["mount"]["mode"] == "local"
