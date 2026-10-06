"""ContextManager 压缩回归测试（OpenAI dict 消息）。

覆盖 H1 修复：compact 生成的摘要 system 消息必须带显式 compact_id，
否则多次 compact 后旧摘要累积、上下文膨胀。
"""
import pytest
from unittest.mock import patch

from src.agent.context import ContextManager


def _make_messages(n: int) -> list:
    """构造 n 轮对话消息（OpenAI dict）。"""
    msgs = [{"role": "user", "content": "你好"}]
    for i in range(n):
        msgs.append({"role": "user", "content": f"问题{i}"})
        msgs.append({"role": "assistant", "content": f"回答{i}"})
    return msgs


def test_compact_summary_has_compact_id():
    """compact 生成的摘要 system 消息必须有显式 compact_id（H1 核心）。"""
    cm = ContextManager()
    msgs = _make_messages(20)
    # mock generate_summary 避免真实 LLM 调用（在方法内部 import，故 patch 源模块）
    with patch("src.tools.compact.generate_summary", return_value="这是摘要"):
        result = cm.compact_messages(list(msgs), keep_count=4)
    assert result is not None
    # 摘要是第一条消息，且必须是 system
    summary_msg = result.compressed_messages[0]
    assert summary_msg["role"] == "system"
    # 关键断言：必须有非空 compact_id
    assert summary_msg.get("compact_id"), "摘要 system 消息必须有显式 compact_id"
    assert summary_msg["compact_id"].startswith("compact-")


def test_compact_summary_id_is_unique_across_runs():
    """多次 compact 生成的摘要 compact_id 必须互不相同。"""
    cm = ContextManager()
    ids = []
    with patch("src.tools.compact.generate_summary", return_value="摘要"):
        for _ in range(3):
            msgs = _make_messages(20)
            result = cm.compact_messages(list(msgs), keep_count=4)
            ids.append(result.compressed_messages[0]["compact_id"])
    assert len(set(ids)) == 3, f"三次 compact 的摘要 id 必须唯一，实际: {ids}"


def test_compact_summary_identifiable_in_list():
    """压缩结果中摘要可被识别（有 compact_id 的 system 消息恰一条）。"""
    cm = ContextManager()
    msgs = _make_messages(20)
    with patch("src.tools.compact.generate_summary", return_value="摘要"):
        result = cm.compact_messages(list(msgs), keep_count=4)
    compressed = result.compressed_messages
    summaries = [m for m in compressed
                 if m.get("role") == "system" and m.get("compact_id")]
    assert len(summaries) == 1, "应只有 1 条摘要 system 消息"


# ─── P4: compact 切分配对保护 ───────────────────────────────────────

def _tool_calls_msg(call_ids, content="让我查一下"):
    """构造一条带多个 tool_calls 的 assistant 消息。"""
    return {
        "role": "assistant", "content": content,
        "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": "ls", "arguments": "{}"}}
            for cid in call_ids
        ],
    }


def _assert_tool_pairing_legal(msgs):
    """每条 tool 消息前面必须存在带对应 tool_call_id 的 assistant(tool_calls)，
    且首条不得是 tool 消息（build_llm_messages 的合法输入契约）。"""
    assert msgs, "消息列表不应为空"
    assert msgs[0].get("role") != "tool", "首条消息不得是孤儿 tool"
    issued_ids = set()
    for m in msgs:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            issued_ids.update(tc.get("id") for tc in m["tool_calls"])
        elif m.get("role") == "tool":
            assert m.get("tool_call_id") in issued_ids, (
                f"tool 消息 {m.get('tool_call_id')!r} 无配对 tool_calls（非法序列）"
            )


