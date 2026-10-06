"""检索打分测试：纯向量主通道（sqlite-vec）+ FTS 关键词降级通道。

打分与排序只来自向量通道（产品决策 2026-09：关键词不再参与融合）；
FTS5 仅在向量通道整体不可用时兜底，detail.source="keyword"。

FakeEmbedder 提供确定性向量（词表独热 → 单位化），测试永不下载真模型。
真实 fastembed 冒烟用 HERMES_EMBEDDING_SMOKE=1 门控，默认 skip。
"""
from __future__ import annotations

import os

import pytest

from src.constants import LOCAL_USER
from src.memory.models import Memory
from src.storage.sqlite_provider import SQLiteProvider


class FakeEmbedder:
    """确定性向量：词表独热 → L2 单位化。内容共享词表词 ⇒ 向量相近。"""

    _VOCAB = ("python", "火锅", "北京", "足球", "音乐", "工作", "旅行", "天气")

    def __init__(self, dim: int = 8, fail: bool = False) -> None:
        self._dim = dim
        self._fail = fail

    def model_tag(self) -> str:
        return "fake"

    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._fail:
            raise RuntimeError("fake embedder down")
        out = []
        for t in texts:
            v = [1.0 if w in t else 0.0 for w in self._VOCAB][: self._dim]
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in v])
        return out


def _mem(content: str, **kw) -> Memory:
    return Memory(user_id=LOCAL_USER, content=content, **kw)


@pytest.fixture()
def store(tmp_path):
    p = SQLiteProvider(db_path=tmp_path / "mem.db", embedder=FakeEmbedder())
    yield p
    p.close()


def _vec_count(store) -> int:
    return store._conn.execute("SELECT COUNT(*) AS c FROM memory_vecs").fetchone()["c"]


def _fts_count(store) -> int:
    return store._conn.execute("SELECT COUNT(*) AS c FROM mem_fts").fetchone()["c"]


# ============================================
# 向量通道
# ============================================


def test_vec_channel_ranks_semantic_match_first(store):
    """近似措辞（共享词表词）经向量通道排序靠前。"""
    store.upsert(_mem("用户喜欢火锅"))
    store.upsert(_mem("用户关注北京天气"))
    hits = store.search("今天想吃火锅")
    # "火锅"向量维度直接命中（cos=1）；另一条正交（低于地板归零）不出局也谈不上排序
    assert hits[0].memory.content == "用户喜欢火锅"
    assert hits[0].detail["source"] == "vector"


def test_attach_embedder_enables_semantic_channel(tmp_path):
    """无 embedder 构造的共享实例补挂后，语义通道对存量 keyword-only 记忆生效。"""
    p = SQLiteProvider(db_path=tmp_path / "mem.db")
    try:
        p.upsert(_mem("用户喜欢火锅"))
        assert not p._has_vec_table  # 构造时无向量通道，纯 keyword-only
        p.attach_embedder(FakeEmbedder())
        assert p._has_vec_table
        assert p.backfill_embeddings() == 1  # 存量记忆补嵌
        hits = p.search("今天想吃火锅")
        assert hits and hits[0].detail["vec"] > 0.0
    finally:
        p.close()


def test_attach_embedder_is_noop_when_present(tmp_path):
    """已有 embedder 时 attach 幂等：不换实例、不重建表、不清已有向量。"""
    p = SQLiteProvider(db_path=tmp_path / "mem.db", embedder=FakeEmbedder())
    try:
        p.upsert(_mem("用户喜欢火锅"))
        emb = p._embedder
        p.attach_embedder(FakeEmbedder())
        assert p._embedder is emb
        assert _vec_count(p) == 1
    finally:
        p.close()


def test_upsert_writes_both_indexes(store):
    store.upsert(_mem("用户喜欢火锅"))
    assert _vec_count(store) == 1
    assert _fts_count(store) == 1


