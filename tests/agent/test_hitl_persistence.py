"""
T3 HITL 持久化测试 —— InterruptStore × SessionLog。

覆盖：
- 带 log 的 store：save 同步落 interrupt/requested；重启（新 SessionLog 同库）
  后 pending_interrupts 能还原快照（messages 反序列化回 OpenAI dict 且相等）
- resolve 后不再 pending
- recover_into 空内存 store → has_pending 为真、pop 得到等价快照
- restore（恢复路径）不重复写 requested 事件
- 不带 log 的 store：行为与旧版完全一致（纯内存、pop 默认参数兼容）
"""
import pytest

from src.storage import paths
from src.agent.hitl import InterruptStore, InterruptSnapshot
from src.agent.session_log import SessionLog, INTERRUPT_REQUESTED, INTERRUPT_RESOLVED


@pytest.fixture
def data_root(tmp_path):
    paths.set_data_root(tmp_path)
    yield tmp_path
    paths.set_data_root(None)


def _make_snapshot(thread_id="t1"):
    """构造一个字段齐全的快照（含三类 OpenAI dict 消息 + tool_calls）。"""
    return InterruptSnapshot.create(
        thread_id=thread_id,
        messages=[
            {"role": "user", "content": "删掉这个文件"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "bash", "arguments": '{"command": "rm x"}'}},
            ]},
            {"role": "tool", "content": "需要审批", "tool_call_id": "call_1"},
        ],
        pending_args={"command": "rm x"},
        tool_call_id="call_1",
        tool_name="bash",
        payload={"action": "执行 shell", "details": "rm x"},
        permission_mode="before_changes",
    )


# ════════════════════════════════════════════════════════════════
# 带 log 的 store：持久化 + 重启还原
# ════════════════════════════════════════════════════════════════
class TestPersistedStore:

    def test_save_then_new_session_log_restores_snapshot(self, data_root):
        """save 后新建 SessionLog（同库）pending_interrupts 还原完整快照。"""
        log = SessionLog()
        store = InterruptStore(session_log=log)
        snap = _make_snapshot("t1")
        store.save(snap)

        # 模拟重启：全新 SessionLog 实例（同一 db 文件）
        log2 = SessionLog()
        pending = log2.pending_interrupts()
        assert len(pending) == 1
        p = pending[0]
        assert p.thread_id == "t1"
        assert p.pending_args == {"command": "rm x"}
        assert p.pending_tool_call_id == "call_1"
        assert p.pending_tool_name == "bash"
        assert p.pending_payload == {"action": "执行 shell", "details": "rm x"}
        assert p.permission_mode_at_interrupt == "before_changes"
        # messages 反序列化回 OpenAI dict 且 deep equal
        assert p.messages == snap.messages
        assert p.messages[0]["role"] == "user"
        assert p.messages[1]["role"] == "assistant"
        assert p.messages[1]["tool_calls"] == snap.messages[1]["tool_calls"]
        assert p.messages[2]["role"] == "tool"
        assert p.messages[2]["tool_call_id"] == "call_1"

    def test_save_writes_requested_event_with_scope_interrupt(self, data_root):
        log = SessionLog()
        store = InterruptStore(session_log=log)
        store.save(_make_snapshot("t9"))
        evs = log.provider.iter_events("interrupt")
        assert len(evs) == 1
        assert evs[0]["type"] == INTERRUPT_REQUESTED
        assert evs[0]["session_id"] == "t9"  # session_id 列存 thread_id

    def test_resolve_makes_not_pending(self, data_root):
        """resolve（经 pop）后该 thread 不再 pending。"""
        log = SessionLog()
        store = InterruptStore(session_log=log)
        store.save(_make_snapshot("t1"))
        assert log.pending_interrupts() != []

        popped = store.pop("t1", decision="approve", reason="用户同意")
        assert popped is not None
        assert log.pending_interrupts() == []
        assert not store.has_pending("t1")

        # resolved 事件落库且带 decision/reason
        evs = [e for e in log.provider.iter_events("interrupt")
               if e["type"] == INTERRUPT_RESOLVED]
        assert len(evs) == 1
        assert evs[0]["payload"]["decision"] == "approve"
        assert evs[0]["payload"]["reason"] == "用户同意"

    def test_pop_without_decision_default_compatible(self, data_root):
        """旧调用形状 pop(thread_id) 兼容：仍消费 + 写 resolved（decision 空串）。"""
        log = SessionLog()
        store = InterruptStore(session_log=log)
        store.save(_make_snapshot("t1"))
        popped = store.pop("t1")
        assert popped is not None
        assert log.pending_interrupts() == []

    def test_pop_missing_thread_writes_nothing(self, data_root):
        """pop 不存在的 thread：不写 resolved 事件。"""
        log = SessionLog()
        store = InterruptStore(session_log=log)
        assert store.pop("ghost") is None
        assert log.provider.iter_events("interrupt") == []

    def test_multiple_threads_only_pending_listed(self, data_root):
        log = SessionLog()
        store = InterruptStore(session_log=log)
        store.save(_make_snapshot("t1"))
        store.save(_make_snapshot("t2"))
        store.pop("t1", decision="reject", reason="危险")
        pend = log.pending_interrupts()
        assert [p.thread_id for p in pend] == ["t2"]


