"""
RunRegistry 测试（T8b 运行登记表入库）。

覆盖：
- upsert/get/list/update_status 基本语义（拷贝隔离、no-op 语义）
- kv 持久：新实例同库恢复（重启可见）
- running→interrupted 恢复语义（残留活跃态标 interrupted，不自动续跑）
- 写穿失败降级：kv_put 抛异常不阻断内存读写
- 三宿主接入烟测：WakerAsyncRunner / FlowRunner / MemoryConsolidationScheduler
  构造时注入 storage，登记表进 RunRegistry 且 get/list 对外形状保持

隔离：全部用 tmp_path 下的 SQLiteProvider，不碰真实 data/hermes.db。
"""
import threading

import pytest

from src.storage.run_registry import SCOPE, RunRegistry
from src.storage.sqlite_provider import SQLiteProvider


@pytest.fixture
def db(tmp_path):
    """tmp 隔离的 KV provider。"""
    return SQLiteProvider(db_path=tmp_path / "runs.db")


def _record(run_id="r1", status="running", **kw):
    base = {
        "run_id": run_id, "flow_name": "f", "user_id": "u1",
        "status": status, "started_at": "2026-08-15T10:00:00",
        "finished_at": "",
    }
    base.update(kw)
    return base


# ============================================
# 基本读写语义
# ============================================
def test_upsert_get_list(db):
    reg = RunRegistry("flow", db)
    reg.upsert(_record("r1"))
    reg.upsert(_record("r2", status="completed"))

    got = reg.get("r1")
    assert got["run_id"] == "r1"
    assert got["status"] == "running"
    assert reg.get("nope") is None

    ids = [r["run_id"] for r in reg.list()]
    assert ids == ["r1", "r2"]  # 保持写入顺序


def test_get_returns_copy(db):
    """get/list 返回拷贝，外部改不动内部状态。"""
    reg = RunRegistry("flow", db)
    reg.upsert(_record("r1"))
    reg.get("r1")["status"] = "hacked"
    reg.list()[0]["status"] = "hacked2"
    assert reg.get("r1")["status"] == "running"


def test_upsert_overwrites_by_run_id(db):
    reg = RunRegistry("flow", db)
    reg.upsert(_record("r1", status="running"))
    reg.upsert(_record("r1", status="completed", finished_at="2026-08-15T10:05:00"))
    assert len(reg) == 1
    got = reg.get("r1")
    assert got["status"] == "completed"
    assert got["finished_at"] == "2026-08-15T10:05:00"


def test_upsert_requires_run_id(db):
    reg = RunRegistry("flow", db)
    with pytest.raises(ValueError):
        reg.upsert({"status": "running"})


def test_delete_terminal_record(db):
    reg = RunRegistry("tasks", db)
    reg.upsert(_record("r1", status="failed"))
    reg.upsert(_record("r2", status="running"))
    assert reg.delete("r1") is True
    assert reg.get("r1") is None
    assert reg.delete("nope") is False
    assert reg.delete("r2") is False   # 活跃态不删
    assert reg.get("r2")["status"] == "running"
    # 重启后仍看不见已删记录
    reg2 = RunRegistry("tasks", db)
    assert reg2.get("r1") is None
    assert reg2.get("r2")["status"] == "interrupted"  # 残留 running 恢复为 interrupted


def test_update_status_partial_fields(db):
    reg = RunRegistry("flow", db)
    reg.upsert(_record("r1", returns={}))
    # 只改提供的字段，其余保持
    reg.update_status("r1", status="completed", returns={"out": 1})
    got = reg.get("r1")
    assert got["status"] == "completed"
    assert got["returns"] == {"out": 1}
    assert got["started_at"] == "2026-08-15T10:00:00"  # 未动的字段不变


def test_update_status_missing_run_is_noop(db):
    reg = RunRegistry("flow", db)
    reg.update_status("ghost", status="error")  # 不抛
    assert reg.get("ghost") is None
    assert len(reg) == 0


