"""
============================================
Hermes Agent 包
============================================
V3 架构（声明式三层工具 + 手写 ReAct + 异常式 HITL）。

子模块：
- agent_v3.py: HermesAgentV3 - 手写 ReAct 循环 + 异常式 HITL（唯一引擎）
- registry_v3.py: ToolRegistryV3 - V3 工具执行（三层权限叠加）
- context.py: ContextManager - 上下文管理（OpenAI dict 消息）
- memory_orch.py: MemoryOrchestrator - 记忆编排
- hitl.py: InterruptSignal/InterruptStore - HITL 异常协议
- session_log.py: SessionLog - 事件溯源会话日志
"""

from src.agent.agent_v3 import HermesAgentV3
from src.agent.registry_v3 import ToolRegistryV3
from src.agent.context import ContextManager
from src.agent.memory_orch import MemoryOrchestrator

__all__ = [
    "HermesAgentV3",
    "ToolRegistryV3",
    "ContextManager",
    "MemoryOrchestrator",
]
