"""M1 回归：mcp 路由的 worker.send 不在事件循环内执行。

mcp.py 全部 5 个 handler（list_servers / add_server / reload_servers /
set_enabled / remove_server）直接调 worker.send——它同步抢 per-worker
锁（lock_wait 最长 5s，mcp_reload 触发重连更久）。旧实现是 async def，
send 阻塞期间整个 uvicorn 事件循环被冻结（主槽 chat 流式持锁时全站
停摆，P2-11 同源）；修后为同步 def，FastAPI 丢线程池执行。

判定手法同 test_worker_send_off_loop.py：worker.send 执行时若当前线程
存在运行中的事件循环（asyncio.get_running_loop 成功）＝调用发生在
async handler 内（冻结事件循环）；线程池内执行时必无运行循环。
"""
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.constants import LOCAL_USER
from web_fastapi.dependencies import get_current_user_id
from web_fastapi.routers import mcp as mcp_router


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class _FakeWorker:
    def __init__(self):
        self.sent = []
        self.send_on_loop = None

    def send(self, op, **kw):
        self.sent.append((op, kw))
        self.send_on_loop = _on_event_loop()
        return [{"type": "result", "data": {"ok": True}}]


class _FakeManager:
    def __init__(self):
        self.worker = _FakeWorker()

    def get_or_create(self, user_id=LOCAL_USER):
        return self.worker


def _make_app() -> tuple[FastAPI, _FakeManager]:
    a = FastAPI()
    m = _FakeManager()
    a.state.worker_manager = m
    a.include_router(mcp_router.router, prefix="/api/mcp")
    a.dependency_overrides[get_current_user_id] = lambda: LOCAL_USER
    return a, m


def test_mcp_handlers_send_off_event_loop():
    app, m = _make_app()
    c = TestClient(app)
    assert c.get("/api/mcp/servers").status_code == 200
    assert c.post("/api/mcp/servers", json={
        "name": "fs", "command": "uvx", "args": ["mcp-server-fetch"],
    }).status_code == 200
    assert c.post("/api/mcp/reload").status_code == 200
    assert c.patch("/api/mcp/servers/fs/enabled",
                   json={"enabled": False}).status_code == 200
    assert c.delete("/api/mcp/servers/fs").status_code == 200

    w = m.worker
    # 5 个 handler 全部路由到 main 槽 worker，且 send 都发生在事件循环外
    assert [op for op, _ in w.sent] == [
        "mcp_list", "mcp_add", "mcp_reload", "mcp_set_enabled", "mcp_remove",
    ]
    assert w.send_on_loop is False, (
        "mcp handler 在事件循环内同步调 worker.send（M1 回归）"
    )


def test_mcp_slow_send_does_not_freeze_sibling_request():
    """行为级回归：mcp 的 worker.send 阻塞期间，其余请求不被冻结。

    慢 send 只允许占一个线程池线程（同步 def 路由）；若回归成 async
    handler 同步调 send，共享事件循环上的探针请求会被冻结到 send 结束。
    """
    import threading
    import time

    app, m = _make_app()
    entered = threading.Event()

    def slow_send(op, **kw):
        if op == "mcp_reload":  # 只慢 reload；probe 的 mcp_add 立即返回
            entered.set()
            time.sleep(2.0)  # 模拟主槽持锁 / mcp_reload 重连
        return [{"type": "result", "data": {"ok": True}}]

    m.worker.send = slow_send

    with TestClient(app) as c:
        t = threading.Thread(
            target=lambda: c.post("/api/mcp/reload"))
        t.start()
        assert entered.wait(5.0)

        t0 = time.monotonic()
        r = c.post("/api/mcp/servers", json={"name": "probe"})
        probe_elapsed = time.monotonic() - t0
        assert r.status_code == 200
        assert probe_elapsed < 1.0, (
            f"事件循环被 mcp worker.send 冻结 {probe_elapsed:.2f}s（M1 回归）"
        )
    t.join(10.0)
    assert not t.is_alive()
