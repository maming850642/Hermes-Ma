"""
T3 SessionLog 测试 —— 事件溯源会话日志。

覆盖：
- append / events 基本语义（自增 id、id 升序、scope=chat）
- derive_messages 投影规则：user/assistant、tool 配对、compact 截断重建、
  未知类型忽略、turn/* 不参与、多 turn
- 隔离：set_data_root(tmp) 指向临时库，不碰真实 data/hermes.db
"""
import pytest

from src.storage import paths
from src.agent.context import CompactResult
from src.agent.session_log import (
    SessionLog,
    SCOPE_CHAT,
    TURN_START, TURN_END,
    USER_MSG, ASSISTANT_MSG,
    TOOL_CALL, TOOL_RESULT,
    COMPACT_APPLIED, LLM_ERROR, TRUNCATED,
    build_compact_applied_payload,
)


@pytest.fixture
def data_root(tmp_path):
    """数据根重定向到 tmp（SQLite 落临时库），测后恢复。"""
    paths.set_data_root(tmp_path)
    yield tmp_path
    paths.set_data_root(None)


@pytest.fixture
def log(data_root):
    return SessionLog()


# ════════════════════════════════════════════════════════════════
# append / events 基本语义
# ════════════════════════════════════════════════════════════════
class TestAppendEvents:

    def test_append_returns_increasing_ids(self, log):
        id1 = log.append("s1", USER_MSG, {"content": "a"})
        id2 = log.append("s1", USER_MSG, {"content": "b"})
        assert isinstance(id1, int) and isinstance(id2, int)
        assert id2 > id1

    def test_events_ascending_and_scoped_to_chat(self, log):
        log.append("s1", USER_MSG, {"content": "a"})
        log.append("s1", ASSISTANT_MSG, {"content": "b"})
        log.append("s2", USER_MSG, {"content": "other session"})
        evs = log.events("s1")
        ids = [e["id"] for e in evs]
        assert ids == sorted(ids)
        assert [e["type"] for e in evs] == [USER_MSG, ASSISTANT_MSG]
        assert all(e["scope"] == SCOPE_CHAT for e in evs)
        assert all(e["session_id"] == "s1" for e in evs)
        # payload 已反序列化为 dict
        assert evs[0]["payload"] == {"content": "a"}

    def test_events_empty_for_unknown_session(self, log):
        assert log.events("nope") == []

    def test_new_session_log_same_db_sees_events(self, data_root, log):
        """新建 SessionLog（同库）读到同样事件（跨实例/跨重启可见）。"""
        log.append("s1", USER_MSG, {"content": "hi"})
        log2 = SessionLog()
        assert [e["type"] for e in log2.events("s1")] == [USER_MSG]


