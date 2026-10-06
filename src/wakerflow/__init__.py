"""
============================================
wakerflow —— WakerFlow DSL 解析层
============================================
WakerFlow：用 YAML 描述一个多步骤、可编排（parallel/pipeline/ask_user/action）
的工作流，每个 worker 节点引用一个 waker（数字员工）跑完整 HermesAgentV3。

本包分层：
- models.py    数据结构（FlowSpec / StepNode / ...）
- parser.py    YAML → FlowSpec（含完整校验）
- template.py  {{...}} 模板插值
- canvas.py    积木块 JSON ↔ FlowSpec（M3 拖拽编辑器用）
- worker_node.py  子进程入口（M2.1，作为 __main__ 直接跑，不在本门面导出）

本门面只导出 DSL 层符号，方便上层一次性 import。
"""
from src.wakerflow.models import (
    ActionConfig,
    AskUserConfig,
    FlowSpec,
    InputField,
    StepNode,
)
from src.wakerflow.parser import FlowParseError, parse_flow
from src.wakerflow.template import TemplateError, render

__all__ = [
    # 数据模型
    "FlowSpec",
    "StepNode",
    "InputField",
    "AskUserConfig",
    "ActionConfig",
    # 解析
    "parse_flow",
    "FlowParseError",
    # 模板
    "render",
    "TemplateError",
]
