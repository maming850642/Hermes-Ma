"""
Git 面板操作层 —— 主进程直跑 git CLI（ADR-0003 同款任务模型）。

设计要点（对齐 project_tasks.py 的 clone 先例）：
- 在主进程线程池里执行（BackgroundTasks / run_in_threadpool），绝不碰
  worker IPC 锁——chat 流式期间 worker 锁最长持 300s，走 worker 会把
  面板卡死。
- argv 列表直跑 git.exe（无 shell=True、不经 bash 包装），Windows 加
  CREATE_NO_WINDOW；每次调用附带 -c core.quotepath=false（中文文件名
  不被八进制转义）与 -c i18n.logOutputEncoding=UTF-8。
- 输出捕获 raw bytes，UTF-8 → GBK → replace 回退解码（中文 commit
  message 防乱码，语义对齐 shell 执行器的 _decode_output）。
- 硬截止：communicate(timeout=) 到点 kill。
- env 叠加 GIT_OPTIONAL_LOCKS=0：status 不写 index 锁文件，避免与
  agent 的 bash git 操作互相碰锁。

解析函数（summary/log/commit_files）与执行分离，全部纯字符串处理，
便于单测用 fixture 仓库覆盖。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("hermes.web.git_ops")

_DEFAULT_TIMEOUT = 20
_STDERR_TAIL_CHARS = 600

# Windows：不弹控制台窗口（对齐 src/mcp/client.py 的子进程纪律）
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_git_exe_cache: str | None = None


class GitUnavailable(RuntimeError):
    """找不到 git 可执行文件——路由层转 503 + 指引。"""


def _find_git() -> str:
    """定位 git.exe：PATH → 常见安装路径（对齐 shell.py 的探测思路）。"""
    global _git_exe_cache
    if _git_exe_cache:
        return _git_exe_cache
    found = shutil.which("git")
    if not found:
        candidates = [
            Path.home() / "AppData" / "Local" / "Programs" / "Git" / "cmd" / "git.exe",
            Path.home() / "AppData" / "Local" / "Programs" / "Git" / "bin" / "git.exe",
            Path("C:/Program Files/Git/cmd/git.exe"),
            Path("C:/Program Files (x86)/Git/cmd/git.exe"),
        ]
        for c in candidates:
            if c.is_file():
                found = str(c)
                break
    if not found:
        raise GitUnavailable(
            "未找到 git 可执行文件：请安装 Git for Windows，或把它加入 PATH 后重启服务"
        )
    _git_exe_cache = found
    return found


def _decode(raw: bytes) -> str:
    """bytes → 文本：UTF-8 优先，GBK 回退，最终 replace（git 输出默认 UTF-8，
    但用户环境/历史配置可能产生 GBK）。"""
    if not raw:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def run_git(root: Path, args: list[str], timeout_s: int = _DEFAULT_TIMEOUT) -> dict:
    """在 root 下执行 git 子命令。返回 {ok, rc, out, err, timed_out}。"""
    argv = [_find_git(), "-c", "core.quotepath=false",
            "-c", "i18n.logOutputEncoding=UTF-8", *args]
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.pop("SSL_CERT_FILE", None)  # 防脏值同 src/ipc.py 的清洗思路
    try:
        proc = subprocess.Popen(
            argv, cwd=str(root),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, creationflags=_CREATE_NO_WINDOW,
        )
    except OSError as e:
        return {"ok": False, "rc": None, "out": "", "err": f"无法启动 git: {e}",
                "timed_out": False}
    try:
        out_b, err_b = proc.communicate(timeout=max(1, int(timeout_s)))
        out, err = _decode(out_b), _decode(err_b)
        return {"ok": proc.returncode == 0, "rc": proc.returncode,
                "out": out, "err": err, "timed_out": False}
    except subprocess.TimeoutExpired:
        proc.kill()
        out_b, err_b = proc.communicate()
        err = _decode(err_b)
        return {"ok": False, "rc": None, "out": _decode(out_b),
                "err": err or f"超时（>{int(timeout_s)}s）", "timed_out": True}


# ============================================
# 解析层（纯字符串处理，便于单测）
# ============================================

def is_repo(root: Path) -> bool:
    r = run_git(root, ["rev-parse", "--is-inside-work-tree"])
    return r["ok"] and r["out"].strip() == "true"


def repo_root(root: Path) -> Path | None:
    """挂载根本身是仓库根才返回其路径；否则 None。

    两种 None：普通目录；或向上寻根命中**外层仓库**（挂载目录是某个更大
    仓库的子目录——如托管空间里的项目挂在主仓库 data/ 下）。后者若放行，
    面板会端出外层仓库的完整历史（此时显示的是宿主
    仓库的提交），且 pull/push 越过挂载边界——一律按非仓库处理。
    """
    r = run_git(root, ["rev-parse", "--show-toplevel"])
    if not r["ok"]:
        return None
    top_s = r["out"].strip()
    if not top_s:
        return None
    try:
        if Path(top_s).resolve() == Path(root).resolve():
            return Path(top_s)
    except OSError:
        return None
    return None


def parse_status(output: str) -> dict:
    """解析 `git status --porcelain=v1 -b` 输出。

    → {branch, upstream, ahead, behind, staged, unstaged, untracked, no_commits}
    """
    lines = output.splitlines()
    branch, upstream, ahead, behind, no_commits = "", "", 0, 0, False
    if lines and lines[0].startswith("## "):
        head = lines[0][3:].strip()
        if head.startswith("No commits yet on "):
            no_commits = True
            branch = head[len("No commits yet on "):].strip()
        else:
            if "..." in head:
                branch_part, rest = head.split("...", 1)
                branch = branch_part.strip()
                if "[" in rest:
                    upstream = rest[: rest.index("[")].strip()
                    bracket = rest[rest.index("[") + 1: rest.rindex("]")] if "]" in rest else ""
                    for seg in bracket.split(","):
                        tok = seg.split()
                        if len(tok) == 2 and tok[1].isdigit():
                            if tok[0] == "ahead":
                                ahead = int(tok[1])
                            elif tok[0] == "behind":
                                behind = int(tok[1])
                else:
                    upstream = rest.strip()
            else:
                branch = head.strip()
    staged = unstaged = untracked = 0
    for ln in lines[1:]:
        if len(ln) < 2:
            continue
        x, y = ln[0], ln[1]
        if ln.startswith("??"):
            untracked += 1
            continue
        if x not in (" ", "?"):
            staged += 1
        if y != " ":
            unstaged += 1
    return {"branch": branch, "upstream": upstream, "ahead": ahead, "behind": behind,
            "staged": staged, "unstaged": unstaged, "untracked": untracked,
            "no_commits": no_commits}


def summary(root: Path) -> dict:
    """面板顶部状态。挂载根不是仓库根 → is_repo False（outer/plain 两种原因）。"""
    top = repo_root(root)
    if top is None:
        inside = run_git(root, ["rev-parse", "--is-inside-work-tree"])
        outer = inside["ok"] and inside["out"].strip() == "true"
        return {"is_repo": False,
                "not_repo_reason": "outer" if outer else "plain"}

    st = parse_status(run_git(root, ["status", "--porcelain=v1", "-b"]).get("out", ""))
    head_r = run_git(root, ["rev-parse", "HEAD"])
    head = head_r["out"].strip() if head_r["ok"] else ""
    return {
        "is_repo": True,
        "branch": st["branch"],
        "upstream": st["upstream"],
        "ahead": st["ahead"],
        "behind": st["behind"],
        "staged": st["staged"],
        "unstaged": st["unstaged"],
        "untracked": st["untracked"],
        "no_commits": st["no_commits"],
        "head": head,
        "toplevel": str(top),
    }


def _parse_log(output: str) -> list[dict]:
    """解析 %H%x1f%P%x1f%an%x1f%at%x1f%s%x1e 格式的 log 输出。"""
    commits = []
    for record in output.split("\x1e"):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split("\x1f")
        if len(parts) < 5:
            continue
        h, parents, author, ts, subject = parts[0], parts[1], parts[2], parts[3], parts[4]
        commits.append({
            "hash": h,
            "abbrev": h[:7],
            "parents": parents.split() if parents else [],
            "author": author,
            "date": int(ts) if ts.isdigit() else 0,
            "subject": subject,
            "heads": [], "remotes": [], "tags": [], "isHead": False,
        })
    return commits


def _parse_refs(output: str) -> dict[str, dict[str, list[str]]]:
    """解析 for-each-ref 的 %(refname)%00%(objectname)%00%(*objectname)%00%(refname:short)。

    → {"heads": {hash: [name]}, "remotes": {...}, "tags": {...}}
    附注标签用 *objectname（peeled commit）。
    """
    out: dict[str, dict[str, list[str]]] = {"heads": {}, "remotes": {}, "tags": {}}
    for ln in output.splitlines():
        parts = ln.split("\x00")
        if len(parts) < 4:
            continue
        refname, obj, peeled, short = parts[0], parts[1], parts[2], parts[3]
        target = peeled or obj
        if refname.startswith("refs/heads/"):
            out["heads"].setdefault(target, []).append(short)
        elif refname.startswith("refs/remotes/"):
            # 跳过 origin/HEAD 别名（与真实分支重复）
            if short.endswith("/HEAD"):
                continue
            out["remotes"].setdefault(target, []).append(short)
        elif refname.startswith("refs/tags/"):
            out["tags"].setdefault(target, []).append(short)
    return out


def log(root: Path, limit: int = 50, skip: int = 0) -> dict:
    """提交图数据：拓扑序 commits + refs 回填 + HEAD 标记 + 分页哨兵。"""
    limit = max(1, min(int(limit or 50), 200))
    skip = max(0, int(skip or 0))
    out = run_git(root, [
        "log", "--all", "--topo-order",
        f"--max-count={limit + 1}", f"--skip={skip}",
        "--pretty=format:%H%x1f%P%x1f%an%x1f%at%x1f%s%x1e",
    ])
    if not out["ok"]:
        return {"commits": [], "more": False, "head": "", "branch": "",
                "error": (out["err"] or "").strip()[-_STDERR_TAIL_CHARS:]}
    commits = _parse_log(out["out"])
    more = len(commits) > limit
    commits = commits[:limit]

    refs = _parse_refs(run_git(root, [
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00%(*objectname)%00%(refname:short)",
    ]).get("out", ""))
    head_r = run_git(root, ["rev-parse", "HEAD"])
    head = head_r["out"].strip() if head_r["ok"] else ""
    for c in commits:
        c["heads"] = refs["heads"].get(c["hash"], [])
        c["remotes"] = refs["remotes"].get(c["hash"], [])
        c["tags"] = refs["tags"].get(c["hash"], [])
        c["isHead"] = bool(head) and c["hash"] == head

    # HEAD 祖先集合（用于非祖先 commit 灰显，对齐 VSCode Git Graph 观感）
    ancestors = set()
    if head:
        anc = run_git(root, ["rev-list", "HEAD"])
        if anc["ok"]:
            ancestors = {x for x in anc["out"].split() if x}
    for c in commits:
        c["inHead"] = c["hash"] in ancestors

    branch_r = run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch_r["out"].strip() if branch_r["ok"] else ""
    return {"commits": commits, "more": more, "head": head, "branch": branch}


def commit_files(root: Path, sha: str) -> list[dict]:
    """单提交的文件变更清单。

    合并提交的裸 diff-tree 输出为空（不带 -m 时）——统一改为对第一父的
    diff（与主流 git GUI 的"该提交带来什么"语义一致）；根提交无父，
    回退 --root 全量列出。
    """
    r = run_git(root, ["diff-tree", "--no-commit-id", "--name-status", "-r",
                       f"{sha}^", sha])
    if not r["ok"]:
        r = run_git(root, ["diff-tree", "--no-commit-id", "--name-status", "-r",
                           "--root", sha])
    files = []
    for ln in (r.get("out") or "").splitlines():
        parts = ln.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        letter = status[0] if status else "M"
        path = parts[1]
        if len(parts) >= 3:  # 重命名 old → new
            path = f"{parts[1]} → {parts[2]}"
        files.append({"status": letter, "path": path})
    return files


def remotes(root: Path) -> list[str]:
    r = run_git(root, ["remote"])
    return [x.strip() for x in (r.get("out") or "").splitlines() if x.strip()] if r["ok"] else []


# ============================================
# 同步任务（fetch / pull / push）——RunRegistry 账本第一公民
# ============================================

def sync_argv(action: str, remote: str) -> list[str]:
    """构建同步命令。action 白名单在路由层校验，这里只组装。

    pull 用 --ff-only：分叉时明确失败（提示去终端处理），绝不静默造出
    合并提交；push 不带 --force（v1 一律拒绝强推，路由层校验）。
    """
    if action == "fetch":
        return ["fetch", remote]
    if action == "pull":
        return ["pull", "--ff-only", remote]
    if action == "push":
        return ["push", remote]
    raise ValueError(f"未知 action: {action}")


def perform_sync(storage, run_id: str, root_str: str, action: str,
                 remote: str, timeout_s: int = 300) -> bool:
    """后台同步：running → run_git → done/failed。异常全部就地消化成 failed
    记录（死信进账本，绝不向请求循环抛错）。由 BackgroundTasks 线程池调用。"""
    from src.storage.run_registry import RunRegistry

    reg = RunRegistry("tasks", storage)
    started = time.time()
    reg.update_status(run_id, status="running", started_at=started)
    try:
        argv = sync_argv(action, remote)
        r = run_git(Path(root_str), argv, timeout_s)
    except Exception as e:  # noqa: BLE001 —— 账本死信语义
        reg.update_status(run_id, status="failed", error=str(e),
                          finished_at=time.time(),
                          duration_s=round(time.time() - started, 2))
        return False
    duration = round(time.time() - started, 2)
    if r["ok"]:
        reg.update_status(run_id, status="done", finished_at=time.time(),
                          duration_s=duration)
        logger.info(f"git {action} 完成: run={run_id} root={root_str} ({duration}s)")
        return True
    detail = (r["err"] or r["out"] or "无输出").strip()[-_STDERR_TAIL_CHARS:]
    if r["timed_out"]:
        detail = f"超时（>{int(timeout_s)}s），已终止：" + detail
    reg.update_status(run_id, status="failed", error=detail,
                      finished_at=time.time(), duration_s=duration)
    logger.warning(f"git {action} 失败: run={run_id}: {detail[:200]}")
    return False
