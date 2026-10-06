"""
scripts/migrate_to_sqlite.py 测试 —— 旧 users/<uid>/ 布局 → 单用户新布局。

用 tmp 伪造旧结构（users/u1/{wakers/x/waker.yaml, profile.md 两行,
memory_config.yaml, projects/p/}），set_data_root 指向 tmp 作新数据根，
跑迁移后断言：
  - home/ 布局（wakers/ projects/ 就位，无 users/ 残留）
  - db（tmp/data/home 同根下 hermes.db）memories 行数 = profile 行数，
    id/created_at 保留
  - memory_config 入 kv（scope=memory, key=config），原 yaml 改名 .migrated
  - profile.md 改名 .migrated（备份不删）
  - 幂等：重跑提示无需迁移

CLI 入口（--dry-run 无旧数据优雅退出）另见 run() 直调用例。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.memory import config_store
from src.storage import paths
from src.storage.sqlite_provider import SQLiteProvider


@pytest.fixture(autouse=True)
def _isolated_roots(tmp_path):
    """旧根 tmp/old，新数据根 tmp/new（home=tmp/new/data/home）。"""
    paths.set_data_root(tmp_path / "new" / "data")
    yield tmp_path
    paths.set_data_root(None)


def _seed_old_layout(old_root: Path, uid: str = "u1") -> dict[str, Path]:
    """伪造旧 users/<uid>/ 数据。返回关键路径 dict。"""
    user_dir = old_root / "users" / uid
    (user_dir / "wakers" / "x").mkdir(parents=True)
    (user_dir / "wakers" / "x" / "waker.yaml").write_text(
        "config:\n  name: x\n  enabled: true\n  schedule_type: none\n", encoding="utf-8"
    )
    (user_dir / "projects" / "p").mkdir(parents=True)
    (user_dir / "projects" / "p" / "META.md").write_text("# p\n", encoding="utf-8")
    (user_dir / "memory_config.yaml").write_text(
        "auto_consolidate: true\ninterval_hours: 6\nthreshold: 5\n", encoding="utf-8"
    )
    profile = user_dir / "profile.md"
    profile.write_text(
        "\n".join([
            json.dumps({"id": "m1", "user_id": uid, "content": "记忆一", "source": "legacy",
                        "created_at": 100.0, "updated_at": 100.5}, ensure_ascii=False),
            json.dumps({"id": "m2", "user_id": uid, "content": "记忆二", "source": "tool:remember",
                        "created_at": 200.0}, ensure_ascii=False),
        ]) + "\n",
        encoding="utf-8",
    )
    return {"user_dir": user_dir, "profile": profile}


def _run(old_root: Path, *args: str) -> int:
    from scripts.migrate_to_sqlite import main
    return main(["--old-root", str(old_root), *args])


# ============================================
# 无旧数据：优雅退出
# ============================================


def test_no_old_data_exits_gracefully(tmp_path, capsys):
    rc = _run(tmp_path / "not_exist")
    assert rc == 0
    assert "无需迁移" in capsys.readouterr().out


def test_dry_run_makes_no_changes(tmp_path, capsys):
    _seed_old_layout(tmp_path)
    rc = _run(tmp_path, "--dry-run")
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    # 无任何改动：home 不存在，users/ 原样
    assert not paths.agent_home().exists()
    assert (tmp_path / "users" / "u1" / "profile.md").exists()


# ============================================
# 多用户目录：须 --user
# ============================================


def test_multiple_users_require_user_flag(tmp_path, capsys):
    _seed_old_layout(tmp_path, "u1")
    _seed_old_layout(tmp_path, "u2")
    rc = _run(tmp_path)
    assert rc == 2
    out = capsys.readouterr().out
    assert "--user" in out
    assert "u1" in out and "u2" in out


def test_user_flag_picks_directory(tmp_path):
    _seed_old_layout(tmp_path, "u1")
    _seed_old_layout(tmp_path, "u2")
    rc = _run(tmp_path, "--user", "u1")
    assert rc == 0
    home = paths.agent_home()
    assert (home / "wakers" / "x" / "waker.yaml").exists()
    # u2 原样未动
    assert (tmp_path / "users" / "u2" / "profile.md").exists()


# ============================================
# 真实迁移一轮
# ============================================


def test_full_migration_round(tmp_path):
    seeded = _seed_old_layout(tmp_path)
    rc = _run(tmp_path)
    assert rc == 0

    home = paths.agent_home()

    # 1) home 布局：wakers/ projects/ memory_config.yaml 就位，无 users/ 段
    assert (home / "wakers" / "x" / "waker.yaml").exists()
    assert (home / "projects" / "p" / "META.md").exists()
    assert (home / "memory_config.yaml.migrated").exists()
    assert not (home / "memory_config.yaml").exists()

    # 2) profile.md 两行 → db 两行，id/created_at 保留
    db = SQLiteProvider()
    mems = db.get_all()
    assert len(mems) == 2
    by_id = {m.id: m for m in mems}
    assert by_id["m1"].content == "记忆一"
    assert by_id["m1"].created_at == 100.0
    assert by_id["m2"].source == "tool:remember"
    assert by_id["m2"].created_at == 200.0
    # 源文件改名备份（不删）
    assert not seeded["profile"].exists()
    assert seeded["profile"].with_name("profile.md.migrated").exists()

    # 3) memory_config 内容在 kv
    cfg = config_store.load()
    assert cfg["auto_consolidate"] is True
    assert cfg["interval_hours"] == 6
    assert cfg["threshold"] == 5


def test_migration_idempotent_rerun(tmp_path, capsys):
    _seed_old_layout(tmp_path)
    assert _run(tmp_path) == 0
    # 重跑：users/u1 已空（或 users/ 已无目录）→ 无需迁移
    rc = _run(tmp_path)
    assert rc == 0
    assert "无需迁移" in capsys.readouterr().out


def test_migration_skips_existing_home_entries(tmp_path, capsys):
    _seed_old_layout(tmp_path)
    home = paths.agent_home()
    (home / "projects").mkdir(parents=True)  # 已存在同名 → 跳过并警告
    rc = _run(tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "跳过" in out
    # 旧 projects 不覆盖 home 的（仍空目录），其余照迁
    assert not (home / "projects" / "p").exists()
    assert (home / "wakers" / "x" / "waker.yaml").exists()