def test_dim_mismatch_rebuilds_table(tmp_path):
    """同库换模型（维度变化）→ 向量表重建、meta 更新（数据靠回填重建）。"""
    db = tmp_path / "mem.db"
    p1 = SQLiteProvider(db_path=db, embedder=FakeEmbedder(dim=8))
    p1.upsert(_mem("用户喜欢火锅"))
    p1.close()

    p2 = SQLiteProvider(db_path=db, embedder=FakeEmbedder(dim=4))
    assert _vec_count(p2) == 0  # 旧维度向量作废
    row = p2._conn.execute(
        "SELECT value FROM meta WHERE key='memory_vec_dim'"
    ).fetchone()
    assert row["value"] == "4"
    p2.close()


# ============================================
# 写同步一致性（四个路径）
# ============================================


def test_delete_cascades_indexes(store):
    m = _mem("用户喜欢火锅")
    store.upsert(m)
    store.delete(m.id)
    assert _vec_count(store) == 0
    assert _fts_count(store) == 0


def test_delete_all_cascades_indexes(store):
    store.upsert(_mem("a 北京"))
    store.upsert(_mem("b 足球"))
    store.delete_all()
    assert _vec_count(store) == 0
    assert _fts_count(store) == 0


def test_replace_all_rebuilds_indexes(store):
    store.upsert(_mem("旧一 北京"))
    store.upsert(_mem("旧二 足球"))
    new = [_mem("整合 北京 足球", source="consolidated")]
    store.replace_all(new, backup=False)
    assert len(store.get_all()) == 1
    assert _vec_count(store) == 1
    assert _fts_count(store) == 1


def test_restore_rebuilds_indexes(tmp_path):
    db = tmp_path / "mem.db"
    p = SQLiteProvider(db_path=db, embedder=FakeEmbedder())
    p.upsert(_mem("原始 北京"))
    label = p.replace_all([_mem("整合 足球", source="consolidated")], backup=True)
    assert p.restore_backup(label) is True
    assert {m.content for m in p.get_all()} == {"原始 北京"}
    assert _vec_count(p) == 1
    assert _fts_count(p) == 1
    p.close()


# ============================================
# 降级
# ============================================


def test_embedder_failure_degrades_to_keyword(tmp_path):
    """embedding 抛异常：写入不受阻（keyword-only）、检索降级 FTS 并标 source。"""
    p = SQLiteProvider(db_path=tmp_path / "mem.db", embedder=FakeEmbedder(fail=True))
    p.upsert(_mem("user likes python"))  # 不抛
    assert _vec_count(p) == 0
    assert _fts_count(p) == 1
    hits = p.search("python")  # 不抛，FTS 通道兜底
    assert len(hits) == 1
    assert hits[0].detail["source"] == "keyword"
    assert hits[0].detail["fts"] > 0.0
    p.close()


def test_keyword_fallback_warns_once(tmp_path, caplog):
    """向量通道不可用时检索降级：source=keyword，且降级 warn 每实例只发一次。"""
    import logging

    p = SQLiteProvider(db_path=tmp_path / "mem.db", embedder=FakeEmbedder(fail=True))
    try:
        p.upsert(_mem("user likes python"))
        with caplog.at_level(logging.WARNING, logger="hermes.storage.sqlite"):
            hits1 = p.search("python")
            hits2 = p.search("python")
        assert len(hits1) == 1 and len(hits2) == 1
        assert hits1[0].detail["source"] == "keyword"
        fallback_warnings = [r for r in caplog.records if "降级为关键词" in r.message]
        assert len(fallback_warnings) == 1
    finally:
        p.close()


# ============================================
# 文档侧结构性降级（keyword-only 库）
# ============================================


