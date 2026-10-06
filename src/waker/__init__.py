"""
============================================
waker —— 数字员工核心子系统
============================================
用 waker（数字员工）模式替换原"总参"模式的改造，本包是第一步：纯后端
配置/存储/调度计算，无 web 代码。后续会有：
- FastAPI 主进程调度线程：遍历 iter_all_wakers + is_due + compute_next_run
- per-user worker 进程的 runner：用 load_persona_prompt + WakerStore 跑任务

导出（包门面）：
- WakerConfig    配置数据模型
- WakerStore     per-user 文件存储
- load_persona_prompt  组装人格 system prompt 段
- compute_next_run / is_due  调度计算
"""
from src.waker.models import WakerConfig, WakerConfigError, validate_name
from src.waker.store import WakerStore, iter_all_wakers
from src.waker.persona import load_persona_prompt
from src.waker.schedule_parse import compute_next_run, is_due, validate_schedule

__all__ = [
    "WakerConfig",
    "WakerConfigError",
    "validate_name",
    "WakerStore",
    "iter_all_wakers",
    "load_persona_prompt",
    "compute_next_run",
    "is_due",
    "validate_schedule",
]

# Scheduler / runner 按需 import（避免无 LLM 的纯单测也触发 worker_manager 等重依赖）
def __getattr__(name):
    if name == "WakerScheduler":
        from src.waker.scheduler import WakerScheduler
        return WakerScheduler
    if name == "run_waker":
        from src.waker.runner import run_waker
        return run_waker
    raise AttributeError(f"module 'src.waker' has no attribute {name!r}")
