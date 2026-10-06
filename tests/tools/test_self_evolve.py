"""
自进化（self-evolve）回归：写自己仓库的护栏 + 三件套工具。

覆盖：
1. shell_safety.classify 的自进化护栏——
   仓库路径字串（三形态）+ 写信号/非纯读 → force_approval（full_access 也审批）；
   .git/ 内部写入、.self-evolve/ 写/删 → force_deny（任何模式拒绝）；
   纯读仓库文件、普通工作区写入不受影响（回归）。
2. permissions.decide 的 force_approval 分支——只顶掉 full_access 的放行。
3. self_backup / verify_self / respawn_self 工具行为（tmp 仓库 fixture）：
   备份快照与禁入范围；静态检查/import 冒烟/基线对比/主进程导入面标注；
   防跳步（未 verify 拒绝）、verdict 门、指纹漂移拒绝、hook 触发与 CLI 降级。
4. 工具面：三个 YAML 正确加载、respawn_self 声明 destructive + blocked_in。
"""
from pathlib import Path

import pytest

import src.agent  # noqa: F401  import 环兜底
from src.storage import paths
from src.tools import shell_safety
from src.tools import self_evolve as se
from src.tools.context import ToolContext
from src.tools.loader import load_builtin_tools
from src.tools.permissions import decide
from src.tools.shell_safety import classify

# 仓库根的三种命令文本形态（按真实 PROJECT_ROOT 动态生成，跨机器可跑）
_REPO_FWD = str(paths.PROJECT_ROOT).replace("\\", "/").lower()
_REPO_BS = str(paths.PROJECT_ROOT).replace("/", "\\").lower()
_DRIVE, _, _REST = _REPO_FWD.partition(":")
_REPO_MSYS = "/" + _DRIVE + _REST


def _fake_settings(allowed="", blocked=""):
    s = type("S", (), {})()
    s.shell_allowed_commands = allowed
    s.shell_blocked_patterns = blocked
    return s


@pytest.fixture(autouse=True)
def _stable_settings(monkeypatch):
    """classify 读 settings：固定为默认两表，避免本机 config 干扰。"""
    monkeypatch.setattr("config.get_settings", lambda: _fake_settings())


@pytest.fixture
def bash():
    return load_builtin_tools(force=True)["bash"]


# ============================================
# 1. force_approval：写自己仓库 → 任何模式都审批
# ============================================

class TestRepoWriteForceApproval:

    @pytest.mark.parametrize("repo_form", [_REPO_FWD, _REPO_BS, _REPO_MSYS])
    def test_write_signal_with_repo_path(self, repo_form):
        ov = classify({"command": f"sed -i 's/a/b/' {repo_form}/src/tools/shell_safety.py"},
                      ToolContext())
        assert ov.force_approval is True
        assert ov.destructive is True

    def test_redirect_with_repo_path(self):
        ov = classify({"command": f"echo x > {_REPO_FWD}/skills/foo/SKILL.md"}, ToolContext())
        assert ov.force_approval is True

    def test_interpreter_inline_with_repo_path(self):
        """python -c 带仓库绝对路径（非白名单程序）→ 强制审批（堵绕过）。"""
        ov = classify({"command": f"python -c \"open('{_REPO_FWD}/src/prompts.py','w')\""},
                      ToolContext())
        assert ov.force_approval is True

    def test_cd_repo_then_relative_write(self):
        ov = classify({"command": f"cd {_REPO_MSYS} && sed -i 's/a/b/' src/x.py"}, ToolContext())
        assert ov.force_approval is True

    def test_git_commit_in_repo_cwd_path(self):
        """在仓库路径上下文跑 git commit（非只读子命令）→ 强制审批。"""
        ov = classify({"command": f"git -C {_REPO_FWD} commit -m x"}, ToolContext())
        assert ov.force_approval is True

    def test_readonly_repo_access_unaffected(self):
        """纯读仓库文件（cat/grep 白名单只读形态）照旧 auto 放行。"""
        ov = classify({"command": f"cat {_REPO_FWD}/src/prompts.py"}, ToolContext())
        assert ov.force_approval is False
        assert ov.destructive is False

    def test_grep_repo_file_unaffected(self):
        ov = classify({"command": f"grep -n def {_REPO_FWD}/src/tools/shell_safety.py"},
                      ToolContext())
        assert ov.force_approval is False
        assert ov.destructive is False

    def test_plain_write_without_repo_mention_not_forced(self):
        """普通写入（无仓库字串）不升级——full_access 直通语义保留（回归）。"""
        ov = classify({"command": "echo x > out.txt"}, ToolContext())
        assert ov.force_approval is False
        assert ov.destructive is True

    def test_plain_rm_without_repo_mention_not_forced(self):
        ov = classify({"command": "rm tmpfile.txt"}, ToolContext())
        assert ov.force_approval is False
        assert ov.destructive is True