def test_keyword_only_library_falls_back_when_query_vec_available(tmp_path, caplog):
    """keyword-only 库 + 查询向量可用 → 检索不得恒空。

    场景：写入期无 embedder（FTS 有行、memory_vecs 未建），之后带 embedder
    打开（vec 表建好但 0 行）。查询侧 embed 正常、KNN 注定空转——此前判据
    只看查询侧（qv is None），s_vec 全零 → 诚实空集，记忆"检索死亡"。
    修后：memory_vecs 总行数==0 是结构性信号（embed 从未成功过），降级
    FTS 关键词（source=keyword）并按 _kw_fallback_warned 告警一次。
    """
    import logging

    db = tmp_path / "kwonly.db"
    p1 = SQLiteProvider(db_path=db)  # 无 embedder 写入
    try:
        p1.upsert(_mem("user likes python"))
        assert _fts_count(p1) == 1
        assert not p1._has_vec_table
    finally:
        p1.close()

    p2 = SQLiteProvider(db_path=db, embedder=FakeEmbedder())
    try:
        assert p2._has_vec_table
        assert _vec_count(p2) == 0  # 结构性信号：库内一行向量都没有
        with caplog.at_level(logging.WARNING, logger="hermes.storage.sqlite"):
            hits = p2.search("python")
            p2.search("python")
        assert [h.memory.content for h in hits] == ["user likes python"]
        assert hits[0].detail["source"] == "keyword"
        warnings = [r for r in caplog.records if "keyword-only" in r.message]
        assert len(warnings) == 1  # 复用 _kw_fallback_warned：每实例一次
    finally:
        p2.close()


def test_true_zero_relevance_stays_empty_not_keyword(tmp_path):
    """区分真·零相关：memory_vecs 有行（文档侧覆盖存在）但查询与全部内容
    余弦低于地板 → 诚实返回 []，不得因"本次 0 命中"降级关键词。

    判据必须是结构性信号（表空），不是本次召回为空——否则弱相关查询
    （词面 FTS 命中、语义不像）会绕过纯向量产品决策被关键词捞回。
    """
    p = SQLiteProvider(db_path=tmp_path / "zero.db", embedder=FakeEmbedder())
    try:
        # 与查询"北京火锅"仅共享火锅一维、自身五维 → cos≈0.316 低于地板
        p.upsert(_mem("火锅 音乐 天气 足球 旅行"))
        assert _vec_count(p) == 1  # 覆盖存在
        assert p.search("北京火锅") == []
    finally:
        p.close()


# ============================================
# 内容级去重（重复记忆不挤占 limit）
# ============================================


def test_duplicate_content_deduped(store):
    """content 完全相同的两条记忆（聚合残留/迁移双写）只返回一条。

    主审实测：重复记忆双条并列挤占 limit——去重发生在 limit 截断之前。
    构造：dup-a/dup-b 与查询同两维（cos=1），other 三维共享两维
    （cos≈0.816，过相对门槛）——去重后 [dup, other] 共两条。
    """
    store.upsert(_mem("用户喜欢火锅也关注北京", id="dup-a"))
    store.upsert(_mem("用户喜欢火锅也关注北京", id="dup-b"))
    store.upsert(_mem("用户关注北京天气迷上火锅", id="other"))
    hits = store.search("火锅 北京")
    got = [h.memory.content for h in hits]
    assert got.count("用户喜欢火锅也关注北京") == 1
    assert got == ["用户喜欢火锅也关注北京", "用户关注北京天气迷上火锅"]
    # limit=1 时重复副本不挤占名额：返回去重后的最高分一条
    assert [h.memory.id for h in store.search("火锅 北京", limit=1)] == [hits[0].memory.id]


def test_duplicate_content_keeps_higher_score(store):
    """同内容多条时保留得分高（并列取新）的那条。"""
    import time as _time

    month_ago = _time.time() - 30 * 86400
    store.upsert(_mem("用户喜欢火锅", id="dup-old",
                      created_at=month_ago, updated_at=month_ago))
    store.upsert(_mem("用户喜欢火锅", id="dup-new"))
    hits = store.search("火锅")
    assert [h.memory.id for h in hits] == ["dup-new"]


