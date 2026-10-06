"""
Tool-Schema V3 测试 —— 覆盖 YAML 加载、三层权限叠加、Layer 3 evaluator。

测试范围：
    1. YAML 加载成 ToolSpec，字段完整
    2. 16 个内置工具全部加载成功
    3. side_effects 标签正确解析
    4. executor 类型分发正确（ShellExecutor/PythonExecutor）
    5. 三层权限叠加（Layer 2 mode + Layer 3 evaluator）
    6. Layer 3 的 classify_command evaluator（ls 降级 / rm 维持 / fork bomb force_deny）
    7. config_guard 过滤（shell_enabled=False → bash 隐藏）
    8. to_openai() 产出符合 OpenAI 格式

"""

import pytest
from pathlib import Path

from src.tools.schema import ToolSpec, SideEffects, SideEffectsOverride
from src.tools.context import ToolContext
from src.tools.permissions import decide, evaluate_layer2, evaluate_layer3
from src.tools.loader import load_builtin_tools, load_tool_from_yaml, get_tool
from src.tools.executors.shell import ShellExecutor
from src.tools.executors.python import PythonExecutor


# ════════════════════════════════════════════════════════════════
# 1. YAML 加载
# ════════════════════════════════════════════════════════════════

class TestYamlLoading:
    """YAML 工具定义加载测试。"""

    def test_load_all_builtin_tools(self):
        """全部内置工具都能加载成功（文件工具退役 20→13，加 manage_mcp →16，
        加 self_evolve 三件套 →19）。"""
        tools = load_builtin_tools(force=True)
        expected_names = {
            # shell
            "bash",
            # python
            "task", "compact_conversation", "request_human_approval",
            "web_fetch", "web_search", "use_skill", "remember",
            "write_todos",
            # manage_waker（chat 内管理数字员工/流程）
            "list_wakers", "create_waker", "set_waker_enabled", "create_wakerflow",
            # manage_mcp（chat 内接入外部 MCP）
            "list_mcps", "create_mcp", "remove_mcp",
            # self_evolve（自进化：备份/验证/重生）
            "self_backup", "verify_self", "respawn_self",
        }
        loaded_names = set(tools.keys())
        missing = expected_names - loaded_names
        extra = loaded_names - expected_names
        assert not missing, f"缺失工具: {missing}"
        assert not extra, f"多余工具: {extra}"
        assert len(tools) == 19

    def test_load_single_tool_fields(self):
        """单个工具加载后字段完整。"""
        tools = load_builtin_tools(force=True)
        bash = tools["bash"]

        assert bash.name == "bash"
        assert "shell" in bash.description.lower() or "bash" in bash.description.lower()
        assert bash.parameters["type"] == "object"
        assert "command" in bash.parameters["properties"]
        assert "command" in bash.parameters["required"]

    def test_shell_executor_config(self):
        """ShellExecutor 的 command_field/timeout_field 从 YAML runtime 解析。"""
        tools = load_builtin_tools(force=True)
        bash = tools["bash"]

        assert isinstance(bash.executor, ShellExecutor)
        assert bash.executor.command_field == "command"
        assert bash.executor.timeout_field == "timeout"

    def test_bash_description_platform_notice(self):
        """bash 工具 description 必须写明平台环境与全盘扫描禁令。

        背景：模型不知道宿主是 Windows+Git Bash，按 Unix 习惯
        `cd /`、`find /` 全盘扫描（MSYS 虚拟根）→ 30s 超时三连。
        description 是模型每次调用都看得到的说明，平台规范必须在这。
        """
        tools = load_builtin_tools(force=True)
        desc = tools["bash"].description or ""
        assert "Windows" in desc
        assert "Git Bash" in desc
        assert "MSYS" in desc
        assert "相对路径" in desc

    def test_python_executor_config(self):
        """PythonExecutor 的 module/function 从 YAML runtime 解析。"""
        tools = load_builtin_tools(force=True)
        web_fetch = tools["web_fetch"]

        assert isinstance(web_fetch.executor, PythonExecutor)
        assert web_fetch.executor.module == "src.tools.web_fetch"
        assert web_fetch.executor.function == "_execute_web_fetch"

    def test_bash_has_config_guard(self):
        """bash 工具有 config_guard: shell_enabled。"""
        tools = load_builtin_tools(force=True)
        assert tools["bash"].config_guard == "shell_enabled"

    def test_bash_has_se_evaluator(self):
        """bash 工具有 Layer 3 evaluator（classify）。"""
        tools = load_builtin_tools(force=True)
        bash = tools["bash"]

        assert bash.se_evaluator is not None
        # evaluator 是 shell_safety.classify 函数
        assert bash.se_evaluator.__name__ == "classify"


