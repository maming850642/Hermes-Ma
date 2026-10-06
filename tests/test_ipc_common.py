"""
src/ipc.py 公共设施测试（T8b）。

覆盖：
- encode_line/decode_line：NDJSON 行编解码
- configure_subprocess_stdio：幂等 + SSL_CERT_FILE 脏值清洗 + 无 reconfigure
  的替身流不报错
- StdoutWriter：send 队列异步写、send_sync 锁守直写、flush/close、
  管道死亡丢弃、default 序列化兜底、并发锁守（多线程写不交错撕裂）
- read_stdin_lines：逐行 NDJSON 解码进队列、无效行丢弃、EOF 放 None

全部用 io.StringIO/替身对象模拟，不碰真实 stdio。
"""
import io
import json
import queue
import threading
import time

import pytest

from src.ipc import (
    StdoutWriter,
    configure_subprocess_stdio,
    decode_line,
    encode_line,
    read_stdin_lines,
)


# ============================================
# encode_line / decode_line
# ============================================
def test_encode_line_is_ndjson():
    line = encode_line({"type": "event", "data": {"text": "你好"}})
    assert line.endswith("\n")
    assert "\n" not in line[:-1]
    assert json.loads(line)["data"]["text"] == "你好"


def test_encode_line_default_param():
    """default=str 时不可序列化对象被字符串化而非抛错。"""
    line = encode_line({"v": object()}, default=str)
    assert json.loads(line)["v"].startswith("<object object at")


def test_decode_line_roundtrip():
    msg = {"id": "r1", "type": "result", "data": {"ok": True}}
    assert decode_line(encode_line(msg)) == msg


def test_decode_line_invalid():
    assert decode_line("") is None
    assert decode_line("   \n ") is None
    assert decode_line("not json") is None
    assert decode_line("[1, 2]") is None  # 非 JSON 对象


# ============================================
# configure_subprocess_stdio
# ============================================
def test_configure_idempotent_and_cleans_ssl(monkeypatch):
    """幂等（连调两次不炸）+ 无效 SSL_CERT_FILE 被清除。"""
    import os
    monkeypatch.setenv("SSL_CERT_FILE", "Z:/definitely/not/exist.pem")
    configure_subprocess_stdio()
    assert "SSL_CERT_FILE" not in os.environ
    # 幂等：重复调用无副作用
    configure_subprocess_stdio()
    assert "SSL_CERT_FILE" not in os.environ


def test_configure_keeps_valid_ssl(monkeypatch):
    import os
    monkeypatch.setenv("SSL_CERT_FILE", __file__)  # 存在的文件
    configure_subprocess_stdio()
    assert os.environ.get("SSL_CERT_FILE") == __file__


def test_configure_tolerates_streams_without_reconfigure(monkeypatch):
    """替换过 sys.stdout/stderr（无 reconfigure 方法）的环境不报错。"""
    import sys

    class _FakeStream:
        def write(self, s):
            return len(s)

        def flush(self):
            pass

        def readline(self):
            return ""

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", _FakeStream())
    monkeypatch.setattr(sys, "stderr", _FakeStream())
    configure_subprocess_stdio()  # 不应抛异常


# ============================================
# StdoutWriter：send（队列异步）
# ============================================
def test_writer_send_writes_via_queue():
    out = io.StringIO()
    w = StdoutWriter(stream=out)
    w.start()
    w.send({"type": "event", "n": 1})
    w.send({"type": "event", "n": 2})
    assert w.flush(timeout=2.0), "队列未在超时内排空"
    lines = out.getvalue().splitlines()
    assert [json.loads(l)["n"] for l in lines] == [1, 2]


def test_writer_send_never_blocks_caller():
    """send 只入队立即返回（流卡死也不阻塞调用方——由写线程承担）。"""

    class _BlockedStream:
        def write(self, s):
            time.sleep(5)
            return len(s)

        def flush(self):
            pass

    w = StdoutWriter(stream=_BlockedStream())
    w.start()
    t0 = time.monotonic()
    for i in range(1000):
        w.send({"type": "event", "n": i})
    assert time.monotonic() - t0 < 2.0
    # 清理：排空队列 + 终止信号，让卡在 write 的写线程尽快退出
    while not w._queue.empty():
        try:
            w._queue.get_nowait()
        except queue.Empty:
            break
    w._queue.put(None)