def test_backfill_fills_missing_then_idempotent(tmp_path):
    db = tmp_path / "mem.db"
    broken = SQLiteProvider(db_path=db, embedder=FakeEmbedder(fail=True))
    broken.upsert(_mem("用户喜欢火锅"))
    broken.upsert(_mem("用户关注北京天气"))
    assert _vec_count(broken) == 0
    broken.close()

    # 换上可用的 embedder（模拟预热线程/回填脚本）
    fixed = SQLiteProvider(db_path=db, embedder=FakeEmbedder())
    n1 = fixed.backfill_embeddings()
    assert n1 == 2
    assert _vec_count(fixed) == 2
    assert fixed.backfill_embeddings() == 0  # 幂等
    fixed.close()


# ============================================
# P1-6：UPDATE 同 id 二次 upsert 的向量一致性
# ============================================


def test_upsert_same_id_recomputes_vector(store):
    """同 id 二次 upsert（content 变化）后向量存在且对应新内容。

    回归：UPDATE 路径此前"旧行仍在 → 不重算"，随后 _sync_aux_rows 无条件
    删向量且不回插——insert 后 vec=1、update 后 vec=0，语义检索静默退化为
    keyword-only（manager.py 的 Decider UPDATE 与 web edit_memory 都走这里）。
    """
    m = _mem("用户喜欢火锅")
    store.upsert(m)
    assert _vec_count(store) == 1

    # 同 id 改内容（Decider UPDATE / edit_memory 的形状）
    store.upsert(Memory(
        id=m.id, user_id=LOCAL_USER, content="用户迷上足球",
        source="edit", created_at=m.created_at, updated_at=m.created_at + 1,
    ))
    # 向量不被删了不补：仍有且只有一行
    assert _vec_count(store) == 1
    # 新内容参与语义检索（vec 分量 > 0）
    hits = store.search("想踢足球")
    assert hits and hits[0].memory.id == m.id
    assert hits[0].detail["vec"] > 0.0


def test_upsert_same_content_keeps_existing_vector(tmp_path):
    """同 id 同 content 的 upsert 不触发重算（内容版本未变，向量仍有效）。"""

    class CountingEmbedder(FakeEmbedder):
        calls = 0

        def embed(self, texts):
            CountingEmbedder.calls += 1
            return super().embed(texts)

    store2 = SQLiteProvider(db_path=tmp_path / "cnt.db", embedder=CountingEmbedder())
    try:
        m = _mem("用户喜欢火锅")
        store2.upsert(m)
        first = CountingEmbedder.calls
        assert first == 1
        store2.upsert(Memory(
            id=m.id, user_id=LOCAL_USER, content="用户喜欢火锅",
            source="touch", created_at=m.created_at, updated_at=m.created_at + 1,
        ))
        assert CountingEmbedder.calls == first  # 内容未变：不重算
        assert _vec_count(store2) == 1
    finally:
        store2.close()


# ============================================
# P2 回归：embed 失败时内容有变的行不得留下过期旧向量
# ============================================


class _ToggleEmbedder(FakeEmbedder):
    """fail 可运行时切换的 FakeEmbedder（模拟 embed 服务时好时坏）。"""

    def __init__(self, dim: int = 8):
        super().__init__(dim=dim)
        self.fail = False

    def embed(self, texts):
        if self.fail:
            raise RuntimeError("fake embedder down")
        return super().embed(texts)


