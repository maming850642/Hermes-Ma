"""
T3 session_store 抽取测试 —— save/list/load/rename/delete 行为一致（T7 dict 格式）。

覆盖：
- save/load 往返（user/assistant(tool_calls+reasoning)/tool/system 四类消息 dict）
- 旧格式（消息类 type 名）会话 JSON 兼容加载 → dict 规范化
- name/waker 的"传入优先、否则保持"语义（rename 语义）
- list_sessions 字段 + 按更新时间倒序
- delete（worker session_delete op 的实现路径：_get_session_file + unlink）
- 路径穿越防御、不存在会话的空返回
- lc_to_dict 规范化 + load_message 未知类型返回 None
- cli re-export 兼容：from src.cli import save_session 仍是同一函数
"""
import json

import pytest

import src.session_store as ss


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch, request):
    """SESSIONS_DIR 重定向到 tmp（实现体在 session_store，cli 是 re-export）。

    P3 起 save/load 还会经 session_state_store 读写默认库 kv——数据根一并
    改道 tmp，避免测试碰真实 data/hermes.db。
    """
    import src.cli as cli
    from src.storage import paths
    paths.set_data_root(tmp_path)
    request.addfinalizer(lambda: paths.set_data_root(None))
    monkeypatch.setattr(ss, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(cli, "SESSIONS_DIR", tmp_path)
    return tmp_path


def _rich_messages():
    """覆盖四类消息的富消息列表（OpenAI dict）。"""
    return [
        {"role": "user", "content": "帮我删文件"},
        {"role": "assistant", "content": "",
         "tool_calls": [{
             "id": "call_1", "type": "function",
             "function": {"name": "bash", "arguments": '{"command": "rm x"}'},
         }],
         "reasoning": "思考中"},
        {"role": "tool", "content": "已删除", "tool_call_id": "call_1"},
        {"role": "system", "content": "压缩摘要"},
        {"role": "assistant", "content": "搞定"},
    ]


# ════════════════════════════════════════════════════════════════
# 会话自动命名（首句派生：句界优先、30 字硬截兑底）
# ════════════════════════════════════════════════════════════════
class TestDeriveSessionName:

    def test_sentence_boundary_before_limit(self):
        """30 字内有句界 → 截到句界符之前（不含句号）。"""
        text = "帮我写一份周报，重点突出项目进度。另外把下周的招聘计划、预算调整和团队建设活动也一起列出来"
        assert ss.derive_session_name(text) == "帮我写一份周报，重点突出项目进度"

    def test_no_boundary_long_sentence_hard_truncates(self):
        """整句无句界且超限 → 硬截 30 字加省略号。"""
        text = "请用一句话解释一下量子纠缠到底是什么意思然后给我举一个通俗易懂的例子最好和生活场景相关"
        out = ss.derive_session_name(text)
        assert out == text[:30] + "…"
        assert len(out) == 31

    def test_short_sentence_without_boundary_kept_whole(self):
        """无句界但不超限 → 原样保留。"""
        assert ss.derive_session_name("你好") == "你好"

    def test_newline_is_boundary(self):
        """换行是句界：多行输入取名第一行。"""
        assert ss.derive_session_name("帮我把这个函数改成异步的\n另外顺便加个错误处理") \
            == "帮我把这个函数改成异步的"

    def test_question_and_semicolon_boundaries(self):
        assert ss.derive_session_name("这个报错怎么解决？麻烦帮我看一下;谢谢") \
            == "这个报错怎么解决？麻烦帮我看一下"

    def test_markdown_symbols_stripped(self):
        assert ss.derive_session_name("**帮我** 总结一下 [会议纪要] > 的要点") \
            == "帮我 总结一下 会议纪要 的要点"

    def test_empty_and_non_string(self):
        assert ss.derive_session_name("") == ""
        assert ss.derive_session_name("   ") == ""
        assert ss.derive_session_name(None) == ""
        assert ss.derive_session_name(123) == ""

    def test_leading_boundary_skipped(self):
        """唯一句界符在首位（cut=0）→ 视为无有效句界，走原样/硬截分支。"""
        assert ss.derive_session_name("。帮我删文件") == "。帮我删文件"


class TestAutoNameInCompose:
    """_compose_session_data 接入：快照无名字时按首条 user 消息派生。"""

    def test_save_without_name_derives_from_first_user_message(self, sessions_dir):
        ss.save_session("u1", [{"role": "user", "content":
                        "帮我写一份周报，重点突出项目进度。另外列下计划"}], "s1")
        data = json.loads((sessions_dir / "u1" / "s1.json").read_text(encoding="utf-8"))
        assert data["name"] == "帮我写一份周报，重点突出项目进度"

    def test_explicit_name_wins_over_derived(self, sessions_dir):
        ss.save_session("u1", [{"role": "user", "content": "你好世界。再来一句"}], "s1",
                        name="手动名")
        data = json.loads((sessions_dir / "u1" / "s1.json").read_text(encoding="utf-8"))
        assert data["name"] == "手动名"

    def test_rename_kept_and_never_overwritten(self, sessions_dir):
        """首轮自动命名 → rename 后保持；再保存不回退自动名。"""
        ss.save_session("u1", [{"role": "user", "content": "帮我删文件"}], "s1")
        ss.save_session("u1", [{"role": "user", "content": "帮我删文件"}], "s1",
                        name="清理任务")
        ss.save_session("u1", [{"role": "user", "content": "帮我删文件"}], "s1")
        data = json.loads((sessions_dir / "u1" / "s1.json").read_text(encoding="utf-8"))
        assert data["name"] == "清理任务"

    def test_image_only_first_message_stays_unnamed(self, sessions_dir):
        """首条消息纯图片（content 为 list 无文本）→ name 保持空。"""
        ss.save_session("u1", [{"role": "user",
                        "content": [{"type": "text", "text": ""},
                                    {"type": "image_url", "image_url": {"url": "data:..."}}]}], "s1")
        data = json.loads((sessions_dir / "u1" / "s1.json").read_text(encoding="utf-8"))
        assert data["name"] == ""


# ════════════════════════════════════════════════════════════════
# save / load 往返
# ════════════════════════════════════════════════════════════════
class TestSaveLoad:

    def test_roundtrip_all_message_types(self, sessions_dir):
        msgs = _rich_messages()
        ss.save_session("u1", msgs, "s1", todos=[{"t": 1}], virtual_fs={"f.txt": "x"},
                        waker="researcher")
        loaded, todos, vfs, waker = ss.load_session("u1", "s1")
        assert loaded == msgs                    # 四类消息 dict deep equal 还原
        assert todos == [{"t": 1}]
        assert vfs == {"f.txt": "x"}
        assert waker == "researcher"
        assert loaded[1]["role"] == "assistant"
        assert loaded[1]["tool_calls"] == msgs[1]["tool_calls"]
        assert loaded[1]["reasoning"] == "思考中"
        assert loaded[2]["role"] == "tool"
        assert loaded[2]["tool_call_id"] == "call_1"

    def test_save_writes_openai_dict_format_marker(self, sessions_dir):
        """save 格式升版：format=openai-dict + schema_version=4。"""
        ss.save_session("u1", [{"role": "user", "content": "hi"}], "s1")
        data = json.loads((sessions_dir / "u1" / "s1.json").read_text(encoding="utf-8"))
        assert data["format"] == "openai-dict"
        assert data["schema_version"] == 4
        assert data["messages"][0]["role"] == "user"

    def test_load_nonexistent_returns_empty(self, sessions_dir):
        assert ss.load_session("u1", "ghost") == ([], [], {}, "")

    def test_load_legacy_type_name_format(self, sessions_dir):
        """旧格式会话 JSON（消息类 type 名）→ 加载为 OpenAI dict。"""
        f = sessions_dir / "u1" / "legacy.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({
            "user_id": "u1", "session_id": "legacy", "name": "",
            "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
            "message_count": 4, "preview": "",
            "messages": [
                {"type": "HumanMessage", "content": "删掉这个文件"},
                {"type": "AIMessage", "content": "", "tool_calls": [
                    {"name": "bash", "args": {"command": "rm x"}, "id": "call_1"},
                ], "additional_kwargs": {"reasoning": "思考中"}},
                {"type": "ToolMessage", "content": "已删除", "tool_call_id": "call_1"},
                {"type": "SystemMessage", "content": "压缩摘要"},
                {"type": "SomethingElse", "content": "未知类型应被跳过"},
            ],
            "todos": [], "virtual_fs": {}, "waker": "", "schema_version": 3,
        }), encoding="utf-8")

        msgs, todos, vfs, waker = ss.load_session("u1", "legacy")
        assert waker == ""
        assert len(msgs) == 4  # 未知 type 被跳过
        assert msgs[0] == {"role": "user", "content": "删掉这个文件"}
        # AIMessage → assistant，tool_calls 转 OpenAI 格式，reasoning 保留
        assert msgs[1]["role"] == "assistant"
        tc = msgs[1]["tool_calls"][0]
        assert tc["id"] == "call_1"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "bash"
        assert json.loads(tc["function"]["arguments"]) == {"command": "rm x"}
        assert msgs[1]["reasoning"] == "思考中"
        # ToolMessage → tool，tool_call_id 保留
        assert msgs[2] == {"role": "tool", "content": "已删除", "tool_call_id": "call_1"}
        # SystemMessage → system
        assert msgs[3] == {"role": "system", "content": "压缩摘要"}

    def test_load_v2_file_without_waker(self, sessions_dir):
        """v2 旧文件（无 waker 字段）→ waker 返回空串（向后兼容）。"""
        f = sessions_dir / "u1" / "old.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({
            "user_id": "u1", "session_id": "old", "name": "",
            "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
            "message_count": 0, "preview": "", "messages": [],
            "todos": [], "virtual_fs": {}, "schema_version": 2,
        }), encoding="utf-8")
        msgs, todos, vfs, waker = ss.load_session("u1", "old")
        assert (msgs, todos, vfs, waker) == ([], [], {}, "")

    def test_save_keeps_existing_created_at_and_waker(self, sessions_dir):
        """二次 save 不传 waker/name → 保持已有值；created_at 不被刷新。"""
        ss.save_session("u1", [{"role": "user", "content": "hi"}], "s1", waker="critic")
        first = json.loads((sessions_dir / "u1" / "s1.json").read_text(encoding="utf-8"))
        ss.save_session("u1", [{"role": "user", "content": "hi2"}], "s1")
        second = json.loads((sessions_dir / "u1" / "s1.json").read_text(encoding="utf-8"))
        assert second["waker"] == "critic"
        assert second["created_at"] == first["created_at"]


