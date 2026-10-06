"""
记忆聚合相关测试：
  - FileMemoryStore.replace_all / list_backups / restore_backup
  - MemoryConsolidator（mock LLM）：protect_outside 并发保护 + 备份 + 整合替换 + 失败不改动
  - config_store 读写（T2b-②：kv 后端，set_data_root 隔离）

不依赖 Qdrant、不依赖网络（LLM mock）。

P3-5：文件后端已退役出 src，FileMemoryStore/FileMemoryProvider 改从
scripts/legacy_memory_backend 加载（退役位置的回归保护）；consolidator
只走协议面，测试语义不变。
"""
import time
from unittest.mock import patch

import pytest

from scripts.legacy_memory_backend import FileMemoryProvider, FileMemoryStore
from src.memory.models import Memory
from src.memory import config_store
from src.storage import paths


def _store(tmp_path):
    return FileMemoryStore(workspace_root=str(tmp_path))


def _provider_store(tmp_path):
    """协议面 store（consolidator 用）：FileMemoryProvider 包装直连 store。"""
    return FileMemoryProvider(_store(tmp_path))


# ============================================
# replace_all + 备份/恢复
# ============================================

def test_replace_all_swaps_and_backs_up(tmp_path):
    """replace_all：旧内容被替换 + 自动备份 + 备份内容等于旧内容。"""
    store = _store(tmp_path)
    store.upsert(Memory(user_id="u1", content="旧记忆A"))
    store.upsert(Memory(user_id="u1", content="旧记忆B"))

    new = [Memory(user_id="u1", content="整合后的一条", source="consolidated")]
    backup_name = store.replace_all("u1", new, backup=True)

    assert backup_name is not None and backup_name.startswith("profile.md.bak.")
    cur = store.get_all("u1")
    assert len(cur) == 1
    assert cur[0].content == "整合后的一条"
    assert cur[0].source == "consolidated"


def test_replace_all_no_backup_when_empty(tmp_path):
    """原库为空时 replace_all 不生成备份（没必要）。"""
    store = _store(tmp_path)
    new = [Memory(user_id="u1", content="第一条")]
    backup_name = store.replace_all("u1", new, backup=True)
    assert backup_name is None
    assert len(store.get_all("u1")) == 1


def test_list_and_restore_backup(tmp_path):
    """list_backups 枚举 + restore_backup 恢复（且恢复前当前内容也被备份）。"""
    store = _store(tmp_path)
    store.upsert(Memory(user_id="u1", content="原始记忆"))

    # 第一次替换 → 产生备份1
    store.replace_all("u1", [Memory(user_id="u1", content="整合v1", source="consolidated")])
    backs = store.list_backups("u1")
    assert len(backs) == 1
    assert backs[0]["name"].startswith("profile.md.bak.")

    # 恢复备份1 → 回到"原始记忆"，且当前"整合v1"被备份为备份2
    ok = store.restore_backup("u1", backs[0]["name"])
    assert ok is True
    cur = store.get_all("u1")
    assert len(cur) == 1
    assert cur[0].content == "原始记忆"

    backs2 = store.list_backups("u1")
    assert len(backs2) == 2  # 恢复前自动备份了"整合v1"


def test_restore_nonexistent_backup(tmp_path):
    """恢复不存在的备份返回 False（不崩）。"""
    store = _store(tmp_path)
    assert store.restore_backup("u1", "profile.md.bak.9999") is False


def test_restore_rejects_path_traversal(tmp_path):
    """backup_name 含路径分隔符 → 拒绝（防穿越）。"""
    store = _store(tmp_path)
    assert store.restore_backup("u1", "../evil.md") is False
    assert store.restore_backup("u1", "not_a_backup.txt") is False


# ============================================
# config_store（T2b-②：kv 后端）
# ============================================

@pytest.fixture(autouse=True)
def _isolated_kv(tmp_path):
    """数据根指向 tmp：config kv 落在 tmp/hermes.db（不碰真实 data/）。"""
    paths.set_data_root(tmp_path)
    yield
    paths.set_data_root(None)


