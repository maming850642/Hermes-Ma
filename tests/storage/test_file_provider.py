"""
FileMemoryProvider 测试 —— 协议适配器与直接用 FileMemoryStore 等价。

适配器绑定 user_id（默认 LOCAL_USER），把无 user 维度的协议调用转发到
inner；用 tmp workspace_root 验证两边读写同一份 profile.md（T2b-② 拍平
后无 users/<uid> 段，user_id 仅归一数据字段）。另覆盖
read_legacy_profile 的坏行跳过 + replace_all 的 protect_outside 并发保护。

P3-5：文件后端已退役出 src（scripts/legacy_memory_backend.py）。本文件
改为对退役位置做回归保护——migrate_to_sqlite.py 仍依赖 read_legacy_profile
读旧 profile.md，防止迁移工具链腐化。测试语义不变。
"""
from __future__ import annotations

import json

from scripts.legacy_memory_backend import (
    FileMemoryProvider,
    FileMemoryStore,
    read_legacy_profile,
)
from src.constants import LOCAL_USER
from src.memory.models import Memory


def _provider(tmp_path, user_id: str = "alice") -> tuple[FileMemoryProvider, FileMemoryStore]:
    """provider + 指向同一 workspace 的直连 store（用于对比等价性）。"""
    direct = FileMemoryStore(workspace_root=str(tmp_path))
    provider = FileMemoryProvider(FileMemoryStore(workspace_root=str(tmp_path)), user_id=user_id)
    return provider, direct


# ============================================
# 转发等价性
# ============================================


def test_upsert_and_get_all_equivalent(tmp_path):
    provider, direct = _provider(tmp_path)
    m1 = Memory(user_id="alice", content="用户用 Python")
    m2 = Memory(user_id="alice", content="用户住北京")

    provider.upsert(m1)
    provider.upsert(m2)

    # provider 读 == 直连 store 按绑定 user 读
    assert provider.get_all() == direct.get_all("alice")


def test_search_equivalent(tmp_path):
    provider, direct = _provider(tmp_path)
    provider.upsert(Memory(user_id="alice", content="用户叫张三"))

    assert provider.search("张三") == direct.search("alice", "张三")
    assert provider.search_candidates("姓名") == direct.search_candidates("alice", "姓名")
    assert provider.search("", min_score=0.5) == direct.search("alice", "", min_score=0.5)


def test_get_by_id_and_delete_equivalent(tmp_path):
    provider, direct = _provider(tmp_path)
    m = Memory(user_id="alice", content="待删")
    provider.upsert(m)

    assert provider.get_by_id(m.id) == direct.get_by_id(m.id)
    assert provider.delete(m.id) is True
    assert direct.get_all("alice") == []
    assert provider.delete(m.id) is False


def test_delete_all_equivalent(tmp_path):
    provider, direct = _provider(tmp_path)
    provider.upsert(Memory(user_id="alice", content="a"))
    provider.upsert(Memory(user_id="alice", content="b"))
    assert provider.delete_all() is True
    assert direct.get_all("alice") == []


def test_replace_all_backups_and_restore_equivalent(tmp_path):
    provider, direct = _provider(tmp_path)
    m1 = Memory(user_id="alice", content="旧-1")
    m2 = Memory(user_id="alice", content="旧-2")
    provider.upsert(m1)
    provider.upsert(m2)

    label = provider.replace_all([Memory(user_id="alice", content="新")], backup=True)
    assert label is not None
    # 直连 store 视角：确实被替换
    assert [m.content for m in direct.get_all("alice")] == ["新"]
    # 协议契约：list_backups 是 [{name, mtime}] 字典列表，含刚生成的备份
    assert label in [b["name"] for b in provider.list_backups()]
    # 恢复往返
    assert provider.restore_backup(label) is True
    assert {m.content for m in direct.get_all("alice")} == {"旧-1", "旧-2"}