def test_compact_protects_tool_call_pair(monkeypatch):
    """切分时不能拆散 assistant(tool_calls) ↔ tool 消息配对。

    构造场景：keep_count=3，切分后 old[-1] 是 assistant(tool_calls)，
    recent[0] 是对应的 tool 消息。compact 应把该 assistant 移到 recent 侧。
    """
    cm = ContextManager()
    msgs = []
    for i in range(10):
        msgs.append({"role": "user", "content": f"问题{i}"})
        msgs.append({"role": "assistant", "content": f"回答{i}"})
    # assistant(tool_calls) 在 old 末尾（index 20 = msgs[-4]）
    msgs.append({
        "role": "assistant", "content": "让我查一下",
        "tool_calls": [{
            "id": "tc1", "type": "function",
            "function": {"name": "shell", "arguments": '{"cmd": "ls"}'},
        }],
    })
    # tool 消息在 recent 开头（index 21 = msgs[-3]）
    msgs.append({"role": "tool", "content": "file1.txt", "tool_call_id": "tc1"})
    msgs.append({"role": "user", "content": "收到"})
    msgs.append({"role": "assistant", "content": "好的"})
    # 总 24 条，keep_count=3 → old=21 (0..20), recent=3 (21..23)
    # old[-1] = tool_calls assistant, recent[0] = tool 消息 → 配对保护触发

    captured = {}

    def fake_generate_summary(text, **kw):
        captured["text"] = text
        return "摘要"

    monkeypatch.setattr("src.tools.compact.generate_summary", fake_generate_summary)

    result = cm.compact_messages(list(msgs), keep_count=3)
    assert result is not None

    # 配对保护把 tool_calls assistant 从 old 移到 recent → recent = 3+1 = 4 条
    recent = result.compressed_messages[1:]  # [0] 是摘要 system 消息
    assert len(recent) == 4, (
        f"配对保护后 recent 应为 4 条，实际: {len(recent)}"
    )
    # tool 消息不孤立——它前面紧挨着对应的 assistant(tool_calls)
    tm_idx = next(i for i, m in enumerate(recent) if m.get("role") == "tool")
    assert tm_idx > 0, "tool 消息不能是 recent 的第一条"
    prev = recent[tm_idx - 1]
    assert prev.get("role") == "assistant" and prev.get("tool_calls"), (
        "tool 消息前必须是带 tool_calls 的 assistant 消息"
    )


def test_compact_cut_inside_tool_batch_pulls_assistant_into_recent(monkeypatch):
    """切点落在同批多个 tool 消息中间：保留区起点必须回溯到 assistant(tool_calls)。

    构造 [.., A(tc×3), T1, T2, T3, user, assistant]，keep_count=4 使切点
    落在 T1 与 T2 之间——old 以 T1 结尾、recent 以孤儿 T2 开头。此前配对
    保护只处理 old[-1]=assistant 一种切法，recent 直接以 tool 开头 →
    build_llm_messages 产出非法序列（严格端点 400），durable kept_messages
    同样投影坏数据。
    """
    cm = ContextManager()
    msgs = [{"role": "user", "content": "q0"}]
    for i in range(6):
        msgs.append({"role": "user", "content": f"问题{i}"})
        msgs.append({"role": "assistant", "content": f"回答{i}"})
    # idx13: A(tc×3)；idx14..16: T1/T2/T3；idx17: user；idx18: assistant
    msgs.append(_tool_calls_msg(["t1", "t2", "t3"]))
    msgs.append({"role": "tool", "content": "r1", "tool_call_id": "t1"})
    msgs.append({"role": "tool", "content": "r2", "tool_call_id": "t2"})
    msgs.append({"role": "tool", "content": "r3", "tool_call_id": "t3"})
    msgs.append({"role": "user", "content": "q9"})
    msgs.append({"role": "assistant", "content": "a9"})
    assert len(msgs) == 19

    monkeypatch.setattr("src.tools.compact.generate_summary", lambda *a, **kw: "摘要")

    # keep_count=4 → old=idx0..14（以 T1 结尾），recent=idx15..18（T2 开头）
    result = cm.compact_messages(list(msgs), keep_count=4)
    assert result is not None
    assert result.compacted_count == 13, (
        f"old 侧应回溯到 A 之前（13 条），实际: {result.compacted_count}"
    )

    kept = result.compressed_messages[1:]  # [0] 是摘要 system；kept 即 durable 保留区
    # recent 必须以该批工具调用的 assistant(tool_calls) 开头，其后是同批 T1..T3
    assert kept[0]["role"] == "assistant" and kept[0].get("tool_calls")
    assert [tc["id"] for tc in kept[0]["tool_calls"]] == ["t1", "t2", "t3"]
    assert [m["tool_call_id"] for m in kept[1:4]] == ["t1", "t2", "t3"]
    assert kept[4]["role"] == "user" and kept[4]["content"] == "q9"
    # 整个保留区投影合法（durable kept_messages 消费同一份数据）
    _assert_tool_pairing_legal(kept)

    # build_llm_messages 的输出必须是配对完整的合法序列
    out = cm.build_llm_messages("u1", "q10", result.compressed_messages)
    conv = out[1:]
    assert [m["role"] for m in conv[:6]] == [
        "assistant", "tool", "tool", "tool", "user", "assistant",
    ]
    _assert_tool_pairing_legal(conv)


