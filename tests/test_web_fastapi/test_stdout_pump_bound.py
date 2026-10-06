"""P2-6：_StdoutPump 队列有界（满则丢最旧 + 计数 + 绝不阻塞）单测。

契约（web_fastapi/worker_manager._StdoutPump）：
- 队列有界（默认 STDOUT_QUEUE_MAXSIZE=2000 行），满时挤掉**最旧行**腾位并
  累计计数，泵线程绝不阻塞（worker 日志型输出不允许拖死读取）；
  P2-26：丢弃策略由「丢新行」演进为「丢最旧」（对齐 chat_bus.put_drop_oldest
  语义）——满时继续丢新行会把当前请求的 result/done 终态帧丢掉，SSE 假死；
- 每累计丢满 STDOUT_DROP_WARN_EVERY 条打一条 warning 汇总（不逐行刷日志）；
- EOF 哨兵必达：队列满时挤掉最旧一行腾位——EOF 丢失会把「worker 已退出」
  误报成响应超时；
- 正常路径（行序、EOF、空流超时 None）与旧无界实现一致。
"""
import logging
import queue as _q
import time

from web_fastapi.worker_manager import (
    _EOF,
    STDOUT_DROP_WARN_EVERY,
    STDOUT_QUEUE_MAXSIZE,
    _StdoutPump,
)


class FakeStream:
    """可编程 stdout 替身：write_line 灌行，close() 触发 EOF（readline 返回 ""）。"""

    def __init__(self):
        self._lines: _q.Queue = _q.Queue()

    def write_line(self, line: str) -> None:
        self._lines.put(line)

    def close(self) -> None:
        self._lines.put(None)

    def readline(self) -> str:
        line = self._lines.get()
        return "" if line is None else line


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_queue_is_bounded_by_default():
    """默认构造即有界：maxsize=STDOUT_QUEUE_MAXSIZE（2000 行）。"""
    pump = _StdoutPump(FakeStream())
    assert pump._q.maxsize == STDOUT_QUEUE_MAXSIZE == 2000


def test_full_queue_drops_oldest_lines_without_blocking():
    """P2-26 改写（原断言「满时丢新行、保留最旧行」）：灌 50 行进 maxsize=10
    的队列：泵线程必须立刻消费完流（不阻塞在 put），丢弃最旧 40 行、保留
    最新 10 行（line41..line50）且行序不变——终态帧/新行比旧行更有价值。"""
    stream = FakeStream()
    pump = _StdoutPump(stream, maxsize=10)
    for i in range(1, 51):
        stream.write_line(f"line{i}\n")

    # dropped==40 只可能由泵线程逐行消费流产生——计数到位本身就是"未阻塞"的证明
    assert _wait_until(lambda: pump.dropped == 40), f"dropped={pump.dropped}"
    assert pump._q.qsize() == 10
    # 保留的是最新行（丢最旧 → 对齐 chat_bus.put_drop_oldest 语义）
    for i in range(41, 51):
        assert pump.get(1.0) == f"line{i}\n"
    assert pump.get(0.1) is None  # line1..line40 已被挤掉，队列里不再有正文


def test_eof_survives_full_queue():
    """队列满后再关闭流：挤掉最旧一行腾位，EOF 必达（绝不误报成超时 None）。"""
    stream = FakeStream()
    pump = _StdoutPump(stream, maxsize=5)
    for i in range(1, 21):
        stream.write_line(f"line{i}\n")
    assert _wait_until(lambda: pump.dropped == 15), f"dropped={pump.dropped}"

    stream.close()
    # 消费端先不读：等泵线程自己完成"挤掉最旧一行腾位 → EOF 入队"
    # （dropped 15→16 即腾位完成的确定性信号），再开始收流——无竞态。
    assert _wait_until(lambda: pump.dropped == 16), f"dropped={pump.dropped}"
    drained = []
    while True:
        item = pump.get(2.0)
        if item is _EOF:
            break
        assert item is not None  # EOF 必达：不能以"超时 None"收尾
        drained.append(item)
    # P2-26 丢最旧语义：挤满后队列里是最新 5 行 line16..line20，line16 为
    # EOF 腾位被挤掉（计入丢弃）——实际可读的是 line17..line20
    assert drained == [f"line{i}\n" for i in range(17, 21)]
    assert pump.dropped == 16  # 15 行挤掉 + line16 为 EOF 腾位


def test_normal_path_order_eof_and_timeout_unchanged():
    """未满时不丢行：行序、EOF 哨兵、空流超时 None 与旧无界实现一致。"""
    stream = FakeStream()
    pump = _StdoutPump(stream, maxsize=100)
    assert pump.get(0.2) is None  # 空流超时 → None（不是 EOF 哨兵）
    for text in ("a\n", "b\n", "c\n"):
        stream.write_line(text)
    assert pump.get(2.0) == "a\n"
    assert pump.get(2.0) == "b\n"
    assert pump.get(2.0) == "c\n"
    stream.close()
    assert pump.get(2.0) is _EOF


def test_drop_warning_summarizes_every_n(caplog):
    """每丢满 STDOUT_DROP_WARN_EVERY 条打一条 warning 汇总（含累计丢弃数）。"""
    stream = FakeStream()
    pump = _StdoutPump(stream, maxsize=10)
    with caplog.at_level(logging.WARNING, logger="hermes.web.worker_manager"):
        # 队列占 10 行 + 丢 1030 行 → 丢第 500/1000 条时各一条 warning，恰好两条
        total = 10 + STDOUT_DROP_WARN_EVERY * 2 + 30
        for i in range(1, total + 1):
            stream.write_line(f"line{i}\n")
        assert _wait_until(lambda: pump.dropped == total - 10)
        # 等泵线程把 warning 写完（warning 与计数同点触发，此刻必然已落）
        time.sleep(0.2)
        texts = [r.getMessage() for r in caplog.records
                 if r.levelno == logging.WARNING and "丢弃" in r.getMessage()]
        assert len(texts) == 2, texts
        # P2-26：文案同步改为「丢弃最旧 N 行」
        assert any("已累计丢弃最旧 500 行输出" in t for t in texts)
        assert any("已累计丢弃最旧 1000 行输出" in t for t in texts)
        assert all("maxsize=10" in t for t in texts)
