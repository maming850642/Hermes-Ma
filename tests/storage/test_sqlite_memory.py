"""
SQLiteProvider 记忆协议测试 —— CRUD / 打分检索 / 聚合替换与备份恢复。

重点验证与 FileMemoryStore 的语义对齐（迁移后召回不变）：
  - upsert 更新保留原 created_at
  - 英文按词命中、CJK 逐字召回、min_score 过滤、空 query 全 0.5
  - replace_all + list_backups + restore_backup 往返
  - protect_outside 并发保护
所有用例显式传 tmp_path 下的 db 路径，不碰真实 data/。
"""
from __future__ import annotations

import pytest

from src.constants import LOCAL_USER
from src.memory.models import Memory
from src.storage.sqlite_provider import SQLiteProvider


@pytest.fixture()
def store(tmp_path):
    p = SQLiteProvider(db_path=tmp_path / "mem.db")
    yield p
    p.close()


def _mem(content: str, **kw) -> Memory:
    """造一条归属 LOCAL_USER 的记忆（id/created_at 等可覆盖）。"""
    return Memory(user_id=LOCAL_USER, content=content, **kw)


# ============================================
# 写入 + 读取
# ============================================


def test_upsert_new_and_get_all(store):
    store.upsert(_mem("用户用 Python"))
    store.upsert(_mem("用户住北京"))
    mems = store.get_all()
    assert len(mems) == 2
    assert {m.content for m in mems} == {"用户用 Python", "用户住北京"}
    # 读回的 user_id 恒为 LOCAL_USER（表无 user 列）
    assert all(m.user_id == LOCAL_USER for m in mems)


def test_upsert_update_preserves_created_at(store):
    """同 id 再 upsert 是更新：保留库中 created_at，content/updated_at 以新值为准。"""
    store.upsert(_mem("旧内容", id="fixed-id", created_at=1000.0))
    store.upsert(
        _mem("新内容", id="fixed-id", created_at=99999.0, updated_at=2000.0)
    )
    got = store.get_by_id("fixed-id")
    assert got is not None
    assert got.content == "新内容"
    assert got.created_at == 1000.0  # 库中原值保留，不被传入值覆盖
    assert got.updated_at == 2000.0
    assert len(store.get_all()) == 1


def test_get_by_id_missing_returns_none(store):
    assert store.get_by_id("no-such-id") is None


# ============================================
# 检索（打分语义与 file_store 对齐）
# ============================================


def test_search_keyword_hit(store):
    """FTS 通道：英文按词命中。"""
    store.upsert(_mem("user likes python"))
    store.upsert(_mem("user lives in beijing"))
    hits = store.search("python")
    assert len(hits) == 1
    assert hits[0].memory.content == "user likes python"
    assert 0 < hits[0].score <= 1.0


def test_search_multi_term_ranks_overlap_first(store):
    """OR 语义 + bm25：命中更多查询词的排前，部分命中的仍被召回。"""
    store.upsert(_mem("user likes python and docker"))
    store.upsert(_mem("user likes python only"))
    hits = store.search("python docker")
    assert [h.memory.content for h in hits][0] == "user likes python and docker"
    assert len(hits) == 2


def test_search_cjk_per_char_recall(store):
    """中文逐字：近义改写（"姓名是张三" vs "用户叫张三"）仍能召回。"""
    store.upsert(_mem("用户叫张三"))
    store.upsert(_mem("今天天气不错"))
    hits = store.search("姓名是张三")
    assert len(hits) == 1
    assert hits[0].memory.content == "用户叫张三"


def test_search_min_score_filters(store):
    """min_score 过滤作用于融合后分数（关键词权重 0.3 × 秩归一 × 新近度）。"""
    store.upsert(_mem("user likes python and docker"))
    store.upsert(_mem("user likes python only"))
    assert len(store.search("python docker")) == 2
    # 首秩 ≈ 0.3×1.0 = 0.30，次秩 ≈ 0.3×0.667 = 0.20（新写入 recency≈1）
    assert len(store.search("python docker", min_score=0.25)) == 1
    assert len(store.search("python docker", min_score=0.5)) == 0


def test_search_empty_query_weak_recall_by_recency(store):
    """空 query：双通道空转 → 弱召回按最近更新排序，分数压低示弱。"""
    import time as _t

    now = _t.time()
    store.upsert(_mem("旧记忆", created_at=now - 7200))
    store.upsert(_mem("新记忆", created_at=now - 60))
    hits = store.search("")
    assert [h.memory.content for h in hits] == ["新记忆", "旧记忆"]
    assert all(h.score < 0.5 for h in hits)


def test_search_candidates_no_filter(store):
    """候选检索（Decider 用）不过滤分数。"""
    store.upsert(_mem("用户用 Python"))
    hits = store.search_candidates("python docker")
    assert len(hits) == 1  # OR 语义：仅 python 词命中该条
    assert hits[0].memory.content == "用户用 Python"


# ============================================
# 删除
# ============================================


def test_delete(store):
    m = _mem("待删除")
    store.upsert(m)
    assert store.delete(m.id) is True
    assert store.get_by_id(m.id) is None
    assert store.delete(m.id) is False  # 再删不谎报成功


