"""Git 面板 API —— 挂载工作区的只读视图 + fetch/pull/push 异步同步。

进程归属（调研结论）：全部在主进程线程池执行（clone 先例 project_tasks.py），
绝不走 worker IPC——chat 流式持锁最长 300s，走 worker 面板会卡死。

安全模型：UI 点击 = 用户显式意图（先例：mount/unmount/clone/删除项目），
不经 agent 的 shell_safety/HITL 通道；但自建约束——sync 子命令白名单、
remote 必须是已配置名（防任意 URL 凭据外带）、一律拒绝 --force、sha 校验。
"""
from __future__ import annotations

import logging
import re
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from web_fastapi.git_ops import (
    GitUnavailable, commit_files, log, remotes, perform_sync, repo_root, summary,
)
from web_fastapi.routers.workspace import _get_service

logger = logging.getLogger("hermes.web.git")

router = APIRouter()

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_SYNC_ACTIONS = ("fetch", "pull", "push")


def _root_or_409(request: Request):
    """当前激活根；未配置/仅对话模式 → 409（对齐 workspace._tree_root_or_409）。"""
    root = _get_service(request).current_root()
    if root is None:
        raise HTTPException(status_code=409, detail="当前无激活的工作区根（收件箱/未配置项目无文件树）")
    return root


def _git_repo_or_4xx(request: Request, need_repo: bool):
    root = _root_or_409(request)
    if need_repo:
        try:
            # repo_root：挂载根必须是仓库根本身——向上寻根命中外层仓库
            # （托管空间项目挂在主仓库 data/ 下的形态）一律按非仓库处理
            if repo_root(root) is None:
                raise HTTPException(
                    status_code=409,
                    detail="当前工作区不是 Git 仓库（或仓库根不在挂载根上）",
                )
        except GitUnavailable as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
    return root


def _wrap_503(fn):
    """GitUnavailable → 503 + 指引文案。"""
    try:
        return fn()
    except GitUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/summary")
def git_summary(request: Request):
    """仓库状态：分支/upstream/ahead-behind/变更计数/HEAD。
    非仓库（含挂载目录是外层仓库子目录）→ is_repo False + 原因。"""
    root = _root_or_409(request)
    try:
        data = summary(root)
        data["remotes"] = remotes(root) if data.get("is_repo") else []
    except GitUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return data


@router.get("/log")
def git_log(request: Request, limit: int = 50, skip: int = 0):
    """提交图数据（拓扑序 + refs 回填 + HEAD 标记 + 分页哨兵）。"""
    root = _git_repo_or_4xx(request, need_repo=True)
    return _wrap_503(lambda: log(root, limit=limit, skip=skip))


@router.get("/commit/{sha}/files")
def git_commit_files(request: Request, sha: str):
    """单提交的文件变更清单。"""
    if not _SHA_RE.fullmatch(sha):
        raise HTTPException(status_code=400, detail="非法的 commit sha")
    root = _git_repo_or_4xx(request, need_repo=True)
    return _wrap_503(lambda: {"files": commit_files(root, sha)})


@router.post("/sync")
def git_sync(request: Request, body: dict, background: BackgroundTasks):
    """fetch / pull / push（异步任务，run_id 轮询 GET /api/runs）。

    约束：action 白名单；remote 必须在已配置列表内（防 `fetch <任意URL>`
    凭据外带）；无 --force 通道（v1 一律拒绝强推）。
    """
    from config import get_settings
    from src.storage.run_registry import RunRegistry

    action = str(body.get("action") or "").strip().lower()
    if action not in _SYNC_ACTIONS:
        raise HTTPException(status_code=400, detail=f"action 只能是 {'/'.join(_SYNC_ACTIONS)}")
    remote = str(body.get("remote") or "origin").strip()
    root = _git_repo_or_4xx(request, need_repo=True)

    known = _wrap_503(lambda: remotes(root))
    if remote not in known:
        raise HTTPException(
            status_code=400,
            detail=f"remote「{remote}」未配置。当前可用：{', '.join(known) or '（无）'}",
        )

    storage = None
    ctx = getattr(request.app.state, "cordis_ctx", None)
    if ctx is not None:
        try:
            storage = ctx.try_get("storage")
        except Exception:
            storage = None
    if storage is None:
        raise HTTPException(status_code=503, detail="存储服务不可用")
    reg = RunRegistry("tasks", storage)
    run_id = uuid.uuid4().hex[:12]
    reg.upsert({
        "run_id": run_id, "kind": f"git_{action}", "label": f"{action} {remote}",
        "status": "queued", "error": "",
        "started_at": None, "finished_at": None,
    })
    timeout_s = int(getattr(get_settings(), "git_sync_timeout_seconds", 300) or 300)
    background.add_task(perform_sync, storage, run_id, str(root), action, remote, timeout_s)
    logger.info(f"git sync 入队: run={run_id} {action} {remote} root={root}")
    return {"ok": True, "run_id": run_id, "status": "queued"}
