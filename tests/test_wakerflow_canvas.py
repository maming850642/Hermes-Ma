"""
M3 WakerFlow 画布转换层测试。

覆盖：
1. blocks_to_flow —— 各类型块正确转 FlowSpec + 各类非法块报错
2. flow_to_blocks —— FlowSpec 还原块列表
3. 往返幂等：blocks → flow → blocks
4. flow_to_yaml —— FlowSpec → YAML 文本（能被 parse_flow 重新解析）
5. 向后兼容：手写 YAML flow → parse_flow → flow_to_blocks 能渲染
"""
import pytest

from src.wakerflow.canvas import (
    CanvasError,
    blocks_to_flow,
    flow_to_blocks,
    flow_to_yaml,
)
from src.wakerflow.parser import parse_flow
from src.wakerflow.store import FlowStore


# ============================================
# 合法积木块样例（覆盖 5 种节点 + 嵌套）
# ============================================
SAMPLE_BLOCKS = [
    {
        "type": "worker",
        "id": "review",
        "waker": "code-reviewer",
        "task": "审查 {{inputs.target}}",
        "tools": ["bash", "read_file"],
        "permission_mode": "full_access",
    },
    {
        "type": "parallel",
        "id": "dual_review",
        "children": [
            {
                "type": "worker",
                "id": "logic",
                "waker": "code-reviewer",
                "task": "审查逻辑 {{inputs.target}}",
                "tools": None,
                "permission_mode": None,
            },
            {
                "type": "worker",
                "id": "style",
                "waker": "code-reviewer",
                "task": "审查风格 {{inputs.target}}",
                "tools": None,
                "permission_mode": None,
            },
        ],
    },
    {
        "type": "ask_user",
        "id": "confirm",
        "question": "生成汇总报告？",
        "options": [
            {"label": "生成", "value": "yes"},
            {"label": "跳过", "value": "no"},
        ],
        "timeout": 300,
        "default": "yes",
    },
    {
        "type": "worker",
        "id": "report",
        "if": "{{steps.confirm.answer}} == yes",
        "waker": "code-reviewer",
        "task": "汇总报告",
        "tools": None,
        "permission_mode": None,
    },
]


SAMPLE_INPUTS = [
    {"name": "target", "type": "string", "required": True},
    {"name": "mode", "type": "string", "default": "quick", "enum": ["quick", "deep"]},
]


# ============================================
# blocks_to_flow：合法路径
# ============================================
class TestBlocksToFlow:
    def test_basic_conversion(self):
        spec = blocks_to_flow(
            SAMPLE_BLOCKS,
            name="code-review-flow",
            description="代码审查流程",
            inputs=SAMPLE_INPUTS,
            returns={"report": "{{steps.report.result}}"},
        )
        assert spec.name == "code-review-flow"
        assert spec.description == "代码审查流程"
        assert len(spec.steps) == 4
        assert len(spec.inputs) == 2
        assert spec.returns == {"report": "{{steps.report.result}}"}

    def test_worker_step_fields(self):
        spec = blocks_to_flow(SAMPLE_BLOCKS, name="t")
        worker = spec.steps[0]
        assert worker.id == "review"
        assert worker.worker == "code-reviewer"
        assert worker.task == "审查 {{inputs.target}}"
        assert worker.tools == ["bash", "read_file"]
        assert worker.permission_mode == "full_access"
        assert worker.node_kind() == "worker"

    def test_parallel_step_with_children(self):
        spec = blocks_to_flow(SAMPLE_BLOCKS, name="t")
        parallel = spec.steps[1]
        assert parallel.id == "dual_review"
        assert parallel.node_kind() == "parallel"
        assert len(parallel.parallel) == 2
        assert parallel.parallel[0].id == "logic"
        assert parallel.parallel[1].id == "style"

    def test_ask_user_step(self):
        spec = blocks_to_flow(SAMPLE_BLOCKS, name="t")
        ask = spec.steps[2]
        assert ask.id == "confirm"
        assert ask.node_kind() == "ask_user"
        assert ask.ask_user.question == "生成汇总报告？"
        assert len(ask.ask_user.options) == 2
        assert ask.ask_user.timeout == 300
        assert ask.ask_user.default == "yes"

    def test_if_condition_preserved(self):
        spec = blocks_to_flow(SAMPLE_BLOCKS, name="t")
        report = spec.steps[3]
        assert report.if_cond == "{{steps.confirm.answer}} == yes"

    def test_id_global_unique_across_nesting(self):
        """id 跨层级唯一（含 parallel children）——与 parser 行为一致。"""
        blocks = [
            {"type": "worker", "id": "dup", "waker": "w", "task": "t"},
            {
                "type": "parallel",
                "id": "par",
                "children": [
                    {"type": "worker", "id": "dup", "waker": "w", "task": "t"},
                ],
            },
        ]
        with pytest.raises(CanvasError, match="id 重复"):
            blocks_to_flow(blocks, name="t")

    def test_empty_blocks_list(self):
        """空块列表 → 空 steps（合法，允许空 flow）。"""
        spec = blocks_to_flow([], name="empty")
        assert spec.steps == []


