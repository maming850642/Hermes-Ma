"""系统配置全键热生效（worker 侧）测试。

覆盖链路：WorkerManager.broadcast_settings_updates（None 过滤 / 全存活
实例送达 / 单实例失败隔离 / 空更新零发送）→ worker 的 settings_update op
（主循环 + chat 内联）→ settings 单例原地合并（agent 持有的旧引用同步
读到新值）→ model_context_window / max_short_term_messages 作废
get_context_window 的 lru_cache → shell_enabled 触发 load_builtin_tools
(force=True) + registry 重绑（bash 工具面随开关出现/消失；失败只告警）。
"""
import pytest


# ════════════════════════════════════════════════════════════════
# 共享桩件
# ════════════════════════════════════════════════════════════════

class _FakeSettings(dict):
    """镜像 config._Settings：支持属性访问的 dict（原地合并语义一致）。"""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"config key not found: {name}") from None

    def __setattr__(self, name, value):
        self[name] = value


class _RecordingAgent:
    """记录 rebind_tools / set_llm_params 调用的假 agent（持有 settings 引用，
    模拟 agent_v3 构造期绑定的 self.settings）。"""

    def __init__(self):
        self.rebind_calls = 0
        self.settings = None

    def rebind_tools(self):
        self.rebind_calls += 1

    def set_llm_params(self, **kwargs):
        pass


@pytest.fixture
def sent(monkeypatch):
    """捕获 worker 的 _send 输出。"""
    from web_fastapi import worker_process as wp
    out = []
    monkeypatch.setattr(wp, "_send", lambda msg, **k: out.append(msg))
    return out


@pytest.fixture
def fake_settings(monkeypatch):
    """替换 config.get_settings 单例为可控假对象（测试结束自动还原）。"""
    import config
    fake = _FakeSettings({
        "shell_timeout": 30,
        "shell_enabled": False,
        "model_context_window": 0,
        "max_short_term_messages": 20,
    })
    monkeypatch.setattr(config, "get_settings", lambda: fake)
    return fake


# ════════════════════════════════════════════════════════════════
# worker_process：settings_update op（主循环）
# ════════════════════════════════════════════════════════════════

def test_settings_update_merges_inplace_agent_reads_new_value(sent, fake_settings):
    """updates 原地合并进 settings 单例；agent 构造期持有的旧引用同步读到新值。"""
    from web_fastapi import worker_process as wp

    state = wp.WorkerState("local", slot="main")
    state.agent = _RecordingAgent()
    state.agent.settings = fake_settings  # 旧引用（reload 语义下应是旧快照）
    assert fake_settings.shell_timeout == 30

    wp.handle_command(state, {"id": "r1", "op": "settings_update",
                              "updates": {"shell_timeout": 120,
                                          "memory_min_score": 0.6}})

    # 字典与属性访问都读到新值；旧引用是同一对象 → 同步生效（原地合并）
    assert fake_settings["shell_timeout"] == 120
    assert fake_settings.shell_timeout == 120
    assert state.agent.settings.shell_timeout == 120
    assert state.agent.settings.memory_min_score == 0.6
    # ack：{"ok": true, "applied": sorted(updates)}
    assert sent[-1]["type"] == "result"
    assert sent[-1]["data"]["ok"] is True
    assert sent[-1]["data"]["restart_required"] is False
    assert sent[-1]["data"]["applied"] == ["memory_min_score", "shell_timeout"]


def test_settings_update_filters_none_values(sent, fake_settings):
    """None 值不下发不合并（清掉的键不覆盖现值）。"""
    from web_fastapi import worker_process as wp

    state = wp.WorkerState("local", slot="main")
    state.agent = _RecordingAgent()
    wp.handle_command(state, {"id": "r1", "op": "settings_update",
                              "updates": {"shell_timeout": None,
                                          "llm_timeout": 240}})
    # None 项被过滤：现值 30 不被覆盖；其余键正常合并
    assert fake_settings.shell_timeout == 30
    assert fake_settings.llm_timeout == 240
    assert sent[-1]["data"]["applied"] == ["llm_timeout"]


