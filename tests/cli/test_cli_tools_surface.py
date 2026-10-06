"""
F3 回归：CLI 工具面不坍缩（boot_context + /tools 打印生效集）。

背景（2026-08-15 R1 深度 review）：
    src/cli.py 直接构造 HermesAgentV3（无 kernel_ctx / registry）→ 进程内
    无 WorkspaceService → resolve_tools 安全回退 chat-only，而 /tools 与
    MCP 菜单却展示全量清单——用户看到的与 Agent 实际能用的不一致。

覆盖：
1. boot_context 后（workspace 插件挂上）+ local 挂载 → resolve_tools 含
   fs 工具（CLI 同款构造路径不再坍缩到 chat-only）；
2. describe_workspace() 的三态文案（挂载 / 纯对话 / 未配置回退）；
3. show_tools(agent) 打印 agent 实际绑定的生效集（非全量清单）。
"""
from unittest.mock import MagicMock

import pytest

import src.agent  # noqa: F401  import 环兜底
from src.plugins import boot_context
from src.storage import paths
from src.storage.sqlite_provider import SQLiteProvider
from src.tools.context import ToolContext
from src.tools.schema import ToolSpec
from src.workspace import state as workspace_state
from src.workspace.service import WorkspaceService

from config import get_settings
from src.tools.resolve import resolve_tools

# 与仓库根 cordis.yaml 同构的 profile（storage 指 tmp 库，测试隔离）
_PROFILE_TMPL = """
plugins:
  - {{id: config,   plugin: "src.plugins.config_plugin:apply"}}
  - {{id: storage,  plugin: "src.plugins.storage_plugin:apply", config: {{db_path: "{db}"}}}}
  - {{id: workspace, plugin: "src.plugins.workspace_plugin:apply", inject: [storage]}}
  - {{id: tools,    plugin: "src.plugins.tools_plugin:apply",    inject: [config]}}
"""


# ============================================
# 1. boot_context 后工具面不坍缩
# ============================================

class TestBootContextToolSurface:

    def test_mounted_resolves_full_tools(self, tmp_path):
        """boot_context（含 workspace 插件）+ local 挂载 → 全量工具可见。

        fs 工具 2026-09 退役；用 bash（不在 chat-only 白名单，受
        shell_enabled config_guard）作为"挂载后进入生效集"的探针——
        CLI 无 boot 时只剩 chat-only，bash 不会出现。
        """
        db = (tmp_path / "boot.db").as_posix()
        profile = tmp_path / "cordis.yaml"
        profile.write_text(_PROFILE_TMPL.format(db=db), encoding="utf-8")

        ctx = boot_context(profile)
        try:
            svc = ctx.get("workspace")
            assert svc is not None, "workspace 服务应随 boot 挂载"
            mnt = tmp_path / "proj"
            mnt.mkdir()
            svc.mount_local(str(mnt))

            specs = resolve_tools(
                ToolContext(caller_context="main"), get_settings(), include_mcp=False,
            )
            names = {s.name for s in specs}
            assert "bash" in names, \
                f"挂载后 bash 应进入生效集（CLI 无 boot 时只剩 chat-only），实际: {sorted(names)}"
        finally:
            ctx.teardown()

    def test_registry_injected_via_tools_service(self, tmp_path):
        """CLI 对齐 worker 的注入物存在：ctx.tools.registry 可取。"""
        db = (tmp_path / "boot2.db").as_posix()
        profile = tmp_path / "cordis.yaml"
        profile.write_text(_PROFILE_TMPL.format(db=db), encoding="utf-8")
        ctx = boot_context(profile)
        try:
            tools_svc = ctx.get("tools")
            assert getattr(tools_svc, "registry", None) is not None
        finally:
            ctx.teardown()


# ============================================
# 2. describe_workspace 三态
# ============================================

class TestDescribeWorkspace:

    @pytest.fixture
    def ws(self, tmp_path):
        paths.set_data_root(tmp_path / "data")
        provider = SQLiteProvider(db_path=tmp_path / "data" / "ws.db")
        svc = WorkspaceService(provider=provider)
        workspace_state.set_service(svc)
        yield svc
        workspace_state.set_service(None)
        provider.close()
        paths.set_data_root(None)

    def test_no_service_reports_fallback(self, monkeypatch):
        from src.cli import describe_workspace
        monkeypatch.setattr(workspace_state, "get_service", lambda: None)
        desc, _detail = describe_workspace()
        assert "chat-only" in desc or "未配置" in desc

    def test_chat_only_mode(self, ws):
        from src.cli import describe_workspace
        ws.choose_chat_only()
        desc, _detail = describe_workspace()
        assert "纯对话" in desc or "chat-only" in desc

    def test_mounted_mode(self, ws, tmp_path):
        from src.cli import describe_workspace
        mnt = tmp_path / "mounted"
        mnt.mkdir()
        ws.mount_local(str(mnt))
        desc, detail = describe_workspace()
        assert "挂载" in desc
        assert str(mnt) in detail


# ============================================
# 3. show_tools 打印生效集
# ============================================

class TestShowTools:

    @pytest.fixture
    def outbuf(self, monkeypatch):
        """cli.console 指向 StringIO 缓冲（rich 对 capsys 流的写入不可靠，直接捕缓冲）。"""
        import io
        from rich.console import Console
        import src.cli as cli_mod
        buf = io.StringIO()
        new_console = Console(file=buf, force_terminal=False, width=200)
        monkeypatch.setattr(cli_mod, "console", new_console)
        return buf

    def _spec(self, name):
        return ToolSpec(
            name=name, description=f"{name} 的说明", parameters={"type": "object", "properties": {}},
            executor=MagicMock(),
        )

    def test_prints_bound_effective_set(self, outbuf):
        """show_tools(agent) 打印 agent 实际绑定的生效集。"""
        from src.cli import show_tools
        agent = MagicMock()
        agent.registry.bound_specs.return_value = [self._spec("web_search"), self._spec("ls")]
        show_tools(agent)
        out = outbuf.getvalue()
        assert "web_search" in out
        assert "ls" in out
        assert "生效" in out, "应标明这是生效集而非全量清单"

    def test_excludes_unbound_tools(self, outbuf):
        """未绑定进生效集的工具不出现（旧实现打印全量 get_builtin_tools）。"""
        from src.cli import show_tools
        agent = MagicMock()
        agent.registry.bound_specs.return_value = [self._spec("web_search")]
        show_tools(agent)
        out = outbuf.getvalue()
        assert "web_search" in out
        assert "write_file" not in out, "全量清单里的工具不应出现（未在生效集内）"
