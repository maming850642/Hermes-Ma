"""
git clone 任务执行器（ADR-0003：RunRegistry 任务账本的第一公民）。

设计要点：
- 在主进程线程池里跑（BackgroundTasks 同步函数），绝不碰 worker IPC 锁
- 硬截止：communicate(timeout=) 到点 kill 进程并排空管道（超时失败）
- 防半成品：先克隆到 `.clone-<run_id>` 临时名，成功后原子 rename 为正式
  slug 目录；任何失败路径都清掉临时目录，绝不留下半截目录冒充项目卡片
- 死信可视化：failed/interrupted 的 error 字段带 stderr 尾部，入口页徽标可查

测试注入点：build_clone_cmd() 返回实际命令列表——单测替换它即可绕开
真实 git 二进制。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("hermes.web.project_tasks")

_STDERR_TAIL_CHARS = 600


def build_clone_cmd(url: str, dest: Path) -> list[str]:
    """生产命令（浅克隆）。"""
    return ["git", "clone", "--depth", "1", url, str(dest)]


def run_git_clone(cmd: list[str], timeout_s: int) -> tuple[int | None, str, bool]:
    """执行外部命令到硬截止。返回 (returncode|None, stderr尾部, 是否超时)。"""
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError as e:
        return None, f"无法启动进程: {e}", False
    try:
        _, err = proc.communicate(timeout=max(1, int(timeout_s)))
        return proc.returncode, (err or "").strip(), False
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
        return None, (err or "").strip(), True


def _err_tail(err: str) -> str:
    tail = (err or "").strip()[-_STDERR_TAIL_CHARS:]
    return tail or "(无 stderr 输出)"


def perform_clone(
    storage,
    run_id: str,
    url: str,
    slug: str,
    display_name: str,
    spaces_root: str,
    timeout_s: int = 600,
) -> bool:
    """完整生命周期：running → clone → rename → create project → done/failed。

    返回是否成功。由 BackgroundTasks 在线程池中调用；异常全部就地消化成
    failed 记录（死信进账本，绝不向请求循环抛错）。
    """
    from src.storage.projects_store import ProjectStore
    from src.storage.run_registry import RunRegistry

    reg = RunRegistry("tasks", storage)
    base = Path(spaces_root)
    tmp = base / f".clone-{run_id}"
    final = base / slug

    started_at = time.time()
    reg.update_status(run_id, status="running", started_at=started_at)

    def _fail(reason: str, timed_out: bool = False):
        shutil.rmtree(tmp, ignore_errors=True)
        reg.update_status(run_id, status="failed", error=reason,
                          finished_at=time.time(),
                          duration_s=round(time.time() - started_at, 2))
        logger.warning(f"clone 失败 run={run_id} slug={slug}: {reason}")

    # 临时目录预清理（防上次中断残留撞名）
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    if final.exists():
        _fail(f"目标目录已存在: {final}")
        return False

    cmd = build_clone_cmd(url, tmp)
    rc, err, timed_out = run_git_clone(cmd, timeout_s)
    if timed_out:
        _fail(f"克隆超时（>{int(timeout_s)}s），已终止", timed_out=True)
        return False
    if rc != 0:
        _fail(_err_tail(err))
        return False

    try:
        os.replace(tmp, final)
    except OSError as e:
        _fail(f"落位失败: {e}")
        return False

    try:
        store = ProjectStore(storage)
        store.create(display_name, type_="hosted", path=str(final), slug=slug)
        store.touch(slug)
    except Exception as e:
        logger.exception("clone 成功但创建项目记录失败")
        reg.update_status(run_id, status="done", error=f"(记录创建失败: {e})",
                          finished_at=time.time())
        return True  # 目录已就位，仅记账侧问题

    reg.update_status(run_id, status="done", finished_at=time.time(),
                      duration_s=round(time.time() - started_at, 2))
    logger.info(f"clone 完成: {slug} ← {url} ({round(time.time()-started_at,1)}s)")
    return True