def test_settings_update_empty_updates_acks_empty_applied(sent, fake_settings):
    """空/None 更新：不合并、ack applied=[]（不崩）。"""
    from web_fastapi import worker_process as wp

    state = wp.WorkerState("local", slot="main")
    state.agent = _RecordingAgent()
    wp.handle_command(state, {"id": "r1", "op": "settings_update",
                              "updates": {"shell_timeout": None}})
    wp.handle_command(state, {"id": "r2", "op": "settings_update"})
    assert sent[-1]["data"]["applied"] == []
    assert sent[-2]["data"]["applied"] == []


# ════════════════════════════════════════════════════════════════
# worker_process：context_window lru_cache 作废
# ════════════════════════════════════════════════════════════════

@pytest.fixture
def cache_clears(monkeypatch):
    """替换 context_window.get_context_window，记录 cache_clear 调用。"""
    import src.agent.context_window as cw_mod
    clears = []

    def _fake():
        return 32768

    _fake.cache_clear = lambda: clears.append(True)
    monkeypatch.setattr(cw_mod, "get_context_window", _fake)
    return clears


def _run_update(wp, state, updates):
    wp.handle_command(state, {"id": "r1", "op": "settings_update",
                              "updates": updates})


def test_model_context_window_key_clears_cache(sent, fake_settings, cache_clears):
    from web_fastapi import worker_process as wp

    state = wp.WorkerState("local", slot="main")
    state.agent = _RecordingAgent()
    _run_update(wp, state, {"model_context_window": 131072})
    assert cache_clears == [True]
    assert fake_settings.model_context_window == 131072


def test_max_short_term_messages_key_clears_cache(sent, fake_settings, cache_clears):
    from web_fastapi import worker_process as wp

    state = wp.WorkerState("local", slot="main")
    state.agent = _RecordingAgent()
    _run_update(wp, state, {"max_short_term_messages": 40})
    assert cache_clears == [True]
    assert fake_settings.max_short_term_messages == 40


def test_llm_model_name_key_clears_cache(sent, fake_settings, cache_clears):
    """Fix7：模型名变化 → 探测目标变化（detect_context_window 匹配 /models
    响应里的模型 id）→ 缓存必须作废。"""
    from web_fastapi import worker_process as wp

    state = wp.WorkerState("local", slot="main")
    state.agent = _RecordingAgent()
    _run_update(wp, state, {"llm_model_name": "qwen3-32b"})
    assert cache_clears == [True]
    assert fake_settings.llm_model_name == "qwen3-32b"


def test_openai_base_url_key_clears_cache(sent, fake_settings, cache_clears):
    """Fix7：base_url 变化 → 自动探测端点变化 → 缓存必须作废。"""
    from web_fastapi import worker_process as wp

    state = wp.WorkerState("local", slot="main")
    state.agent = _RecordingAgent()
    _run_update(wp, state, {"openai_base_url": "http://new-host/v1"})
    assert cache_clears == [True]
    assert fake_settings.openai_base_url == "http://new-host/v1"


def test_plain_key_does_not_clear_cache(sent, fake_settings, cache_clears):
    """与窗口无关的键不清缓存（shell_timeout 等）。"""
    from web_fastapi import worker_process as wp

    state = wp.WorkerState("local", slot="main")
    state.agent = _RecordingAgent()
    _run_update(wp, state, {"shell_timeout": 90})
    assert cache_clears == []


# ════════════════════════════════════════════════════════════════
# worker_process：shell_enabled → 工具面重载
# ════════════════════════════════════════════════════════════════

@pytest.fixture
def tool_loads(monkeypatch):
    """替换 loader.load_builtin_tools，记录 (force) 调用。"""
    import src.tools.loader as loader_mod
    calls = []
    monkeypatch.setattr(loader_mod, "load_builtin_tools",
                        lambda force=False: calls.append(force))
    return calls


def test_shell_enabled_triggers_tool_reload_and_rebind(sent, fake_settings, tool_loads):
    """shell_enabled → load_builtin_tools(force=True) + registry 重绑。"""
    from web_fastapi import worker_process as wp

    state = wp.WorkerState("local", slot="main")
    state.agent = _RecordingAgent()
    _run_update(wp, state, {"shell_enabled": True})

    assert tool_loads == [True]                    # 强制重扫（作废 loader 缓存）
    assert state.agent.rebind_calls == 1           # registry 重绑（对齐 mcp_reload）
    assert fake_settings.shell_enabled is True