def test_config_store_defaults_and_roundtrip(tmp_path):
    """kv 无值时返回默认值；save→load 往返一致（参数被忽略，单用户）。"""
    cfg = config_store.load("", "u1")
    assert cfg["auto_consolidate"] is False
    assert cfg["interval_hours"] == 24
    assert cfg["threshold"] == 20
    assert cfg["last_run_at"] == ""

    config_store.save("", "u1", {"auto_consolidate": True, "interval_hours": 6, "threshold": 5})
    cfg2 = config_store.load("", "u1")
    assert cfg2["auto_consolidate"] is True
    assert cfg2["interval_hours"] == 6
    assert cfg2["threshold"] == 5


def test_config_store_touch_last_run(tmp_path):
    """touch_last_run 更新时间戳，保留其它字段。"""
    config_store.save("", "u1", {"auto_consolidate": True, "interval_hours": 12})
    config_store.touch_last_run("", "u1")
    cfg = config_store.load("", "u1")
    assert cfg["auto_consolidate"] is True  # 保留
    assert cfg["interval_hours"] == 12      # 保留
    assert cfg["last_run_at"] != ""         # 已更新


def test_config_store_legacy_yaml_fallback(tmp_path):
    """kv 空且 agent_home 下有旧 yaml → 首次 load 自动迁入 kv（一次性兜底）。"""
    legacy = paths.agent_home("memory_config.yaml")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        "auto_consolidate: true\ninterval_hours: 8\nthreshold: 3\nlast_run_at: '2026-01-01T00:00:00'\n",
        encoding="utf-8",
    )

    cfg = config_store.load("", "u1")
    assert cfg["auto_consolidate"] is True
    assert cfg["interval_hours"] == 8
    assert cfg["threshold"] == 3
    assert cfg["last_run_at"] == "2026-01-01T00:00:00"

    # 修改 yaml 不再影响 load（kv 已接管；yaml 未删，等迁移脚本改名）
    legacy.write_text("auto_consolidate: false\n", encoding="utf-8")
    assert config_store.load("", "u1")["auto_consolidate"] is True


# ============================================
# MemoryConsolidator（mock LLM）
# ============================================

def _fake_manager(store):
    """构造一个仅含 store 的假 manager（跳过 MemoryManager.__init__ 的 LLM 配置）。"""
    class _M:
        def __init__(self):
            self.store = store
    return _M()


def test_consolidator_merges_and_replaces(tmp_path):
    """LLM 返回整合结果 → 存储被替换为更少的条目 + 备份生成。"""
    store = _store(tmp_path)
    store.upsert(Memory(user_id="u1", content="用户喜欢 Python"))
    store.upsert(Memory(user_id="u1", content="用户用 Python 做数据分析"))
    store.upsert(Memory(user_id="u1", content="用户住北京"))

    from src.memory.consolidator import MemoryConsolidator
    c = MemoryConsolidator(_fake_manager(_provider_store(tmp_path)), "k", "u", "m")

    fake_resp = '{"items": [{"content": "用户喜欢 Python，用 Python 做数据分析", "source_ids": ["%s", "%s"]}, {"content": "用户住北京", "source_ids": ["%s"]}]}' % (
        store.get_all("u1")[0].id, store.get_all("u1")[1].id, store.get_all("u1")[2].id,
    )
    with patch.object(c.llm, "invoke_simple", return_value=fake_resp):
        result = c.consolidate("u1")

    assert result["ok"] is True
    assert result["before_count"] == 3
    assert result["after_count"] == 2
    assert result["backup_name"] is not None
    cur = store.get_all("u1")
    assert len(cur) == 2
    assert all(m.source == "consolidated" for m in cur)


def test_consolidator_too_few_skips(tmp_path):
    """≤1 条记忆 → 跳过，不调 LLM，文件不变。"""
    store = _store(tmp_path)
    store.upsert(Memory(user_id="u1", content="唯一一条"))

    from src.memory.consolidator import MemoryConsolidator
    c = MemoryConsolidator(_fake_manager(_provider_store(tmp_path)), "k", "u", "m")

    called = {"n": 0}
    def _boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("不应调 LLM")
    with patch.object(c.llm, "invoke_simple", side_effect=_boom):
        result = c.consolidate("u1")

    assert result["ok"] is False
    assert result["error"] == "too_few"
    assert called["n"] == 0
    assert len(store.get_all("u1")) == 1  # 没动


