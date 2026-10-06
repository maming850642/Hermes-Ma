"""
P3 会话事件冷归档测试 —— archive_compacted_events / load_events_with_archive
/ StorageHousekeeping 归档 tick / 三消费方回退读。

核心不变量（K）：
    derive_messages 归档前后逐字一致——最后 COMPACT_APPLIED 之前的事件对
    投影冗余（compact payload 内嵌 kept_messages，derive 在 compact 处重置），
    归档只搬走这段前缀，投影必须不动。

覆盖：
- 无 compact 会话不动（返回 0、无冷文件、热表原样）
- 有 compact：冷文件内容=被删行（JSONL 带原 id）、热表剩 compact 及之后
- derive 投影不变量（默认视图 + include_reasoning 视图 + 新实例重读）
- 合并视图（load_events_with_archive）有序、完整、与归档前 events() 等价
- 崩溃窗口冷热并存 → 合并视图按 id 去重；冷文件损坏行跳过
- 二次归档幂等；新 compact 出现后增量归档（旧 compact 一并入冷区）
- purge_session 连冷文件一并删（E1 防 fork 复活）
- StorageHousekeeping：静默会话归档 / 新会话（静默门槛）跳过 / 活跃回调
  跳过与异常保守跳过 / 无 compact 跳过
- 消费方回退读：Web events 端点、Web fork 截断点落冷区、CLI /events、
  CLI /fork 落冷区
- 降级安全：provider 缺归档原语 → 归档禁用（返回 0、不落文件）；
  waker: 前缀 sid 不落冷文件
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.agent.session_log import (
    ARCHIVE_FILE_SUFFIX,
    ASSISTANT_MSG,
    COMPACT_APPLIED,
    SessionLog,
    TOOL_CALL,
    TOOL_RESULT,
    TURN_END,
    TURN_START,
    USER_MSG,
)
from src.storage import paths
from src.storage.sqlite_provider import SQLiteProvider


# ════════════════════════════════════════════════════════════════
# 夹具：临时库 + 会话目录双重定向（库走 set_data_root，冷文件与 JSON 快照
# 同目录 → monkeypatch src.session_store.SESSIONS_DIR，与
# test_session_events_api 同一惯例）
# ════════════════════════════════════════════════════════════════

@pytest.fixture
def env(tmp_path, monkeypatch):
    paths.set_data_root(tmp_path)
    import src.session_store as ss
    monkeypatch.setattr(ss, "SESSIONS_DIR", tmp_path / "sessions")
    provider = SQLiteProvider(db_path=tmp_path / "db" / "hermes.db")
    log = SessionLog(provider=provider)
    yield SimpleNamespace(tmp=tmp_path, provider=provider, log=log,
                          sessions_dir=tmp_path / "sessions")
    provider.close()
    paths.set_data_root(None)


def _build_session(log, sid="s1") -> int:
    """构造"3 段历史 + compact（kept 带工具轮）+ compact 后 2 事件"的会话。

    返回 COMPACT_APPLIED 的事件 id。历史里故意埋一个悬空 tool_calls（c9，
    中断遗留）验证投影不变量覆盖占位补齐语义。
    """
    log.append(sid, TURN_START, {"input": "q1"})
    log.append(sid, USER_MSG, {"content": "q1"})
    log.append(sid, ASSISTANT_MSG, {"content": "", "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "ls", "arguments": "{}"}}]})
    log.append(sid, TOOL_CALL, {"tool_call_id": "c1", "name": "ls", "args": {}})
    log.append(sid, TOOL_RESULT, {"tool_call_id": "c1", "name": "ls",
                                  "content": "f1", "ok": True})
    log.append(sid, ASSISTANT_MSG, {"content": "a1"})
    log.append(sid, USER_MSG, {"content": "q2"})
    # 悬空：assistant(tool_calls c9) 永远等不到 tool/result（中断遗留）
    log.append(sid, ASSISTANT_MSG, {"content": "", "tool_calls": [
        {"id": "c9", "type": "function",
         "function": {"name": "rm", "arguments": "{}"}}]})
    log.append(sid, TURN_END, {})
    kept = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "k1", "type": "function",
             "function": {"name": "ls", "arguments": "{}"}}]},
        {"role": "tool", "content": "f1", "tool_call_id": "k1"},
        {"role": "user", "content": "q9"},
    ]
    compact_id = log.append(sid, COMPACT_APPLIED, {
        "summary": "此前讨论了 q1/q2", "original_count": 5, "compacted_count": 3,
        "kept_messages": kept})
    log.append(sid, USER_MSG, {"content": "q3"})
    log.append(sid, ASSISTANT_MSG, {"content": "a3", "reasoning": "思考"})
    log.append(sid, TURN_END, {})
    return compact_id


def _cold_path(env, sid="s1"):
    return env.sessions_dir / f"{sid}{ARCHIVE_FILE_SUFFIX}"


def _read_cold_lines(env, sid="s1") -> list[dict]:
    text = _cold_path(env, sid).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ════════════════════════════════════════════════════════════════
# 归档核心语义
# ════════════════════════════════════════════════════════════════

class TestArchiveCore:

    def test_no_compact_session_untouched(self, env):
        """无 COMPACT_APPLIED 的会话不动：返回 0、无冷文件、热表原样。"""
        log = env.log
        log.append("s0", USER_MSG, {"content": "hi"})
        log.append("s0", ASSISTANT_MSG, {"content": "hello"})
        before = log.events("s0")

        assert log.archive_compacted_events("s0") == 0
        assert not _cold_path(env, "s0").exists()
        assert log.events("s0") == before

    def test_archive_moves_prefix_keeps_compact_and_after(self, env):
        """有 compact：冷文件内容=被删行（逐行 JSON 带原 id），热表剩
        compact 及之后。"""
        log = env.log
        compact_id = _build_session(log, "s1")
        all_rows = log.events("s1")
        expect_archived = [e for e in all_rows if e["id"] < compact_id]
        expect_hot = [e for e in all_rows if e["id"] >= compact_id]

        n = log.archive_compacted_events("s1")
        assert n == len(expect_archived) > 0

        cold = _read_cold_lines(env, "s1")
        assert [e["id"] for e in cold] == [e["id"] for e in expect_archived]
        for got, want in zip(cold, expect_archived):
            assert got["id"] == want["id"]
            assert got["type"] == want["type"]
            assert got["payload"] == want["payload"]
            assert got["session_id"] == "s1"

        hot = log.events("s1")
        assert [e["id"] for e in hot] == [e["id"] for e in expect_hot]
        assert hot[0]["type"] == COMPACT_APPLIED  # compact 本身永远留热表

    def test_cold_file_next_to_session_snapshots(self, env):
        """冷文件落位惯例：与 JSON 快照同目录，{sid}.events-archive.jsonl。"""
        _build_session(env.log, "s1")
        env.log.archive_compacted_events("s1")
        assert _cold_path(env, "s1").exists()
        assert _cold_path(env, "s1").parent == env.sessions_dir

    def test_archive_idempotent_until_new_compact(self, env):
        """二次归档幂等（返回 0）；新 compact 出现后增量归档：旧 compact
        与其间事件整体入冷区，热表只剩新 compact 及之后。"""
        log = env.log
        c1 = _build_session(log, "s1")
        n1 = log.archive_compacted_events("s1")
        assert n1 > 0
        assert log.archive_compacted_events("s1") == 0  # 幂等

        # 第二代历史：再聊两轮 + 二次 compact
        log.append("s1", USER_MSG, {"content": "q4"})
        log.append("s1", ASSISTANT_MSG, {"content": "a4"})
        c2 = log.append("s1", COMPACT_APPLIED, {
            "summary": "二代摘要", "original_count": 4, "compacted_count": 2,
            "kept_messages": [{"role": "user", "content": "q4"}]})
        log.append("s1", USER_MSG, {"content": "q5"})
        merged_before = log.load_events_with_archive("s1")
        derive_before = log.derive_messages("s1")

        n2 = log.archive_compacted_events("s1")
        assert n2 == len([e for e in merged_before if c1 <= e["id"] < c2])

        cold_ids = [e["id"] for e in _read_cold_lines(env, "s1")]
        assert cold_ids == sorted(cold_ids)
        assert set(range(c1, c2)) <= set(cold_ids)          # 增量段含旧 compact
        hot_ids = [e["id"] for e in log.events("s1")]
        assert all(eid >= c2 for eid in hot_ids)            # 热表只剩新 compact 之后
        # 合并视图仍完整（id 集合不变）、投影不变量保持
        assert [e["id"] for e in log.load_events_with_archive("s1")] == \
            [e["id"] for e in merged_before]
        assert log.derive_messages("s1") == derive_before

    def test_purge_session_removes_cold_file(self, env):
        """E1 联动：purge_session 连冷文件一并删（防 events/fork 从冷区复活）。"""
        log = env.log
        _build_session(log, "s1")
        log.archive_compacted_events("s1")
        assert _cold_path(env, "s1").exists()
        log.purge_session("s1")
        assert not _cold_path(env, "s1").exists()
        assert log.load_events_with_archive("s1") == []


# ════════════════════════════════════════════════════════════════
# 关键不变量：derive 投影归档前后逐字一致 + 合并视图完整性
# ════════════════════════════════════════════════════════════════

class TestDeriveInvariantAndMergedView:

    def test_derive_identical_before_and_after_archive(self, env):
        """（K）投影逐字一致：默认视图与 include_reasoning 视图都要过。"""
        log = env.log
        _build_session(log, "s1")
        pure_before = log.derive_messages("s1")
        ui_before = log.derive_messages("s1", include_reasoning=True)

        log.archive_compacted_events("s1")

        assert log.derive_messages("s1") == pure_before
        assert log.derive_messages("s1", include_reasoning=True) == ui_before
        # 跨实例（同库新 SessionLog，模拟重启后重读）同样一致
        assert env.log.derive_messages("s1") == pure_before

    def test_merged_view_equals_pre_archive_events(self, env):
        """合并视图与归档前 events() 逐条等价（id 升序、type/payload 原样）。"""
        log = env.log
        _build_session(log, "s1")
        before = log.events("s1")
        assert log.load_events_with_archive("s1") == before  # 未归档 = 纯热读

        log.archive_compacted_events("s1")
        merged = log.load_events_with_archive("s1")
        assert merged == before

    def test_merged_view_dedups_crash_window_duplicates(self, env):
        """崩溃窗口（冷已写、热未删）→ 冷热并存；合并视图按 id 去重，热侧权威，
        且下一次归档把残留热行补删。"""
        log = env.log
        compact_id = _build_session(log, "s1")
        first_archived = log.events("s1")[0]

        env.log.archive_compacted_events("s1")
        assert log.events("s1")[0]["id"] == compact_id  # 热表已从 compact 起
        # 人为把冷区首行回插热表——复现"写冷成功、删热前崩溃"的终态（冷热并存）
        env.provider.execute(
            "INSERT INTO events(id, scope, session_id, type, payload, ts) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (first_archived["id"], "chat", "s1", first_archived["type"],
             json.dumps(first_archived["payload"], ensure_ascii=False),
             first_archived["ts"]),
        )

        merged = log.load_events_with_archive("s1")
        ids = [e["id"] for e in merged]
        assert ids == sorted(set(ids))                      # 无重复且升序
        row = [e for e in merged if e["id"] == first_archived["id"]][0]
        assert row["type"] == first_archived["type"]        # 去重后仍完整可读

        # 下一次归档：残留热行按已有冷文件 id 去重（不重写冷文件）并补删
        assert log.archive_compacted_events("s1") == 1
        assert log.load_events_with_archive("s1") == merged

    def test_corrupt_cold_line_skipped(self, env):
        """冷文件损坏行（半截写入）跳过，不毒化整个合并视图。"""
        log = env.log
        _build_session(log, "s1")
        log.archive_compacted_events("s1")
        path = _cold_path(env, "s1")
        path.write_text(
            path.read_text(encoding="utf-8") + '{"id": 999, "broken":\n',
            encoding="utf-8")
        merged = log.load_events_with_archive("s1")
        assert all(e["id"] != 999 for e in merged)
        assert log.derive_messages("s1")[0] == {"role": "system",
                                                "content": "此前讨论了 q1/q2"}


# ════════════════════════════════════════════════════════════════
# StorageHousekeeping 归档 tick（含并发防护）
# ════════════════════════════════════════════════════════════════

def _make_housekeeping(env, **kwargs):
    from src.storage.housekeeping import StorageHousekeeping
    return StorageHousekeeping(storage=env.provider, **kwargs)


class TestHousekeepingArchive:

    def test_archives_quiet_session(self, env):
        """静默会话（静默门槛关掉）被 tick 归档：热表收缩、冷文件生成。"""
        log = env.log
        _build_session(log, "s1")
        hk = _make_housekeeping(env, archive_quiet_seconds=0)
        summary = hk.run_once()
        assert summary["archived_events"] > 0
        assert _cold_path(env, "s1").exists()
        assert log.events("s1")[0]["type"] == COMPACT_APPLIED

    def test_fresh_session_skipped_by_quiet_gate(self, env):
        """并发防护②（静默门槛）：最后事件很新的会话跳过，不落冷文件。"""
        _build_session(env.log, "s1")  # 事件 ts = now
        hk = _make_housekeeping(env, archive_quiet_seconds=15 * 60)
        summary = hk.run_once()
        assert summary["archived_events"] == 0
        assert not _cold_path(env, "s1").exists()

    def test_active_session_skipped_by_callback(self, env):
        """并发防护①（活跃回调）：判定为活跃 → 跳过；不活跃 → 正常归档。"""
        _build_session(env.log, "s1")
        hk = _make_housekeeping(env, archive_quiet_seconds=0,
                                is_session_active=lambda sid: True)
        assert hk.run_once()["archived_events"] == 0
        assert not _cold_path(env, "s1").exists()

        hk2 = _make_housekeeping(env, archive_quiet_seconds=0,
                                 is_session_active=lambda sid: False)
        assert hk2.run_once()["archived_events"] > 0
        assert _cold_path(env, "s1").exists()

    def test_active_callback_exception_skips_conservatively(self, env):
        """活跃回调抛异常 → 按活跃处理（保守跳过），归档绝不带伤执行。"""
        def _boom(sid):
            raise RuntimeError("worker_manager 不可用")
        _build_session(env.log, "s1")
        hk = _make_housekeeping(env, archive_quiet_seconds=0,
                                is_session_active=_boom)
        assert hk.run_once()["archived_events"] == 0
        assert not _cold_path(env, "s1").exists()

    def test_no_compact_session_skipped(self, env):
        """无 compact 的会话不在候选里：不建冷文件、不计归档数。"""
        env.log.append("s0", USER_MSG, {"content": "hi"})
        hk = _make_housekeeping(env, archive_quiet_seconds=0)
        assert hk.run_once()["archived_events"] == 0
        assert not _cold_path(env, "s0").exists()

    def test_events_archive_disabled_flag(self, env):
        """events_archive_enabled=False：归档整体关闭（其余卫生动作照常）。"""
        _build_session(env.log, "s1")
        hk = _make_housekeeping(env, archive_quiet_seconds=0,
                                events_archive_enabled=False)
        assert hk.run_once()["archived_events"] == 0
        assert not _cold_path(env, "s1").exists()


# ════════════════════════════════════════════════════════════════
# 消费方回退读：Web events 端点 / Web fork / CLI /events / CLI /fork
# ════════════════════════════════════════════════════════════════

def _make_web_client(log):
    """精简 app（只挂 sessions router，cordis_ctx 注入 log），仿
    test_session_events_api。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web_fastapi.routers import sessions as sessions_router

    class _FakeCtx:
        def __init__(self, log):
            self._log = log

        def try_get(self, key):
            return self._log if key == "sessions" else None

    a = FastAPI()
    a.include_router(sessions_router.router, prefix="/api/sessions")
    a.state.cordis_ctx = _FakeCtx(log)
    return TestClient(a)


