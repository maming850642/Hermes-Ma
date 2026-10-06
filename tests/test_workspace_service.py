"""
T5 WorkspaceService 单元测试：挂载状态机（kv 持久化 + 校验矩阵）。

覆盖：
- mount_local 校验矩阵：空/相对/不存在/是文件/不可写（monkeypatch os.access）/
  data_root 子树拒/系统目录拒/盘符根拒
- kv 持久跨实例（provider 重开同一库，挂载状态仍在）
- unmount / choose_chat_only 语义（None vs mode="none"）
- mount_upload：正常解压 / zip-slip 拒绝+清残留 / 大小超限 / 非 zip / 损坏包
- history：卸载/切换入史、最近 20 条上限、最新在前

隔离：set_data_root(tmp) + SQLiteProvider(db_path=tmp/…db)，不碰真实 data/。
"""
import os
import sys
import zipfile
from pathlib import Path

import pytest

from src.storage import paths
from src.storage.sqlite_provider import SQLiteProvider
from src.workspace import state as workspace_state
from src.workspace.models import MODE_LOCAL, MODE_NONE, MODE_UPLOAD
from src.workspace.service import (
    DEFAULT_CHAT_ONLY_TOOLS,
    MAX_HISTORY,
    WorkspaceError,
    WorkspaceService,
)


@pytest.fixture
def ws(tmp_path):
    """数据根指向 tmp 的 WorkspaceService（mounts 落 tmp/data/mounts）。"""
    paths.set_data_root(tmp_path / "data")
    provider = SQLiteProvider(db_path=tmp_path / "data" / "ws.db")
    workspace_state.set_service(WorkspaceService(provider=provider))
    yield workspace_state.get_service()
    workspace_state.set_service(None)
    provider.close()
    paths.set_data_root(None)


