"""Workspace 挂载 API（/api/workspace）——T5 双模式。

端点：
    GET  /api/workspace               当前挂载状态（configured=false=未配置，进门禁）
    POST /api/workspace/mount_local   挂载本地文件夹 {path, display_name?}
    POST /api/workspace/mount_upload  上传 zip（multipart 字段 file=.zip）
    POST /api/workspace/unmount       卸载（mount 置 None，下次进门禁重新选择）
    POST /api/workspace/choose_chat_only  仅对话（引导页第三选）
    GET  /api/workspace/history       历史条目（最新在前）

免认证（ADR-0005：auth 闸门已移除，无登录/签名 cookie；安全边界 =
默认仅绑定 127.0.0.1 回环 + 非回环监听显著警告，另有 Host 白名单与
Origin 同源校验两个全局中间件兜底，见 app.py）。mount/unmount/choose
成功后经 BackgroundTasks 向 worker 发 fire-and-forget op `workspace_changed`
（worker 每 turn 动态 resolve，op 仅用于即时同步+ack，失败不影响结果）。

R3-20 韧性：
- mount_upload 分块流式落盘（8KB 块，边读边累计，超限即中止 413，
  不再整包进内存）；解压挪 run_in_threadpool（不阻塞事件循环）
- 全部端点的状态读取异常 → 503 降级 JSON（不再裸 500）
"""
import logging
import tempfile
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool

from web_fastapi.dependencies import get_current_user_id
from src.constants import LOCAL_USER
from src.workspace.service import WorkspaceError
from src.workspace import state as workspace_state

logger = logging.getLogger("hermes.web.workspace")
router = APIRouter()

# zip 原始上传大小上限（解压总大小上限在 WorkspaceService 里按 settings 控制）
_ZIP_RAW_MAX = 512 * 1024 * 1024

# R3-20：上传分块大小（流式落盘，避免整包进内存）
_UPLOAD_CHUNK_BYTES = 8 * 1024


def _get_service(request: Request):
    """取主进程的 WorkspaceService。

    优先 app.state.cordis_ctx（lifespan 里 boot_context() 的组合根，
    ctx.workspace 即 workspace 插件注册的服务）；ctx 未 boot 的场景
    （如部分测试）回退进程内单例 workspace_state.get_service()——
    两者由 workspace 插件 apply 同时设置，语义一致。

    都没有时 503——主进程无法读写挂载状态。
    """
    svc = None
    ctx = getattr(request.app.state, "cordis_ctx", None)
    if ctx is not None:
        try:
            svc = ctx.try_get("workspace")
        except Exception:
            svc = None
    if svc is None:
        svc = workspace_state.get_service()
    if svc is None:
        raise HTTPException(status_code=503, detail="Workspace 服务不可用（主进程未 boot）")
    return svc


def _degraded_503(op: str, exc: Exception):
    """服务层异常（存储读写失败等）→ 503 降级 JSON（R3-20）。"""
    logger.warning(f"workspace {op} 服务异常，降级 503: {exc}", exc_info=True)
    raise HTTPException(status_code=503, detail=f"Workspace 服务暂不可用（{op} 失败）") from exc


def _notify_worker_changed(request: Request) -> None:
    """fire-and-forget 通知 worker 挂载状态变化（BackgroundTask，失败忽略）。

    worker 侧每 turn 都重新 resolve_tools，此 op 只是即时同步（ack 语义）。
    get_or_create 可能耗时（fork 子进程），所以放后台任务不阻塞响应。
    """
    try:
        wm = getattr(request.app.state, "worker_manager", None)
        if wm is None:
            return
        wp = wm.get_or_create(LOCAL_USER)
        wp.send_fire_and_forget("workspace_changed")
    except Exception:
        logger.debug("workspace_changed 通知 worker 失败（下一 turn 动态 resolve 兜底）", exc_info=True)


