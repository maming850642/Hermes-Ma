"""模型热切换核心链路测试（运行中不重启更换模型）。

覆盖链路：set_llm_params 扩参（model/base_url/api_key/context_window）→
_get_llm_client 构造传参与变更失效 → 压缩阈值的窗口覆盖 → worker 的
llm_params_set op（主循环 + chat 内联，prefs 不回显 api_key）→
WorkerManager.broadcast_llm_params（单实例失败不影响其余）→
config.reload_settings 重读 config.yaml → PUT /api/config/system 的
热切换传播与 restart_required 语义 → CLI /model 命令。
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ════════════════════════════════════════════════════════════════
# agent_v3：set_llm_params 扩参 + _get_llm_client 构造与失效
# ════════════════════════════════════════════════════════════════

def _bare_agent():
    """最小化 agent（仿 test_llm_override：绕开 __init__ 的重依赖）。"""
    from src.agent.agent_v3 import HermesAgentV3
    from config import get_settings

    agent = HermesAgentV3.__new__(HermesAgentV3)
    agent.settings = get_settings()
    agent._llm_client = None
    agent._llm_temperature = None
    agent._llm_max_tokens = None
    agent._llm_overrides = {}
    agent._llm_client_build_sig = None
    return agent


def test_set_llm_params_model_overrides_reach_client(monkeypatch):
    """模型四项经 set_llm_params 存入 _llm_overrides，_get_llm_client 构造时传入。"""
    import src.agent.agent_v3 as v3mod

    agent = _bare_agent()
    agent.set_llm_params(model="m2", base_url="http://x.test/v1",
                         api_key="sk-2", context_window=4096)
    assert agent._llm_overrides == {
        "model": "m2", "base_url": "http://x.test/v1",
        "api_key": "sk-2", "context_window": 4096,
    }
    assert agent._llm_client is None  # 已构造客户端作废

    captured = {}

    class FakeLLMClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(v3mod, "LLMClient", FakeLLMClient)
    agent._get_llm_client()
    assert captured["model"] == "m2"
    assert captured["base_url"] == "http://x.test/v1"
    assert captured["api_key"] == "sk-2"


def test_set_llm_params_none_keeps_model_overrides():
    """None = 模型项保持现值：每轮 prefs 注入（温度/上限）不冲掉已切换的模型。"""
    agent = _bare_agent()
    agent.set_llm_params(model="m2", api_key="sk-2")
    agent.set_llm_params(temperature=0.3, max_tokens=77)  # worker _op_chat 每轮的注入形状
    assert agent._llm_overrides["model"] == "m2"
    assert agent._llm_overrides["api_key"] == "sk-2"
    assert agent._llm_temperature == 0.3
    assert agent._llm_max_tokens == 77


def test_set_llm_params_clear_mode_pops_missing_overrides():
    """清除模式：四键全量覆盖——None/空串 = 清除该项 override。

    场景：P 档案（key+ctx）切 Q 档案（无 key 无 ctx）——Q 显式带空 key
    清除，P 的 api_key/context_window 不得残留继续生效。
    """
    agent = _bare_agent()
    agent.set_llm_params(model="p-m", base_url="http://p.test/v1",
                         api_key="sk-p", context_window=9999)
    agent.set_llm_params(clear_model_overrides=True, model="q-m",
                         base_url="http://q.test/v1", api_key="",
                         context_window=None)
    assert agent._llm_overrides == {"model": "q-m", "base_url": "http://q.test/v1"}
    assert agent._llm_client is None


def test_set_llm_params_clear_mode_back_to_default_empties_all():
    """回默认：显式清除通道把四个 override 全清空（回落 settings/config）。"""
    agent = _bare_agent()
    agent.set_llm_params(model="p-m", base_url="http://p.test/v1",
                         api_key="sk-p", context_window=9999)
    agent.set_llm_params(clear_model_overrides=True, model=None, base_url=None,
                         api_key=None, context_window=None)
    assert agent._llm_overrides == {}


def test_set_llm_params_clear_mode_temperature_still_default_semantics():
    """清除模式只作用于四个模型键：temperature/max_tokens 仍是 None=默认。"""
    agent = _bare_agent()
    agent.set_llm_params(temperature=0.3)
    agent.set_llm_params(clear_model_overrides=True, model="m",
                         temperature=None, max_tokens=None)
    assert agent._llm_temperature is None and agent._llm_max_tokens is None
    assert agent._llm_overrides == {"model": "m"}


def test_model_override_change_invalidates_cached_client(monkeypatch):
    """覆盖变更 → 缓存客户端作废按新模型重建；未变时复用缓存。"""
    import src.agent.agent_v3 as v3mod

    built = []

    class FakeLLMClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            built.append(self)

    monkeypatch.setattr(v3mod, "LLMClient", FakeLLMClient)
    agent = _bare_agent()
    agent.set_llm_params(model="m1")
    c1 = agent._get_llm_client()
    assert agent._get_llm_client() is c1          # 覆盖未变 → 复用
    agent.set_llm_params(model="m2")
    c2 = agent._get_llm_client()
    assert c2 is not c1 and c2.kwargs["model"] == "m2"
    assert len(built) == 2


def test_compact_threshold_uses_context_window_override(monkeypatch):
    """压缩阈值：context_window 覆盖优先于 settings/自动探测（get_context_window）。"""
    agent = _bare_agent()
    agent.settings = SimpleNamespace(max_tokens=0)

    import src.agent.token_counter as tc_mod
    import src.agent.context_window as cw_mod
    monkeypatch.setattr(tc_mod, "count_tokens", lambda messages: 700)
    monkeypatch.setattr(cw_mod, "get_context_window", lambda: 10 ** 9)  # 兜底（本测试不该被用到）

    messages = [{"role": "user", "content": "x"}]
    agent.set_llm_params(context_window=1000)
    assert agent._compact_threshold_hit(messages, 80) is False   # 阈值 1000×80%=800 > 700
    agent.set_llm_params(context_window=800)
    assert agent._compact_threshold_hit(messages, 80) is True    # 阈值 640 ≤ 700

    # 无覆盖 → 回落 get_context_window（此处 stub 成超大值 → 永不压缩）
    agent._llm_overrides.pop("context_window")
    assert agent._compact_threshold_hit(messages, 80) is False


# ════════════════════════════════════════════════════════════════
# worker_process：llm_params_set op（主循环 + 内联）
# ════════════════════════════════════════════════════════════════

class _RecordingAgent:
    """记录 set_llm_params 调用的假 agent。"""

    def __init__(self):
        self.calls = []

    def set_llm_params(self, **kwargs):
        self.calls.append(kwargs)


@pytest.fixture
def sent(monkeypatch):
    """捕获 worker 的 _send 输出。"""
    from web_fastapi import worker_process as wp
    out = []
    monkeypatch.setattr(wp, "_send", lambda msg, **k: out.append(msg))
    return out


def test_llm_params_set_op_filters_none_and_reports(sent):
    from web_fastapi import worker_process as wp

    state = wp.WorkerState("local", slot="main")
    state.agent = _RecordingAgent()
    wp.handle_command(state, {"id": "r1", "op": "llm_params_set",
                              "model": "m2", "base_url": None,
                              "api_key": "sk-9", "context_window": 8192})
    # None 项被过滤，不传给 set_llm_params（默认模式 None=保持现值）
    assert state.agent.calls == [{"clear_model_overrides": False,
                                  "model": "m2", "api_key": "sk-9",
                                  "context_window": 8192}]
    assert sent[-1]["type"] == "result"
    assert sent[-1]["data"]["restart_required"] is False
    assert sent[-1]["data"]["applied"] == ["api_key", "context_window", "model"]


def test_llm_params_set_clear_mode_passes_none_through(sent):
    """清除模式：None/空串值透传给 set_llm_params（显式清除该项 override）。"""
    from web_fastapi import worker_process as wp

    state = wp.WorkerState("local", slot="main")
    state.agent = _RecordingAgent()
    wp.handle_command(state, {"id": "r1", "op": "llm_params_set",
                              "clear_model_overrides": True,
                              "model": "m2", "base_url": "http://x/v1",
                              "api_key": "", "context_window": None})
    assert state.agent.calls == [{"clear_model_overrides": True,
                                  "model": "m2", "base_url": "http://x/v1",
                                  "api_key": "", "context_window": None}]
    # 清除项也计入 applied（对账可见 context_window 被清除）
    assert sent[-1]["data"]["applied"] == ["api_key", "base_url",
                                           "context_window", "model"]


def test_llm_params_set_op_does_not_touch_prefs(sent):
    """api_key 经 IPC 注入属既有信任域，但不得进 prefs（prefs_get 回显会泄漏）。"""
    from web_fastapi import worker_process as wp

    state = wp.WorkerState("local", slot="main")
    state.agent = _RecordingAgent()
    before = dict(state.prefs)
    wp.handle_command(state, {"id": "r1", "op": "llm_params_set",
                              "model": "m2", "api_key": "sk-secret"})
    assert state.prefs == before
    assert "api_key" not in state.prefs

    # prefs_get 回显不含 api_key
    wp.handle_command(state, {"id": "r2", "op": "prefs_get"})
    assert sent[-1]["type"] == "result"
    assert "api_key" not in sent[-1]["data"]["prefs"]


def test_llm_params_set_inline_during_chat(sent):
    """chat 进行中的内联路径同样生效（下一轮 LLM 调用按新模型重建）。"""
    from web_fastapi import worker_process as wp

    state = wp.WorkerState("local", slot="main")
    state.agent = _RecordingAgent()
    assert "llm_params_set" in wp._INLINE_CMDS
    wp._handle_inline_cmd(state, "llm_params_set", "r2",
                          {"id": "r2", "op": "llm_params_set", "model": "m3"})
    assert state.agent.calls == [{"clear_model_overrides": False, "model": "m3"}]
    assert sent[-1]["data"]["restart_required"] is False


def test_llm_params_keys_defined_once():
    """Fix9：_LLM_PARAMS_KEYS 全模块单处定义（曾重复两份易漂移）。"""
    import inspect
    from web_fastapi import worker_process as wp
    src = inspect.getsource(wp)
    assert src.count("_LLM_PARAMS_KEYS = ") == 1
    assert set(wp._LLM_PARAMS_KEYS) == {"model", "base_url", "api_key",
                                        "context_window"}


# ════════════════════════════════════════════════════════════════
# worker_manager：broadcast_llm_params
# ════════════════════════════════════════════════════════════════

class _FakeSlotWorker:
    """duck-typed WorkerProcess：记录广播调用（ok=False 模拟写入失败）。"""

    def __init__(self, alive=True, ok=True):
        self._alive = alive
        self._ok = ok
        self.calls = []

    def is_alive(self):
        return self._alive

    def send_fire_and_forget(self, op, **kwargs):
        self.calls.append((op, kwargs))
        if not self._ok:
            raise RuntimeError("stdin 写入失败")
        return True


def _manager(tmp_path):
    from web_fastapi.worker_manager import WorkerManager
    return WorkerManager(max_parallel=2, state_path=tmp_path / "web_state.json")


def test_broadcast_llm_params_reaches_all_live_workers(tmp_path):
    m = _manager(tmp_path)
    a = _FakeSlotWorker()
    b = _FakeSlotWorker()
    dead = _FakeSlotWorker(alive=False)
    m._workers = {("local", "s1"): a, ("local", "main"): b, ("local", "dead"): dead}

    delivered = m.broadcast_llm_params(model="m9", base_url=None)  # None 项不下发

    assert delivered == 2
    assert a.calls == [("llm_params_set", {"clear_model_overrides": False,
                                           "model": "m9"})]
    assert b.calls == a.calls
    assert dead.calls == []                        # 死实例跳过


def test_broadcast_llm_params_single_failure_does_not_stop_others(tmp_path):
    m = _manager(tmp_path)
    bad = _FakeSlotWorker(ok=False)
    good = _FakeSlotWorker()
    m._workers = {("local", "a"): bad, ("local", "b"): good}

    delivered = m.broadcast_llm_params(model="m1", api_key="sk-1")

    assert delivered == 1                          # 单实例失败不影响其余
    assert good.calls == [("llm_params_set", {"clear_model_overrides": False,
                                              "model": "m1", "api_key": "sk-1"})]


def test_broadcast_llm_params_clear_mode_keeps_none(tmp_path):
    """清除模式：None/空串不被过滤、flag 透传——worker 侧语义=显式清除。"""
    m = _manager(tmp_path)
    a = _FakeSlotWorker()
    m._workers = {("local", "a"): a}

    delivered = m.broadcast_llm_params(
        clear_model_overrides=True, model="m1", base_url="http://x/v1",
        api_key="", context_window=None)

    assert delivered == 1
    assert a.calls == [("llm_params_set", {
        "clear_model_overrides": True, "model": "m1",
        "base_url": "http://x/v1", "api_key": "", "context_window": None})]


def test_broadcast_llm_params_queued_for_spawning_slot(tmp_path):
    """Fix3 spawn 窗口竞态：在途 spawn 的槽挂队补发，不漏收广播。"""
    import threading
    m = _manager(tmp_path)
    live = _FakeSlotWorker()
    m._workers = {("local", "main"): live}
    m._spawning[("local", "s1")] = threading.Event()   # s1 正在 spawn

    m.broadcast_llm_params(clear_model_overrides=True, model="m9",
                           base_url=None, api_key="", context_window=None)

    # 存量实例即时送达 + spawning 占位挂队
    assert live.calls[0][1]["model"] == "m9"
    assert m._pending_broadcasts[("local", "s1")] == [
        ("llm_params_set", {"clear_model_overrides": True, "model": "m9",
                            "base_url": None, "api_key": "",
                            "context_window": None})]

    # spawn 落地后补发（get_or_create 注册完成 → 镜像重放之后）
    newborn = _FakeSlotWorker()
    m._replay_pending_broadcasts(("local", "s1"), newborn)
    assert len(newborn.calls) == 1
    assert newborn.calls[0][0] == "llm_params_set"
    assert newborn.calls[0][1]["model"] == "m9"
    assert newborn.calls[0][1]["clear_model_overrides"] is True
    # 补发后队清空（不重复）
    assert ("local", "s1") not in m._pending_broadcasts


def test_broadcast_llm_params_does_not_persist_mirror(tmp_path):
    """模型参数不进 web_state.json 镜像（api_key 不落盘；持久真相源是 config.yaml）。"""
    m = _manager(tmp_path)
    m.broadcast_llm_params(model="m1", api_key="sk-1")
    assert m._mirror_prefs is None and m._mirror_perm is None
    assert not (tmp_path / "web_state.json").exists()


# ════════════════════════════════════════════════════════════════
# config.reload_settings
# ════════════════════════════════════════════════════════════════

def test_reload_settings_rereads_yaml(tmp_path, monkeypatch):
    """重读 config.yaml：改模型后 reload 生效；旧引用保持旧值快照。"""
    import config

    cfg = tmp_path / "config.yaml"
    cfg.write_text("llm_model_name: model-a\n", encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    try:
        stale = config.settings  # import 时绑定的真实配置单例（旧引用快照）
        config.get_settings.cache_clear()
        assert config.get_settings().llm_model_name == "model-a"
        assert config.settings is stale          # reload 前旧引用保持旧值

        cfg.write_text("llm_model_name: model-b\n", encoding="utf-8")
        assert config.get_settings().llm_model_name == "model-a"  # 缓存未失效

        fresh = config.reload_settings()
        assert fresh.llm_model_name == "model-b"
        assert config.settings.llm_model_name == "model-b"
        assert config.get_settings() is fresh
        assert config.get_settings().llm_model_name == "model-b"
    finally:
        # 还原真实配置单例，避免污染其他测试
        monkeypatch.undo()
        config.get_settings.cache_clear()
        config.reload_settings()


# ════════════════════════════════════════════════════════════════
# PUT /api/config/system：热切换传播 + restart_required 语义
# ════════════════════════════════════════════════════════════════

def _web_put(monkeypatch, old_cfg, updates):
    """掩码 config 读写的 TestClient 内执行一次 PUT /system。

    返回 (响应 json, 广播参数列表, reload 次数)。广播桩挂在 lifespan
    创建的 worker_manager 实例上（同一 with 块内替换，避免重启被换掉）。
    """
    from fastapi.testclient import TestClient
    from web_fastapi.app import create_app
    import web_fastapi.routers.config_router as cr

    monkeypatch.setattr(cr, "load_yaml", lambda: old_cfg)
    monkeypatch.setattr(cr, "save_yaml", lambda d: None)
    reloads = []
    monkeypatch.setattr(cr, "reload_settings", lambda: (
        reloads.append(True),
        {"llm_model_name": "model-b", "openai_base_url": "http://new.test/v1"},
    )[1])

    app = create_app()
    broadcasts = []
    settings_syncs = []
    with TestClient(app) as c:
        c.post("/api/auth/login", json={})
        app.state.worker_manager.broadcast_llm_params = (
            lambda **params: broadcasts.append(params) or 2)
        app.state.worker_manager.broadcast_settings_updates = (
            lambda updates: settings_syncs.append(dict(updates)) or len(updates))
        r = c.put("/api/config/system", json={"updates": updates})
    return r.json(), broadcasts, reloads, settings_syncs


def test_put_system_hot_model_keys_reload_and_broadcast(monkeypatch):
    """模型三项 + shell 键混提：reload + 广播（api_key 变化才传）；restart 语义做准到键。

    广播为清除模式 + context_window=None：全局配置是新的真相源，档案切换
    残留的窗口覆盖不得遮蔽 config.yaml 的 model_context_window。
    """
    j, broadcasts, reloads, syncs = _web_put(
        monkeypatch,
        {"llm_model_name": "model-a", "openai_base_url": "http://old.test/v1",
         "openai_api_key": "sk-old"},
        {"llm_model_name": "model-b",
         "openai_base_url": "http://new.test/v1",
         "openai_api_key": "sk-new",
         "shell_enabled": "true"})
    assert reloads == [True]
    assert broadcasts == [{"clear_model_overrides": True,
                           "model": "model-b",
                           "base_url": "http://new.test/v1",
                           "context_window": None,
                           "api_key": "sk-new"}]
    # 全键热生效：shell_enabled 亦即时同步（无重启键）；模型三项另有专用通道
    assert j["restart_required"] is False
    assert j["restart_required_keys"] == []
    assert j["applied_immediately"] == ["llm_model_name", "openai_api_key", "openai_base_url", "shell_enabled"]
    assert any(s.get("shell_enabled") == "true" and s.get("llm_model_name") == "model-b"
               for s in syncs)


def test_put_system_api_key_unchanged_not_broadcast(monkeypatch):
    """api_key 同值重存：不下发 api_key（不打扰 worker、不作废其客户端缓存）。"""
    j, broadcasts, reloads, _syncs = _web_put(
        monkeypatch, {"openai_api_key": "sk-same"},
        {"openai_api_key": "sk-same", "llm_model_name": "model-b"})
    assert reloads == [True]
    assert broadcasts == [{"clear_model_overrides": True,
                           "model": "model-b",
                           "base_url": "http://new.test/v1",
                           "context_window": None}]
    assert j["restart_required"] is False   # 只有模型项 → 无需重启


def test_put_system_masked_key_skip_means_no_key_broadcast(monkeypatch):
    """掩码值被跳过（未写入）→ 广播不含 api_key；llm_timeout 热同步无需重启。"""
    j, broadcasts, reloads, syncs = _web_put(
        monkeypatch, {"openai_api_key": "sk-real-123"},
        {"openai_api_key": "sk-r****----",     # GET 回显的掩码值原样回传
         "llm_model_name": "model-b",
         "llm_timeout": "240"})
    assert "openai_api_key" in j["skipped_masked"]
    assert reloads == [True]
    assert broadcasts == [{"clear_model_overrides": True,
                           "model": "model-b",
                           "base_url": "http://new.test/v1",
                           "context_window": None}]
    assert j["restart_required"] is False
    assert j["restart_required_keys"] == []
    assert any(s.get("llm_timeout") == "240" for s in syncs)


def test_put_system_non_model_keys_hot_sync_without_llm_broadcast(monkeypatch):
    """不含模型三项的更新：重读 settings + 热同步到 worker；不广播模型参数。"""
    j, broadcasts, reloads, syncs = _web_put(monkeypatch, {}, {"shell_enabled": "true"})
    assert reloads == [True] and broadcasts == []
    assert syncs == [{"shell_enabled": "true"}]
    assert j["restart_required_keys"] == []


# ════════════════════════════════════════════════════════════════
# CLI /model 命令
# ════════════════════════════════════════════════════════════════

class _FakeModelAgent:
    """记录 set_llm_params 的假 agent（_llm_overrides 随调用更新）。"""

    def __init__(self):
        self._llm_overrides = {}
        self.calls = []

    def set_llm_params(self, **kwargs):
        self.calls.append(kwargs)
        self._llm_overrides.update(kwargs)


def _write_profiles(root, profiles):
    home = root / "home"
    home.mkdir(exist_ok=True)
    (home / "model_profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False), encoding="utf-8")


def _printed(console, needle):
    for call in console.print.call_args_list:
        for arg in call.args:
            if isinstance(arg, str) and needle in arg:
                return True
    return False


def test_cmd_model_switch_resolves_profile_and_injects(tmp_path):
    """`/model <id>`：从 model_profiles.json 解析档案后注入 set_llm_params。"""
    from src import cli
    from src.storage import paths

    paths.set_data_root(tmp_path)
    try:
        _write_profiles(tmp_path, [
            {"id": "deep", "display": "DeepSeek", "model": "deepseek-v3"},
            {"id": "qwen", "display": "Qwen", "model": "qwen3",
             "base_url": "http://q.test/v1", "api_key": "sk-q",
             "context_window": 32768},
        ])
        agent = _FakeModelAgent()
        with patch.object(cli, "console"):
            cli._cmd_model(agent, "qwen")
        # 四键全量 + clear：省略键显式清除，防跨档案 key/ctx 残留
        assert agent.calls == [{"model": "qwen3", "base_url": "http://q.test/v1",
                                "api_key": "sk-q", "context_window": 32768,
                                "clear_model_overrides": True}]
    finally:
        paths.set_data_root(None)


def test_cmd_model_unknown_id_gives_clear_error(tmp_path):
    from src import cli
    from src.storage import paths

    paths.set_data_root(tmp_path)
    try:
        _write_profiles(tmp_path, [{"id": "deep", "display": "D", "model": "m"}])
        agent = _FakeModelAgent()
        with patch.object(cli, "console") as mock_console:
            cli._cmd_model(agent, "ghost")
        assert agent.calls == []
        assert _printed(mock_console, "模型档案不存在: ghost")
    finally:
        paths.set_data_root(None)


def test_cmd_model_no_arg_lists_current_and_profiles(tmp_path):
    """无参：列出当前模型 + 档案清单（rich Table），不触发切换。"""
    import io

    from rich.console import Console
    from rich.table import Table

    from src import cli
    from src.storage import paths

    paths.set_data_root(tmp_path)
    try:
        _write_profiles(tmp_path, [{"id": "deep", "display": "DeepSeek", "model": "deepseek-v3"}])
        agent = _FakeModelAgent()
        agent.set_llm_params(model="qwen3")   # 已有覆盖 → 显示覆盖值
        with patch.object(cli, "console") as mock_console:
            cli._cmd_model(agent, "")
        assert agent.calls == [{"model": "qwen3"}]
        assert _printed(mock_console, "当前模型")
        tables = [a for call in mock_console.print.call_args_list
                  for a in call.args if isinstance(a, Table)]
        assert len(tables) == 1
        buf = io.StringIO()
        Console(file=buf, width=120).print(tables[0])
        rendered = buf.getvalue()
        assert "deepseek-v3" in rendered
    finally:
        paths.set_data_root(None)


def test_main_dispatches_model_command(monkeypatch, tmp_path):
    """主循环挂载：`/model <id>` 经 main() 派发到 agent.set_llm_params。"""
    from src import cli
    from src.storage import paths
    from tests.cli.test_cli_history import _boot_main

    paths.set_data_root(tmp_path)
    try:
        _write_profiles(tmp_path, [{"id": "agent-x", "display": "X", "model": "model-x"}])
        calls = []

        class Agent:
            _llm_overrides = {}

            def set_llm_params(self, **kwargs):
                calls.append(kwargs)

        _boot_main(monkeypatch, responses=["/model agent-x", EOFError()],
                   agent_cls=lambda *a, **k: Agent())
        # 档案缺省键以空值/None 显式清除（clear 模式四键全量）
        assert calls == [{"model": "model-x", "base_url": "", "api_key": "",
                          "context_window": None, "clear_model_overrides": True}]
    finally:
        paths.set_data_root(None)