def test_consolidator_llm_failure_no_change(tmp_path):
    """LLM 输出非 JSON → ok=False，存储完全不变。"""
    store = _store(tmp_path)
    for i in range(3):
        store.upsert(Memory(user_id="u1", content=f"记忆{i}"))

    from src.memory.consolidator import MemoryConsolidator
    c = MemoryConsolidator(_fake_manager(_provider_store(tmp_path)), "k", "u", "m")

    with patch.object(c.llm, "invoke_simple", return_value="这不是JSON"):
        result = c.consolidate("u1")

    assert result["ok"] is False
    assert result["error"] == "llm_failed"
    assert len(store.get_all("u1")) == 3  # 一条没少
    assert store.list_backups("u1") == []  # 没生成备份


def test_consolidator_concurrent_write_protected(tmp_path):
    """LLM 期间新增的记忆：baseline 守卫下被保留（不丢写）。"""
    store = _store(tmp_path)
    store.upsert(Memory(user_id="u1", content="原始A"))
    store.upsert(Memory(user_id="u1", content="原始B"))

    from src.memory.consolidator import MemoryConsolidator
    c = MemoryConsolidator(_fake_manager(_provider_store(tmp_path)), "k", "u", "m")

    # LLM 调用时先记录快照 id，调用过程中模拟 worker 新写一条
    snapshot_ids = set()
    def _slow_llm(*_a, **_k):
        snapshot_ids.update(m.id for m in store.get_all("u1"))
        # 模拟 LLM 执行期间，另一进程写入了新记忆
        store.upsert(Memory(user_id="u1", content="并发新增C"))
        # source_ids 用快照里的两条（排除并发新增的）
        sids = [m.id for m in store.get_all("u1") if m.id in snapshot_ids]
        return '{"items": [{"content": "整合AB", "source_ids": ["%s", "%s"]}]}' % (sids[0], sids[1])

    with patch.object(c.llm, "invoke_simple", side_effect=_slow_llm):
        result = c.consolidate("u1")

    assert result["ok"] is True
    cur = store.get_all("u1")
    contents = [m.content for m in cur]
    # 整合条目 + 并发新增条目 都在
    assert "整合AB" in contents
    assert "并发新增C" in contents
    assert len(cur) == 2
    assert result["after_count"] == 2


def test_consolidator_baseline_update_delete_protected(tmp_path):
    """LLM 期间被 UPDATE/DELETE 的记忆：活值胜出、删除不复活（旧 protect_outside 的盲区）。"""
    store = _store(tmp_path)
    a = Memory(user_id="u1", content="原始A")
    b = Memory(user_id="u1", content="原始B")
    dmem = Memory(user_id="u1", content="待删D")
    for m in (a, b, dmem):
        store.upsert(m)

    from src.memory.consolidator import MemoryConsolidator
    c = MemoryConsolidator(_fake_manager(_provider_store(tmp_path)), "k", "u", "m")

    def _mutating_llm(*_a, **_k):
        cur = {m.id: m for m in store.get_all("u1")}
        # 窗口内并发写：UPDATE 一条（版本号前移）、DELETE 一条
        store.upsert(Memory(
            user_id="u1", content="原始A-已修订", id=a.id, updated_at=time.time(),
        ))
        store.delete(dmem.id)
        sids = [mid for mid in cur if mid != dmem.id]
        return '{"items": [{"content": "整合ABD", "source_ids": ["%s", "%s"]}]}' % (
            sids[0], sids[1],
        )

    with patch.object(c.llm, "invoke_simple", side_effect=_mutating_llm):
        result = c.consolidate("u1")

    assert result["ok"] is True
    by_content = {m.content for m in store.get_all("u1")}
    # a 修订版保住；d 不复活；其余按整合结果
    assert by_content == {"原始A-已修订", "整合ABD"}


# ============================================
# 按项目分组聚合（隔离硬边界）
# ============================================

def test_consolidator_groups_by_project_no_cross_merge(tmp_path):
    """A/B 两项目各自合并：LLM 按组各调一次，产物继承本组 project。"""
    store = _store(tmp_path)
    for proj in ("etf", "web"):
        for i in (1, 2):
            store.upsert(Memory(user_id="u1", content=f"{proj}事实{i}", project=proj))

    from src.memory.consolidator import MemoryConsolidator
    c = MemoryConsolidator(_fake_manager(_provider_store(tmp_path)), "k", "u", "m")

    merged_calls: list[str] = []

    def _fake_merge(user_id, snapshot, project=""):
        merged_calls.append(project)
        return [Memory(user_id=user_id, content=f"整合-{project}",
                       source="consolidated", project=project)]

    with patch.object(c, "_llm_merge", side_effect=_fake_merge):
        result = c.consolidate("u1")

    assert result["ok"] is True
    assert sorted(merged_calls) == ["etf", "web"]  # 每组独立一次
    cur = store.get_all("u1")
    assert {m.content: m.project for m in cur} == {"整合-etf": "etf", "整合-web": "web"}