# ════════════════════════════════════════════════════════════════
# 2. side_effects 标签
# ════════════════════════════════════════════════════════════════

class TestSideEffects:
    """side_effects 标签正确性。"""

    @pytest.fixture
    def tools(self):
        return load_builtin_tools(force=True)

    def test_read_only_tools_all_false(self, tools):
        """use_skill 全 false；compact_conversation 非 destructive
        （writes_state=True 合法——它置 compact_requested 状态信号）。"""
        for name in ["use_skill"]:
            se = tools[name].side_effects
            assert not se.destructive, f"{name} 不该标 destructive"
            assert not se.writes_state
            assert not se.network_access
            assert not se.spawns_process
        assert not tools["compact_conversation"].side_effects.destructive

    def test_network_tools(self, tools):
        """web_fetch / web_search 标 network_access。"""
        for name in ["web_fetch", "web_search"]:
            assert tools[name].side_effects.network_access

    def test_spawns_tools(self, tools):
        """bash / task 标 spawns_process。"""
        for name in ["bash", "task"]:
            assert tools[name].side_effects.spawns_process, f"{name} 该标 spawns_process"

    def test_remember_not_destructive(self, tools):
        """remember 标 writes_state 但不标 destructive（写记忆不是改用户文件）。"""
        se = tools["remember"].side_effects
        assert se.writes_state
        assert not se.destructive

    def test_write_todos_not_destructive(self, tools):
        """write_todos 标 writes_state 但不标 destructive。"""
        se = tools["write_todos"].side_effects
        assert se.writes_state
        assert not se.destructive


# ════════════════════════════════════════════════════════════════
# 3. 三层权限叠加
# ════════════════════════════════════════════════════════════════

class TestPermissionModes:
    """Layer 2 mode 决策表测试。"""

    @pytest.fixture
    def tools(self):
        return load_builtin_tools(force=True)

    def test_use_skill_passes_all_modes(self, tools):
        """use_skill（非破坏性）在所有 mode 下放行。"""
        spec = tools["use_skill"]
        for mode in ["full_access", "before_changes", "plan"]:
            ctx = ToolContext(permission_mode=mode)
            d = decide(spec, {}, ctx)
            assert d.is_allow, f"use_skill 在 {mode} 应放行"

    def test_create_waker_in_full_access(self, tools):
        """create_waker（destructive）在 full_access 放行。"""
        ctx = ToolContext(permission_mode="full_access")
        d = decide(tools["create_waker"], {"name": "x"}, ctx)
        assert d.is_allow

    def test_create_waker_in_before_changes(self, tools):
        """create_waker 在 before_changes 需审批。"""
        ctx = ToolContext(permission_mode="before_changes")
        d = decide(tools["create_waker"], {"name": "x"}, ctx)
        assert d.needs_approval

    def test_create_waker_in_plan(self, tools):
        """create_waker 在 plan 被拒绝。"""
        ctx = ToolContext(permission_mode="plan")
        d = decide(tools["create_waker"], {"name": "x"}, ctx)
        assert d.is_deny

    def test_remember_in_plan_passes(self, tools):
        """remember 在 plan 放行（writes_state 不参与 mode 决策）。"""
        ctx = ToolContext(permission_mode="plan")
        d = decide(tools["remember"], {"content": "x"}, ctx)
        assert d.is_allow, "remember 不该被 plan 拒绝（非 destructive）"


# ════════════════════════════════════════════════════════════════
# 4. Layer 3 evaluator（shell_safety.classify）
# ════════════════════════════════════════════════════════════════

