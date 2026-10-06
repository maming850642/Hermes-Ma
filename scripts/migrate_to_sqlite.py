"""
migrate_to_sqlite —— 旧 users/<uid>/ 布局 → 单用户新布局迁移脚本（T2b-②）。

旧语义（config.workspace_root 多用户数据区）废弃。本脚本把：

  <old_root>/users/<uid>/
    wakers/ wakerflows/ projects/ memory_config.yaml 及其他杂项
        → paths.agent_home()/（= data/home/，同名已存在则跳过并警告）
    profile.md（jsonl）
        → SQLiteProvider（data/hermes.db memories 表）逐条 upsert
          （保留 id/created_at），源文件改名 profile.md.migrated（不删）
    memory_config.yaml（迁到 home 后）
        → kv（scope="memory", key="config"），成功后改名 .migrated

用法：
    .venv/Scripts/python.exe scripts/migrate_to_sqlite.py --dry-run   # 只打印计划
    .venv/Scripts/python.exe scripts/migrate_to_sqlite.py             # 打印计划并执行
    .venv/Scripts/python.exe scripts/migrate_to_sqlite.py --user u1   # 多用户目录时强制指定

特性：幂等可重跑（users/ 已迁走 → 提示无需迁移；kv 已有 config → 跳过导入）。
不自动执行：由用户手动运行。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# 项目根入 sys.path（脚本可独立运行）；scripts 目录入 sys.path（取退役的
# 旧文件后端模块——P3-5 已迁出 src，见 scripts/legacy_memory_backend.py）
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (_PROJECT_ROOT, _SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from legacy_memory_backend import read_legacy_profile  # noqa: E402
from src.memory import config_store  # noqa: E402
from src.storage import paths  # noqa: E402
from src.storage.sqlite_provider import SQLiteProvider  # noqa: E402

# ════════════════════════════════════════════════════════════════
# 旧根定位
# ════════════════════════════════════════════════════════════════

def _default_old_root() -> Path:
    """旧根：settings.workspace_root（遗留值）或 PROJECT_ROOT/data/memories。"""
    try:
        from config import PROJECT_ROOT, get_settings
        ws = getattr(get_settings(), "workspace_root", "") or ""
        if ws.strip():
            return Path(ws.strip())
        return Path(PROJECT_ROOT) / "data" / "memories"
    except Exception:
        return _PROJECT_ROOT / "data" / "memories"


def _find_user_dirs(old_root: Path) -> list[Path]:
    """old_root 下 users/*/ 目录（字母序）。不存在返回 []。"""
    users_dir = old_root / "users"
    if not users_dir.is_dir():
        return []
    return sorted(d for d in users_dir.iterdir() if d.is_dir())


def _unique_migrated(path: Path) -> Path:
    """生成不冲突的 .migrated 改名目标（同名追加 -2/-3）。"""
    candidate = path.with_name(path.name + ".migrated")
    suffix = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.migrated-{suffix}")
        suffix += 1
    return candidate


# ════════════════════════════════════════════════════════════════
# 迁移步骤
# ════════════════════════════════════════════════════════════════

def _plan_home_moves(user_dir: Path, home: Path) -> list[tuple[Path, Path, bool]]:
    """枚举 users/<uid>/ 下可搬移的条目 → (src, dst, dst已存在)。

    - profile.md 由 SQLite 导入步骤单独处理（改名 .migrated，不移动）
    - *.migrated 残留（上次运行留下的源文件备份）不再搬移，原样保留
    """
    moves: list[tuple[Path, Path, bool]] = []
    if not user_dir.is_dir():
        return moves
    for entry in sorted(user_dir.iterdir()):
        if entry.name == "profile.md" or entry.name.endswith(".migrated"):
            continue
        dst = home / entry.name
        moves.append((entry, dst, dst.exists()))
    return moves


def _migrate_profile(profile: Path, db: SQLiteProvider, dry_run: bool) -> tuple[int, str | None]:
    """profile.md → SQLite 逐条 upsert。返回 (导入条数, 改名后路径 or None)。"""
    if not profile.exists():
        return 0, None
    mems = read_legacy_profile(profile)
    if not dry_run:
        for m in mems:
            db.upsert(m)
    renamed: str | None = None
    if not dry_run:
        target = _unique_migrated(profile)
        profile.rename(target)
        renamed = str(target)
    return len(mems), renamed


def _migrate_config(home: Path, db: SQLiteProvider, dry_run: bool) -> str:
    """home/memory_config.yaml → kv。返回状态描述。"""
    yaml_path = home / "memory_config.yaml"
    if not yaml_path.exists():
        return "无旧 yaml（跳过）"
    if db.kv_get(config_store._SCOPE, config_store._KEY) is not None:
        if not dry_run:
            yaml_path.rename(_unique_migrated(yaml_path))
        return "kv 已有配置（跳过导入，改名 .migrated）"
    imported = config_store._parse_legacy_yaml(yaml_path)
    if imported is None:
        return "yaml 解析失败（保留原文件，请手工处理）"
    if not dry_run:
        db.kv_put(config_store._SCOPE, config_store._KEY, config_store._normalize(imported))
        yaml_path.rename(_unique_migrated(yaml_path))
    return f"导入 kv（auto_consolidate={imported.get('auto_consolidate')}），改名 .migrated"


# ════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════

def run(old_root: Path | None = None, user: str = "", dry_run: bool = False,
        data_root: Path | None = None) -> int:
    """执行迁移。返回进程退出码（0 成功/无需迁移，2 错误）。"""
    if data_root is not None:
        paths.set_data_root(data_root)

    old_root = old_root or _default_old_root()
    home = paths.agent_home()
    db_path = paths.data_dir("hermes.db")

    print(f"旧根（users/ 所在）: {old_root}")
    print(f"agent home（迁移目标）: {home}")
    print(f"SQLite 库: {db_path}")
    if dry_run:
        print("*** DRY RUN：只打印计划，不执行 ***")

    # 1) 定位 users/<uid>/
    user_dirs = _find_user_dirs(old_root)
    if not user_dirs:
        print(f"未发现 {old_root / 'users'} 下的用户目录 → 无需迁移，退出。")
        return 0
    if len(user_dirs) > 1 and not user:
        print(f"错误：{old_root / 'users'} 下有 {len(user_dirs)} 个用户目录，须用 --user 指定一个：")
        for d in user_dirs:
            print(f"  - {d.name}")
        return 2
    if user:
        picked = old_root / "users" / user
        if not picked.is_dir():
            print(f"错误：指定的用户目录不存在: {picked}")
            return 2
        user_dir = picked
    else:
        user_dir = user_dirs[0]
    print(f"迁移用户: {user_dir.name}")

    # 2) 计划：目录条目搬入 home
    moves = _plan_home_moves(user_dir, home)
    profile = user_dir / "profile.md"

    # 幂等：只剩 .migrated 残留（已迁过）且无 profile.md → 无需迁移
    if not moves and not profile.exists():
        print(f"{user_dir} 下无可迁移内容（只剩上次运行的 .migrated 备份）→ 无需迁移，退出。")
        return 0

    print("\n--- 迁移计划 ---")
    if moves:
        for src, dst, exists in moves:
            tag = "  [跳过：目标已存在]" if exists else ""
            print(f"  move {src} -> {dst}{tag}")
    else:
        print("  （users/<uid>/ 下无目录条目需搬移）")
    if profile.exists():
        n = len(read_legacy_profile(profile))
        print(f"  profile.md -> SQLite upsert {n} 条（源文件改名 .migrated）")
    else:
        print("  （无 profile.md，跳过记忆导入）")
    if any(src.name == "memory_config.yaml" for src, _, _ in moves) or \
            (home / "memory_config.yaml").exists():
        print("  memory_config.yaml -> kv（scope=memory, key=config）")

    if dry_run:
        print("\nDRY RUN 结束（未做任何改动）。")
        return 0

    db = SQLiteProvider()  # 建库/建表（幂等）

    # 执行：步骤 2 —— 搬移目录条目
    moved, skipped = 0, 0
    for src, dst, exists in moves:
        if exists:
            print(f"  [警告] 跳过（目标已存在）: {dst}")
            skipped += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        moved += 1

    # 步骤 3 —— profile.md 入库
    n_mems, renamed = _migrate_profile(profile, db, dry_run=False)

    # 步骤 4 —— memory_config 入 kv
    cfg_status = _migrate_config(home, db, dry_run=False)

    # 汇总
    print("\n--- 迁移汇总 ---")
    print(f"  目录条目搬移: {moved} 个（跳过 {skipped} 个）")
    print(f"  记忆入库: {n_mems} 条（SQLite {db_path}）")
    if renamed:
        print(f"  profile.md 备份: {renamed}")
    print(f"  聚合配置: {cfg_status}")
    print("迁移完成。可重跑校验（应提示无需迁移）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="旧 users/<uid>/ 布局 → 单用户（agent home + SQLite）迁移脚本"
    )
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不执行")
    p.add_argument("--user", default="", help="users/ 下有多个用户目录时强制指定 <uid>")
    p.add_argument("--old-root", default="", help="覆盖旧根（默认 settings.workspace_root 或 data/memories）")
    p.add_argument("--data-root", default="", help="覆盖数据根（默认 PROJECT_ROOT/data；测试用）")
    args = p.parse_args(argv)
    return run(
        old_root=Path(args.old_root) if args.old_root else None,
        user=args.user,
        dry_run=args.dry_run,
        data_root=Path(args.data_root) if args.data_root else None,
    )


if __name__ == "__main__":
    sys.exit(main())
