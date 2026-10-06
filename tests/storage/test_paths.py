"""
paths 测试 —— 数据根定位、覆盖与拼接约定。

不碰真实 data/：所有覆盖场景用 tmp_path。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.storage import paths


@pytest.fixture(autouse=True)
def _reset_data_root():
    """每个用例前后恢复默认数据根，避免覆盖状态泄漏到其他测试。"""
    paths.set_data_root(None)
    yield
    paths.set_data_root(None)


def test_project_root_is_repo_root():
    """PROJECT_ROOT 由 __file__ 推导，应等于仓库根（本测试文件的上上级目录）。"""
    assert paths.PROJECT_ROOT == Path(__file__).resolve().parents[2]


def test_default_data_root():
    """默认数据根 = PROJECT_ROOT/data。"""
    assert paths.data_root() == paths.PROJECT_ROOT / "data"


def test_set_data_root_override(tmp_path):
    """set_data_root 覆盖后 data_root/data_dir/agent_home 全部落在新根下。"""
    paths.set_data_root(tmp_path)
    assert paths.data_root() == tmp_path
    assert paths.data_dir("sessions", "a.json") == tmp_path / "sessions" / "a.json"
    assert paths.agent_home("wakers") == tmp_path / "home" / "wakers"


def test_data_dir_does_not_create_dirs(tmp_path):
    """拼接函数只算路径不建目录。"""
    paths.set_data_root(tmp_path)
    p = paths.data_dir("x", "y", "z.db")
    assert p == tmp_path / "x" / "y" / "z.db"
    assert not p.parent.exists()
    assert not p.exists()


def test_agent_home_equals_data_dir_home(tmp_path):
    """agent_home(*parts) ≡ data_dir("home", *parts)。"""
    paths.set_data_root(tmp_path)
    assert paths.agent_home("wakers", "alice") == paths.data_dir("home", "wakers", "alice")


def test_set_data_root_none_resets(tmp_path):
    """传 None 恢复默认根。"""
    paths.set_data_root(tmp_path)
    assert paths.data_root() == tmp_path
    paths.set_data_root(None)
    assert paths.data_root() == paths.PROJECT_ROOT / "data"