# ============================================
# kv 持久（重启可见）
# ============================================
def test_kv_persistence_same_db_new_instance(tmp_path):
    db1 = SQLiteProvider(db_path=tmp_path / "runs.db")
    reg = RunRegistry("flow", db1)
    reg.upsert(_record("r1", status="completed"))
    reg.upsert(_record("r2", status="running"))
    reg.update_status("r2", status="ok", finished_at="t")

    # 模拟重启：同库新实例
    db2 = SQLiteProvider(db_path=tmp_path / "runs.db")
    reg2 = RunRegistry("flow", db2)
    assert reg2.get("r1")["status"] == "completed"
    assert reg2.get("r2")["status"] == "ok"

    # kv 布局：scope="runs"，key=宿主名，value=dict 列表
    raw = db2.kv_get(SCOPE, "flow")
    assert isinstance(raw, list) and {r["run_id"] for r in raw} == {"r1", "r2"}


def test_scope_keys_isolated(db):
    """不同 scope_key 互不可见（三宿主同库共存）。"""
    flow = RunRegistry("flow", db)
    waker = RunRegistry("waker_async", db)
    flow.upsert(_record("f1"))
    waker.upsert({"run_id": "w1", "waker_name": "n", "status": "running"})
    assert flow.get("w1") is None
    assert waker.get("f1") is None
    assert len(flow) == 1 and len(waker) == 1


# ============================================
# running → interrupted 恢复语义
# ============================================
def test_running_marked_interrupted_on_reload(tmp_path):
    """重启后 running/pending 残留标 interrupted；终态保持不变。"""
    db1 = SQLiteProvider(db_path=tmp_path / "runs.db")
    reg = RunRegistry("flow", db1)
    reg.upsert(_record("r-run", status="running"))
    reg.upsert(_record("r-pend", status="pending"))
    reg.upsert(_record("r-done", status="completed"))
    reg.upsert(_record("r-fail", status="failed"))

    db2 = SQLiteProvider(db_path=tmp_path / "runs.db")
    reg2 = RunRegistry("flow", db2)
    assert reg2.get("r-run")["status"] == "interrupted"
    assert reg2.get("r-pend")["status"] == "interrupted"
    assert reg2.get("r-done")["status"] == "completed"
    assert reg2.get("r-fail")["status"] == "failed"

    # 修正后的状态已写回 kv 固化（再次重启不再重复标）
    db3 = SQLiteProvider(db_path=tmp_path / "runs.db")
    reg3 = RunRegistry("flow", db3)
    assert reg3.get("r-run")["status"] == "interrupted"
    assert all(r["status"] != "running" for r in reg3.list())


def test_custom_active_statuses_and_interrupted_value(db):
    """active_statuses / interrupted_status 可参数化（宿主自定义状态机）。"""
    db.kv_put(SCOPE, "custom", [
        {"run_id": "x", "status": "in-flight"},
        {"run_id": "y", "status": "done"},
    ])
    reg = RunRegistry(
        "custom", db, interrupted_status="stale",
        active_statuses=("in-flight",),
    )
    assert reg.get("x")["status"] == "stale"
    assert reg.get("y")["status"] == "done"


def test_corrupted_kv_payload_starts_empty(db):
    """kv 里存了非列表（历史脏数据）→ 按空表启动，不抛。"""
    db.kv_put(SCOPE, "flow", {"not": "a list"})
    reg = RunRegistry("flow", db)
    assert len(reg) == 0


# ============================================
# 写穿失败降级（kv 失败仅告警不阻断）
# ============================================
class _FlakyKV:
    """kv_put 永远失败的 provider（kv_get 正常）。"""

    def __init__(self, inner):
        self._inner = inner
        self.put_calls = 0

    def kv_get(self, scope, key, default=None):
        return self._inner.kv_get(scope, key, default)

    def kv_put(self, scope, key, value):
        self.put_calls += 1
        raise OSError("disk full")

    def kv_delete(self, scope, key):
        return self._inner.kv_delete(scope, key)

    def kv_list(self, scope):
        return self._inner.kv_list(scope)


def test_write_through_failure_does_not_block(db):
    flaky = _FlakyKV(db)
    reg = RunRegistry("flow", flaky)
    # upsert / update_status 不因 kv_put 失败而抛
    reg.upsert(_record("r1"))
    reg.update_status("r1", status="ok")
    assert flaky.put_calls >= 2
    # 内存快路径不受影响
    assert reg.get("r1")["status"] == "ok"
    assert len(reg) == 1