# ============================================
# blocks_to_flow：非法路径
# ============================================
class TestBlocksToFlowErrors:
    def test_missing_type(self):
        with pytest.raises(CanvasError, match="缺少 type"):
            blocks_to_flow([{"id": "x", "waker": "w"}], name="t")

    def test_invalid_type(self):
        with pytest.raises(CanvasError, match="非法 type"):
            blocks_to_flow(
                [{"type": "magic", "id": "x", "waker": "w"}], name="t"
            )

    def test_missing_id(self):
        with pytest.raises(CanvasError, match="缺少 id"):
            blocks_to_flow(
                [{"type": "worker", "waker": "w", "task": "t"}], name="t"
            )

    def test_worker_missing_waker(self):
        with pytest.raises(CanvasError, match="缺少 waker"):
            blocks_to_flow(
                [{"type": "worker", "id": "x", "task": "t"}], name="t"
            )

    def test_worker_missing_task(self):
        with pytest.raises(CanvasError, match="缺少 task"):
            blocks_to_flow(
                [{"type": "worker", "id": "x", "waker": "w"}], name="t"
            )

    def test_parallel_missing_children(self):
        with pytest.raises(CanvasError, match="children"):
            blocks_to_flow([{"type": "parallel", "id": "x"}], name="t")

    def test_parallel_empty_children(self):
        with pytest.raises(CanvasError, match="children"):
            blocks_to_flow(
                [{"type": "parallel", "id": "x", "children": []}], name="t"
            )

    def test_ask_user_missing_question(self):
        with pytest.raises(CanvasError, match="question"):
            blocks_to_flow(
                [{"type": "ask_user", "id": "x", "options": [{"label": "a", "value": "1"}]}],
                name="t",
            )

    def test_ask_user_bad_default(self):
        with pytest.raises(CanvasError, match="default"):
            blocks_to_flow(
                [{
                    "type": "ask_user", "id": "x",
                    "question": "q",
                    "options": [{"label": "a", "value": "yes"}],
                    "default": "nonexistent",
                }],
                name="t",
            )

    def test_action_missing_url(self):
        with pytest.raises(CanvasError, match="url"):
            blocks_to_flow([{"type": "action", "id": "x"}], name="t")

    def test_blocks_not_list(self):
        with pytest.raises(CanvasError, match="须为列表"):
            blocks_to_flow("not a list", name="t")  # type: ignore[arg-type]


# ============================================
# schedule 语义校验（P2-21：canvas 建块复用 parser._parse_schedule）
# ============================================
class TestBlocksToFlowScheduleValidation:
    def test_dirty_schedule_type_rejected(self):
        """schedule_type:"weekly" 此前原样灌入 FlowSpec 入库哑火，现 4xx 拒绝。"""
        with pytest.raises(CanvasError, match="schedule"):
            blocks_to_flow([], name="t", schedule={"schedule_type": "weekly"})

    def test_non_positive_interval_rejected(self):
        with pytest.raises(CanvasError, match="schedule"):
            blocks_to_flow(
                [], name="t",
                schedule={"schedule_type": "interval", "interval_minutes": 0},
            )

    def test_bad_daily_at_rejected(self):
        with pytest.raises(CanvasError, match="schedule"):
            blocks_to_flow(
                [], name="t",
                schedule={"schedule_type": "daily", "daily_at": "9am"},
            )

    def test_valid_schedule_passes(self):
        spec = blocks_to_flow(
            [], name="t",
            schedule={"schedule_type": "interval", "interval_minutes": 30},
        )
        assert spec.schedule_type == "interval"
        assert spec.interval_minutes == 30

    def test_none_values_fall_back_to_defaults(self):
        """schedule 值为 None 的键被过滤，FlowSpec 默认值生效（原有语义）。"""
        spec = blocks_to_flow(
            [], name="t", schedule={"schedule_type": None, "enabled": None},
        )
        assert spec.schedule_type == "none"
        assert spec.enabled is True