# ============================================
# 2. force_deny：.git/ 与 .self-evolve/ 硬底线
# ============================================

class TestGitAndBackupHardFloor:

    def test_rm_git_dir_denied(self):
        ov = classify({"command": f"rm -rf {_REPO_FWD}/.git"}, ToolContext())
        assert ov.force_deny is True

    def test_redirect_into_git_denied(self):
        ov = classify({"command": f"echo x > {_REPO_FWD}/.git/config"}, ToolContext())
        assert ov.force_deny is True

    def test_read_git_config_allowed(self):
        """纯读 .git 内部文件照旧放行（只读白名单）。"""
        ov = classify({"command": f"cat {_REPO_FWD}/.git/config"}, ToolContext())
        assert ov.force_deny is False
        assert ov.destructive is False

    def test_gitignore_not_false_positive(self):
        """.gitignore 不是 .git 内部——不硬拒（仓库路径写入仍走强制审批）。"""
        ov = classify({"command": f"echo x > {_REPO_FWD}/.gitignore"}, ToolContext())
        assert ov.force_deny is False
        assert ov.force_approval is True

    def test_rm_self_evolve_denied(self):
        ov = classify({"command": f"rm -rf {_REPO_FWD}/.self-evolve"}, ToolContext())
        assert ov.force_deny is True

    def test_redirect_touching_self_evolve_denied(self):
        """写信号 + 提到 .self-evolve → 硬拒（防篡改验证状态）。"""
        ov = classify({"command": f"cat {_REPO_FWD}/.self-evolve/backups/a > /tmp/out"},
                      ToolContext())
        assert ov.force_deny is True

    def test_rollback_cp_from_backup_not_denied(self):
        """从备份 cp 回原路径（回滚通道）不得硬拒——走强制审批即可。"""
        ov = classify(
            {"command": f"cp {_REPO_FWD}/.self-evolve/backups/t/src/a.py {_REPO_FWD}/src/a.py"},
            ToolContext(),
        )
        assert ov.force_deny is False
        assert ov.force_approval is True


# ============================================
# 3. decide() 集成：force_approval 只顶掉 full_access 的放行
# ============================================

class TestDecideForceApproval:

    def test_full_access_repo_write_needs_approval(self, bash):
        d = decide(
            bash,
            {"command": f"sed -i 's/a/b/' {_REPO_FWD}/src/tools/shell_safety.py"},
            ToolContext(permission_mode="full_access"),
        )
        assert d.needs_approval, "full_access 下写仓库也必须审批"

    def test_before_changes_repo_write_needs_approval(self, bash):
        d = decide(
            bash,
            {"command": f"sed -i 's/a/b/' {_REPO_FWD}/src/tools/shell_safety.py"},
            ToolContext(permission_mode="before_changes"),
        )
        assert d.needs_approval

    def test_plan_repo_write_denied(self, bash):
        d = decide(
            bash,
            {"command": f"sed -i 's/a/b/' {_REPO_FWD}/src/tools/shell_safety.py"},
            ToolContext(permission_mode="plan"),
        )
        assert d.is_deny, "force_approval 蕴含 destructive，plan 仍拒绝"

    def test_full_access_plain_write_still_allowed(self, bash):
        """回归：full_access 下普通写入（无仓库字串）仍直通。"""
        d = decide(bash, {"command": "echo x > out.txt"},
                   ToolContext(permission_mode="full_access"))
        assert d.is_allow


