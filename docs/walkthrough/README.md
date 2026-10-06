# Hermes-Ma 代码梳理（Walkthrough）

> **⚠️ 历史快照**：本系列写作于 LangChain 时代——文中 LangChain BaseTool / @tool 装饰器 / adapters.py 等
> 已于 2026-08-15（T7）随 langgraph/langchain 全量退役，现行架构为手写 ReAct + ToolSpec（src/tools/）。
> 现状以根 README 与 docs/architecture.md 为准，本文仅作演进史料。

逐模块吃透 Hermes-Ma，重点讲「为什么这么写」的动机与权衡，而非走马观花。

## 阅读顺序

| 章 | 模块 | 文件 | 核心看点 |
|----|------|------|---------|
| [1](01-agent-graph.md) | Agent 编排核心 | `src/agent/graph.py` | StateGraph 拓扑、流式同步阻塞隔离、contextvars 跨线程、HITL 中断检测、ReAct 循环检测 |
| [2](02-agent-tools.md) | 工具调度核心 | `src/agent/tools_registry.py` | ResultHandler 注册表、串行/并发策略、interrupt 透传、ThreadPoolExecutor 死锁修复 |
| [3](03-agent-context.md) | 上下文管理组 | `context.py` + `memory_orch.py` + `tool_result.py` + `session_lifecycle.py` + `token_counter.py` + `context_window.py` + `multimodal.py` | 消息构建防线、三条压缩路径、token 计数、上下文窗口探测、多模态 |
| [4](04-memory.md) | 记忆层 | `src/memory/`（8 文件） | 纯文件后端 FileMemoryStore、关键词检索、三层架构（Store/Extractor/Decider）、ADD/UPDATE/DELETE/NOOP 决策 |
| [5](05-tools-impl.md) | 工具实现层 | `src/tools/`（12 文件） | virtual_fs 路径穿越防护 + per-user 分层、run_shell 纵深防御、web_fetch SSRF 防护 + 四级降级链 |
| [6](06-mcp.md) | MCP 客户端 | `src/mcp/`（3 文件） | 异步/同步桥接、per-server 锁、JSON Schema→Pydantic |
| [7](07-entry-config.md) | 入口与配置 | `cli.py` + `web_fastapi/` + `config.py` + `state.py` | 事件消费+Live 渲染、自动分段、HITL 双层循环、Web 多进程架构 |
| [8](08-crosscutting-appendix.md) | 横切关注点 | `prompts.py` + `skills.py` + `health.py` + `logging_config.py` + `exceptions.py` | System Prompt 双模板工程、技能注册、分层日志、精简后异常体系 |

## 每章统一结构

```
0. 一句话定位
1. 模块全景（文件清单 + 职责矩阵 + 行数规模）
2. 架构与数据流（图 + 主干调用链）
3. 逐文件/逐段深潜（关键代码走读 + 行号 + 「为什么这么写」动机）
4. 设计权衡总结（优缺点、技术债）
```

## 阅读建议

- **初学者**：按 1→2→3→4 顺序读，这是 Agent 的「脑-手-补给-记忆」主线
- **想改某个模块**：直接跳到对应章节，每章自包含
- **想了解某个设计**：看每章「设计权衡总结」和「为什么这么写」小节
- **想看基础设施**：第 8 章横向收口五个横切模块（prompts / skills / health / logging / exceptions）
