"""会话总结按需触发（2026-09-08 用户决策）的回归测试。

产品语义：总结不再随会话结束自动跑——reset/退出都不总结；只有用户
显式请求（Web 侧栏「⋯ → 生成总结」→ session_summary op；
CLI /exit 菜单显式选 1/2）才会调 LLM 总结。
旧的 reset 自动后台总结（_summarize_in_background/_SUMMARY_MEMO/
_bg_summarize_executor/_drain_bg_summarize）已整体移除。
"""
from unittest.mock import MagicMock

import pytest

from src.constants import LOCAL_USER
from web_fastapi import worker_process as wp


@pytest.fixture(autouse=True)
def _capture_send(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(wp, "_send", lambda msg, **k: sent.append(msg))
    yield sent


@pytest.fixture(autouse=True)
def _no_project_lookup(monkeypatch):
    """桶创建/保存时的项目归属读取不碰真实存储（测试隔离）。"""
    monkeypatch.setattr(wp.WorkerState, "_get_active_project", lambda self: "")


def _state_with_messages(slot: str, sid: str, n: int = 4) -> wp.WorkerState:
    """构造带历史消息桶的 worker state（agent 用桩，不走 LLM）。"""
    state = wp.WorkerState(LOCAL_USER, slot=slot)
    state.agent = MagicMock()
    bucket = state.get_bucket(sid)
    bucket.messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"消息{i}"}
        for i in range(n)
    ]
    state.set_current(sid)
    return state


def test_op_session_reset_does_not_summarize(monkeypatch, _capture_send):
    """reset（新建会话）不再触发总结——没有用户显式请求。"""
    calls: list[str] = []

    def fake_on_end(manager, user_id, messages, session_id, **kw):
        calls.append(session_id)
        return {"summary_stored": True, "facts_count": 0, "markdown_path": None}

    monkeypatch.setattr("src.agent.session_lifecycle.on_session_end", fake_on_end)

    state = _state_with_messages("main", "rset0001")
    wp._op_session_reset(state, "r1", {"new_sid": "new00001"})

    assert _capture_send[-1]["data"] == {"ok": True, "session_id": "new00001"}
    assert calls == []  # 不总结：旧实现的自动后台总结已移除


def test_session_summary_op_summarizes_on_demand(monkeypatch, _capture_send):
    """用户显式请求（session_summary op）才总结，且总结的是目标会话。"""
    calls: list[tuple] = []

    def fake_on_end(manager, user_id, messages, session_id, **kw):
        calls.append((session_id, len(messages)))
        return {"summary_stored": True, "facts_count": 2,
                "markdown_path": None, "summary_text": "总结正文"}

    monkeypatch.setattr("src.agent.session_lifecycle.on_session_end", fake_on_end)

    state = _state_with_messages("main", "demd0001", n=4)
    wp.handle_command(state, {"id": "r1", "op": "session_summary",
                              "session_id": "demd0001"})

    assert calls == [("demd0001", 4)]  # 目标会话 + 其真实消息数
    data = _capture_send[-1]["data"]
    assert data["summary_stored"] is True
    assert data["facts_count"] == 2


def test_worker_exit_no_summary_drain(monkeypatch, _capture_send):
    """worker 统一收尾不再有总结排空步骤（后台总结线程池已移除）。"""
    assert not hasattr(wp, "_bg_summarize_executor")
    assert not hasattr(wp, "_summarize_in_background")
    assert not hasattr(wp, "_drain_bg_summarize")
    assert not hasattr(wp, "_SUMMARY_MEMO")
