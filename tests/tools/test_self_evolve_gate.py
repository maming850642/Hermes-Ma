"""
P3-6：self_evolve 工具面门控（self_evolve_enabled）回归。

背景（与 shell_enabled 完全同构的门控链路，用户拍板：开源出厂默认关、
Web 设置页可开、开启后才注入工具）：
    config 字段（config._BOOL_KEYS）→ 出厂模板 false → 设置页开关
    → classify_keys 写白名单 → PUT /system 落盘 + settings_update 广播
    → worker 内存合并 → resolve_tools 按 config_guard 逐 turn 过滤。
    force_approval 审批护栏（shell_safety，只认仓库路径字串）不受开关
    影响——开关只控工具注入，不削护栏。

覆盖（对齐 test_subagent_gate / test_settings_hot_apply / test_tool_schema
的既有同构用例）：
1. 工具面：默认关（三件套不注入）/ 开（注入）——CLI 与 Web 共用的
   resolve_tools 单一路径（子代理 inherit_tools 亦经此处）；
2. config 解析：缺省键视为关、'true'/'false' 字符串归一、出厂模板为
   真布尔 false；
3. 设置页开关写读 roundtrip：PUT /system '1'/'0' → yaml 真布尔落盘 +
   广播出网 + worker 合并为真布尔 + GET 回读。
"""
import yaml
import pytest

import src.agent  # noqa: F401  import 环兜底
from pathlib import Path

from src.storage import paths
from src.storage.sqlite_provider import SQLiteProvider
from src.tools.context import ToolContext
from src.tools.loader import load_builtin_tools
from src.workspace import state as workspace_state
from src.workspace.service import WorkspaceService

_SELF_EVOLVE_TOOLS = ("self_backup", "verify_self", "respawn_self")


# ============================================
# 公共设施（与 test_subagent_gate 同款）
# ============================================

@pytest.fixture
def ws(tmp_path):
    """数据根指向 tmp 的 WorkspaceService（挂载状态走 tmp 库，不污染真实数据）。"""
    paths.set_data_root(tmp_path / "data")
    provider = SQLiteProvider(db_path=tmp_path / "data" / "ws.db")
    svc = WorkspaceService(provider=provider)
    workspace_state.set_service(svc)
    yield svc
    workspace_state.set_service(None)
    provider.close()
    paths.set_data_root(None)


def _fake_settings(**overrides):
    """resolve_tools 所需的最小 settings 对象（缺 self_evolve_enabled 属性
    = config.yaml 缺键形态，getattr 默认 False 视为关）。"""
    s = type("S", (), {})()
    s.shell_enabled = False
    s.workspace_chat_only_tools = ""
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _resolved_names(settings):
    from src.tools.resolve import resolve_tools
    specs = resolve_tools(
        ToolContext(caller_context="main"), settings, include_mcp=False,
    )
    return {s.name for s in specs}


# ============================================
# 1. 工具面：默认关 / 开（resolve_tools 单一路径）
# ============================================

class TestToolSurfaceGating:

    def test_default_off_hides_self_evolve_tools(self, ws, tmp_path):
        """默认关（settings 缺键）+ 挂载 → 三件套不注入。

        挂载使 workspace 过滤放行全量，只剩 config_guard 拦——与
        test_subagent_gate 的 bash 探针同一构造。"""
        mnt = tmp_path / "proj"
        mnt.mkdir()
        ws.mount_local(str(mnt))

        names = _resolved_names(_fake_settings())
        for name in _SELF_EVOLVE_TOOLS:
            assert name not in names, \
                f"默认关时 {name} 不得进入工具面: {sorted(names)}"

    def test_explicit_false_hides_too(self, ws, tmp_path):
        """显式 False 同样隐藏（getattr 真值语义，非 None 判断）。"""
        mnt = tmp_path / "proj"
        mnt.mkdir()
        ws.mount_local(str(mnt))
        names = _resolved_names(_fake_settings(self_evolve_enabled=False))
        for name in _SELF_EVOLVE_TOOLS:
            assert name not in names

    def test_enabled_injects_all_three(self, ws, tmp_path):
        """开 → 三件套全部注入（确保上一条不是恒真）。"""
        mnt = tmp_path / "proj"
        mnt.mkdir()
        ws.mount_local(str(mnt))
        names = _resolved_names(_fake_settings(self_evolve_enabled=True))
        for name in _SELF_EVOLVE_TOOLS:
            assert name in names, f"开启后 {name} 应注入工具面: {sorted(names)}"

    def test_three_specs_declare_config_guard(self):
        """三件套 YAML 都声明 config_guard: self_evolve_enabled（加载层无感，
        过滤发生在 resolve_tools——与 bash 的 shell_enabled 同款）。"""
        tools = load_builtin_tools(force=True)
        for name in _SELF_EVOLVE_TOOLS:
            assert tools[name].config_guard == "self_evolve_enabled"

    def test_guard_does_not_touch_side_effects(self):
        """开关不削护栏：三件套的 side_effects / blocked_in 原样保留
        （respawn 仍 destructive 弹审批，employee/subagent 仍禁用）。"""
        tools = load_builtin_tools(force=True)
        for name in _SELF_EVOLVE_TOOLS:
            spec = tools[name]
            assert spec.blocked_in == ["employee", "subagent"]
        assert tools["respawn_self"].side_effects.destructive is True


# ============================================
# 2. config 解析：默认值 / 布尔归一
# ============================================