# ============================================
# flow_to_blocks：FlowSpec → 块列表
# ============================================
class TestFlowToBlocks:
    def test_roundtrip_all_types(self):
        """blocks → flow → blocks 幂等（核心契约）。"""
        spec1 = blocks_to_flow(
            SAMPLE_BLOCKS, name="t", inputs=SAMPLE_INPUTS,
            returns={"report": "{{steps.report.result}}"},
        )
        blocks_back = flow_to_blocks(spec1)
        # 重新转回 flow 比对（比 dict 相等更稳，过滤 None 默认值差异）
        spec2 = blocks_to_flow(
            blocks_back, name="t", inputs=SAMPLE_INPUTS,
            returns={"report": "{{steps.report.result}}"},
        )
        assert len(spec2.steps) == len(spec1.steps)
        for s1, s2 in zip(spec1.steps, spec2.steps):
            assert s1.id == s2.id
            assert s1.node_kind() == s2.node_kind()
            assert s1.if_cond == s2.if_cond

    def test_worker_block_fields(self):
        spec = blocks_to_flow(SAMPLE_BLOCKS, name="t")
        blocks = flow_to_blocks(spec)
        worker_block = blocks[0]
        assert worker_block["type"] == "worker"
        assert worker_block["id"] == "review"
        assert worker_block["waker"] == "code-reviewer"
        assert worker_block["tools"] == ["bash", "read_file"]
        assert worker_block["permission_mode"] == "full_access"

    def test_parallel_block_children(self):
        spec = blocks_to_flow(SAMPLE_BLOCKS, name="t")
        blocks = flow_to_blocks(spec)
        parallel_block = blocks[1]
        assert parallel_block["type"] == "parallel"
        assert len(parallel_block["children"]) == 2
        assert parallel_block["children"][0]["id"] == "logic"

    def test_if_condition_in_block(self):
        spec = blocks_to_flow(SAMPLE_BLOCKS, name="t")
        blocks = flow_to_blocks(spec)
        report_block = blocks[3]
        assert report_block["if"] == "{{steps.confirm.answer}} == yes"


# ============================================
# flow_to_yaml：FlowSpec → YAML 文本
# ============================================
class TestFlowToYaml:
    def test_yaml_roundtrip_parseable(self):
        """blocks → flow → yaml → parse_flow 能重新解析。"""
        spec = blocks_to_flow(
            SAMPLE_BLOCKS, name="code-review-flow",
            description="测试", inputs=SAMPLE_INPUTS,
            returns={"report": "{{steps.report.result}}"},
        )
        yaml_text = flow_to_yaml(spec)
        assert "name: code-review-flow" in yaml_text

        # 重新解析
        spec2 = parse_flow(yaml_text)
        assert spec2.name == "code-review-flow"
        assert len(spec2.steps) == 4
        assert spec2.steps[1].node_kind() == "parallel"
        assert len(spec2.steps[1].parallel) == 2

    def test_action_in_yaml(self):
        """action 节点的 yaml 往返。"""
        blocks = [{
            "type": "action", "id": "hook",
            "method": "POST", "url": "https://example.com/hook",
            "headers": {"X-Token": "abc"},
            "body": {"text": "{{steps.report.result}}"},
        }]
        spec = blocks_to_flow(blocks, name="t")
        yaml_text = flow_to_yaml(spec)
        spec2 = parse_flow(yaml_text)
        action = spec2.steps[0]
        assert action.node_kind() == "action"
        assert action.action.url == "https://example.com/hook"
        assert action.action.method == "POST"
        assert action.action.headers == {"X-Token": "abc"}

    def test_pipeline_in_yaml(self):
        """pipeline 节点的 yaml 往返。"""
        blocks = [{
            "type": "pipeline", "id": "pipe",
            "children": [
                {"type": "worker", "id": "a", "waker": "w", "task": "do a"},
                {"type": "worker", "id": "b", "waker": "w", "task": "do b after {{steps.a.result}}"},
            ],
        }]
        spec = blocks_to_flow(blocks, name="t")
        yaml_text = flow_to_yaml(spec)
        spec2 = parse_flow(yaml_text)
        assert spec2.steps[0].node_kind() == "pipeline"
        assert len(spec2.steps[0].pipeline) == 2


