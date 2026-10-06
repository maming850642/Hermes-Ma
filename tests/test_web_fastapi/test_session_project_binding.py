"""会话归属绑定（ADR-0005 D2）worker 侧行为锁定。

覆盖两个关键语义：
1. 首绑竞态：归属在"桶创建时点"确定（get_bucket stamp），首次落盘前切
   激活项目不漂移——旧实现是"保存时现读激活项目"，首条消息生成期间切
   项目会把会话绑错。
2. current_session 响应携带快照归属 project（前端恢复会话前校验用）；
   从未落盘的空白会话返回 ""（与 inbox 同义）。
"""
import pytest

import src.session_store as ss_mod
from config import get_settings
from src.constants import LOCAL_USER
from src.session_store import read_session_meta, save_session
from web_fastapi.worker_manager import DEFAULT_SLOT, SlotsFullError


@pytest.fixture
def sessions_root(tmp_path, monkeypatch, request):
    from src.storage import paths
    # P3 起 save 还会经 session_state_store 读写默认库 kv——数据根一并改道
    paths.set_data_root(tmp_path)
    request.addfinalizer(lambda: paths.set_data_root(None))
    root = tmp_path / "sessions"
    monkeypatch.setattr(ss_mod, "SESSIONS_DIR", root)
    return root


@pytest.fixture(autouse=True)
def force_persist(monkeypatch):
    monkeypatch.setattr(get_settings(), "session_persist", True, raising=False)


def test_bucket_project_stamped_at_creation_not_save(sessions_root):
    """首绑竞态：桶创建于 proj-a，落盘前激活已切 proj-b → 快照仍归 proj-a。"""
    from web_fastapi.worker_process import WorkerState

    state = WorkerState(LOCAL_USER, "bind-test")
    state._get_active_project = lambda: "proj-a"   # 会话诞生时刻的激活项目
    bucket = state.get_bucket("race0001")
    assert bucket.project == "proj-a"

    state._get_active_project = lambda: "proj-b"   # 保存发生前的项目切换
    bucket.messages = [{"role": "user", "content": "第一条"}]
    state._save_bucket(bucket)

    meta = read_session_meta(LOCAL_USER, "race0001")
    assert meta is not None
    assert meta["project"] == "proj-a"

    # 切换后再建的桶 stamp 新项目（每个会话独立归属）
    bucket2 = state.get_bucket("race0002")
    assert bucket2.project == "proj-b"


def test_current_session_reports_snapshot_project(sessions_root, monkeypatch):
    """current_session 响应含 project：有快照→快照归属；无快照→""。"""
    from web_fastapi import worker_process as wp_mod

    save_session(LOCAL_USER, [{"role": "user", "content": "hi"}],
                 "curp0001", project="proj-cur")

    sent: list = []
    monkeypatch.setattr(wp_mod, "_send", lambda msg: sent.append(msg))
    state = wp_mod.WorkerState(LOCAL_USER, "t")

    wp_mod.handle_command(state, {"id": "r1", "op": "current_session",
                                  "session_id": "curp0001"})
    data = sent[0]["data"]
    assert data["session_id"] == "curp0001"
    assert data["project"] == "proj-cur"

    # 未落盘的会话（幽灵 sid 水合成空桶）→ 无归属
    sent.clear()
    wp_mod.handle_command(state, {"id": "r2", "op": "current_session",
                                  "session_id": "ghost999"})
    assert sent[0]["data"]["project"] == ""


# ══════════════════════════════════════════════════════════════════
# POST /api/sessions/save 的 session_id 亲和（P2-14 同款，对齐 /compact）
# ══════════════════════════════════════════════════════════════════
# 此前 /save 恒走 Depends(get_worker) 的 main 槽：M5 下标签页会话在专属
# 槽，/save 存的是 main 槽当前会话且返回误导性成功。修法：
# - 路由带 session_id 时经 chat 闸门亲和路由 + 把 sid 传给 worker；
# - worker 侧 session_save 按 cmd.session_id 定位桶（对齐 _op_compact）。

class _SaveFakeWorker:
    """替身 worker：记录 send 调用并回一个可断言的 result。"""

    def __init__(self, slot):
        self.slot = slot
        self.sent = []

    def send(self, op, **kwargs):
        self.sent.append((op, kwargs))
        return [{"type": "result", "data": {"ok": True, "slot": self.slot,
                                            "session_id": kwargs.get("session_id", "")}}]