# ============================================
# 4. 工具面：三个 YAML 正确加载
# ============================================

class TestToolSurface:

    def test_yaml_tools_load(self):
        tools = load_builtin_tools(force=True)
        for name in ("self_backup", "verify_self", "respawn_self"):
            assert name in tools, f"{name} 应被 loader 扫到"

    def test_respawn_declared_destructive_and_blocked(self):
        spec = load_builtin_tools(force=True)["respawn_self"]
        assert spec.side_effects.destructive is True, "respawn 必须弹审批卡"
        assert set(spec.blocked_in) == {"employee", "subagent"}

    def test_verify_and_backup_not_destructive(self):
        tools = load_builtin_tools(force=True)
        assert tools["verify_self"].side_effects.destructive is False
        assert tools["self_backup"].side_effects.destructive is False
        assert set(tools["verify_self"].blocked_in) == {"employee", "subagent"}


# ============================================
# 5. self_backup / verify_self / respawn_self（tmp 仓库）
# ============================================

@pytest.fixture
def mini_repo(tmp_path, monkeypatch):
    """迷你仓库：覆盖指纹目录 + 主进程侧文件，隔离真实仓库。"""
    monkeypatch.setattr(se, "_REPO_ROOT_OVERRIDE", tmp_path)
    monkeypatch.setattr(se, "_CORE_IMPORTS", ())
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text("X = 1\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_mini.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    wf = tmp_path / "web_fastapi"
    wf.mkdir()
    # 主进程侧文件 import 了 src.pkg.mod —— verify_self 应标注
    (wf / "app.py").write_text("from src.pkg.mod import X\n", encoding="utf-8")
    return tmp_path


class TestSelfBackup:

    def test_creates_snapshot_with_rollback_hint(self, mini_repo):
        out = se._execute_self_backup(paths=["src/pkg/mod.py"])
        backups = list((mini_repo / ".self-evolve" / "backups").iterdir())
        assert len(backups) == 1
        snap = backups[0] / "src" / "pkg" / "mod.py"
        assert snap.read_text(encoding="utf-8") == "X = 1\n"
        assert "回滚" in out

    def test_rejects_forbidden_scopes(self, mini_repo):
        for bad in (".git/HEAD", "data/sessions/x.json", ".self-evolve/verify_state.json"):
            out = se._execute_self_backup(paths=[bad])
            assert "失败" in out, f"{bad} 不该允许备份"

    def test_rejects_outside_repo_and_missing(self, mini_repo):
        assert "失败" in se._execute_self_backup(paths="../outside.py")
        assert "不存在" in se._execute_self_backup(paths=["src/nope.py"])


class TestVerifySelf:

    def test_pass_verdict_and_state_written(self, mini_repo):
        out = se._execute_verify_self(paths=["src/pkg/mod.py"])
        assert "通过" in out
        state_file = mini_repo / ".self-evolve" / "verify_state.json"
        assert state_file.exists()
        import json
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["verdict"] == "pass"
        assert state["fingerprint"]
        assert "src/pkg/mod.py" in state["files"]

    def test_main_process_import_face_annotated(self, mini_repo):
        out = se._execute_verify_self(paths=["src/pkg/mod.py"])
        assert "主进程导入面" in out
        assert "web_fastapi/app.py" in out

    def test_syntax_error_fails(self, mini_repo):
        bad = mini_repo / "src" / "pkg" / "bad.py"
        bad.write_text("def broken(:\n", encoding="utf-8")
        out = se._execute_verify_self(paths=["src/pkg/bad.py"])
        assert "未通过" in out
        assert "静态检查" in out

    def test_import_smoke_catches_import_time_failure(self, mini_repo):
        """py_compile 查不到的 import 期异常由冒烟抓到。"""
        boom = mini_repo / "src" / "pkg" / "boom.py"
        boom.write_text("raise RuntimeError('boom at import')\n", encoding="utf-8")
        out = se._execute_verify_self(paths=["src/pkg/boom.py"])
        assert "未通过" in out
        assert "冒烟" in out

    def test_skill_md_frontmatter_checked(self, mini_repo):
        skill_dir = mini_repo / "skills" / "foo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# 没有 frontmatter\n", encoding="utf-8")
        out = se._execute_verify_self(paths=["skills/foo/SKILL.md"])
        assert "未通过" in out
        assert "frontmatter" in out

    def test_new_failure_detected_against_baseline(self, mini_repo):
        """基线对比语义：先建基线，再引入新失败 → 拦下，verdict=fail。"""
        first = se._execute_verify_self(paths=["src/pkg/mod.py"])
        assert "基线已建立" in first
        (mini_repo / "tests" / "test_bad.py").write_text(
            "def test_bad():\n    assert False\n", encoding="utf-8")
        second = se._execute_verify_self(paths=["src/pkg/mod.py"])
        assert "未通过" in second
        assert "新增 1 个失败" in second
        assert "test_bad" in second

    def test_no_new_failure_passes_with_existing_baseline(self, mini_repo):
        """基线里的失败不拦第二次（既有红测试不算新账）。"""
        (mini_repo / "tests" / "test_bad.py").write_text(
            "def test_bad():\n    assert False\n", encoding="utf-8")
        se._execute_verify_self(paths=["src/pkg/mod.py"])  # 建基线（含 1 个失败）
        out = se._execute_verify_self(paths=["src/pkg/mod.py"])
        assert "通过" in out
        assert "无新增失败" in out