def test_delete_all(store):
    store.upsert(_mem("a"))
    store.upsert(_mem("b"))
    assert store.delete_all() is True
    assert store.get_all() == []


# ============================================
# 全量替换 + 备份/恢复
# ============================================


def test_replace_all_with_backup_and_restore_roundtrip(store):
    """replace_all 备份 → list_backups → restore_backup 完整往返。"""
    old = [_mem("旧-1", id=f"old-{i}") for i in range(3)]
    for m in old:
        store.upsert(m)

    new = [_mem("新-1", id="new-1"), _mem("新-2", id="new-2")]
    label = store.replace_all(new, backup=True)

    assert label is not None
    assert label.startswith("backup-")
    assert {m.id for m in store.get_all()} == {"new-1", "new-2"}

    backs = store.list_backups()
    assert [b["name"] for b in backs] and label in [b["name"] for b in backs]
    mtimes = [b["mtime"] for b in backs]
    assert mtimes == sorted(mtimes, reverse=True)  # 新→旧契约

    # 恢复 → 回到旧全量
    assert store.restore_backup(label) is True
    assert {m.id for m in store.get_all()} == {m.id for m in old}
    restored = {m.id: m for m in store.get_all()}
    assert restored["old-0"].content == "旧-1"

    # 恢复前当前内容被快照为 pre-restore-*
    assert any(b["name"].startswith("pre-restore-") for b in store.list_backups())

    # 未知 label → False
    assert store.restore_backup("backup-nonexistent") is False


def test_replace_all_no_backup_returns_none(store):
    """backup=False 或旧数据为空时不产生备份，返回 None。"""
    assert store.replace_all([_mem("a")], backup=False) is None
    # 现在旧数据非空，但 backup=False
    assert store.replace_all([_mem("b")], backup=False) is None
    assert store.list_backups() == []
    # 旧数据为空时即使 backup=True 也无备份可做
    store.delete_all()
    assert store.replace_all([_mem("c")], backup=True) is None


def test_replace_all_protect_outside(store):
    """protect_outside：old 中 id 不在集合内的行在 replace 后幸存。"""
    store.upsert(_mem("a-旧", id="a"))
    store.upsert(_mem("b-并发写入", id="b"))
    store.upsert(_mem("c-并发写入", id="c"))

    store.replace_all([_mem("a-新", id="a")], backup=False, protect_outside={"a"})

    mems = {m.id: m.content for m in store.get_all()}
    # a 被替换为集合内新内容；b/c 不在 protect 集合 → 幸存
    assert mems == {"a": "a-新", "b": "b-并发写入", "c": "c-并发写入"}


def test_replace_all_without_protect_replaces_everything(store):
    """protect_outside=None（默认）→ 纯全量替换。"""
    store.upsert(_mem("a", id="a"))
    store.upsert(_mem("b", id="b"))
    store.replace_all([_mem("c", id="c")], backup=False)
    assert {m.id for m in store.get_all()} == {"c"}


def _baseline_of(store) -> dict:
    return {
        m.id: (m.updated_at if m.updated_at is not None else m.created_at)
        for m in store.get_all()
    }


def test_replace_all_baseline_preserves_live_writes(store):
    """baseline 行级守卫：窗口内被 UPDATE 的行活值胜出、被 DELETE 的不复活。"""
    store.upsert(_mem("原始A", id="a", created_at=1000.0))
    store.upsert(_mem("原始B", id="b", created_at=1001.0))
    store.upsert(_mem("待删D", id="d", created_at=1002.0))
    baseline = _baseline_of(store)
    merged = [_mem("整合ABD", source="consolidated")]

    # 模拟"快照→replace_all"窗口内的并发写：改 a（版本号前移）、删 d、增 n
    store.upsert(_mem("A-已修订", id="a", created_at=1003.0, updated_at=9999.0))
    store.delete("d")
    store.upsert(_mem("新增N", id="n", created_at=2000.0))

    store.replace_all(merged, backup=False, baseline=baseline)

    mems = {m.id: m.content for m in store.get_all()}
    # a 活值保留（不被整合版覆盖）；b 被正常聚合掉；d 已删不复活；
    # n 不在基线 → 保留；整合条目入库
    assert mems == {"a": "A-已修订", "n": "新增N", merged[0].id: "整合ABD"}


def test_replace_all_none_baseline_is_pure_replace(store):
    """baseline=None：不设防（迁移脚本等调用方的全量覆盖语义）。"""
    store.upsert(_mem("原始A", id="a", created_at=1000.0))
    baseline = _baseline_of(store)
    store.upsert(_mem("A-已修订", id="a", updated_at=9999.0))

    store.replace_all([_mem("全新", source="consolidated")], backup=False)

    assert baseline  # 快照锚点算过但未传入
    assert [m.content for m in store.get_all()] == ["全新"]


def test_read_back_user_id_is_local(store):
    """单用户架构：所有读回记忆归属 LOCAL_USER。"""
    store.upsert(_mem("x"))
    got = store.get_all()[0]
    assert got.user_id == LOCAL_USER
