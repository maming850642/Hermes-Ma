"""
进程内 workspace 访问器（模块级单例）。

生命周期：workspace 插件启动时 set_service(svc)，teardown 时 set_service(None)。
worker 子进程与 Web 主进程各自 boot_context，各自持有一个 service
（同一 SQLite 库，跨进程经 WAL 共享状态）。

未 set 时（独立进程 / CLI / 测试）：
    current_root() 回退 paths.agent_home()（行为与 T2b 一致）
    current_status() 返回 None（resolve_tools 据此走 chat-only 安全回退）
"""
from __future__ import annotations

import logging
from pathlib import Path

from src.workspace.models import MountState

logger = logging.getLogger("hermes.workspace.state")

# 进程内单例（WorkspaceService | None；避免运行时 import service 造成环）
_service: "object | None" = None

# 挂载根校验缓存：{"path", "mounted_at", "resolved"}（R3-17 换根防御）。
# 同一 (path, mounted_at) 的首次校验结论（resolve 后的真实路径）缓存复用；
# 每次调用仍做轻量 recheck（存在 + realpath 未漂移），失效即回退。
_mount_check_cache: dict | None = None


def set_service(svc) -> None:
    """注册/注销进程内 WorkspaceService（插件 apply/teardown 调用）。"""
    global _service
    _service = svc


def get_service():
    """当前进程的 WorkspaceService；未 boot 时 None。"""
    return _service


def current_status() -> MountState | None:
    """当前挂载状态（None = 从未配置 / 未 boot）。"""
    if _service is None:
        return None
    try:
        return _service.status()  # type: ignore[attr-defined]
    except Exception:
        return None


def _validated_mount_root(st: MountState) -> "Path | None":
    """挂载根校验（带缓存 + recheck）。校验失败返回 None（调用方回退）。

    R3-17 junction 换根防御：挂载点目录在挂载后被删除/替换成指向别处的
    链接时，path.resolve() 会漂移到新目标——每次调用 recheck
    （目录仍存在 + realpath 与首验一致），漂移即回退 agent_home 并告警。
    """
    global _mount_check_cache
    path = Path(st.path)
    key = (str(path), st.mounted_at)
    cached = _mount_check_cache
    if cached is None or (cached["path"], cached["mounted_at"]) != key:
        # 新挂载（或挂载变更）→ 首次校验，缓存 resolve 结论
        try:
            resolved = path.resolve()
        except OSError:
            logger.warning(f"挂载根无法解析，回退 agent 家目录: {st.path}")
            return None
        _mount_check_cache = {"path": str(path), "mounted_at": st.mounted_at, "resolved": resolved}
        cached = _mount_check_cache
    resolved = cached["resolved"]
    try:
        if path.exists() and path.resolve() == resolved:
            return path
    except OSError:
        pass
    logger.warning(
        f"挂载根校验失效（目录被删/替换成链接？），回退 agent 家目录: {st.path}"
    )
    return None


def current_root() -> Path:
    """fs/shell 工具的工作根。

    挂载（local/upload）→ 挂载路径（recheck 失效回退 agent_home，R3-17）；
    未挂载/未配置 → paths.agent_home()。
    注意：未配置模式下 fs/shell 工具根本不会出现在工具集里
    （resolve_tools 的 chat-only 过滤），这里是双保险。
    """
    st = current_status()
    if st is not None and st.is_mounted() and st.path:
        root = _validated_mount_root(st)
        if root is not None:
            return root
    from src.storage import paths
    return paths.agent_home()