# ════════════════════════════════════════════════════════════════
# rename / delete（worker op 的实现路径）
# ════════════════════════════════════════════════════════════════
class TestRenameDelete:

    def test_rename_via_save_with_name(self, sessions_dir):
        """rename = load 目标 + save(name=新名)（worker session_rename op 路径）。"""
        ss.save_session("u1", [{"role": "user", "content": "hi"}], "s1")
        msgs, t, v, wk = ss.load_session("u1", "s1")
        ss.save_session("u1", msgs, "s1", todos=t, virtual_fs=v,
                        name="新名字", waker=wk)
        entries = {s["session_id"]: s for s in ss.list_sessions("u1")}
        assert entries["s1"]["name"] == "新名字"

    def test_delete_via_get_session_file_unlink(self, sessions_dir):
        """delete = _get_session_file + unlink（worker session_delete op 路径）。"""
        ss.save_session("u1", [{"role": "user", "content": "hi"}], "s1")
        fpath = ss._get_session_file("u1", "s1")
        assert fpath.exists()
        fpath.unlink()
        assert ss.load_session("u1", "s1") == ([], [], {}, "")
        assert ss.list_sessions("u1") == []

    def test_get_session_file_rejects_traversal(self, sessions_dir):
        with pytest.raises(ValueError):
            ss._get_session_file("u1", "../escape")
        with pytest.raises(ValueError):
            ss._get_session_file("../u", "s1")
        with pytest.raises(ValueError):
            ss._get_session_file("u1", "a/b")


