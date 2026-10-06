"""
路径守卫 —— 把调用方提供的路径强制限制在根目录内。

resolve_under_root(root, p)：
    - 相对 p：相对 root 解析
    - 绝对 p：必须落在 root 内（target == root 或 root in target.parents）
    - 越界抛 PathEscapeError（消息中文）

判包含前两侧都 resolve()：符号链接被解开后再判边界，symlink 逃逸
（挂载根内一个符号链接指向外部）在解析阶段即被拦截。

接入点（chokepoint）：
    - web_fastapi/routers/workspace.py  挂载/上传路径校验
    - （文件工具 2026-09 退役：FsExecutor/_resolve_real_path 接入点随工具删除）

安全边界（如实声明）：这是工具层的路径守卫，不是 OS 级沙箱——
shell 命令仍可 cd 逃逸，破坏性命令依赖 HITL 审批拦截。
"""
from __future__ import annotations

from pathlib import Path


class PathEscapeError(Exception):
    """路径越界（逃出根目录）。"""


def resolve_under_root(root: Path, p: str | Path) -> Path:
    """把 p 解析到 root 内的绝对路径；越界抛 PathEscapeError。

    Args:
        root: 根目录（挂载目录 / agent home），resolve 后作为边界。
        p:    调用方路径。相对 → 相对 root；绝对 → 必须落在 root 内。

    Returns:
        resolve 后的目标绝对路径（可以不存在——写文件的父目录场景）。
    """
    root_resolved = Path(root).resolve()
    raw = Path(p)

    if raw.is_absolute():
        target = raw
    else:
        target = root_resolved / raw
    target = target.resolve()

    if target == root_resolved or root_resolved in target.parents:
        return target

    raise PathEscapeError(f"路径越界：{p} 不在 {root_resolved} 之内")