class _SaveFakeManager:
    """替身 worker_manager：acquire_chat_slot 可模拟槽满。"""

    def __init__(self, full=False):
        self.main = _SaveFakeWorker("main")
        self.slots = {}
        self.full = full
        self.acquired = []

    def acquire_chat_slot(self, session_id):
        if self.full:
            raise SlotsFullError("并发槽位已满")
        self.acquired.append(session_id)
        return session_id

    def get_or_create(self, user_id, slot=DEFAULT_SLOT):
        if slot == DEFAULT_SLOT:
            return self.main
        return self.slots.setdefault(slot, _SaveFakeWorker(slot))

    def lookup(self, user_id, sid):
        return self.slots.get(sid)


def _save_app(manager):
    from fastapi import FastAPI
    from web_fastapi.routers import sessions as sessions_router
    a = FastAPI()
    a.include_router(sessions_router.router, prefix="/api/sessions")
    a.state.worker_manager = manager
    return a


def test_session_save_targets_cmd_session_bucket(sessions_root, monkeypatch):
    """worker 侧：带 session_id 的 session_save 保存的是该会话的桶
    （消息数按目标桶断言），槽的 current 桶不被误存/污染。"""
    from web_fastapi import worker_process as wp_mod

    sent: list = []
    monkeypatch.setattr(wp_mod, "_send", lambda msg, **k: sent.append(msg))
    saved: list = []
    monkeypatch.setattr(wp_mod.WorkerState, "_save_bucket",
                        lambda self, b: saved.append(b.session_id))
    state = wp_mod.WorkerState(LOCAL_USER, "main")
    state.set_current("curAAAAA")
    state.get_bucket("curAAAAA").messages = [{"role": "user", "content": "主槽当前桶"}]
    state.get_bucket("sessBBBB").messages = [
        {"role": "user", "content": "甲"},
        {"role": "assistant", "content": "乙"},
    ]

    wp_mod.handle_command(state, {"id": "r1", "op": "session_save",
                                  "session_id": "sessBBBB"})

    assert saved == ["sessBBBB"]                     # 存的是目标会话桶
    assert state.get_bucket("curAAAAA").messages == [
        {"role": "user", "content": "主槽当前桶"}]    # 当前桶不受影响
    assert sent[-1]["type"] == "result"
    assert sent[-1]["data"] == {"ok": True, "session_id": "sessBBBB",
                                "message_count": 2}


def test_session_save_without_sid_keeps_current_bucket(sessions_root, monkeypatch):
    """worker 侧：无 session_id（main 槽旧行为）保存 current 桶，不落别的桶。"""
    from web_fastapi import worker_process as wp_mod

    sent: list = []
    monkeypatch.setattr(wp_mod, "_send", lambda msg, **k: sent.append(msg))
    saved: list = []
    monkeypatch.setattr(wp_mod.WorkerState, "_save_bucket",
                        lambda self, b: saved.append(b.session_id))
    state = wp_mod.WorkerState(LOCAL_USER, "main")
    state.set_current("curAAAAA")
    state.get_bucket("curAAAAA").messages = [{"role": "user", "content": "主槽当前桶"}]
    state.get_bucket("sessBBBB").messages = [{"role": "user", "content": "专属桶"}]

    wp_mod.handle_command(state, {"id": "r2", "op": "session_save"})

    assert saved == ["curAAAAA"]
    assert sent[-1]["data"] == {"ok": True, "session_id": "curAAAAA",
                                "message_count": 1}


def test_api_save_with_session_id_routes_to_slot_worker(monkeypatch):
    """路由侧：带 session_id → chat 闸门亲和路由 + sid 透传给 worker；main 不动。"""
    from fastapi.testclient import TestClient

    manager = _SaveFakeManager()
    client = TestClient(_save_app(manager))

    r = client.post("/api/sessions/save", params={"session_id": "sessBBBB"})

    assert r.status_code == 200, r.text
    assert manager.acquired == ["sessBBBB"]          # 走 chat 闸门亲和
    assert manager.slots["sessBBBB"].sent == [
        ("session_save", {"session_id": "sessBBBB"})]
    assert manager.main.sent == []                   # main 槽未被误用


def test_api_save_without_session_id_keeps_main_worker(monkeypatch):
    """路由侧：无 session_id 维持 main 槽旧行为（不触发 chat 闸门抢槽）。"""
    from fastapi.testclient import TestClient

    manager = _SaveFakeManager()
    client = TestClient(_save_app(manager))

    r = client.post("/api/sessions/save")

    assert r.status_code == 200, r.text
    assert manager.acquired == []                    # 未走 chat 闸门
    assert manager.main.sent == [("session_save", {"session_id": ""})]


def test_api_save_slots_full_returns_429(monkeypatch):
    """路由侧：专属槽满（chat 闸门 SlotsFullError）→ 429，对齐 /compact。"""
    from fastapi.testclient import TestClient

    manager = _SaveFakeManager(full=True)
    client = TestClient(_save_app(manager))

    r = client.post("/api/sessions/save", params={"session_id": "sessBBBB"})

    assert r.status_code == 429
