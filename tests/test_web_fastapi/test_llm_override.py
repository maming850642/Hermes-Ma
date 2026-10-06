"""HermesAgentV3 每请求 LLM 参数覆盖测试。

T6/T7 后语义：stream_invoke 的 llm_override 形参保留但被忽略（兼容旧调用方）；
worker prefs 经 set_llm_params(temperature, max_tokens) 注入，
_get_llm_client 按覆盖值构造客户端，覆盖变更作废旧客户端。
用 mock 避免真实 LLM 调用。
"""
import inspect

from src.storage import paths


def test_stream_invoke_accepts_llm_override_param():
    """stream_invoke 接受 llm_override 可选参数且默认 None（兼容形参，被忽略）。"""
    from src.agent.agent_v3 import HermesAgentV3
    sig = inspect.signature(HermesAgentV3.stream_invoke)
    assert "llm_override" in sig.parameters
    assert sig.parameters["llm_override"].default is None


def test_set_llm_params_overrides_client(tmp_path, monkeypatch):
    """set_llm_params 后 _get_llm_client 按覆盖值构造 LLMClient。"""
    paths.set_data_root(tmp_path)
    try:
        from src.agent.agent_v3 import HermesAgentV3

        agent = HermesAgentV3.__new__(HermesAgentV3)
        from config import get_settings
        agent.settings = get_settings()
        agent._llm_client = None
        agent._llm_temperature = None
        agent._llm_max_tokens = None

        agent.set_llm_params(temperature=0.3, max_tokens=777)
        assert agent._llm_temperature == 0.3
        assert agent._llm_max_tokens == 777

        captured = {}

        class FakeLLMClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        import src.agent.agent_v3 as v3mod
        monkeypatch.setattr(v3mod, "LLMClient", FakeLLMClient)
        agent._get_llm_client()
        assert captured.get("temperature") == 0.3
        assert captured.get("max_tokens") == 777
    finally:
        paths.set_data_root(None)


def test_set_llm_params_invalidates_old_client(tmp_path):
    """覆盖参数变更 → 已构造的旧客户端作废（按新参数重建）。"""
    paths.set_data_root(tmp_path)
    try:
        from src.agent.agent_v3 import HermesAgentV3

        agent = HermesAgentV3.__new__(HermesAgentV3)
        agent._llm_client = object()  # 假装已有客户端
        agent._llm_temperature = None
        agent._llm_max_tokens = None

        agent.set_llm_params(temperature=0.9)
        assert agent._llm_client is None
        assert agent._llm_temperature == 0.9
    finally:
        paths.set_data_root(None)