def test_writer_close_drains_and_stops():
    out = io.StringIO()
    w = StdoutWriter(stream=out)
    w.start()
    w.send({"type": "a"})
    w.send({"type": "b"})
    w.close(timeout=2.0)
    thread = w._thread
    assert thread is not None and not thread.is_alive()
    # close 前入队的消息都被写完
    got = [json.loads(l)["type"] for l in out.getvalue().splitlines()]
    assert got == ["a", "b"]


# ============================================
# StdoutWriter：send_sync（锁守直写）
# ============================================
def test_writer_send_sync_writes_immediately():
    """send_sync 不依赖后台线程（未 start 也能直写）。"""
    out = io.StringIO()
    w = StdoutWriter(stream=out)
    w.send_sync({"type": "ready", "user": "alice"})
    parsed = json.loads(out.getvalue())
    assert parsed == {"type": "ready", "user": "alice"}


def test_writer_send_sync_lock_guarded_no_interleave():
    """并发 send_sync 的行不交错撕裂（锁守语义）。"""
    out = io.StringIO()
    w = StdoutWriter(stream=out)
    pad = "x" * 200

    def _emit_many(prefix: str):
        for i in range(50):
            w.send_sync({"type": "evt", "pad": f"{prefix}{i}-{pad}"})

    threads = [
        threading.Thread(target=_emit_many, args=(f"t{n}-",)) for n in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = out.getvalue().splitlines()
    assert len(lines) == 200
    for line in lines:  # 每行都是完整合法 JSON（未被撕开）
        json.loads(line)


def test_writer_default_param_used_in_both_modes():
    """default=str 在 send 与 send_sync 两条路径都生效。"""
    out = io.StringIO()
    w = StdoutWriter(stream=out, default=str)
    w.start()
    w.send({"obj": object()})
    assert w.flush(timeout=2.0)
    w.send_sync({"obj2": {1}})
    lines = out.getvalue().splitlines()
    v1 = json.loads(lines[0])
    assert v1["obj"].startswith("<object object at")
    v2 = json.loads(lines[1])
    assert "1" in v2["obj2"]


def test_writer_pipe_dead_drops_queued_events():
    """写线程写失败一次后置 pipe_dead，后续队列事件被丢弃且不再抛错。"""

    class _FailStream:
        def __init__(self):
            self.calls = 0

        def write(self, s):
            self.calls += 1
            if self.calls > 1:  # 第一条成功，之后全失败
                raise OSError("broken pipe")
            return len(s)

        def flush(self):
            if self.calls > 1:
                raise OSError("broken pipe")

    stream = _FailStream()
    w = StdoutWriter(stream=stream)
    w.start()
    w.send({"type": "first"})  # 成功
    w.send({"type": "boom"})   # 失败 → pipe_dead
    w.send({"type": "dropped"})
    assert w.flush(timeout=2.0)
    assert w.pipe_dead
    # pipe_dead 后再 send 不炸（丢弃在写线程里发生）
    w.send({"type": "also-dropped"})
    assert w.flush(timeout=2.0)


# ============================================
# read_stdin_lines
# ============================================
def test_read_stdin_lines_decodes_into_queue():
    q: "queue.Queue" = queue.Queue()
    stdin = io.StringIO('{"id": "r1", "op": "chat"}\n\nbad json\n{"id": "r2", "op": "health"}\n')
    t = read_stdin_lines(q, stream=stdin, name="test-stdin")
    t.join(timeout=2.0)
    assert not t.is_alive()
    # 有效行解码进队列；无效行（空行/坏 JSON）丢弃
    got = [q.get_nowait() for _ in range(2)]
    assert got == [
        {"id": "r1", "op": "chat"},
        {"id": "r2", "op": "health"},
    ]
    # EOF 后放 None 终止信号
    assert q.get_nowait() is None


def test_read_stdin_lines_empty_stream_signals_none():
    q: "queue.Queue" = queue.Queue()
    t = read_stdin_lines(q, stream=io.StringIO(""), name="test-stdin")
    t.join(timeout=2.0)
    assert q.get_nowait() is None


def test_read_stdin_lines_returns_daemon_thread():
    q: "queue.Queue" = queue.Queue()
    t = read_stdin_lines(q, stream=io.StringIO(""), name="test-stdin")
    assert t.daemon is True
    t.join(timeout=2.0)