# ════════════════════════════════════════════════════════════════
# list_sessions
# ════════════════════════════════════════════════════════════════
class TestListSessions:

    def test_list_fields_and_order(self, sessions_dir):
        ss.save_session("u1", [{"role": "user", "content": "第一条"}], "s_old")
        ss.save_session("u1", [{"role": "user", "content": "第二条"}], "s_new", waker="researcher")
        sessions = ss.list_sessions("u1")
        assert len(sessions) == 2
        # 按更新时间倒序（后保存的在前；updated_at 秒级相同则字符串比较也稳定）
        assert sessions[0]["session_id"] in ("s_new", "s_old")
        by_id = {s["session_id"]: s for s in sessions}
        assert by_id["s_new"]["waker"] == "researcher"
        assert by_id["s_old"]["waker"] == ""
        assert by_id["s_old"]["preview"] == "第一条"
        assert by_id["s_old"]["message_count"] == 1
        assert "updated_at" in by_id["s_old"] and "name" in by_id["s_old"]

    def test_list_empty_user(self, sessions_dir):
        assert ss.list_sessions("nobody") == []


# ════════════════════════════════════════════════════════════════
# lc_to_dict（规范化助手）/ load_message（两格式识别）
# ════════════════════════════════════════════════════════════════
class TestMessageConversion:

    def test_lc_to_dict_normalizes(self):
        d = ss.lc_to_dict({
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "bash", "arguments": "{}"},
            }],
            "reasoning": "r",
            "compact_id": "compact-xyz",   # 运行期附带键被剔除
        })
        assert d == {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "bash", "arguments": "{}"},
            }],
            "reasoning": "r",
        }
        t = ss.lc_to_dict({"role": "tool", "content": "ok", "tool_call_id": "c1"})
        assert t == {"role": "tool", "content": "ok", "tool_call_id": "c1"}
        h = ss.lc_to_dict({"role": "user", "content": "hi"})
        assert h == {"role": "user", "content": "hi"}

    def test_lc_to_dict_roundtrip_with_load(self):
        for m in _rich_messages():
            assert ss.load_message(ss.lc_to_dict(m)) == ss.lc_to_dict(m)

    def test_load_message_unknown_returns_none(self):
        assert ss.load_message({"type": "SomethingElse", "content": "x"}) is None
        assert ss.load_message({}) is None