def test_upsert_embed_failure_deletes_stale_vector_then_backfill_heals(tmp_path):
    """upsert 改内容 + embed 失败 → 旧向量必须被删（可自愈），回填后与新内容一致。

    回归：_sync_aux_rows 只在"算出新向量"时才 DELETE+INSERT——embed 失败时
    force 行的旧向量既不删也不换，memories.content 是新内容而 memory_vecs
    挂旧内容向量，且 backfill_embeddings 只补"行不存在"，错误向量永不自愈。
    """
    import struct

    emb = _ToggleEmbedder()
    p = SQLiteProvider(db_path=tmp_path / "stale.db", embedder=emb)
    try:
        m = _mem("用户喜欢火锅")
        p.upsert(m)
        assert _vec_count(p) == 1

        # embed 服务下线期间改内容
        emb.fail = True
        p.upsert(Memory(
            id=m.id, user_id=LOCAL_USER, content="用户迷上足球",
            source="edit", created_at=m.created_at, updated_at=m.created_at + 1,
        ))
        # 关键断言：过期旧向量已删（0 行），回到"无向量可回填"的可自愈状态
        assert _vec_count(p) == 0
        assert _fts_count(p) == 1  # 关键词索引照常刷新为新内容

        # embed 服务恢复 → 回填收敛，且向量与新内容一致（非旧内容残影）
        emb.fail = False
        assert p.backfill_embeddings() == 1
        assert _vec_count(p) == 1
        row = p._conn.execute(
            "SELECT embedding FROM memory_vecs WHERE memory_id=?", (m.id,)
        ).fetchone()
        got = list(struct.unpack(f"<{len(row['embedding']) // 4}f", row["embedding"]))
        assert got == pytest.approx(emb.embed(["用户迷上足球"])[0])
        hits = p.search("想踢足球")
        assert hits and hits[0].memory.id == m.id
        assert hits[0].detail["vec"] > 0.0
    finally:
        p.close()


# ============================================
# P3 回归：replace_all/restore_backup 的全量重嵌不得占用写事务
# ============================================


class _RecordingEmbedder(FakeEmbedder):
    """记录每次 embed 调用时连接是否处于写事务内。"""

    def __init__(self):
        super().__init__()
        self.provider = None
        self.in_txn: list[bool] = []

    def embed(self, texts):
        conn = self.provider._conn if self.provider is not None else None
        self.in_txn.append(bool(conn is not None and conn.in_transaction))
        return super().embed(texts)


def test_replace_all_embeds_outside_write_transaction(tmp_path):
    """replace_all 的 embed 调用必须发生在 BEGIN IMMEDIATE 之外。

    回归：全量重嵌留在写事务 + 模块锁内时，聚合期间 worker 子进程的
    remember_fact 全部 database is locked 被静默丢弃。
    """
    emb = _RecordingEmbedder()
    p = SQLiteProvider(db_path=tmp_path / "rall.db", embedder=emb)
    emb.provider = p
    try:
        p.upsert(_mem("旧一 北京", id="a"))
        p.upsert(_mem("旧二 足球", id="b"))
        emb.in_txn.clear()  # 丢弃种子写入（事务内）的记录

        label = p.replace_all([_mem("整合 北京 足球", source="consolidated")], backup=False)

        assert label is None
        assert emb.in_txn == [False], f"embed 发生在写事务内: {emb.in_txn}"
        assert len(p.get_all()) == 1
        assert _vec_count(p) == 1 and _fts_count(p) == 1  # fts/vec 与全量一致
    finally:
        p.close()


def test_restore_backup_embeds_outside_write_transaction(tmp_path):
    """restore_backup 的 embed 调用同样必须在写事务之外，且行数与全量一致。"""
    emb = _RecordingEmbedder()
    p = SQLiteProvider(db_path=tmp_path / "rest.db", embedder=emb)
    emb.provider = p
    try:
        p.upsert(_mem("原始 北京", id="a"))
        label = p.replace_all([_mem("整合 足球", source="consolidated")], backup=True)
        emb.in_txn.clear()

        assert p.restore_backup(label) is True
        assert emb.in_txn == [False], f"embed 发生在写事务内: {emb.in_txn}"
        assert {m.content for m in p.get_all()} == {"原始 北京"}
        assert _vec_count(p) == 1 and _fts_count(p) == 1
    finally:
        p.close()