def test_shell_reload_failure_still_acks(sent, fake_settings, monkeypatch):
    """loader 强制重载抛异常：只告警不崩，ack 仍 ok（applied 完整）。"""
    import src.tools.loader as loader_mod
    from web_fastapi import worker_process as wp

    def _boom(force=False):
        raise RuntimeError("rescan failed")

    monkeypatch.setattr(loader_mod, "load_builtin_tools", _boom)

    state = wp.WorkerState("local", slot="main")
    state.agent = _RecordingAgent()
    _run_update(wp, state, {"shell_enabled": True, "shell_timeout": 60})

    assert sent[-1]["type"] == "result"
    assert sent[-1]["data"]["ok"] is True
    assert sent[-1]["data"]["applied"] == ["shell_enabled", "shell_timeout"]


def test_rebind_failure_still_acks(sent, fake_settings, tool_loads):
    """registry 重绑失败：只告警（loader 重载已生效），ack 不受影响。"""
    from web_fastapi import worker_process as wp

    state = wp.WorkerState("local", slot="main")
    agent = _RecordingAgent()

    def _boom():
        raise RuntimeError("rebind failed")

    agent.rebind_tools = _boom
    state.agent = agent
    _run_update(wp, state, {"shell_enabled": True})

    assert tool_loads == [True]
    assert sent[-1]["data"]["ok"] is True
    assert sent[-1]["data"]["applied"] == ["shell_enabled"]


# ════════════════════════════════════════════════════════════════
# worker_process：chat 进行中的内联路径
# ════════════════════════════════════════════════════════════════

def test_settings_update_inline_during_chat(sent, fake_settings, cache_clears):
    """settings_update 与 llm_params_set 同属内联命令：chat 事件间隙即时生效。"""
    from web_fastapi import worker_process as wp

    assert "settings_update" in wp._INLINE_CMDS

    state = wp.WorkerState("local", slot="main")
    state.agent = _RecordingAgent()
    state.agent.settings = fake_settings
    wp._handle_inline_cmd(state, "settings_update", "r2",
                          {"id": "r2", "op": "settings_update",
                           "updates": {"shell_enabled": True,
                                       "model_context_window": 65536}})
    assert fake_settings.shell_enabled is True
    assert state.agent.settings.shell_enabled is True
    assert cache_clears == [True]                  # 内联路径同样清缓存
    assert sent[-1]["data"]["ok"] is True
    assert sent[-1]["data"]["applied"] == ["model_context_window", "shell_enabled"]


# ════════════════════════════════════════════════════════════════
# Fix1：布尔键字符串归一（'false' 恒真 → 真 False）
# ════════════════════════════════════════════════════════════════

def test_normalize_setting_value_matrix():
    """config.normalize_setting_value：布尔键归一矩阵 + 非清单键直通。"""
    from config import normalize_setting_value as nsv
    for truthy in ("true", "1", 1, True):
        assert nsv("shell_enabled", truthy) is True
    for falsy in ("false", "0", 0, False):
        assert nsv("shell_enabled", falsy) is False
    assert nsv("shell_enabled", "weird") == "weird"    # 未知值不猜
    assert nsv("session_persist", "false") is False
    assert nsv("log_to_file", "true") is True
    assert nsv("shell_timeout", "false") == "false"    # 非布尔键原样直通


def test_settings_update_bool_string_normalized(sent, fake_settings):
    """worker 合并前归一：shell_enabled='false' → False。

    （c）resolve 的 config_guard 门控按 getattr(settings, key) 真值过滤
    bash 工具——值必须是假布尔而非恒真的 'false' 字符串。"""
    from web_fastapi import worker_process as wp

    state = wp.WorkerState("local", slot="main")
    state.agent = _RecordingAgent()
    wp.handle_command(state, {"id": "r1", "op": "settings_update",
                              "updates": {"shell_enabled": "false",
                                          "shell_timeout": "60"}})
    assert fake_settings["shell_enabled"] is False          # 非 'false' 字符串
    assert fake_settings["shell_timeout"] == "60"           # 非清单键原样直通
    # 内联路径同一归一（'0' 也归一为 False）
    fake_settings["shell_enabled"] = "true"
    wp._handle_inline_cmd(state, "settings_update", "r2",
                          {"id": "r2", "op": "settings_update",
                           "updates": {"shell_enabled": "0"}})
    assert fake_settings["shell_enabled"] is False