# ════════════════════════════════════════════════════════════════
# cli re-export 兼容（T3 抽取后调用形状不变）
# ════════════════════════════════════════════════════════════════
def test_cli_reexports_same_functions():
    import src.cli as cli
    assert cli.save_session is ss.save_session
    assert cli.load_session is ss.load_session
    assert cli.list_sessions is ss.list_sessions
    assert cli._get_session_file is ss._get_session_file
    assert cli._migrate_old_sessions is ss._migrate_old_sessions
    assert cli.lc_to_dict is ss.lc_to_dict
    assert cli.load_message is ss.load_message


# ════════════════════════════════════════════════════════════════
# ensure_session_stub（fork stub 场景）：F7 字段透传契约
# ════════════════════════════════════════════════════════════════
class TestEnsureSessionStub:

    def test_stub_persists_todos_vfs_waker_project(self, sessions_dir):
        """F7：stub 创建时传入的 todos/vfs/waker/project 必须落盘可读回——
        fork 端点靠此透传源会话的非消息状态，字段丢了 fork 分支即回默认态。"""
        created = ss.ensure_session_stub(
            "u1", [{"role": "user", "content": "hi"}], "s1",
            todos=[{"t": 1}], virtual_fs={"f.txt": "x"},
            waker="researcher", name="fork:src", project="demo-proj",
        )
        assert created is True
        msgs, todos, vfs, waker = ss.load_session("u1", "s1")
        assert msgs == [{"role": "user", "content": "hi"}]
        assert todos == [{"t": 1}]
        assert vfs == {"f.txt": "x"}
        assert waker == "researcher"
        meta = ss.read_session_meta("u1", "s1")
        assert meta["project"] == "demo-proj"
        assert meta["name"] == "fork:src"
        # 列表侧也可见（stub 出现在侧栏的机制本身）
        entries = {s["session_id"]: s for s in ss.list_sessions("u1")}
        assert entries["s1"]["waker"] == "researcher"
        assert entries["s1"]["project"] == "demo-proj"

    def test_stub_never_overwrites_existing(self, sessions_dir):
        """ADR-0004-D2：已有快照时 stub 放弃（worker 是权威写者）。"""
        ss.save_session("u1", [{"role": "user", "content": "权威"}], "s1", waker="critic")
        created = ss.ensure_session_stub(
            "u1", [{"role": "user", "content": "旁路"}], "s1", waker="other")
        assert created is False
        _, _, _, waker = ss.load_session("u1", "s1")
        assert waker == "critic"


