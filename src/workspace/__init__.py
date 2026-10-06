"""
workspace —— T5 挂载 + 双模式。

- models.MountState：挂载状态（none/local/upload）
- service.WorkspaceService：状态机（kv 持久化、挂载校验、zip 解压）
- state：进程内访问器（set_service/get_service/current_root/current_status）
"""
from src.workspace.models import MountState

__all__ = ["MountState"]
