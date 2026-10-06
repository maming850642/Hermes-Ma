"""MemoryDecider 测试，mock LLM。"""
import json
from unittest.mock import MagicMock

from src.memory.decider import MemoryDecider
from src.memory.models import Hit, Memory


def _make_decider(mock_response: str) -> MemoryDecider:
    """绕过 __init__（避免真实 LLM），注入 mock。"""
    d = MemoryDecider.__new__(MemoryDecider)
    d.llm = MagicMock()
    d.llm.invoke_simple = MagicMock(return_value=mock_response)
    return d


def test_decide_add_when_no_candidates():
    """无候选旧记忆 → 直接 ADD（短路，不调 LLM）。"""
    d = _make_decider("should not be called")
    decisions = d.decide("新事实", [])
    assert len(decisions) == 1
    assert decisions[0].action == "ADD"
    assert decisions[0].content == "新事实"
    assert decisions[0].target_id is None
    # 无候选时不应调 LLM
    d.llm.invoke.assert_not_called()


def test_decide_add_with_candidates():
    """有候选但 LLM 判定为全新 → ADD。"""
    resp = json.dumps({"decisions": [{"action": "ADD", "content": "新事实", "target_id": None}]}, ensure_ascii=False)
    d = _make_decider(resp)
    old = Hit(memory=Memory(id="old1", user_id="u", content="无关内容"), score=0.3)
    decisions = d.decide("新事实", [old])
    assert decisions[0].action == "ADD"
    assert decisions[0].content == "新事实"


def test_decide_update_merges_content():
    """新事实与旧记忆互补 → UPDATE，content 是合并后的文本。"""
    old = Hit(memory=Memory(id="old1", user_id="u", content="用户喜欢Python"), score=0.85)
    resp = json.dumps({"decisions": [{"action": "UPDATE", "content": "用户喜欢Python，主要做数据分析", "target_id": "old1"}]}, ensure_ascii=False)
    d = _make_decider(resp)
    decisions = d.decide("主要做数据分析", [old])
    assert decisions[0].action == "UPDATE"
    assert decisions[0].target_id == "old1"
    assert "数据分析" in decisions[0].content


def test_decide_delete_on_contradiction():
    """直接矛盾 → DELETE。"""
    old = Hit(memory=Memory(id="old1", user_id="u", content="用户喜欢爬山"), score=0.8)
    resp = json.dumps({"decisions": [{"action": "DELETE", "content": "", "target_id": "old1"}]}, ensure_ascii=False)
    d = _make_decider(resp)
    decisions = d.decide("用户不喜欢爬山", [old])
    assert decisions[0].action == "DELETE"
    assert decisions[0].target_id == "old1"


def test_decide_noop_on_identical():
    """完全相同 → NOOP。"""
    old = Hit(memory=Memory(id="old1", user_id="u", content="用户叫小明"), score=0.95)
    resp = json.dumps({"decisions": [{"action": "NOOP", "content": "", "target_id": None}]}, ensure_ascii=False)
    d = _make_decider(resp)
    decisions = d.decide("用户叫小明", [old])
    assert decisions[0].action == "NOOP"


def test_decide_handles_malformed_json():
    """非 JSON → FAIL（与「已记过」的 NOOP 区分，绝不 ADD）。"""
    d = _make_decider("不是JSON")
    old = Hit(memory=Memory(id="old1", user_id="u", content="某内容"), score=0.5)
    decisions = d.decide("新事实", [old])
    assert len(decisions) == 1
    assert decisions[0].action == "FAIL"


def test_decide_handles_empty_content():
    """空 content（thinking 耗尽 token 上限/输出截断的典型产物）→ FAIL。"""
    d = _make_decider("")
    old = Hit(memory=Memory(id="old1", user_id="u", content="某内容"), score=0.5)
    decisions = d.decide("新事实", [old])
    assert decisions[0].action == "FAIL"


def test_decide_repairs_partial_json():
    """截断/畸形 JSON（extract_json 失败）→ parse_partial_json 兜底修复。"""
    resp = '{"decisions": [{"action": "NOOP", "content": ""'  # 未闭合
    d = _make_decider(resp)
    old = Hit(memory=Memory(id="old1", user_id="u", content="用户叫小明"), score=0.9)
    decisions = d.decide("用户叫小明", [old])
    assert decisions[0].action == "NOOP"


def test_decide_fenced_json_ok():
    """带 ```json 围栏的输出可正常解析（extract_json 的正则容忍前后缀）。"""
    inner = json.dumps({"decisions": [{"action": "UPDATE", "content": "合并", "target_id": "old1"}]},
                       ensure_ascii=False)
    d = _make_decider(f"```json\n{inner}\n```")
    old = Hit(memory=Memory(id="old1", user_id="u", content="旧"), score=0.8)
    decisions = d.decide("新", [old])
    assert decisions[0].action == "UPDATE"
    assert decisions[0].target_id == "old1"


def test_decide_empty_decisions_means_noop():
    """模型显式返回空 decisions 数组 = 判定无需变更 → NOOP（历史误为 ADD）。"""
    resp = json.dumps({"decisions": []}, ensure_ascii=False)
    d = _make_decider(resp)
    old = Hit(memory=Memory(id="old1", user_id="u", content="某内容"), score=0.5)
    decisions = d.decide("新事实", [old])
    assert decisions[0].action == "NOOP"


def test_decide_uses_call_level_payload():
    """决策调用必须显式 temperature=0 且 extra_body={}（不带任何模型专属
    kwarg——qwen chat_template_kwargs 已移除）。"""
    d = _make_decider("[]")
    old = Hit(memory=Memory(id="old1", user_id="u", content="x"), score=0.5)
    d.decide("新事实", [old])
    kwargs = d.llm.invoke_simple.call_args.kwargs
    assert kwargs.get("temperature") == 0
    assert kwargs.get("extra_body") == {}


def test_decide_handles_llm_exception():
    """LLM 调用异常 → FAIL（不是「已记过」的 NOOP）。"""
    d = MemoryDecider.__new__(MemoryDecider)
    d.llm = MagicMock()
    d.llm.invoke_simple = MagicMock(side_effect=Exception("API 挂了"))
    old = Hit(memory=Memory(id="old1", user_id="u", content="x"), score=0.5)
    decisions = d.decide("新事实", [old])
    assert decisions[0].action == "FAIL"