def test_default_user_is_local(tmp_path):
    """不传 user_id 时默认绑定 LOCAL_USER（写进 local 的 profile）。"""
    direct = FileMemoryStore(workspace_root=str(tmp_path))
    provider = FileMemoryProvider(FileMemoryStore(workspace_root=str(tmp_path)))
    provider.upsert(Memory(user_id=LOCAL_USER, content="默认用户"))

    assert direct.get_all(LOCAL_USER) == provider.get_all()
    assert provider.get_all()[0].user_id == LOCAL_USER


def test_upsert_normalizes_user_id(tmp_path):
    """调用方误传其他 user_id 也被归一为绑定值（数据字段一致）。

    T2b-② 拍平后路径无 user 段——归一保证 profile.md 里 user_id 字段
    恒为绑定值，不留脏数据。
    """
    direct = FileMemoryStore(workspace_root=str(tmp_path))
    provider = FileMemoryProvider(FileMemoryStore(workspace_root=str(tmp_path)), user_id="alice")

    provider.upsert(Memory(user_id="bob", content="越权写入"))

    mems = provider.get_all()
    assert len(mems) == 1
    assert all(m.user_id == "alice" for m in mems)  # 字段被归一
    assert direct.get_all("alice") == mems           # 同一份 profile.md


def test_replace_all_protect_outside_keeps_concurrent_writes(tmp_path):
    """protect_outside：替换时保留 id 不在集合内的并发写入（不丢写）。"""
    provider, direct = _provider(tmp_path)
    m1 = Memory(user_id="alice", content="原始-1")
    m2 = Memory(user_id="alice", content="原始-2")
    provider.upsert(m1)
    provider.upsert(m2)

    # 模拟聚合：protect_outside = 聚合前快照 id；LLM 期间新增了 m3
    m3 = Memory(user_id="alice", content="并发新增-3")
    direct.upsert(m3)

    provider.replace_all(
        [Memory(user_id="alice", content="整合-12", source="consolidated")],
        backup=True,
        protect_outside={m1.id, m2.id},
    )

    contents = {m.content for m in direct.get_all("alice")}
    assert contents == {"整合-12", "并发新增-3"}


def test_replace_all_without_protect_outside_drops_all(tmp_path):
    """protect_outside=None：纯全量替换（旧行为）。"""
    provider, direct = _provider(tmp_path)
    provider.upsert(Memory(user_id="alice", content="原始-1"))
    provider.upsert(Memory(user_id="alice", content="原始-2"))

    provider.replace_all([Memory(user_id="alice", content="新")], backup=True)

    assert [m.content for m in direct.get_all("alice")] == ["新"]


# ============================================
# read_legacy_profile
# ============================================


def test_read_legacy_profile_skips_bad_lines(tmp_path):
    p = tmp_path / "profile.md"
    good1 = {"id": "g1", "user_id": "alice", "content": "好行-1", "source": "legacy", "created_at": 1.0}
    good2 = {"id": "g2", "user_id": "alice", "content": "好行-2"}  # 缺省字段走默认
    lines = [
        json.dumps(good1, ensure_ascii=False),
        "",                       # 空行跳过
        "   ",                    # 空白行跳过
        "{broken json",           # 坏 JSON 跳过
        json.dumps({"id": "no-content-key"}),  # 缺必需键跳过
        json.dumps(good2, ensure_ascii=False),
    ]
    p.write_text("\n".join(lines), encoding="utf-8")

    mems = read_legacy_profile(p)
    assert [m.id for m in mems] == ["g1", "g2"]
    assert mems[0].content == "好行-1"
    assert mems[0].source == "legacy"
    assert mems[1].source == "legacy"  # 缺省默认
    assert mems[1].created_at == 0.0   # 缺省默认


def test_read_legacy_profile_missing_file(tmp_path):
    assert read_legacy_profile(tmp_path / "nope.md") == []