def test_save_yaml_normalizes_bool_keys_before_write(tmp_path):
    """save_yaml 写盘归一：'false' 落盘为不带引号的 false（读回真布尔）。"""
    import yaml as yaml_mod
    from web_fastapi.services.config_service import load_yaml, save_yaml

    p = tmp_path / "config.yaml"
    p.write_text("shell_enabled: true\n", encoding="utf-8")
    save_yaml({"shell_enabled": "false", "session_persist": 0}, path=p)

    raw = p.read_text(encoding="utf-8")
    assert "shell_enabled: false" in raw          # 不带引号
    assert "'false'" not in raw and '"false"' not in raw
    cfg = load_yaml(p)
    assert cfg["shell_enabled"] is False
    assert cfg["session_persist"] is False


def test_get_settings_read_path_normalizes_bool_keys(tmp_path, monkeypatch):
    """yaml 读取侧兜底：存量带引号的 'true'/'false' 归一为真布尔。"""
    import config

    (tmp_path / "config.yaml").write_text(
        "shell_enabled: 'true'\nsession_persist: 'false'\nshell_timeout: '30'\n",
        encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    try:
        config.get_settings.cache_clear()
        s = config.get_settings()
        assert s["shell_enabled"] is True
        assert s["session_persist"] is False
        assert s["shell_timeout"] == 30            # _INT_KEYS 原有归一不受影响
    finally:
        monkeypatch.undo()
        config.get_settings.cache_clear()
        config.reload_settings()


def test_put_system_shell_enabled_false_end_to_end(tmp_path, monkeypatch,
                                                    sent, fake_settings):
    """Fix1 全链路：PUT /system shell_enabled='false' →（a）worker settings 读到
    False（b）yaml 落盘为不带引号的 false（c）门控值是假布尔（'false' 字符串
    恒真会放行 bash）。"""
    import yaml as yaml_mod
    from fastapi.testclient import TestClient
    from web_fastapi.app import create_app
    import web_fastapi.services.config_service as cs
    import web_fastapi.routers.config_router as cr
    from web_fastapi import worker_process as wp

    monkeypatch.setattr(cs, "PROJECT_ROOT", tmp_path)   # 真实 save_yaml → tmp
    (tmp_path / "config.yaml").write_text("shell_enabled: 'true'\n", encoding="utf-8")
    monkeypatch.setattr(cr, "reload_settings", lambda: {"llm_model_name": "m"})

    app = create_app()
    captured = []
    with TestClient(app) as c:
        c.post("/api/auth/login", json={})
        app.state.worker_manager.broadcast_settings_updates = (
            lambda updates: captured.append(dict(updates)) or len(updates))
        app.state.worker_manager.broadcast_llm_params = lambda **k: 0
        r = c.put("/api/config/system", json={"updates": {"shell_enabled": "false"}})
    assert r.status_code == 200

    # （b）落盘：不带引号的 false（yaml 真布尔），不再是恒真的 'false'
    raw = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "shell_enabled: false" in raw and "'false'" not in raw
    assert yaml_mod.safe_load(raw)["shell_enabled"] is False

    # 广播载荷确实出网（字符串形态，归一在 worker 咽喉）
    assert captured == [{"shell_enabled": "false"}]

    # （a）worker 合并后读到 False
    state = wp.WorkerState("local", slot="main")
    state.agent = _RecordingAgent()
    wp._apply_settings_update(state, {"updates": captured[0]})
    assert fake_settings.shell_enabled is False


# ════════════════════════════════════════════════════════════════
# worker_manager：broadcast_settings_updates
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


def test_broadcast_reaches_all_live_workers_with_updates_kwarg(tmp_path):
    """None 过滤后以 updates=... 形状送达全部存活实例；死实例跳过。"""
    m = _manager(tmp_path)
    a = _FakeSlotWorker()
    b = _FakeSlotWorker()
    dead = _FakeSlotWorker(alive=False)
    m._workers = {("local", "s1"): a, ("local", "main"): b, ("local", "dead"): dead}

    delivered = m.broadcast_settings_updates(
        {"shell_timeout": 120, "shell_enabled": None, "memory_min_score": 0.6})

    assert delivered == 2
    payload = {"shell_timeout": 120, "memory_min_score": 0.6}   # None 项被过滤
    assert a.calls == [("settings_update", {"updates": payload})]
    assert b.calls == [("settings_update", {"updates": payload})]
    assert dead.calls == []


def test_broadcast_single_failure_does_not_stop_others(tmp_path):
    """单实例写入失败只告警：其余实例照常送达，返回成功数。"""
    m = _manager(tmp_path)
    bad = _FakeSlotWorker(ok=False)
    good = _FakeSlotWorker()
    m._workers = {("local", "a"): bad, ("local", "b"): good}

    delivered = m.broadcast_settings_updates({"shell_timeout": 60})

    assert delivered == 1
    assert good.calls == [("settings_update", {"updates": {"shell_timeout": 60}})]


def test_broadcast_empty_or_none_updates_sends_nothing(tmp_path):
    """全 None / 空 dict / None：返回 0，零 IPC。"""
    m = _manager(tmp_path)
    a = _FakeSlotWorker()
    m._workers = {("local", "a"): a}

    assert m.broadcast_settings_updates({}) == 0
    assert m.broadcast_settings_updates(None) == 0
    assert m.broadcast_settings_updates({"shell_timeout": None}) == 0
    assert a.calls == []


def test_broadcast_does_not_persist_mirror(tmp_path):
    """系统配置不进 web_state.json 镜像（持久真相源是 config.yaml）。"""
    m = _manager(tmp_path)
    m.broadcast_settings_updates({"shell_timeout": 60})
    assert m._mirror_prefs is None and m._mirror_perm is None
    assert not (tmp_path / "web_state.json").exists()


def test_broadcast_payload_shape_matches_worker_op(tmp_path, sent, fake_settings,
                                                   cache_clears, tool_loads):
    """契约对齐：manager 发出的 kwargs 经 make_request 组包后，worker op 直接消费。"""
    from web_fastapi.ipc import make_request
    from web_fastapi import worker_process as wp

    m = _manager(tmp_path)
    a = _FakeSlotWorker()
    m._workers = {("local", "a"): a}
    m.broadcast_settings_updates({"shell_enabled": True, "model_context_window": 4096})

    op, kwargs = a.calls[0]
    cmd = make_request("req-test", op, **kwargs)
    assert cmd["op"] == "settings_update" and cmd["updates"] == {
        "shell_enabled": True, "model_context_window": 4096}

    state = wp.WorkerState("local", slot="main")
    state.agent = _RecordingAgent()
    wp.handle_command(state, cmd)
    assert fake_settings.shell_enabled is True
    assert fake_settings.model_context_window == 4096
    assert tool_loads == [True]
    assert cache_clears == [True]
    assert sent[-1]["data"]["applied"] == ["model_context_window", "shell_enabled"]


def test_broadcast_settings_queued_for_spawning_slot(tmp_path):
    """Fix3 spawn 窗口竞态：在途 spawn 的槽挂队，落地后补发不漏收。"""
    import threading

    m = _manager(tmp_path)
    live = _FakeSlotWorker()
    m._workers = {("local", "main"): live}
    m._spawning[("local", "s9")] = threading.Event()   # s9 正在 spawn

    m.broadcast_settings_updates({"shell_enabled": "false"})

    # 存量实例即时送达；spawning 占位挂队
    assert live.calls == [("settings_update", {"updates": {"shell_enabled": "false"}})]
    assert m._pending_broadcasts[("local", "s9")] == [
        ("settings_update", {"updates": {"shell_enabled": "false"}})]

    # spawn 落地（get_or_create 注册 + 镜像重放之后）补发
    newborn = _FakeSlotWorker()
    m._replay_pending_broadcasts(("local", "s9"), newborn)
    assert newborn.calls == [("settings_update", {"updates": {"shell_enabled": "false"}})]
    assert ("local", "s9") not in m._pending_broadcasts   # 补发后队清空


def test_pending_broadcasts_dropped_when_spawn_fails(tmp_path):
    """spawn 失败：挂队广播一并丢弃（补发非正确性依赖——新进程从
    config.yaml/镜像读最新值），不残留给下一次 spawn 误收。"""
    import threading

    m = _manager(tmp_path)
    m._spawning[("local", "dead")] = threading.Event()
    m._pending_broadcasts[("local", "dead")] = [("settings_update", {"updates": {}})]
    # 模拟 get_or_create 的 finally 清理路径
    with m._lock:
        m._spawning.pop(("local", "dead"), None)
        m._pending_broadcasts.pop(("local", "dead"), None)
    assert m._pending_broadcasts == {}
