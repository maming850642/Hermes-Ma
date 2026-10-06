"""P2-6：_StdoutPump 队列有界（满则丢新行 + 计数 + 绝不阻塞）单测。

契约（web_fastapi/worker_manager._StdoutPump）：
- 队列有界（默认 STDOUT_QUEUE_MAXSIZE=2000 行），满时丢弃**新行**并累计
  计数，泵线程绝不阻塞（worker 日志型输出不允许拖死读取）；
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


def test_full_queue_drops_new_lines_without_blocking():
    """灌 50 行进 maxsize=10 的队列：泵线程必须立刻消费完流（不阻塞在 put），
    丢弃 40 行新行、保留最旧 10 行且行序不变。"""
    stream = FakeStream()
    pump = _StdoutPump(stream, maxsize=10)
    for i in range(1, 51):
        stream.write_line(f"line{i}\n")

    # dropped==40 只可能由泵线程逐行消费流产生——计数到位本身就是"未阻塞"的证明
    assert _wait_until(lambda: pump.dropped == 40), f"dropped={pump.dropped}"
    assert pump._q.qsize() == 10
    # 保留的是最旧行（丢弃新行 → 保序语义"最旧优先"）
    for i in range(1, 11):
        assert pump.get(1.0) == f"line{i}\n"
    assert pump.get(0.1) is None  # 第 11..50 行已被丢弃，队列里不再有正文


def test_eof_survives_full_queue():
    """队列满后再关闭流：挤掉最旧一行腾位，EOF 必达（绝不误报成超时 None）。"""
    stream = FakeStream()
    pump = _StdoutPump(stream, maxsize=5)
    for i in range(1, 21):
        stream.write_line(f"line{i}\n")
    assert _wait_until(lambda: pump.dropped == 15), f"dropped={pump.dropped}"

    stream.close()
    # 消费端先不读：等泵线程自己完成"挤掉 line1 腾位 → EOF 入队"
    # （dropped 15→16 即腾位完成的确定性信号），再开始收流——无竞态。
    assert _wait_until(lambda: pump.dropped == 16), f"dropped={pump.dropped}"
    drained = []
    while True:
        item = pump.get(2.0)
        if item is _EOF:
            break
        assert item is not None  # EOF 必达：不能以"超时 None"收尾
        drained.append(item)
    # line1 为 EOF 腾位被挤掉（计入丢弃）：实际可读的是 line2..line5
    assert drained == [f"line{i}\n" for i in range(2, 6)]
    assert pump.dropped == 16  # 15 行刷掉 + line1 为 EOF 腾位


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
        assert any("已累计丢弃 500 行输出" in t for t in texts)
        assert any("已累计丢弃 1000 行输出" in t for t in texts)
        assert all("maxsize=10" in t for t in texts)
