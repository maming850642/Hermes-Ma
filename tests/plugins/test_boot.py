"""
T4 组合根 boot_context 测试。

覆盖：
1. boot 后核心服务键可得（config/storage/sessions/memory/llm/tools/skills）
2. inject 顺序：storage 先于 sessions/memory（共享同一 provider 实例）
3. 重复 boot 互不干扰（独立 storage 实例，teardown 一个不影响另一个）
4. teardown 后可再 boot
"""

from __future__ import annotations

import pytest

from src.plugins import boot_context
from src.plugins.boot import DEFAULT_PROFILE

# 与仓库根 cordis.yaml 同构的 profile，storage 指向 tmp 库（测试隔离）
_PROFILE_TMPL = """
plugins:
  - {{id: config,   plugin: "src.plugins.config_plugin:apply"}}
  - {{id: storage,  plugin: "src.plugins.storage_plugin:apply", config: {{db_path: "{db}"}}}}
  - {{id: sessions, plugin: "src.plugins.sessions_plugin:apply", inject: [storage]}}
  - {{id: memory,   plugin: "src.plugins.memory_plugin:apply",   inject: [storage]}}
  - {{id: llm,      plugin: "src.plugins.llm_plugin:apply",      inject: [config]}}
  - {{id: tools,    plugin: "src.plugins.tools_plugin:apply",    inject: [config]}}
  - {{id: skills,   plugin: "src.plugins.skills_plugin:apply",   inject: [config]}}
  - {{id: mcp,      plugin: "src.plugins.mcp_plugin:apply"}}
"""

_CORE_KEYS = ["config", "storage", "sessions", "memory", "llm", "tools", "skills"]


def write_profile(tmp_path, name: str = "cordis.yaml"):
    """写一份指向 tmp 库的 profile，返回路径。"""
    db = (tmp_path / f"{name}.db").as_posix()
    profile = tmp_path / name
    profile.write_text(_PROFILE_TMPL.format(db=db), encoding="utf-8")
    return profile


class TestBootContext:

    def test_all_core_keys_available(self, tmp_path):
        ctx = boot_context(write_profile(tmp_path))
        try:
            for key in _CORE_KEYS:
                assert ctx.get(key) is not None, f"服务键缺失: {key}"
            # 属性访问（稳定键语义）也成立
            assert ctx.storage is ctx.get("storage")
            # mcp 是可选依赖：键可取（None 表示不可用）
            assert "mcp" in ctx._services
        finally:
            ctx.teardown()

    def test_default_profile_is_repo_cordis_yaml(self):
        """boot_context() 缺省读仓库根 cordis.yaml。"""
        from src.plugins.boot import DEFAULT_PROFILE as default
        assert default.name == "cordis.yaml"
        assert default.exists()
        ctx = boot_context()
        try:
            for key in _CORE_KEYS:
                assert ctx.get(key) is not None
        finally:
            ctx.teardown()

    def test_inject_order_storage_before_dependents(self, tmp_path):
        """inject 拓扑：storage 必须先于 sessions/memory 挂载。"""
        ctx = boot_context(write_profile(tmp_path))
        try:
            order = [child.name for child in ctx._children]
            assert order.index("storage") < order.index("sessions")
            assert order.index("storage") < order.index("memory")
            # inject 缺服务时 loader 会 RuntimeError，能 boot 成功即拓扑成立
        finally:
            ctx.teardown()

    def test_sessions_and_memory_share_storage(self, tmp_path):
        """sessions/memory 注入的是 storage 插件的同一 provider 实例。"""
        ctx = boot_context(write_profile(tmp_path))
        try:
            assert ctx.sessions.provider is ctx.storage
            assert ctx.memory.store is ctx.storage
        finally:
            ctx.teardown()

    def test_tools_service_registry_wired_to_kernel_ctx(self, tmp_path):
        """tools 服务的 registry 带 kernel_ctx（权限段事件化路径）。"""
        from src.plugins.tools_plugin import ToolsService

        ctx = boot_context(write_profile(tmp_path))
        try:
            tools = ctx.get("tools")
            assert isinstance(tools, ToolsService)
            assert tools.registry is not None
            assert tools.registry._kernel is not None
        finally:
            ctx.teardown()

    def test_repeated_boots_independent(self, tmp_path):
        """两个 boot 互不干扰：独立 storage 实例，teardown 一个另一个照常。"""
        ctx_a = boot_context(write_profile(tmp_path, "a"))
        ctx_b = boot_context(write_profile(tmp_path, "b"))
        try:
            assert ctx_a.storage is not ctx_b.storage
            assert ctx_a.memory is not ctx_b.memory

            ctx_a.storage.kv_put("t", "who", "A")
            ctx_b.storage.kv_put("t", "who", "B")
            assert ctx_a.storage.kv_get("t", "who") == "A"
            assert ctx_b.storage.kv_get("t", "who") == "B"

            ctx_a.teardown()
            # B 不受 A teardown 影响
            assert ctx_b.storage.kv_get("t", "who") == "B"
            ctx_b.storage.kv_put("t", "alive", True)
            assert ctx_b.storage.kv_get("t", "alive") is True
        finally:
            ctx_a.teardown()
            ctx_b.teardown()

    def test_teardown_then_reboot(self, tmp_path):
        """teardown 后同一 profile 可再 boot（服务键重新可得、可读写）。"""
        profile = write_profile(tmp_path)
        ctx1 = boot_context(profile)
        ctx1.storage.kv_put("t", "round", 1)
        ctx1.teardown()

        ctx2 = boot_context(profile)
        try:
            for key in _CORE_KEYS:
                assert ctx2.get(key) is not None
            assert ctx2.storage.kv_get("t", "round") == 1  # 同一库文件，数据还在
            ctx2.storage.kv_put("t", "round", 2)
            assert ctx2.storage.kv_get("t", "round") == 2
        finally:
            ctx2.teardown()

    def test_session_log_roundtrip_via_ctx(self, tmp_path):
        """组合根上 sessions 可直接做事件追加 + 投影（T3 行为不变）。"""
        from src.agent.session_log import USER_MSG

        ctx = boot_context(write_profile(tmp_path))
        try:
            ctx.sessions.append("s1", USER_MSG, {"content": "hi"})
            msgs = ctx.sessions.derive_messages("s1")
            assert msgs == [{"role": "user", "content": "hi"}]
        finally:
            ctx.teardown()
