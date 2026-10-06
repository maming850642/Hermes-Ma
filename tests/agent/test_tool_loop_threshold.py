"""
验证 _detect_tool_loop 在不同 tool_loop_threshold 配置下的行为。

不实例化完整 agent（避免依赖 LLM/Qdrant），而是构造一个最小桩对象，
把 _detect_tool_loop 作为未绑定函数调用，self 只需带 settings 属性。
消息为 OpenAI dict（tool_calls 为 OpenAI function 格式）。

覆盖场景：
  1. threshold=5（新默认），3 次相同调用 → 不应触发（原硬编码 3 会误杀）
  2. threshold=5，5 次相同调用 → 触发
  3. threshold=0 → 关闭检测，任意次数都不触发
  4. config 缺失该键 → getattr 兜底为 3，3 次相同触发
  5. 中间穿插纯文本 assistant 消息 → 重置窗口，不触发
  6. P3-5 三方一致锁：出厂模板（config.example.yaml）、agent 注释、
     测试桩三方阈值必须同为 5
"""

import json
from pathlib import Path

from src.agent.agent_v3 import HermesAgentV3

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def make_tool_msg(name="read_file", args=None):
    """构造一个带 tool_calls 的 assistant dict（OpenAI 格式）。"""
    if args is None:
        args = {"path": "/workspace/a.txt"}
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "x",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        }],
    }


def make_text_msg(text="好的"):
    """构造一个纯文本 assistant dict（无 tool_calls）。"""
    return {"role": "assistant", "content": text}


class _Cfg:
    """模拟 settings 对象（self.settings），只暴露 tool_loop_threshold。"""

    def __init__(self, threshold):
        self.tool_loop_threshold = threshold


class _NoThresholdCfg:
    """模拟旧 config.yaml 未更新该键的情况（无 tool_loop_threshold 属性）。"""
    pass


class _AgentStub:
    """模拟 agent（self），带 settings 属性。"""

    def __init__(self, cfg):
        self.settings = cfg


def run_detect(cfg, messages):
    """调用未绑定方法，传入桩 self。cfg 包成 _AgentStub。"""
    return HermesAgentV3._detect_tool_loop(_AgentStub(cfg), messages)


def test_threshold5_three_same_not_triggered():
    """场景 1: threshold=5，3 次相同 → 不触发（旧硬编码 3 会误杀，新默认救活）。"""
    cfg5 = _Cfg(5)
    msgs = [make_tool_msg(), make_tool_msg(), make_tool_msg()]
    assert run_detect(cfg5, msgs) is False


def test_threshold5_five_same_triggered():
    """场景 2: threshold=5，5 次相同 → 触发。"""
    cfg5 = _Cfg(5)
    msgs = [make_tool_msg()] * 5
    assert run_detect(cfg5, msgs) is True


def test_threshold0_disables_detection():
    """场景 3: threshold=0 → 关闭检测。"""
    cfg0 = _Cfg(0)
    msgs = [make_tool_msg()] * 10
    assert run_detect(cfg0, msgs) is False


def test_missing_config_falls_back_to_3():
    """场景 4: 配置缺失 → getattr 兜底为 3，3 次相同触发、2 次不触发。"""
    cfg_old = _NoThresholdCfg()
    assert run_detect(cfg_old, [make_tool_msg()] * 3) is True
    assert run_detect(cfg_old, [make_tool_msg()] * 2) is False


def test_interleaved_text_resets_window():
    """场景 5: 中间穿插纯文本 → 重置窗口，不触发。"""
    cfg5 = _Cfg(5)
    msgs = [make_tool_msg(), make_tool_msg(), make_text_msg(), make_tool_msg(), make_tool_msg()]
    assert run_detect(cfg5, msgs) is False


def test_same_tool_different_args_not_triggered():
    """同工具不同参数 → 不构成循环。"""
    cfg5 = _Cfg(5)
    msgs = [
        make_tool_msg(args={"path": "/a"}),
        make_tool_msg(args={"path": "/b"}),
        make_tool_msg(args={"path": "/c"}),
        make_tool_msg(args={"path": "/d"}),
        make_tool_msg(args={"path": "/e"}),
    ]
    assert run_detect(cfg5, msgs) is False


def test_alternating_two_tools_triggered():
    """交替型死循环：A/B/A/B… 永远凑不齐"连续全同"，但大窗口内唯一
    签名数不再增长（≤2）→ 触发。实测线上 compact/write_todos 交替
    烧掉 14 轮 LLM 调用才被旧规则逮住。"""
    cfg3 = _Cfg(3)
    a = make_tool_msg(name="compact_conversation", args={})
    b = make_tool_msg(name="write_todos", args={"todos": [{"id": "1", "content": "x"}]})
    msgs = [a, b] * 5  # 10 条 → 收集窗口 9（threshold*3）
    assert run_detect(cfg3, msgs) is True


def test_alternating_below_wide_window_not_triggered():
    """交替对数不足大窗口（threshold*3）→ 不触发，留正常交替空间。"""
    cfg3 = _Cfg(3)
    a = make_tool_msg(name="compact_conversation", args={})
    b = make_tool_msg(name="write_todos", args={"todos": [{"id": "1", "content": "x"}]})
    msgs = [a, b] * 3  # 6 条 < 9
    assert run_detect(cfg3, msgs) is False


def test_normal_varied_work_not_triggered():
    """正常多样任务：窗口内唯一签名数 > 2 → 不误伤。"""
    cfg3 = _Cfg(3)
    msgs = [
        make_tool_msg(name="read_file", args={"path": "/a"}),
        make_tool_msg(name="ls", args={"path": "/"}),
        make_tool_msg(name="web_search", args={"query": "x"}),
        make_tool_msg(name="read_file", args={"path": "/b"}),
        make_tool_msg(name="edit_file", args={"path": "/a"}),
        make_tool_msg(name="write_todos", args={"todos": [{"id": "1"}]}),
        make_tool_msg(name="read_file", args={"path": "/c"}),
        make_tool_msg(name="bash", args={"command": "ls"}),
        make_tool_msg(name="write_todos", args={"todos": [{"id": "1", "status": "in_progress"}]}),
    ]
    assert run_detect(cfg3, msgs) is False


def test_example_config_and_agent_comment_agree_on_5():
    """P3-5 三方一致锁：出厂模板 config.example.yaml 的 tool_loop_threshold
    必须是 5，agent_v3 注释声称的"出厂值 5"必须与之一致——出厂即 3 会偏严
    误杀正常任务；三方（模板/注释/本文件场景 1-2 的 _Cfg(5) 桩）漂移即红。"""
    import yaml

    cfg = yaml.safe_load(
        (_PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8")
    )
    assert cfg["tool_loop_threshold"] == 5, (
        "config.example.yaml 的 tool_loop_threshold 漂移——"
        "同步更新 agent_v3._detect_tool_loop 注释与本文件场景 1/2 的桩值"
    )

    agent_src = (_PROJECT_ROOT / "src" / "agent" / "agent_v3.py").read_text(
        encoding="utf-8"
    )
    assert "出厂值 5" in agent_src, (
        "agent_v3._detect_tool_loop 的阈值注释与出厂模板不一致"
    )