class TestRespawnSelf:

    def test_refuses_without_verify(self, mini_repo):
        out = se._execute_respawn_self()
        assert "拒绝" in out
        assert "verify_self" in out

    def test_refuses_on_drift(self, mini_repo):
        se._execute_verify_self(paths=["src/pkg/mod.py"])
        # verify 之后又有改动（并行会话/手动）→ 指纹漂移
        (mini_repo / "src" / "pkg" / "extra.py").write_text("Y = 2\n", encoding="utf-8")
        out = se._execute_respawn_self()
        assert "拒绝" in out
        assert "指纹" in out

    def test_refuses_failed_verdict(self, mini_repo):
        se._execute_verify_self(paths=["src/pkg/mod.py"])  # 建基线
        (mini_repo / "tests" / "test_bad.py").write_text(
            "def test_bad():\n    assert False\n", encoding="utf-8")
        se._execute_verify_self(paths=["src/pkg/mod.py"])  # fail verdict + 新指纹
        out = se._execute_respawn_self()
        assert "拒绝" in out
        assert "未通过" in out

    def test_triggers_hook_and_reports(self, mini_repo):
        se._execute_verify_self(paths=["src/pkg/mod.py"])
        called = []
        se.set_respawn_hook(lambda: called.append(1))
        try:
            out = se._execute_respawn_self()
            assert called == [1]
            assert "重生" in out
        finally:
            se.set_respawn_hook(None)

    def test_cli_degrades_gracefully(self, mini_repo):
        """无 hook（CLI 环境）→ 降级为提示，不置位。"""
        se._execute_verify_self(paths=["src/pkg/mod.py"])
        out = se._execute_respawn_self()
        assert "CLI" in out
        assert "重新启动" in out