# ============================================
# 向后兼容：手写 YAML flow 能被 flow_to_blocks 渲染
# ============================================
class TestBackwardCompat:
    def test_handwritten_yaml_renders_to_blocks(self):
        """现有手写 YAML flow → parse → flow_to_blocks 能完整还原。"""
        yaml_text = """
name: legacy-flow
description: 手写的旧 flow
inputs:
  repo:
    type: string
    required: true

steps:
  - id: scan
    worker: scanner
    task: 扫描 {{inputs.repo}}
  - id: decide
    ask_user:
      question: 继续？
      options:
        - {label: 是, value: true}
        - {label: 否, value: false}
      default: true
  - id: act
    if: "{{steps.decide.answer}} == true"
    action:
      url: https://example.com/hook
      method: POST
      body: {result: "{{steps.scan.result}}"}

returns:
  scan: "{{steps.scan.result}}"
"""
        spec = parse_flow(yaml_text)
        blocks = flow_to_blocks(spec)

        assert len(blocks) == 3
        assert blocks[0]["type"] == "worker"
        assert blocks[0]["waker"] == "scanner"
        assert blocks[1]["type"] == "ask_user"
        assert len(blocks[1]["options"]) == 2
        assert blocks[2]["type"] == "action"
        assert blocks[2]["if"] == "{{steps.decide.answer}} == true"

        # 再转回 YAML，能被 parse_flow 解析（完整往返）
        spec2 = blocks_to_flow(
            blocks, name=spec.name, description=spec.description,
            inputs=[{"name": f.name, "type": f.type, "required": f.required,
                     "default": f.default, "enum": f.enum} for f in spec.inputs],
            returns=spec.returns,
        )
        yaml_back = flow_to_yaml(spec2)
        spec3 = parse_flow(yaml_back)
        assert len(spec3.steps) == 3


# ============================================
# FlowStore.save 名称字符集校验（三条创建路径收敛）
# ============================================
class TestFlowNameCharsetAtStore:
    """save 是 name 入库的唯一入口，在此统一 parser._NAME_RE 同款字符集
    校验（^[a-zA-Z0-9_-]{1,64}$）。此前只拒 _ 前缀与 / \\ . ..：
    - blocks 路径 canvas.blocks_to_flow 纯 dataclass 构造绕过 parser；
    - yaml 路径 router 存的顶层 body.name 与 parse_flow 校验的 yaml 内层
      name 可不同。
    引号/尖括号等入库后，前端卡片按钮 onclick 字符串拼接的注入可达。
    """

    @pytest.fixture
    def store(self, tmp_path):
        return FlowStore("u1", workspace_root=str(tmp_path / "ws"))

    def test_blocks_path_injection_name_rejected_at_save(self, store):
        """blocks 模式带注入名：canvas 能构造 spec（名不归它校验），保存被拒。"""
        bad = "x');alert(1)#"
        spec = blocks_to_flow(SAMPLE_BLOCKS, name=bad)
        assert spec.name == bad  # canvas 层现状：不校验 name（缺口在 store 收口）
        with pytest.raises(ValueError, match="非法 flow name"):
            store.save(spec.name, flow_to_yaml(spec))

    def test_yaml_top_level_name_mismatch_rejected(self, store):
        """yaml 模式：内层 name 合法（parse_flow 放行）但顶层 name 非法 → 拒。"""
        inner_legal = "name: inner-flow\nsteps: []\n"
        assert parse_flow(inner_legal).name == "inner-flow"
        with pytest.raises(ValueError, match="非法 flow name"):
            store.save("x');alert(1)#", inner_legal)

    def test_legal_names_save_via_both_paths(self, store):
        """合法名称：yaml 直存与 blocks 产物照常入库。"""
        store.save("yaml-made", "name: yaml-made\nsteps: []\n")
        assert store.get("yaml-made") is not None
        spec = blocks_to_flow(SAMPLE_BLOCKS, name="blocks-made")
        store.save("blocks-made", flow_to_yaml(spec))
        assert store.get("blocks-made") is not None

    def test_max_length_name_accepted(self, store):
        ok = "x" * 64
        store.save(ok, "name: t\nsteps: []\n")
        assert store.get(ok) is not None

    @pytest.mark.parametrize("bad", [
        "",            # 空
        "_lead",       # 下划线前缀（保留给 _approvals 等辅助目录）
        "..",          # 穿越保留字
        "a/b",         # 路径分隔
        "a\\b",        # 路径分隔（Windows）
        "a b",         # 空格
        "名字",         # 非 ASCII
        "x" * 65,      # 超长
    ])
    def test_charset_violations_rejected(self, store, bad):
        with pytest.raises(ValueError, match="非法 flow name"):
            store.save(bad, "name: t\nsteps: []\n")
