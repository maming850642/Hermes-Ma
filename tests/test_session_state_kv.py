"""P3 会话状态 kv 权威（todos / virtual_fs / waker）不变量测试。

锁定的语义（P3 双轨收口主体）：
1. roundtrip：save → load 状态逐字段一致（JSON 快照与 kv 行同写）；
2. 旧会话回退 + 回填：只有 JSON 无 kv → 正确加载，且 kv 行被回填
   （迁移自愈：读一次旧会话即升级）；形状非法的 kv 行同襟自愈；
3. JSON 可丢：删掉/损坏 JSON 文件后，状态仍从 kv 完整恢复
   （消息侧权威在事件流，经 load_session_events_first 投影）；
4. fork：源会话 kv 状态行复制到新 sid（源 JSON 缺失时 kv 直拷仍保真）；
5. purge/删会话联动：worker session_delete op 与项目级联兜底都清 kv 行；
6. 降级安全：kv 写失败不阻断 JSON 保存（读侧回退 + 再回填）；
7. 并发写安全：kv 与 events 同库同约定（模块锁 + WAL），并发 save 与
   事件追加互不踩。
"""
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.session_store as ss
from config import get_settings
from src.agent.session_log import ASSISTANT_MSG, USER_MSG, SessionLog
from src.constants import LOCAL_USER
from src.storage import session_state_store as sst
from src.storage.sqlite_provider import SQLiteProvider
from src.workspace.service import WorkspaceService
from web_fastapi.routers import projects as proj_router
from web_fastapi.routers import sessions as sessions_router


@pytest.fixture(autouse=True)
def _isolated(isolated_data_env, monkeypatch):
    """数据根/SESSIONS_DIR/默认连接缓存全量隔离（conftest）+ 持久化开关。"""
    monkeypatch.setattr(get_settings(), "session_persist", True, raising=False)
    return isolated_data_env


@pytest.fixture
def events_log():
    """默认库 SessionLog（数据根已隔离 → tmp 库，与 kv 同一库文件，同生产）。"""
    log = SessionLog()
    yield log
    try:
        log.provider.close()
    except Exception:
        pass


def _state(todos=None, vfs=None, waker=""):
    return (todos if todos is not None else [{"t": 1}],
            vfs if vfs is not None else {"f.txt": "x"},
            waker)


def _write_json_only(user_id: str, sid: str, todos, vfs, waker):
    """直接落一份 JSON 快照（不经 save_session → 不写 kv），模拟旧会话。"""
    user_dir = ss.SESSIONS_DIR / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / f"{sid}.json").write_text(json.dumps({
        "user_id": user_id, "session_id": sid, "name": "", "project": "",
        "messages": [{"role": "user", "content": "旧消息"}],
        "todos": todos, "virtual_fs": vfs, "waker": waker,
        "schema_version": 3,
    }, ensure_ascii=False), encoding="utf-8")


# ════════════════════════════════════════════════════════════════
# 1. roundtrip：save → load 状态逐字段一致
# ════════════════════════════════════════════════════════════════
class TestRoundtrip:

    def test_save_then_load_fields_identical(self):
        todos, vfs, waker = _state([{"t": 1}, {"t": 2}], {"a.txt": "1"}, "researcher")
        ss.save_session("u1", [{"role": "user", "content": "hi"}], "rt1",
                        todos=todos, virtual_fs=vfs, waker=waker)
        _, got_todos, got_vfs, got_waker = ss.load_session("u1", "rt1")
        assert (got_todos, got_vfs, got_waker) == (todos, vfs, waker)
        # kv 行同步落了（权威在）
        assert sst.load_state("rt1") == (todos, vfs, waker)

    def test_events_first_state_identical(self, events_log):
        todos, vfs, waker = _state([{"t": 9}], {"b.txt": "2"}, "critic")
        ss.save_session("u1", [{"role": "user", "content": "旧"}], "rt2",
                        todos=todos, virtual_fs=vfs, waker=waker)
        events_log.append("rt2", USER_MSG, {"content": "新问题"})
        events_log.append("rt2", ASSISTANT_MSG, {"content": "新回答"})

        msgs, got_todos, got_vfs, got_waker = ss.load_session_events_first(
            "u1", "rt2", session_log=events_log)
        assert msgs == [{"role": "user", "content": "新问题"},
                        {"role": "assistant", "content": "新回答"}]
        assert (got_todos, got_vfs, got_waker) == (todos, vfs, waker)

    def test_waker_empty_string_roundtrip(self):
        """显式 waker=""（默认助手）也是合法状态，kv 原样保存读回。"""
        ss.save_session("u1", [{"role": "user", "content": "hi"}], "rt3", waker="")
        assert ss.load_session("u1", "rt3")[3] == ""
        assert sst.load_state("rt3")[2] == ""

    def test_second_save_overwrites_kv(self):
        """kv 是最新快照语义：二次保存覆盖（不留历史版本）。"""
        ss.save_session("u1", [{"role": "user", "content": "1"}], "rt4", waker="a")
        ss.save_session("u1", [{"role": "user", "content": "2"}], "rt4", waker="b")
        assert sst.load_state("rt4")[2] == "b"
        assert ss.load_session("u1", "rt4")[3] == "b"


