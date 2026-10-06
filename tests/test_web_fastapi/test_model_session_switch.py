"""PUT /api/config/model（会话/全局模型热切换）测试。

不启动完整 create_app：构造仅挂 config_router 的精简 app，
worker_manager 用假对象；会话亲和的 chat_gate_worker 打桩，
验证路由选择、参数解析（档案/默认回退）、四键显式清除语义
（P 有 key+ctx → Q 无 key：Q 显式带空 key/None ctx 清除残留）、
即发即忘（不再抢 worker 锁）、伪造 sid 的 404 防护与掩码。
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.model_registry import add_profile
from src.storage import paths
from web_fastapi.routers import config_router

KEY = "sk-secret-key-0123456789abcdef"


@pytest.fixture(autouse=True)
def _isolate_data_root(tmp_path):
    paths.set_data_root(tmp_path)
    yield tmp_path
    paths.set_data_root(None)


class FakeWorker:
    """只实现 send_fire_and_forget——路由若误用 send（抢锁路径）会
    AttributeError，测试即失败（守护"即发即忘"语义）。"""

    def __init__(self):
        self.ops = []
        self.ff_ok = True

    def send_fire_and_forget(self, op, **kw):
        self.ops.append((op, kw))
        return self.ff_ok


class FakeManager:
    def __init__(self):
        self.broadcasts = []

    def broadcast_llm_params(self, **params):
        self.broadcasts.append(params)
        return sorted(params)


@pytest.fixture
def client(monkeypatch):
    a = FastAPI()
    mgr = FakeManager()
    a.state.worker_manager = mgr
    a.include_router(config_router.router, prefix="/api/config", tags=["config"])
    c = TestClient(a)
    c._mgr = mgr
    # 会话亲和打桩：记录目标 worker
    c._slot_workers = {}

    def _fake_gate(request, sid):
        if sid not in c._slot_workers:
            c._slot_workers[sid] = FakeWorker()
        return c._slot_workers[sid]

    monkeypatch.setattr(config_router, "chat_gate_worker", _fake_gate)
    # 默认放行会话存在性校验（既有会话场景）；草稿/伪造测试恢复真实现
    c._real_session_exists = config_router._session_exists
    monkeypatch.setattr(config_router, "_session_exists",
                        lambda request, sid: True)
    yield c


def _seed_profile(profile_id="qwen-local", **kw):
    fields = dict(display="Qwen 本地", model="Qwen2.5-14B",
                  base_url="http://192.168.1.9:8000/v1", api_key=KEY,
                  context_window=32768, profile_id=profile_id)
    fields.update(kw)
    add_profile(**fields)
    return profile_id


def test_switch_with_session_routes_to_slot_worker(client):
    pid = _seed_profile()
    r = client.put("/api/config/model", json={"session_id": "s1", "profile_id": pid})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["scope"] == "session" and body["session_id"] == "s1"
    worker = client._slot_workers["s1"]
    assert worker.ops and worker.ops[0][0] == "llm_params_set"
    kw = worker.ops[0][1]
    assert kw["model"] == "Qwen2.5-14B"
    assert kw["base_url"] == "http://192.168.1.9:8000/v1"
    assert kw["api_key"] == KEY
    assert kw["context_window"] == 32768
    assert kw["session_id"] == "s1"
    # 显式清除语义：四键全量 + clear 标记
    assert kw["clear_model_overrides"] is True
    assert body["applied"] == ["api_key", "base_url", "context_window", "model"]
    assert client._mgr.broadcasts == []  # 会话切换不广播


def test_switch_response_never_contains_api_key(client):
    pid = _seed_profile()
    r = client.put("/api/config/model", json={"session_id": "s1", "profile_id": pid})
    assert KEY not in r.text


def test_switch_global_broadcasts(client):
    pid = _seed_profile()
    r = client.put("/api/config/model", json={"profile_id": pid})
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == "global"
    assert len(client._mgr.broadcasts) == 1
    bc = client._mgr.broadcasts[0]
    assert bc["model"] == "Qwen2.5-14B"
    assert bc["clear_model_overrides"] is True
    assert client._slot_workers == {}


def test_switch_default_falls_back_to_settings_and_clears(client, monkeypatch):
    """回默认分支：四键全量——api_key 无则空串（清除）、context_window 恒 None
    （清除，回落 settings.model_context_window / 自动探测）。"""
    class FakeSettings:
        def get(self, k, d=None):
            return {"llm_model_name": "cfg-model", "openai_base_url": "http://cfg/v1",
                    "openai_api_key": "sk-cfg"}.get(k, d)
    monkeypatch.setattr("config.get_settings", FakeSettings)
    r = client.put("/api/config/model", json={"profile_id": ""})
    assert r.status_code == 200
    bc = client._mgr.broadcasts[0]
    assert bc["model"] == "cfg-model"
    assert bc["base_url"] == "http://cfg/v1"
    assert bc["api_key"] == "sk-cfg"
    assert bc["context_window"] is None          # 显式清除档案窗口覆盖
    assert bc["clear_model_overrides"] is True


def test_switch_default_without_config_key_sends_empty_string(client, monkeypatch):
    """config 无 key：空串 = 清除（None 会被广播层过滤，空串才能送达清除语义）。"""
    class FakeSettings:
        def get(self, k, d=None):
            return {"llm_model_name": "cfg-model",
                    "openai_base_url": "http://cfg/v1"}.get(k, d)
    monkeypatch.setattr("config.get_settings", FakeSettings)
    client.put("/api/config/model", json={"profile_id": ""})
    bc = client._mgr.broadcasts[0]
    assert bc["api_key"] == ""


def test_switch_to_keyless_profile_clears_previous_key(client):
    """Fix2 跨档案残留：P（key+ctx）→ Q（无 key 无 ctx）——Q 显式带空 key/
    None ctx 清除，P 的凭据不得残留发给 Q 的服务端。"""
    _seed_profile("p-prof", display="P", model="p-m", base_url="http://p/v1",
                  api_key="sk-p", context_window=8192)
    _seed_profile("q-prof", display="Q", model="q-m", base_url="http://q/v1",
                  api_key="", context_window=None)
    client.put("/api/config/model", json={"session_id": "s1", "profile_id": "p-prof"})
    r = client.put("/api/config/model", json={"session_id": "s1", "profile_id": "q-prof"})
    assert r.status_code == 200
    kw = client._slot_workers["s1"].ops[-1][1]
    assert kw["model"] == "q-m"
    assert kw["base_url"] == "http://q/v1"       # base_url 是 Q 的
    assert kw["api_key"] == ""                   # P 的 key 被显式清除
    assert kw["context_window"] is None          # P 的 ctx 被显式清除
    assert kw["clear_model_overrides"] is True


def test_switch_unknown_profile_404(client):
    r = client.put("/api/config/model", json={"session_id": "s1", "profile_id": "nope"})
    assert r.status_code == 404


def test_switch_session_slots_full_429(client, monkeypatch):
    from web_fastapi.worker_manager import SlotsFullError

    def _full(request, sid):
        raise SlotsFullError("并发会话已满")

    monkeypatch.setattr(config_router, "chat_gate_worker", _full)
    r = client.put("/api/config/model", json={"session_id": "s1", "profile_id": ""})
    assert r.status_code == 429


def test_switch_session_worker_down_reports_not_ok(client):
    """fire-and-forget 写入失败（worker 已死）：ok=False + applied=[]。"""
    _seed_profile()
    r = client.put("/api/config/model", json={"session_id": "s1",
                                              "profile_id": "qwen-local"})
    assert r.status_code == 200
    client._slot_workers["s1"].ff_ok = False
    r2 = client.put("/api/config/model", json={"session_id": "s1",
                                               "profile_id": "qwen-local"})
    body = r2.json()
    assert body["ok"] is False and body["applied"] == []
    assert body["error"]


# ============================================
# Fix6：伪造 sid 不占槽（spawn 前零锁校验会话存在）
# ============================================
class _EmptyEventsLog:
    def events(self, sid):
        return []


def test_forged_session_id_degrades_to_global_no_spawn(client, monkeypatch, tmp_path):
    """草稿/伪造 sid（快照与事件流均无此会话）：降级全局广播（200），
    绝不 spawn 专属槽（DoS 防护不变）——草稿期切模型是合法操作，不再 404。"""
    import src.session_store as ss
    import web_fastapi.routers.sessions as sessions_router

    # 恢复真 _session_exists；快照目录指向空 tmp、事件流恒空
    monkeypatch.setattr(config_router, "_session_exists",
                        client._real_session_exists)
    monkeypatch.setattr(ss, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(sessions_router, "_sessions_log",
                        lambda req: _EmptyEventsLog())

    gate_calls = []
    monkeypatch.setattr(config_router, "chat_gate_worker",
                        lambda request, sid: gate_calls.append(sid))

    r = client.put("/api/config/model", json={"session_id": "ghost",
                                              "profile_id": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["scope"] == "global"
    assert gate_calls == []                       # 未 spawn / 未路由
    assert len(client._mgr.broadcasts) == 1       # 降级全局广播


def test_existing_session_snapshot_passes_existence_gate(client, monkeypatch, tmp_path):
    """真实快照（data/sessions/{user}/{sid}.json）存在 → 校验放行。"""
    import src.session_store as ss

    monkeypatch.setattr(config_router, "_session_exists",
                        client._real_session_exists)
    monkeypatch.setattr(ss, "SESSIONS_DIR", tmp_path)
    d = tmp_path / "local"
    d.mkdir()
    (d / "s1.json").write_text(
        json.dumps({"session_id": "s1",
                    "messages": [{"role": "user", "content": "hi"}]}),
        encoding="utf-8")

    _seed_profile()
    r = client.put("/api/config/model", json={"session_id": "s1",
                                              "profile_id": "qwen-local"})
    assert r.status_code == 200
    assert client._slot_workers["s1"].ops          # 放行并送达专属槽


def test_session_with_events_but_no_snapshot_passes(client, monkeypatch, tmp_path):
    """T9 事件流会话（无 JSON 快照）：事件存在即真，不误拒。"""
    import src.session_store as ss
    import web_fastapi.routers.sessions as sessions_router

    monkeypatch.setattr(config_router, "_session_exists",
                        client._real_session_exists)
    monkeypatch.setattr(ss, "SESSIONS_DIR", tmp_path)   # 无快照

    class _HasEventsLog:
        def events(self, sid):
            return [{"id": 1, "type": "turn/start"}] if sid == "s1" else []

    monkeypatch.setattr(sessions_router, "_sessions_log",
                        lambda req: _HasEventsLog())
    _seed_profile()
    r = client.put("/api/config/model", json={"session_id": "s1",
                                              "profile_id": "qwen-local"})
    assert r.status_code == 200
    assert client._slot_workers["s1"].ops


# ============================================
# 草稿切换的内存挂账：零 worker 场景由 pending 镜像接住，下一个 spawn 重放
# ============================================
def test_broadcast_llm_params_records_pending_mirror(tmp_path):
    """零存活 worker 时广播仍记录内存挂账（最新一次胜出），不落盘。"""
    from web_fastapi.worker_manager import WorkerManager

    mgr = WorkerManager(state_path=tmp_path / "web_state.json")
    delivered = mgr.broadcast_llm_params(
        clear_model_overrides=True, model="m-draft",
        base_url="http://x/v1", api_key="sk-draft", context_window=None)
    assert delivered == 0                          # 无存活实例
    assert mgr._pending_llm_params == {
        "model": "m-draft", "base_url": "http://x/v1",
        "api_key": "sk-draft", "context_window": None,
        "clear_model_overrides": True}
    # 不落盘：web_state.json 不含 api_key / 模型键
    state_text = (tmp_path / "web_state.json").read_text(encoding="utf-8") \
        if (tmp_path / "web_state.json").exists() else ""
    assert "sk-draft" not in state_text and "m-draft" not in state_text


def test_pending_llm_mirror_replayed_on_next_spawn(tmp_path):
    """挂账值经 _replay_user_state 在下一个 spawn 重放（顺序在镜像之后）。"""
    from web_fastapi.worker_manager import WorkerManager

    mgr = WorkerManager(state_path=tmp_path / "web_state.json")
    mgr.remember_permission_mode("plan")           # 落盘镜像也重放，供顺序断言
    mgr.broadcast_llm_params(clear_model_overrides=True, model="m-draft",
                             api_key="sk-draft")

    class _FakeWP:
        def __init__(self):
            self.ops = []

        def send_fire_and_forget(self, op, **kw):
            self.ops.append((op, kw))
            return True

    wp = _FakeWP()
    mgr._replay_user_state(("local", "s-draft"), wp)
    ops = list(wp.ops)
    assert ops[-1] == ("llm_params_set",
                       {"model": "m-draft", "api_key": "sk-draft",
                        "clear_model_overrides": True})
    assert ops[0] == ("permission_mode_set", {"mode": "plan"})  # 镜像先行