def test_concurrent_writer_not_blocked_during_replace_all_embed(tmp_path):
    """聚合 embed 阻塞期间，另一连接的写入不再被写事务压住（busy 不再触发）。

    模拟 worker 子进程：裸连接把 busy_timeout 压到 300ms。修前 embed 在
    BEGIN IMMEDIATE 内 → 裸连接 BEGIN IMMEDIATE 等 300ms 后抛
    database is locked；修后 embed 在事务外 → 写入即刻成功，且该写入
    （发生在 replace 事务开启前）被随后的纯全量替换正常覆盖。
    """
    import sqlite3
    import threading

    embed_started = threading.Event()
    release_embed = threading.Event()

    class _BlockingEmbedder(FakeEmbedder):
        """armed 后在 embed 处闸住（只闸 replace_all 的那次全量重嵌）。"""

        def __init__(self):
            super().__init__()
            self.armed = False

        def embed(self, texts):
            if self.armed:
                embed_started.set()
                release_embed.wait(timeout=10)
            return super().embed(texts)

    p = SQLiteProvider(db_path=tmp_path / "busy.db", embedder=_BlockingEmbedder())
    try:
        p.upsert(_mem("旧一 北京", id="a"))
        p.upsert(_mem("旧二 足球", id="b"))

        result: dict = {}

        def _replace():
            try:
                p.replace_all(
                    [_mem("整合 北京 足球", source="consolidated")], backup=False
                )
                result["error"] = None
            except Exception as e:  # noqa: BLE001
                result["error"] = e

        emb = p._embedder
        emb.armed = True  # 种子写入完成后才开始闸（保证闸的是 replace 的 embed）
        t = threading.Thread(target=_replace, daemon=True)
        t.start()
        try:
            assert embed_started.wait(timeout=10), "replace_all 未进入 embed"
            # 聚合 embed 仍阻塞时，裸连接写库（旧实现此处 busy 超时炸锁）
            raw = sqlite3.connect(str(tmp_path / "busy.db"), timeout=0.3)
            try:
                raw.execute("PRAGMA busy_timeout=300")
                raw.execute("BEGIN IMMEDIATE")
                raw.execute(
                    "INSERT INTO memories(id, content, source, created_at) "
                    "VALUES('live-1', '活写入', 'worker', 0)"
                )
                raw.execute("COMMIT")
            finally:
                raw.close()
        finally:
            release_embed.set()
            t.join(timeout=10)

        assert not t.is_alive()
        assert result.get("error") is None, f"replace_all 异常: {result.get('error')!r}"
        # live-1 在 replace 事务开启前落库（release 在裸写之后才 set），
        # 被纯全量替换覆盖 → 证明写窗口确实没有横跨 embed
        mems = p.get_all()
        assert [m.content for m in mems] == ["整合 北京 足球"]
        assert _vec_count(p) == 1 and _fts_count(p) == 1
    finally:
        p.close()


# ============================================
# P2-16：vec0 扩展缺失时的构造降级
# ============================================


def test_init_degrades_when_vec_extension_missing(tmp_path, monkeypatch):
    """sqlite_vec 缺失 + embedder 可用 → 构造不再崩溃，降级纯关键词。

    回归：_ensure_aux_tables 此前在 try 外执行 CREATE VIRTUAL TABLE ... vec0，
    扩展缺失时 OperationalError 直接冒出构造函数，启动链整体不可用。
    """
    import sys

    monkeypatch.setitem(sys.modules, "sqlite_vec", None)  # import 即失败
    p = SQLiteProvider(db_path=tmp_path / "mem.db", embedder=FakeEmbedder())
    try:
        assert p._vec_ready is False
        assert p._has_vec_table is False
        # 关键词降级通道照常可用（向量通道经 _vec_ready 守卫自动关闭）
        p.upsert(_mem("user likes python"))
        hits = p.search("python")
        assert len(hits) == 1
        assert hits[0].detail["source"] == "keyword"
    finally:
        p.close()