def _status_payload(svc) -> dict:
    st = svc.status()
    if st is None:
        return {"configured": False, "mode": None, "path": "", "display_name": "", "mounted_at": None}
    return {
        "configured": True,
        "mode": st.mode,
        "path": st.path,
        "display_name": st.display_name,
        "mounted_at": st.mounted_at,
    }


def _safe_status_payload(svc, op: str) -> dict:
    """状态读取 + 降级（读取失败 → 503，不再 500）。"""
    try:
        return _status_payload(svc)
    except Exception as e:
        _degraded_503(op, e)


@router.get("")
async def get_workspace_status(
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    svc = _get_service(request)
    return _safe_status_payload(svc, "status")


@router.post("/mount_local")
async def mount_local(
    request: Request,
    background: BackgroundTasks,
    path: str = Form(...),
    display_name: str = Form(""),
    user_id: str = Depends(get_current_user_id),
):
    svc = _get_service(request)
    try:
        st = svc.mount_local(path, display_name=display_name)
    except WorkspaceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        _degraded_503("mount_local", e)
    background.add_task(_notify_worker_changed, request)
    return {"ok": True, "mount": _safe_status_payload(svc, "status")}


@router.post("/mount_upload")
async def mount_upload(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user_id),
):
    svc = _get_service(request)

    # 落临时文件再交给 service 解压（service 只认 .zip 后缀）
    suffix = Path(file.filename or "").suffix.lower()
    if suffix != ".zip":
        raise HTTPException(status_code=415, detail="仅支持上传 .zip 文件")

    # R3-20：分块流式落盘（8KB）——边读边累计，超限即中止 413。
    # 旧实现 await file.read() 整包进内存（500MB 上限=同量级内存峰值），
    # 并发上传可打爆主进程；现在内存占用恒为单块。
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    try:
        written = 0
        with open(tmp.name, "wb") as out:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > _ZIP_RAW_MAX:
                    raise HTTPException(status_code=413, detail="zip 文件过大")
                out.write(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="空文件")

        # 解压是同步 CPU/IO 密集（可能几百 MB），挪线程池执行避免阻塞事件循环
        display_name = Path(file.filename or "upload.zip").stem
        try:
            st = await run_in_threadpool(svc.mount_upload, Path(tmp.name), display_name=display_name)
        except WorkspaceError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            _degraded_503("mount_upload", e)
    finally:
        try:
            Path(tmp.name).unlink(missing_ok=True)
        except OSError:
            pass

    background.add_task(_notify_worker_changed, request)
    return {"ok": True, "mount": _safe_status_payload(svc, "status")}


@router.post("/unmount")
async def unmount(
    request: Request,
    background: BackgroundTasks,
    user_id: str = Depends(get_current_user_id),
):
    svc = _get_service(request)
    try:
        svc.unmount()
    except WorkspaceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        _degraded_503("unmount", e)
    background.add_task(_notify_worker_changed, request)
    return {"ok": True, "mount": _safe_status_payload(svc, "status")}


@router.post("/choose_chat_only")
async def choose_chat_only(
    request: Request,
    background: BackgroundTasks,
    user_id: str = Depends(get_current_user_id),
):
    svc = _get_service(request)
    try:
        svc.choose_chat_only()
    except WorkspaceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        _degraded_503("choose_chat_only", e)
    background.add_task(_notify_worker_changed, request)
    return {"ok": True, "mount": _safe_status_payload(svc, "status")}


