"""
stream_invoke 调用点签名守卫（reviewall 终审 #7）。

背景：waker= 签名错误越过了全绿测试套件——mock agent 的 **kwargs 吞掉了
不存在的参数，只有真实调用才炸。本文件用 AST 从**所有真实调用点**的源码
提取 kwargs，逐一断言 ⊆ HermesAgentV3.stream_invoke 真实签名——任何调用
点加错参数，此处立刻红（与格式化/重构无关，AST 提取不像正则那样脆弱）。
"""
import ast
import inspect
from pathlib import Path

import pytest

from src.agent.agent_v3 import HermesAgentV3

# 全部真实调用点（文件: 函数名）
# 2026-09-10: src/cli.py 拆为 src/cli/ 包，chat() 现居 src/cli/chat_loop.py
CALL_SITES = [
    ("src/cli/chat_loop.py", "chat"),
    ("web_fastapi/worker_process.py", "_op_chat"),
    ("web_fastapi/worker_process.py", "_op_chat_approve"),
    ("src/waker/runner.py", "_run_stream"),
    ("src/wakerflow/worker_node.py", "_run_stream"),
]


def _stream_invoke_kwargs_in_func(tree: ast.Module, func_name: str) -> set[str]:
    """AST 提取指定函数体内所有 agent.stream_invoke(...) 调用的关键字参数名。"""
    kwargs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for call in ast.walk(node):
                if (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "stream_invoke"):
                    kwargs.update(kw.arg for kw in call.keywords if kw.arg)
    return kwargs


@pytest.mark.parametrize("path,func", CALL_SITES, ids=[f"{p}:{f}" for p, f in CALL_SITES])
def test_stream_invoke_call_kwargs_are_real(path, func):
    sig = inspect.signature(HermesAgentV3.stream_invoke)
    legal = set(sig.parameters)
    src = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    kwargs = _stream_invoke_kwargs_in_func(tree, func)
    assert kwargs, f"{path}:{func} 未找到 stream_invoke 调用（函数改名或调用被删？）"
    illegal = kwargs - legal
    assert not illegal, (
        f"{path}:{func} 传了 stream_invoke 不存在的参数: {illegal}"
        f"（合法参数: {sorted(legal)}）"
    )


def test_cli_chat_kwarg_guard_replaces_fragile_regex():
    """旧守卫（正则提取）退位说明：AST 版已覆盖，且对格式化免疫。

    此测试锁定：test_cli_history 里的旧正则守卫若被重新依赖会在这里暴露
    （此处直接断言 AST 提取能抓到 CLI 的全部 kwargs）。
    """
    src = Path("src/cli/chat_loop.py").read_text(encoding="utf-8")
    kwargs = _stream_invoke_kwargs_in_func(ast.parse(src), "chat")
    assert "waker_persona" in kwargs
    assert "waker" not in kwargs  # 已修复的 bug 参数不得回归