def _mk_dir(tmp_path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


# ============================================
# status / 初始状态
# ============================================


def test_status_none_when_never_configured(ws):
    """从未配置 → status() 为 None（区别于 mode="none"）。"""
    assert ws.status() is None
    assert ws.current_root() is None
    assert ws.history() == []


def test_chat_only_tools_default(ws):
    """chat_only_tools() 缺省集合（settings 未配置该键）。"""
    assert ws.chat_only_tools() == {t.strip() for t in DEFAULT_CHAT_ONLY_TOOLS.split(",")}


# ============================================
# mount_local 校验矩阵
# ============================================


def test_mount_local_ok(ws, tmp_path):
    d = _mk_dir(tmp_path, "proj")
    st = ws.mount_local(str(d), display_name="我的项目")
    assert st.mode == MODE_LOCAL
    assert st.is_mounted()
    assert st.display_name == "我的项目"
    assert Path(st.path) == d.resolve()
    # 状态持久在 kv
    assert ws.status().path == str(d.resolve())
    assert ws.current_root() == d.resolve()


def test_mount_local_display_name_defaults_to_dirname(ws, tmp_path):
    d = _mk_dir(tmp_path, "proj2")
    st = ws.mount_local(f'"{str(d)}"')  # 顺手验证首尾引号被剥掉
    assert st.display_name == "proj2"


def test_mount_local_empty_path(ws):
    with pytest.raises(WorkspaceError):
        ws.mount_local("   ")


def test_mount_local_relative_rejected(ws):
    with pytest.raises(WorkspaceError, match="绝对路径"):
        ws.mount_local("relative/dir")


def test_mount_local_not_exist(ws, tmp_path):
    with pytest.raises(WorkspaceError, match="不存在"):
        ws.mount_local(str(tmp_path / "nope"))


def test_mount_local_not_a_dir(ws, tmp_path):
    f = tmp_path / "afile.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="不是目录"):
        ws.mount_local(str(f))


def test_mount_local_not_writable(ws, tmp_path, monkeypatch):
    """不可写目录拒（monkeypatch os.access 模拟，Windows 无 chmod 可靠手段）。"""
    d = _mk_dir(tmp_path, "ro")
    real_access = os.access
    monkeypatch.setattr(
        os, "access",
        lambda p, m: False if Path(p) == d.resolve() and m == os.W_OK else real_access(p, m),
    )
    with pytest.raises(WorkspaceError, match="不可写"):
        ws.mount_local(str(d))


def test_mount_local_rejects_data_root_subtree(ws, tmp_path):
    """data_root 本身与其子目录都拒（防 agent 写进系统内部数据）。"""
    with pytest.raises(WorkspaceError, match="数据目录"):
        ws.mount_local(str(paths.data_root()))
    sub = paths.data_dir("sessions")
    sub.mkdir(parents=True, exist_ok=True)
    with pytest.raises(WorkspaceError, match="数据目录"):
        ws.mount_local(str(sub))


def test_mount_local_rejects_windows_system_dir(ws, tmp_path, monkeypatch):
    """Windows 系统目录（%WINDIR%）拒。os.access 打桩为可写，隔离权限差异。"""
    if sys.platform != "win32":
        pytest.skip("Windows 系统目录用例")
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if not windir:
        pytest.skip("无 WINDIR 环境变量")
    monkeypatch.setattr(os, "access", lambda p, m: True)
    with pytest.raises(WorkspaceError, match="系统目录"):
        ws.mount_local(windir)


def test_mount_local_rejects_drive_root(ws, tmp_path, monkeypatch):
    """盘符根（C:\\ / D:\\）与 POSIX 根（/）拒——parent == 自身。"""
    anchor = Path(tmp_path.anchor)  # Windows: "D:\\"；POSIX: "/"
    monkeypatch.setattr(os, "access", lambda p, m: True)
    with pytest.raises(WorkspaceError, match="盘符根|系统目录"):
        ws.mount_local(str(anchor))


# ============================================
# kv 持久跨实例
# ============================================


def test_kv_persistence_across_instances(ws, tmp_path):
    """provider 重开同一库 → 挂载状态与历史仍在（跨进程共享的依据）。"""
    d = _mk_dir(tmp_path, "persist")
    ws.mount_local(str(d))
    ws.choose_chat_only()

    db = tmp_path / "data" / "ws.db"
    provider2 = SQLiteProvider(db_path=db)
    try:
        svc2 = WorkspaceService(provider=provider2)
        st = svc2.status()
        assert st is not None
        assert st.mode == MODE_NONE  # 最后一次写入生效
        hist = svc2.history()
        assert len(hist) == 1
        assert hist[0]["mode"] == MODE_LOCAL
        assert hist[0]["path"] == str(d.resolve())
    finally:
        provider2.close()


def test_corrupted_mount_treated_as_unconfigured(ws, monkeypatch):
    """kv 里的 mount 数据损坏/模式非法 → status() 安全侧返回 None。"""
    ws._provider.kv_put("workspace", "mount", {"mode": "weird", "path": "/x"})
    assert ws.status() is None
    ws._provider.kv_put("workspace", "mount", "not-a-dict")
    assert ws.status() is None


# ============================================
# unmount / choose_chat_only
# ============================================


def test_unmount(ws, tmp_path):
    d = _mk_dir(tmp_path, "u1")
    ws.mount_local(str(d))
    ws.unmount()
    assert ws.status() is None  # 回到"从未配置"（进门禁重新选择）
    assert ws.current_root() is None
    # 卸载动作入史
    hist = ws.history()
    assert len(hist) == 1
    assert hist[0]["mode"] == MODE_LOCAL
    assert "unmounted_at" in hist[0]
    # 二次卸载报错
    with pytest.raises(WorkspaceError, match="没有已配置"):
        ws.unmount()


def test_choose_chat_only(ws):
    """仅对话：已配置（不再进门禁）但零文件权限。"""
    st = ws.choose_chat_only()
    assert st.mode == MODE_NONE
    assert st is not None
    assert ws.status().mode == MODE_NONE
    assert not ws.status().is_mounted()
    assert ws.current_root() is None


def test_mount_over_closes_previous_into_history(ws, tmp_path):
    """切换挂载时，上一条状态收进 history（最新在前）。"""
    d1 = _mk_dir(tmp_path, "s1")
    d2 = _mk_dir(tmp_path, "s2")
    ws.mount_local(str(d1))
    ws.mount_local(str(d2))
    ws.choose_chat_only()
    hist = ws.history()
    # 两条被替换的 local 状态入史（none 是当前态，尚未入史）
    assert [e["mode"] for e in hist] == [MODE_LOCAL, MODE_LOCAL]  # 最新在前
    assert hist[0]["path"] == str(d2.resolve())
    assert hist[-1]["path"] == str(d1.resolve())
    # 当前态已是 none；卸载后 none 也入史（path 为空）
    ws.unmount()
    hist2 = ws.history()
    assert [e["mode"] for e in hist2][0] == MODE_NONE
    assert hist2[0]["path"] == ""


# ============================================
# mount_upload
# ============================================


def _make_zip(path: Path, members: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return path


def test_mount_upload_ok(ws, tmp_path):
    zp = _make_zip(tmp_path / "pack.zip", {
        "readme.md": "# hello",
        "src/app.py": "print('hi')",
    })
    st = ws.mount_upload(zp, display_name="上传包")
    assert st.mode == MODE_UPLOAD
    assert st.is_mounted()
    root = Path(st.path)
    # 解压到 data/mounts/<12hex>
    assert root.parent == paths.data_dir("mounts")
    assert len(root.name) == 12 and all(c in "0123456789abcdef" for c in root.name)
    assert (root / "readme.md").read_text(encoding="utf-8") == "# hello"
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "print('hi')"
    assert ws.current_root() == root


def test_mount_upload_display_name_defaults_to_stem(ws, tmp_path):
    zp = _make_zip(tmp_path / "我的素材.zip", {"a.txt": "1"})
    st = ws.mount_upload(zp)
    assert st.display_name == "我的素材"


def test_mount_upload_rejects_non_zip(ws, tmp_path):
    f = tmp_path / "pack.tar"
    f.write_bytes(b"xxx")
    with pytest.raises(WorkspaceError, match="zip"):
        ws.mount_upload(f)


def test_mount_upload_rejects_missing_file(ws, tmp_path):
    with pytest.raises(WorkspaceError, match="不存在"):
        ws.mount_upload(tmp_path / "nope.zip")


def test_mount_upload_rejects_bad_zip(ws, tmp_path):
    f = tmp_path / "bad.zip"
    f.write_bytes(b"this is not a zip")
    with pytest.raises(WorkspaceError, match="损坏"):
        ws.mount_upload(f)


def test_mount_upload_zip_slip_rejected_and_cleaned(ws, tmp_path):
    """成员 resolve 落在目标外 → 整体拒绝 + 清残留 + 越界文件不落盘。"""
    zp = _make_zip(tmp_path / "evil.zip", {
        "good.txt": "ok",
        "../evil.txt": "escaped",
    })
    with pytest.raises(WorkspaceError, match="越界"):
        ws.mount_upload(zp)
    # 越界文件未写出
    assert not (tmp_path / "evil.txt").exists()
    # 残留的解压目录已清（mounts 下无残留 <id> 目录）
    mounts_dir = paths.data_dir("mounts")
    if mounts_dir.exists():
        assert list(mounts_dir.iterdir()) == []
    # 未写入挂载状态
    assert ws.status() is None


def test_mount_upload_zip_slip_absolute_member_rejected(ws, tmp_path):
    """绝对路径成员（C:/x 或 /x）同样拒绝。"""
    member = "C:/Windows/evil.txt" if sys.platform == "win32" else "/etc/evil.txt"
    zp = _make_zip(tmp_path / "evil2.zip", {member: "x"})
    with pytest.raises(WorkspaceError, match="越界"):
        ws.mount_upload(zp)


def test_mount_upload_size_limit(ws, tmp_path, monkeypatch):
    """settings 键 workspace_upload_max_mb：声明大小超限直接拒绝（不解压）。"""

    class _Settings:
        workspace_upload_max_mb = 0  # 0MB → 任何内容都超限

    monkeypatch.setattr("config.get_settings", lambda: _Settings())
    zp = _make_zip(tmp_path / "big.zip", {"a.txt": "x" * 1024})
    with pytest.raises(WorkspaceError, match="上限"):
        ws.mount_upload(zp)
    mounts_dir = paths.data_dir("mounts")
    if mounts_dir.exists():
        assert list(mounts_dir.iterdir()) == []


# ============================================
# history 上限
# ============================================


def test_history_cap_20(ws, tmp_path):
    """连续挂载 25 次 → history 恒保留最近 20 条。"""
    for i in range(25):
        d = _mk_dir(tmp_path, f"h{i}")
        ws.mount_local(str(d))
    hist = ws.history()
    assert len(hist) == MAX_HISTORY == 20
    # 保留的是最近 20 条（最新在前：h24 的上一条是 h23…）
    assert hist[0]["display_name"] == "h23"
    assert hist[-1]["display_name"] == "h4"
