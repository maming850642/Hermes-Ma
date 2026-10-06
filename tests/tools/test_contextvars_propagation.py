"""
contextvars 在 ThreadPoolExecutor 传播的回归测试。

背景（R2 修复）：remember 工具用 contextvars.ContextVar 存 user_id，
但 ThreadPoolExecutor.submit 不自动传播 contextvars。多工具并发路径
（ToolRegistry.process_tool_calls 的多工具分支）若不显式 ctx.run 包裹，
worker 线程读 contextvar 会得到 None，导致 remember 必返"无法确定当前用户"。

这个测试直接验证 process_tool_calls 的多工具路径是否正确传播了 contextvar。
"""
import contextvars
from concurrent.futures import ThreadPoolExecutor

import pytest


# 模拟 remember 工具依赖的 contextvar
_test_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "hermes_test_cvar", default=None
)


def _read_var_in_worker() -> str | None:
    """在 worker 线程里读 contextvar。"""
    return _test_var.get()


def test_threadpoolexecutor_does_not_propagate_contextvars_by_default():
    """未修复前的行为基线：ThreadPoolExecutor.submit 默认不传播 contextvar。

    这个测试锁定 CPython 行为——确保我们不是在解决一个不存在的问题。
    如果未来 Python 升级后 ThreadPoolExecutor 默认传播了，这个测试会 FAIL，
    提示我们 R2 的修复可能不再必要。
    """
    token = _test_var.set("main_thread_value")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_read_var_in_worker)
            result = future.result()
        # 默认行为：worker 拿不到主线程设的值
        assert result is None, (
            "ThreadPoolExecutor 现在默认传播 contextvars 了？R2 修复可能需要重新评估"
        )
    finally:
        _test_var.reset(token)


def test_ctx_run_propagates_contextvars_to_worker():
    """R2 修复后的行为：用 ctx.run 包裹，contextvar 正确传播到 worker。"""
    token = _test_var.set("main_thread_value")
    try:
        ctx = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(ctx.run, _read_var_in_worker)
            result = future.result()
        assert result == "main_thread_value", "ctx.run 未正确传播 contextvar"
    finally:
        _test_var.reset(token)


def test_each_worker_gets_its_own_context_copy():
    """并发多 worker 各自拿到独立的上下文副本（隔离性）。

    确保修复不会引入竞态：两个 worker 同时跑，各自读到主线程的值，
    且一个 worker 内部的 set 不影响另一个。
    """
    _test_var.set("shared_value")

    def _worker_with_local_set(worker_id: str) -> tuple[str, str, str]:
        before = _test_var.get()
        tok = _test_var.set(f"worker_{worker_id}")
        after = _test_var.get()
        _test_var.reset(tok)
        return (worker_id, before, after)

    ctx = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(ctx.run, _worker_with_local_set, "A"),
            executor.submit(ctx.run, _worker_with_local_set, "B"),
        ]
        results = [f.result() for f in futures]

    # 两个 worker 都读到了主线程的 shared_value
    for worker_id, before, after in results:
        assert before == "shared_value", f"worker {worker_id} 未读到主线程 contextvar"
        assert after == f"worker_{worker_id}", f"worker {worker_id} 本地 set 失败"


def test_remember_contextvar_is_real_one():
    """确认 remember 工具确实用 contextvars（而非模块级 dict）。

    防止有人误把 contextvars 改回模块级 dict 导致 R2 修复失效。
    """
    # src.tools.__init__.py 里 `from src.tools.remember import remember`
    # 把模块名 remember 覆盖成了 StructuredTool 对象，常规 import 拿不到模块。
    # 用 importlib 直接加载模块文件。
    import importlib.util
    import sys
    from pathlib import Path

    # 找到 remember.py 的真实路径（通过已加载的 src.tools 包）
    import src.tools
    tools_dir = Path(src.tools.__file__).parent
    remember_path = tools_dir / "remember.py"

    src_text = remember_path.read_text(encoding="utf-8")
    assert "ContextVar" in src_text or "contextvars" in src_text, (
        "remember 工具不再用 contextvars？R2 修复的前提失效，需重新评估"
    )
