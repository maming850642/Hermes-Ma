"""ShellExecutor.execute 超时路径（V3 yaml 工具的真实执行器）。

此前零覆盖：超时 → 树杀 → 返回话术全链路无断言（test_run_shell.py
测的是旧版直调函数）。锁三件事：
1. 超时后必须调用 _kill_process_tree（进程树真被杀，不留孤儿 find）；
2. 话术含平台提示（Windows + Git Bash，`/` 是 MSYS 虚拟根）——
   引导模型在工作区内/盘符路径重查，而不是加大 timeout 硬扫；
3. 旧话术"增加 timeout 参数"必须消失（它在教唆模型全盘重扫）。
"""
import sys
from unittest.mock import patch

import pytest

from src.storage.sqlite_provider import SQLiteProvider
from src.tools.context import ToolContext
from src.tools.executors import shell as shell_mod
from src.tools.executors.shell import ShellExecutor
from src.workspace import state as workspace_state
from src.workspace.service import WorkspaceService


@pytest.fixture
def mounted_ws(tmp_path):
    """数据根指向 tmp 的挂载工作区（executor 锚定用）。"""
    from src.storage import paths
    paths.set_data_root(tmp_path / "data")
    provider = SQLiteProvider(db_path=tmp_path / "data" / "ws.db")
    svc = WorkspaceService(provider=provider)
    workspace_state.set_service(svc)
    mnt = tmp_path / "mounted"
    mnt.mkdir(parents=True)
    svc.mount_local(str(mnt))
    yield mnt
    workspace_state.set_service(None)
    provider.close()
    paths.set_data_root(None)


@pytest.fixture
def ctx():
    return ToolContext(permission_mode="full_access")


@pytest.mark.skipif(
    sys.platform.startswith("win") and shell_mod._resolve_shell_program() is None,
    reason="Windows 无 Git Bash（cmd 回退不支持 sleep），超时链路无法验证",
)
def test_timeout_kills_tree_and_reports_platform(mounted_ws, ctx, monkeypatch):
    calls = []
    orig_kill = shell_mod._kill_process_tree

    def spy(proc):
        calls.append(proc)
        orig_kill(proc)

    monkeypatch.setattr(shell_mod, "_kill_process_tree", spy)

    ex = ShellExecutor()
    result = ex.execute({"command": "sleep 30", "timeout": 1}, ctx)

    assert calls, "超时后必须调用 _kill_process_tree（不留孤儿进程）"
    content = result.content
    assert "已被终止" in content or "被终止" in content
    assert "Git Bash" in content          # 平台提示
    assert "MSYS" in content              # `/` 语义说明
    assert "增加 timeout 参数" not in content  # 旧话术必须消失
