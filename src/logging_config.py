"""
============================================
统一日志配置（分层 + 文件留存）
============================================
一处配置，main.py / web.py 两个入口都调用，消除重复 basicConfig。

分层设计：
  - 控制台（stderr）：WARNING（--debug 时 DEBUG），保持终端安静
  - logs/info.log：INFO+，按天滚动保留 7 天，只收 hermes.* 自家日志
  - logs/error.log：ERROR+，按天滚动保留 7 天，收全部 logger 的错误

关键设计：
  - root 设 DEBUG，让各 handler 各自按 level 过滤（而非全局压制）
  - info.log 加 HermesFilter，挡住第三方 INFO（双保险，noisy_logger 已压住大部分）
  - error.log 不加 filter，所有 logger 的 ERROR 都进（含第三方致命错误）
  - TimedRotatingFileHandler when="midnight" 每天 0 点滚动，backupCount=7 自动清理
"""
import logging
import sys
import threading
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


class SafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """多进程安全的按天轮转（F1 修复）。

    Windows 下多个 hermes 进程（web 主进程/worker/waker 子进程）同时持有
    同一日志文件，午夜轮转 rename 必然被占用（WinError 32）。标准库
    TimedRotatingFileHandler 此时抛 PermissionError → emit() 的 handleError
    **丢弃该条记录**，且 rolloverAt 不推进 → 此后每条记录都重复失败被丢，
    文件日志整体静默死亡（2026-08-17 实测发生过）。

    本类改三点：
      1. 轮转失败不抛——记录照常写入当前（未轮转的）文件，绝不丢
      2. 失败后仍推进 rolloverAt 到下一周期（下个午夜再试），不再每条重试
      3. 失败告警节流（每个进程每次轮转点只 stderr 提示一次）
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rotate_warned = False
        self._warn_lock = threading.Lock()

    def rolloverAt_next(self) -> float:  # pragma: no cover - 薄封装
        """下一轮转点（按当前实现推算一个周期）。"""
        return time.time() + max(self.interval, 1)

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        try:
            super().doRollover()
            self._rotate_warned = False
        except PermissionError:
            # 其他进程占着旧文件（或新文件被占用）——跳过本次轮转：
            # 推进轮转点到下一周期，让 emit 重新打开当前文件继续追加
            self._set_next_rollover()
            if not self._rotate_warned:
                with self._warn_lock:
                    if not self._rotate_warned:
                        self._rotate_warned = True
                        print(
                            f"[logging] 日志轮转被其他进程占用（{self.baseFilename}），"
                            f"本次跳过、记录继续写入当前文件",
                            file=sys.stderr,
                        )
        except OSError:
            # 同类占位问题（网络盘/权限等）按同一策略处理
            self._set_next_rollover()

    def _set_next_rollover(self) -> None:
        try:
            self.rolloverAt = self.rolloverAt_next()
        except Exception:
            self.rolloverAt = time.time() + 3600


class HermesFilter(logging.Filter):
    """只放行 hermes.* 自家 logger 的日志，挡住第三方 INFO。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith("hermes")


# 第三方噪音库：强制 CRITICAL，避免 info 文件被淹没（双保险）
_NOISY_LOGGERS = [
    "httpx", "httpcore", "urllib3",
    "markdown_it",
]


def setup_logging(debug: bool = False, log_dir: str = "logs", log_to_file: bool = True) -> None:
    """
    统一日志配置。必须在 `from src.* import ...` 之前调用（各模块 import 时就 getLogger）。

    Args:
        debug: True 则控制台显示 DEBUG（--debug 模式）
        log_dir: 日志目录（相对项目根或绝对路径），默认 "logs"
        log_to_file: False 则只配控制台，不写文件（默认 True）
    """
    root = logging.getLogger()
    # root 设最低，让各 handler 各自按 level 过滤
    root.setLevel(logging.DEBUG)

    # 幂等：清掉 basicConfig 可能挂的默认 handler
    for h in list(root.handlers):
        root.removeHandler(h)

    # 文件日志带完整日期（跨天不混乱），控制台沿用带日期格式保持一致
    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台：DEBUG（--debug 时）。非 debug 只出 CRITICAL——WARNING/ERROR
    # （含 exc_info 堆栈）全进 logs/error.log：终端里滚英文堆栈对单机用户
    # 等同"程序崩了"观感（实测 LLM 不可达时 openai 全家桶堆栈刷屏）。
    # 文件 handler 不变：INFO+（hermes.*）与 ERROR+ 照常落盘。
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if debug else logging.CRITICAL)
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_to_file:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)

        # info.log：INFO+，只收 hermes.* 日志（Safe 版：多进程轮转不丢记录）
        info_handler = SafeTimedRotatingFileHandler(
            log_path / "info.log",
            when="midnight",
            backupCount=7,
            encoding="utf-8",
        )
        info_handler.setLevel(logging.INFO)
        info_handler.setFormatter(fmt)
        info_handler.addFilter(HermesFilter())
        root.addHandler(info_handler)

        # error.log：ERROR+，收全部 logger 的错误（含第三方致命错误）
        error_handler = SafeTimedRotatingFileHandler(
            log_path / "error.log",
            when="midnight",
            backupCount=7,
            encoding="utf-8",
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(fmt)
        root.addHandler(error_handler)

    # 第三方噪音库静默
    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.CRITICAL)