# ════════════════════════════════════════════════════════════════
# 2. 旧会话回退 + kv 回填（迁移自愈）
# ════════════════════════════════════════════════════════════════
class TestLegacyFallbackBackfill:

    def test_json_only_loads_and_backfills_kv(self, events_log):
        """只有 JSON 无 kv（旧会话）→ 状态按 JSON 加载，且 kv 行回填。"""
        todos, vfs, waker = _state([{"legacy": True}], {"old.txt": "0"}, "night-watcher")
        _write_json_only("u1", "lg1", todos, vfs, waker)
        events_log.append("lg1", USER_MSG, {"content": "新问题"})

        _, got_todos, got_vfs, got_waker = ss.load_session_events_first(
            "u1", "lg1", session_log=events_log)
        assert (got_todos, got_vfs, got_waker) == (todos, vfs, waker)
        # 回填完成：kv 行已是权威（删 JSON 后再读仍一致）
        assert sst.load_state("lg1") == (todos, vfs, waker)

    def test_corrupt_kv_row_heals_from_json(self):
        """kv 行形状非法（旧版本/损坏）→ 回退 JSON 且回填修复。"""
        todos, vfs, waker = _state([{"t": 5}], {"c.txt": "3"}, "critic")
        ss.save_session("u1", [{"role": "user", "content": "hi"}], "lg2",
                        todos=todos, virtual_fs=vfs, waker=waker)
        # 伪造损坏行（value 不是状态 dict）
        sst.default_provider().kv_put(sst.SCOPE, "lg2", "garbage")

        _, got_todos, got_vfs, got_waker = ss.load_session("u1", "lg2")
        assert (got_todos, got_vfs, got_waker) == (todos, vfs, waker)
        assert sst.load_state("lg2") == (todos, vfs, waker)  # 已被回填覆盖修复

    def test_no_backfill_for_nonexistent_session(self):
        """读不存在的会话（无 JSON 无 kv）→ 默认值，且不繁殖空 kv 行。"""
        assert ss.load_session("u1", "ghost-x") == ([], [], {}, "")
        assert sst.load_state("ghost-x") is None


