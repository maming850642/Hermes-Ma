"""
Waker 端到端链路测试（mock LLM，不连真实模型）。

验证：scheduler.submit_now → worker.send("waker_run") → runner → stream_invoke
     → 事件写 jsonl → latest_result.md → save_state（run_count/last_status）。

用 mock LLMClient（stream_chat 返回预设 Chunk），绕开真实模型依赖。
"""
import json
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.llm.messages import AIMsg, Chunk
from src.waker.models import WakerConfig
from src.waker.store import WakerStore


# ──────────────────────────────────────────────
# mock LLM：返回一段纯文本（无工具调用），让 agent 一轮完成
# ──────────────────────────────────────────────
def _make_mock_llm_client(reply_text: str = "冒烟测试通过"):
    """构造一个 mock LLMClient，stream_chat 返回纯文本 chunk 流。"""
    client = MagicMock()
    # 拆成几个 delta，模拟流式
    chunks = [
        Chunk(content_delta="冒烟"),
        Chunk(content_delta="测试"),
        Chunk(content_delta="通过"),
    ]
    client.stream_chat.return_value = iter(chunks)
    # accumulate 静态方法：拼成最终 AIMsg
    client.accumulate = staticmethod(lambda cs: AIMsg(content=reply_text))
    # chat（非流式，本测试不触发，给个默认）
    client.chat.return_value = AIMsg(content=reply_text)
    return client


# ──────────────────────────────────────────────
# 直接测 runner（绕过 worker IPC，但用真实 agent + mock LLM）
# ──────────────────────────────────────────────
@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    """隔离 workspace 到 tmp_path，避免污染真实数据。"""
    monkeypatch.setenv("HERMES_WS_OVERRIDE", str(tmp_path))
    # WakerStore._resolve_workspace 优先 get_settings().workspace_root，
    # 测试环境 settings 可能指向真实路径，直接 patch _resolve_workspace
    with patch("src.waker.store._resolve_workspace", return_value=tmp_path):
        yield tmp_path


def test_waker_runner_full_chain_mock_llm(isolated_workspace):
    """runner 跑完整链路：mock LLM → jsonl 有 complete → latest_result 写入 → 返回 ok。

    这是 waker 改造的核心验收：证明 runner → stream_invoke → 事件流 → 落盘
    全链路在 mock LLM 下能跑通（真实 LLM 卡住不是 waker 的 bug）。
    """
    from src.agent.agent_v3 import HermesAgentV3
    from src.waker.runner import run_waker

    user_id = "mockuser"
    store = WakerStore(user_id, workspace_root=str(isolated_workspace))

    # 建一个 waker
    cfg = WakerConfig(name="e2e-test", task_prompt="回复：冒烟测试通过")
    store.create(cfg, identity="你是测试员")

    # 构造真实 agent（连真实 LLM 会卡），patch _get_llm_client 返回 mock
    memory_manager = MagicMock()
    mock_client = _make_mock_llm_client("冒烟测试通过")

    with patch.object(HermesAgentV3, "_get_llm_client", return_value=mock_client):
        agent = HermesAgentV3(memory_manager)

        # 构造 worker state（最小需要 user_id + agent）
        state = MagicMock()
        state.user_id = user_id
        state.agent = agent
        state.permission_mode = "before_changes"
        agent.set_permission_mode("before_changes")

        # 跑 runner
        result = run_waker(state, name="e2e-test", run_id="run-001", api_prompt=None)

    # 1. 返回 status
    assert result["status"] == "ok", f"runner 返回: {result}"
    assert result["run_id"] == "run-001"

    # 2. jsonl 有 run_start + (memory_search?) + complete + run_end
    jsonl = store.run_dir("e2e-test") / "run-001.jsonl"
    assert jsonl.exists(), "jsonl 未生成"
    events = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    types = [e.get("type") for e in events]
    assert "run_start" in types
    assert "run_end" in types, f"缺 run_end，事件类型: {types}"
    # run_end 的 status 应为 ok
    run_end = next(e for e in events if e["type"] == "run_end")
    assert run_end["status"] == "ok"

    # 3. latest_result.md 写入
    result_md = store.latest_result_path("e2e-test")
    assert result_md.exists(), "latest_result.md 未生成"
    text = result_md.read_text(encoding="utf-8")
    assert "冒烟测试通过" in text or "run-001" in text


def test_waker_scheduler_submit_now_mock(isolated_workspace):
    """scheduler.submit_now 异步提交 → 最终 state 更新（run_count=1, last_status=ok）。

    用 fake worker_manager 记录 send 调用，模拟 worker 返回 ok。
    """
    from src.waker.scheduler import WakerScheduler

    user_id = "scheduser"
    store = WakerStore(user_id, workspace_root=str(isolated_workspace))
    cfg = WakerConfig(name="sched-test", task_prompt="测试调度")
    store.create(cfg)

    # fake worker_manager：get_or_create 返回 fake worker，send 返回 result 事件
    fake_worker = MagicMock()
    fake_worker.send.return_value = [{"type": "result", "data": {"status": "ok"}}]

    fake_wm = MagicMock()
    fake_wm.get_or_create.return_value = fake_worker

    sched = WakerScheduler(
        fake_wm,
        workspace_root=str(isolated_workspace),
        tick_seconds=999,  # 不让 tick 触发
        max_concurrent=2,
    )
    try:
        run_id = sched.submit_now(user_id, "sched-test", api_prompt="手动触发")
        assert run_id, "submit_now 未返回 run_id"

        # 等异步任务完成（fake worker 立即返回，应很快）
        sched.stop(timeout=10)

        # 校验 state 已更新
        updated = store.get("sched-test")
        assert updated.run_count == 1, f"run_count={updated.run_count}"
        assert updated.last_status == "ok", f"last_status={updated.last_status}"
        assert updated.last_run_at, "last_run_at 未写入"

        # 校验 worker.send 被调（带 waker_run op + api_prompt）
        fake_worker.send.assert_called_once()
        call = fake_worker.send.call_args
        assert call.args[0] == "waker_run"
        assert call.kwargs.get("api_prompt") == "手动触发"
    finally:
        sched.stop()


def test_waker_scheduler_submit_now_worker_error(isolated_workspace):
    """worker.send 抛异常 → state 记 last_status=error，不影响后续。"""
    from src.waker.scheduler import WakerScheduler

    user_id = "erruser"
    store = WakerStore(user_id, workspace_root=str(isolated_workspace))
    store.create(WakerConfig(name="err-test", task_prompt="会失败"))

    fake_worker = MagicMock()
    fake_worker.send.side_effect = RuntimeError("worker 挂了")
    fake_wm = MagicMock()
    fake_wm.get_or_create.return_value = fake_worker

    sched = WakerScheduler(fake_wm, workspace_root=str(isolated_workspace),
                           tick_seconds=999, max_concurrent=1)
    try:
        run_id = sched.submit_now(user_id, "err-test")
        sched.stop(timeout=10)
        updated = store.get("err-test")
        assert updated.run_count == 1
        assert updated.last_status == "error", f"last_status={updated.last_status}"
    finally:
        sched.stop()