# ════════════════════════════════════════════════════════════════
# derive_messages 投影
# ════════════════════════════════════════════════════════════════
class TestDeriveMessages:

    def test_user_assistant_roundtrip(self, log):
        log.append("s1", USER_MSG, {"content": "你好"})
        log.append("s1", ASSISTANT_MSG, {"content": "在的"})
        assert log.derive_messages("s1") == [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "在的"},
        ]

    def test_assistant_tool_calls_attached_openai_format(self, log):
        tc = [{"id": "c1", "type": "function",
               "function": {"name": "bash", "arguments": "{\"cmd\": \"ls\"}"}}]
        log.append("s1", USER_MSG, {"content": "ls"})
        log.append("s1", ASSISTANT_MSG, {"content": "", "tool_calls": tc})
        msgs = log.derive_messages("s1")
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["tool_calls"] == tc  # OpenAI 格式透传

    def test_tool_call_result_pairing(self, log):
        """tool/call 不直接投影；tool/result 投影为 tool 消息（经 tool_call_id 配对）。"""
        log.append("s1", USER_MSG, {"content": "ls"})
        log.append("s1", ASSISTANT_MSG, {"content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "bash", "arguments": "{}"}}]})
        log.append("s1", TOOL_CALL, {"tool_call_id": "c1", "name": "bash", "args": {"cmd": "ls"}})
        log.append("s1", TOOL_RESULT, {"tool_call_id": "c1", "name": "bash",
                                       "content": "file1", "ok": True})
        msgs = log.derive_messages("s1")
        assert msgs[2] == {"role": "tool", "tool_call_id": "c1", "content": "file1"}

    def test_compact_applied_truncates_and_rebuilds(self, log):
        """compact/applied 丢弃之前全部投影，以 system summary 重新开始。"""
        log.append("s1", USER_MSG, {"content": "q1"})
        log.append("s1", ASSISTANT_MSG, {"content": "a1"})
        log.append("s1", USER_MSG, {"content": "q2"})
        log.append("s1", COMPACT_APPLIED, {
            "summary": "此前讨论了 q1/a1/q2", "original_count": 3, "compacted_count": 1})
        log.append("s1", USER_MSG, {"content": "q3"})
        log.append("s1", ASSISTANT_MSG, {"content": "a3"})
        assert log.derive_messages("s1") == [
            {"role": "system", "content": "此前讨论了 q1/a1/q2"},
            {"role": "user", "content": "q3"},
            {"role": "assistant", "content": "a3"},
        ]

    def test_compact_twice_last_one_wins(self, log):
        log.append("s1", USER_MSG, {"content": "q1"})
        log.append("s1", COMPACT_APPLIED, {"summary": "第一代摘要", "original_count": 1, "compacted_count": 1})
        log.append("s1", USER_MSG, {"content": "q2"})
        log.append("s1", COMPACT_APPLIED, {"summary": "第二代摘要", "original_count": 2, "compacted_count": 1})
        log.append("s1", USER_MSG, {"content": "q3"})
        assert log.derive_messages("s1") == [
            {"role": "system", "content": "第二代摘要"},
            {"role": "user", "content": "q3"},
        ]

    def test_unknown_event_types_ignored(self, log):
        """未知类型忽略（向前兼容），已知类型照常投影。"""
        log.append("s1", USER_MSG, {"content": "q"})
        log.append("s1", "future/cool_feature", {"anything": 1})
        log.append("s1", ASSISTANT_MSG, {"content": "a"})
        assert log.derive_messages("s1") == [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]

    def test_turn_events_not_projected(self, log):
        log.append("s1", TURN_START, {"input": "hi"})
        log.append("s1", USER_MSG, {"content": "hi"})
        log.append("s1", TURN_END, {"message_count": 2})
        assert log.derive_messages("s1") == [{"role": "user", "content": "hi"}]

    def test_multi_turn_projection_in_order(self, log):
        """多 turn（含工具轮）按事件 id 升序投影出完整对话。"""
        # turn 1（带工具）
        log.append("s1", TURN_START, {"input": "查天气"})
        log.append("s1", USER_MSG, {"content": "查天气"})
        log.append("s1", ASSISTANT_MSG, {"content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "weather", "arguments": "{\"city\": \"北京\"}"}}]})
        log.append("s1", TOOL_CALL, {"tool_call_id": "c1", "name": "weather", "args": {"city": "北京"}})
        log.append("s1", TOOL_RESULT, {"tool_call_id": "c1", "name": "weather", "content": "晴 30℃"})
        log.append("s1", ASSISTANT_MSG, {"content": "北京今天晴，30 度。"})
        log.append("s1", TURN_END, {})
        # turn 2（纯文本）
        log.append("s1", TURN_START, {"input": "谢谢"})
        log.append("s1", USER_MSG, {"content": "谢谢"})
        log.append("s1", ASSISTANT_MSG, {"content": "不客气。"})
        log.append("s1", TURN_END, {})

        msgs = log.derive_messages("s1")
        assert [m["role"] for m in msgs] == [
            "user", "assistant", "tool", "assistant", "user", "assistant",
        ]
        assert msgs[2]["tool_call_id"] == "c1"
        assert msgs[3]["content"] == "北京今天晴，30 度。"

    def test_derive_isolated_per_session(self, log):
        log.append("s1", USER_MSG, {"content": "one"})
        log.append("s2", USER_MSG, {"content": "two"})
        assert log.derive_messages("s1") == [{"role": "user", "content": "one"}]
        assert log.derive_messages("s2") == [{"role": "user", "content": "two"}]

    # ─── R2-8: 悬空 tool_calls（中断/取消遗留）合成占位 tool 消息 ───

    def test_dangling_tool_calls_end_of_stream_gets_placeholder(self, log):
        """流末尾悬空：assistant(tool_calls) 无 tool/result → 末尾合成占位
        tool 消息（"(中断，无结果)"），投影序列对 LLM 合法。"""
        tc = [{"id": "c1", "type": "function",
               "function": {"name": "bash", "arguments": "{}"}}]
        log.append("s1", USER_MSG, {"content": "删文件"})
        log.append("s1", ASSISTANT_MSG, {"content": "", "tool_calls": tc})
        log.append("s1", TOOL_CALL, {"tool_call_id": "c1", "name": "bash", "args": {}})
        log.append("s1", TURN_END, {"message_count": 2})
        msgs = log.derive_messages("s1")
        assert msgs == [
            {"role": "user", "content": "删文件"},
            {"role": "assistant", "content": "", "tool_calls": tc},
            {"role": "tool", "tool_call_id": "c1", "content": "(中断，无结果)"},
        ]

    def test_dangling_tool_calls_mid_history_repaired_before_next_message(self, log):
        """历史中悬空：中断轮的 tool_calls 永远等不到结果，下一条 user
        消息之前合成占位（后续轮次照常投影）。"""
        tc = [{"id": "c9", "type": "function",
               "function": {"name": "rm", "arguments": "{}"}}]
        log.append("s1", USER_MSG, {"content": "q1"})
        log.append("s1", ASSISTANT_MSG, {"content": "", "tool_calls": tc})
        log.append("s1", TURN_END, {})
        # 用户放弃审批，直接开新一轮对话
        log.append("s1", TURN_START, {"input": "q2"})
        log.append("s1", USER_MSG, {"content": "q2"})
        log.append("s1", ASSISTANT_MSG, {"content": "a2"})
        msgs = log.derive_messages("s1")
        assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "user", "assistant"]
        assert msgs[2] == {"role": "tool", "tool_call_id": "c9", "content": "(中断，无结果)"}

    def test_dangling_partial_placeholder_only_for_missing(self, log):
        """多 call 部分完成：已收到结果的照常投影，只给缺失的合成占位。"""
        tcs = [
            {"id": "c1", "type": "function", "function": {"name": "ls", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "cat", "arguments": "{}"}},
        ]
        log.append("s1", USER_MSG, {"content": "q"})
        log.append("s1", ASSISTANT_MSG, {"content": "", "tool_calls": tcs})
        log.append("s1", TOOL_RESULT, {"tool_call_id": "c1", "name": "ls", "content": "ok"})
        # c2 的结果永远没来（中断）
        log.append("s1", USER_MSG, {"content": "next"})
        msgs = log.derive_messages("s1")
        assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "tool", "user"]
        assert msgs[2]["tool_call_id"] == "c1"
        assert msgs[2]["content"] == "ok"
        assert msgs[3] == {"role": "tool", "tool_call_id": "c2", "content": "(中断，无结果)"}

    def test_completed_tool_calls_no_placeholder(self, log):
        """配对完整的 assistant(tool_calls) → tool/result 不受影响（无占位）。"""
        tc = [{"id": "c1", "type": "function",
               "function": {"name": "bash", "arguments": "{}"}}]
        log.append("s1", USER_MSG, {"content": "ls"})
        log.append("s1", ASSISTANT_MSG, {"content": "", "tool_calls": tc})
        log.append("s1", TOOL_RESULT, {"tool_call_id": "c1", "name": "bash", "content": "f1"})
        msgs = log.derive_messages("s1")
        assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]
        assert msgs[2]["content"] == "f1"

    def test_interrupt_then_resume_result_pairs_with_dangling_call(self, log):
        """中断 → resume(approve) 续写 tool/result：结果与挂起 call_id 配对，
        不被占位机制误伤（turn/* 事件不触发 flush）。"""
        tc = [{"id": "c1", "type": "function",
               "function": {"name": "rm", "arguments": "{}"}}]
        # 中断轮
        log.append("s1", TURN_START, {"input": "删"})
        log.append("s1", USER_MSG, {"content": "删"})
        log.append("s1", ASSISTANT_MSG, {"content": "", "tool_calls": tc})
        log.append("s1", TOOL_CALL, {"tool_call_id": "c1", "name": "rm", "args": {}})
        log.append("s1", TURN_END, {})
        # resume 轮（无 user/message）：续跑出真实结果
        log.append("s1", TURN_START, {"input": "(resume)", "resume": "approve"})
        log.append("s1", TOOL_CALL, {"tool_call_id": "c1", "name": "rm", "args": {}})
        log.append("s1", TOOL_RESULT, {"tool_call_id": "c1", "name": "rm", "content": "已删除"})
        log.append("s1", ASSISTANT_MSG, {"content": "删完了"})
        log.append("s1", TURN_END, {})
        msgs = log.derive_messages("s1")
        assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]
        assert msgs[2]["content"] == "已删除"  # 真实结果（非占位）

    def test_orphan_tool_result_without_pending_dropped(self, log):
        """孤儿 tool/result（其 assistant 已被 compact 截断等）→ 丢弃，
        不投影出无配对 assistant 的开头 tool 消息。"""
        log.append("s1", USER_MSG, {"content": "q"})
        log.append("s1", TOOL_RESULT, {"tool_call_id": "ghost", "name": "x", "content": "?"})
        log.append("s1", ASSISTANT_MSG, {"content": "a"})
        msgs = log.derive_messages("s1")
        assert [m["role"] for m in msgs] == ["user", "assistant"]

    # ─── R2-9: compact/applied 带 kept_messages 保留区 ───

    def test_compact_with_kept_messages_preserves_retention_zone(self, log):
        """kept_messages 在场：derive 重置为 [system:summary] + 保留区投影
        （真实压缩保留最后 N 条，重载后保留区不丢）。"""
        kept = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "k1", "type": "function",
                 "function": {"name": "ls", "arguments": "{}"}}]},
            {"role": "tool", "content": "f1", "tool_call_id": "k1"},
            {"role": "user", "content": "q9"},
        ]
        log.append("s1", USER_MSG, {"content": "q1"})
        log.append("s1", ASSISTANT_MSG, {"content": "a1"})
        log.append("s1", COMPACT_APPLIED, {
            "summary": "早期摘要", "original_count": 2, "compacted_count": 2,
            "kept_messages": kept,
        })
        log.append("s1", ASSISTANT_MSG, {"content": "a9"})
        assert log.derive_messages("s1") == [
            {"role": "system", "content": "早期摘要"},
            {"role": "assistant", "content": "", "tool_calls": kept[0]["tool_calls"]},
            {"role": "tool", "content": "f1", "tool_call_id": "k1"},
            {"role": "user", "content": "q9"},
            {"role": "assistant", "content": "a9"},
        ]

    def test_compact_without_kept_messages_legacy_compat(self, log):
        """旧事件（无 kept_messages 字段）：维持旧行为（只留 summary 重启）。"""
        log.append("s1", USER_MSG, {"content": "q1"})
        log.append("s1", ASSISTANT_MSG, {"content": "a1"})
        log.append("s1", COMPACT_APPLIED, {
            "summary": "旧摘要", "original_count": 2, "compacted_count": 2})
        log.append("s1", USER_MSG, {"content": "q2"})
        assert log.derive_messages("s1") == [
            {"role": "system", "content": "旧摘要"},
            {"role": "user", "content": "q2"},
        ]

    def test_compact_discards_pre_compact_dangling(self, log):
        """compact 之前的历史悬空 call_id 不再补占位（已被截断，不受影响）。"""
        tc = [{"id": "c1", "type": "function",
               "function": {"name": "rm", "arguments": "{}"}}]
        log.append("s1", USER_MSG, {"content": "q1"})
        log.append("s1", ASSISTANT_MSG, {"content": "", "tool_calls": tc})
        log.append("s1", COMPACT_APPLIED, {
            "summary": "S", "original_count": 2, "compacted_count": 2,
            "kept_messages": [{"role": "user", "content": "q9"}],
        })
        msgs = log.derive_messages("s1")
        assert msgs == [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "q9"},
        ]