# ════════════════════════════════════════════════════════════════
# 3. JSON 可丢：删/坏 JSON 后状态仍完整恢复（kv 在）
# ════════════════════════════════════════════════════════════════
class TestJsonDroppable:

    def test_state_survives_json_deletion(self, events_log):
        """核心不变量：删掉 JSON 文件，load_session_events_first 仍完整恢复。"""
        todos, vfs, waker = _state([{"t": 42}], {"keep.txt": "kv"}, "researcher")
        ss.save_session("u1", [{"role": "user", "content": "旧"}], "drop1",
                        todos=todos, virtual_fs=vfs, waker=waker)
        events_log.append("drop1", USER_MSG, {"content": "q"})
        events_log.append("drop1", ASSISTANT_MSG, {"content": "a"})
        ss._get_session_file("u1", "drop1").unlink()
        assert not ss._get_session_file("u1", "drop1").exists()

        msgs, got_todos, got_vfs, got_waker = ss.load_session_events_first(
            "u1", "drop1", session_log=events_log)
        assert msgs == [{"role": "user", "content": "q"},
                        {"role": "assistant", "content": "a"}]
        assert (got_todos, got_vfs, got_waker) == (todos, vfs, waker)

    def test_state_survives_json_corruption(self):
        """JSON 损坏（半截文件）→ 消息空，但状态从 kv 恢复。"""
        todos, vfs, waker = _state([{"t": 7}], {"d.txt": "4"}, "critic")
        ss.save_session("u1", [{"role": "user", "content": "hi"}], "drop2",
                        todos=todos, virtual_fs=vfs, waker=waker)
        ss._get_session_file("u1", "drop2").write_text("{半截", encoding="utf-8")

        msgs, got_todos, got_vfs, got_waker = ss.load_session("u1", "drop2")
        assert msgs == []
        assert (got_todos, got_vfs, got_waker) == (todos, vfs, waker)

    def test_kv_overrides_stale_json(self, events_log):
        """kv 优先：JSON 缓存陈旧（模拟双写窗口的旧值）时以 kv 为准。"""
        ss.save_session("u1", [{"role": "user", "content": "hi"}], "drop3", waker="old")
        # 直接改 kv（模拟 kv 已前进、JSON 缓存未跟上）
        sst.save_state("drop3", [{"t": 2}], {}, "new")
        events_log.append("drop3", USER_MSG, {"content": "q"})

        _, got_todos, got_vfs, got_waker = ss.load_session_events_first(
            "u1", "drop3", session_log=events_log)
        assert (got_todos, got_vfs, got_waker) == ([{"t": 2}], {}, "new")


# ════════════════════════════════════════════════════════════════
# 4. fork：源 kv 状态行复制到新 sid
# ════════════════════════════════════════════════════════════════
class _FakeCtx:
    def __init__(self, mapping):
        self._mapping = mapping

    def try_get(self, name):
        return self._mapping.get(name)


@pytest.fixture
def fork_app(tmp_path):
    """仅挂 sessions router 的精简 app；SessionLog 指 tmp 独立库。"""
    provider = SQLiteProvider(db_path=tmp_path / "fork-events.db")
    log = SessionLog(provider=provider)
    a = FastAPI()
    a.include_router(sessions_router.router, prefix="/api/sessions")
    a.state.cordis_ctx = _FakeCtx({"sessions": log})
    yield a, log
    provider.close()


class TestForkKvCopy:

    def test_fork_copies_kv_state_row(self, fork_app):
        """fork 后新 sid 的 kv 行 == 源状态（加载即继承 todos/vfs/waker）。"""
        app, log = fork_app
        todos, vfs, waker = _state([{"t": 1}], {"a.txt": "1"}, "night-watcher")
        ss.save_session(LOCAL_USER, [{"role": "user", "content": "源"}], "fsrc1",
                        todos=todos, virtual_fs=vfs, waker=waker)
        log.append("fsrc1", USER_MSG, {"content": "源"})

        r = TestClient(app).post("/api/sessions/fsrc1/fork")
        assert r.status_code == 200, r.text
        new_sid = r.json()["session_id"]

        assert sst.load_state(new_sid) == (todos, vfs, waker)
        _, got_todos, got_vfs, got_waker = ss.load_session_events_first(
            LOCAL_USER, new_sid, session_log=log)
        assert (got_todos, got_vfs, got_waker) == (todos, vfs, waker)

    def test_fork_kv_wins_over_stale_or_missing_json(self, fork_app):
        """源 JSON 缓存已删、仅 kv 在：fork 仍把完整状态带给新 sid
        （stub 透传拿不到 JSON 值，kv 行直拷兜底——kv 权威的意义所在）。"""
        app, log = fork_app
        todos, vfs, waker = _state([{"k": 1}], {"kv.txt": "9"}, "researcher")
        ss.save_session(LOCAL_USER, [{"role": "user", "content": "源"}], "fsrc2",
                        todos=todos, virtual_fs=vfs, waker=waker)
        log.append("fsrc2", USER_MSG, {"content": "源"})
        ss._get_session_file(LOCAL_USER, "fsrc2").unlink()

        r = TestClient(app).post("/api/sessions/fsrc2/fork")
        assert r.status_code == 200, r.text
        new_sid = r.json()["session_id"]
        assert sst.load_state(new_sid) == (todos, vfs, waker)

    def test_fork_from_legacy_source_backfills_via_stub(self, fork_app):
        """源是旧会话（只有 JSON 无 kv）→ copy_state no-op，stub 透传值
        落 kv 兜底，新 sid 状态继承不丢。"""
        app, log = fork_app
        todos, vfs, waker = _state([{"legacy": 1}], {"o.txt": "0"}, "critic")
        _write_json_only(LOCAL_USER, "fsrc3", todos, vfs, waker)
        log.append("fsrc3", USER_MSG, {"content": "源"})
        assert sst.load_state("fsrc3") is None  # 前置：源无 kv 行

        r = TestClient(app).post("/api/sessions/fsrc3/fork")
        assert r.status_code == 200, r.text
        new_sid = r.json()["session_id"]
        assert sst.load_state(new_sid) == (todos, vfs, waker)