# ════════════════════════════════════════════════════════════════
# F7 fork 端点透传（sessions router 集成：源快照字段 → 新 stub）
# ════════════════════════════════════════════════════════════════
class TestForkEndpointStubPassthrough:

    @pytest.fixture
    def fork_app(self, tmp_path, monkeypatch, request):
        """仅挂 sessions router 的精简 app（仿 test_session_events_api.py）：
        SessionLog 指向 tmp 库经 _FakeCtx 注入；快照目录重定向 tmp。"""
        from fastapi import FastAPI

        from src.storage import paths
        from web_fastapi.routers import sessions as sessions_router

        monkeypatch.setattr(ss, "SESSIONS_DIR", tmp_path / "sessions")
        paths.set_data_root(tmp_path)
        request.addfinalizer(lambda: paths.set_data_root(None))
        provider = SQLiteProvider(db_path=tmp_path / "db" / "hermes.db")

        class _FakeCtx:
            def try_get(self, key):
                return SessionLog(provider=provider) if key == "sessions" else None

        a = FastAPI()
        a.include_router(sessions_router.router, prefix="/api/sessions")
        a.state.cordis_ctx = _FakeCtx()
        try:
            yield a, SessionLog(provider=provider)
        finally:
            provider.close()

    def test_fork_inherits_todos_vfs_waker_project(self, fork_app):
        """带 todos/vfs/waker 的会话 fork 后 stub 含这些字段（F7 回归）。"""
        from fastapi.testclient import TestClient

        from src.constants import LOCAL_USER

        app, log = fork_app
        src_msgs = [{"role": "user", "content": "源问题"}]
        ss.save_session(LOCAL_USER, src_msgs, "src1",
                        todos=[{"t": 1}], virtual_fs={"a.txt": "1"},
                        waker="night-watcher", project="demo-proj")
        log.append("src1", USER_MSG, {"content": "源问题"})
        log.append("src1", ASSISTANT_MSG, {"content": "源回答"})

        r = TestClient(app).post("/api/sessions/src1/fork")
        assert r.status_code == 200, r.text
        new_sid = r.json()["session_id"]

        meta = ss.read_session_meta(LOCAL_USER, new_sid) or {}
        assert meta["todos"] == [{"t": 1}]
        assert meta["virtual_fs"] == {"a.txt": "1"}
        assert meta["waker"] == "night-watcher"
        assert meta["project"] == "demo-proj"     # 归属继承（ADR-0005 D2）
        # 消息仍是事件投影（不是 JSON 快照拷贝）
        msgs, todos, vfs, waker = ss.load_session(LOCAL_USER, new_sid)
        assert msgs == [
            {"role": "user", "content": "源问题"},
            {"role": "assistant", "content": "源回答"},
        ]
        assert (todos, vfs, waker) == ([{"t": 1}], {"a.txt": "1"}, "night-watcher")

    def test_fork_without_source_snapshot_gets_defaults(self, fork_app):
        """源会话无 JSON 快照（纯事件流）→ stub 字段取默认值，不炸。"""
        from fastapi.testclient import TestClient

        from src.constants import LOCAL_USER

        app, log = fork_app
        log.append("src2", USER_MSG, {"content": "只有事件"})

        r = TestClient(app).post("/api/sessions/src2/fork")
        assert r.status_code == 200, r.text
        new_sid = r.json()["session_id"]

        meta = ss.read_session_meta(LOCAL_USER, new_sid) or {}
        assert meta["todos"] == []
        assert meta["virtual_fs"] == {}
        assert meta["waker"] == ""
        assert meta["project"] == ""