class TestWebConsumers:

    def test_events_endpoint_returns_merged_view(self, env):
        """事件对话框：归档后端点仍返回完整有序事件流（冷+热合并）。"""
        log = env.log
        _build_session(log, "s1")
        client = _make_web_client(log)
        before = client.get("/api/sessions/s1/events").json()["events"]

        log.archive_compacted_events("s1")
        after = client.get("/api/sessions/s1/events").json()["events"]
        assert after == before
        ids = [e["id"] for e in after]
        assert ids == sorted(ids) and len(ids) >= 12

    def test_fork_cutoff_in_cold_region_gets_full_prefix(self, env):
        """fork 截断点落冷区：从合并视图切，新会话拿到完整前缀（逐条同型）。"""
        log = env.log
        _build_session(log, "s1")
        client = _make_web_client(log)
        merged = log.load_events_with_archive("s1")
        cold_cut = [e for e in merged if e["type"] == USER_MSG][0]["id"]  # 冷区事件

        log.archive_compacted_events("s1")
        r = client.post("/api/sessions/s1/fork", json={"up_to_event_id": cold_cut})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["up_to_event_id"] == cold_cut

        prefix = [e for e in merged if e["id"] <= cold_cut]
        assert j["event_count"] == len(prefix)
        copied = log.events(j["session_id"])
        assert len(copied) == len(prefix)
        for got, want in zip(copied, prefix):
            assert got["type"] == want["type"]
            assert got["payload"] == want["payload"]
        # fork 分支投影 == 源前缀投影（用临时会话回放同一前缀作参照——
        # 截断点在 compact 之前，投影自然是压缩前历史而非现源投影）
        for ev in prefix:
            log.append("chk_ref", ev["type"], ev["payload"])
        assert log.derive_messages(j["session_id"]) == \
            log.derive_messages("chk_ref")


