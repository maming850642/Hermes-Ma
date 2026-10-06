"""
M2.2a WakerFlow DSL 解析层测试。

覆盖：
1. parse_flow —— 合法 YAML 各节点类型 / 各类非法 YAML 报错
2. render    —— 模板插值正常/多级/缺失键/非字符串值转 JSON
3. validate_inputs —— required 缺失 / 默认值填充 / enum 校验 / 类型校验
"""
import pytest

from src.wakerflow import (
    ActionConfig,
    AskUserConfig,
    FlowParseError,
    FlowSpec,
    InputField,
    StepNode,
    parse_flow,
    render,
)
from src.wakerflow.template import TemplateError


# ============================================
# 合法的 DSL 样例（覆盖 5 种节点类型）
# ============================================
SAMPLE_YAML = """
name: repo-audit
description: 仓库风险巡检
inputs:
  repo_path:
    type: string
    required: true
  mode:
    type: string
    default: quick
    enum: [quick, deep]

steps:
  - id: scan
    worker: scanner
    task: "扫描 {{inputs.repo_path}}"

  - id: analyze
    parallel:
      - id: research
        worker: researcher
        task: "调研 {{steps.scan.result}}"
      - id: critique
        worker: critic
        task: "挑刺 {{steps.scan.result}}"

  - id: approve
    ask_user:
      question: "是否发布？"
      options:
        - label: 发布
          value: publish
        - label: 打回
          value: reject
      timeout: 86400
      default: reject

  - id: notify
    if: "{{steps.approve.answer}} == publish"
    action:
      method: POST
      url: https://hooks.example.com/notify
      body: "findings={{steps.analyze}}"

  - id: refine
    pipeline:
      - id: rewrite
        worker: rewriter
        task: "改写 {{steps.analyze.research.result}}"
      - id: proofread
        worker: proofreader
        task: "校对"

returns:
  summary: "{{steps.refine.result}}"
  approved: "{{steps.approve.answer}}"
"""


# ============================================
# parse_flow: 合法解析
# ============================================
def test_parse_flow_ok_basic():
    spec = parse_flow(SAMPLE_YAML)
    assert isinstance(spec, FlowSpec)
    assert spec.name == "repo-audit"
    assert spec.description == "仓库风险巡检"
    assert spec.raw_yaml == SAMPLE_YAML


def test_parse_flow_ok_inputs():
    spec = parse_flow(SAMPLE_YAML)
    assert len(spec.inputs) == 2
    by_name = {f.name: f for f in spec.inputs}
    assert by_name["repo_path"].required is True
    assert by_name["repo_path"].type == "string"
    assert by_name["mode"].default == "quick"
    assert by_name["mode"].enum == ["quick", "deep"]


def test_parse_flow_ok_step_types():
    spec = parse_flow(SAMPLE_YAML)
    by_id = {s.id: s for s in spec.steps}
    # worker
    assert by_id["scan"].node_kind() == "worker"
    assert by_id["scan"].worker == "scanner"
    # parallel
    assert by_id["analyze"].node_kind() == "parallel"
    assert {c.id for c in by_id["analyze"].parallel} == {"research", "critique"}
    # ask_user
    assert by_id["approve"].node_kind() == "ask_user"
    assert isinstance(by_id["approve"].ask_user, AskUserConfig)
    assert len(by_id["approve"].ask_user.options) == 2
    assert by_id["approve"].ask_user.default == "reject"
    # action
    assert by_id["notify"].node_kind() == "action"
    assert isinstance(by_id["notify"].action, ActionConfig)
    assert by_id["notify"].action.url.endswith("/notify")
    assert by_id["notify"].if_cond == "{{steps.approve.answer}} == publish"
    # pipeline
    assert by_id["refine"].node_kind() == "pipeline"
    assert [c.id for c in by_id["refine"].pipeline] == ["rewrite", "proofread"]


def test_step_ids_and_find_step():
    spec = parse_flow(SAMPLE_YAML)
    ids = spec.step_ids()
    # 含 parallel / pipeline 内部
    assert "research" in ids
    assert "critique" in ids
    assert "rewrite" in ids
    assert "proofread" in ids
    # 全局唯一
    assert len(ids) == len(set(ids))

    found = spec.find_step("research")
    assert found is not None
    assert found.worker == "researcher"

    # 递归 find
    assert spec.find_step("not-exists") is None


