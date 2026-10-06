"""
============================================
虚拟文件系统状态（会话持久化基础设施）
============================================
历史：这里曾实现 7 个文件工具（ls/read_file/write_file/edit_file/
copy_file/move_file/delete_file），2026-09 文件工具退役（bash 统一文件
操作）后业务逻辑删除。保留的只有 VFS 状态管理——session_store 的
schema v2 持久化该字段，CLI/Web 的会话保存/恢复链路依赖：

- CLI 不调 set_current_vfs → 回退模块全局 _virtual_fs（行为零变化）。
- Web 每请求 set_current_vfs(session.vfs) → 走实例 dict（per-user 隔离）。
"""

import contextvars

# 虚拟文件系统（仅 workspace_root 为空时使用；文件工具退役后已无写入方，
# 仅为旧会话数据的兼容保留）
_virtual_fs: dict[str, str] = {}

# 2026-06-25: Web 多用户隔离用 contextvar。
_current_vfs: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "hermes_vfs", default=None,
)


def set_current_vfs(d: dict) -> contextvars.Token:
    """Web 端注入当前用户的 vfs（contextvar）。返回 token 供 reset。"""
    return _current_vfs.set(d)


def get_virtual_fs() -> dict[str, str]:
    """获取当前虚拟文件系统。

    Web：优先返回 contextvar 注入的 per-user dict。
    CLI：未注入时回退模块全局 _virtual_fs。
    """
    v = _current_vfs.get()
    return v if v is not None else _virtual_fs


def reset_virtual_fs():
    """重置全局虚拟文件系统（CLI 新会话时调用，不重置 contextvar）。"""
    global _virtual_fs
    _virtual_fs = {}
