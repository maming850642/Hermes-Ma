"""
T7 dict 迁移验证：旧格式会话 JSON（langchain 类名序列化）兼容加载 +
新格式往返 + compact_messages 的 dict 路径 tool 配对保护。
"""
import json

import pytest

from src.storage import paths


@pytest.fixture(autouse=True)
def _isolated_data_root(tmp_path, monkeypatch):
    """会话目录指向 tmp，不碰真实 data/。"""
    paths.set_data_root(tmp_path)
    monkeypatch.setattr("src.session_store.SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr("src.session_store.SUMMARIES_DIR", tmp_path / "summaries")
    yield
    paths.set_data_root(None)


# ════════════════════════════════════════════════════════════════
# 1. 旧格式（T7 前的 langchain 类名序列化）兼容加载
# ════════════════════════════════════════════════════════════════

def _write_legacy_session(session_id: str, messages: list[dict]):
    """伪造 T7 前的会话 JSON（messages 用类名 type）。"""
    from src.session_store import SESSIONS_DIR
    d = SESSIONS_DIR / "u1"
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "user_id": "u1",
        "session_id": session_id,
        "created_at": "2026-08-01T10:00:00",
        "updated_at": "2026-08-01T11:00:00",
        "message_count": len(messages),
        "preview": "旧会话",
        "messages": messages,
    }
    (d / f"{session_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_legacy_types_map_to_roles():
    """旧格式四类消息 → OpenAI role 映射。"""
    from src.session_store import load_message

    assert load_message({"type": "HumanMessage", "content": "你好"}) == {
        "role": "user", "content": "你好",
    }
    assert load_message({"type": "AIMessage", "content": "在"}) == {
        "role": "assistant", "content": "在",
    }
    assert load_message({"type": "SystemMessage", "content": "提示"}) == {
        "role": "system", "content": "提示",
    }
    tool = load_message({"type": "ToolMessage", "content": "结果", "tool_call_id": "c1"})
    assert tool == {"role": "tool", "content": "结果", "tool_call_id": "c1"}


def test_legacy_aimessage_tool_calls_converted():
    """旧 AIMessage 的 {"name","args","id"} tool_calls → OpenAI function 格式。"""
    from src.session_store import load_message

    out = load_message({
        "type": "AIMessage",
        "content": "",
        "tool_calls": [{"name": "echo", "args": {"q": "hi"}, "id": "c1"}],
        "additional_kwargs": {"reasoning": "想一想"},
    })
    assert out["role"] == "assistant"
    assert out["tool_calls"] == [{
        "id": "c1",
        "type": "function",
        "function": {"name": "echo", "arguments": '{"q": "hi"}'},
    }]
    assert out["reasoning"] == "想一想"


def test_legacy_session_load_end_to_end():
    """旧格式会话文件 load_session → 全 dict 消息列表。"""
    from src.session_store import load_session

    _write_legacy_session("old1", [
        {"type": "HumanMessage", "content": "查一下"},
        {"type": "AIMessage", "content": "", "tool_calls": [
            {"name": "echo", "args": {"q": "x"}, "id": "c1"}]},
        {"type": "ToolMessage", "content": "ok", "tool_call_id": "c1"},
        {"type": "AIMessage", "content": "完成"},
    ])

    messages, todos, vfs, name = load_session("u1", "old1")
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[1]["tool_calls"][0]["function"]["name"] == "echo"
    assert messages[2]["tool_call_id"] == "c1"


def test_unknown_message_type_skipped():
    """未知 type / 无 role 的消息条目加载时跳过（不炸整份会话）。"""
    from src.session_store import load_message

    assert load_message({"type": "WhateverMessage", "content": "?"}) is None
    assert load_message({"content": "无 role 无 type"}) is None
    assert load_message("不是 dict") is None


# ════════════════════════════════════════════════════════════════
# 2. 新格式保存/加载往返
# ════════════════════════════════════════════════════════════════

def test_new_format_roundtrip():
    """新格式（openai-dict）保存 → 加载往返保真（含 tool_calls/reasoning）。

    load_session 返回 (messages, todos, virtual_fs, waker)——name 只用于
    列表展示，不随 load 返回。
    """
    from src.session_store import save_session, load_session

    messages = [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "echo", "arguments": '{"q": "问题"}'},
        }]},
        {"role": "tool", "content": "ok", "tool_call_id": "c1"},
        {"role": "assistant", "content": "回答", "reasoning": "推理"},
    ]
    save_session("u1", messages, "new1", todos=["t"], virtual_fs={"a": "b"},
                 name="测试", waker="w1")

    loaded, todos, vfs, waker = load_session("u1", "new1")
    assert loaded == messages
    assert todos == ["t"]
    assert vfs == {"a": "b"}
    assert waker == "w1"


def test_saved_format_field_is_openai_dict():
    """保存的 JSON 带 format 字段（openai-dict），消息为 role 形态。"""
    from src.session_store import save_session, SESSIONS_DIR

    save_session("u1", [{"role": "user", "content": "hi"}], "fmt1")
    data = json.loads((SESSIONS_DIR / "u1" / "fmt1.json").read_text(encoding="utf-8"))
    assert data.get("format") == "openai-dict"
    assert data["messages"][0]["role"] == "user"


# ════════════════════════════════════════════════════════════════
# 3. compact_messages dict 路径：tool 配对保护
# ════════════════════════════════════════════════════════════════

def test_compact_keeps_tool_pairing(monkeypatch, tmp_path):
    """压缩保留区不产生孤儿 tool 消息：带 tool_calls 的 assistant 与其
    tool 应答要么都在保留区、要么一起进入摘要。"""
    from src.agent.context import ContextManager

    cm = ContextManager()
    # 摘要走 LLM——patch 其真实来源（compact_messages 方法内
    # `from src.tools.compact import generate_summary`）
    monkeypatch.setattr("src.tools.compact.generate_summary", lambda *a, **k: "摘要")

    messages: list[dict] = []
    for i in range(30):
        messages.append({"role": "user", "content": f"问题{i}"})
        messages.append({"role": "assistant", "content": f"答{i}"})
    # 尾部：一次完整的 tool 调用对
    messages.append({"role": "assistant", "content": "", "tool_calls": [{
        "id": "c9", "type": "function",
        "function": {"name": "echo", "arguments": '{}'},
    }]})
    messages.append({"role": "tool", "content": "ok", "tool_call_id": "c9"})

    result = cm.compact_messages(messages, keep_count=4)
    assert result is not None
    kept = result.compressed_messages

    # 保留区中不允许出现孤儿：tool 消息前必有携带对应 tool_call_id 的 assistant
    seen_call_ids: set[str] = set()
    for m in kept:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls", []):
                seen_call_ids.add(tc["id"])
        elif m.get("role") == "tool":
            assert m["tool_call_id"] in seen_call_ids, (
                f"压缩产生了孤儿 tool 消息: {m['tool_call_id']}"
            )
    # 摘要以 system dict 落在队首
    assert kept[0]["role"] == "system"
    assert "摘要" in kept[0]["content"]