# ════════════════════════════════════════════════════════════════
# recover_into：重启 → 恢复内存 → chat_approve 可用
# ════════════════════════════════════════════════════════════════
class TestRecoverInto:

    def test_recover_into_empty_store(self, data_root):
        """重启后 recover_into 空内存 store：has_pending 为真、pop 得到等价快照。"""
        log = SessionLog()
        orig = _make_snapshot("t1")
        InterruptStore(session_log=log).save(orig)

        # 重启：新 log + 新 store，恢复
        log2 = SessionLog()
        store2 = InterruptStore(session_log=log2)
        assert not store2.has_pending("t1")
        n = log2.recover_into(store2)
        assert n == 1
        assert store2.has_pending("t1")

        popped = store2.pop("t1", decision="approve", reason="")
        assert popped is not None
        assert popped.thread_id == orig.thread_id
        assert popped.messages == orig.messages
        assert popped.pending_args == orig.pending_args
        assert popped.pending_tool_name == orig.pending_tool_name
        # pop 后写 resolved，后续重启不再恢复
        assert log2.pending_interrupts() == []

    def test_recover_does_not_duplicate_requested_events(self, data_root):
        """recover_into 走内存 restore，不重复写 interrupt/requested。"""
        log = SessionLog()
        InterruptStore(session_log=log).save(_make_snapshot("t1"))

        log2 = SessionLog()
        store2 = InterruptStore(session_log=log2)
        log2.recover_into(store2)
        requested = [e for e in log2.provider.iter_events("interrupt")
                     if e["type"] == INTERRUPT_REQUESTED]
        assert len(requested) == 1  # 仍是原来那一条，没有新增

    def test_recover_into_returns_count(self, data_root):
        log = SessionLog()
        store = InterruptStore(session_log=log)
        store.save(_make_snapshot("t1"))
        store.save(_make_snapshot("t2"))
        assert SessionLog().recover_into(InterruptStore()) == 2
        assert SessionLog().recover_into(InterruptStore()) == 2  # 幂等（事件流不变）


# ════════════════════════════════════════════════════════════════
# 不带 log 的 store：行为照旧（回归保护）
# ════════════════════════════════════════════════════════════════
class TestPureMemoryStore:

    def test_no_log_save_pop_behavior_unchanged(self):
        store = InterruptStore()
        assert store._log is None
        snap = _make_snapshot("t1")
        store.save(snap)
        assert store.has_pending("t1")
        assert store.get("t1") is snap
        popped = store.pop("t1")  # 旧签名
        assert popped is snap
        assert not store.has_pending("t1")
        assert store.pop("t1") is None

    def test_no_log_clear(self):
        store = InterruptStore()
        store.save(_make_snapshot("t1"))
        store.clear()
        assert not store.has_pending("t1")

    def test_no_log_no_events_written(self, data_root):
        """无 log 的 store 不产生任何事件（默认构造零副作用）。"""
        store = InterruptStore()
        store.save(_make_snapshot("t1"))
        store.pop("t1")
        # 没建过 SessionLog，events 表不应有 interrupt 事件
        log = SessionLog()
        assert log.pending_interrupts() == []
