"""
T5 path_guard 单元测试：resolve_under_root 的边界判定。

覆盖：
- 相对路径相对 root 解析（可不存在——写文件场景）
- 绝对路径在 root 内 ok / root 外拒
- ../ 穿越（解析后越界）拒
- root 本身 ok（target == root）
- symlink 逃逸：root 内符号链接指向外部 → resolve 后越界拦截
  （无权限创建 symlink 的环境 skipif 跳过）
"""
from pathlib import Path

import pytest

import src.agent  # noqa: F401  先完整初始化 agent 包（src.tools ↔ src.agent 有 import 环）
from src.tools.path_guard import PathEscapeError, resolve_under_root


@pytest.fixture
def root(tmp_path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    (r / "sub").mkdir()
    (r / "sub" / "a.txt").write_text("a", encoding="utf-8")
    return r


# ============================================
# 相对路径
# ============================================


def test_relative_ok(root):
    p = resolve_under_root(root, "sub/a.txt")
    assert p == (root / "sub" / "a.txt").resolve()


def test_relative_nonexistent_target_ok(root):
    """目标可以不存在（写文件的父目录场景）。"""
    p = resolve_under_root(root, "new/dir/b.txt")
    assert p == (root / "new" / "dir" / "b.txt").resolve()
    assert not p.exists()


def test_relative_dotdot_resolved_inside_ok(root):
    """../ 组合但解析后仍在 root 内（sub/../x）→ 放行。"""
    (root / "x.txt").write_text("x", encoding="utf-8")
    p = resolve_under_root(root, "sub/../x.txt")
    assert p == (root / "x.txt").resolve()


def test_relative_dotdot_escape_rejected(root):
    with pytest.raises(PathEscapeError):
        resolve_under_root(root, "../outside.txt")


def test_deep_dotdot_escape_rejected(root):
    with pytest.raises(PathEscapeError):
        resolve_under_root(root, "sub/../../../../etc/passwd")


# ============================================
# 绝对路径
# ============================================


def test_absolute_inside_ok(root):
    p = resolve_under_root(root, root / "sub" / "a.txt")
    assert p == (root / "sub" / "a.txt").resolve()


def test_absolute_root_itself_ok(root):
    """target == root 放行（ls / 场景）。"""
    assert resolve_under_root(root, root) == root.resolve()


def test_absolute_outside_rejected(root, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(PathEscapeError):
        resolve_under_root(root, outside)


def test_absolute_parent_rejected(root):
    with pytest.raises(PathEscapeError):
        resolve_under_root(root, root.parent)


def test_error_message_chinese(root):
    with pytest.raises(PathEscapeError, match="路径越界"):
        resolve_under_root(root, "../outside.txt")


# ============================================
# symlink 逃逸
# ============================================


def _can_symlink(root: Path) -> bool:
    try:
        (root / "_probe_link").symlink_to(root.parent)
        (root / "_probe_link").unlink()
        return True
    except (OSError, NotImplementedError):
        return False


def test_symlink_escape_rejected(root, tmp_path):
    """root 内符号链接指向外部 → resolve 解开链接后越界，拦截。"""
    if not _can_symlink(root):
        pytest.skip("当前环境无 symlink 权限（Windows 非开发者模式）")
    secret = tmp_path / "secret.txt"
    secret.write_text("s", encoding="utf-8")
    (root / "link").symlink_to(secret)
    with pytest.raises(PathEscapeError):
        resolve_under_root(root, "link")


def test_symlink_inside_ok(root, tmp_path):
    """root 内符号链接指向 root 内 → 放行。"""
    if not _can_symlink(root):
        pytest.skip("当前环境无 symlink 权限（Windows 非开发者模式）")
    (root / "alias").symlink_to(root / "sub")
    p = resolve_under_root(root, "alias/a.txt")
    assert p == (root / "sub" / "a.txt").resolve()
