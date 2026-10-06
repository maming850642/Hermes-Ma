"""MemoryExtractor 测试，mock LLM 响应。"""
import json
from unittest.mock import MagicMock

from src.memory.extractor import MemoryExtractor


def _make_extractor(mock_response: str) -> MemoryExtractor:
    """绕过 __init__（避免真实 LLM 连接），直接注入 mock。"""
    ext = MemoryExtractor.__new__(MemoryExtractor)
    ext.llm = MagicMock()
    ext.llm.invoke_simple = MagicMock(return_value=mock_response)
    return ext


def test_extract_from_session():
    ext = _make_extractor(json.dumps({"facts": ["用户在做LangGraph项目", "项目用Qdrant"]}, ensure_ascii=False))
    conv = "用户: 我在用LangGraph\n助手: 不错\n用户: 配Qdrant做记忆"
    facts = ext.extract_from_session(conv)
    assert len(facts) == 2