class TestCliConsumers:

    def _boot(self, log):
        return SimpleNamespace(get=lambda name: log if name == "sessions" else None)

    def test_cmd_events_reads_merged_view(self, env, monkeypatch):
        """/events：归档后仍展示全部事件（共 N 条含冷区行）。"""
        from src import cli
        log = env.log
        _build_session(log, "s1")
        total = len(log.load_events_with_archive("s1"))
        console = MagicMock()
        monkeypatch.setattr(cli, "console", console)

        cli._cmd_events(self._boot(log), "s1", "50")
        table = console.print.call_args[0][0]
        assert f"共 {total} 条" in table.title

        log.archive_compacted_events("s1")
        console2 = MagicMock()
        monkeypatch.setattr(cli, "console", console2)
        cli._cmd_events(self._boot(log), "s1", "50")
        table2 = console2.print.call_args[0][0]
        assert f"共 {total} 条" in table2.title  # 归档不丢事件

    def test_cmd_fork_in_cold_region_full_prefix(self, env, monkeypatch):
        """/fork [id]：切片点落冷区仍复制完整前缀，切过去即可聊。"""
        from src import cli
        import src.session_store as ss
        log = env.log
        _build_session(log, "s1")
        merged = log.load_events_with_archive("s1")
        cold_cut = [e for e in merged if e["type"] == USER_MSG][0]["id"]

        log.archive_compacted_events("s1")
        monkeypatch.setattr(cli, "console", MagicMock())
        new_sid = cli._cmd_fork(self._boot(log), "local", "s1", [], str(cold_cut))
        assert new_sid

        prefix = [e for e in merged if e["id"] <= cold_cut]
        copied = log.events(new_sid)
        assert len(copied) == len(prefix)
        for got, want in zip(copied, prefix):
            assert got["type"] == want["type"]
            assert got["payload"] == want["payload"]
        # stub 落盘（会话列表可见）且投影与前缀一致
        entries = {s["session_id"] for s in ss.list_sessions("local")}
        assert new_sid in entries


