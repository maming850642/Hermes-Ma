"""LLMClient 请求参数契约：max_tokens=None 不发字段、per-call extra_body 覆盖。

历史行为：默认 max_tokens=2000 且恒发送——记忆决策器撞上限后 content
为空，"决策 JSON 解析失败，默认 ADD" 刷屏。现契约：None = 不发字段
（服务端默认生效）；extra_body 调用级整体替换（记忆链路传 {} 以发出
不带任何模型专属 kwarg 的纯标准请求）。
"""
from types import SimpleNamespace

from src.llm.client import LLMClient


def _client(**kw):
    c = LLMClient(api_key="k", base_url="http://unit.test", model="m", **kw)
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        msg = SimpleNamespace(content="hi", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    c._client.chat.completions.create = fake_create
    return c, captured


def _fake_stream(**kwargs):
    return iter(())  # 空 chunk 流即可（stream_chat 只是 for 消费）


def test_default_omits_max_tokens_and_extra_body():
    c, cap = _client()
    c.chat([{"role": "user", "content": "hi"}])
    assert "max_tokens" not in cap, "默认构造不得再隐式携带 2000 上限"
    assert cap["extra_body"] == {}


def test_instance_max_tokens_sent():
    c, cap = _client(max_tokens=28000)
    c.chat([{"role": "user", "content": "hi"}])
    assert cap["max_tokens"] == 28000


def test_per_call_max_tokens_overrides():
    c, cap = _client(max_tokens=28000)
    c.chat([{"role": "user", "content": "hi"}], max_tokens=7)
    assert cap["max_tokens"] == 7


def test_per_call_extra_body_replaces_instance():
    c, cap = _client(
        extra_body={"chat_template_kwargs": {"enable_thinking": True}})
    c.chat([{"role": "user", "content": "hi"}], extra_body={})
    assert cap["extra_body"] == {}, "调用级 {} 应整体替换实例值（不发模型专属 kwarg）"

    c2, cap2 = _client(
        extra_body={"chat_template_kwargs": {"enable_thinking": True}})
    c2.chat([{"role": "user", "content": "hi"}])  # 不传 → 用实例值
    assert cap2["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}


def test_invoke_simple_passes_temperature_and_extra_body():
    c, cap = _client(temperature=0.7)
    c.invoke_simple("prompt", temperature=0, extra_body={})
    assert cap["temperature"] == 0
    assert cap["extra_body"] == {}


def test_stream_chat_same_semantics(monkeypatch):
    c = LLMClient(api_key="k", base_url="http://unit.test", model="m",
                  extra_body={"chat_template_kwargs": {"enable_thinking": True}})
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_stream()

    c._client.chat.completions.create = fake_create
    list(c.stream_chat([{"role": "user", "content": "hi"}],
                       temperature=0, extra_body={}))
    assert "max_tokens" not in captured
    assert captured["temperature"] == 0
    assert captured["extra_body"] == {}


# ════════════════════════════════════════════════════════════════
# llm 插件 cancel_event 接线（P2-8 防线 C 的 kernel 路径补全）
# ════════════════════════════════════════════════════════════════

def test_llm_plugin_stream_carries_cancel_event(monkeypatch):
    """LlmService.stream(cancel_event=...) 必须写进 StreamRequest 并透传。"""
    import threading

    from src.plugins.llm_plugin import LlmService

    captured = {}

    def fake_raw_stream(client, req):
        captured["cancel_event"] = req.cancel_event
        return iter(())

    monkeypatch.setattr(LlmService, "_raw_stream", staticmethod(fake_raw_stream))

    svc = LlmService()  # 未 start（无 ctx）→ passthrough 兜底直接 _raw_stream
    evt = threading.Event()
    list(svc.stream([{"role": "user", "content": "hi"}], cancel_event=evt))
    assert captured["cancel_event"] is evt

    # 缺省 None（不传）：请求携带 None，底层自建事件
    captured.clear()
    list(svc.stream([{"role": "user", "content": "hi"}]))
    assert captured["cancel_event"] is None


def test_llm_plugin_default_listener_passes_cancel_event(monkeypatch):
    """默认监听器把 req.cancel_event 接进 stream_with_hard_timeout。

    背景：kernel 路径下走本插件的默认监听器（agent_v3 的同名监听器
    不注册），此前 _raw_stream 恒不传 cancel_event——agent 预置的
    stream_cancel 被丢弃，超时/停止后 worker 继续把流读完。
    """
    import threading

    import src.agent.llm_stream as llm_stream_mod
    from src.plugins.llm_plugin import LlmService, StreamRequest

    captured = {}

    def fake_sht(client, messages, tools=None, timeout_s=120.0,
                 temperature=None, max_tokens=None, cancel_event=None):
        captured["cancel_event"] = cancel_event
        return iter(())

    monkeypatch.setattr(llm_stream_mod, "stream_with_hard_timeout", fake_sht)

    svc = LlmService()
    evt = threading.Event()
    svc._default_stream_listener(
        StreamRequest(messages=[{"role": "user", "content": "hi"}],
                      cancel_event=evt),
        lambda: None,
    )
    assert captured["cancel_event"] is evt
