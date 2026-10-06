"""
分层日志配置测试。

验证 setup_logging 的分层行为：
- 控制台 WARNING / info.log INFO+（仅 hermes.*）/ error.log ERROR+（全部）
- HermesFilter 过滤第三方 INFO
- 按天滚动 handler 配置正确
- 用 tmp_path 隔离，不污染真实 logs/
"""
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pytest

from src.logging_config import setup_logging, HermesFilter


@pytest.fixture
def teardown_root_handlers():
    """每个测试后清理 root logger 的 handler，避免互相污染。"""
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def _read_log(path: Path) -> str:
    """读取日志文件内容（文件不存在返回空串）。"""
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def test_setup_creates_three_handlers(tmp_path, teardown_root_handlers):
    """setup_logging 后 root 有 3 个 handler（console/info/error）。"""
    setup_logging(debug=False, log_dir=str(tmp_path), log_to_file=True)
    root = logging.getLogger()
    assert len(root.handlers) == 3
    # 分类确认
    file_handlers = [h for h in root.handlers if isinstance(h, TimedRotatingFileHandler)]
    stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)
                       and not isinstance(h, TimedRotatingFileHandler)]
    assert len(file_handlers) == 2  # info + error
    assert len(stream_handlers) == 1  # console


def test_info_handler_level_and_filter(tmp_path, teardown_root_handlers):
    """info handler level=INFO 且有 HermesFilter。"""
    setup_logging(debug=False, log_dir=str(tmp_path), log_to_file=True)
    root = logging.getLogger()
    info_handlers = [h for h in root.handlers
                     if isinstance(h, TimedRotatingFileHandler) and h.level == logging.INFO]
    assert len(info_handlers) == 1
    # 应该有 HermesFilter
    has_hermes_filter = any(isinstance(f, HermesFilter) for f in info_handlers[0].filters)
    assert has_hermes_filter


def test_error_handler_level_no_filter(tmp_path, teardown_root_handlers):
    """error handler level=ERROR 且无 HermesFilter（收全部 logger 错误）。"""
    setup_logging(debug=False, log_dir=str(tmp_path), log_to_file=True)
    root = logging.getLogger()
    error_handlers = [h for h in root.handlers
                      if isinstance(h, TimedRotatingFileHandler) and h.level == logging.ERROR]
    assert len(error_handlers) == 1
    # 不应有 HermesFilter（要收第三方 ERROR）
    has_hermes_filter = any(isinstance(f, HermesFilter) for f in error_handlers[0].filters)
    assert not has_hermes_filter


def test_info_log_goes_to_info_file_only(tmp_path, teardown_root_handlers):
    """hermes.* 的 INFO 只进 info.log，不进 error.log。"""
    setup_logging(debug=False, log_dir=str(tmp_path), log_to_file=True)
    logger = logging.getLogger("hermes.test_module")
    logger.info("测试信息日志")

    # flush 所有 handler
    for h in logging.getLogger().handlers:
        h.flush()

    info_content = _read_log(tmp_path / "info.log")
    error_content = _read_log(tmp_path / "error.log")
    assert "测试信息日志" in info_content
    assert "测试信息日志" not in error_content


def test_error_log_goes_to_both_files(tmp_path, teardown_root_handlers):
    """hermes.* 的 ERROR 同时进 info.log 和 error.log。"""
    setup_logging(debug=False, log_dir=str(tmp_path), log_to_file=True)
    logger = logging.getLogger("hermes.test_module")
    logger.error("测试错误日志")

    for h in logging.getLogger().handlers:
        h.flush()

    info_content = _read_log(tmp_path / "info.log")
    error_content = _read_log(tmp_path / "error.log")
    assert "测试错误日志" in info_content  # INFO+ 包含 ERROR
    assert "测试错误日志" in error_content


def test_third_party_info_filtered_from_info_file(tmp_path, teardown_root_handlers):
    """第三方 logger 的 INFO 不进 info.log（HermesFilter 生效）。"""
    setup_logging(debug=False, log_dir=str(tmp_path), log_to_file=True)
    # 用一个非 hermes 前缀的 logger（模拟第三方）
    third_party = logging.getLogger("some_third_party_lib")
    # 注意：noisy_logger 列表会把 httpx 等压到 CRITICAL，这里用一个不在列表里的名字
    third_party.info("第三方信息日志")

    for h in logging.getLogger().handlers:
        h.flush()

    info_content = _read_log(tmp_path / "info.log")
    assert "第三方信息日志" not in info_content, "第三方 INFO 不应进 info.log"


def test_third_party_error_goes_to_error_file(tmp_path, teardown_root_handlers):
    """第三方 logger 的 ERROR 进 error.log（无 filter）。"""
    setup_logging(debug=False, log_dir=str(tmp_path), log_to_file=True)
    third_party = logging.getLogger("some_third_party_lib")
    third_party.error("第三方致命错误")

    for h in logging.getLogger().handlers:
        h.flush()

    error_content = _read_log(tmp_path / "error.log")
    assert "第三方致命错误" in error_content


def test_log_to_file_false_skips_file_handlers(tmp_path, teardown_root_handlers):
    """log_to_file=False 只配控制台，不写文件。"""
    setup_logging(debug=False, log_dir=str(tmp_path), log_to_file=False)
    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, TimedRotatingFileHandler)]
    stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)
                       and not isinstance(h, TimedRotatingFileHandler)]
    assert len(file_handlers) == 0
    assert len(stream_handlers) == 1


def test_console_critical_level_in_production(tmp_path, teardown_root_handlers):
    """生产模式（debug=False）控制台 level=CRITICAL。

    2026-09-05 行为变更：WARNING/ERROR（含 exc_info 堆栈）只进 logs/，
    终端不再滚英文堆栈——LLM 不可达时 openai 全家桶堆栈刷屏等同"程序
    崩了"的观感（CLI 用户视角实测）。错误详情靠 error.log + 界面提示
    "详情见 logs/error.log"。
    """
    setup_logging(debug=False, log_dir=str(tmp_path), log_to_file=True)
    root = logging.getLogger()
    console = next(h for h in root.handlers
                   if isinstance(h, logging.StreamHandler)
                   and not isinstance(h, TimedRotatingFileHandler))
    assert console.level == logging.CRITICAL


def test_console_debug_level_in_debug_mode(tmp_path, teardown_root_handlers):
    """--debug 模式控制台 level=DEBUG。"""
    setup_logging(debug=True, log_dir=str(tmp_path), log_to_file=True)
    root = logging.getLogger()
    console = next(h for h in root.handlers
                   if isinstance(h, logging.StreamHandler)
                   and not isinstance(h, TimedRotatingFileHandler))
    assert console.level == logging.DEBUG


def test_hermes_filter_directly():
    """HermesFilter 单元测试：hermes.* 放行，其他拒绝。"""
    f = HermesFilter()
    # 构造假 LogRecord
    hermes_record = logging.LogRecord(
        name="hermes.memory", level=logging.INFO, pathname="", lineno=0,
        msg="test", args=(), exc_info=None,
    )
    third_party_record = logging.LogRecord(
        name="httpx", level=logging.INFO, pathname="", lineno=0,
        msg="test", args=(), exc_info=None,
    )
    assert f.filter(hermes_record) is True
    assert f.filter(third_party_record) is False