# ════════════════════════════════════════════════════════════════
# T9 事件优先加载（load_session_events_first）
# ════════════════════════════════════════════════════════════════
from src.agent.session_log import (  # noqa: E402
    ASSISTANT_MSG,
    TURN_END,
    TURN_START,
    USER_MSG,
    SessionLog,
)
from src.storage import paths  # noqa: E402
from src.storage.sqlite_provider import SQLiteProvider  # noqa: E402


@pytest.fixture
def events_log(tmp_path):
    """SessionLog 指向 tmp 库（事件流隔离，不碰真实 data/hermes.db）。"""
    provider = SQLiteProvider(db_path=tmp_path / "db" / "events.db")
    yield SessionLog(provider=provider)
    provider.close()


class TestLoadSessionEventsFirst:

    def test_events_preferred_over_json(self, sessions_dir, events_log):
        """有事件 → 消息以事件流投影为准（JSON 里的旧消息被忽略）；
        todos/vfs/waker 仍从 JSON 快照补齐。"""
        ss.save_session("u1", [{"role": "user", "content": "旧消息"}], "s1",
                        todos=[{"t": 1}], virtual_fs={"f.txt": "x"}, waker="researcher")
        events_log.append("s1", TURN_START, {"input": "新问题"})
        events_log.append("s1", USER_MSG, {"content": "新问题"})
        events_log.append("s1", ASSISTANT_MSG, {"content": "新回答"})
        events_log.append("s1", TURN_END, {"message_count": 2})

        msgs, todos, vfs, waker = ss.load_session_events_first(
            "u1", "s1", session_log=events_log)
        # 消息 = 事件投影（turn/* 不投影）
        assert msgs == [
            {"role": "user", "content": "新问题"},
            {"role": "assistant", "content": "新回答"},
        ]
        # 非 messages 字段从 JSON 快照读
        assert todos == [{"t": 1}]
        assert vfs == {"f.txt": "x"}
        assert waker == "researcher"

    def test_events_with_missing_snapshot_defaults(self, sessions_dir, events_log):
        """有事件但 JSON 快照缺失 → todos/vfs/waker 给默认值 []/{}/""。"""
        events_log.append("s1", USER_MSG, {"content": "hi"})
        msgs, todos, vfs, waker = ss.load_session_events_first(
            "u1", "s1", session_log=events_log)
        assert msgs == [{"role": "user", "content": "hi"}]
        assert (todos, vfs, waker) == ([], {}, "")

    def test_no_events_falls_back_to_json(self, sessions_dir, events_log):
        """无事件（旧会话）→ 走原 JSON 路径，与 load_session 完全一致。"""
        msgs_in = [{"role": "user", "content": "旧会话"}]
        ss.save_session("u1", msgs_in, "legacy1", todos=[{"t": 9}])
        result = ss.load_session_events_first("u1", "legacy1", session_log=events_log)
        assert result == ss.load_session("u1", "legacy1")
        assert result[0] == msgs_in
        assert result[1] == [{"t": 9}]

    def test_session_log_failure_falls_back_to_json(self, sessions_dir, events_log):
        """SessionLog 查询抛异常 → 回退 JSON 路径（降级安全）。"""
        msgs_in = [{"role": "user", "content": "兜底"}]
        ss.save_session("u1", msgs_in, "s2")

        class _BrokenLog:
            def events(self, sid):
                raise RuntimeError("db gone")

        msgs, todos, vfs, waker = ss.load_session_events_first(
            "u1", "s2", session_log=_BrokenLog())
        assert msgs == msgs_in
        assert (todos, vfs, waker) == ([], {}, "")

    def test_default_session_log_construction(self, sessions_dir, tmp_path):
        """session_log=None → 按需构造默认库 SessionLog（data_root 重定向验证）。"""
        paths.set_data_root(tmp_path / "data")
        try:
            # 不显式传 log：函数内自建 SessionLog()（默认库 = data_root/hermes.db）
            SessionLog().append("s3", USER_MSG, {"content": "默认库事件"})
            ss.save_session("u1", [{"role": "user", "content": "旧"}], "s3")
            msgs, _, _, _ = ss.load_session_events_first("u1", "s3")
            assert msgs == [{"role": "user", "content": "默认库事件"}]
        finally:
            paths.set_data_root(None)