def test_returns_captured():
    spec = parse_flow(SAMPLE_YAML)
    assert spec.returns["summary"] == "{{steps.refine.result}}"
    assert spec.returns["approved"] == "{{steps.approve.answer}}"


# ============================================
# parse_flow: 各类非法 YAML
# ============================================
def test_parse_flow_bad_yaml_syntax():
    with pytest.raises(FlowParseError, match="YAML 语法"):
        parse_flow("name: x\n  - bad: indent: [unclosed")


def test_parse_flow_empty():
    with pytest.raises(FlowParseError):
        parse_flow("")


def test_parse_flow_missing_name():
    with pytest.raises(FlowParseError, match="name"):
        parse_flow("description: hi\nsteps: []\n")


def test_parse_flow_bad_name():
    # 含空格、点号、过长
    cases = [
        "name: 'has space'\ndescription: x\nsteps: []\n",
        "name: 'a.b'\ndescription: x\nsteps: []\n",
        "name: '" + "x" * 65 + "'\ndescription: x\nsteps: []\n",
    ]
    for c in cases:
        with pytest.raises(FlowParseError, match="name"):
            parse_flow(c)


def test_parse_flow_step_missing_id():
    yaml = """
name: x
steps:
  - worker: w
    task: t
"""
    with pytest.raises(FlowParseError, match="id"):
        parse_flow(yaml)


def test_parse_flow_dup_id_top_level():
    yaml = """
name: x
steps:
  - id: a
    worker: w
    task: t
  - id: a
    worker: w
    task: t2
"""
    with pytest.raises(FlowParseError, match="重复"):
        parse_flow(yaml)


def test_parse_flow_dup_id_nested():
    # parallel 内的 id 与顶层重复
    yaml = """
name: x
steps:
  - id: a
    worker: w
    task: t
  - id: b
    parallel:
      - id: a
        worker: w
        task: t2
"""
    with pytest.raises(FlowParseError, match="重复"):
        parse_flow(yaml)


def test_parse_flow_multiple_node_types():
    yaml = """
name: x
steps:
  - id: a
    worker: w
    task: t
    action:
      url: https://e.com
"""
    with pytest.raises(FlowParseError, match="多种"):
        parse_flow(yaml)


def test_parse_flow_no_node_type():
    yaml = """
name: x
steps:
  - id: a
    task: t
"""
    with pytest.raises(FlowParseError, match="节点类型"):
        parse_flow(yaml)


def test_parse_flow_worker_missing_task():
    yaml = """
name: x
steps:
  - id: a
    worker: w
"""
    with pytest.raises(FlowParseError, match="task"):
        parse_flow(yaml)


def test_parse_flow_parallel_empty():
    yaml = """
name: x
steps:
  - id: a
    parallel: []
"""
    with pytest.raises(FlowParseError, match="parallel"):
        parse_flow(yaml)


def test_parse_flow_pipeline_empty():
    yaml = """
name: x
steps:
  - id: a
    pipeline: []
"""
    with pytest.raises(FlowParseError, match="pipeline"):
        parse_flow(yaml)


def test_parse_flow_ask_user_no_options():
    yaml = """
name: x
steps:
  - id: a
    ask_user:
      question: q?
      options: []
"""
    with pytest.raises(FlowParseError, match="option"):
        parse_flow(yaml)


def test_parse_flow_action_no_url():
    yaml = """
name: x
steps:
  - id: a
    action:
      method: POST
"""
    with pytest.raises(FlowParseError, match="url"):
        parse_flow(yaml)


def test_parse_flow_bad_step_reference():
    # 引用了不存在的 step id
    yaml = """
name: x
steps:
  - id: a
    worker: w
    task: "use {{steps.ghost.result}}"
"""
    with pytest.raises(FlowParseError, match="ghost"):
        parse_flow(yaml)


def test_parse_flow_bad_step_reference_in_returns():
    yaml = """
name: x
steps:
  - id: a
    worker: w
    task: t
returns:
  out: "{{steps.ghost.result}}"
"""
    with pytest.raises(FlowParseError, match="ghost"):
        parse_flow(yaml)


