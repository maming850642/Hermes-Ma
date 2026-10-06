"""
M2.6 chat waker 选择器 —— 数据层 + worker op 测试。

验证：
1. save/load_session 的 waker 往返（schema v3）
2. v2 旧文件向后兼容（无 waker 字段 → load 返回 ""）
3. list_sessions 含 waker
4. SessionBucket.waker 字段 + _op_chat 透传 waker_persona
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# worker_process 模块顶部会 sys.stdin.reconfigure，pytest 下 stdin 是 DontReadFromInput
# 在 import worker_process 前 patch 掉这个副作用
if not hasattr(sys.stdin, "reconfigure"):
    sys.stdin = MagicMock(reconfigure=lambda **kw: None)

import pytest


# ════════════════════════════════════════════════════════════════
# cli.py save/load_session waker 往返
# ════════════════════════════════════════════════════════════════
@pytest.fixture
def sessions_dir(tmp_path, monkeypatch, request):
    """把 SESSIONS_DIR 重定向到 tmp_path。"""
    import src.cli as cli
    import src.session_store as session_store
    from src.storage import paths
    # P3 起 save/load 还会经 session_state_store 读写默认库 kv——数据根一并改道
    paths.set_data_root(tmp_path)
    request.addfinalizer(lambda: paths.set_data_root(None))
    monkeypatch.setattr(cli, "SESSIONS_DIR", tmp_path)
    # T3 抽取后实现体住在 src.session_store（cli 只是 re-export），两边一起重定向
    monkeypatch.setattr(session_store, "SESSIONS_DIR", tmp_path)
    # session_persist 要为 True
    monkeypatch.setattr(cli.get_settings(), "session_persist", True, raising=False)
    return tmp_path


def _human(content):
    return {"role": "user", "content": content}


def test_save_load_session_waker_roundtrip(sessions_dir):
    """save 带 waker → load 能读回同一个 waker。"""
    from src.cli import save_session, load_session
    save_session("u1", [_human("hi")], "sess1", waker="researcher")
    msgs, todos, vfs, waker = load_session("u1", "sess1")
    assert waker == "researcher"
    assert len(msgs) == 1


def test_save_load_session_default_waker_empty(sessions_dir):
    """不传 waker → save 默认空串 → load 返回空串（默认助手）。"""
    from src.cli import save_session, load_session
    save_session("u1", [_human("hi")], "sess2")
    _, _, _, waker = load_session("u1", "sess2")
    assert waker == ""


def test_save_session_waker_keeps_existing(sessions_dir):
    """再次 save 不传 waker → 保持已有 waker（与 name 同样的"传入优先"逻辑）。"""
    from src.cli import save_session, load_session
    save_session("u1", [_human("hi")], "sess3", waker="critic")
    # 第二次 save 不传 waker（None）→ 应保持 "critic"
    save_session("u1", [_human("hi2")], "sess3")
    _, _, _, waker = load_session("u1", "sess3")
    assert waker == "critic"


def test_save_session_waker_override(sessions_dir):
    """再次 save 传 waker="" → 覆盖为空（切回默认助手）。"""
    from src.cli import save_session, load_session
    save_session("u1", [_human("hi")], "sess4", waker="critic")
    save_session("u1", [_human("hi2")], "sess4", waker="")
    _, _, _, waker = load_session("u1", "sess4")
    assert waker == ""


def test_load_v2_schema_backcompat(sessions_dir):
    """v2 旧文件（无 waker 字段）→ load 返回空串（向后兼容）。"""
    # 手工写一个 v2 schema 文件
    f = sessions_dir / "u1" / "old.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({
        "user_id": "u1", "session_id": "old", "name": "",
        "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
        "message_count": 1, "preview": "hi", "messages": [],
        "todos": [], "virtual_fs": {}, "schema_version": 2,
    }), encoding="utf-8")
    from src.cli import load_session
    msgs, todos, vfs, waker = load_session("u1", "old")
    assert waker == ""
    assert msgs == []


def test_list_sessions_includes_waker(sessions_dir):
    """list_sessions 返回的 dict 含 waker 字段。"""
    from src.cli import save_session, list_sessions
    save_session("u1", [_human("hi")], "s_a", waker="researcher")
    save_session("u1", [_human("yo")], "s_b")  # 无 waker
    sessions = list_sessions("u1")
    by_id = {s["session_id"]: s for s in sessions}
    assert by_id["s_a"]["waker"] == "researcher"
    assert by_id["s_b"]["waker"] == ""


# ════════════════════════════════════════════════════════════════
# SessionBucket.waker 字段
# ════════════════════════════════════════════════════════════════
def test_session_bucket_has_waker_field():
    """SessionBucket 有 waker 字段，默认空串。"""
    from web_fastapi.worker_process import SessionBucket
    b = SessionBucket("sid")
    assert b.waker == ""
    b.waker = "researcher"
    assert b.waker == "researcher"


# ════════════════════════════════════════════════════════════════
# _op_chat 透传 waker_persona（mock agent）
# ════════════════════════════════════════════════════════════════
def test_op_chat_passes_waker_persona(tmp_path, monkeypatch):
    """_op_chat：bucket.waker 非空时，加载 persona 并传 waker_persona 给 stream_invoke。"""
    import web_fastapi.worker_process as wp

    # 构造 mock state
    state = MagicMock()
    state.user_id = "wuser"
    state.current_sid = "sid1"
    state.prefs = {}
    bucket = wp.SessionBucket("sid1")
    bucket.waker = "researcher"
    state.get_bucket = lambda sid: bucket
    state.current_bucket = bucket

    # mock agent.stream_invoke 返回带 close 的 generator
    captured = {}
    class _FakeStream:
        def __iter__(self): return self
        def __next__(self): raise StopIteration
        def close(self): pass
    def fake_stream_invoke(*args, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeStream()
    state.agent.stream_invoke = fake_stream_invoke
    state.agent.get_permission_mode = lambda: "before_changes"
    state.agent.set_permission_mode = lambda m: None
    state._save_bucket = lambda b: None

    # mock waker 人格加载（避免文件系统依赖）
    monkeypatch.setattr("src.waker.store.WakerStore", lambda *a, **kw: MagicMock())
    monkeypatch.setattr("src.waker.persona.load_persona_prompt", lambda *a, **kw: "## 核心职责\n调研")

    # 捕获 _send 输出
    sent = []
    monkeypatch.setattr(wp, "_send", lambda msg: sent.append(msg))

    cmd = {"id": "r1", "op": "chat", "message": "hi", "session_id": "sid1"}
    wp._op_chat(state, "r1", cmd)

    # 验证 stream_invoke 收到 waker_persona
    assert "waker_persona" in captured["kwargs"]
    assert captured["kwargs"]["waker_persona"] == "## 核心职责\n调研"


def test_op_chat_no_waker_when_bucket_empty(tmp_path, monkeypatch):
    """_op_chat：bucket.waker 空时，waker_persona=None（默认助手）。"""
    import web_fastapi.worker_process as wp

    state = MagicMock()
    state.user_id = "wuser"
    state.current_sid = "sid2"
    state.prefs = {}
    bucket = wp.SessionBucket("sid2")
    # bucket.waker 默认空串
    state.get_bucket = lambda sid: bucket
    state.current_bucket = bucket

    captured = {}
    class _FakeStream:
        def __iter__(self): return self
        def __next__(self): raise StopIteration
        def close(self): pass
    def fake_stream_invoke(*args, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeStream()
    state.agent.stream_invoke = fake_stream_invoke
    state.agent.get_permission_mode = lambda: "before_changes"
    state.agent.set_permission_mode = lambda m: None
    state._save_bucket = lambda b: None

    monkeypatch.setattr(wp, "_send", lambda msg: None)

    cmd = {"id": "r2", "op": "chat", "message": "hi", "session_id": "sid2"}
    wp._op_chat(state, "r2", cmd)

    assert captured["kwargs"].get("waker_persona") is None
