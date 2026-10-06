# Hermes-Ma V3 架构文档集

> **定位**：V3 = 脱离 langchain / langgraph、全部手搓的架构形态。
> 本文档集撰写于 2026-07-13（`toolschema` 分支时期），作为**架构决策的历史参考**：每章回顾当时设计的来龙去脉与踩坑。
>
> - 其中的阶段性描述（langchain 混合态、遗留层并行等）已随后续 Cordis 重构完成退役；项目现状以仓库根 README 与 `docs/cordis/01-rollout.md` 为准。

---

## 阅读顺序

| # | 文件 | 覆盖 | 看点 |
|---|------|------|----------------|
| 01 | [V3架构总览](01-V3架构总览.md) | 全景图 / 三层架构 / V2→V3 演进时间线 / 模块索引 | 全局认识与架构图 |
| 02 | [Tier1手搓引擎](02-Tier1手搓引擎.md) | LLMClient / 自建消息类型 / 双 deadline 硬超时 / ThinkSplitter / json_fix | LLM 集成与流式工程复盘 |
| 03 | [声明式三层工具](03-声明式三层工具.md) | ToolSpec / SideEffects / decide 三层叠加 / 4 种执行器 / YAML 加载 / 动态组装 | 工具系统设计复盘 |
| 04 | [异常式HITL与权限系统](04-异常式HITL与权限系统.md) | InterruptSignal / InterruptSnapshot / InterruptStore / 三层权限 / mode 切换自动放行 | HITL 与权限复盘 |
| 05 | [手写ReAct循环](05-手写ReAct循环.md) | HermesAgentV3 / stream_invoke / 循环检测 / 混合态边界转换 / 子智能体 | Agent 编排复盘 |
| 06 | [流式韧性工程](06-流式韧性工程.md) | stdout 写线程 / per-user 子进程 / IPC 超时 / worker 锁 503 / contextvars 三坑 | 韧性设计复盘（重点章） |
| 07 | [Context与Memory工程](07-Context与Memory工程.md) | 窗口截断 / 三路 compact / 纯文件记忆 / 评分公式 / 原子写 / 会话分桶 | Context / Memory 复盘 |
| 08 | [配置与部署](08-配置与部署.md) | config.yaml 全表 / 双访问 / env 覆盖 / _INT_KEYS / 多进程部署 | 运维与配置 |

---

## V3 一句话定位

V3 架构用 **`openai` SDK + 手写 ReAct 循环 + 异常式 HITL + 声明式三层工具**，取代 `langchain-core` / `langchain-openai` / `langgraph` 三个重依赖。整个 Agent 内核全部自研，仅 ContextManager / compact_messages 还保留对 langchain 消息类型的引用（Phase 5 待清理的混合态边界）。

## Tier 分层模型

```
┌─────────────────────────────────────────────────────────────┐
│                      调用层（CLI / Web）                       │
│         cli.py / web_fastapi/worker_process.py               │
└──────────────────┬──────────────────────────┬───────────────┘
                   │                          │
         ┌─────────▼──────────┐    ┌──────────▼──────────┐
         │  ✅ Tier 1 核心     │    │  ⚠️ Tier 3 混合态   │
         │  手搓引擎层         │    │  （Phase 4 过渡）    │
         │                    │    │                     │
         │  LLMClient         │◄──►│  HermesAgentV3      │
         │  自建消息类型       │    │  （内部 state 仍用   │
         │  stream_with_      │    │   langchain 消息）   │
         │    hard_timeout    │    │                     │
         │  ToolRegistryV3    │    │  ContextManager     │
         │  HITL 三件套        │    │  （顶层 langchain   │
         │  ToolResult        │    │   import，复用不动） │
         │  ThinkSplitter     │    │                     │
         │  json_fix          │    └─────────────────────┘
         └────────┬───────────┘
                  │
        ┌─────────▼──────────────────────────┐
        │  ✅ Tier 2 声明式三层工具           │
        │                                    │
        │  ToolSpec（YAML 定义）              │
        │    ├─ Layer 1: side_effects 标签   │
        │    ├─ Layer 2: 权限模式 decide()   │
        │    └─ Layer 3: 细粒度规则修正器     │
        │                                    │
        │  四种执行器：                       │
        │    Shell / Fs / Python / Mcp       │
        └────────────────────────────────────┘
                  │
        ┌─────────▼──────────┐
        │  遗留层（并行存活）  │
        │  graph.py (LangGraph)│
        │  tools_registry.py   │
        │  @tool 装饰器工具集   │
        └─────────────────────┘
```