class TestShellSafetyClassifier:
    """bash 工具的 Layer 3 evaluator：classify_command 包装。"""

    @pytest.fixture(autouse=True)
    def _isolate_shell_settings(self, monkeypatch):
        """S4 起 classify 读 settings（shell_allowed_commands / shell_blocked_patterns）。

        本类锁定「默认策略表」的行为，须隔离本地 config.yaml 的自定义覆盖
        （如仓库 config.yaml 的黑名单不含 fork bomb/mkfs 模式，否则测试
        结果随用户本机配置漂移）。"""
        s = type("S", (), {})()
        s.shell_allowed_commands = ""
        s.shell_blocked_patterns = ""
        monkeypatch.setattr("config.get_settings", lambda: s)

    @pytest.fixture
    def bash(self):
        return load_builtin_tools(force=True)["bash"]

    def test_ls_downgrades_to_non_destructive(self, bash):
        """ls 命令被 Layer 3 降级为非破坏性 → plan 放行。"""
        ctx = ToolContext(permission_mode="plan")
        d = decide(bash, {"command": "ls -la"}, ctx)
        assert d.is_allow, "ls 在 plan 下应放行（白名单降级）"

    def test_git_log_downgrades(self, bash):
        """git log 被降级。"""
        ctx = ToolContext(permission_mode="plan")
        d = decide(bash, {"command": "git log --oneline -5"}, ctx)
        assert d.is_allow

    def test_rm_maintains_destructive_in_plan(self, bash):
        """rm 在 plan 被拒（维持 destructive）。"""
        ctx = ToolContext(permission_mode="plan")
        d = decide(bash, {"command": "rm somefile"}, ctx)
        assert d.is_deny

    def test_rm_in_before_changes(self, bash):
        """rm 在 before_changes 审批。"""
        ctx = ToolContext(permission_mode="before_changes")
        d = decide(bash, {"command": "rm somefile"}, ctx)
        assert d.needs_approval

    def test_rm_in_full_access(self, bash):
        """rm 在 full_access 放行。"""
        ctx = ToolContext(permission_mode="full_access")
        d = decide(bash, {"command": "rm somefile"}, ctx)
        assert d.is_allow

    def test_fork_bomb_force_deny_in_full_access(self, bash):
        """fork bomb 在 full_access 也拒绝（force_deny 硬底线）。"""
        ctx = ToolContext(permission_mode="full_access")
        d = decide(bash, {"command": ":(){ :|:& };:"}, ctx)
        assert d.is_deny, "fork bomb 必须被 force_deny 拦截，即使 full_access"

    def test_fork_bomb_force_deny_in_plan(self, bash):
        """fork bomb 在 plan 也拒绝。"""
        ctx = ToolContext(permission_mode="plan")
        d = decide(bash, {"command": ":(){ :|:& };:"}, ctx)
        assert d.is_deny

    def test_mkfs_force_deny(self, bash):
        """mkfs 被黑名单 force_deny。"""
        ctx = ToolContext(permission_mode="full_access")
        d = decide(bash, {"command": "mkfs /dev/sda1"}, ctx)
        assert d.is_deny

    def test_pip_install_maintains_destructive(self, bash):
        """pip install 非白名单 → 维持 destructive → before_changes 审批。"""
        ctx = ToolContext(permission_mode="before_changes")
        d = decide(bash, {"command": "pip install requests"}, ctx)
        assert d.needs_approval


# ════════════════════════════════════════════════════════════════
# 5. to_openai() 格式
# ════════════════════════════════════════════════════════════════

class TestOpenAiFormat:
    """ToolSpec.to_openai() 产出符合 OpenAI tools 参数格式。"""

    def test_to_openai_structure(self):
        tools = load_builtin_tools(force=True)
        openai_spec = tools["bash"].to_openai()

        assert openai_spec["type"] == "function"
        assert openai_spec["function"]["name"] == "bash"
        assert "description" in openai_spec["function"]
        assert "parameters" in openai_spec["function"]
        assert openai_spec["function"]["parameters"]["type"] == "object"

    def test_all_tools_to_openai(self):
        """全部内置工具都能转成 OpenAI 格式。"""
        tools = load_builtin_tools(force=True)
        for name, spec in tools.items():
            openai_spec = spec.to_openai()
            assert openai_spec["type"] == "function"
            assert "name" in openai_spec["function"]
            assert "parameters" in openai_spec["function"]


# ════════════════════════════════════════════════════════════════
# 6. config_guard 过滤（resolve_tools 集成）
# ════════════════════════════════════════════════════════════════

class TestConfigGuard:
    """config_guard 字段的声明正确性。
    resolve_tools 的过滤逻辑在 Phase 3 的 Registry 测试里覆盖。
    """

    def test_bash_config_guard_is_shell_enabled(self):
        tools = load_builtin_tools(force=True)
        assert tools["bash"].config_guard == "shell_enabled"

    def test_self_evolve_tools_config_guard_is_self_evolve_enabled(self):
        """self_evolve 三件套（自进化工具面）有 config_guard: self_evolve_enabled。

        出厂默认关（P3-6 工具面门控）：开关只控工具注入，不削护栏。"""
        tools = load_builtin_tools(force=True)
        for name in ("self_backup", "verify_self", "respawn_self"):
            assert tools[name].config_guard == "self_evolve_enabled", \
                f"{name} 应由 self_evolve_enabled 门控"

    def test_other_tools_no_config_guard(self):
        """bash 与 self_evolve 三件套之外的工具无 config_guard（None）。"""
        tools = load_builtin_tools(force=True)
        guarded = {"bash", "self_backup", "verify_self", "respawn_self"}
        for name, spec in tools.items():
            if name in guarded:
                continue
            assert spec.config_guard is None, f"{name} 不该有 config_guard"


# ════════════════════════════════════════════════════════════════
# 7. P2-4：bash timeout 的 JSON Schema 约束经 coerce_args 生效
# ════════════════════════════════════════════════════════════════

