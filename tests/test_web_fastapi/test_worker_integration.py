"""
Worker 进程集成测试（M5 多槽位）：fork 真实子进程，验证 IPC + 槽位语义。

这些测试启动真实的 worker_process 子进程（含 MemoryManager + HermesAgent），
所以需要有效的 config.yaml。不调 LLM 的操作（tools_list/prefs/current_session）
即使没有 LLM 连接也能通过。

槽位语义（ADR-0005/M5）：键 = (LOCAL_USER, slot)。
- 默认槽 "main"：杂项 op；无 session_id 的请求都落这里（与历史行为一致）。
- 会话槽：同 slot 同实例（亲和路由），异 slot 异实例（并行生成的载体）。
"""
import time

import pytest
from src.constants import LOCAL_USER
from web_fastapi.worker_manager import WorkerManager


@pytest.fixture
def mgr(tmp_path):
    m = WorkerManager(max_parallel=4,   # 固定上限，测试内不受 settings 影响
                      state_path=tmp_path / "web_state.json")  # 隔离真实镜像文件
    yield m
    m.shutdown_all()


def test_worker_starts_and_responds(mgr):
    """main 槽 worker 能启动并发送 ready + 响应 tools_list；身份恒 LOCAL_USER。"""
    wp = mgr.get_or_create("test_user_1")
    assert wp.user_id == LOCAL_USER
    assert wp.slot == "main"
    assert wp.is_alive()
    events = wp.send("tools_list")
    assert len(events) >= 1
    data = events[0]["data"]
    assert "tools" in data
    assert data["count"] > 0


def test_same_slot_same_instance(mgr):
    """亲和路由：同 slot 多次 get_or_create 返回同一实例。"""
    wa = mgr.get_or_create(LOCAL_USER, slot="main")
    wa2 = mgr.get_or_create(LOCAL_USER, slot="main")
    assert wa is wa2
    assert wa.proc.pid == wa2.proc.pid


def test_distinct_slots_distinct_instances(mgr):
    """并行载体：不同 slot 各自独立子进程（这是多标签页并行生成的机制）。"""
    wa = mgr.get_or_create(LOCAL_USER, slot="sess-a")
    wb = mgr.get_or_create(LOCAL_USER, slot="sess-b")
    assert wa is not wb
    assert wa.proc.pid != wb.proc.pid
    slots = {s for (_, s) in mgr.all_users()}
    assert slots == {"sess-a", "sess-b"}     # 本用例未触碰 main 槽


def test_prefs_roundtrip_single_worker(mgr):
    wp = mgr.get_or_create(LOCAL_USER, slot="main")
    wp.send("prefs_set", prefs={"temperature": 0.2, "workspace_root": "/local"})
    events = wp.send("prefs_get")
    prefs = events[0]["data"]["prefs"]
    assert prefs["temperature"] == 0.2
    assert prefs["workspace_root"] == "/local"


def test_mirror_replayed_into_new_slot(mgr):
    """用户级状态镜像：main 槽设置的 prefs/权限模式，新会话槽 spawn 即重放。"""
    main = mgr.get_or_create(LOCAL_USER, slot="main")
    main.send("prefs_set", prefs={"temperature": 0.33})
    mgr.remember_prefs({"temperature": 0.33})
    mgr.remember_permission_mode("full_access")

    sess = mgr.get_or_create(LOCAL_USER, slot="mirror-1")
    time.sleep(0.4)   # fire_and_forget 异步送达
    prefs = sess.send("prefs_get")[0]["data"]["prefs"]
    assert prefs["temperature"] == 0.33
    mode = sess.send("permission_mode_get")[0]["data"]["permission_mode"]
    assert mode == "full_access"


def test_current_session_responds_and_targets_sid(mgr):
    """current_session：无参返回本槽 current；带 session_id 定位并水合。"""
    wp = mgr.get_or_create(LOCAL_USER, slot="sess-cur")
    events = wp.send("current_session")
    sid = events[0]["data"]["session_id"]
    assert sid
    # 带目标 sid：返回值即该 sid（不存在的历史保持空态，不报错）
    events2 = wp.send("current_session", session_id="ghost000")
    assert events2[0]["data"]["session_id"] == "ghost000"


def test_send_stream_toggles_streaming_flag(mgr):
    """streaming 标志：流在途为 True，耗尽后复位（/api/chat/active 的依据）。"""
    wp = mgr.get_or_create(LOCAL_USER, slot="sess-flag")
    assert wp.streaming is False
    g = wp.send_stream("tools_list")
    next(g)                                    # 进入流：锁 + 标志置位
    assert wp.streaming is True
    for _ in g:                                # 消费至 done
        pass
    assert wp.streaming is False


def test_reset_accepts_main_generated_sid(mgr):
    """reset 接受主进程下发的 new_sid（修跨槽 sid 错位）。"""
    wp = mgr.get_or_create(LOCAL_USER, slot="sess-rst")
    events = wp.send("session_reset", new_sid="zzzz9999")
    assert events[0]["data"]["session_id"] == "zzzz9999"
    cur = wp.send("current_session")[0]["data"]["session_id"]
    assert cur == "zzzz9999"


def test_worker_remove_kills_process(mgr):
    """remove 关闭指定槽；注册表同步清空。"""
    wp = mgr.get_or_create(LOCAL_USER, slot="sess-rm")
    assert wp.is_alive()
    mgr.remove(LOCAL_USER, slot="sess-rm")
    time.sleep(1)
    assert ("local", "sess-rm") not in mgr.all_users()


def test_worker_shutdown_all(mgr):
    """shutdown_all 关闭全部槽位实例。"""
    mgr.get_or_create(LOCAL_USER, slot="main")
    mgr.get_or_create(LOCAL_USER, slot="sess-s1")
    mgr.get_or_create(LOCAL_USER, slot="sess-s2")
    assert len(mgr.all_users()) == 3
    mgr.shutdown_all()
    assert mgr.all_users() == []