# ════════════════════════════════════════════════════════════════
# 事件类型常量（表驱动快照，防手滑改值破坏兼容）
# ════════════════════════════════════════════════════════════════
def test_event_type_constants():
    import src.agent.session_log as sl
    assert sl.TURN_START == "turn/start"
    assert sl.TURN_END == "turn/end"
    assert sl.USER_MSG == "user/message"
    assert sl.ASSISTANT_MSG == "assistant/message"
    assert sl.TOOL_CALL == "tool/call"
    assert sl.TOOL_RESULT == "tool/result"
    assert sl.INTERRUPT_REQUESTED == "interrupt/requested"
    assert sl.INTERRUPT_RESOLVED == "interrupt/resolved"
    assert sl.COMPACT_APPLIED == "compact/applied"
    assert sl.SCOPE_CHAT == "chat"
    assert sl.SCOPE_INTERRUPT == "interrupt"


# ════════════════════════════════════════════════════════════════
# include_reasoning：UI 历史视图（reasoning 仅在开关打开时透传）
# ════════════════════════════════════════════════════════════════
class TestDeriveIncludeReasoning:

    def test_reasoning_hidden_by_default_and_shown_with_flag(self, log):
        log.append("s1", USER_MSG, {"content": "hi"})
        log.append("s1", ASSISTANT_MSG, {
            "content": "回答", "reasoning": "思考过程",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "bash", "arguments": "{}"}}],
        })
        log.append("s1", TOOL_RESULT, {"tool_call_id": "c1", "content": "ok"})

        pure = log.derive_messages("s1")
        assert all("reasoning" not in m for m in pure)          # LLM 视图纯净
        assert pure[1]["tool_calls"][0]["id"] == "c1"           # 其余字段不变

        ui = log.derive_messages("s1", include_reasoning=True)
        assert ui[1]["reasoning"] == "思考过程"
        assert ui[1]["tool_calls"][0]["id"] == "c1"
        assert len(ui) == len(pure)                             # 仅加字段不变形

    def test_llm_error_ui_projection_only(self, log):
        """llm/error：UI 视图还原为带 llm_error 标记的 assistant（切页/重启
        后历史可回看失败信息）；LLM 视图忽略——模型看不到失败文本（P2-9）。"""
        log.append("s1", USER_MSG, {"content": "你好"})
        log.append("s1", LLM_ERROR, {
            "content": "⚠️ LLM 调用失败: connection reset",
            "error": "connection reset",
        })

        pure = log.derive_messages("s1")
        assert pure == [{"role": "user", "content": "你好"}]

        ui = log.derive_messages("s1", include_reasoning=True)
        assert ui == [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "⚠️ LLM 调用失败: connection reset",
             "llm_error": True},
        ]

    def test_llm_error_after_dangling_tool_calls_flushes_placeholder(self, log):
        """llm/error 前有未配对 tool/call 时，UI 投影先补占位 tool 保持序列合法。"""
        log.append("s1", ASSISTANT_MSG, {
            "content": "",
            "tool_calls": [{"id": "c9", "type": "function",
                            "function": {"name": "bash", "arguments": "{}"}}],
        })
        log.append("s1", LLM_ERROR, {"content": "⚠️ LLM 调用失败: x", "error": "x"})

        ui = log.derive_messages("s1", include_reasoning=True)
        assert [m["role"] for m in ui] == ["assistant", "tool", "assistant"]
        assert ui[1]["tool_call_id"] == "c9"
        assert ui[2]["llm_error"] is True