class TestBashTimeoutSchemaConstraint:

    @pytest.fixture
    def bash(self):
        return load_builtin_tools(force=True)["bash"]

    def test_timeout_above_maximum_clamped(self, bash):
        """bash.yaml 声明 timeout maximum: 600——越界值经 coerce_args 钳回。"""
        from src.agent.registry_v3 import coerce_args
        coerced = coerce_args({"command": "ls", "timeout": 99999}, bash.parameters)
        assert coerced["timeout"] == 600

    def test_timeout_zero_unlimited_survives_clamp(self, bash):
        """"0=不限"哨兵语义必须原样穿过 clamp（minimum: 0），负值钳到 0
        而非被剔除——executor 侧非正数都不限时，语义不变。"""
        from src.agent.registry_v3 import coerce_args
        assert coerce_args({"command": "ls", "timeout": 0}, bash.parameters)["timeout"] == 0
        assert coerce_args({"command": "ls", "timeout": -3}, bash.parameters)["timeout"] == 0

    def test_timeout_normal_untouched(self, bash):
        from src.agent.registry_v3 import coerce_args
        assert coerce_args({"command": "ls", "timeout": 30}, bash.parameters)["timeout"] == 30


# ════════════════════════════════════════════════════════════════
# 8. P2-4：ShellExecutor 的 cancel 钩子（registry 串行外层超时路径调用）
# ════════════════════════════════════════════════════════════════

class TestShellExecutorCancelHook:

    def test_cancel_kills_running_process_tree(self, monkeypatch):
        """cancel(call_token) 终止该调用登记表中仍在运行的进程（杀整棵树）；
        已退出进程与空登记表均为无害 no-op。P2-4b 后登记表按调用
        （ToolContext id）分桶，cancel 只作用于自己那一桶。"""
        from src.tools.executors import shell as shell_mod

        killed = []

        class FakeProc:
            pid = 4321
            def __init__(self, running=True):
                self._running = running
            def poll(self):
                return None if self._running else 0

        running = FakeProc(running=True)
        monkeypatch.setattr(shell_mod, "_kill_process_tree", lambda p: killed.append(p))

        ex = shell_mod.ShellExecutor()
        ctx = ToolContext(permission_mode="full_access")
        with ex._proc_lock:
            ex._active_procs.setdefault(id(ctx), set()).add(running)
        ex.cancel(id(ctx))
        assert killed == [running]

        # 进程已退出 → 不重复杀；再 cancel（登记表已空）→ 无害
        running._running = False
        ex.cancel(id(ctx))
        assert killed == [running]

    def test_cancel_without_token_kills_all_compat(self, monkeypatch):
        """cancel() 不带 token = 旧语义兼容：终止全部登记进程（跨调用桶）。"""
        from src.tools.executors import shell as shell_mod

        killed = []

        class FakeProc:
            def __init__(self, pid):
                self.pid = pid
            def poll(self):
                return None

        a, b = FakeProc(1), FakeProc(2)
        monkeypatch.setattr(shell_mod, "_kill_process_tree", lambda p: killed.append(p))

        ex = shell_mod.ShellExecutor()
        with ex._proc_lock:
            ex._active_procs[101] = {a}
            ex._active_procs[202] = {b}
        ex.cancel()
        assert sorted(p.pid for p in killed) == [1, 2]

    def test_execute_registers_and_unregisters_process(self, monkeypatch, tmp_path):
        """execute 期间登记 Popen、结束后注销；timeout=0 显式映射为
        communicate 不限时（bash.yaml"0 表示不限"）。"""
        from src.tools.executors import shell as shell_mod

        recorded = {}

        class FakeProc:
            pid = 7
            returncode = 0
            def communicate(self, timeout=None):
                recorded["timeout"] = timeout
                return (b"out", b"")
            def poll(self):
                return 0

        monkeypatch.setattr(shell_mod.subprocess, "Popen", lambda *a, **k: FakeProc())
        monkeypatch.setattr(shell_mod, "_resolve_shell_program", lambda: None)
        monkeypatch.setattr(shell_mod, "resolve_workspace_root", lambda: tmp_path)

        ex = shell_mod.ShellExecutor(timeout_field="timeout")
        result = ex.execute({"command": "echo hi", "timeout": 0}, None)

        assert recorded["timeout"] is None, "timeout=0（不限）必须映射为 communicate 不限时"
        assert "退出码 0" in result.content
        assert not ex._active_procs, "执行结束后必须从 cancel 登记表注销"

        # 非零超时 → 有限 timeout 透传给 communicate
        ex.execute({"command": "echo hi", "timeout": 600}, None)
        assert recorded["timeout"] == 600