# ─── P5: tool 消息角色标签 ──────────────────────────────────────────

def test_compact_toolmessage_role_label(monkeypatch):
    """摘要文本中 tool 消息应标为"工具"而非"系统"。"""
    cm = ContextManager()
    msgs = []
    for i in range(6):
        msgs.append({"role": "user", "content": f"问题{i}"})
        msgs.append({"role": "assistant", "content": f"回答{i}"})
    # 在 old 区域放一对 assistant(tool_calls) → tool（后面有足够消息不会被配对保护拉走）
    msgs.append({
        "role": "assistant", "content": "查文件",
        "tool_calls": [{
            "id": "tc2", "type": "function",
            "function": {"name": "shell", "arguments": '{"cmd": "ls"}'},
        }],
    })
    msgs.append({"role": "tool", "content": "result.txt", "tool_call_id": "tc2"})
    # 再加足够多的消息使这对落在 old 区（不会触发配对保护）
    for i in range(6, 10):
        msgs.append({"role": "user", "content": f"问题{i}"})
        msgs.append({"role": "assistant", "content": f"回答{i}"})
    # 总 22 条，keep_count=4 → old=18（含这对）, recent=4（不含它们）

    captured = {}

    def fake_generate_summary(text, **kw):
        captured["text"] = text
        return "摘要"

    monkeypatch.setattr("src.tools.compact.generate_summary", fake_generate_summary)

    result = cm.compact_messages(list(msgs), keep_count=4)
    assert result is not None
    assert "工具:" in captured["text"], (
        f"摘要文本应包含'工具:'标签，实际前200字: {captured['text'][:200]}"
    )


# ─── P2: 自适应递减 keep_count ──────────────────────────────────────

def test_compact_adaptive_keep_count(monkeypatch):
    """消息数 ≤ keep_count 时应自适应递减 keep_count 而非直接返回 None。

    构造 8 条消息（< keep_count=10）：keep_count 应从 10 递减到 5，
    使 old=3 条可压缩。
    """
    cm = ContextManager()
    msgs = []
    for i in range(4):
        msgs.append({"role": "user", "content": f"问题{i}内容" * 100})
        msgs.append({"role": "assistant", "content": f"回答{i}内容" * 100})

    monkeypatch.setattr("src.tools.compact.generate_summary", lambda *a, **kw: "摘要")

    # 8 条消息，keep_count=10 → while 递减到 5 → 8 > 5 → old=3, recent=5
    result = cm.compact_messages(list(msgs), keep_count=10)
    assert result is not None, "8 条消息 + keep_count=10 应自适应递减后压缩"
    assert result.original_count == 8
    # recent = 5（递减后的 keep_count），old = 3
    assert result.compacted_count == 3


# ─── T7: build_llm_messages dict 行为 ────────────────────────────────

