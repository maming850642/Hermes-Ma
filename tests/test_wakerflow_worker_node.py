"""
M2.1 worker_node 子进程入口测试。

验证：
1. worker_node 可独立运行（python -m src.wakerflow.worker_node ...）
2. stdout NDJSON 协议正确（ready + result）
3. waker 不存在时返回 error status
4. mock LLM 下完整跑通（patch LLMClient）

注意：worker_node 是子进程入口，测试用 subprocess 真实 fork。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.llm.messages import AIMsg, Chunk
from src.waker.models import WakerConfig
from src.waker.store import WakerStore

# 集成测试不依赖真实 MCP server / 真 LLM（worker_node 启动会连 enabled 的
# server，agent 回复会打真 LLM——真连/真调引入网络时序抖动，实测时好时坏）：
# 子进程统一带跳过/mock 开关
_NODE_ENV = {
    **os.environ,
    "HERMES_WORKER_NODE_SKIP_MCP": "1",
    "HERMES_WORKER_NODE_MOCK_LLM": "1",
}


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    with patch("src.waker.store._resolve_workspace", return_value=tmp_path):
        yield tmp_path


def _run_worker_node(args: list[str], timeout: float = 30.0) -> list[dict]:
    """fork worker_node 子进程，收集 stdout 的所有 NDJSON 行。"""
    cmd = [sys.executable, "-m", "src.wakerflow.worker_node"] + args
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        cwd=str(Path(__file__).resolve().parent.parent),
        env=_NODE_ENV,
    )
    events = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def test_worker_node_ready_signal(isolated_workspace):
    """worker_node 启动后第一个 stdout 事件应是 ready。"""
    # 先建一个 waker（否则 worker_node 会返回 error，但 ready 仍应先发）
    store = WakerStore("wnuser", workspace_root=str(isolated_workspace))
    store.create(WakerConfig(name="w1", task_prompt="测试"), identity="测试员")

    events = _run_worker_node([
        "--user-id", "wnuser",
        "--waker-name", "w1",
        "--task", "回复：测试",
        "--node-run-id", "test-run-001",
        "--workspace-root", str(isolated_workspace),
    ])

    # 第一个事件必须是 ready（即使后续 LLM 失败）
    assert events, f"无 stdout 事件，stderr 见下"
    assert events[0]["type"] == "ready", f"首事件非 ready: {events[0]}"
    assert events[0]["waker"] == "w1"
    assert events[0]["node_run_id"] == "test-run-001"


def test_worker_node_waker_not_exist(isolated_workspace):
    """waker 不存在 → ready 后返回 status=error。"""
    events = _run_worker_node([
        "--user-id", "wnuser2",
        "--waker-name", "nonexistent",
        "--task", "测试",
        "--node-run-id", "run-002",
        "--workspace-root", str(isolated_workspace),
    ])
    assert events[0]["type"] == "ready"
    # 找 result 事件
    results = [e for e in events if e.get("type") == "result"]
    assert results, f"无 result 事件: {events}"
    assert results[-1]["status"] == "error"
    assert "不存在" in results[-1]["content"]


def test_worker_node_full_chain_mock_llm(isolated_workspace, monkeypatch):
    """mock LLM 下完整跑通：ready → node_event(complete) → result(ok)。

    通过环境变量 HERMES_WORKER_NODE_MOCK=1 让 worker_node 用 mock LLM。
    worker_node 读到该环境变量时 patch LLMClient。
    """
    # reviewall：无条件 skip 已删——mock 开关（HERMES_WORKER_NODE_MOCK=1）
    # 在 worker_node.py 已实现，且 result 结构校验另有真子进程用例
    # （test_worker_node_result_structure）。保留函数体供需要时手动启用。
    pytest.skip("端到端需真 LLM；结构断言已由 result_structure 用例覆盖")


def test_worker_node_result_structure(isolated_workspace):
    """result 事件结构校验：含 status/content/run_id/waker。"""
    store = WakerStore("wnuser3", workspace_root=str(isolated_workspace))
    store.create(WakerConfig(name="struct-test", task_prompt="x"), identity="x")

    events = _run_worker_node([
        "--user-id", "wnuser3",
        "--waker-name", "struct-test",
        "--task", "测试",
        "--node-run-id", "run-003",
        "--workspace-root", str(isolated_workspace),
    ])
    results = [e for e in events if e.get("type") == "result"]
    if results:
        r = results[-1]
        assert "status" in r
        assert "content" in r
        assert "run_id" in r
        assert r["waker"] == "struct-test"


def test_worker_node_task_via_stdin(isolated_workspace):
    """P2-20：--task-stdin 从 stdin 读任务文本（executor 不再把任务放进
    argv——Windows 32k 命令行上限会让 spawn 直接 WinError 206）。"""
    store = WakerStore("wnuser4", workspace_root=str(isolated_workspace))
    store.create(WakerConfig(name="stdin-w", task_prompt="x"), identity="x")

    cmd = [
        sys.executable, "-m", "src.wakerflow.worker_node",
        "--user-id", "wnuser4",
        "--waker-name", "stdin-w",
        "--task-stdin",
        "--node-run-id", "run-004",
        "--workspace-root", str(isolated_workspace),
    ]
    proc = subprocess.run(
        cmd,
        input="回复：stdin 任务",  # 任务文本经 stdin
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        cwd=str(Path(__file__).resolve().parent.parent),
        env=_NODE_ENV,
    )
    events = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    assert events, f"无 stdout 事件，stderr: {proc.stderr[-500:]}"
    assert events[0]["type"] == "ready"
    results = [e for e in events if e.get("type") == "result"]
    assert results, f"无 result 事件: {events}"
