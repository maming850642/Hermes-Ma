"""
S1 安全回归：子代理（task 工具）不得绕过工具门禁。

背景（2026-08-15 R1 深度 review）：
    task(inherit_tools=True) 原先直取 get_all_tools()（未过滤全量），
    StreamingSubAgent 内部又直连 spec.executor.execute —— 跳过
    permissions.decide。后果：waker 白名单 / workspace chat-only /
    shell_enabled / plan 模式全部被绕过。

本文件锁定三条门禁链：
① resolve_tools 过滤透传到子代理工具集（config_guard + workspace 模式）；
② 子代理工具执行走 ToolRegistryV3（decide 三层权限生效，plan 拒
   destructive、before_changes 按工具拒绝并继续，不整段失败）；
③ 父级调用级白名单 allowed_tools 透传（waker/flow cfg.tools）。

复现测试先行：修复前全文件应当红。
"""
from unittest.mock import patch, MagicMock

import pytest

import src.agent  # noqa: F401  先完整初始化 agent 包（src.tools ↔ src.agent 有 import 环）
from src.agent.tool_result import ToolResult
from src.llm.messages import Chunk
from src.storage import paths
from src.storage.sqlite_provider import SQLiteProvider
from src.tools.context import ToolContext
from src.tools.schema import ToolSpec, SideEffects
from src.workspace import state as workspace_state
from src.workspace.service import WorkspaceService

# 对 subagent 可见的 chat-only 集：待办/压缩/task + 只读管理面（list_wakers / list_mcps）
# + 收件箱开箱即用的 web_fetch / web_search。
# create_waker / set_waker_enabled / create_wakerflow 声明 blocked_in: [employee, subagent]；
# create_mcp / remove_mcp 声明 blocked_in: [subagent]——子代理不能改配置。
CHAT_ONLY = {
    "write_todos", "compact_conversation", "task", "list_wakers",
    "list_mcps", "web_fetch", "web_search", "use_skill",
}


# ============================================
# 公共设施
# ============================================

@pytest.fixture
def ws(tmp_path):
    """数据根指向 tmp 的 WorkspaceService（挂载状态走 tmp 库，不污染真实数据）。"""
    paths.set_data_root(tmp_path / "data")
    provider = SQLiteProvider(db_path=tmp_path / "data" / "ws.db")
    svc = WorkspaceService(provider=provider)
    workspace_state.set_service(svc)
    yield svc
    workspace_state.set_service(None)
    provider.close()
    paths.set_data_root(None)


def _fake_settings(**overrides):
    """sub_agent.task() / resolve_tools 所需的最小 settings 对象。"""
    s = type("S", (), {})()
    s.sub_agent_max_depth = 1
    s.sub_agent_default_max_tokens = 1000
    s.sub_agent_default_timeout = 60
    s.openai_api_key = "sk-test"
    s.openai_base_url = "http://localhost:1/v1"
    s.llm_model_name = "test-model"
    s.shell_enabled = False
    s.workspace_chat_only_tools = ""
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _capture_subagent_tools(**task_kwargs):
    """调 task() 并捕获传给 StreamingSubAgent 的 kwargs（重点是 tools）。"""
    from src.tools.sub_agent import task, SubAgentResult

    captured = {}

    class FakeSubAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self, instruction):
            return SubAgentResult(success=True, result="ok")

    with patch("src.tools.sub_agent.LLMClient"), \
         patch("src.tools.sub_agent.StreamingSubAgent", FakeSubAgent):
        task(**task_kwargs)
    return captured


class _BoomExecutor:
    """destructive 假执行器：记录调用即视为越权执行。"""

    def __init__(self):
        self.called = False

    def execute(self, args, ctx):
        self.called = True
        return ToolResult(content="BOOM 已执行（不应发生）")


def _destructive_spec() -> tuple[ToolSpec, _BoomExecutor]:
    ex = _BoomExecutor()
    spec = ToolSpec(
        name="boom_tool",
        description="destructive 测试工具",
        parameters={"type": "object", "properties": {}},
        executor=ex,
        side_effects=SideEffects(destructive=True),
    )
    return spec, ex