def test_init_survives_aux_table_failure(tmp_path, monkeypatch):
    """辅助索引建 vec0 虚表抛异常 → 构造兜底降级，不中断启动链。

    模拟真实失败形态：fts 建表成功、vec0（扩展缺失）失败。
    """
    import sqlite3

    def _fts_ok_vec0_boom(self):
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5("
            "content, memory_id UNINDEXED)"
        )
        raise sqlite3.OperationalError("no such module: vec0")

    monkeypatch.setattr(SQLiteProvider, "_ensure_aux_tables", _fts_ok_vec0_boom)
    p = SQLiteProvider(db_path=tmp_path / "mem.db", embedder=FakeEmbedder())
    try:
        assert p._has_vec_table is False
        assert p._embedder is None
        # memories + mem_fts 可用，写入与关键词降级检索正常
        p.upsert(_mem("用户喜欢火锅"))
        assert len(p.get_all()) == 1
        assert _fts_count(p) == 1
        hits = p.search("火锅")
        assert len(hits) == 1
        assert hits[0].detail["source"] == "keyword"
    finally:
        p.close()


# ============================================
# P2-17：recency 衰减下限钳制
# ============================================


def test_recency_decay_clamped_to_floor(store):
    """30 天前的记忆 recency 钳在 RECENCY_FLOOR，不再指数趋零。"""
    import time as _time

    month_ago = _time.time() - 30 * 86400
    store.upsert(_mem("用户喜欢火锅", created_at=month_ago, updated_at=month_ago))
    hits = store.search("想吃火锅")
    assert hits and hits[0].detail["recency"] == 0.3  # RECENCY_FLOOR


def test_old_strong_match_survives_competition_with_fresh_weak_match(store):
    """30 天的强相关记忆与新鲜次强记忆竞争时仍可达（P2-17 回归，纯向量版）。

    构造（词表独热）：查询 火锅+北京 两维；老记忆同两维 cos=1（s_vec=1.0），
    新鲜记忆 北京+天气+火锅 三维共享两维 cos≈0.816（VEC_SIM_FLOOR=0.40 重标
    定后 s_vec≈0.694，次强非弱）。修前老记忆 recency=0.995^720h≈0.027，得分
    0.027 < 相对门槛（0.35×次强得分≈0.24）被砍掉；修后钳在 0.3，得分
    0.3 ≥ 门槛 0.243 保留。
    """
    import time as _time

    month_ago = _time.time() - 30 * 86400
    store.upsert(_mem("用户喜欢火锅也关注北京", created_at=month_ago, updated_at=month_ago))
    store.upsert(_mem("用户关注北京天气迷上火锅"))
    hits = store.search("火锅 北京")
    got = {h.memory.content for h in hits}
    assert "用户喜欢火锅也关注北京" in got, f"老记忆被门槛吞掉: {[h.memory.content for h in hits]}"


# ============================================
# 真模型冒烟（默认 skip，CI 无网/不装模型时零开销）
# ============================================


# ============================================
# 打分校准（噪声地板 / 相对门槛 / 弱召回收敛）
# ============================================
# P2-26：skipif 门控此前错位在本节首个测试上（隔着一个注释块），导致
# 线上事故回归测试被静默跳过、真模型 smoke 反而每次必跑（~100MB 下载）。
# 门控已挪回 test_real_fastembed_smoke，本节恢复为常规必跑。


def test_no_evidence_query_returns_zero_hits(store):
    """词表外查询（FakeEmbedder 下零向量、FTS 无 token 命中）→ 诚实 0 命中。

    回归用例：真机上发"go"曾 4/4 全命中——根因是未校准余弦的 0.5 分地板。
    """
    store.upsert(_mem("用户喜欢火锅"))
    store.upsert(_mem("用户关注北京天气"))
    store.upsert(_mem("用户在做音乐项目"))
    hits = store.search("zzz")
    assert hits == []


def test_nonempty_query_zero_evidence_no_weak_recall(store):
    """非空查询零证据 → 不走弱召回兜底（弱召回仅限空 query）。

    查询用词表外英文串：逐字/分词后与中文记忆无任何字符重叠，向量侧
    也是零向量——两条通道都无证据。
    """
    store.upsert(_mem("用户喜欢火锅"))
    store.upsert(_mem("用户关注北京天气"))
    assert store.search("qqqxyz") == []
    # 空 query 才允许弱召回兜底
    assert len(store.search("")) == 2


