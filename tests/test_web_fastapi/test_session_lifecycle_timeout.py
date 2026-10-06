"""P2-18 回归：会话退出总结的超时保护是真超时。

旧实现 future.result(timeout) 超时抛出后，with 块退出的
executor.shutdown(wait=True) 仍同步 join 到 LLM 跑完——超时被击穿，
/exit 与 worker 退出照样被卡死的总结拖住。修复后总结跑在自管 daemon
线程上、join 有界：超时后调用方立即拿到"未总结"结果，不再阻塞退出；
超时的总结线程若进程保持存活可能仍在后台完成（结果不再采用）。
"""
import threading
import time

import src.agent.session_lifecycle as sl


def test_timeout_returns_promptly_and_leaves_daemon_thread(monkeypatch):
    release = threading.Event()
    entered = threading.Event()

    class _FakeSummarizer:
        def __init__(self, *a, **k):
            pass

        def summarize_and_store(self, user_id, text, sid, **kw):
            entered.set()
            release.wait(10.0)  # 模拟 LLM 卡死
            return {"summary_stored": True, "facts_count": 1,
                    "markdown_path": None, "summary_text": "s"}

    monkeypatch.setattr("src.memory.summarizer.Summarizer", _FakeSummarizer)

    t0 = time.monotonic()
    result = sl.on_session_end(
        manager=None, user_id="local", session_id="tout1234",
        messages=[{"role": "user", "content": "hi"}],
        timeout=0.4,
    )
    elapsed = time.monotonic() - t0

    assert entered.wait(3.0), "总结线程未启动"

    # 超时保护生效：不等 LLM 跑完即返回"未总结"
    assert result == {"summary_stored": False, "facts_count": 0,
                      "markdown_path": None, "summary_text": ""}
    assert elapsed < 3.0, f"超时保护被同步 join 击穿（耗时 {elapsed:.2f}s，P2-18 回归）"

    # 超时的总结在 daemon 线程里继续（进程保持存活时可能仍在后台完成）
    bg = [t for t in threading.enumerate() if t.name == "session-summary"]
    assert bg and bg[0].daemon

    release.set()
    deadline = time.time() + 10
    while any(t.is_alive() for t in threading.enumerate()
              if t.name == "session-summary") and time.time() < deadline:
        time.sleep(0.02)


def test_result_returned_when_summarize_completes_in_time(monkeypatch):
    class _FakeSummarizer:
        def __init__(self, *a, **k):
            pass

        def summarize_and_store(self, user_id, text, sid, **kw):
            return {"summary_stored": True, "facts_count": 2,
                    "markdown_path": None, "summary_text": "正文"}

    monkeypatch.setattr("src.memory.summarizer.Summarizer", _FakeSummarizer)

    result = sl.on_session_end(
        manager=None, user_id="local", session_id="okin0001",
        messages=[{"role": "user", "content": "hi"},
                  {"role": "assistant", "content": "答"}],
        timeout=5.0,
    )
    assert result["summary_stored"] is True
    assert result["facts_count"] == 2


def test_summarize_error_returns_unstored(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("LLM 瞬断")

    monkeypatch.setattr("src.memory.summarizer.Summarizer", _boom)

    result = sl.on_session_end(
        manager=None, user_id="local", session_id="errr0001",
        messages=[{"role": "user", "content": "hi"}],
        timeout=5.0,
    )
    assert result["summary_stored"] is False