def test_parse_flow_ok_reference_in_pipeline():
    # pipeline 内引用 parallel 父节点的兄弟 id —— 应通过（依赖校验只看存在性）
    yaml = """
name: x
steps:
  - id: a
    parallel:
      - id: a1
        worker: w
        task: t1
      - id: a2
        worker: w
        task: "{{steps.a1.result}}"
"""
    spec = parse_flow(yaml)
    assert spec.find_step("a2").task == "{{steps.a1.result}}"


def test_parse_flow_bad_input_type():
    yaml = """
name: x
inputs:
  flag:
    type: boolish
steps: []
"""
    with pytest.raises(FlowParseError, match="type"):
        parse_flow(yaml)


def test_parse_flow_bad_default_enum():
    yaml = """
name: x
inputs:
  m:
    type: string
    default: foo
    enum: [a, b]
steps: []
"""
    with pytest.raises(FlowParseError, match="default"):
        parse_flow(yaml)


def test_parse_flow_bad_ask_user_default():
    yaml = """
name: x
steps:
  - id: a
    ask_user:
      question: q?
      options:
        - label: A
          value: a
      default: not_in
"""
    with pytest.raises(FlowParseError, match="default"):
        parse_flow(yaml)


# ============================================
# render: 模板插值
# ============================================
def test_render_simple():
    ctx = {"inputs": {"repo_path": "hermes_ma"}}
    out = render("扫描 {{inputs.repo_path}}", ctx)
    assert out == "扫描 hermes_ma"


def test_render_multilevel():
    ctx = {
        "steps": {
            "analyze": {
                "research": {"result": "OK"},
            }
        }
    }
    out = render("{{steps.analyze.research.result}}", ctx)
    assert out == "OK"


def test_render_no_placeholder():
    ctx = {"inputs": {"x": "y"}}
    assert render("plain text", ctx) == "plain text"


def test_render_multiple_occurrences():
    ctx = {"inputs": {"x": "v"}}
    assert render("a {{inputs.x}} b {{inputs.x}} c", ctx) == "a v b v c"


def test_render_missing_key_raises():
    ctx = {"inputs": {}}
    with pytest.raises(TemplateError, match="repo_path"):
        render("{{inputs.repo_path}}", ctx)


def test_render_dict_to_json():
    ctx = {
        "steps": {
            "analyze": {"research": {"result": "R"}, "critique": {"result": "C"}}
        }
    }
    out = render("{{steps.analyze}}", ctx)
    # dict → JSON 字符串
    import json
    parsed = json.loads(out)
    assert parsed == {"research": {"result": "R"}, "critique": {"result": "C"}}


def test_render_list_to_json():
    ctx = {"inputs": {"tags": ["a", "b"]}}
    out = render("[{{inputs.tags}}]", ctx)
    assert out == '[["a", "b"]]'


def test_render_number_to_json():
    ctx = {"inputs": {"n": 42}}
    assert render("count={{inputs.n}}", ctx) == "count=42"


def test_render_none_to_empty():
    ctx = {"inputs": {"x": None}}
    assert render("[{{inputs.x}}]", ctx) == "[]"


def test_render_non_string_template_passthrough():
    # caller 直接把 dict 当 template 传时（如 returns 字段），返回原值
    val = {"k": "v"}
    assert render(val, {}) is val


def test_render_path_into_list_index():
    # 防御性：通过数字索引访问 list
    ctx = {"inputs": {"tags": ["first", "second"]}}
    out = render("{{inputs.tags.0}}", ctx)
    # tags.0 取到字符串 "first"，作为字符串渲染（不加引号）
    assert out == "first"


def test_render_spaces_around_path():
    ctx = {"inputs": {"x": "v"}}
    assert render("{{  inputs.x  }}", ctx) == "v"


# ============================================
# validate_inputs
# ============================================
def _spec_with_inputs(*fields: InputField) -> FlowSpec:
    return FlowSpec(name="t", inputs=list(fields))


def test_validate_inputs_ok():
    spec = _spec_with_inputs(
        InputField("repo", type="string", required=True),
        InputField("mode", type="string", default="quick", enum=["quick", "deep"]),
    )
    out = spec.validate_inputs({"repo": "hermes_ma"})
    assert out == {"repo": "hermes_ma", "mode": "quick"}