def _tool_call_chunks(name: str, args_json: str = "{}"):
    """构造一次会产生 tool_call 的 LLM 流。"""
    return [
        Chunk(tool_call_deltas=[{
            "index": 0, "id": "call_1",
            "function": {"name": name, "arguments": args_json},
        }]),
    ]


def _plain_chunks(text: str = "final answer"):
    return [Chunk(content_delta=text)]


# ============================================
# ① resolve_tools 门禁透传到子代理工具集
# ============================================

class TestInheritToolsFiltered:
    """inherit_tools 必须经 resolve_tools 组装（config_guard / workspace 过滤生效）。"""

    def test_shell_disabled_hides_bash(self, ws, tmp_path):
        """shell_enabled=False → 子代理工具集不含 bash（config_guard 生效）。"""
        mnt = tmp_path / "proj"
        mnt.mkdir()
        ws.mount_local(str(mnt))  # 挂载 → workspace 过滤放行全量，只剩 config_guard 拦 bash

        settings = _fake_settings(shell_enabled=False)
        with patch("src.tools.sub_agent.get_settings", return_value=settings):
            captured = _capture_subagent_tools(
                instruction="x", inherit_tools=True,
            )
        names = {t.name for t in captured["tools"]}
        assert "bash" not in names, f"shell_enabled=False 时 bash 不得进入子代理工具集: {sorted(names)}"
        assert "web_search" in names  # 挂载 + 无 config_guard → 其余工具应在

    def test_shell_enabled_includes_bash(self, ws, tmp_path):
        """对照组：shell_enabled=True + 挂载 → bash 在（确保上一条不是恒真）。"""
        mnt = tmp_path / "proj"
        mnt.mkdir()
        ws.mount_local(str(mnt))

        settings = _fake_settings(shell_enabled=True)
        with patch("src.tools.sub_agent.get_settings", return_value=settings):
            captured = _capture_subagent_tools(
                instruction="x", inherit_tools=True,
            )
        names = {t.name for t in captured["tools"]}
        assert "bash" in names

    def test_chat_only_mode_hides_fs_tools(self, ws):
        """workspace 纯对话模式 → 子代理只剩 chat-only 集（fs 工具消失）。"""
        ws.choose_chat_only()

        settings = _fake_settings(shell_enabled=True)  # 即便 shell 开着，chat-only 也应拦
        with patch("src.tools.sub_agent.get_settings", return_value=settings):
            captured = _capture_subagent_tools(
                instruction="x", inherit_tools=True,
            )
        names = {t.name for t in captured["tools"]}
        assert names == CHAT_ONLY, f"纯对话模式子代理工具集应为 chat-only，实际: {sorted(names)}"

    def test_no_inherit_tools_stays_pure_text(self):
        """显式 inherit_tools=False → tools=None（纯文本子代理）。"""
        settings = _fake_settings()
        with patch("src.tools.sub_agent.get_settings", return_value=settings):
            captured = _capture_subagent_tools(instruction="x", inherit_tools=False)
        assert captured["tools"] is None

    def test_default_inherit_tools_injects(self, ws, tmp_path):
        """省略 inherit_tools 时默认 True，注入工具（模型省略参数也能干活）。"""
        mnt = tmp_path / "proj"
        mnt.mkdir()
        ws.mount_local(str(mnt))
        settings = _fake_settings(shell_enabled=True)
        with patch("src.tools.sub_agent.get_settings", return_value=settings):
            captured = _capture_subagent_tools(instruction="x")
        assert captured["tools"], "默认 inherit_tools=True 应注入非空工具集"


# ============================================
# ② 子代理工具执行走 Registry（decide 生效）
# ============================================

