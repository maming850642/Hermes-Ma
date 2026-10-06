"""
LLM 流式超时保护回归测试。

背景（2026-06-23）：自托管 Qwen 端点偶发"流式半开连接"——开启 SSE 后偶尔
发心跳字节，让 httpx 的 read-timeout（按字节间隙计）永不触发，主线程卡在
native socket read，连 Ctrl+C 都无法终止。

根因复盘（踩过的坑）：
  - 初版 watchdog 用"滑动窗口"（每个 chunk reset 计时器）→ trickle 心跳让它
    永不触发，且即使触发只设 flag，主线程卡在 native read 永远检查不到 flag。
  - 最终方案：线程隔离 + queue 硬超时。把阻塞的 stream() 放进 daemon 工作线程，
    主线程 queue.get(timeout=) 消费，超时则放弃 worker（OS 清理 socket），
    主线程永远不进入不可中断的阻塞。

本测试覆盖 llm_stream.stream_with_hard_timeout 的核心契约：
  - 半开挂死（发几 chunk 后永停）→ 必在 timeout 内抛 TimeoutError
  - 完全无响应 → 必在 timeout 内抛 TimeoutError
  - 正常快速流 → 不超时，正常产出所有 chunk
  - worker 线程经 copy_context 传播 contextvars（历史卡死根因 B 的回归保护）
  - worker 内部异常 → re-raise 给消费方
"""
import contextvars
import threading
import time

import pytest

from src.agent.llm_stream import WORKER_THREAD_NAME, stream_with_hard_timeout
from src.llm.messages import Chunk


# ============================================
# contextvar 回归测试专用
# ============================================

# 模拟 openai SDK 内部的 contextvar：起裸线程时 CPython 不传播 contextvar，
# 必须用 contextvars.copy_context() + ctx.run 才能带过去。
_test_ctxvar: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_test_ctxvar", default=None
)


class _FakeLLM:
    """模拟 LLMClient.stream_chat，可控制流行为。"""

    def __init__(self, mode, sink=None):
        self.mode = mode  # "hang_after_2" / "silent" / "fast" / "capture_ctxvar"
        self.sink = sink  # 模式 capture_ctxvar 时存放 worker 读到的 contextvar 值

    def stream_chat(self, msgs, tools=None, temperature=None, max_tokens=None):
        if self.mode == "hang_after_2":
            # 半开连接：发 2 个 chunk 后永久挂起
            yield Chunk(content_delta="hello")
            yield Chunk(content_delta=" world")
            time.sleep(999)
        elif self.mode == "silent":
            # 完全无响应：一个 chunk 都不发，直接挂起
            time.sleep(999)
        elif self.mode == "fast":
            # 正常快速：发 3 个 chunk 立即结束
            yield Chunk(content_delta="a")
            yield Chunk(content_delta="b")
            yield Chunk(content_delta="c")
        elif self.mode == "capture_ctxvar":
            # 在 worker 线程内读取 contextvar，回放给主线程。
            # 若 stream_with_hard_timeout 起的是裸线程（无 ctx.run），
            # 这里读到的会是 default=None，sink 记下 None。
            val = _test_ctxvar.get()
            if self.sink is not None:
                self.sink.append(val)
            yield Chunk(content_delta="done")


def test_half_open_stream_times_out():
    """半开连接（2 chunk 后挂起）必须在 timeout 内抛 TimeoutError。"""
    streamer = stream_with_hard_timeout(_FakeLLM("hang_after_2"), [], timeout_s=1.5)
    start = time.time()
    chunks = []
    with pytest.raises(TimeoutError):
        for chunk in streamer:
            chunks.append(chunk)
    elapsed = time.time() - start
    assert elapsed < 3.0, f"超时应≈1.5s，实际 {elapsed:.1f}s"
    assert len(chunks) == 2, f"超时前应收到 2 个 chunk，实际 {len(chunks)}"


def test_silent_stream_times_out():
    """完全无响应（0 chunk）必须在 timeout 内抛 TimeoutError。"""
    streamer = stream_with_hard_timeout(_FakeLLM("silent"), [], timeout_s=1.0)
    start = time.time()
    chunks = []
    with pytest.raises(TimeoutError):
        for chunk in streamer:
            chunks.append(chunk)
    elapsed = time.time() - start
    assert elapsed < 2.5
    assert len(chunks) == 0


def test_fast_stream_completes_without_timeout():
    """正常快速流不应超时，应产出所有 chunk。"""
    streamer = stream_with_hard_timeout(_FakeLLM("fast"), [], timeout_s=10.0)
    chunks = []
    for chunk in streamer:
        chunks.append(chunk)
    assert len(chunks) == 3, f"正常流应产出 3 chunk，实际 {len(chunks)}"


def test_main_thread_stays_alive_after_timeout():
    """超时后主线程必须存活可返回（这是 Ctrl+C 重新可用的前提）。"""
    streamer = stream_with_hard_timeout(_FakeLLM("hang_after_2"), [], timeout_s=0.8)
    try:
        for _ in streamer:
            pass
    except TimeoutError:
        pass
    # 到这里说明主线程没被 native 阻塞，正常返回
    assert True


