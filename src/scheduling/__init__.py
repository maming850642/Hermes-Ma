"""
统一调度层（P3-3）：SchedulerService 的唯一定义处。

原先定义在 src/plugins/scheduler_plugin.py，storage / memory / waker /
wakerflow 等下层模块为自建兜底不得不上探 plugins 层惰性 import（依赖
倒置）。P3-3 把服务本体下沉到本层（仅依赖 cordis），下层模块顶层正常
import 即可；plugins 侧只保留插件装配（scheduler_plugin.py 经
cordis.yaml 的 "schedule" 键注册）。
"""

from __future__ import annotations

from src.scheduling.service import SchedulerService

__all__ = ["SchedulerService"]
