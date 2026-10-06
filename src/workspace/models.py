"""
Workspace 模型 —— MountState 挂载状态 + 历史条目。

三种模式（T5 双模式架构）：
    "none"   仅对话：用户明确选择零文件权限（无 fs/shell/网络/进程/MCP/skill 工具）
    "local"  挂载本地文件夹：agent 真实在该目录干活（文件读写/bash 都以它为根）
    "upload" 上传文件夹 zip：解压成托管工作区（data/mounts/<id>）

历史条目与 MountState 同构，多一个 unmounted_at（卸载/切换时间戳）。

注意：MountState 与 None 的语义区分——
    None          从未配置（进门禁，引导页强制选择）
    mode="none"   用户明确选了纯对话（已"配置"，不再弹引导页，但工具集仍是 chat-only）
"""
from __future__ import annotations

from dataclasses import dataclass

# 模式常量
MODE_NONE = "none"
MODE_LOCAL = "local"
MODE_UPLOAD = "upload"

VALID_MODES = frozenset({MODE_NONE, MODE_LOCAL, MODE_UPLOAD})


@dataclass
class MountState:
    """一次挂载/模式选择的当前状态（kv scope=workspace key=mount）。"""

    mode: str
    """none / local / upload。"""

    path: str
    """挂载根的绝对路径。mode=none 时为空串。"""

    display_name: str
    """UI 显示名（缺省用目录名/zip 文件名）。"""

    mounted_at: float
    """挂载时间戳（time.time()）。"""

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "path": self.path,
            "display_name": self.display_name,
            "mounted_at": self.mounted_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MountState":
        """dict → MountState。字段缺失/模式非法时抛异常（调用方自行降级）。"""
        return cls(
            mode=str(d.get("mode", "")),
            path=str(d.get("path", "")),
            display_name=str(d.get("display_name", "")),
            mounted_at=float(d.get("mounted_at", 0.0)),
        )

    def is_mounted(self) -> bool:
        """local/upload 模式（有真实文件根）；none 不算挂载。"""
        return self.mode in (MODE_LOCAL, MODE_UPLOAD)


def history_entry(state: MountState, unmounted_at: float) -> dict:
    """把一条 MountState 转成历史条目（同构 + unmounted_at）。"""
    entry = state.to_dict()
    entry["unmounted_at"] = unmounted_at
    return entry