class TestBuildLlmMessages:

    def test_returns_openai_dicts(self):
        cm = ContextManager()
        msgs = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]
        out = cm.build_llm_messages("u1", "q2", msgs)
        assert out[0]["role"] == "system"
        # current_input 不重复注入（历史已有 user 消息，走防御性兜底之外的路径）
        assert [m["role"] for m in out[1:]] == ["user", "assistant"]

    def test_orphan_tool_call_assistant_skipped_on_truncation(self, monkeypatch):
        """窗口截断后首条若是带 tool_calls 的 assistant（对应 tool 结果被截掉）
        → 向后跳过，避免孤儿 tool_calls。

        R2-11：跳过该 assistant 后，其配对的 tool 消息成为开头孤儿
        （它的 assistant 在窗口外）→ 同样丢弃，请求开头无孤儿 tool。"""
        cm = ContextManager()
        monkeypatch.setattr(type(cm.settings), "max_short_term_messages",
                            property(lambda self: 1), raising=False)
        msgs = [
            {"role": "user", "content": "q0"},
            {"role": "assistant", "content": "a0"},
            {
                "role": "assistant", "content": "call it",
                "tool_calls": [{
                    "id": "t1", "type": "function",
                    "function": {"name": "ls", "arguments": "{}"},
                }],
            },
            {"role": "tool", "content": "r1", "tool_call_id": "t1"},
        ]
        out = cm.build_llm_messages("u1", "q0", msgs)
        # max=1*2=2 → 截断后前两条是 assistant(tool_calls)+tool；
        # 两者互为孤儿（配对的另一方都在窗口外）→ 都丢弃；剩余无 user 消息
        # → 防御性兜底补一条 user(current_input)
        assert [m["role"] for m in out[1:]] == ["user"]

    def test_truncation_drops_leading_orphan_tool_messages(self, monkeypatch):
        """R2-11：切点落在 tool 序列中间/开头——截断窗口的首条是 tool 消息
        （其 assistant(tool_calls) 在窗口外）→ 丢弃开头连续 tool，直到首个
        非 tool 消息。否则 LLM 收到无 tool_calls 配对的 tool 消息（400）。"""
        cm = ContextManager()
        monkeypatch.setattr(type(cm.settings), "max_short_term_messages",
                            property(lambda self: 2), raising=False)
        tc = [{"id": "t1", "type": "function",
               "function": {"name": "ls", "arguments": "{}"}}]
        msgs = [
            {"role": "user", "content": "q0"},
            {
                "role": "assistant", "content": "call it",
                "tool_calls": tc,
            },
            {"role": "tool", "content": "r1", "tool_call_id": "t1"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q9"},
        ]
        # max=2*2=4 → 截断后前 4 条 = [tool(r1), user(q1), assistant(a1), user(q9)]
        # 开头 tool 是孤儿（assistant 在窗口外）→ 丢弃
        out = cm.build_llm_messages("u1", "q9", msgs)
        assert [m["role"] for m in out[1:]] == ["user", "assistant", "user"]
        assert out[1]["content"] == "q1"

    def test_truncation_drops_consecutive_leading_tool_run(self, monkeypatch):
        """R2-11：开头连续多条孤儿 tool（多 call 批量执行被切中段）全丢弃。"""
        cm = ContextManager()
        monkeypatch.setattr(type(cm.settings), "max_short_term_messages",
                            property(lambda self: 2), raising=False)
        msgs = [
            {"role": "user", "content": "q0"},
            {"role": "assistant", "content": "call", "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "ls", "arguments": "{}"}},
                {"id": "t2", "type": "function",
                 "function": {"name": "cat", "arguments": "{}"}},
            ]},
            {"role": "tool", "content": "r1", "tool_call_id": "t1"},
            {"role": "tool", "content": "r2", "tool_call_id": "t2"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "q9"},
        ]
        # 截断后前 4 条 = [tool r1, tool r2, assistant done, user q9]
        # → 连续两条孤儿 tool 全丢弃，从 assistant(done) 起
        out = cm.build_llm_messages("u1", "q9", msgs)
        assert [m["role"] for m in out[1:]] == ["assistant", "user"]

    def test_mid_history_system_merged_into_head(self):
        """历史中间的 system 消息（压缩摘要）合并进首条 system prompt。"""
        cm = ContextManager()
        msgs = [
            {"role": "user", "content": "q1"},
            {"role": "system", "content": "旧摘要"},
            {"role": "assistant", "content": "a1"},
        ]
        out = cm.build_llm_messages("u1", "q1", msgs)
        systems = [m for m in out if m["role"] == "system"]
        assert len(systems) == 1
        assert "旧摘要" in systems[0]["content"]

    def test_reasoning_key_not_sent_to_llm(self):
        """state 里的 assistant 附带 reasoning 键不进 LLM payload。"""
        cm = ContextManager()
        msgs = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1", "reasoning": "思考过程"},
        ]
        out = cm.build_llm_messages("u1", "q1", msgs)
        assert all("reasoning" not in m for m in out)


def test_build_llm_messages_filters_llm_error_marker():
    """llm_error 标记消息（LLM 调用失败留底，供 UI 回看）不进 LLM payload。

    P2-9 语义保持：模型永远看不到失败文本。标记消息带 role=assistant，
    过滤必须在窗口截断/投影之前完成。
    """
    cm = ContextManager()
    msgs = [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "⚠️ LLM 调用失败: connection reset",
         "llm_error": True},
        {"role": "user", "content": "第二问"},
    ]
    out = cm.build_llm_messages("u1", "第二问", msgs)
    flat = "".join(str(m.get("content", "")) for m in out)
    assert "调用失败" not in flat
    assert "connection reset" not in flat
    # 正常消息不受影响
    assert any(m.get("content") == "第一问" for m in out)
    assert any(m.get("content") == "第二问" for m in out)