def test_worker_exception_propagates():
    """worker 内部异常 → re-raise 给消费方。"""
    def error_stream(msgs, **kw):
        yield Chunk(content_delta="partial")
        raise RuntimeError("LLM error")

    class _ErrLLM:
        stream_chat = staticmethod(error_stream)

    with pytest.raises(RuntimeError, match="LLM error"):
        list(stream_with_hard_timeout(_ErrLLM(), [], timeout_s=10.0))


# ============================================
# contextvar 回归测试（根因 B：裸线程丢失 contextvars）
# ============================================

def test_worker_receives_caller_contextvar():
    """
    worker 线程必须读到主线程设置的 contextvar。

    这是历史卡死根因 B 的回归保护：stream_with_hard_timeout 起的 worker
    线程若不用 ctx.run 包裹，CPython 不传播 contextvar，worker 拿不到
    调用线程的 ambient contextvar（user_id / vfs / depth 等），后续工具
    执行状态污染。
    """
    sink: list = []
    llm = _FakeLLM("capture_ctxvar")
    llm.sink = sink  # 把可变 list 注入，让 worker 回写

    # 主线程设置 contextvar，随后消费（其内部 copy_context 会捕获此刻的值）
    token = _test_ctxvar.set("CALLER_VALUE")
    try:
        chunks = []
        for chunk in stream_with_hard_timeout(llm, [], timeout_s=5.0):
            chunks.append(chunk)
    finally:
        _test_ctxvar.reset(token)

    assert len(chunks) == 1, f"应产出 1 chunk，实际 {len(chunks)}"
    assert sink, "worker 未写入 sink，说明 stream 没真正执行"
    assert sink[0] == "CALLER_VALUE", (
        f"worker 没读到调用线程的 contextvar（拿到 {sink[0]!r}），"
        f"说明 contextvars 未传播 —— 检查 stream_with_hard_timeout 是否用了 ctx.run"
    )


def test_llm_timeout_config_exists():
    """config 必须提供 llm_timeout 键。"""
    from config import get_settings
    s = get_settings()
    assert hasattr(s, "llm_timeout")
    assert isinstance(s.llm_timeout, int)
    assert s.llm_timeout > 0


# ============================================
# 防线 C（P2-8）：cancel_event 断流
# ============================================

class _EndlessLLM:
    """无限产 chunk 的假 LLM（模拟长输出），底层流被 close 时记录。"""

    def __init__(self, sink):
        self.closed_sink = sink  # list：close() 时追加

    def stream_chat(self, msgs, tools=None, temperature=None, max_tokens=None):
        try:
            i = 0
            while True:
                yield Chunk(content_delta=f"c{i}")
                i += 1
        finally:
            self.closed_sink.append(True)


def _snapshot_worker_idents():
    """快照既有 llm-stream-worker 线程 ident（此前超时测试会遗留
    sleep(999) 的同名 daemon 线程，等待时必须排除）。"""
    return {t.ident for t in threading.enumerate() if t.name == WORKER_THREAD_NAME}


def _wait_worker_exit(preexisting, timeout=2.0):
    """轮询等待"本测试新起"的 worker 线程退出。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = [
            t for t in threading.enumerate()
            if t.name == WORKER_THREAD_NAME and t.ident not in preexisting
        ]
        if not any(t.is_alive() for t in current):
            return True
        time.sleep(0.02)
    return False


def test_cancel_event_closes_stream_and_worker_exits():
    """防线 C：消费方置位 cancel_event 后，worker 必须在 chunk 边界退出
    并 close() 底层流——不再把流读到生成完（vLLM 槽位白占、token 白烧）。"""
    preexisting = _snapshot_worker_idents()
    closed = []
    llm = _EndlessLLM(closed)
    cancel_event = threading.Event()

    streamer = stream_with_hard_timeout(llm, [], timeout_s=30, cancel_event=cancel_event)
    got = []
    for chunk in streamer:
        got.append(chunk)
        if len(got) >= 3:
            cancel_event.set()  # 模拟消费方中途停止

    # 积压 chunk 被丢弃：只拿到置位前消费的 3 个
    assert len(got) == 3
    # worker 线程确实退出（退出前 finally 必已 close 底层流）
    assert _wait_worker_exit(preexisting), "worker 线程未在 cancel 后退出"
    assert closed, "底层流未被 close()——防线 C 未生效"


def test_cancel_event_default_internal_and_close_propagates():
    """不传 cancel_event：内部自建；消费方 close() 生成器 → finally 置位
    → worker 在边界退出并 close 底层流（worker 侧 stream.close() 传导链）。"""
    preexisting = _snapshot_worker_idents()
    closed = []
    llm = _EndlessLLM(closed)

    streamer = stream_with_hard_timeout(llm, [], timeout_s=30)
    got = []
    for chunk in streamer:
        got.append(chunk)
        if len(got) >= 2:
            break
    streamer.close()  # 消费方（如 worker stop 路径）显式关闭

    assert len(got) == 2
    assert _wait_worker_exit(preexisting), "worker 线程未在 close 后退出"
    assert closed, "close() 后底层流未被关闭——防线 C 未生效"
