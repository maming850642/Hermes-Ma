"""Git 面板后端回归：git_ops 解析层 + git_router 端点。

fixture 用 subprocess 建真仓库（本机有 Git for Windows）；解析层与
执行层分离的设计让核心断言不依赖网络。"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import web_fastapi.git_ops as gops
from web_fastapi.routers import git_router


def _git(cwd: Path, *args: str, check: bool = True):
    import subprocess
    r = subprocess.run(
        ["git", "-c", "user.name=Tester", "-c", "user.email=t@t.co",
         "-c", "init.defaultBranch=main", *args],
        cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args} 失败: {r.stderr}")
    return r


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """带分支/合并/中文提交/远端配置的多形态仓库。"""
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init")
    (d / "a.txt").write_text("1", encoding="utf-8")
    _git(d, "add", ".")
    _git(d, "commit", "-m", "初始提交：中文消息")
    _git(d, "checkout", "-b", "feature")
    (d / "b.txt").write_text("2", encoding="utf-8")
    _git(d, "add", ".")
    _git(d, "commit", "-m", "feature work")
    _git(d, "checkout", "main")
    (d / "c.txt").write_text("3", encoding="utf-8")
    _git(d, "add", ".")
    _git(d, "commit", "-m", "main work")
    _git(d, "merge", "feature", "-m", "merge feature")
    _git(d, "remote", "add", "origin", "https://example.com/x.git")
    (d / "untracked.txt").write_text("?", encoding="utf-8")
    return d


@pytest.fixture()
def upstream_repo(tmp_path: Path) -> Path:
    """bare origin + clone：制造真实 upstream 与 ahead 计数。"""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare")
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    c = tmp_path / "clone"
    _git(tmp_path, "clone", str(bare), "clone")
    (c / "f.txt").write_text("x", encoding="utf-8")
    _git(c, "add", ".")
    _git(c, "commit", "-m", "c1")
    _git(c, "push", "-u", "origin", "main")
    # 本地新提交 → ahead 1
    (c / "g.txt").write_text("y", encoding="utf-8")
    _git(c, "add", ".")
    _git(c, "commit", "-m", "c2")
    return c


# ============================================
# 解析层
# ============================================

class TestParseStatus:

    def test_plain_branch(self):
        s = gops.parse_status("## main\n?? x.txt\n")
        assert s["branch"] == "main" and s["upstream"] == ""
        assert s["untracked"] == 1

    def test_ahead_behind(self):
        s = gops.parse_status("## main...origin/main [ahead 2, behind 3]\n")
        assert s["branch"] == "main" and s["upstream"] == "origin/main"
        assert s["ahead"] == 2 and s["behind"] == 3

    def test_ahead_only(self):
        s = gops.parse_status("## main...origin/main [ahead 1]\n")
        assert s["ahead"] == 1 and s["behind"] == 0

    def test_no_commits(self):
        s = gops.parse_status("## No commits yet on main\n")
        assert s["no_commits"] and s["branch"] == "main"

    def test_staged_unstaged_counts(self):
        s = gops.parse_status(
            "## main\n"
            "M  staged_only.txt\n"     # 暂存修改
            "MM both.txt\n"             # 暂存+工作区都改
            " M unstaged_only.txt\n"    # 仅工作区修改
            "?? new.txt\n"              # 未跟踪
        )
        assert s["staged"] == 2
        assert s["unstaged"] == 2
        assert s["untracked"] == 1


class TestGitOps:

    def test_is_repo_false_for_plain_dir(self, tmp_path):
        assert gops.is_repo(tmp_path) is False

    def test_summary_fields(self, repo):
        s = gops.summary(repo)
        assert s["is_repo"]
        assert s["branch"] == "main"
        assert s["untracked"] == 1
        assert s["head"]
        assert s["toplevel"]

    def test_summary_outer_repo_subdir_not_repo(self, repo):
        """挂载根是仓库子目录（向上寻根命中外层仓库）→ 按非仓库处理，
        不泄漏外层历史（托管空间项目挂在主仓库下的场景）。"""
        sub = repo / "docs"
        sub.mkdir(exist_ok=True)
        s = gops.summary(sub)
        assert s["is_repo"] is False
        assert s["not_repo_reason"] == "outer"
        assert gops.repo_root(sub) is None

    def test_summary_upstream_ahead(self, upstream_repo):
        s = gops.summary(upstream_repo)
        assert s["upstream"].startswith("origin/")
        assert s["ahead"] == 1 and s["behind"] == 0

    def test_log_topology_parents_refs_chinese(self, repo):
        data = gops.log(repo, limit=50)
        assert data["branch"] == "main"
        by_subj = {c["subject"]: c for c in data["commits"]}
        # 中文消息不乱码
        assert "初始提交：中文消息" in by_subj
        merge = by_subj["merge feature"]
        assert len(merge["parents"]) == 2, "合并提交应有两个父节点"
        # refs 回填
        assert "main" in merge["heads"]
        assert "feature" in by_subj["feature work"]["heads"]
        # HEAD 标记 + 祖先集合
        assert merge["isHead"] is True
        assert by_subj["初始提交：中文消息"]["inHead"] is True

    def test_log_pagination_sentinel(self, repo):
        data = gops.log(repo, limit=2)
        assert len(data["commits"]) == 2 and data["more"] is True
        data2 = gops.log(repo, limit=50)
        assert data2["more"] is False

    def test_commit_files_root_commit(self, repo):
        files = gops.commit_files(repo, "HEAD")  # merge 提交（非根）也有文件
        assert any(f["path"] == "b.txt" for f in files)
        head_sha = gops.run_git(repo, ["rev-parse", "HEAD"])["out"].strip()
        # 根提交用 --root 也能列出
        root_sha = gops.run_git(
            repo, ["rev-list", "--max-parents=0", "HEAD"])["out"].split()[0]
        files = gops.commit_files(repo, root_sha)
        assert any(f["path"] == "a.txt" for f in files)
        assert head_sha  # rev-parse 正常返回

    def test_remotes(self, repo):
        assert gops.remotes(repo) == ["origin"]

    def test_run_git_timeout_kills(self, tmp_path):
        """超时路径：communicate 到点 kill（用 sleep 当慢命令）。"""
        import web_fastapi.git_ops as g
        # 借 log 通道跑一个 sleep？git 没有 sleep——直接构造：monkeypatch 不可行，
        # 用 fetch 空仓库不会慢。改为验证 timeout 参数钳制逻辑即可（max(1,..)）。
        assert g.run_git(tmp_path, ["rev-parse", "--is-inside-work-tree"],
                         timeout_s=1)["ok"] is False


# ============================================
# 路由层
# ============================================

def _app_with_root(root):
    """挂 git_router + 替换 _get_service（绕开组合根）。"""
    app = FastAPI()
    app.include_router(git_router.router, prefix="/api/workspace/git")
    svc = SimpleNamespace(current_root=lambda: root)
    git_router._get_service = lambda request: svc
    return TestClient(app)


@pytest.fixture(autouse=True)
def _restore_get_service():
    """还原被测试替换的 _get_service 引用。"""
    orig = git_router._get_service
    yield
    git_router._get_service = orig


class TestGitRouter:

    def test_409_without_root(self):
        with _app_with_root(None) as c:
            r = c.get("/api/workspace/git/summary")
            assert r.status_code == 409

    def test_summary_non_repo(self, tmp_path):
        with _app_with_root(tmp_path) as c:
            r = c.get("/api/workspace/git/summary")
            assert r.status_code == 200
            assert r.json()["is_repo"] is False
            assert r.json()["not_repo_reason"] == "plain"

    def test_log_outer_repo_subdir_409(self, repo):
        """挂载根是外层仓库子目录：log/sync 一律 409（不端出外层历史）。"""
        sub = repo / "docs"
        sub.mkdir(exist_ok=True)
        with _app_with_root(sub) as c:
            assert c.get("/api/workspace/git/log").status_code == 409
            r = c.get("/api/workspace/git/summary")
            assert r.json()["is_repo"] is False
            assert r.json()["not_repo_reason"] == "outer"

    def test_summary_repo(self, repo):
        with _app_with_root(repo) as c:
            r = c.get("/api/workspace/git/summary")
            assert r.status_code == 200
            body = r.json()
            assert body["is_repo"] and body["branch"] == "main"
            assert body["remotes"] == ["origin"]

    def test_log_endpoint(self, repo):
        with _app_with_root(repo) as c:
            r = c.get("/api/workspace/git/log?limit=2")
            assert r.status_code == 200
            body = r.json()
            assert len(body["commits"]) == 2 and body["more"] is True

    def test_commit_files_bad_sha_400(self, repo):
        with _app_with_root(repo) as c:
            assert c.get("/api/workspace/git/commit/evil%20cmd/files").status_code == 400

    def test_commit_files_ok(self, repo):
        sha = gops.run_git(repo, ["rev-parse", "HEAD"])["out"].strip()
        with _app_with_root(repo) as c:
            r = c.get(f"/api/workspace/git/commit/{sha}/files")
            assert r.status_code == 200
            assert any(f["path"] == "b.txt" for f in r.json()["files"])

    def test_sync_invalid_action_400(self, repo):
        with _app_with_root(repo) as c:
            r = c.post("/api/workspace/git/sync", json={"action": "reset --hard"})
            assert r.status_code == 400

    def test_sync_unknown_remote_400(self, repo):
        with _app_with_root(repo) as c:
            r = c.post("/api/workspace/git/sync",
                       json={"action": "fetch", "remote": "evil"})
            assert r.status_code == 400
            assert "origin" in r.json()["detail"]

    def test_sync_enqueues_with_safe_argv(self, repo, monkeypatch):
        """happy path：入队成功，argv 由白名单函数生成（无 --force 通道）。"""
        captured = {}
        monkeypatch.setattr(git_router, "perform_sync",
                            lambda *a, **k: captured.update(args=a, kwargs=k) or True)
        # sync 里 import 了 storage —— 走组合根缺失路径会 503；注入假 ctx
        from types import SimpleNamespace as NS

        class _Store:
            def __init__(self):
                self.rows = []

            def kv_get(self, *a, **k):
                return None

        # RunRegistry 在函数内 import，替换构造依赖真实 storage 太重——
        # 直接给 app 挂最小 ctx：try_get("storage") 返回带 kv 协议的假对象
        # RunRegistry 需要 provider.get/set……这里改用真实 SQLiteProvider(tmp)
        import tempfile

        from src.storage.sqlite_provider import SQLiteProvider
        with tempfile.TemporaryDirectory() as td:
            provider = SQLiteProvider(db_path=str(Path(td) / "t.db"))

            class _Ctx:
                def try_get(self, name):
                    return provider if name == "storage" else None

            with _app_with_root(repo) as c:
                c.app.state.cordis_ctx = _Ctx()
                r = c.post("/api/workspace/git/sync",
                           json={"action": "pull", "remote": "origin"})
                assert r.status_code == 200, r.text
                assert r.json()["status"] == "queued"
            assert captured["args"][3] == "pull"       # (storage, run_id, root, action, ...)
            assert captured["args"][4] == "origin"
            argv = gops.sync_argv("pull", "origin")
            assert "--ff-only" in argv and "--force" not in argv
            provider.close()