class TestSubagentToolExecutionGated:

    def _run_stream(self, spec, permission_mode):
        """构造 StreamingSubAgent 并跑一次带 tool_call 的流。返回 (agent, collected)。"""
        from src.tools.sub_agent import StreamingSubAgent

        agent = StreamingSubAgent(
            name="gate-test", tools=[spec], permission_mode=permission_mode,
        )
        calls = iter([_tool_call_chunks(spec.name), _plain_chunks("done")])

        def fake_stream(llm, messages, **kwargs):
            return iter(next(calls))

        collected = []
        with patch("src.agent.llm_stream.stream_with_hard_timeout", side_effect=fake_stream):
            for tok in agent.stream("run boom"):
                collected.append(tok)
        return agent, collected

    def test_plan_mode_denies_destructive(self):
        """plan 模式：destructive 工具被 decide 拒绝，executor 不执行。"""
        spec, ex = _destructive_spec()
        agent, collected = self._run_stream(spec, permission_mode="plan")

        assert ex.called is False, "plan 模式下 destructive 工具的 executor 不得执行"
        assert any("计划模式" in c for c in collected), \
            f"工具结果应包含 plan 拒绝原因，实际: {collected}"

    def test_before_changes_skips_destructive_and_continues(self):
        """before_changes：destructive 不执行，回写拒绝 tool 消息，run() 仍成功。"""
        from src.tools.sub_agent import StreamingSubAgent

        spec, ex = _destructive_spec()
        agent = StreamingSubAgent(name="gate-test", tools=[spec])  # 默认 before_changes
        calls = iter([_tool_call_chunks(spec.name), _plain_chunks("done")])

        def fake_stream(llm, messages, **kwargs):
            return iter(next(calls))

        collected = []
        with patch("src.agent.llm_stream.stream_with_hard_timeout", side_effect=fake_stream):
            for tok in agent.stream("run boom"):
                collected.append(tok)

        assert ex.called is False, "before_changes 下 destructive 不得执行"
        joined = "".join(collected)
        assert "审批" in joined or "跳过" in joined, \
            f"应回写跳过/审批拒绝，实际: {collected}"
        assert "done" in joined, "拒绝后应继续 ReAct 拿到最终回复"

        # run() 不再整段失败（只读子任务不能被一次写操作拖死）
        agent2 = StreamingSubAgent(name="gate-test2", tools=[spec])
        calls2 = iter([_tool_call_chunks(spec.name), _plain_chunks("final")])
        with patch("src.agent.llm_stream.stream_with_hard_timeout",
                   side_effect=lambda llm, messages, **kw: iter(next(calls2))):
            result = agent2.run("run boom")
        assert result.success is True
        assert ex.called is False
        assert "审批" in result.result or "跳过" in result.result

    def test_full_access_executes(self):
        """full_access 模式：destructive 放行（对照组，证明不是一刀切禁用）。"""
        spec, ex = _destructive_spec()
        agent, collected = self._run_stream(spec, permission_mode="full_access")
        assert ex.called is True
        assert any("BOOM 已执行" in c for c in collected)


# ============================================
# ③ 父级 allowed_tools 白名单透传
# ============================================

class TestAllowedToolsPassthrough:

    def test_parent_whitelist_scopes_subagent(self, ws, tmp_path):
        """父级 ctx.allowed_tools={web_search} → 子代理只继承 web_search。"""
        mnt = tmp_path / "proj"
        mnt.mkdir()
        ws.mount_local(str(mnt))  # 挂载全量环境下，白名单是唯一的收窄来源

        settings = _fake_settings(shell_enabled=True)
        from src.tools.sub_agent import _execute_task

        captured = {}

        class FakeSubAgent:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def run(self, instruction):
                from src.tools.sub_agent import SubAgentResult
                return SubAgentResult(success=True, result="ok")

        ctx = ToolContext(
            permission_mode="before_changes",
            caller_context="main",
            allowed_tools={"web_search"},
        )
        with patch("src.tools.sub_agent.LLMClient"), \
             patch("src.tools.sub_agent.get_settings", return_value=settings), \
             patch("src.tools.sub_agent.StreamingSubAgent", FakeSubAgent):
            _execute_task(
                instruction="x", inherit_tools=True, ctx=ctx,
            )

        names = {t.name for t in captured["tools"]}
        assert names == {"web_search"}, \
            f"父级白名单应透传收窄子代理工具集，实际: {sorted(names)}"
        # permission_mode 仍照旧透传
        assert captured["permission_mode"] == "before_changes"