def test_validate_inputs_missing_required():
    spec = _spec_with_inputs(InputField("repo", type="string", required=True))
    with pytest.raises(ValueError, match="repo"):
        spec.validate_inputs({})


def test_validate_inputs_extra_key_rejected():
    spec = _spec_with_inputs(InputField("a", type="string"))
    with pytest.raises(ValueError, match="未声明"):
        spec.validate_inputs({"a": "x", "unknown": "y"})


def test_validate_inputs_enum_violation():
    spec = _spec_with_inputs(
        InputField("mode", type="string", default="quick", enum=["quick", "deep"])
    )
    with pytest.raises(ValueError, match="enum"):
        spec.validate_inputs({"mode": "full"})


def test_validate_inputs_type_violation_string():
    spec = _spec_with_inputs(InputField("a", type="string", required=False))
    with pytest.raises(ValueError, match="string"):
        spec.validate_inputs({"a": 123})


def test_validate_inputs_type_violation_number_bool():
    # bool 不能算 number
    spec = _spec_with_inputs(InputField("n", type="number", required=False))
    with pytest.raises(ValueError, match="number"):
        spec.validate_inputs({"n": True})


def test_validate_inputs_type_violation_boolean():
    spec = _spec_with_inputs(InputField("b", type="boolean", required=False))
    with pytest.raises(ValueError, match="boolean"):
        spec.validate_inputs({"b": "yes"})


def test_validate_inputs_number_int_ok():
    spec = _spec_with_inputs(InputField("n", type="number", required=False))
    out = spec.validate_inputs({"n": 5})
    assert out == {"n": 5}


def test_validate_inputs_default_skips_when_provided():
    spec = _spec_with_inputs(
        InputField("m", type="string", default="quick", enum=["quick", "deep"])
    )
    # 显式提供，不应触发 default
    out = spec.validate_inputs({"m": "deep"})
    assert out == {"m": "deep"}


# ============================================
# find_step / step_ids 边界
# ============================================
def test_step_ids_empty_when_no_steps():
    spec = FlowSpec(name="t")
    assert spec.step_ids() == []
    assert spec.find_step("anything") is None


# ============================================
# schedule 配置字段（M3.2）
# ============================================
def test_parse_schedule_fields():
    """parse_flow 读顶层调度配置字段。"""
    yaml_text = """
name: scheduled-flow
schedule_type: interval
interval_minutes: 30
api_enabled: true
max_runs: 10
steps:
  - id: s1
    worker: w
    task: t
"""
    spec = parse_flow(yaml_text)
    assert spec.schedule_type == "interval"
    assert spec.interval_minutes == 30
    assert spec.api_enabled is True
    assert spec.max_runs == 10
    assert spec.enabled is True  # 默认值
    assert spec.api_token == ""  # 默认值


def test_parse_schedule_default_when_absent():
    """旧 YAML（无 schedule 字段）→ 全部默认值，向后兼容。"""
    yaml_text = """
name: legacy
steps:
  - id: s1
    worker: w
    task: t
"""
    spec = parse_flow(yaml_text)
    assert spec.schedule_type == "none"
    assert spec.enabled is True
    assert spec.interval_minutes == 60
    assert spec.api_enabled is False


def test_parse_schedule_invalid_type():
    """非法 schedule_type → FlowParseError。"""
    yaml_text = """
name: bad
schedule_type: weekly
steps:
  - id: s1
    worker: w
    task: t
"""
    with pytest.raises(FlowParseError, match="schedule_type"):
        parse_flow(yaml_text)


def test_parse_schedule_interval_zero():
    """interval_minutes <= 0 → FlowParseError。"""
    yaml_text = """
name: bad
interval_minutes: 0
steps:
  - id: s1
    worker: w
    task: t
"""
    with pytest.raises(FlowParseError, match="interval_minutes"):
        parse_flow(yaml_text)


def test_parse_schedule_daily_bad_format():
    """daily_at 格式错 → FlowParseError。"""
    yaml_text = """
name: bad
daily_at: "25:99"
steps:
  - id: s1
    worker: w
    task: t
"""
    with pytest.raises(FlowParseError, match="daily_at"):
        parse_flow(yaml_text)
