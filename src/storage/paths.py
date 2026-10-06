"""
路径约定 —— 数据根定位与拼接。

职责：
- 定位项目根（PROJECT_ROOT，由 __file__ 相对推导，不 import config，
  避免存储层反向依赖配置加载副作用）
- 提供数据目录约定：data_root / data_dir / agent_home
- set_data_root 允许测试覆盖数据根（指向 tmp_path），不碰真实 data/

设计要点：
- 拼接函数只算路径、不建目录（创建时机交给使用方，避免 import 即落盘）
- agent_home 是 agent 的家目录（未来 wakers/wakerflows/projects 所在），
  与 data/ 下 sessions/db 等系统内部数据隔离，家目录整体可拷贝迁移
"""
from __future__ import annotations

from pathlib import Path

# src/storage/paths.py → parents[0]=src/storage, parents[1]=src, parents[2]=项目根
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

# 数据根覆盖（测试用）。None 表示用默认 PROJECT_ROOT/data。
_data_root_override: Path | None = None


def set_data_root(path: str | Path | None) -> None:
    """覆盖数据根（测试隔离用）。传 None 恢复默认。"""
    global _data_root_override
    _data_root_override = Path(path) if path is not None else None


def data_root() -> Path:
    """数据根目录。默认 PROJECT_ROOT/data，可被 set_data_root 覆盖。"""
    if _data_root_override is not None:
        return _data_root_override
    return PROJECT_ROOT / "data"


def data_dir(*parts: str | Path) -> Path:
    """data_root 下拼路径。只算路径，不创建目录。"""
    return data_root().joinpath(*parts)


def agent_home(*parts: str | Path) -> Path:
    """agent 家目录：data/home 下拼路径（= data_dir("home", *parts)）。"""
    return data_dir("home", *parts)