状态标注：
- ✅ **纯手搓**——零 langchain/langgraph import，完全独立
- ⚠️ **混合态**——V3 路径组件，但内部仍 import langchain（Phase 5 彻底替换）
- 无标注——遗留层（LangGraph/langchain 旧路径，保留并行）

## 演进时间线（一图看懂为什么有 V3）

```
2026-06-17  commit 117ccd7 ── 单 agent + Qdrant + Mem0 + Streamlit web
2026-06-19  技能系统 / MCP / 配置重构（.env → config.yaml）
2026-06-22  自研 Agent Memory（Store/Extractor/Decider + Qdrant）
2026-06-23~25 分层日志 / sub_agent / 会话分页 / FastAPI Web
2026-06-29  智囊团升级：记忆全文件化（移除 Qdrant/Embedding → FileMemoryStore）
            + 角色系统 + 文件总线 + FastAPI 多进程（每用户 worker 子进程）
            + 韧性加固（stdout 写线程 + _cancel_event + precheck token 阈值压缩）
            ↑ 以上是 V2（LangGraph 编排）的成熟形态

2026-07-06~09  V3 手搓重构（脱离 langchain-langgraph）  ← 本文档集覆盖
    Phase 1-3  ToolSpec 三层架构 + ToolRegistryV3（并行，零破坏）
    Phase 4    HermesAgentV3 手写 ReAct 循环 + 异常式 HITL
    Phase 5    LLMClient + 自建消息类型 + stream_with_hard_timeout
    Phase 6    （目标）删除 LangGraph 编排 + 清理依赖（进行中）
= 当前形态（toolschema 分支 HEAD）
```

> **为什么从 LangGraph 改成手搓？** 三个驱动：
> 1. **框架行为和文档不一致**——`langgraph.interrupt()` 在 0.2.76 的 stream 里不注入 `__interrupt__`，HITL 静默失效，排查到源码级才定位（见 04 章）。
> 2. **黑盒难调**——LangChain 的 monkeypatch、`RunnableConfig` 透传、`_create_subset_model` 的 KeyError 都需要读框架源码才能绕过。
> 3. **依赖太重**——只用 `langchain_core.messages` + `convert_to_openai_tool` + `parse_partial_json` 三个函数，却拖进整个 langchain-core/langchain-openai/langgraph 三包。
>
> V3 把这三个函数直接手搓替代，换来了：零框架魔法、所有行为可在自己代码里 grep 到、依赖瘦身。

---

## 模块索引（文件 → 章节）

| 模块 / 文件 | 章节 |
|------------|------|
| `src/llm/client.py` | 02 §1 |
| `src/llm/messages.py` | 02 §2 |
| `src/agent/llm_stream.py` | 02 §3 |
| `src/think.py` | 02 §4 |
| `src/llm/json_fix.py` | 02 §5 |
| `src/tools/schema.py` | 03 §1-2 |
| `src/tools/permissions.py` | 03 §3 + 04 |
| `src/tools/executor_base.py` + `executors/*` | 03 §4 |
| `src/tools/loader.py` + `tools/*.yaml` | 03 §5 |
| `src/tools/resolve.py` | 03 §6 |
| `src/tools/context.py` | 03 §7 |
| `src/agent/hitl.py` | 04 §1 |
| `src/agent/registry_v3.py` | 04 §2 |
| `src/agent/mode_guidance.py` | 04 §3 |
| `src/agent/agent_v3.py` | 05 |
| `src/agent/context.py` | 07 §1 |
| `src/memory/file_store.py` | 07 §2 |
| `web_fastapi/worker_process.py` | 06 §1 + 07 §3 |
| `web_fastapi/worker_manager.py` | 06 §2 |
| `web_fastapi/routers/chat.py` + `sse.py` | 06 §3 |
| `config.py` + `config.yaml` | 08 |