# ════════════════════════════════════════════════════════════════
# 降级安全
# ════════════════════════════════════════════════════════════════

class _BareProvider:
    """只实现事件读写最小面的替身 provider（无归档原语）。"""

    def __init__(self):
        self._rows: list[dict] = []
        self._next_id = 1

    def append_event(self, scope, session_id, type_, payload):
        row = {"id": self._next_id, "scope": scope, "session_id": session_id,
               "type": type_, "payload": payload, "ts": 0.0}
        self._next_id += 1
        self._rows.append(row)
        return row["id"]

    def iter_events(self, scope, session_id=None, after_id=0):
        return [
            dict(r) for r in self._rows
            if r["scope"] == scope and r["id"] > after_id
            and (session_id is None or r["session_id"] == session_id)
        ]


class TestDegradation:

    def test_provider_without_primitives_disables_archive(self, env):
        """provider 缺归档原语：归档禁用（返回 0、不落文件），合并读退化为纯热读。"""
        p = _BareProvider()
        log = SessionLog(provider=p)
        log.append("s1", USER_MSG, {"content": "q"})
        log.append("s1", COMPACT_APPLIED, {"summary": "S"})
        assert log.archive_compacted_events("s1") == 0
        assert not _cold_path(env, "s1").exists()
        assert [e["type"] for e in log.load_events_with_archive("s1")] == \
            [USER_MSG, COMPACT_APPLIED]

    def test_waker_sid_never_gets_cold_file(self, env):
        """waker: 前缀 sid（含 ':'，Windows 非法文件名）不落冷文件。"""
        log = env.log
        log.append("waker:x1", USER_MSG, {"content": "q"})
        log.append("waker:x1", COMPACT_APPLIED, {"summary": "S"})
        assert log.archive_compacted_events("waker:x1") == 0
        assert not (env.sessions_dir / "waker:x1.events-archive.jsonl").exists()
        assert log.load_events_with_archive("waker:x1")[0]["type"] == USER_MSG
