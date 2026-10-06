"""用户发消息路径：remember 工具写入 + 下一轮检索召回。

不打真 LLM：空库/无候选时 Decider 短路 ADD。
检索走 SQLite 关键词通道（不注入 embedder），查询词与记忆正文要有交集。
"""
from __future__ import annotations

import pytest

from src.constants import LOCAL_USER
from src.memory.manager import MemoryManager
from src.agent.memory_orch import MemoryOrchestrator
from src.storage.sqlite_provider import SQLiteProvider
from src.tools import remember as remember_mod


@pytest.fixture
def mem_env(tmp_path, monkeypatch):
    """隔离库 + 激活项目可切换。"""
    store = SQLiteProvider(db_path=tmp_path / "mem.db", embedder=None)
    mgr = MemoryManager(store=store)
    remember_mod.set_memory_manager(mgr)
    remember_mod.set_current_user_id(LOCAL_USER)
    active = {"slug": "inbox"}
    monkeypatch.setattr(
        "src.storage.projects_store.get_active_project",
        lambda provider=None: active["slug"],
    )
    orch = MemoryOrchestrator(mgr)
    yield {"mgr": mgr, "orch": orch, "store": store, "active": active}
    remember_mod._manager_holder.clear()
    remember_mod._current_user_id.set(None)
    remember_mod._current_project.set(None)


def _user_turn_remember_then_ask(orch, remember_text: str, user_ask: str):
    """模拟两轮用户请求：先 remember 工具，再发检索问句。"""
    out = remember_mod.remember(remember_text)
    detail = orch.retrieve_with_detail(
        LOCAL_USER, user_ask, messages=[{"role": "user", "content": user_ask}],
        session_id="sess-test",
    )
    return out, detail


def test_remember_tool_then_user_query_retrieves(mem_env):
    """用户透露姓名 → remember → 下一句询问应召回。"""
    out, detail = _user_turn_remember_then_ask(
        mem_env["orch"],
        "用户名叫张三",
        "张三是谁",
    )
    assert out.startswith("已记住：")
    assert detail["hit_count"] >= 1
    assert any("张三" in (h.get("memory") or "") for h in detail["hits"])
    assert "张三" in " ".join(detail["memories"])


def test_remember_stamps_active_project_and_isolates(mem_env):
    """项目 A 记住的事实，项目 B 的用户提问召不回（全局空 project 除外）。"""
    mem_env["active"]["slug"] = "proj-a"
    r1 = remember_mod.remember("项目A使用 Python 做量化")
    assert r1.startswith("已记住：")

    mem_env["active"]["slug"] = "proj-b"
    r2 = remember_mod.remember("项目B使用 Wind 终端")
    assert r2.startswith("已记住：")

    # 用户在 B 发问
    d_b = mem_env["orch"].retrieve_with_detail(
        LOCAL_USER, "Wind 终端", messages=[], session_id="s-b")
    assert any("Wind" in (h.get("memory") or "") for h in d_b["hits"])
    assert not any("量化" in (h.get("memory") or "") for h in d_b["hits"])

    # 用户切回 A 发问
    mem_env["active"]["slug"] = "proj-a"
    d_a = mem_env["orch"].retrieve_with_detail(
        LOCAL_USER, "Python 量化", messages=[], session_id="s-a")
    assert any("量化" in (h.get("memory") or "") for h in d_a["hits"])
    assert not any("Wind" in (h.get("memory") or "") for h in d_a["hits"])


def test_remember_and_retrieve_follow_project_contextvar(mem_env):
    """会话绑定项目（contextvar）压过全局激活指针。

    场景：多项目并发，顶栏/激活指针停在 B，但本会话绑定 A——
    remember 写入与检索都必须按 A 走，不受顶栏切换污染。
    """
    mem_env["active"]["slug"] = "proj-b"
    tok = remember_mod.set_current_project("proj-a")
    try:
        r = remember_mod.remember("项目A的会员到期日是月底")
        assert r.startswith("已记住：")
        # 生产链路：agent_v3 prestep 从 contextvar 读出会话项目传给检索
        d = mem_env["orch"].retrieve_with_detail(
            LOCAL_USER, "会员到期", messages=[], session_id="s-a",
            project=remember_mod.get_current_project())
        assert any("会员" in (h.get("memory") or "") for h in d["hits"])
    finally:
        remember_mod._current_project.reset(tok)

    # 复位后回落激活指针 B：A 的事实对 B 不可见
    d_b = mem_env["orch"].retrieve_with_detail(
        LOCAL_USER, "会员到期", messages=[], session_id="s-b")
    assert not any("会员" in (h.get("memory") or "") for h in d_b["hits"])
    # 落库归属确实是 A
    stored = [m for m in mem_env["store"].get_all() if "会员" in m.content]
    assert stored and stored[0].project == "proj-a"


def test_global_memory_visible_in_any_project(mem_env):
    """旧数据 project='' 作为全局，任意项目检索都可见。"""
    from src.memory.models import Memory
    mem_env["store"].upsert(Memory(
        user_id=LOCAL_USER, content="用户偏好深色主题", project=""))
    mem_env["active"]["slug"] = "proj-a"
    d = mem_env["orch"].retrieve_with_detail(
        LOCAL_USER, "深色主题", messages=[], session_id="s1")
    assert any("深色" in (h.get("memory") or "") for h in d["hits"])


def test_remember_without_user_id_does_not_write(mem_env):
    remember_mod._current_user_id.set(None)
    out = remember_mod.remember("不该入库的事实")
    assert "无法确定当前用户" in out
    assert mem_env["mgr"].get_all(LOCAL_USER) == []


def test_remember_without_manager():
    remember_mod._manager_holder.clear()
    remember_mod.set_current_user_id(LOCAL_USER)
    out = remember_mod.remember("任何内容")
    assert "未就绪" in out


def test_duplicate_remember_same_fact_no_llm_when_no_keyword_hit(mem_env):
    """完全相同的短句：第一次 ADD；若关键词能召到候选则第二次走 LLM（测试环境
    无网会 NOOP）。至少第一次必须写入且能被用户问句召回。"""
    first = remember_mod.remember("用户在南京办公")
    assert first.startswith("已记住：")
    second = remember_mod.remember("用户在南京办公")
    # 有候选则 decide；无网 NOOP；无候选会再 ADD。两种都可接受，但不能失败。
    assert "记忆存储失败" not in second
    d = mem_env["orch"].retrieve_with_detail(
        LOCAL_USER, "南京办公", messages=[], session_id="s")
    assert d["hit_count"] >= 1
