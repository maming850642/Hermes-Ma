"""
R3 深度 review 修复测试 —— 运维治理与边界（items 15-20）。

覆盖（每项先写测试再修）：
- 15 增长治理：durable tool/result 64KB 截断；snapshots 保留 10；
  RunRegistry 上限 200（活跃态优先保留）
- 16 waker 会话隔离：waker:/wakerflow: sid 事件路由独立 scope（读侧
  双 scope 兼容旧数据）；pending_interrupts 排除 waker 前缀；chat API
  拒绝含 ":" sid；waker 无人值守 auto-reject 写 interrupt/resolved
- 17 mount 校验强化：junction/symlink 拒绝；用户数据目录/项目根封禁；
  换根后 current_root 回退 agent_home
- 18 exit op 统一关停；approve-resume 不再重复写 tool/call
- 19 fork 排他语义 + up_to_event_id + new_sid 碰撞重生成
- 20 上传分块流式（413 中途中止）；workspace 端点 503 降级；
  scheduler stop 排空 deadline

隔离：全部 tmp_path + set_data_root，不碰真实 data/。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.agent.hitl import InterruptSnapshot, InterruptStore
from src.agent.session_log import (
    INTERRUPT_REQUESTED,
    INTERRUPT_RESOLVED,
    SCOPE_CHAT,
    SCOPE_INTERRUPT,
    SCOPE_WAKER,
    SessionLog,
    TOOL_RESULT,
    TURN_START,
    ASSISTANT_MSG,
    USER_MSG,
)
from src.constants import LOCAL_USER
from src.memory.models import Memory
from src.storage import paths
from src.storage.run_registry import RunRegistry
from src.storage.sqlite_provider import MEMORY_SNAPSHOT_KEEP, SQLiteProvider
from src.workspace.models import MODE_LOCAL
from src.workspace.service import WorkspaceError
from src.workspace import state as workspace_state
from tests.agent.test_agent_events import (
    ROUND_TEXT,
    ROUND_TOOL,
    FakeExecutor,
    RecordingLLM,
    find_event,
    make_agent,
    make_tool_spec,
    run_turn,
)
from tests.test_web_fastapi.test_session_events_api import _make_app


@pytest.fixture
def data_root(tmp_path):
    """数据根重定向到 tmp（SQLite 落临时库），测后恢复。"""
    paths.set_data_root(tmp_path)
    yield tmp_path
    paths.set_data_root(None)


# ════════════════════════════════════════════════════════════════
# 15. 增长治理
# ════════════════════════════════════════════════════════════════

class TestToolResultTruncation:
    """15a：durable tool/result 超 64KB 截断（UI tool_end 不动）。"""

    def test_big_result_truncated_in_durable_log_only(self, data_root, monkeypatch):
        log = SessionLog()
        big = "x" * (70 * 1024)
        spec = make_tool_spec(name="echo", executor=FakeExecutor(content=big))
        agent = make_agent(monkeypatch, specs=[spec], session_log=log)
        agent._llm_client = RecordingLLM(rounds=[ROUND_TOOL, ROUND_TEXT])

        events = run_turn(agent, session_id="big")

        # UI tool_end 事件不截断（前端照常拿全量）
        assert find_event(events, "tool_end")["result"] == big

        tr = [e for e in log.events("big") if e["type"] == TOOL_RESULT]
        assert len(tr) == 1
        content = tr[0]["payload"]["content"]
        marker = f"\n...(已截断，原长 {len(big.encode('utf-8'))} 字节)"
        assert content.endswith(marker)
        assert content[:65536] == "x" * 65536
        # 截断体（不含标记）恰为前 64KB
        assert len(content[: -len(marker)].encode("utf-8")) == 64 * 1024

    def test_small_result_not_truncated(self, data_root, monkeypatch):
        log = SessionLog()
        spec = make_tool_spec(name="echo", executor=FakeExecutor(content="ok"))
        agent = make_agent(monkeypatch, specs=[spec], session_log=log)
        agent._llm_client = RecordingLLM(rounds=[ROUND_TEXT])
        run_turn(agent, session_id="small")
        tr = [e for e in log.events("small") if e["type"] == TOOL_RESULT]
        assert tr == []


class TestSnapshotRetention:
    """15b：replace_all 成功后 snapshots(kind='memory') 滚动保留最近 10 个。"""

    def _mem(self, i: int) -> Memory:
        return Memory(
            id=f"m{i}", user_id="local", content=f"c{i}",
            source="manual", created_at=1.0, updated_at=None,
        )

    def test_snapshots_rolled_to_10(self, tmp_path):
        prov = SQLiteProvider(db_path=tmp_path / "db" / "hermes.db")
        try:
            first_id = None
            for i in range(11):
                prov.upsert(self._mem(i))
                label = prov.replace_all([self._mem(i + 100)])
                assert label, "旧数据非空时每次都应产生备份"
                if first_id is None:
                    row = prov._conn.execute(
                        "SELECT MIN(id) AS m FROM snapshots WHERE kind='memory'"
                    ).fetchone()
                    first_id = row["m"]
            count = prov._conn.execute(
                "SELECT COUNT(*) AS c FROM snapshots WHERE kind='memory'"
            ).fetchone()["c"]
            assert count == 10
            # 丢的是最旧的（第一个备份已被滚动清理）
            min_id = prov._conn.execute(
                "SELECT MIN(id) AS m FROM snapshots WHERE kind='memory'"
            ).fetchone()["m"]
            assert min_id > first_id
        finally:
            prov.close()

    def test_snapshots_kept_at_10_or_below(self, tmp_path):
        prov = SQLiteProvider(db_path=tmp_path / "db" / "hermes.db")
        try:
            for i in range(4):
                prov.upsert(self._mem(i))
                prov.replace_all([self._mem(i + 100)])
            count = prov._conn.execute(
                "SELECT COUNT(*) AS c FROM snapshots WHERE kind='memory'"
            ).fetchone()["c"]
            assert count == 4  # 不足 10 不删
        finally:
            prov.close()

    def test_restore_also_prunes_snapshots(self, tmp_path):
        """恢复产生的 pre-restore 快照与 backup 同窗滚动：连续恢复不撑大表。"""
        prov = SQLiteProvider(db_path=tmp_path / "db3" / "hermes.db")
        try:
            for i in range(MEMORY_SNAPSHOT_KEEP):
                prov.upsert(self._mem(i))
                prov.replace_all([self._mem(i + 100)])
            count = lambda: prov._conn.execute(
                "SELECT COUNT(*) AS c FROM snapshots WHERE kind='memory'"
            ).fetchone()["c"]
            assert count() == MEMORY_SNAPSHOT_KEEP

            backups = [b["name"] for b in prov.list_backups()]
            assert prov.restore_backup(backups[-1]) is True
            assert prov.restore_backup(backups[-2]) is True
            # 每次恢复 +1 个 pre-restore 快照，但滚动清理把总数摁回 KEEP
            assert count() == MEMORY_SNAPSHOT_KEEP
        finally:
            prov.close()


class TestRunRegistryCap:
    """15c：upsert 后超 200 条 → 保留最新 200，活跃态优先保留。"""

    def _rec(self, run_id: str, status: str = "ok") -> dict:
        return {"run_id": run_id, "status": status, "started_at": f"t{run_id}"}

    def test_cap_200_drops_oldest_terminal(self, tmp_path):
        db = SQLiteProvider(db_path=tmp_path / "a.db")
        try:
            reg = RunRegistry("flow", db)
            for i in range(200):
                reg.upsert(self._rec(f"r{i:03d}"))
            assert len(reg) == 200
            reg.upsert(self._rec("r-new", status="running"))
            assert len(reg) == 200
            assert reg.get("r-new") is not None      # running 不丢
            assert reg.get("r000") is None           # 最旧终态被丢弃
            assert reg.get("r001") is not None
        finally:
            db.close()

    def test_cap_prefers_active_over_terminal(self, tmp_path):
        db = SQLiteProvider(db_path=tmp_path / "b.db")
        try:
            reg = RunRegistry("flow", db)
            reg.upsert(self._rec("old-active", status="running"))
            for i in range(200):
                reg.upsert(self._rec(f"r{i:03d}"))
            assert len(reg) == 200
            assert reg.get("old-active") is not None  # 活跃态最旧也不丢
            assert reg.get("r000") is None            # 丢的是更靠前的终态
            # 修剪结果写穿 kv（重启后同样 200）
            db2 = SQLiteProvider(db_path=tmp_path / "b.db")
            try:
                reg2 = RunRegistry("flow", db2)
                assert len(reg2) == 200
            finally:
                db2.close()
        finally:
            db.close()


# ════════════════════════════════════════════════════════════════
# 16. waker 会话隔离
# ════════════════════════════════════════════════════════════════

class TestWakerScopeRouting:
    """16a：waker:/wakerflow: sid 事件进 scope="waker"，读侧双 scope 合并。"""

    def test_waker_sid_routes_to_waker_scope(self, data_root):
        log = SessionLog()
        log.append("waker:w:r1", USER_MSG, {"content": "hi"})
        log.append("wakerflow:f:n1", USER_MSG, {"content": "hi"})
        log.append("chat-sid", USER_MSG, {"content": "hi"})

        assert log.provider.iter_events(SCOPE_CHAT, "waker:w:r1") == []
        assert [e["type"] for e in log.provider.iter_events(SCOPE_WAKER, "waker:w:r1")] == [USER_MSG]
        assert [e["type"] for e in log.provider.iter_events(SCOPE_WAKER, "wakerflow:f:n1")] == [USER_MSG]
        # chat sid 仍进 chat scope
        assert [e["type"] for e in log.provider.iter_events(SCOPE_CHAT, "chat-sid")] == [USER_MSG]
        # 读侧按 sid 可见 + 投影正常
        assert [e["type"] for e in log.events("waker:w:r1")] == [USER_MSG]
        assert log.derive_messages("wakerflow:f:n1") == [{"role": "user", "content": "hi"}]

    def test_legacy_waker_events_in_chat_scope_still_readable(self, data_root):
        """旧数据兼容：R3 前 waker 事件写在 chat scope，events/derive 仍可见。"""
        log = SessionLog()
        log.provider.append_event(SCOPE_CHAT, "waker:w:r0", USER_MSG, {"content": "old"})
        log.append("waker:w:r0", USER_MSG, {"content": "new"})
        evs = log.events("waker:w:r0")
        assert [e["payload"]["content"] for e in evs] == ["old", "new"]  # 按 id 合并保序
        assert log.derive_messages("waker:w:r0") == [
            {"role": "user", "content": "old"},
            {"role": "user", "content": "new"},
        ]


class TestPendingInterruptsExcludesWaker:
    """16b：恢复扫描排除 waker:/wakerflow: 前缀 thread（worker 不重导入）。"""

    def _snap(self, thread_id: str) -> InterruptSnapshot:
        return InterruptSnapshot.create(
            thread_id=thread_id,
            messages=[{"role": "user", "content": "hi"}],
            pending_args={"command": "rm x"},
            tool_call_id="call_1",
            tool_name="bash",
            payload={"action": "执行", "details": ""},
            permission_mode="before_changes",
        )

    def test_waker_threads_not_recovered(self, data_root):
        log = SessionLog()
        store = InterruptStore(session_log=log)
        store.save(self._snap("waker:w"))
        store.save(self._snap("wakerflow:f"))
        store.save(self._snap("chat-1"))
        pending = log.pending_interrupts()
        assert [s.thread_id for s in pending] == ["chat-1"]
        # waker thread 的事件仍在 interrupt scope（审计可查），只是不恢复
        assert [e["type"] for e in log.provider.iter_events(SCOPE_INTERRUPT, "waker:w")] == [
            INTERRUPT_REQUESTED,
        ]


class TestChatApiRejectsWakerSid:
    """16c：chat 侧 API / _get_session_file 拒绝 waker sid（含 ":"）。"""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        app, provider = _make_app(tmp_path, monkeypatch)
        yield TestClient(app)
        provider.close()

    @pytest.fixture
    def auth_headers(self):
        # 免认证形态：头部仅作占位，不再是身份来源
        return {}

    def test_events_rejects_waker_sid(self, client, auth_headers):
        r = client.get("/api/sessions/waker:w:r1/events", headers=auth_headers)
        assert r.status_code == 400

    def test_events_rejects_colon_sid(self, client, auth_headers):
        r = client.get("/api/sessions/a:b/events", headers=auth_headers)
        assert r.status_code == 400

    def test_fork_rejects_wakerflow_sid(self, client, auth_headers):
        r = client.post("/api/sessions/wakerflow:f:n/fork", headers=auth_headers)
        assert r.status_code == 400

    def test_validate_id_rejects_colon(self):
        from fastapi import HTTPException
        from web_fastapi.security import validate_id
        with pytest.raises(HTTPException):
            validate_id("waker:x", "会话 ID")
        with pytest.raises(HTTPException):
            validate_id("a:b", "会话 ID")

    def test_get_session_file_rejects_colon(self):
        from src.session_store import _get_session_file
        with pytest.raises(ValueError):
            _get_session_file("u1", "waker:w:r1")
        with pytest.raises(ValueError):
            _get_session_file("u1", "a:b")


class TestWakerAutoRejectResolves:
    """16d：waker 无人值守 auto-reject 调 pop → 写 interrupt/resolved。"""

    def _snap(self, thread_id: str) -> InterruptSnapshot:
        return InterruptSnapshot.create(
            thread_id=thread_id,
            messages=[{"role": "user", "content": "hi"}],
            pending_args={"command": "rm x"},
            tool_call_id="call_1",
            tool_name="bash",
            payload={"action": "执行", "details": ""},
            permission_mode="before_changes",
        )

    def test_runner_auto_reject_pops_interrupt(self, data_root):
        from src.waker import runner
        log = SessionLog()
        store = InterruptStore(session_log=log)
        store.save(self._snap("waker:w"))

        def gen():
            yield {"type": "token", "content": "t"}
            yield {
                "type": "human_approval_request", "action": "执行",
                "details": "d", "thread_id": "waker:w",
            }

        appended = []
        final_text, hit = runner._consume_stream(
            gen(), lambda e: appended.append(e), "w", "r1",
            interrupt_store=store, thread_id="waker:w",
        )
        assert hit is True
        assert final_text == ""
        # 内存 pending 清空 + interrupt/resolved 事件落库
        assert not store.has_pending("waker:w")
        evs = log.provider.iter_events(SCOPE_INTERRUPT, "waker:w")
        assert [e["type"] for e in evs] == [INTERRUPT_REQUESTED, INTERRUPT_RESOLVED]
        assert evs[1]["payload"] == {
            "thread_id": "waker:w", "decision": "reject", "reason": "waker_auto",
        }
        assert any(e.get("type") == "approval_auto_rejected" for e in appended)

    def test_runner_passes_store_from_agent(self, data_root, monkeypatch):
        """_run_stream 把 agent.interrupt_store + thread_id 传给 _consume_stream。"""
        from src.waker import runner
        log = SessionLog()
        store = InterruptStore(session_log=log)
        store.save(self._snap("waker:w"))

        captured = {}

        def _fake_consume(stream, append_fn, name, run_id, **kw):
            captured.update(kw)
            return "", False

        monkeypatch.setattr(runner, "_consume_stream", _fake_consume)

        agent = SimpleNamespace(
            stream_invoke=lambda *a, **k: iter([]),
            interrupt_store=store,
        )
        runner._run_stream(agent, "u1", "task", "persona", "r1", "w", None, lambda e: None)
        assert captured.get("interrupt_store") is store
        assert captured.get("thread_id") == "waker:w"

    def test_worker_node_auto_reject_pops_interrupt(self, monkeypatch):
        """async 子进程路径（worker_node）同样补 auto-reject pop。"""
        from src.wakerflow import worker_node
        monkeypatch.setattr(worker_node, "_emit", lambda obj: None)
        store = InterruptStore()
        store.save(self._snap("wakerflow:f"))

        def gen():
            yield {"type": "human_approval_request", "action": "a", "details": "d",
                   "thread_id": "wakerflow:f"}

        agent = SimpleNamespace(
            stream_invoke=lambda *a, **k: gen(),
            interrupt_store=store,
        )
        worker_node._run_stream(agent, "u1", "task", None, "f", "n1", None)
        assert not store.has_pending("wakerflow:f")


# ════════════════════════════════════════════════════════════════
# 17. mount 校验强化
# ════════════════════════════════════════════════════════════════

def _make_junction(link: Path, target: Path) -> bool:
    """尽力创建 Windows junction（无需管理员）。失败返回 False。"""
    if os.name != "nt":
        return False
    try:
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def _make_symlink(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


@pytest.fixture
def ws(tmp_path):
    from src.workspace.service import WorkspaceService
    paths.set_data_root(tmp_path / "data")
    provider = SQLiteProvider(db_path=tmp_path / "data" / "ws.db")
    workspace_state.set_service(WorkspaceService(provider=provider))
    yield workspace_state.get_service()
    workspace_state.set_service(None)
    provider.close()
    paths.set_data_root(None)


class TestMountRejectsLinks:

    def test_mount_local_rejects_junction(self, ws, tmp_path):
        target = tmp_path / "t"
        target.mkdir()
        link = tmp_path / "j"
        if not (_make_junction(link, target) or _make_symlink(link, target)):
            pytest.skip("无 junction/symlink 创建权限")
        with pytest.raises(WorkspaceError, match="链接|junction"):
            ws.mount_local(str(link))

    def test_mount_local_accepts_plain_realpath_dir(self, ws, tmp_path):
        """普通真实目录（大小写不同写法）不受 reparse 校验误伤。"""
        real = tmp_path / "plain"
        real.mkdir()
        raw = str(real)
        if os.name == "nt" and len(raw) > 2 and raw[1] == ":":
            raw = raw[0].lower() + raw[1:]  # 小写盘符写法
        st = ws.mount_local(raw)
        assert st.mode == MODE_LOCAL


class TestMountForbiddenRoots:

    @pytest.mark.parametrize("env", [
        "USERPROFILE", "APPDATA", "LOCALAPPDATA", "ProgramData", "ALLUSERSPROFILE",
    ])
    def test_user_data_roots_rejected(self, ws, env):
        v = os.environ.get(env)
        if not v or not Path(v).is_dir():
            pytest.skip(f"环境无 {env}")
        with pytest.raises(WorkspaceError):
            ws.mount_local(v)

    def test_project_root_rejected(self, ws):
        with pytest.raises(WorkspaceError):
            ws.mount_local(str(paths.PROJECT_ROOT))

    def test_project_root_subtree_rejected(self, ws):
        with pytest.raises(WorkspaceError):
            ws.mount_local(str(paths.PROJECT_ROOT / "src"))

    def test_subdir_of_user_profile_still_allowed(self, ws, tmp_path):
        """封禁的是用户数据根本身；其下的普通子目录（如临时工程）仍可挂载。"""
        sub = tmp_path / "sub"
        sub.mkdir()
        st = ws.mount_local(str(sub))
        assert st.mode == MODE_LOCAL


class TestCurrentRootFallback:
    """17c：挂载点换根（junction 替换）后 current_root 回退 agent_home。"""

    def test_falls_back_when_mount_swapped_to_junction(self, ws, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        ws.mount_local(str(real))
        assert workspace_state.current_root() == real

        os.rename(real, tmp_path / "real_old")
        if not (_make_junction(real, other) or _make_symlink(real, other)):
            pytest.skip("无 junction/symlink 创建权限")
        assert workspace_state.current_root() == paths.agent_home()

    def test_falls_back_when_mount_dir_deleted(self, ws, tmp_path):
        real = tmp_path / "gone"
        real.mkdir()
        ws.mount_local(str(real))
        assert workspace_state.current_root() == real
        real.rmdir()
        assert workspace_state.current_root() == paths.agent_home()

    def test_remount_updates_root(self, ws, tmp_path):
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        ws.mount_local(str(d1))
        assert workspace_state.current_root() == d1
        ws.mount_local(str(d2))
        assert workspace_state.current_root() == d2


# ════════════════════════════════════════════════════════════════
# 18. exit op 统一关停 + approve-resume 重复 tool/call
# ════════════════════════════════════════════════════════════════

class TestExitOpUnifiedShutdown:

    def test_exit_op_sets_flag_and_acks_no_sysexit(self, monkeypatch):
        import web_fastapi.worker_process as wp
        sent = []
        monkeypatch.setattr(wp, "_send", lambda msg, **kw: sent.append(msg))
        state = MagicMock()
        wp._should_exit.clear()
        try:
            wp.handle_command(state, {"id": "9", "op": "exit"})
            assert wp._should_exit.is_set()
            assert sent and sent[0].get("type") == "result"
            assert sent[0].get("data", {}).get("ok") is True
        finally:
            wp._should_exit.clear()

    def test_shutdown_worker_calls_cleanup_in_order(self, monkeypatch):
        import web_fastapi.worker_process as wp
        state = MagicMock()
        wp._shutdown_worker(state)
        names = [c[0] for c in state.mock_calls]
        # 顺序：save_all_buckets → agent.shutdown_mcp → shutdown_context
        # （后台总结排空步骤已随"总结按需触发"改造移除）
        assert names.index("save_all_buckets") < names.index("agent.shutdown_mcp")
        assert names.index("agent.shutdown_mcp") < names.index("shutdown_context")


class TestResumeNoDuplicateToolCall:
    """18b：approve-resume 不再重复写中断轮已写过的 tool/call。"""

    def test_approve_resume_writes_tool_result_only(self, data_root, monkeypatch):
        from src.agent.session_log import (
            ASSISTANT_MSG, TOOL_CALL, TURN_END, TURN_START,
        )
        from src.agent.session_log import ASSISTANT_MSG, TOOL_CALL, TURN_END, TURN_START
        from src.llm.messages import Chunk
        from tests.agent.test_agent_events import tool_call_chunks
        log = SessionLog()
        store = InterruptStore(session_log=log)
        spec = make_tool_spec(name="rm", destructive=True, executor=FakeExecutor(content="已删除"))
        agent = make_agent(
            monkeypatch, specs=[spec], session_log=log, interrupt_store=store,
        )
        llm = RecordingLLM(rounds=[
            [Chunk(reasoning_delta="思考")] + tool_call_chunks("rm"),
            ROUND_TEXT,
        ])
        agent._llm_client = llm

        run_turn(agent, session_id="dup", thread_id="dup")
        list(agent.stream_invoke(
            "u1", "(resume)", session_id="dup", thread_id="dup", resume_payload="approve",
        ))

        types = [e["type"] for e in log.events("dup")]
        # 中断轮 1 条 tool/call（event_sink 在 InterruptSignal 前写过），
        # resume 轮只补 tool/result——不再出现第二条 tool/call
        assert types.count(TOOL_CALL) == 1
        assert types == [
            TURN_START, USER_MSG, ASSISTANT_MSG, TOOL_CALL, TURN_END,
            TURN_START, TOOL_RESULT, ASSISTANT_MSG, TURN_END,
        ]
        # resume 轮 tool/result 与中断轮 tool/call 的 id 配对
        tr = [e for e in log.events("dup") if e["type"] == TOOL_RESULT][0]
        assert tr["payload"]["tool_call_id"] == "call_1"
        assert tr["payload"]["content"] == "已删除"
        # 投影仍合法（tool 消息配对成功，无悬空占位）
        derived = log.derive_messages("dup")
        assert [m["role"] for m in derived] == [
            "user", "assistant", "tool", "assistant",
        ]
        assert derived[2]["content"] == "已删除"


# ════════════════════════════════════════════════════════════════
# 19. fork 语义
# ════════════════════════════════════════════════════════════════

class TestForkSemantics:

    @pytest.fixture
    def setup(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        app, provider = _make_app(tmp_path, monkeypatch)
        log = app.state.cordis_ctx.try_get("sessions")
        client = TestClient(app)
        # 免认证：无需会话头
        headers = {}
        from tests.test_web_fastapi.test_session_events_api import _append_sample_events
        _append_sample_events(log)
        yield client, headers, log, app
        provider.close()

    def test_after_event_id_exclusive(self, setup):
        client, headers, log, app = setup
        second_id = log.events("src1")[1]["id"]  # user/message 的 id
        r = client.post(
            "/api/sessions/src1/fork", headers=headers,
            json={"after_event_id": second_id},
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["event_count"] == 1  # 仅 id < second_id（排他，不含该事件）
        new_events = log.events(j["session_id"])
        assert [e["type"] for e in new_events] == ["turn/start"]
        # 响应的 up_to_event_id = 实际复制到的最后一条源事件 id
        assert j["up_to_event_id"] == log.events("src1")[0]["id"]

    def test_up_to_event_id_inclusive(self, setup):
        client, headers, log, app = setup
        second_id = log.events("src1")[1]["id"]
        r = client.post(
            "/api/sessions/src1/fork", headers=headers,
            json={"up_to_event_id": second_id},
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["event_count"] == 2  # id <= up_to_event_id（含端点）
        new_events = log.events(j["session_id"])
        assert [e["type"] for e in new_events] == ["turn/start", "user/message"]
        assert j["up_to_event_id"] == second_id

    def test_full_copy_reports_last_event_id(self, setup):
        client, headers, log, app = setup
        src_last = log.events("src1")[-1]["id"]
        r = client.post("/api/sessions/src1/fork", headers=headers)
        j = r.json()
        assert j["event_count"] == 4
        assert j["up_to_event_id"] == src_last

    def test_up_to_user_ordinal_includes_full_turn(self, setup):
        """消息级寻址（2026-09-19「从此 fork」）：含该轮完整问答——保留到
        第 N+1 条 user/message 事件之前（N 为最后一条 user 时等价全量）。"""
        client, headers, log, app = setup
        # 追加第二轮：再问/再答
        log.append("src1", TURN_START, {"input": "再问"})
        log.append("src1", USER_MSG, {"content": "再问"})
        log.append("src1", ASSISTANT_MSG, {"content": "再答"})
        log.append("src1", "turn/end", {"message_count": 2})

        # fork 到第 0 条 user → 保留第一轮完整 4 条，第二轮整体丢弃
        r = client.post("/api/sessions/src1/fork", headers=headers,
                        json={"up_to_user_ordinal": 0})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["event_count"] == 4
        types = [e["type"] for e in log.events(j["session_id"])]
        assert types == ["turn/start", "user/message", "assistant/message", "turn/end"]

        # fork 到第 1 条 user（最后一条）→ 全量 8 条
        r2 = client.post("/api/sessions/src1/fork", headers=headers,
                         json={"up_to_user_ordinal": 1})
        assert r2.status_code == 200, r2.text
        assert r2.json()["event_count"] == 8

    def test_up_to_user_ordinal_out_of_range_404(self, setup):
        client, headers, log, app = setup
        r = client.post("/api/sessions/src1/fork", headers=headers,
                        json={"up_to_user_ordinal": 9})
        assert r.status_code == 404

    def test_new_sid_collision_regenerates(self, setup, monkeypatch):
        client, headers, log, app = setup
        # 预占两个候选 sid（有事件即视为占用）
        log.append("aaaaaaaa", USER_MSG, {"content": "occupied"})
        log.append("bbbbbbbb", USER_MSG, {"content": "occupied"})

        import web_fastapi.routers.sessions as sr
        seq = ["aaaaaaaa", "bbbbbbbb", "cccccccc"]

        class _FakeUUID:
            def __init__(self, v):
                self._v = v

            def __str__(self):
                return self._v

        monkeypatch.setattr(sr.uuid, "uuid4", lambda: _FakeUUID(seq.pop(0)))
        r = client.post("/api/sessions/src1/fork", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["session_id"] == "cccccccc"
        # 没有把事件写进被占用的 sid
        assert len(log.events("aaaaaaaa")) == 1

    def test_new_sid_collision_exhausts_5_attempts(self, setup, monkeypatch):
        client, headers, log, app = setup
        for sid in ("aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd", "eeeeeeee"):
            log.append(sid, USER_MSG, {"content": "occupied"})

        import web_fastapi.routers.sessions as sr
        seq = ["aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd", "eeeeeeee", "ffffffff"]

        class _FakeUUID:
            def __init__(self, v):
                self._v = v

            def __str__(self):
                return self._v

        monkeypatch.setattr(sr.uuid, "uuid4", lambda: _FakeUUID(seq.pop(0)))
        r = client.post("/api/sessions/src1/fork", headers=headers)
        assert r.status_code == 409


# ════════════════════════════════════════════════════════════════
# 20. 上传与服务韧性
# ════════════════════════════════════════════════════════════════

class _FakeUploadFile:
    """async 分块读的假 UploadFile（记录每次请求的块大小）。"""

    def __init__(self, chunks, filename="up.zip"):
        self.chunks = list(chunks)
        self.filename = filename
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1):
        self.read_sizes.append(size)
        return self.chunks.pop(0) if self.chunks else b""


class TestUploadStreaming:

    def _run_route(self, monkeypatch, svc, upload, zip_max=None):
        """直接调 mount_upload 协程（不经 TestClient 的 multipart 编码）。"""
        import web_fastapi.routers.workspace as wr
        if zip_max is not None:
            monkeypatch.setattr(wr, "_ZIP_RAW_MAX", zip_max)
        monkeypatch.setattr(wr, "_get_service", lambda request: svc)
        from fastapi import BackgroundTasks
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        bg = BackgroundTasks()
        return asyncio.run(wr.mount_upload(
            request=request, background=bg, file=upload, user_id="u1",
        ))

    def test_chunked_write_8k_and_threadpool_unzip(self, monkeypatch, tmp_path):
        svc = MagicMock()
        svc.status.return_value = None

        captured = {}

        def _fake_mount_upload(zip_path, display_name=""):
            captured["path"] = Path(zip_path)
            captured["display_name"] = display_name
            captured["bytes"] = Path(zip_path).read_bytes()
            from src.workspace.models import MountState
            return MountState(mode="upload", path=str(zip_path),
                              display_name=display_name, mounted_at=1.0)

        svc.mount_upload.side_effect = _fake_mount_upload
        upload = _FakeUploadFile([b"x" * 8192, b"y" * 8192, b"z" * 100])
        result = self._run_route(monkeypatch, svc, upload)
        # 分块 8KB 读（非整包 await file.read()）：3 个数据块 + 1 次 EOF 探测
        assert upload.read_sizes and set(upload.read_sizes) == {8192}
        assert len(upload.read_sizes) == 4
        # 临时文件内容按序写全；解压在 threadpool 里调用（mock 被执行）
        assert captured["bytes"] == b"x" * 8192 + b"y" * 8192 + b"z" * 100
        assert result["ok"] is True

    def test_oversize_aborts_mid_stream_413(self, monkeypatch):
        svc = MagicMock()
        # 上限 16KB：第 3 个 8KB 块累计 24KB 超限即中止（不读完 10MB 尾巴）
        upload = _FakeUploadFile(
            [b"a" * 8192, b"b" * 8192, b"c" * 8192, b"d" * (10 * 1024 * 1024)],
        )
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            self._run_route(monkeypatch, svc, upload, zip_max=16 * 1024)
        assert ei.value.status_code == 413
        # 中途即中止：没有读第 4 块（大尾巴没进内存）
        assert len(upload.read_sizes) == 3
        svc.mount_upload.assert_not_called()

    def test_empty_file_400(self, monkeypatch):
        svc = MagicMock()
        upload = _FakeUploadFile([])
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            self._run_route(monkeypatch, svc, upload)
        assert ei.value.status_code == 400
        svc.mount_upload.assert_not_called()


class TestWorkspaceDegraded503:
    """20b：状态读取异常 → 503 降级 JSON（不再 500）。"""

    @pytest.fixture
    def client(self, tmp_path):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from web_fastapi.routers import workspace as workspace_router

        class _BrokenSvc:
            def status(self):
                raise RuntimeError("storage down")

            def history(self):
                raise RuntimeError("storage down")

            def unmount(self):
                raise RuntimeError("storage down")

            def choose_chat_only(self):
                raise RuntimeError("storage down")

            def mount_local(self, path, display_name=""):
                raise RuntimeError("storage down")

        workspace_state.set_service(_BrokenSvc())
        a = FastAPI()
        a.include_router(workspace_router.router, prefix="/api/workspace", tags=["workspace"])
        yield TestClient(a)
        workspace_state.set_service(None)

    @pytest.fixture
    def auth_headers(self, client):
        return {}

    @pytest.mark.parametrize("method,url,payload", [
        ("get", "/api/workspace", None),
        ("get", "/api/workspace/history", None),
        ("post", "/api/workspace/unmount", None),
        ("post", "/api/workspace/choose_chat_only", None),
        ("post", "/api/workspace/mount_local", {"path": "D:\\some\\dir"}),
    ])
    def test_service_error_degrades_503(self, client, auth_headers, method, url, payload):
        if method == "get":
            r = client.get(url, headers=auth_headers)
        else:
            r = client.post(url, headers=auth_headers, data=payload)
        assert r.status_code == 503, r.text
        assert "detail" in r.json()


class TestSchedulerStopDeadline:
    """20c：stop() 排空加总上限，慢任务不再挂死关停。"""

    def test_stop_returns_within_deadline_with_slow_task(self, caplog):
        from src.plugins.scheduler_plugin import SchedulerService
        svc = SchedulerService()
        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            release.wait(30)

        svc.register("slow", 0.05, slow, max_workers=1)
        try:
            assert started.wait(5), "慢任务未被调度"
            with caplog.at_level("WARNING", logger="hermes.plugins.scheduler"):
                t0 = time.monotonic()
                svc.stop(timeout=1.0, drain_timeout=0.5)
                elapsed = time.monotonic() - t0
            assert elapsed < 4.0, f"stop 被慢任务挂死 {elapsed:.1f}s"
            assert any("排空超时" in rec.message for rec in caplog.records)
        finally:
            release.set()