# ════════════════════════════════════════════════════════════════
# 5. purge/删会话：kv 行联动清理
# ════════════════════════════════════════════════════════════════
class TestPurgeKv:

    def test_worker_session_delete_removes_kv_row(self, monkeypatch, events_log):
        """worker session_delete op：JSON / 事件流 / kv 状态行三清。"""
        import web_fastapi.worker_process as wp

        todos, vfs, waker = _state([{"t": 1}], {"del.txt": "x"}, "critic")
        ss.save_session(LOCAL_USER, [{"role": "user", "content": "hi"}], "delkv1",
                        todos=todos, virtual_fs=vfs, waker=waker)
        events_log.append("delkv1", USER_MSG, {"content": "hi"})
        assert sst.load_state("delkv1") is not None  # 前置：kv 行在

        state = wp.WorkerState(LOCAL_USER)
        state.agent = SimpleNamespace(_session_log=events_log)
        sent = []
        monkeypatch.setattr(wp, "_send", lambda msg, **k: sent.append(msg))
        wp.handle_command(state, {"id": "r1", "op": "session_delete",
                                  "session_id": "delkv1"})

        assert sent[0]["data"]["ok"] is True
        assert not ss._get_session_file(LOCAL_USER, "delkv1").exists()
        assert events_log.events("delkv1") == []
        assert sst.load_state("delkv1") is None  # kv 行联动清理

    def test_delete_state_idempotent(self):
        ss.save_session("u1", [{"role": "user", "content": "hi"}], "delkv2")
        assert sst.delete_state("delkv2") is True
        assert sst.delete_state("delkv2") is False  # 再删幂等

    def test_projects_cascade_fallback_removes_kv_row(self, tmp_path):
        """项目级联删除的主进程兜底（③）也清 kv 行（无 worker_manager 的路径）。"""
        from web_fastapi.dependencies import get_current_user_id

        storage = SQLiteProvider(db_path=tmp_path / "cascade-projects.db")
        events_provider = SQLiteProvider(db_path=tmp_path / "cascade-events.db")
        log = SessionLog(provider=events_provider)
        a = FastAPI()
        a.state.cordis_ctx = _FakeCtx({
            "storage": storage,
            "workspace": WorkspaceService(storage),
            "sessions": log,
        })
        a.include_router(proj_router.router, prefix="/api/projects")
        a.dependency_overrides[get_current_user_id] = lambda: LOCAL_USER
        client = TestClient(a)

        r = client.post("/api/projects", json={"name": "级联项目"})
        assert r.status_code == 200, r.text
        slug = r.json()["project"]["slug"]

        todos, vfs, waker = _state([{"t": 3}], {"p.txt": "5"}, "critic")
        ss.save_session(LOCAL_USER, [{"role": "user", "content": "hi"}], "delkv3",
                        todos=todos, virtual_fs=vfs, waker=waker, project=slug)
        log.append("delkv3", USER_MSG, {"content": "hi"})
        assert sst.load_state("delkv3") is not None  # 前置：kv 行在

        r = client.delete(f"/api/projects/{slug}?force=true")
        assert r.status_code == 200, r.text
        assert r.json()["sessions_deleted"] == 1
        assert sst.load_state("delkv3") is None  # 兜底路径同样清 kv 行
        storage.close()
        events_provider.close()


