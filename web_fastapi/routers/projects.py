"""项目空间 API（/api/projects）——ADR-0005 产品面后端。

端点：
    GET  /api/projects                    卡片列表（最近打开在前）+ 当前激活 slug
    POST /api/projects                    新建项目 {name, type=hosted|mounted, path?, slug?}
    POST /api/projects/{slug}/activate    激活项目（叠在现有单挂载槽上复用挂载流水线）
    DELETE /api/projects/{slug}           删除项目（内建收件箱拒绝；F6：项目下
                                          还有会话时默认 409，?force=true 级联清会话）
    POST /api/projects/pick-dir           弹出主机原生目录选择器（限本机来源）

激活语义（零新运行时概念）：
- inbox  → WorkspaceService.choose_chat_only()（mode=none，工具白名单最小化）
- hosted → 确保 data/projects/spaces/<slug> 存在后 mount_local 该目录
- mounted→ mount_local 原路径（完整走既有黑名单/symlink 校验，失败即 400）
成功后 touch last_opened_at + 写激活指针 + fire-and-forget 通知 worker。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
)
from pydantic import BaseModel

from src.constants import LOCAL_USER
from src.storage.projects_store import (
    INBOX_SLUG,
    TYPE_HOSTED,
    TYPE_MOUNTED,
    ProjectError,
    ProjectStore,
    validate_slug,
)
from src.workspace.service import WorkspaceError
from web_fastapi.dependencies import get_current_user_id

logger = logging.getLogger("hermes.web.projects")
router = APIRouter()


def _get_storage(request: Request):
    """主进程共享 storage（ctx.boot 的组合根）；未 boot 时回退默认库。"""
    ctx = getattr(request.app.state, "cordis_ctx", None)
    if ctx is not None:
        try:
            svc = ctx.try_get("storage")
        except Exception:
            svc = None
        if svc is not None:
            return svc
    from src.storage.sqlite_provider import SQLiteProvider
    return SQLiteProvider()


def _get_store(request: Request) -> ProjectStore:
    return ProjectStore(_get_storage(request))


@router.post("/pick-dir")
async def pick_local_directory(request: Request):
    """弹出服务器主机上的原生目录选择器，返回所选绝对路径。

    "打开本地目录"的产品预期是弹系统资源管理器式选目录，而不是让用户
    手敲绝对路径。本应用是本机单用户定位（服务与浏览器同机），tkinter
    原生对话框弹在用户自己屏幕上。
    限本机来源：暴露到局域网时，远端请求不允许在别人机器上弹 GUI。
    用户取消 → {"path": null}；无图形环境/tkinter 缺失 → 501，前端回退手输。
    """
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="目录选择器仅限本机使用")

    def _pick() -> str:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)  # 保证盖在浏览器窗口之上
        try:
            return filedialog.askdirectory(parent=root, title="选择项目目录") or ""
        finally:
            root.destroy()

    try:
        # 对话框必须离开事件循环线程；Tk root 在同一线程内创建并销毁
        path = await asyncio.to_thread(_pick)
    except Exception as e:
        raise HTTPException(status_code=501, detail=f"无法打开系统目录选择器: {e}")
    return {"path": path or None}


def _get_workspace_service(request: Request):
    """与 routers/workspace._get_service 同源：组合根 workspace 服务，
    测试场景回退进程内单例；都没有时 503。"""
    svc = None
    ctx = getattr(request.app.state, "cordis_ctx", None)
    if ctx is not None:
        try:
            svc = ctx.try_get("workspace")
        except Exception:
            svc = None
    if svc is None:
        from src.workspace import state as workspace_state
        svc = workspace_state.get_service()
    if svc is None:
        raise HTTPException(status_code=503, detail="Workspace 服务不可用（主进程未 boot）")
    return svc


def _notify_worker_changed(request: Request) -> None:
    """复用 workspace 路由的即时同步通知（BackgroundTask，失败忽略）。"""
    try:
        from web_fastapi.routers.workspace import _notify_worker_changed as fn
        fn(request)
    except Exception:
        logger.debug("projects: workspace_changed 通知失败（下一 turn 动态 resolve 兜底）",
                     exc_info=True)


def _ensure_default(store: ProjectStore) -> None:
    try:
        store.ensure_default()
    except Exception:
        logger.warning("初始化内建收件箱失败（忽略）", exc_info=True)


class CreateProjectBody(BaseModel):
    name: str
    type: str = TYPE_HOSTED          # hosted | mounted
    path: str = ""                   # mounted 必填；hosted 留空自动落托管目录
    slug: str | None = None


class CloneBody(BaseModel):
    url: str                         # 仅 https://
    name: str | None = None          # 展示名；缺省取 URL 末段去 .git
    slug: str | None = None          # 可选指定标识；冲突时自动追加 -N


def _derive_clone_name(url: str) -> str:
    """URL → 展示名：取路径末段去 .git；退化用整 URL。"""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return tail or url


@router.post("/clone")
async def clone_project(request: Request, body: CloneBody,
                        background: BackgroundTasks,
                        user_id: str = Depends(get_current_user_id)):
    """入队 git 克隆任务（异步执行，立即返回 run_id）。

    仅接受 https:// 地址。任务经 RunRegistry("tasks") 账本跟踪，
    进度/失败原因查 GET /api/runs（ADR-0003：死信可视化）。克隆成功后
    才创建 projects 记录——失败不留半成品目录、不留幽灵卡片。
    """
    import uuid as _uuid

    from config import get_settings
    from src.storage import paths
    from src.storage.projects_store import default_slug_for
    from src.storage.run_registry import RunRegistry
    from web_fastapi import project_tasks

    url = (body.url or "").strip()
    if not url.startswith("https://") or len(url) <= len("https://"):
        raise HTTPException(status_code=400, detail="仅支持 https:// 的仓库地址")

    store = ProjectStore(_get_storage(request))
    _ensure_default(store)
    spaces = paths.data_dir("projects", "spaces")
    spaces.mkdir(parents=True, exist_ok=True)

    display_name = (body.name or "").strip() or _derive_clone_name(url)
    try:
        slug_base = validate_slug(body.slug) if body.slug else default_slug_for(display_name)
        slug = store.unique_slug(slug_base)
    except ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    storage = _get_storage(request)
    reg = RunRegistry("tasks", storage)
    run_id = _uuid.uuid4().hex[:12]
    reg.upsert({
        "run_id": run_id, "kind": "git_clone", "label": url,
        "status": "queued", "slug": slug, "error": "",
        "started_at": None, "finished_at": None,
    })

    timeout_s = int(getattr(get_settings(), "git_clone_timeout_seconds", 600) or 600)
    background.add_task(
        project_tasks.perform_clone, storage, run_id, url, slug,
        display_name, str(spaces), timeout_s)

    logger.info(f"clone 入队: run={run_id} slug={slug} url={url}")
    return {"ok": True, "run_id": run_id, "slug": slug, "status": "queued"}


@router.get("")
async def list_projects(request: Request,
                        user_id: str = Depends(get_current_user_id)):
    store = _get_store(request)
    try:
        _ensure_default(store)
        projects = store.list()
        active = store.active_slug()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"项目存储暂不可用: {e}") from e
    for p in projects:
        if p.get("type") in (TYPE_HOSTED, TYPE_MOUNTED) and p.get("path"):
            p["path_exists"] = Path(p["path"]).exists()
    return {"projects": projects, "active": active}


@router.post("")
async def create_project(request: Request, body: CreateProjectBody,
                         user_id: str = Depends(get_current_user_id)):
    store = _get_store(request)
    try:
        record = store.create(body.name, type_=body.type, path=(body.path or "").strip(),
                              slug=body.slug)
    except ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"项目存储暂不可用: {e}") from e
    # hosted 保证目录真实存在（挂载校验要求 exists）
    if record["type"] == TYPE_HOSTED and record.get("path"):
        Path(record["path"]).mkdir(parents=True, exist_ok=True)
    return {"ok": True, "project": record}


@router.post("/{slug}/activate")
async def activate_project(request: Request, slug: str, background: BackgroundTasks,
                           user_id: str = Depends(get_current_user_id)):
    slug = validate_slug(slug)
    store = _get_store(request)
    _ensure_default(store)
    record = store.get(slug)
    if record is None:
        raise HTTPException(status_code=404, detail=f"项目不存在: {slug}")

    ws = _get_workspace_service(request)
    try:
        ptype = record["type"]
        if ptype == "inbox":
            ws.choose_chat_only()
        elif ptype == TYPE_HOSTED:
            root = Path(record["path"])
            root.mkdir(parents=True, exist_ok=True)
            ws.mount_local(str(root), display_name=record["name"])
        elif ptype == TYPE_MOUNTED:
            if not record.get("path") or not Path(record["path"]).exists():
                raise HTTPException(
                    status_code=409,
                    detail=f"项目目录不可用（可能已被移动/删除），请编辑或重建该项目: "
                           f"{record.get('path', '')}")
            ws.mount_local(record["path"], display_name=record["name"])
        else:
            raise HTTPException(status_code=500, detail=f"未知项目类型: {ptype}")
    except HTTPException:
        raise
    except WorkspaceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"挂载服务暂不可用: {e}") from e

    store.touch(slug)
    store.set_active(slug)
    background.add_task(_notify_worker_changed, request)
    logger.info(f"激活项目: {slug} (type={record['type']})")
    return {"ok": True, "active": slug}


@router.delete("/{slug}")
def delete_project(request: Request, slug: str, background: BackgroundTasks,
                   force: bool = False,
                   user_id: str = Depends(get_current_user_id)):
    """删除项目。F6 会话守卫：项目下还有会话时默认 409 拒绝——此前直接删
    项目会把会话变成永久不可达的孤儿（侧栏按项目过滤后看不到、也删不掉）。
    ?force=true 级联删除：先清会话（复用 sessions.py delete 的三连堵，见
    _cascade_delete_sessions），全清成功才删项目，部分失败则 503 保留项目
    （会话仍可达，可重试，不制造新孤儿）。

    同步 def（P2-11，sessions.py 同一规则）：force 级联经 worker IPC 调
    session_delete，worker.send 同步抢 per-worker 锁——async def 里调它会把
    事件循环卡住；同步 def 由 FastAPI 丢线程池执行。
    """
    slug = validate_slug(slug)
    store = _get_store(request)
    was_active = store.active_slug() == slug

    # 404 先于守卫（保持既有契约：项目本身不存在就是 404；inbox 走后面
    # store.delete 的 ProjectError 400，不进会话守卫——收件箱本就不可删）
    if slug != INBOX_SLUG and store.get(slug) is None:
        raise HTTPException(status_code=404, detail=f"项目不存在: {slug}")

    bound_sessions: list[dict] = []
    if slug != INBOX_SLUG:
        from src.session_store import list_sessions
        bound_sessions = list_sessions(LOCAL_USER, project=slug)
        if bound_sessions and not force:
            raise HTTPException(
                status_code=409,
                detail=f"该项目还有 {len(bound_sessions)} 个会话，请先在会话侧栏删除它们",
            )

    sessions_deleted = 0
    if bound_sessions:
        # 级联先于删项目：全清才动项目，避免"项目已删、会话残留"的 F6 孤儿路径
        sessions_deleted = _cascade_delete_sessions(
            request, [s["session_id"] for s in bound_sessions])
        if sessions_deleted < len(bound_sessions):
            raise HTTPException(
                status_code=503,
                detail=f"{len(bound_sessions) - sessions_deleted} 个会话级联删除失败"
                       f"（worker 忙或清理异常），项目未删除，请稍后重试",
            )

    try:
        removed = store.delete(slug)
    except ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not removed:
        raise HTTPException(status_code=404, detail=f"项目不存在: {slug}")
    if was_active:
        # 删除的是激活项：卸载工作区回到未配置态（无挂载时 unmount 抛错属正常）
        try:
            _get_workspace_service(request).unmount()
        except WorkspaceError:
            pass
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"挂载服务暂不可用: {e}") from e
        background.add_task(_notify_worker_changed, request)
    return {"ok": True, "sessions_deleted": sessions_deleted}


def _cascade_delete_sessions(request: Request, sids: list[str]) -> int:
    """F6 force 级联：逐会话复用 sessions.py delete 的三连堵清理。

    三连堵（与 DELETE /api/sessions/{sid} 同一逻辑）：
      ① worker session_delete op——事件库 purge（SessionLog.purge_session，
         chat+waker 两 scope，防 events/fork 复活已删会话）+ 内存桶弹出
         （防 worker 退出时 save_all_buckets 把删档重写回盘）+ 快照 unlink；
      ② 专属槽回收（wm.remove，防僵尸槽占并发名额）；
      ③ 主进程幂等兜底——purge + unlink 再做一遍：worker 忙（锁超时）或
         报错时由此补刀；worker 已做则为空操作（DELETE WHERE 幂等）。
    无 worker_manager 的环境（精简测试 app）无内存桶，直接走 ③。

    返回确认清理成功的会话数（快照文件确已不存在）。
    """
    from src.session_store import _get_session_file
    from web_fastapi.routers.sessions import _sessions_log

    wm = getattr(request.app.state, "worker_manager", None)
    deleted = 0
    for sid in sids:
        try:
            if wm is not None:
                from web_fastapi.worker_manager import DEFAULT_SLOT
                try:
                    worker = wm.lookup(LOCAL_USER, sid) or wm.get_or_create(LOCAL_USER)
                    worker.send("session_delete", session_id=sid)
                except (TimeoutError, RuntimeError):
                    # worker 报"会话文件不存在"（RuntimeError）或锁超时
                    # （TimeoutError）——③的主进程兜底统一收口
                    logger.debug(f"级联删会话: worker IPC 未完成 sid={sid}", exc_info=True)
                if sid != DEFAULT_SLOT:
                    wm.remove(LOCAL_USER, slot=sid)
            # ③ 幂等兜底（对 worker 已完成的路径是空操作）
            _sessions_log(request).purge_session(sid)
            # P3 kv 权威联动：状态行（todos/vfs/waker）与 JSON 缓存同删
            from src.storage.session_state_store import delete_state
            delete_state(sid)
            fpath = _get_session_file(LOCAL_USER, sid)
            if fpath.exists():
                fpath.unlink()
            if not fpath.exists():
                deleted += 1
        except Exception:
            logger.warning(f"级联删除会话失败（跳过）: sid={sid}", exc_info=True)
    return deleted
