"""Summarizer 测试，mock LLM 与 MemoryManager。

I4 修复后（2026-06-22）：Step 2 改为复用 manager.ingest_conversation，
不再手写 extract_from_session 逻辑，不再第二次 llm.invoke。
"""
from unittest.mock import MagicMock

from src.memory.summarizer import Summarizer


def _make_summarizer(summary_resp: str, ingest_result: dict | None = None):
    """绕过 __init__，注入 mock。

    summary_resp: Step 1 的 LLM 总结返回
    ingest_result: Step 2 的 manager.ingest_conversation 返回（None 表示用默认）
    """
    s = Summarizer.__new__(Summarizer)
    s.llm = MagicMock()
    s.llm.invoke_simple = MagicMock(return_value=summary_resp)
    s.manager = MagicMock()
    s.manager.store = MagicMock()
    s.manager.store.upsert = MagicMock()
    if ingest_result is None:
        ingest_result = {"success": True, "events": ["ADD", "ADD"], "item_count": 2}
    s.manager.ingest_conversation = MagicMock(return_value=ingest_result)
    return s


def test_summarize_always_produces_summary():
    """即使没有任何事实，总结也会被存（主产品）。"""
    s = _make_summarizer(
        summary_resp="用户讨论了记忆模块重构方案。",
        ingest_result={"success": True, "events": [], "item_count": 0},
    )
    result = s.summarize_and_store("alice", "用户: ...", "session_1")
    assert result["summary_stored"] is True
    # 总结作为特殊记忆存入
    s.manager.store.upsert.assert_called_once()
    # ingest_conversation 被调用了（即使无事实）
    s.manager.ingest_conversation.assert_called_once()


def test_summarize_extracts_facts_via_ingest():
    """总结后调 ingest_conversation 提取长期事实。"""
    s = _make_summarizer(
        summary_resp="讨论了架构。",
        ingest_result={"success": True, "events": ["ADD", "UPDATE"], "item_count": 2},
    )
    result = s.summarize_and_store("alice", "对话内容", "session_1")
    assert result["summary_stored"] is True
    assert result["facts_count"] == 2  # 2 个非 NOOP 事件
    s.manager.ingest_conversation.assert_called_once()


def test_summarize_handles_llm_failure_gracefully():
    """LLM 总结异常 → 总结失败但不抛，ingest 仍尝试（两步独立）。"""
    s = Summarizer.__new__(Summarizer)
    s.llm = MagicMock()
    s.llm.invoke_simple = MagicMock(side_effect=Exception("API 挂了"))
    s.manager = MagicMock()
    s.manager.ingest_conversation = MagicMock(
        return_value={"success": True, "events": ["ADD"], "item_count": 1}
    )
    result = s.summarize_and_store("alice", "对话", "session_1")
    assert result["summary_stored"] is False
    # Step 2 仍执行（两步独立）
    assert result["facts_count"] == 1
    s.manager.ingest_conversation.assert_called_once()


def test_summarize_summary_failure_still_tries_facts():
    """总结失败时，事实提取仍应尝试（两步独立）。"""
    s = Summarizer.__new__(Summarizer)
    s.llm = MagicMock()
    s.llm.invoke_simple = MagicMock(side_effect=Exception("总结失败"))
    s.manager = MagicMock()
    s.manager.ingest_conversation = MagicMock(
        return_value={"success": True, "events": ["ADD"], "item_count": 1}
    )
    result = s.summarize_and_store("alice", "对话", "session_1")
    assert result["summary_stored"] is False
    assert result["facts_count"] == 1


def test_summarize_empty_conversation():
    """空对话文本 → 不调用 LLM，直接返回未存储。"""
    s = _make_summarizer("不该被调用", {"success": True, "events": [], "item_count": 0})
    result = s.summarize_and_store("alice", "", "session_1")
    assert result["summary_stored"] is False
    assert result["facts_count"] == 0
    s.llm.invoke_simple.assert_not_called()
    s.manager.ingest_conversation.assert_not_called()


def test_summarize_skips_empty_value_marker():
    """提示词约定的"无可沉淀"输出：不写记忆、不做事实提取（空转只会产垃圾行）。"""
    s = _make_summarizer(
        "本次会话无可沉淀的有效内容。",
        ingest_result={"success": True, "events": ["ADD"], "item_count": 1},
    )
    result = s.summarize_and_store("alice", "用户: 你好\n助手: 再见", "s1")
    assert result["summary_stored"] is False
    assert result["facts_count"] == 0
    assert result["markdown_path"] is None
    s.manager.store.upsert.assert_not_called()
    s.manager.ingest_conversation.assert_not_called()