# ════════════════════════════════════════════════════════════════
# 6. 降级安全：kv 写失败不阻断 JSON 保存
# ════════════════════════════════════════════════════════════════
class TestDegradation:

    def test_kv_write_failure_still_saves_json(self, monkeypatch):
        """kv 先行失败 → 仅告警，JSON 照常落盘；恢复后 load 回退 JSON 并回填。"""
        todos, vfs, waker = _state([{"t": 8}], {"e.txt": "6"}, "researcher")

        def _boom(*a, **k):
            raise RuntimeError("kv down")

        provider = sst.default_provider()
        real_put = provider.kv_put
        monkeypatch.setattr(provider, "kv_put", _boom)
        ss.save_session("u1", [{"role": "user", "content": "hi"}], "deg1",
                        todos=todos, virtual_fs=vfs, waker=waker)
        # JSON 缓存在（kv 缺席）
        data = json.loads(ss._get_session_file("u1", "deg1").read_text(encoding="utf-8"))
        assert data["waker"] == "researcher" and data["todos"] == todos

        # kv 恢复：load 回退 JSON 值并回填
        monkeypatch.setattr(provider, "kv_put", real_put)
        _, got_todos, got_vfs, got_waker = ss.load_session("u1", "deg1")
        assert (got_todos, got_vfs, got_waker) == (todos, vfs, waker)
        assert sst.load_state("deg1") == (todos, vfs, waker)

    def test_kv_read_failure_falls_back_to_json(self, monkeypatch):
        """kv 读失败 → 回退 JSON 快照值（行为与迁移前一致）。"""
        todos, vfs, waker = _state([{"t": 6}], {"g.txt": "7"}, "critic")
        ss.save_session("u1", [{"role": "user", "content": "hi"}], "deg2",
                        todos=todos, virtual_fs=vfs, waker=waker)

        def _boom(*a, **k):
            raise RuntimeError("kv down")

        monkeypatch.setattr(sst.default_provider(), "kv_get", _boom)
        _, got_todos, got_vfs, got_waker = ss.load_session("u1", "deg2")
        assert (got_todos, got_vfs, got_waker) == (todos, vfs, waker)


# ════════════════════════════════════════════════════════════════
# 7. 并发写安全：kv 与 events 同库同约定（模块锁 + WAL）
# ════════════════════════════════════════════════════════════════
class TestConcurrentWrites:

    def test_parallel_saves_with_event_appends(self, events_log):
        """多线程 save_session（kv+JSON 双写）与事件追加并发：零异常、
        终态全部可读——与 events 写同一连接约定（模块锁串行 + WAL）。"""
        errors: list[Exception] = []

        def _saver(i: int):
            try:
                for j in range(6):
                    sid = f"cc{i}{j}"
                    ss.save_session("u1", [{"role": "user", "content": f"m{i}{j}"}],
                                    sid, todos=[{"i": i, "j": j}],
                                    virtual_fs={f"f{i}": str(j)}, waker=f"w{i}")
                    events_log.append(sid, USER_MSG, {"content": f"m{i}{j}"})
            except Exception as e:  # pragma: no cover —— 失败经 errors 断言
                errors.append(e)

        threads = [threading.Thread(target=_saver, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        for i in range(4):
            for j in range(6):
                sid = f"cc{i}{j}"
                _, got_todos, got_vfs, got_waker = ss.load_session("u1", sid)
                assert (got_todos, got_vfs, got_waker) == (
                    [{"i": i, "j": j}], {f"f{i}": str(j)}, f"w{i}")
                assert events_log.events(sid)[0]["payload"]["content"] == f"m{i}{j}"


# ════════════════════════════════════════════════════════════════
# 8. stub 写 kv：fork 透传 / chat 开轮 stub 的 kv 侧兜底
# ════════════════════════════════════════════════════════════════
class TestStubWritesKv:

    def test_ensure_stub_writes_kv(self):
        created = ss.ensure_session_stub(
            "u1", [{"role": "user", "content": "hi"}], "stub1",
            todos=[{"t": 1}], virtual_fs={"s.txt": "x"}, waker="researcher")
        assert created is True
        assert sst.load_state("stub1") == ([{"t": 1}], {"s.txt": "x"}, "researcher")

    def test_stub_decline_keeps_existing_kv(self):
        """stub 只做「从无到有」：JSON 已在时放弃，kv 权威行不被旁路覆盖。"""
        ss.save_session("u1", [{"role": "user", "content": "权威"}], "stub2", waker="critic")
        created = ss.ensure_session_stub(
            "u1", [{"role": "user", "content": "旁路"}], "stub2", waker="other")
        assert created is False
        assert sst.load_state("stub2")[2] == "critic"