# ════════════════════════════════════════════════════════════════
# T9 worker session_load op 走事件优先路径（mock worker state）
# ════════════════════════════════════════════════════════════════
class TestWorkerSessionLoadOp:

    def _make_state(self, log):
        from types import SimpleNamespace

        from web_fastapi.worker_process import WorkerState

        state = WorkerState("u1")
        # worker 的 SessionLog 与组合根同源（agent._session_log）
        state.agent = SimpleNamespace(_session_log=log)
        return state

    def test_op_loads_event_projection(self, sessions_dir, events_log, monkeypatch):
        """session_load op：有事件 → bucket.messages 是事件投影（非 JSON 旧消息）。"""
        import web_fastapi.worker_process as wp

        state = self._make_state(events_log)
        ss.save_session("u1", [{"role": "user", "content": "旧消息"}], "s1",
                        todos=[{"t": 1}], waker="researcher")
        events_log.append("s1", USER_MSG, {"content": "新问题"})
        events_log.append("s1", ASSISTANT_MSG, {"content": "新回答"})

        sent = []
        monkeypatch.setattr(wp, "_send", lambda msg: sent.append(msg))
        wp._op_session_load(state, "r1", {"session_id": "s1"})

        assert sent[0]["data"]["ok"] is True
        assert sent[0]["data"]["message_count"] == 2
        assert sent[0]["data"]["waker"] == "researcher"
        bucket = state.get_bucket("s1")
        assert bucket.messages == [
            {"role": "user", "content": "新问题"},
            {"role": "assistant", "content": "新回答"},
        ]
        assert bucket.todos == [{"t": 1}]          # 从 JSON 快照补齐
        assert state.current_sid == "s1"

    def test_op_falls_back_to_json_without_events(self, sessions_dir, events_log, monkeypatch):
        """session_load op：无事件 → 原 JSON 路径（旧会话兼容）。"""
        import web_fastapi.worker_process as wp

        state = self._make_state(events_log)
        ss.save_session("u1", [{"role": "user", "content": "旧会话"}], "legacy9")

        sent = []
        monkeypatch.setattr(wp, "_send", lambda msg: sent.append(msg))
        wp._op_session_load(state, "r1", {"session_id": "legacy9"})

        assert sent[0]["data"]["ok"] is True
        assert state.get_bucket("legacy9").messages == [{"role": "user", "content": "旧会话"}]

    def test_op_error_when_both_empty(self, sessions_dir, events_log, monkeypatch):
        """事件与 JSON 都空 → 报"会话不存在或为空"（原语义保持）。"""
        import web_fastapi.worker_process as wp

        state = self._make_state(events_log)
        sent = []
        monkeypatch.setattr(wp, "_send", lambda msg: sent.append(msg))
        wp._op_session_load(state, "r1", {"session_id": "ghost"})
        assert sent[0]["type"] == "error"
        assert "不存在" in sent[0]["message"]
