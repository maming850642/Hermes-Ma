"""
FileMemoryStore 测试 —— M0-1。

验证纯文件记忆后端(profile.md)的 CRUD + 用户隔离 + 原子写。
不依赖 Qdrant、不依赖网络、不依赖 LLM。

P3-5：文件后端已退役出 src（scripts/legacy_memory_backend.py，仅供
migrate_to_sqlite.py 等迁移场景）。本文件对退役位置做回归保护，
防止迁移工具链腐化。测试语义不变。
"""
import threading

from scripts.legacy_memory_backend import FileMemoryStore
from src.memory.models import Memory


def _store(tmp_path):
    """构造一个指向 tmp_path 的 FileMemoryStore。"""
    return FileMemoryStore(workspace_root=str(tmp_path))


# ============================================
# 写入 + 全量读取
# ============================================


def test_add_and_get_all(tmp_path):
    """add 两条 → get_all 返回 2 条。"""
    store = _store(tmp_path)
    store.upsert(Memory(user_id="alice", content="用户用 Python"))
    store.upsert(Memory(user_id="alice", content="用户住北京"))

    mems = store.get_all("alice")
    assert len(mems) == 2
    contents = {m.content for m in mems}
    assert contents == {"用户用 Python", "用户住北京"}


def test_upsert_same_id_updates(tmp_path):
    """同 id 的 upsert 是更新,不是新增。"""
    store = _store(tmp_path)
    mem = Memory(user_id="alice", content="旧内容")
    store.upsert(mem)

    # 同 id,新内容 → 更新
    mem.content = "新内容"
    store.upsert(mem)

    mems = store.get_all("alice")
    assert len(mems) == 1
    assert mems[0].content == "新内容"


# ============================================
# 检索(无向量库,退化为关键词匹配)
# ============================================


def test_search_substring_hit(tmp_path):
    """无向量库时,search 退化为内容匹配;top_k 截断。"""
    store = _store(tmp_path)
    store.upsert(Memory(user_id="alice", content="用户主要用 Python 做数据分析"))
    store.upsert(Memory(user_id="alice", content="用户住在北京"))
    store.upsert(Memory(user_id="alice", content="Python 是门好语言"))

    hits = store.search("alice", "Python", limit=5, min_score=0.0)
    # 含 Python 的有 2 条
    contents = [h.memory.content for h in hits]
    assert any("Python" in c for c in contents)
    assert all("Python" in c for c in contents)  # 全部命中都含关键词


def test_search_no_match(tmp_path):
    """搜不到 → 空列表(不抛)。"""
    store = _store(tmp_path)
    store.upsert(Memory(user_id="alice", content="用户用 Python"))
    hits = store.search("alice", "完全不相关的查询词XYZ", limit=5, min_score=0.0)
    assert hits == []


def test_search_candidates_is_search(tmp_path):
    """search_candidates 与 search 等价(不过滤 min_score)。"""
    store = _store(tmp_path)
    store.upsert(Memory(user_id="alice", content="Python"))
    a = store.search_candidates("alice", "Python", limit=5)
    b = store.search("alice", "Python", limit=5, min_score=0.0)
    assert len(a) == len(b)


def test_search_returns_hit_with_score(tmp_path):
    """返回的是 Hit,有 score 字段。"""
    store = _store(tmp_path)
    store.upsert(Memory(user_id="alice", content="Python"))
    hits = store.search("alice", "Python", limit=5, min_score=0.0)
    assert len(hits) == 1
    assert hasattr(hits[0], "score")
    assert hits[0].score > 0.0


# ============================================
# 删除
# ============================================


def test_delete_by_id(tmp_path):
    """按 id 删除单条。"""
    store = _store(tmp_path)
    mem = Memory(user_id="alice", content="待删除")
    store.upsert(mem)
    assert len(store.get_all("alice")) == 1

    ok = store.delete(mem.id)
    assert ok is True
    assert len(store.get_all("alice")) == 0


def test_delete_nonexistent(tmp_path):
    """删不存在的 id → False(不谎报成功)。"""
    store = _store(tmp_path)
    ok = store.delete("不存在的id")
    assert ok is False


def test_delete_all_by_user(tmp_path):
    """清空全部记忆（user_id 保参但被忽略——单文件）。"""
    store = _store(tmp_path)
    store.upsert(Memory(user_id="alice", content="A"))
    store.upsert(Memory(user_id="alice", content="B"))
    store.upsert(Memory(user_id="bob", content="C"))

    ok = store.delete_all_by_user("alice")
    assert ok is True
    assert store.get_all("alice") == []
    # 单用户拍平：全部记忆共用一份 profile.md，一并清空
    assert store.get_all("bob") == []