@router.get("/history")
async def get_history(
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    svc = _get_service(request)
    try:
        history = svc.history()
    except Exception as e:
        _degraded_503("history", e)
    return {"history": history}


# ════════════════════════════════════════════════════════════════
# 只读文件树（ADR-0005 D6）：浏览 + 点击预览；一切写操作仍走对话
# 工具流与 HITL。根＝当前激活工作区（挂载根/托管项目目录）。
# ════════════════════════════════════════════════════════════════

# 大目录哨兵：这些目录不展开，返回"已折叠"节点防 DoS（SLO：千文件渲染≤500ms 的服务端前提）
_TREE_COLLAPSED_DIRS = {".git", "node_modules", "__pycache__", ".venv"}
_PREVIEW_BINARY_SNIFF_BYTES = 8192
# 自包含 HTML（会话导出/原型）截断 = 从中间剪断脚本与内嵌数据，页面必废。
# 预览是用户主动点开的按需读取，放宽到硬顶 8MB 无副作用。
_PREVIEW_HTML_MAX_BYTES = 8 * 1024 * 1024


def _tree_max_entries() -> int:
    from config import get_settings
    try:
        v = int(getattr(get_settings(), "web_tree_max_entries", 500))
    except (TypeError, ValueError):
        v = 500
    return max(10, min(v, 5000))


def _preview_max_bytes() -> int:
    from config import get_settings
    try:
        v = int(getattr(get_settings(), "web_tree_preview_max_bytes", 512 * 1024))
    except (TypeError, ValueError):
        v = 512 * 1024
    return max(1024, min(v, 8 * 1024 * 1024))


def _tree_root_or_409(request: Request):
    """当前激活根；未配置/仅对话模式 → 409（前端据此隐藏树栏）。"""
    root = _get_service(request).current_root()
    if root is None:
        raise HTTPException(status_code=409, detail="当前无激活的工作区根（收件箱/未配置项目无文件树）")
    return root


def _resolve_in_root(root, rel: str) -> Path:
    from src.tools.path_guard import PathEscapeError, resolve_under_root
    try:
        return resolve_under_root(root, rel or "")
    except PathEscapeError as e:
        raise HTTPException(status_code=400, detail=f"路径越界: {e}") from e


def _entry_stat(p: Path) -> dict:
    st = p.stat()
    return {"name": p.name, "type": "dir" if p.is_dir() else "file", "size": st.st_size}


@router.get("/tree")
async def workspace_tree(
    request: Request,
    path: str = "",
    user_id: str = Depends(get_current_user_id),
):
    """懒加载单层列表。

    返回 {root, path, entries:[{name,type,size,collapsed?}], truncated}；
    目录在前、名称升序；命中折叠名单的子目录以 collapsed 标记返回，
    不读取其内容。
    """
    root = _tree_root_or_409(request)
    target = _resolve_in_root(root, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在: {path}")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="tree 仅针对目录；文件请用 preview")

    cap = _tree_max_entries()
    dirs, files = [], []
    truncated = False
    for child in target.iterdir():
        if len(dirs) + len(files) >= cap:
            truncated = True
            break
        name = child.name
        if child.is_dir():
            if name in _TREE_COLLAPSED_DIRS:
                dirs.append({"name": name, "type": "dir", "size": 0, "collapsed": True})
            else:
                dirs.append(_entry_stat(child))
        else:
            files.append(_entry_stat(child))
    dirs.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())
    return {
        "root": str(root),
        "path": path,
        "entries": dirs + files,
        "truncated": truncated,
    }


@router.get("/tree/preview")
async def workspace_tree_preview(
    request: Request,
    path: str,
    user_id: str = Depends(get_current_user_id),
):
    """文本文件只读预览。二进制 → 415；超过大小上限截断并带 truncated 标记。"""
    root = _tree_root_or_409(request)
    target = _resolve_in_root(root, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在: {path}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="仅支持文件预览")

    cap = _preview_max_bytes()
    if target.suffix.lower() in (".html", ".htm"):
        cap = max(cap, _PREVIEW_HTML_MAX_BYTES)
    size = target.stat().st_size
    # 第一遍：嗅探二进制；第二遍：从头按上限截取（嗅探块可能大于 cap）
    with open(target, "rb") as f:
        probe = f.read(min(size, _PREVIEW_BINARY_SNIFF_BYTES))
        if b"\x00" in probe:
            raise HTTPException(status_code=415, detail="二进制文件不支持文本预览")
    with open(target, "rb") as f:
        raw = f.read(cap)
    truncated = size > len(raw)
    text = raw.decode("utf-8", errors="replace")
    return {"path": path, "size": size, "truncated": truncated, "content": text}