class TestConfigParsing:

    def test_normalize_setting_value_matrix(self):
        """config.normalize_setting_value：self_evolve_enabled 归一矩阵
        （'false' 字符串恒真问题必须被咽喉拦住——与 shell_enabled 同款）。"""
        from config import normalize_setting_value as nsv
        for truthy in ("true", "1", 1, True):
            assert nsv("self_evolve_enabled", truthy) is True
        for falsy in ("false", "0", 0, False):
            assert nsv("self_evolve_enabled", falsy) is False
        assert nsv("self_evolve_enabled", "weird") == "weird"  # 未知值不猜

    def test_missing_attr_defaults_false(self):
        """config.yaml 缺键 → settings 无该属性 → 消费点的
        getattr(..., False) 视为关（出厂默认关闭的代码侧兜底）。"""
        assert not getattr(_fake_settings(), "self_evolve_enabled", False)

    def test_example_template_ships_false(self):
        """出厂模板必须自带 self_evolve_enabled: false（真布尔，不带引号）。"""
        cfg_path = Path(__file__).resolve().parent.parent.parent / "config.example.yaml"
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data["self_evolve_enabled"] is False

    def test_read_path_normalizes_quoted_bool(self, tmp_path, monkeypatch):
        """yaml 读取侧兜底：存量带引号的 'true' 归一为真布尔。"""
        import config

        (tmp_path / "config.yaml").write_text(
            "self_evolve_enabled: 'true'\n", encoding="utf-8")
        monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
        try:
            config.get_settings.cache_clear()
            s = config.get_settings()
            assert s["self_evolve_enabled"] is True
        finally:
            monkeypatch.undo()
            config.get_settings.cache_clear()
            config.reload_settings()


# ============================================
# 3. 设置页开关写读 roundtrip（PUT /system 全链路）
# ============================================

class _FakeSettings(dict):
    """镜像 config._Settings：支持属性访问的 dict（worker 合并语义一致）。"""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"config key not found: {name}") from None

    def __setattr__(self, name, value):
        self[name] = value


class _RecordingAgent:
    def rebind_tools(self):
        pass

    def set_llm_params(self, **kwargs):
        pass


class TestSettingsRoundtrip:

    def _boot_client(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient
        from web_fastapi.app import create_app
        import web_fastapi.services.config_service as cs
        import web_fastapi.routers.config_router as cr

        monkeypatch.setattr(cs, "PROJECT_ROOT", tmp_path)   # 真实 save_yaml → tmp
        monkeypatch.setattr(cr, "reload_settings", lambda: {"llm_model_name": "m"})
        (tmp_path / "config.yaml").write_text(
            "self_evolve_enabled: false\n", encoding="utf-8")
        app = create_app()
        return TestClient(app), app

    def test_put_on_then_off_roundtrip(self, monkeypatch, tmp_path):
        """"设置页开 → 写盘 true + 广播 + worker 内存 True + GET 回读 true；
        关 → 全链路反转。前端开关提交 '1'/'0' 字符串（collectFormUpdates
        布尔特例），归一发生在 worker/config 咽喉。"""
        import yaml as yaml_mod
        from web_fastapi import worker_process as wp

        # worker 侧 settings 桩（_apply_settings_update 合并目标）
        import config
        fake = _FakeSettings({"self_evolve_enabled": False})
        monkeypatch.setattr(config, "get_settings", lambda: fake)

        client, app = self._boot_client(monkeypatch, tmp_path)
        captured = []
        with client as c:
            c.post("/api/auth/login", json={})
            app.state.worker_manager.broadcast_settings_updates = (
                lambda updates: captured.append(dict(updates)) or len(updates))
            app.state.worker_manager.broadcast_llm_params = lambda **k: 0

            # —— 开：前端开关形态 '1' ——
            r = c.put("/api/config/system",
                      json={"updates": {"self_evolve_enabled": "1"}})
            assert r.status_code == 200
            body = r.json()
            assert body["rejected"] == [], "白名单键不得被拒"
            assert "self_evolve_enabled" in body["applied_immediately"]

            raw = (tmp_path / "config.yaml").read_text(encoding="utf-8")
            assert "self_evolve_enabled: true" in raw      # 不带引号真布尔
            assert yaml_mod.safe_load(raw)["self_evolve_enabled"] is True

            assert captured == [{"self_evolve_enabled": "1"}]  # 广播出网（字符串）

            # worker 合并 → 真布尔 True（config_guard 真值消费）
            state = wp.WorkerState("local", slot="main")
            state.agent = _RecordingAgent()
            wp._apply_settings_update(state, {"updates": captured[0]})
            assert fake["self_evolve_enabled"] is True

            # GET 回读（设置页 fillSystemFields 的数据源）
            g = c.get("/api/config/system")
            assert g.status_code == 200
            assert g.json()["config"]["self_evolve_enabled"] is True

            # —— 关：前端开关形态 '0' ——
            captured.clear()
            r2 = c.put("/api/config/system",
                       json={"updates": {"self_evolve_enabled": "0"}})
            assert r2.status_code == 200
            raw2 = (tmp_path / "config.yaml").read_text(encoding="utf-8")
            assert "self_evolve_enabled: false" in raw2
            assert yaml_mod.safe_load(raw2)["self_evolve_enabled"] is False

            fake["self_evolve_enabled"] = "1"   # 先污染成恒真形态
            wp._apply_settings_update(state, {"updates": captured[0]})
            assert fake["self_evolve_enabled"] is False, \
                "'0' 必须经咽喉归一为假布尔（'false' 字符串恒真教训）"

    def test_classify_keys_accepts_self_evolve_enabled(self):
        """classify_keys 写白名单放行 self_evolve_enabled（同 shell_enabled）。"""
        from web_fastapi.services.config_service import classify_keys
        system, _personal = classify_keys()
        assert "self_evolve_enabled" in system