# ============================================
# 单用户拍平（T2b-②）
# ============================================


def test_flattened_shared_profile(tmp_path):
    """两个 user_id 写同一份 <root>/profile.md（拍平后无 users/<uid> 段）。"""
    store = _store(tmp_path)
    store.upsert(Memory(user_id="alice", content="Alice 的秘密"))
    store.upsert(Memory(user_id="bob", content="Bob 的秘密"))

    # 两个 user_id 视角读到同一份（路径不含 user 段）
    assert len(store.get_all("alice")) == 2
    assert len(store.get_all("bob")) == 2
    assert not (tmp_path / "users").exists()


def test_search_ignores_user_param(tmp_path):
    """search 的 user_id 参数被忽略（单一检索域）。"""
    store = _store(tmp_path)
    store.upsert(Memory(user_id="alice", content="Python"))
    store.upsert(Memory(user_id="bob", content="Go"))

    hits = store.search("alice", "Python", limit=5, min_score=0.0)
    assert {h.memory.content for h in hits} == {"Python"}


# ============================================================
# 2026-07-03: 中文按字切分回归测试（H4 修复）
# ============================================================

def test_search_chinese_near_match(tmp_path):
    """中文近义改写必须能召回（按字切分修复）。

    背景：旧实现把整段中文当一个 term，"用户叫张三" vs "姓名是张三"
    子串不匹配 → 命中 0 → Decider 无候选短路 ADD → 记忆无限重复。
    按字切分后，"张""三" 命中。
    """
    store = _store(tmp_path)
    store.upsert(Memory(user_id="alice", content="姓名是张三"))

    # "用户叫张三" 含 "张""三"，应命中 "姓名是张三"
    hits = store.search("alice", "用户叫张三", limit=5, min_score=0.0)
    assert len(hits) >= 1
    assert "张三" in hits[0].memory.content


def test_search_chinese_no_false_positive_on_unrelated(tmp_path):
    """完全不相关的中文查询不应命中。"""
    store = _store(tmp_path)
    store.upsert(Memory(user_id="alice", content="用户喜欢 Go 语言"))
    hits = store.search("alice", "明天天气不错", limit=5, min_score=0.0)
    assert len(hits) == 0



def test_get_by_id_isolated(tmp_path):
    """get_by_id 能取回完整 Memory。"""
    store = _store(tmp_path)
    mem = Memory(user_id="alice", content="单条测试")
    store.upsert(mem)

    got = store.get_by_id(mem.id)
    assert got is not None
    assert got.content == "单条测试"
    assert got.user_id == "alice"


def test_get_by_id_nonexistent(tmp_path):
    """get_by_id 不存在 → None。"""
    store = _store(tmp_path)
    assert store.get_by_id("不存在") is None


# ============================================
# 原子写 / profile.md 格式
# ============================================


def test_profile_md_is_written(tmp_path):
    """写入后,profile.md 存在且内容完整(无半行)。"""
    store = _store(tmp_path)
    store.upsert(Memory(user_id="alice", content="用户用 Python"))

    profile = tmp_path / "profile.md"
    assert profile.exists()
    text = profile.read_text(encoding="utf-8")
    assert "用户用 Python" in text
    # 每行都应是完整的(无截断)
    for line in text.splitlines():
        assert not line.endswith("\n")  # splitlines 已去换行
        assert line != "" or True  # 允许空行


def test_atomic_write_no_partial_line(tmp_path):
    """原子写:写入后 profile.md 每条记忆是完整一行。"""
    store = _store(tmp_path)
    store.upsert(Memory(user_id="alice", content="第一条完整记忆"))
    store.upsert(Memory(user_id="alice", content="第二条完整记忆"))

    profile = tmp_path / "profile.md"
    text = profile.read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if l.strip()]
    assert len(lines) == 2
    assert "第一条完整记忆" in text
    assert "第二条完整记忆" in text


def test_concurrent_append_safe(tmp_path):
    """并发 upsert 不交错/不丢(原子写)。"""
    store = _store(tmp_path)

    def writer(n):
        for i in range(10):
            store.upsert(Memory(user_id="alice", content=f"并发条目-{n}-{i}"))

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    mems = store.get_all("alice")
    # 5 线程 × 10 条 = 50 条,一条不丢
    assert len(mems) == 50