def test_load_failure_degrades_to_empty():
    """kv_get 抛异常 → 按空表启动 + 后续写穿失败不阻断。"""

    class _BrokenKV:
        def kv_get(self, scope, key, default=None):
            raise OSError("db unavailable")

        def kv_put(self, scope, key, value):
            raise OSError("db unavailable")

    reg = RunRegistry("flow", _BrokenKV())
    assert len(reg) == 0
    reg.upsert(_record("r1"))
    assert reg.get("r1")["run_id"] == "r1"


def test_thread_safety_concurrent_upserts(db):
    """多线程并发 upsert/update_status 不丢不串（内存 dict 持锁）。"""
    reg = RunRegistry("waker_async", db)
    errs = []

    def _worker(n):
        try:
            for i in range(20):
                reg.upsert({"run_id": f"r{n}-{i}", "status": "running", "user_id": f"u{n}"})
                reg.update_status(f"r{n}-{i}", status="ok")
        except Exception as e:  # pragma: no cover
            errs.append(e)

    threads = [threading.Thread(target=_worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs
    assert len(reg) == 80
    assert all(r["status"] == "ok" for r in reg.list())


# ============================================
# 三宿主接入烟测
# ============================================
def test_waker_async_runner_registry_roundtrip(tmp_path):
    from src.waker.async_runner import WakerAsyncRunner

    db = SQLiteProvider(db_path=tmp_path / "a.db")
    r1 = WakerAsyncRunner(storage=db)
    r1._runs.upsert({
        "run_id": "wr1", "waker_name": "w", "user_id": "u1",
        "status": "running", "started_at": "t0", "finished_at": "",
    })
    # 对外形状保持
    assert r1.get_status("wr1") == {
        "run_id": "wr1", "waker_name": "w", "status": "running",
        "started_at": "t0", "finished_at": "",
    }
    assert [a["run_id"] for a in r1.list_active("u1")] == ["wr1"]

    # 重启可见：同库新 runner 恢复，running → interrupted，不再算 active
    db2 = SQLiteProvider(db_path=tmp_path / "a.db")
    r2 = WakerAsyncRunner(storage=db2)
    assert r2.get_status("wr1")["status"] == "interrupted"
    assert r2.list_active("u1") == []


def test_flow_runner_registry_roundtrip(tmp_path):
    from src.wakerflow.runner import FlowRunner

    db = SQLiteProvider(db_path=tmp_path / "f.db")
    f1 = FlowRunner(storage=db)
    f1._runs.upsert({
        "run_id": "fr1", "flow_name": "demo", "user_id": "u1",
        "status": "running", "started_at": "2026-08-15T10:00:00",
        "finished_at": "", "returns": {}, "error": "",
    })
    f1._set_status("fr1", "completed", returns={"a": 1})
    assert f1.get_status("fr1")["status"] == "completed"
    assert f1.get_status("fr1")["returns"] == {"a": 1}
    assert [r["run_id"] for r in f1.list_recent("u1")] == ["fr1"]

    # 重启恢复：completed 保持；未完成的 running → interrupted
    f1._runs.upsert({
        "run_id": "fr2", "flow_name": "demo", "user_id": "u1",
        "status": "running", "started_at": "2026-08-15T11:00:00",
        "finished_at": "", "returns": {}, "error": "",
    })
    db2 = SQLiteProvider(db_path=tmp_path / "f.db")
    f2 = FlowRunner(storage=db2)
    assert f2.get_status("fr1")["status"] == "completed"
    assert f2.get_status("fr2")["status"] == "interrupted"
    assert f2.list_active("u1") == []


def test_memory_consolidation_scheduler_registry(tmp_path):
    """scheduler 手动触发的登记表走 RunRegistry（_runs 不再是裸 dict）。"""
    from src.memory.scheduler import MemoryConsolidationScheduler

    db = SQLiteProvider(db_path=tmp_path / "m.db")
    s = MemoryConsolidationScheduler(storage=db)
    try:
        assert isinstance(s._runs, RunRegistry)
        s._runs.upsert({"run_id": "mr1", "status": "running", "result": None, "user_id": "u"})
        assert s.get_status("mr1") == {"status": "running", "result": None}
        s._runs.update_status("mr1", status="done", result={"ok": True})
        assert s.get_status("mr1") == {"status": "done", "result": {"ok": True}}
        assert s.get_status("nope") == {"status": "not_found", "result": None}
    finally:
        s.stop()