# ════════════════════════════════════════════════════════════════
# session/truncated：编辑重发的截断标记（2026-09-19）
# ════════════════════════════════════════════════════════════════

def test_truncated_resets_projection_to_kept_prefix(log):
    """session/truncated：投影重置为 kept_messages（无 system 摘要行），
    之前的事件（含悬空 tool_calls）全部作废，之后的事件正常追加。"""
    log.append("s1", USER_MSG, {"content": "问一"})
    log.append("s1", ASSISTANT_MSG, {
        "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "bash", "arguments": "{}"}}],
    })
    log.append("s1", USER_MSG, {"content": "问二"})
    log.append("s1", TRUNCATED, {
        "kept_messages": [
            {"role": "user", "content": "新前缀问"},
            {"role": "assistant", "content": "新前缀答"},
        ],
        "original_count": 9, "truncated_count": 7,
    })
    log.append("s1", USER_MSG, {"content": "截断后的新消息"})

    derived = log.derive_messages("s1")
    assert [m["role"] for m in derived] == ["user", "assistant", "user"]
    assert derived[0]["content"] == "新前缀问"
    assert derived[2]["content"] == "截断后的新消息"
    # 截断点之前的悬空 c1 不补占位（与 compact 处之前的悬空同规则）
    assert all(m.get("tool_call_id") != "c1" for m in derived)


