"""
============================================
self_evolve 工具组 —— agent 源码级自进化的安全脚手架
============================================
让 agent 在 chat 里修改自己仓库的代码（src/ tools/ skills/ tests/），
并安全地让改动生效：

    self_backup   改前快照 → .self-evolve/backups/<ts>/（回滚 = cp 回来）
    verify_self   静态检查 + import 冒烟 + pytest 基线对比 + 主进程导入面标注
    respawn_self  防跳步 + 指纹漂移检测 → 置位 worker 重生

生效链路（respawn-only，2026-09 决策）：worker 进程退出（复用 exit op 的
R3-18 统一收尾：落盘会话 → 关 MCP → 拆上下文），manager 下次请求自动拉新
进程，重新 import 全部代码 + 重扫 tools/*.yaml + skills/。会话靠每轮落盘
+ prelog + 冷槽 hydration 无缝续上。

设计约束（与 manage_waker / manage_mcp 同一教义）：
- 验证与备份是工具内置的，不靠 LLM 自觉；respawn 没过 verify 直接拒绝。
- 基线对比而非绝对绿：工作树可能半重构（既有红测试），只拦"新增失败"。
- 主进程导入面自动标注：被 web_fastapi 主进程侧 import 的模块，respawn
  刷不到，必须向用户声明"需手动重启服务"——不靠 agent 记忆红线清单。
- bash 侧的写入护栏在 shell_safety.classify（force_approval / force_deny），
  本模块只做验证、备份与重生编排。

respawn 回调注入（web worker 用，CLI 无）：
模块级 holder + set_respawn_hook。worker_process.main() 在 init 成功后
注册 `_should_exit.set`；CLI / 测试不注册 → respawn_self 降级为提示。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("hermes.tools.self_evolve")

# ── 可注入缝（测试隔离用；生产走 paths.PROJECT_ROOT）──
_REPO_ROOT_OVERRIDE: Path | None = None

_respawn_hook: Callable[[], None] | None = None


def set_respawn_hook(hook: Callable[[], None] | None) -> None:
    """注册/清除重生回调（worker_process 注入 _should_exit.set；测试可注入记录器）。"""
    global _respawn_hook
    _respawn_hook = hook


def _repo_root() -> Path:
    from src.storage import paths
    return _REPO_ROOT_OVERRIDE if _REPO_ROOT_OVERRIDE is not None else paths.PROJECT_ROOT


def _evolve_dir() -> Path:
    return _repo_root() / ".self-evolve"


# ── 常量 ──
# 指纹覆盖面：respawn 会刷新的代码面 + 测试。data/（运行数据）与
# .self-evolve/ 自身不进指纹（否则 verify 写状态文件就把自己判定成漂移）。
_FINGERPRINT_DIRS = ("src", "tools", "skills", "tests", "web_fastapi")
_EXCLUDE_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_EXCLUDE_SUFFIXES = {".pyc", ".pyo"}

# 备份范围外（.git 体积大；data/ 是运行数据，改前备份该走 waker/memory 自己的机制）
_BACKUP_FORBIDDEN_PARTS = (".git", "data", ".self-evolve")

# import 冒烟固定包含的核心入口（新 worker init 必然 import 的模块）
_CORE_IMPORTS = ("src.agent.agent_v3", "src.tools.loader", "src.tools.executors.shell")

_IMPORT_SMOKE_TIMEOUT = 180
_PYTEST_TIMEOUT = 600
_BACKUP_KEEP = 20


# ============================================
# 指纹与路径解析
# ============================================

def _repo_fingerprint() -> str:
    """代码面指纹：(相对路径, 大小, mtime_ns) 全集的 sha256。

    用途：respawn 防跳步——verify_self 记录指纹，respawn 前重算对比，
    任何未被 verify 覆盖的代码改动（并行会话/手动编辑）都会被拒绝。
    """
    root = _repo_root()
    h = hashlib.sha256()
    for top in _FINGERPRINT_DIRS:
        top_path = root / top
        if not top_path.is_dir():
            continue
        for p in sorted(top_path.rglob("*")):
            if not p.is_file():
                continue
            if any(part in _EXCLUDE_DIR_NAMES for part in p.parts):
                continue
            if p.suffix in _EXCLUDE_SUFFIXES:
                continue
            rel = p.relative_to(root).as_posix()
            st = p.stat()
            h.update(f"{rel}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:16]


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_repo_paths(paths: list[str] | str | None) -> tuple[list[Path], str]:
    """把 LLM 给的路径清单解析成仓库内已存在的文件列表。

    Returns:
        (resolved, err)：err 非空表示应直接返回给 LLM 的错误。
    """
    if paths is None:
        return [], "paths 为空。"
    if isinstance(paths, str):
        raw_list = [p.strip() for p in paths.split("\n") if p.strip()]
        if len(raw_list) == 1 and " " in raw_list[0]:
            raw_list = [p.strip() for p in shlex.split(raw_list[0]) if p.strip()]
    else:
        raw_list = [str(p).strip() for p in paths if str(p).strip()]
    if not raw_list:
        return [], "paths 为空。"

    root = _repo_root()
    resolved: list[Path] = []
    missing: list[str] = []
    forbidden: list[str] = []
    seen: set[Path] = set()
    for raw in raw_list:
        p = Path(raw)
        full = p if p.is_absolute() else root / p
        try:
            full = full.resolve()
            full.relative_to(root)
        except (ValueError, OSError):
            forbidden.append(raw)
            continue
        if any(part in _BACKUP_FORBIDDEN_PARTS for part in full.relative_to(root).parts):
            forbidden.append(raw)
            continue
        if not full.is_file():
            missing.append(raw)
            continue
        if full not in seen:
            seen.add(full)
            resolved.append(full)
    if forbidden:
        return [], (
            f"以下路径不在允许范围（须是仓库内文件，且不在 .git/ data/ .self-evolve/ 下）: "
            f"{', '.join(forbidden)}"
        )
    if missing:
        return [], f"以下文件不存在: {', '.join(missing)}"
    return resolved, ""


def _prune_backups(keep: int = _BACKUP_KEEP) -> None:
    """只保留最近 keep 个备份目录（删除走本工具内部，bash 侧被 force_deny 保护）。"""
    backups_root = _evolve_dir() / "backups"
    if not backups_root.is_dir():
        return
    dirs = sorted(
        (d for d in backups_root.iterdir() if d.is_dir()),
        key=lambda d: d.name,
    )
    for old in dirs[:-keep] if len(dirs) > keep else []:
        try:
            shutil.rmtree(old)
        except OSError:
            logger.warning(f"清理旧备份失败: {old}", exc_info=True)


# ============================================
# self_backup
# ============================================

def _execute_self_backup(*, paths: Any, ctx=None) -> str:
    """把待改文件快照到 .self-evolve/backups/<ts>/，返回回滚指引。"""
    resolved, err = _resolve_repo_paths(paths)
    if err:
        return f"备份失败：{err}"

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_root = _evolve_dir() / "backups" / ts
    root = _repo_root()
    for src in resolved:
        dest = dest_root / src.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    _prune_backups()

    rel_list = "\n".join(f"  - {src.relative_to(root).as_posix()}" for src in resolved)
    return (
        f"已备份 {len(resolved)} 个文件 → .self-evolve/backups/{ts}/\n{rel_list}\n"
        f"回滚方式：cp .self-evolve/backups/{ts}/<相对路径> <原路径>"
    )


# ============================================
# verify_self
# ============================================

def _py_module_name(py_file: Path) -> str | None:
    """仓库内 .py → dotted 模块名。tests/ 无包结构（import 会假失败）→ None。"""
    root = _repo_root()
    try:
        rel = py_file.relative_to(root)
    except ValueError:
        return None
    parts = list(rel.parts)
    if parts and parts[0] == "tests":
        return None
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][: -len(".py")]
    if not parts:
        return None
    return ".".join(parts)


def _check_static(resolved: list[Path]) -> list[str]:
    """按扩展名做静态检查，返回问题清单（空 = 全过）。"""
    problems: list[str] = []
    import py_compile

    for p in resolved:
        try:
            if p.suffix == ".py":
                py_compile.compile(str(p), doraise=True)
            elif p.suffix in (".yaml", ".yml"):
                import yaml
                with open(p, encoding="utf-8") as f:
                    yaml.safe_load(f)
            elif p.name == "SKILL.md":
                text = p.read_text(encoding="utf-8", errors="replace")
                m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
                fm = m.group(1) if m else ""
                if not m:
                    problems.append(f"{p.name}: 缺少 frontmatter（--- name/description ---）")
                elif not re.search(r"^name:\s*\S", fm, re.MULTILINE) or not re.search(
                    r"^description:\s*\S", fm, re.MULTILINE
                ):
                    problems.append(f"{p.name}: frontmatter 缺 name 或 description")
        except Exception as e:
            problems.append(f"{p.relative_to(_repo_root()).as_posix()}: {e}")
    return problems


def _run_import_smoke(modules: list[str]) -> tuple[bool, str]:
    """子进程真实 import 被改模块 + 核心入口（抓 py_compile 查不到的 import 期失败）。"""
    uniq = sorted(set(modules) | set(_CORE_IMPORTS))
    code = "import " + ", ".join(uniq)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_repo_root()) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_repo_root()), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_IMPORT_SMOKE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"import 冒烟超时（>{_IMPORT_SMOKE_TIMEOUT}s）"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if len(err) > 1500:
            err = err[:1500] + "…"
        return False, err or f"exit code {proc.returncode}"
    return True, ""


def _run_pytest(pytest_args: str) -> tuple[int, list[str], str, bool]:
    """跑测试（--tb=no -q -rf 收失败清单）。

    Returns:
        (exit_code, failed_ids, tail_summary, timed_out)
    """
    cmd = [sys.executable, "-m", "pytest", "--tb=no", "-q", "-rf"]
    if pytest_args:
        cmd += pytest_args.split()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_repo_root()) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(
            cmd, cwd=str(_repo_root()), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_PYTEST_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return -1, [], f"pytest 超时（>{_PYTEST_TIMEOUT}s），可传 pytest_args 圈子集", True

    out = proc.stdout or ""
    failed_ids = [
        line.split()[1]
        for line in out.splitlines()
        if line.startswith("FAILED ") and len(line.split()) > 1
    ]
    summary_lines = [l for l in out.splitlines() if l.strip().startswith(("failed", "passed", "no tests"))]
    tail = summary_lines[-1] if summary_lines else (out.strip().splitlines()[-1] if out.strip() else "")
    return proc.returncode, sorted(set(failed_ids)), tail, False


_MAIN_SIDE_SKIP = {"worker_process.py"}
_IMPORT_RE_CACHE: dict[str, re.Pattern] = {}


def _file_imports_module(text: str, module: str) -> bool:
    """粗粒度判断一段源文本是否 import 了指定模块（含 from 父包 import 叶名）。"""
    if module not in _IMPORT_RE_CACHE:
        parent, _, leaf = module.rpartition(".")
        parts = [
            rf"^\s*import\s+{re.escape(module)}\b",
            rf"^\s*from\s+{re.escape(module)}\s+import\b",
        ]
        if parent:
            parts.append(
                rf"^\s*from\s+{re.escape(parent)}\s+import\s+[^#\n]*\b{re.escape(leaf)}\b"
            )
        _IMPORT_RE_CACHE[module] = re.compile("|".join(parts), re.MULTILINE)
    return bool(_IMPORT_RE_CACHE[module].search(text))


def _main_process_importers(modules: list[str], changed_files: list[Path]) -> dict[str, list[str]]:
    """对每个被改模块，找 web_fastapi 主进程侧（除 worker_process.py）的导入者。

    主进程 import 的模块 respawn 刷不到——返回给 LLM 的标注让它向用户
    声明"需手动重启服务"。粗粒度正则匹配，宁可多报（是警告不是拦截）。
    """
    root = _repo_root()
    importers: dict[str, set[str]] = defaultdict(set)
    main_files: list[Path] = []
    for pattern in ("web_fastapi/*.py", "web_fastapi/routers/*.py"):
        main_files += [p for p in root.glob(pattern) if p.name not in _MAIN_SIDE_SKIP]
    texts = {}
    for f in main_files:
        try:
            texts[f] = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    for module, f in zip(modules, changed_files):
        # 被改文件本身就在主进程侧 → 直接算
        if f in texts:
            importers[module].add(f.relative_to(root).as_posix())
            continue
        for mf, text in texts.items():
            if _file_imports_module(text, module):
                importers[module].add(mf.relative_to(root).as_posix())
    return {m: sorted(v) for m, v in importers.items() if v}


def _git_status_hint() -> str:
    """git status --porcelain 交叉提示（区分不了谁改的，只报数与样例；失败静默跳过）。"""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(_repo_root()), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    lines = [l for l in (proc.stdout or "").splitlines() if l.strip()]
    if not lines:
        return "git status 干净（无未提交改动）。"
    sample = "; ".join(lines[:5]) + ("…" if len(lines) > 5 else "")
    return f"git status 有 {len(lines)} 处改动（含用户长期未提交文件，非本次全部）：{sample}"


def _execute_verify_self(*, paths: Any, pytest_args: str = "", ctx=None) -> str:
    """静态检查 + import 冒烟 + pytest 基线对比 + 主进程导入面标注 + 记录指纹。"""
    resolved, err = _resolve_repo_paths(paths)
    if err:
        return f"验证失败：{err}"

    root = _repo_root()
    sections: list[str] = []
    verdict_ok = True

    # ① 静态检查
    problems = _check_static(resolved)
    if problems:
        verdict_ok = False
        sections.append("✗ 静态检查失败：\n" + "\n".join(f"  - {p}" for p in problems))
    else:
        sections.append(f"✓ 静态检查通过（{len(resolved)} 个文件：py_compile / YAML / frontmatter）")

    # ② import 冒烟（tests/ 无包结构不参与；由 ③ pytest 覆盖）
    modules = [m for m in (_py_module_name(p) for p in resolved) if m]
    if modules:
        ok, detail = _run_import_smoke(modules)
        if ok:
            sections.append(f"✓ import 冒烟通过（{len(modules)} 个模块 + 核心入口）")
        else:
            verdict_ok = False
            sections.append(f"✗ import 冒烟失败（新 worker 会在 init 阶段炸掉，必须先修）：\n  {detail}")

    # ③ pytest 基线对比
    exit_code, failed_ids, tail, timed_out = _run_pytest(pytest_args or "")
    baseline_file = _evolve_dir() / "baseline.json"
    if timed_out:
        verdict_ok = False
        sections.append("✗ " + tail)
    else:
        baseline = None
        if baseline_file.exists():
            try:
                baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                baseline = None
        if baseline is None:
            # 首次：建立基线（当前树可能既有红测试——只拦以后的新增失败）
            baseline = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "pytest_args": pytest_args or "",
                "failed": failed_ids,
            }
            _evolve_dir().mkdir(parents=True, exist_ok=True)
            baseline_file.write_text(
                json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            n = len(failed_ids)
            sections.append(
                f"✓ 基线已建立：当前 {n} 个失败（{'; '.join(failed_ids[:8])}{'…' if n > 8 else ''}）"
                f"{'，' + tail if tail else ''}。本次改动尚未对比——改完后再跑一次 verify_self。"
            )
        else:
            new_failed = sorted(set(failed_ids) - set(baseline.get("failed", [])))
            recovered = sorted(set(baseline.get("failed", [])) - set(failed_ids))
            if baseline.get("pytest_args", "") != (pytest_args or ""):
                sections.append(
                    "⚠ 基线与本次的 pytest_args 不同，失败集对比可能不可比"
                    "（建议改前改后用同一 scope）。"
                )
            if new_failed:
                verdict_ok = False
                sections.append(
                    f"✗ 新增 {len(new_failed)} 个失败（相对基线）：\n"
                    + "\n".join(f"  - {t}" for t in new_failed[:15])
                )
            else:
                sections.append(
                    f"✓ 无新增失败（当前失败 {len(failed_ids)} 个均在基线内"
                    + (f"，且修复了 {len(recovered)} 个基线失败" if recovered else "")
                    + "）"
                )

    # ④ 主进程导入面标注
    importer_map = _main_process_importers(modules, resolved)
    if importer_map:
        lines = []
        for m, files in importer_map.items():
            lines.append(f"  - {m} ← {', '.join(files)}")
        sections.append(
            "⚠ 主进程导入面：以下改动 respawn 不完全生效（主进程已加载旧代码），"
            "必须向用户声明需手动重启服务：\n" + "\n".join(lines)
        )

    # ⑤ git status 交叉提示
    hint = _git_status_hint()
    if hint:
        sections.append(f"ℹ {hint}")

    # ⑥ 记录验证状态（respawn 防跳步 + 漂移检测的依据）
    state = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "fingerprint": _repo_fingerprint(),
        "files": {
            p.relative_to(root).as_posix(): _file_sha256(p) for p in resolved
        },
        "verdict": "pass" if verdict_ok else "fail",
    }
    _evolve_dir().mkdir(parents=True, exist_ok=True)
    (_evolve_dir() / "verify_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    header = "verify_self：通过 ✅（可 respawn_self）" if verdict_ok else "verify_self：未通过 ❌（先修复或回滚，不要 respawn）"
    rollback = "回滚：从 .self-evolve/backups/<ts>/ cp 回原路径后重跑本工具。"
    return header + "\n" + "\n".join(sections) + "\n" + rollback


# ============================================
# respawn_self
# ============================================

def _execute_respawn_self(*, ctx=None) -> str:
    """防跳步 + 指纹漂移检测通过后，置位 worker 重生标志。"""
    state_file = _evolve_dir() / "verify_state.json"
    if not state_file.exists():
        return (
            "拒绝重生：本会话尚未跑过 verify_self（防跳步——写完必须先验证）。"
            "先调用 verify_self 验证当前仓库状态，再 respawn_self。"
        )
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "拒绝重生：verify_state.json 损坏，请重跑 verify_self。"

    if state.get("verdict") != "pass":
        return (
            "拒绝重生：最近一次 verify_self 未通过。先修复问题或从 "
            ".self-evolve/backups/ 回滚，重跑 verify_self 通过后再来。"
        )
    if _repo_fingerprint() != state.get("fingerprint"):
        return (
            "拒绝重生：仓库自上次 verify_self 后又发生了变化（指纹不一致——"
            "可能有并行会话或手动改动混入）。重新跑 verify_self 后再 respawn_self。"
        )

    hook = _respawn_hook
    if hook is None:
        return (
            "当前运行环境（CLI）不支持进程内重生。代码改动已验证就绪，"
            "请用户退出并重新启动 CLI 以加载新代码。"
        )
    try:
        hook()
    except Exception as e:
        logger.error("respawn 回调执行失败", exc_info=True)
        return f"重生回调执行失败：{e}"
    return (
        "重生已排程：本轮对话结束后 worker 将干净退出（落盘会话 → 关闭 MCP → "
        "拆卸上下文），下一条消息自动拉起新进程、加载全部新代码，会话无缝续上。"
        "请立即收尾本轮（不要再发起新的长任务）。"
    )
