"""LLM 用量记账埋点测试（D1）。

覆盖：
- 非流式 chat()：成功记 ok 行（usage → tokens_in/out + 归属字段）；异常记
  error 行（摘要截断 200 字）后原样冒出；无 ctx 时归属字段空串
- 流式 stream_chat()：请求携带 stream_options.include_usage；usage-only
  尾包被生成器捕获并在流耗尽时记 ok 行；中途异常记 error 行后冒出
- stream_options 防御：端点报错含 stream_options 字样 → 去参重试一次并
  告警；其他异常不重试
- agent 层接线：stream_invoke 用 session_id + caller_context 设 usage ctx
  （scope 经 sid_scope 推导，waker: 前缀 → waker）
- 全局兜底回归：无 db fixture 的裸 LLMClient("m") 记账也只落 tmp
  （tests/conftest.py 的 autouse fixture，防真实 data/hermes.db 被污染）

存储一律走 set_data_root(tmp) 的临时库，不碰真实 data/hermes.db。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.llm.client import (
    LLMClient,
    _TRUNCATED_NOTE,
    get_current_usage_ctx,
    reset_current_usage_ctx,
    set_current_usage_ctx,
)
from src.storage import paths, usage_store
from src.storage.sqlite_provider import SQLiteProvider


@pytest.fixture
def db(tmp_path, monkeypatch):
    """数据根改道 tmp + usage_store 默认连接缓存清零（前后各一次）。"""
    paths.set_data_root(tmp_path)
    usage_store.reset_default_provider()
    yield tmp_path
    usage_store.reset_default_provider()
    paths.set_data_root(None)


@pytest.fixture(autouse=True)
def _isolated_usage_ctx():
    """每个用例从空归属开始，测毕还原外层 ctx。

    背景：stream_invoke 的 usage ctx 写入与 _current_llm_overrides 同款
    （不随 generator 结束还原），同线程先跑的 agent 测试会把值泄漏进来
    ——本文件的"空归属"断言必须自带隔离。
    """
    token = set_current_usage_ctx("", "", "")
    yield
    reset_current_usage_ctx(token)


def _rows():
    p = SQLiteProvider()
    rows = p.query("SELECT * FROM llm_usage ORDER BY id")
    p.close()
    return [dict(r) for r in rows]


def _client_with_create(fake_create):
    c = LLMClient(api_key="k", base_url="http://unit.test", model="test-model")
    c._client.chat.completions.create = fake_create
    return c


# ── openai 响应假件 ──

def _ok_response(usage=None):
    msg = SimpleNamespace(content="hi", tool_calls=None)
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg)])
    if usage is not None:
        resp.usage = usage
    return resp


def _usage(prompt=11, completion=7):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)


def _content_chunk(text, finish=None):
    delta = SimpleNamespace(content=text, reasoning=None,
                            reasoning_content=None, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish)], usage=None)


def _reasoning_chunk(text):
    delta = SimpleNamespace(content=None, reasoning=text,
                            reasoning_content=None, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=None)], usage=None)


def _usage_tail(prompt=11, completion=7):
    """usage-only 尾包：choices 为空、只带 usage（include_usage 生效形态）。"""
    return SimpleNamespace(choices=[], usage=_usage(prompt, completion))


# ============================================
# 非流式 chat()
# ============================================

def test_chat_records_ok_row_with_usage(db):
    c = _client_with_create(lambda **kw: _ok_response(_usage(11, 7)))
    token = set_current_usage_ctx("sess-1", "chat", "main")
    try:
        c.chat([{"role": "user", "content": "hi"}])
    finally:
        reset_current_usage_ctx(token)

    rows = _rows()
    assert len(rows) == 1
    r = rows[0]
    assert r["session_id"] == "sess-1"
    assert r["scope"] == "chat"
    assert r["caller"] == "main"
    assert r["model"] == "test-model"
    assert r["tokens_in"] == 11
    assert r["tokens_out"] == 7
    assert r["status"] == "ok"
    assert r["error"] == ""
    assert r["duration_ms"] is not None and r["duration_ms"] >= 0
    assert r["ts"] > 0


def test_chat_without_ctx_records_empty_attribution(db):
    c = _client_with_create(lambda **kw: _ok_response())  # 无 usage → tokens NULL
    c.chat([{"role": "user", "content": "hi"}])
    rows = _rows()
    assert len(rows) == 1
    r = rows[0]
    assert r["session_id"] == "" and r["scope"] == "" and r["caller"] == ""
    assert r["status"] == "ok"
    assert r["tokens_in"] is None and r["tokens_out"] is None


def test_chat_error_records_error_row_and_reraises(db):
    def boom(**kw):
        raise RuntimeError("x" * 500)

    c = _client_with_create(boom)
    token = set_current_usage_ctx("sess-err", "chat", "main")
    try:
        with pytest.raises(RuntimeError):
            c.chat([{"role": "user", "content": "hi"}])
    finally:
        reset_current_usage_ctx(token)

    rows = _rows()
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "error"
    assert r["tokens_in"] is None and r["tokens_out"] is None
    assert len(r["error"]) == 200  # 摘要截断 ~200 字
    assert r["error"] == "x" * 200


# ============================================
# 调用详情留底（req_messages / reasoning / output，v5+）
# ============================================

def test_chat_ok_row_carries_request_and_output(db):
    c = _client_with_create(lambda **kw: _ok_response(_usage(3, 2)))
    c.chat([{"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"}])
    r = _rows()[0]
    assert json.loads(r["req_messages"]) == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    assert r["output"] == "hi"
    assert r["reasoning"] == ""   # fake 响应无 reasoning 字段
    assert r["tools"] == ""       # 非工具轮无工具名


def test_chat_error_row_carries_request(db):
    """error 行也带请求留底——失败请求正是排查时最想回看的。"""

    def boom(**kw):
        raise RuntimeError("timeout")

    c = _client_with_create(boom)
    with pytest.raises(RuntimeError):
        c.chat([{"role": "user", "content": "查不到就怪我"}])
    r = _rows()[0]
    assert r["status"] == "error"
    assert json.loads(r["req_messages"]) == [
        {"role": "user", "content": "查不到就怪我"}]
    assert r["output"] == ""


def test_chat_req_record_strips_image_data_uri(db):
    """多模态 content 的 base64 data URI 入库前剥成占位标记。"""
    content = [
        {"type": "text", "text": "看图说话"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAAAAAA"}},
    ]
    c = _client_with_create(lambda **kw: _ok_response())
    c.chat([{"role": "user", "content": content}])
    r = _rows()[0]
    assert "base64" not in r["req_messages"]
    assert "图片已省略" in r["req_messages"]
    assert "看图说话" in r["req_messages"]


def test_chat_req_record_stays_valid_json_when_over_budget(db):
    """留底超预算：丢最旧非 system 消息重序列化——JSON 恒合法、尾部最新
    消息保住（此前对 JSON 串硬截，18 行实测解析失败）。"""
    long_msg = "x" * 3000
    msgs = [{"role": "user", "content": long_msg} for _ in range(10)]
    msgs.append({"role": "user", "content": "最新一条必须保留"})
    c = _client_with_create(lambda **kw: _ok_response(_usage(3, 2)))
    c.chat(msgs)
    r = _rows()[0]
    parsed = json.loads(r["req_messages"])   # 不抛 = JSON 合法
    assert parsed[-1]["content"] == "最新一条必须保留"
    assert len(r["req_messages"]) <= 12250


def test_chat_tool_only_round_records_tool_summary(db):
    """工具调用轮（content 空、只回 tool_calls）：output 记工具摘要而非
    空串——此前这类「✓ 成功」行在明细里看起来像模型什么都没说。"""
    msg = SimpleNamespace(
        content="",
        tool_calls=[SimpleNamespace(
            id="c1", type="function",
            function=SimpleNamespace(name="web_search",
                                     arguments='{"query": "softtime"}'))],
    )
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg)])
    resp.usage = _usage(5, 9)
    c = _client_with_create(lambda **kw: resp)
    c.chat([{"role": "user", "content": "hi"}])
    r = _rows()[0]
    assert "⚙️ 工具调用 #1: web_search" in r["output"]
    assert "softtime" in r["output"]
    assert "未产生文本输出" in r["output"]
    assert r["tools"] == "web_search"


def test_stream_tool_only_round_records_tool_summary(db):
    """流式纯工具轮：tool_call_deltas 按 index 增量合并 → output 记摘要。"""

    def _tool_chunk(name_part, args_part, with_id=None):
        delta = SimpleNamespace(
            content=None, reasoning=None, reasoning_content=None,
            tool_calls=[SimpleNamespace(
                index=0, id=with_id,
                function=SimpleNamespace(name=name_part, arguments=args_part))])
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason=None)],
            usage=None)

    def stream(**kw):
        return iter([
            _tool_chunk("web_", "", with_id="c9"),
            _tool_chunk("search", '{"q": '),
            _tool_chunk("", '"ddg"}'),
            _usage_tail(3, 30),
        ])

    c = _client_with_create(stream)
    list(c.stream_chat([{"role": "user", "content": "hi"}]))
    r = _rows()[0]
    assert "web_search" in r["output"]
    assert '"ddg"}' in r["output"]
    assert "未产生文本输出" in r["output"]
    assert r["tools"] == "web_search"


def test_chat_truncated_by_max_tokens_marks_output(db):
    """finish_reason=length → output 尾部追加截断标记（截断不再静默）。"""
    msg = SimpleNamespace(content="part", tool_calls=None)
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="length")])
    resp.usage = _usage(5, 2000)
    c = _client_with_create(lambda **kw: resp)
    c.chat([{"role": "user", "content": "hi"}])
    r = _rows()[0]
    assert r["status"] == "ok"
    assert r["output"] == "part" + _TRUNCATED_NOTE


def test_stream_ok_row_carries_reasoning_and_output(db):
    """流式 ok 行留底经 ThinkSplitter 透传后的推理/正文累积。"""
    token = set_current_usage_ctx("sess-d", "chat", "main")
    try:
        c = _client_with_create(
            lambda **kw: iter([_reasoning_chunk("想一下"), _content_chunk("答"),
                                _usage_tail(9, 9)]))
        list(c.stream_chat([{"role": "system", "content": "sys"},
                            {"role": "user", "content": "q"}]))
    finally:
        reset_current_usage_ctx(token)
    r = _rows()[0]
    assert r["reasoning"] == "想一下"
    assert r["output"] == "答"
    assert [m["role"] for m in json.loads(r["req_messages"])] == ["system", "user"]


def test_stream_truncated_by_max_tokens_marks_output(db):
    c = _client_with_create(
        lambda **kw: iter([_content_chunk("abc", finish="length"),
                           _usage_tail(1, 2000)]))
    list(c.stream_chat([{"role": "user", "content": "hi"}]))
    r = _rows()[0]
    assert r["status"] == "ok"
    assert r["output"] == "abc" + _TRUNCATED_NOTE


# ============================================
# 流式 stream_chat()
# ============================================

def test_stream_request_carries_include_usage(db):
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return iter([_content_chunk("a")])

    c = _client_with_create(fake_create)
    list(c.stream_chat([{"role": "user", "content": "hi"}]))
    assert captured["stream_options"] == {"include_usage": True}


def test_stream_records_ok_with_usage_tail(db):
    """usage-only 尾包（choices 空）被捕获，流耗尽记 ok 行。"""
    token = set_current_usage_ctx("waker:job1", "waker", "employee")
    try:
        c = _client_with_create(
            lambda **kw: iter([_content_chunk("你"), _content_chunk("好"),
                                _usage_tail(100, 31)]))
        chunks = list(c.stream_chat([{"role": "user", "content": "hi"}]))
    finally:
        reset_current_usage_ctx(token)

    # 消费方视图不变：正文原样到达（相邻 chunk 可能被 ThinkSplitter 合并），
    # usage 尾包不外泄为任何多余 chunk
    assert "".join(ch.content_delta for ch in chunks) == "你好"
    assert all(not ch.reasoning_delta and not ch.tool_call_deltas for ch in chunks)

    rows = _rows()
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "ok"
    assert r["session_id"] == "waker:job1"
    assert r["scope"] == "waker"
    assert r["caller"] == "employee"
    assert r["tokens_in"] == 100
    assert r["tokens_out"] == 31


def test_stream_without_usage_tail_records_null_tokens(db):
    c = _client_with_create(lambda **kw: iter([_content_chunk("a")]))
    list(c.stream_chat([{"role": "user", "content": "hi"}]))
    r = _rows()[0]
    assert r["status"] == "ok"
    assert r["tokens_in"] is None and r["tokens_out"] is None


def test_stream_midway_error_records_error_row_and_reraises(db):
    def flaky_stream(**kw):
        yield _content_chunk("part")
        raise RuntimeError("connection reset mid-stream")

    c = _client_with_create(flaky_stream)
    with pytest.raises(RuntimeError):
        list(c.stream_chat([{"role": "user", "content": "hi"}]))

    rows = _rows()
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "error"
    assert "connection reset mid-stream" in r["error"]
    assert r["duration_ms"] is not None


def test_stream_options_fallback_retry(db, caplog):
    """端点报错信息含 stream_options → 去参重试一次并告警，记账照常。"""
    import logging

    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ValueError("Unknown request parameter: 'stream_options'")
        return iter([_content_chunk("a"), _usage_tail(3, 4)])

    c = _client_with_create(fake_create)
    with caplog.at_level(logging.WARNING, logger="hermes.llm.client"):
        list(c.stream_chat([{"role": "user", "content": "hi"}]))

    assert len(calls) == 2
    assert "stream_options" in calls[0]
    assert "stream_options" not in calls[1]  # 重试已去掉该参数
    assert any("stream_options" in r.message for r in caplog.records)
    r = _rows()[0]
    assert r["status"] == "ok"
    assert (r["tokens_in"], r["tokens_out"]) == (3, 4)


def test_stream_non_stream_options_error_no_retry(db):
    """其他异常不触发去参重试（一次失败即记账 error + 冒出）。"""
    calls = []

    def boom(**kw):
        calls.append(kw)
        raise RuntimeError("connect timeout")

    c = _client_with_create(boom)
    with pytest.raises(RuntimeError):
        list(c.stream_chat([{"role": "user", "content": "hi"}]))
    assert len(calls) == 1
    r = _rows()[0]
    assert r["status"] == "error"
    assert "connect timeout" in r["error"]


# ============================================
# ctx 读写函数
# ============================================

def test_usage_ctx_default_empty():
    ctx = get_current_usage_ctx()
    assert ctx == {"session_id": "", "scope": "", "caller": ""}


# ============================================
# agent 层接线（stream_invoke 设 usage ctx）
# ============================================

class CtxProbeLLM:
    """记录 stream_chat 被调那一刻的 usage ctx（worker 线程 copy_context 后）。"""

    def __init__(self):
        self.ctxs = []

    def stream_chat(self, messages, tools=None, temperature=None, max_tokens=None):
        self.ctxs.append(get_current_usage_ctx())
        from src.llm.messages import Chunk
        return iter([Chunk(content_delta="最终"), Chunk(content_delta="回答")])


def test_agent_stream_invoke_sets_usage_ctx(db, monkeypatch):
    from tests.agent.test_agent_events import make_agent

    agent = make_agent(monkeypatch)
    llm = CtxProbeLLM()
    agent._llm_client = llm

    # waker 前缀 sid → scope "waker"；waker 链路 caller_context="employee"
    list(agent.stream_invoke("u1", "跑任务", session_id="waker:job1",
                             caller_context="employee"))
    assert llm.ctxs, "LLM 未被调用"
    assert llm.ctxs[0] == {
        "session_id": "waker:job1",
        "scope": "waker",
        "caller": "employee",
    }
    # 清理：stream_invoke 的 ctx 写入随线程上下文存续，防泄漏进后续测试
    set_current_usage_ctx("", "", "")


def test_agent_stream_invoke_chat_scope(db, monkeypatch):
    from tests.agent.test_agent_events import make_agent

    agent = make_agent(monkeypatch)
    llm = CtxProbeLLM()
    agent._llm_client = llm

    list(agent.stream_invoke("u1", "你好", session_id="sess-abc"))
    assert llm.ctxs[0] == {
        "session_id": "sess-abc",
        "scope": "chat",
        "caller": "main",
    }
    set_current_usage_ctx("", "", "")


# ============================================
# 全局兜底回归（tests/conftest.py 的 autouse fixture）
# ============================================

def test_global_fixture_keeps_default_usage_db_out_of_real_root(tmp_path):
    """不带 db fixture 的裸 LLMClient("m") + stub 内层跑一次 chat()：记账默认
    provider 必须落在全局 fixture 的 tmp，真实 data/hermes.db 零写入。

    回归背景：test_llm_client_params.py 这类无隔离测试曾把 D1 记账行写进
    生产库（model='m'、tokens NULL、duration 0，污染用量看板）。本用例只
    断言默认库路径位于本用例 tmp（不打开真实库，可靠且零风险）；若全局
    autouse fixture 被移除，路径断言立即失败。同时断言记账功能本身未被
    隔离误伤：tmp 库里恰好记一行。
    """
    real_db = paths.PROJECT_ROOT / "data" / "hermes.db"

    c = LLMClient(api_key="k", base_url="http://unit.test", model="m")
    c._client.chat.completions.create = lambda **kw: _ok_response(_usage(3, 5))
    c.chat([{"role": "user", "content": "hi"}])

    # 记账已触发默认 provider 构建：库文件必须在本用例 tmp 下，绝非真实根
    #（全局 fixture 的数据根是 tmp 的子目录 usage-root，挂载类测试的兄弟
    # 目录才不会落进数据根子树——见 conftest._isolated_usage_store 注释）
    p = usage_store._default_provider
    assert p is not None, "chat() 应已触发默认 provider 构建"
    assert tmp_path in p.db_path.parents
    assert p.db_path != real_db
    assert tmp_path in paths.data_root().parents

    # 记账功能本身未被隔离误伤：行落在 tmp 库且恰好一行
    items = usage_store.records()["items"]
    assert len(items) == 1
    assert items[0]["model"] == "m"
    assert (items[0]["tokens_in"], items[0]["tokens_out"]) == (3, 5)


def test_subagent_stream_tags_usage_ctx_caller(db):
    """子代理直接流式路径的记账 caller 打上 task:{name} 标记——否则继承
    主 agent 的 caller，用量明细里与主 agent 调用无法区分。"""
    from src.llm.messages import Chunk
    from src.tools.sub_agent import StreamingSubAgent

    seen: dict = {}

    class FakeLLM:
        def stream_chat(self, messages, **kw):
            seen["caller"] = get_current_usage_ctx()["caller"]
            yield Chunk(content_delta="ok")

    token = set_current_usage_ctx("sess-x", "chat", "main")
    try:
        sa = StreamingSubAgent(name="scout", timeout=0)
        sa.llm = FakeLLM()
        out = "".join(sa.stream("干活"))
    finally:
        reset_current_usage_ctx(token)

    assert out == "ok"
    assert seen["caller"] == "task:scout"