# ════════════════════════════════════════════════════════════════
# build_compact_applied_payload：COMPACT_APPLIED payload 工厂
# （cli.compact_session / worker _op_compact / agent _compact_messages
#   三处写侧共用；此前各自维护逐字相同的复制品）
# ════════════════════════════════════════════════════════════════
def _make_result(**overrides) -> CompactResult:
    kwargs = dict(
        compressed_messages=[],
        summary="早期对话摘要",
        original_count=10,
        compacted_count=6,
    )
    kwargs.update(overrides)
    return CompactResult(**kwargs)


class TestBuildCompactAppliedPayload:

    def test_builds_all_four_fields(self, log):
        """正常构造：4 字段齐全，计数取自 result，保留区逐条规范化。"""
        kept = [
            {"role": "user", "content": "q8"},
            {"role": "assistant", "content": "a8"},
        ]
        payload = build_compact_applied_payload(_make_result(), kept)
        assert payload == {
            "summary": "早期对话摘要",
            "original_count": 10,
            "compacted_count": 6,
            "kept_messages": [
                {"role": "user", "content": "q8"},
                {"role": "assistant", "content": "a8"},
            ],
        }
        # 字段集合精确（不多不少，derive 只认这 4 个键）
        assert set(payload.keys()) == {
            "summary", "original_count", "compacted_count", "kept_messages"}

    def test_strips_leading_summary_system_message(self):
        """头部摘要 system 消息剥掉（压缩结果以 [system:summary] 重启，
        保留区不应再含旧 system——否则投影出现双 system）。"""
        kept = [
            {"role": "system", "content": "本轮压缩生成的摘要"},
            {"role": "user", "content": "q8"},
        ]
        payload = build_compact_applied_payload(_make_result(), kept)
        assert payload["kept_messages"] == [{"role": "user", "content": "q8"}]

    def test_keeps_non_system_head(self):
        """首元素不是 system → 不剥（边界：只有头部 system 才剥）。"""
        kept = [{"role": "user", "content": "q1"}]
        payload = build_compact_applied_payload(_make_result(), kept)
        assert payload["kept_messages"] == kept

    def test_keeps_non_dict_head(self):
        """首元素非 dict（防御脏数据）→ 不剥（isinstance 判断边界）。"""
        kept = ["not-a-dict", {"role": "system", "content": "mid"}]
        payload = build_compact_applied_payload(_make_result(), kept)
        assert payload["kept_messages"][0] == {"role": "user", "content": "not-a-dict"}
        assert payload["kept_messages"][1] == {"role": "system", "content": "mid"}

    def test_keeps_mid_stream_system(self):
        """system 不在头部（历史代摘要）→ 原样保留，不剥。"""
        kept = [
            {"role": "user", "content": "q1"},
            {"role": "system", "content": "历史代摘要"},
        ]
        payload = build_compact_applied_payload(_make_result(), kept)
        assert payload["kept_messages"] == kept

    def test_empty_source_gives_empty_kept(self):
        """空保留区 → kept_messages = []（derive 据此只留 summary 重启）。"""
        payload = build_compact_applied_payload(_make_result(), [])
        assert payload["kept_messages"] == []

    def test_lc_to_dict_normalization_strips_runtime_keys(self):
        """lc_to_dict 规范化：剔除 compact_id 等运行期附带键，保留
        tool_calls/tool_call_id 等模型可见键。"""
        kept = [
            {"role": "user", "content": "q8", "compact_id": "c-42"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "k1", "type": "function",
                 "function": {"name": "ls", "arguments": "{}"}}]},
            {"role": "tool", "content": "f1", "tool_call_id": "k1"},
        ]
        payload = build_compact_applied_payload(_make_result(), kept)
        assert payload["kept_messages"] == [
            {"role": "user", "content": "q8"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "k1", "type": "function",
                 "function": {"name": "ls", "arguments": "{}"}}]},
            {"role": "tool", "content": "f1", "tool_call_id": "k1"},
        ]
        assert all("compact_id" not in m for m in payload["kept_messages"])

    def test_source_list_not_mutated(self):
        """工厂内部 list() 拷贝：调用方传入的原列表不被修改（含头部 system
        的场景，剥离只发生在拷贝上）。"""
        kept = [
            {"role": "system", "content": "摘要"},
            {"role": "user", "content": "q8"},
        ]
        build_compact_applied_payload(_make_result(), kept)
        assert kept == [
            {"role": "system", "content": "摘要"},
            {"role": "user", "content": "q8"},
        ]

    def test_payload_feeds_derive_roundtrip(self, log):
        """端到端：工厂产出的 payload 写入 durable 事件后，derive 投影
        重建 [system:summary] + 保留区（三处写侧语义对齐的最终锚点）。"""
        kept = [
            {"role": "user", "content": "q8", "compact_id": "c-42"},
        ]
        log.append("s1", USER_MSG, {"content": "q1"})
        log.append("s1", COMPACT_APPLIED,
                   build_compact_applied_payload(_make_result(), kept))
        assert log.derive_messages("s1") == [
            {"role": "system", "content": "早期对话摘要"},
            {"role": "user", "content": "q8"},
        ]