def test_summary_memory_id_is_stable_per_session():
    """同一会话的总结写入固定 id sess-summary-{sid}：重复总结覆盖而非新增。"""
    s = _make_summarizer("用户讨论了 X 并决定 Y。")
    s.summarize_and_store("alice", "对话第一版", "bbd8d173")
    assert s.manager.store.upsert.call_count == 1
    first = s.manager.store.upsert.call_args.args[0]
    assert first.id == "sess-summary-bbd8d173"
    assert first.source == "session_summary"

    # 第二次总结（消息有更新）→ 同一 id 再次 upsert（存储层覆盖语义）
    s.summarize_and_store("alice", "对话第二版", "bbd8d173")
    second = s.manager.store.upsert.call_args.args[0]
    assert second.id == first.id


def test_facts_count_excludes_noop():
    """NOOP 事件不计入 facts_count。"""
    s = _make_summarizer(
        summary_resp="总结",
        ingest_result={"success": True, "events": ["ADD", "NOOP", "ADD", "NOOP"], "item_count": 2},
    )
    result = s.summarize_and_store("alice", "对话", "session_1")
    assert result["facts_count"] == 2  # 只有 2 个非 NOOP


def test_summarize_exports_markdown_when_dir_given(tmp_path):
    """传入 summaries_dir 时，Step 1 成功后落盘一份 Markdown，路径写入返回值。"""
    s = _make_summarizer(
        summary_resp="这是一段总结正文。",
        ingest_result={"success": True, "events": ["ADD"], "item_count": 1},
    )
    result = s.summarize_and_store("alice", "对话内容", "session_1", summaries_dir=tmp_path)
    assert result["summary_stored"] is True
    assert result["markdown_path"] is not None
    md = tmp_path / "alice" / "session_1.md"
    assert result["markdown_path"] == md
    assert md.exists()
    text = md.read_text(encoding="utf-8")
    assert "# 会话总结 session_1" in text
    assert "这是一段总结正文。" in text  # 正文落盘
    assert "alice" in text  # 用户元信息


def test_summarize_no_markdown_when_dir_none():
    """不传 summaries_dir（默认 None）时不落盘，markdown_path 为 None（向后兼容）。"""
    s = _make_summarizer("总结正文", {"success": True, "events": [], "item_count": 0})
    result = s.summarize_and_store("alice", "对话", "session_1")
    assert result["markdown_path"] is None


def test_summarize_markdown_failure_does_not_break_summary(tmp_path):
    """落盘失败（如目录不可写）不应影响 Qdrant 写入和返回值。"""
    s = _make_summarizer("总结正文", {"success": True, "events": [], "item_count": 0})
    # 传入一个文件路径作为目录，mkdir 会失败
    bad_dir = tmp_path / "afile"
    bad_dir.write_text("x", encoding="utf-8")
    result = s.summarize_and_store("alice", "对话", "session_1", summaries_dir=bad_dir)
    assert result["summary_stored"] is True  # Qdrant 仍写入
    assert result["markdown_path"] is None  # 落盘失败被吞掉
    s.manager.store.upsert.assert_called_once()


def test_summary_stamps_explicit_project():
    """显式传入 project：总结记忆与事实提取都带该归属。"""
    s = _make_summarizer("总结正文")
    s.summarize_and_store("alice", "对话", "session_1", project="proj-a")
    mem = s.manager.store.upsert.call_args.args[0]
    assert mem.project == "proj-a"
    s.manager.ingest_conversation.assert_called_once()
    assert s.manager.ingest_conversation.call_args.kwargs.get("project") == "proj-a"


def test_summary_project_resolved_from_session_meta(monkeypatch):
    """未显式传 project 时，从会话快照 meta 解析归属（跟会话走，不落全局）。"""
    monkeypatch.setattr(
        "src.session_store.read_session_meta",
        lambda user_id, session_id: {"project": "etf"},
    )
    s = _make_summarizer("总结正文")
    s.summarize_and_store("alice", "对话", "sess-42")
    mem = s.manager.store.upsert.call_args.args[0]
    assert mem.project == "etf"
    assert s.manager.ingest_conversation.call_args.kwargs.get("project") == "etf"
