"""
============================================
Hermes 项目主入口
============================================
启动 Hermes CLI 交互界面。

使用方式：
    python main.py
"""

import os
import sys

# 清除无效的 SSL_CERT_FILE，必须在所有 import 之前
# ollama 库在模块级创建 httpx.Client，SSL_CERT_FILE 无效会导致导入失败
if os.environ.get("SSL_CERT_FILE") and not os.path.exists(os.environ["SSL_CERT_FILE"]):
    del os.environ["SSL_CERT_FILE"]

import config  # noqa: F401

import logging
import warnings
# 抑制第三方 deprecation warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*LangChainPendingDeprecationWarning.*")

# 统一日志配置（必须在导入 src.* 之前）
# 2026-06-23: 抽取到 src/logging_config.py，main.py 与 web.py 共用，消除重复。
# 分层：控制台 WARNING + logs/info.log（INFO+，仅 hermes.*）+ logs/error.log（ERROR+）
from src.logging_config import setup_logging

_settings = config.get_settings()
setup_logging(
    debug="--debug" in sys.argv,
    log_dir=_settings.get("log_dir", "logs"),
    log_to_file=_settings.get("log_to_file", True),
)


if __name__ == "__main__":
    from src.cli import main

    main()