def test_consolidator_merged_items_inherit_group_project(tmp_path):
    """真实 _llm_merge 路径：整合条目继承组 project，不再落成全局。"""
    store = _store(tmp_path)
    ids = []
    for i in (1, 2):
        m = Memory(user_id="u1", content=f"etf事实{i}", project="etf")
        store.upsert(m)
        ids.append(m.id)

    from src.memory.consolidator import MemoryConsolidator
    c = MemoryConsolidator(_fake_manager(_provider_store(tmp_path)), "k", "u", "m")
    fake_resp = '{"items": [{"content": "etf整合条目", "source_ids": ["%s", "%s"]}]}' % (
        ids[0], ids[1],
    )
    with patch.object(c.llm, "invoke_simple", return_value=fake_resp):
        result = c.consolidate("u1")

    assert result["ok"] is True
    cur = store.get_all("u1")
    assert len(cur) == 1
    assert cur[0].project == "etf"
    assert cur[0].source == "consolidated"


def test_consolidator_all_single_groups_skips(tmp_path):
    """每组都 ≤1 条：无可合并，too_few 不替换（不调 LLM）。"""
    store = _store(tmp_path)
    store.upsert(Memory(user_id="u1", content="etf唯一", project="etf"))
    store.upsert(Memory(user_id="u1", content="web唯一", project="web"))

    from src.memory.consolidator import MemoryConsolidator
    c = MemoryConsolidator(_fake_manager(_provider_store(tmp_path)), "k", "u", "m")

    def _boom(*_a, **_k):
        raise AssertionError("不应调 LLM")
    with patch.object(c.llm, "invoke_simple", side_effect=_boom):
        result = c.consolidate("u1")

    assert result["ok"] is False
    assert result["error"] == "too_few"
    assert len(store.get_all("u1")) == 2  # 一条没动


def test_consolidator_single_group_passthrough_survives(tmp_path):
    """多条组被整合时，单条组必须原样保留（replace_all 整库替换，缺组=删组）。"""
    store = _store(tmp_path)
    for i in (1, 2):
        store.upsert(Memory(user_id="u1", content=f"etf事实{i}", project="etf"))
    store.upsert(Memory(user_id="u1", content="web唯一", project="web"))
    store.upsert(Memory(user_id="u1", content="全局唯一", project=""))

    from src.memory.consolidator import MemoryConsolidator
    c = MemoryConsolidator(_fake_manager(_provider_store(tmp_path)), "k", "u", "m")

    def _fake_merge(user_id, snapshot, project=""):
        assert project == "etf"  # 只有 etf 组该被合并
        return [Memory(user_id=user_id, content="整合-etf",
                       source="consolidated", project=project)]

    with patch.object(c, "_llm_merge", side_effect=_fake_merge):
        result = c.consolidate("u1")

    assert result["ok"] is True
    cur = {m.content: m.project for m in store.get_all("u1")}
    assert cur == {"整合-etf": "etf", "web唯一": "web", "全局唯一": ""}


def test_consolidator_one_group_llm_fail_aborts_all(tmp_path):
    """任一组 LLM 失败 → 整体放弃替换（否则失败组的记忆会被整库替换删掉）。"""
    store = _store(tmp_path)
    for i in (1, 2):
        store.upsert(Memory(user_id="u1", content=f"etf事实{i}", project="etf"))
        store.upsert(Memory(user_id="u1", content=f"web事实{i}", project="web"))

    from src.memory.consolidator import MemoryConsolidator
    c = MemoryConsolidator(_fake_manager(_provider_store(tmp_path)), "k", "u", "m")

    def _fail_etf(user_id, snapshot, project=""):
        if project == "etf":
            return None
        return [Memory(user_id=user_id, content="整合-web", project=project)]

    with patch.object(c, "_llm_merge", side_effect=_fail_etf):
        result = c.consolidate("u1")

    assert result["ok"] is False
    assert result["error"] == "llm_failed"
    # 两个项目的原记忆都还在
    assert len(store.get_all("u1")) == 4