def test_hit_detail_carries_channel_breakdown(store):
    """每条命中的 detail 携带 source/vec/recency（诊断面板数据源）。"""
    store.upsert(_mem("用户喜欢火锅"))
    hits = store.search("想吃火锅")
    assert hits, "共享词表词应有命中"
    d = hits[0].detail
    assert set(d.keys()) == {"source", "vec", "recency"}
    assert d["source"] == "vector"
    assert 0.0 <= d["vec"] <= 1.0
    assert 0.0 < d["recency"] <= 1.0  # 刚写入，新近度接近 1


def test_relative_gate_drops_weak_tail(store):
    """相对门槛：纯向量分数远低于最高分的候选被丢弃。

    构造（词表独热，查询 python+火锅 两维，VEC_SIM_FLOOR=0.40 重标定
    s_vec=(cos-0.40)/0.60）：
    - 强命中同两维 cos=1 → s_vec=1.0；
    - 次强三维共享两维 cos≈0.816 → s_vec≈0.694 ≥ 0.35 → 保留；
    - 弱命中六维共享两维 cos≈0.577 → s_vec≈0.296 < 0.35×最高分 → 丢弃。
    """
    store.upsert(_mem("python 火锅"))
    store.upsert(_mem("python 火锅 旅行"))
    store.upsert(_mem("python 火锅 旅行 天气 足球 音乐"))
    hits = store.search("python 火锅")
    got = [h.memory.content for h in hits]
    assert got[0] == "python 火锅"
    assert "python 火锅 旅行" in got
    assert "python 火锅 旅行 天气 足球 音乐" not in got


def test_vec_below_floor_is_zeroed(store):
    """余弦低于噪声地板 → 语义分量归零；词面命中不再救场（纯向量打分）。

    构造 cos≈0.316：内容占 火锅+音乐+天气+足球+旅行 五维，查询共享
    火锅 一维 → cos = 1/√(2×5) ≈ 0.316 < 地板 0.40 → vec 分量压成 0。
    旧融合下 FTS 逐字（火/锅）还能把它捞回来，纯向量下零证据 → 诚实返回空。
    """
    store.upsert(_mem("火锅 音乐 天气 足球 旅行"))
    assert store.search("北京火锅") == []


def test_fts_only_match_not_returned_under_pure_vector(store):
    """纯向量断言：仅 FTS 词面命中、向量相似度低于地板的条目不再返回。

    旧融合下 doc1 靠 fts 通道（0.3 权重）必然进结果；现在打分只看向量——
    doc1 与查询（北京+火锅 两维）仅共享 火锅 一维且自身五维
    （cos≈0.316 < 地板 0.40 → 归零）→ 出局，只剩 doc2（cos≈0.707）。
    """
    store.upsert(_mem("火锅 音乐 天气 足球 旅行"))  # FTS 命中 火/锅，向量低于地板
    store.upsert(_mem("用户喜欢火锅"))  # 向量 cos≈0.707
    hits = store.search("北京火锅")
    assert [h.memory.content for h in hits] == ["用户喜欢火锅"]
    assert hits[0].detail["source"] == "vector"
    assert hits[0].detail["vec"] > 0.0


@pytest.mark.skipif(
    os.environ.get("HERMES_EMBEDDING_SMOKE") != "1",
    reason="设置 HERMES_EMBEDDING_SMOKE=1 才运行（需下载真实模型 ~100MB）",
)
def test_real_fastembed_smoke(tmp_path):
    from src.memory.embeddings import get_default_embedder

    p = SQLiteProvider(db_path=tmp_path / "mem.db", embedder=get_default_embedder())
    p.upsert(_mem("用户喜欢火锅"))
    hits = p.search("想吃辣锅")  # 无共享 trigram/逐字词也应有语义召回
    assert hits and hits[0].memory.content == "用户喜欢火锅"
    p.close()
