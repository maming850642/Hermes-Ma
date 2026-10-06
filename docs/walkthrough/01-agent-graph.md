# 第 1 章 · Agent 编排核心 —— `src/agent/graph.py`

> 本系列目标：逐模块吃透 Hermes-Ma，重点讲「为什么这么写」的动机与权衡，而非走马观花。

---

## 0. 一句话定位

`graph.py` 是整个 Agent 的**中枢神经**：它把 LLM、工具、记忆、上下文压缩组装成一张 LangGraph `StateGraph`，对外只暴露一个 `stream_invoke()` 流式接口。CLI / Web 只管消费它吐出的事件流，完全不用知道图内部怎么走。

如果只能读一个文件来理解 Hermes，就是这个。

---

## 1. 模块全景

| 文件 | 行数 | 角色 |
|------|------|------|
| `src/agent/graph.py` | 1146（截至 2026-07-03） | **本章主角**：StateGraph 构建 + `stream_invoke` 流式入口 + 流式硬超时 + 循环检测 + token 阈值自动压缩 + HITL 中断检测 |

它组合了 3 个子模块（这些是后续章节的内容，本章只看它怎么用它们）：

| 子模块 | 文件 | graph.py 里的用途 |
|--------|------|-------------------|
| `ToolRegistry` | `src/agent/tools_registry.py` | tools 节点执行工具（第 2 章） |
| `ContextManager` | `src/agent/context.py` | agent_llm 节点构建消息列表；precheck / compact 节点压缩上下文（第 3 章） |
| `MemoryOrchestrator` | `src/agent/memory_orch.py` | retrieve_memory 节点检索长期记忆（第 3 章） |

**本章核心看点**（按重要性排序，每一项都对应一段「踩过坑才有的设计」）：

1. StateGraph 拓扑与条件路由 —— 一张图是怎么搭起来的（**含 precheck 自动压缩节点**）
2. `_stream_llm_with_hard_timeout` —— 工作线程 + 一次性 httpx.Client 的硬超时与连接泄漏修复
3. contextvars 跨线程传播 —— 一个潜伏很深的 bug
4. HITL 中断检测 —— langgraph 0.2.76 的版本陷阱
5. ReAct 循环检测 —— 两道防线
6. token 阈值自动压缩（precheck）—— 防上下文溢出
7. `recursion_limit` 动态计算 —— 防止框架兜底抢在自己前面

> 注：`_StreamWatchdog` 是一份**刻意保留**的独立超时组件（生产路径未实例化，由 `tests/test_llm_timeout.py` 覆盖），见 §3.2。

---

## 2. 架构与数据流

### 2.1 图拓扑

```
START → [retrieve_memory] → [precheck] → [agent_llm] ──有 tool_calls──→ [tools]
                                      ↑                  │
                                      │无 tool_calls     │
                                      │                  ↓
                                      │          [after_tools_router]
                                      │                  │
                                      │       ┌──────────┴──────────┐
                                      │   compact_requested       否则
                                      │       ↓                      │
                                      │   [compact] → agent_llm      │
                                      │                              │
                                      ↓                              │
                                 [store_memory] ←────────────────────┘
                                 (no-op, 留空)
                                      │
                                      ↓
                                     END
```

关键点：**`retrieve_memory` 之后、`agent_llm` 之前多了一个 `precheck` 节点**（commit `7e1c03e` 引入）。它每轮一次地按 token 阈值自动压缩上下文。注意循环路径 `agent_llm ⇄ tools` 不经过 `precheck`，故每个 ReAct 循环内只压缩一次（在首轮进入 agent_llm 前）。

对应代码 `_build_graph()`（`graph.py` `_build_graph`）：

```python
graph.add_node("retrieve_memory", self._retrieve_memory_node)
graph.add_node("precheck",        self._precheck_node)
graph.add_node("agent_llm",       self._agent_llm_node)
graph.add_node("tools",           self._tools_node)
graph.add_node("compact",         self._compact_node)
graph.add_node("store_memory",    self._store_memory_node)

graph.add_edge(START, "retrieve_memory")
graph.add_edge("retrieve_memory", "precheck")
graph.add_edge("precheck", "agent_llm")
graph.add_conditional_edges("agent_llm", self._should_continue)       # → tools | store_memory
graph.add_conditional_edges("tools",     self._after_tools_router)    # → compact | agent_llm
graph.add_edge("compact", "agent_llm")
graph.add_edge("store_memory", END)
```

### 2.2 主干调用链（一次对话轮回）

```
CLI: agent.stream_invoke(user_id, input, session_messages, ...)
  │
  ├─ set_current_user_id(user_id)          # 给 remember 工具注入 user_id
  ├─ 计算 recursion_limit                  # 动态：固定节点(3) + 迭代×3 + 余量(9)
  ├─ self.app.stream(stream_mode=["custom","values"], config={thread_id, recursion_limit})
  │     │
  │     ├─ retrieve_memory 节点 → writer 推 memory_search 事件
  │     ├─ precheck 节点（每轮一次）→ token 达阈值 → 推 auto_compact + messages_snapshot
  │     ├─ agent_llm 节点     → writer 推 token / reasoning_token 事件（流式逐字）
  │     │      ├─ 防线1: _detect_tool_loop → 死循环？终止
  │     │      ├─ 防线2: iteration_count >= max_iter？终止
  │     │      └─ _stream_llm_with_hard_timeout(...)  # 工作线程 + 一次性 client 硬超时
  │     ├─ tools 节点        → writer 推 tool_start/tool_end/subagent_token/todos_update
  │     ├─ compact 节点（可选）→ 压缩历史 + 推 messages_snapshot
  │     └─ store_memory 节点 → no-op（记忆改由 remember 工具负责）
  │
  ├─ stream 结束后：get_state() 查 checkpoint
  │      └─ 若 task.interrupts 非空 → yield human_approval_request（HITL）
  │
  └─ yield turn_messages（增量）+ complete
```

### 2.3 事件协议（graph → CLI 的通信契约）

节点函数通过 `writer: StreamWriter`（LangGraph 注入）推送 dict 事件，`stream_invoke` 用 `stream_mode=["custom"]` 接收后原样 `yield` 给 CLI：

| 事件 type | 推送方 | 用途 |
|-----------|--------|------|
| `memory_search` | retrieve_memory | 让 UI 展示"检索到 N 条记忆" |
| `auto_compact` | precheck | 上下文自动触发压缩，UI 可提示"已压缩早期对话"（含 compacted_count/original_count） |
| `messages_snapshot` | precheck / compact | 压缩后的全量消息快照，让调用方**整体替换** session_messages（修复压缩结果不落盘的 bug，见 §3.8） |
| `token` | agent_llm | 流式逐字输出（仅纯文本，不含工具调用） |
| `reasoning_token` | agent_llm | 流式推理思考片段（仅「主思考」开启时，由 `delta.reasoning` 字段或 `<think>` 标签拆分而来，见 §3.7） |
| `tool_start` / `tool_end` | tools 节点 | 工具执行状态 |
| `subagent_token` | tools 节点 | 子智能体流式 token |
| `todos_update` | tools 节点 | 待办事项变更（由 `ToolRegistry` 推送，让 UI 实时刷新 todos 面板） |
| `human_approval_request` | stream_invoke | HITL 暂停后请求人工确认 |
| `turn_messages` | stream_invoke | 一轮回合的增量消息 |
| `complete` | stream_invoke | 回合正常结束 |

> **设计动机**：用「事件流」而非「返回值」做 UI 通信，是因为 LLM 输出本质是流式的，且工具执行、HITL、记忆检索、自动压缩都是异步发生的事件。事件流让 CLI/Web 能用统一的 `match event["type"]` 渲染，不必关心图内部时序。

---

## 3. 逐段深潜

### 3.1 类初始化 `__init__`（`graph.py` `HermesAgent.__init__`）—— 组合式装配 + LLM 调参

```python
def __init__(self, memory_manager, tool_callback=None):
    self.settings = get_settings()
    self._init_mcp()                                              # 连 MCP
    self.tool_registry = ToolRegistry(tools=get_all_tools(), tool_callback=tool_callback)
    self.context_manager = ContextManager()
    self.memory_orchestrator = MemoryOrchestrator(memory_manager)
    self.memory = memory_manager        # 向后兼容
    self.tools = self.tool_registry.tools
    ...
    self.llm = ChatOpenAI(...)
    self.llm_with_tools = self._safe_bind_tools(get_all_tools())
    self.app = self._build_graph()
    from src.tools.remember import set_memory_manager
    set_memory_manager(memory_manager)  # 把 manager 注入给无状态的 remember 工具
    self._llm_override = None           # Web per-request LLM 覆盖
    self._compact_threshold_pct = None  # per-request 自动压缩阈值
```

**为什么这么写：**

- **组合而非继承**：`HermesAgent` 不自己实现工具调度/上下文构建/记忆检索，而是持有三个子模块。这让每个子模块可独立测试、独立演进（第 2/3 章会看到它们原本是从 `HermesAgent` 拆出来的，拆分动机就是「单一职责」）。
- **`self.memory` / `self.tools` 是「向后兼容属性」**：早期这些逻辑都在 `HermesAgent` 里，外部代码（CLI、测试）直接 `agent.memory.xxx`。拆分后保留这些属性，避免大面积改调用方。这是一种**渐进式重构**策略。
- **`set_memory_manager` 注入**：`remember` 工具是无状态的 `@tool` 函数，无法通过构造函数传参。所以用模块级 holder dict 注入单例。这和 user_id 用 contextvars 注入是**两种不同的依赖注入策略**——manager 是全局单例（无竞态），user_id 是每请求隔离（必须 contextvars）。这个区分很关键，第 5 章讲 remember 时会展开。
- **`_llm_override` / `_compact_threshold_pct`（per-request 覆盖）**：两者都是「`None` = 走 CLI / config 默认；非 `None` = Web 注入的用户偏好」。在 `stream_invoke` 入口处赋值（见 §3.9），让同一进程并发服务多 Web 会话时按请求隔离配置。这是把 Agent 从「CLI 单进程」演进到「Web 多会话」的折中。

#### LLM 调参的三个「坑后参数」（`graph.py` `HermesAgent.__init__`）

```python
self.llm = ChatOpenAI(
    ...
    streaming=True,
    temperature=0.7,
    max_tokens=20000,
    request_timeout=self.settings.llm_timeout,
    max_retries=0,                    # ← 不自动重试
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},  # ← 关思考模式
)
```

| 参数 | 值 | 踩坑动机 |
|------|-----|---------|
| `max_retries=0` | 0 | OpenAI 客户端默认重试 2 次。自托管端点超时时，最坏挂死 `600s × 3 = 30 分钟`才报错。设 0 让用户**立即感知失败**，自己决定重试 |
| `enable_thinking: False` | 关（默认） | Qwen3 的思考模式会消耗 max_tokens，导致正式 content 为空。这是自托管 Qwen 系列特有的坑。**默认关**，但 agent_llm 节点读到 per-send 的 `state["thinking"]` 开关（前端「主思考」按钮）时按需 `bind(enable_thinking=True)`，并装配推理拆分（`ThinkSplitter` + `delta.reasoning` 字段）把思考发成 `reasoning_token` 事件 |
| `request_timeout` | 配置可调（默认 120s） | 见 §3.3，与硬超时互补 |

> **动机总结**：这三个参数都是「自托管 LLM 端点」场景特有的防御。如果用的是官方 OpenAI API，默认值就够；但自托管 Qwen 端点负载高、网络抖动多，必须显式收紧。

#### MCP 与工具绑定的生命周期

四个 MCP 相关方法构成一套「连接 + 安全绑定 + 热刷新 + 关闭」的完整生命周期：

| 方法 | 作用 | 失败兜底 |
|------|------|---------|
| `_init_mcp()` | 启动时连接所有 enabled 的 MCP Server | 失败仅 `logger.warning`，**不阻塞**内置工具 |
| `_safe_bind_tools(tools)` | `bind_tools` 前逐个 `convert_to_openai_tool` 测试，**跳过 schema 转换失败的工具**（某些 MCP 工具的 `data` 等字段 LangChain 处理不了，否则会让 agent 整体启动崩溃） | 跳过坏工具，记录成功绑定的工具到 `self._good_tools`（供 §3.3 一次性 client 重新 bind 保持一致） |
| `rebind_tools()` | `/mcp` 变更后热刷新工具列表 | 重建 `ToolRegistry` 前先迁移旧 `_last_todos`，避免 `get_todos()` 返回空 |
| `shutdown_mcp()` | 退出时关闭所有 MCP 连接 | `except Exception: pass` 静默 |

> **为什么 `_safe_bind_tools` 必要**：MCP server 的工具 schema 是外部输入，质量参差。`bind_tools` 是「全有或全无」——一个坏 schema 会毁掉整次启动。逐个测试 + 跳过是稳健的容错姿势，代价只是 agent 少用一两个工具。

---

### 3.2 `_StreamWatchdog`（`graph.py` `_StreamWatchdog`）—— 一份刻意保留的可复用超时组件

这是项目里**最容易被误读**的一段。先把结论讲清楚：**生产路径的 `_stream_llm_with_hard_timeout` 内联了等价的双 deadline 逻辑，没有实例化这个类。但这个类不是死代码**——它的 docstring 明确说明保留意图：

> 当前生产路径未实例化本类。保留本类是因为它作为独立的、可复用的超时防御组件被 `tests/test_llm_timeout.py` 覆盖（`gap_timer` / `total_timer` / trickle-hang 场景）。若未来要给其它流式调用加超时，可复用此类而非重新内联。

所以它的定位是：**被测试覆盖、供未来复用的独立组件**，而不是「忘了删的旧实现」。

#### 它要解决的问题：trickle-hang（涓流挂死）

自托管 Qwen 端点会「**流式半开**」：SSE 连接建立后，偶尔发心跳字节，每隔不到 `interval` 秒一个，导致：
- httpx 的 read-timeout（按字节间隙计）**永不触发**
- 主线程卡在 native socket read，**Ctrl+C 都无法中断**
- 表现：对话「卡住 3-4 分钟」，只能强杀进程

#### 为什么不能用「滑动窗口」（第一版错误解法）

```python
# 错误思路：每个 chunk 到达就 reset 计时器
# → 但心跳字节每隔 < interval 秒发一个
# → 计时器永远被 reset → watchdog 永不触发
```

对方一直在发数据，只是发得极慢，永远不发完——这就是 trickle-hang。

#### 正确解法：两道独立防线（`graph.py` `_StreamWatchdog.start` / `reset`）

```python
def __init__(self, interval, on_timeout, max_total=None):
    self._interval = interval
    self._max_total = max_total if max_total is not None else interval * 3
    self._gap_timer = None    # 防线1：间隙超时
    self._total_timer = None  # 防线2：总时长上限

def start(self):
    self._gap_timer = threading.Timer(self._interval, self._fire)   # reset 可重置
    self._gap_timer.start()
    if self._max_total and self._max_total > 0:
        self._total_timer = threading.Timer(self._max_total, self._fire)  # reset 不重置
        self._total_timer.start()

def reset(self):
    """每个 chunk 调用。只重置 gap_timer，total_timer 不受影响。"""
    if self._gap_timer is not None:
        self._gap_timer.cancel()
    self._gap_timer = threading.Timer(self._interval, self._fire)
    ...
```

| 防线 | 防什么 | reset 是否重置 |
|------|--------|---------------|
| `gap_timer` | 完全无响应（interval 秒内无任何字节） | ✅ 是（正常 chunk 该重置） |
| `total_timer` | trickle-hang（一直慢滴字节但永不结束） | ❌ 否（从 start 起累计，硬上限） |

> **为什么 `max_total` 默认是 `interval × 3`**：给正常响应留 3 倍容差。一个正常的流式响应，即便有间歇停顿，总时长不该超过单次间隙超时的 3 倍。超过就一定是病态。

> **设计权衡**：为什么不用 `signal.alarm`（Unix SIGALRM）？因为它**不能中断 native socket read**——Python 的信号只在主线程字节码间隙被处理，卡在 C 层 socket read 时收不到。`threading.Timer` + daemon 线程放弃策略才能破局（见 §3.3）。

#### 替代方案对比

| 方案 | 为何不采用 |
|------|-----------|
| 调小 httpx `read_timeout` | 对 trickle-hang 无效：心跳字节会不断重置间隙计时 |
| `signal.alarm` | 无法中断 C 层 socket read；且 Windows 不支持 |
| `asyncio.wait_for` | 整个图是同步的，引入 async 改造成本过大 |
| **当前方案**（Timer × 2） | ✅ 跨平台、能破 native 阻塞、与同步图兼容 |

---

### 3.3 `_stream_llm_with_hard_timeout`（`graph.py` `_stream_llm_with_hard_timeout`）—— 工作线程 + 一次性 httpx.Client

看门狗解决了「什么时候判定超时」，但还剩两个问题：**主线程已经卡在 socket read 了，超时触发了又怎样？** 以及 **超时后那个半开连接和它占用的线程怎么办？**

> **签名说明**：`_stream_llm_with_hard_timeout(self, llm_messages, timeout_s, active_llm=None)`。`active_llm` 由调用方按需注入（如 thinking 模式的 `bind` 副本或 Web 的 `_llm_override`）；默认 `None` 时本函数自建一次性 LLM。

#### 解法：工作线程 + Queue 消费 + 一次性 Client

```python
def _stream_llm_with_hard_timeout(self, llm_messages, timeout_s, active_llm=None):
    _chunks_q = _q.Queue()
    ctx = contextvars.copy_context()          # ← 关键！见 §3.4

    # 根因 C 修复（commit 4391c01）：默认路径构造一次性 httpx.Client 包装的
    # ChatOpenAI，超时后 close() 强制断 socket。
    _disposable_client = None
    if active_llm is None and hasattr(self, "settings") and hasattr(self, "llm"):
        _disposable_client = httpx.Client(timeout=self.settings.llm_timeout)
        active_llm = ChatOpenAI(..., http_client=_disposable_client)
        if getattr(self, "_good_tools", None):
            active_llm = active_llm.bind_tools(self._good_tools)

    def _worker():
        try:
            for chunk in active_llm.stream(llm_messages):
                _chunks_q.put(chunk)
        except Exception as e:
            _worker_exc.append(e)
        finally:
            _chunks_q.put(_SENTINEL_END)

    t = threading.Thread(target=ctx.run, args=(_worker,), daemon=True)
    t.start()

    deadline = _t.time() + timeout_s
    total_deadline = _t.time() + timeout_s * 3
    while True:
        wait = min(timeout_s, deadline - now, total_deadline - now)
        if wait <= 0:
            raise TimeoutError(...)
        try:
            chunk = _chunks_q.get(timeout=wait)    # ← 主线程在这里等，可被中断
        except _q.Empty:
            raise TimeoutError("...疑似半开连接")
        ...
        yield chunk
        deadline = last_chunk_time + timeout_s      # 收到 chunk 重置间隙 deadline
```

#### 核心动机

1. **主线程永不进入不可中断的 native 阻塞**。`queue.get(timeout=)` 是纯 Python 实现，Ctrl+C / 异常都能正常抛出。socket read 被隔离在 daemon 工作线程里。

2. **超时后强制 close 一次性 client（连接泄漏修复，commit `4391c01`）**：

```python
except TimeoutError:
    # 关闭一次性 httpx client，强制中断 worker 的阻塞 socket
    # read → worker 抛异常退出，释放线程与连接。
    if _disposable_client is not None:
        try:
            _disposable_client.close()
        except Exception:
            pass
    raise
finally:
    # 正常结束也关闭一次性 client（释放连接池）
    if _disposable_client is not None:
        try:
            _disposable_client.close()
        except Exception:
            pass
```

> **⚠️ 关键修正（commit `4391c01`）**：早期实现超时后**仅丢弃 daemon 线程、保留复用的 httpx 连接不动**——这套旧逻辑**已被推翻**。问题在于：半开连接在自托管端点是高频问题，留着连接不关，worker 仍阻塞在 native socket read、httpx 连接不释放；线程与连接持续累积 → **耗尽连接池 → 后续所有 LLM 调用超时**。
>
> 现行实现**反其道而行**：默认路径构造一个**一次性 `httpx.Client`** 包装的 `ChatOpenAI`，超时后 `_disposable_client.close()` 强制断 socket → worker 的阻塞 read 抛异常退出 → 线程与连接一并释放。`finally` 里无论是否超时都 close，保证连接池不残留。

3. **`active_llm is None` 分支的取舍**：只有默认路径（真实 agent，有 `settings` 和 `llm`）才构造一次性 client；测试用的 `_FakeAgent`（无 `settings`/`llm`）或调用方显式传入 `active_llm`（如 Web 的 `_llm_override`）时，由调用方自管生命周期，仍走旧逻辑。这是因为 override 副本是 per-request 的、用完即弃，本身不存在跨请求复用问题。

> **权衡代价**：一次性 client 牺牲了 httpx 连接池的复用收益（每次 agent_llm 调用都新建连接）。在「自托管端点半开高频」与「连接复用」之间，项目选择前者优先——可靠性 >> 单次握手开销。

#### 间隙 deadline 的双语义（`graph.py` `_stream_llm_with_hard_timeout` 消费循环）

```python
total_deadline = _t.time() + timeout_s * 3        # 总时长，不重置
...
deadline = last_chunk_time + timeout_s              # 间隙，每个 chunk 重置
```

这和 `_StreamWatchdog` 的双防线是**同构的**——只是这里直接内联在消费循环里，没用 Timer。这也解释了为什么 `_StreamWatchdog` 保留为独立组件：内联版本用于生产路径，类版本留给测试与未来复用（见 §3.2）。

---

### 3.4 contextvars 跨线程传播（`graph.py` `_stream_llm_with_hard_timeout`）—— 一个深藏的 bug

```python
# 根因 B 修复：裸 threading.Thread 不传播 contextvars
ctx = contextvars.copy_context()
...
t = threading.Thread(target=ctx.run, args=(_worker,), daemon=True)
```

#### 为什么必须 `copy_context()`

这是整个项目**最隐蔽**的一类 bug。注释（`graph.py` `_stream_llm_with_hard_timeout` docstring 根因 B）讲得很细，因果链如下：

```
LLM tool_call 参数绕过 @tool 的 pydantic 校验
    ↓
langchain 的 var_child_runnable_config（callback/tracer 配置）是 contextvar
    ↓
裸 threading.Thread 不会传播父线程的 contextvars（CPython 行为）
    ↓
worker 线程拿不到 langchain 的 contextvar
    ↓
LLM 流跑在一个「孤立的」callback/tracer run tree 上
    ↓
第一轮虽能返回 chunk，却污染了 langchain 共享的 callback 状态
    ↓
下一轮工具节点撞上被污染的状态 → 图卡死（实测：web_search 解码完 HTML 后整图无任何后续日志）
```

**关键认知**：`threading.Thread(target=fn)` 启动的线程，**不会自动继承**父线程的 `contextvars.Context`。必须显式 `copy_context()` 后用 `ctx.run(fn)` 执行。

#### 同类 bug 在项目里的统一范式

| 位置 | 文件 / 符号 | 状态 |
|------|------------|------|
| LLM 流式 | `graph.py` `_stream_llm_with_hard_timeout` | ✅ 已修（本章） |
| 工具并发执行 | `tools_registry.py` `process_tool_calls` 的 `ThreadPoolExecutor`（每个 worker 各自 `copy_context()`） | ✅ 已修（第 2 章） |
| 子智能体嵌套深度 | `tools/sub_agent.py` `_current_depth` ContextVar | ✅ **已从 `threading.local` 改为 `contextvars`**（见下方） |

#### sub_agent 的 contextvars：隐患已根治

`tools/sub_agent.py` 里子智能体嵌套深度计数 `_current_depth` **曾用 `threading.local`**，存在隐患：`task` 工具可能在 `ToolRegistry` 的 `ThreadPoolExecutor` 并发路径里执行（见 `tools_registry.py` 的 `_submit → copy_context`）。`threading.local` 在每个 worker 线程各自独立、主线程的深度修改不传播，会导致并发子智能体各自看到 `depth=0`，**突破 `sub_agent_max_depth` 限制**。

现已改为 `contextvars.ContextVar`（`tools/sub_agent.py:34`）：

```python
# contextvars 会随 copy_context() 正确传播到 worker 且各自独立可写，
# 与项目其它隔离（remember 的 user_id）范式统一。
_current_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "hermes_subagent_depth", default=0
)
```

`contextvars` 既随 `copy_context()` 传播到 worker，又各自独立可写——这是并发隔离的正确语义。

> **动机总结**：这是「同步图 + 多线程 + langchain contextvar」组合下的系统性陷阱。项目用 `copy_context() + ctx.run` 作为统一范式。**学会识别这个模式**，读项目其它多线程代码就轻松了一半。

---

### 3.5 HITL 中断检测（`graph.py` `stream_invoke`）—— langgraph 0.2.76 的版本陷阱

这是项目里**注释最长、踩坑最久**的地方。

#### 错误的旧实现

```python
# 旧实现（不工作）：
for mode, chunk in self.app.stream(...):
    if "__interrupt__" in chunk:    # ← 永远不命中
        ...
```

**根因**：在项目实际使用的 langgraph **0.2.76** 中，`interrupt()` 暂停图时，**不会**向 `stream_mode=["custom","values"]` 的 chunk 注入 `__interrupt__` 键。stream 只是静默结束（generator 耗尽）。

结果：`interrupted` 恒为 `False`，落到「无有效回复」兜底，返回「⚠️ 模型未返回有效内容」——**HITL 完全失效**。

#### 正确实现（`graph.py` `stream_invoke` 末尾的 `get_state()` 检查）

```python
# stream 结束后，查 checkpoint 判断是否被暂停
snapshot = self.app.get_state(config)
if snapshot is not None:
    snap_msgs = (snapshot.values or {}).get("messages", [])
    if snap_msgs:
        final_messages = snap_msgs
    for task in (getattr(snapshot, "tasks", None) or ()):
        for it in (getattr(task, "interrupts", None) or ()):
            interrupted = True
            payload = getattr(it, "value", it) or {}
            if not isinstance(payload, dict):
                payload = {"action": "", "details": str(payload)}
            yield {
                "type": "human_approval_request",
                "action": payload.get("action", ""),
                "details": payload.get("details", ""),
                "thread_id": effective_thread_id,
            }
            break
        if interrupted:
            break
```

判定逻辑：`snapshot.next` 非空（图停在某个节点）**且** task 带 `interrupts`（是被 `interrupt()` 暂停，而非异常半途结束）。payload 在 `task.interrupts[0].value` 里——就是当初传给 `interrupt(dict)` 的那个 dict。

> 顺带一提：循环里先 `final_messages = snap_msgs` 用 checkpoint 最新快照覆盖，保证 `final_messages` 在中断场景下也是最新值。

#### 为什么 `except GraphBubbleUp: raise`（`graph.py` `stream_invoke`）

```python
except GraphBubbleUp:
    # interrupt() 的暂停信号（GraphInterrupt 是其子类），必须透传，
    # 不能被下方 except Exception 二次吞掉导致图无法暂停。
    # 0.2.76 下 interrupt 通常表现为 stream 静默结束而非抛异常，
    # 此 except 作兜底；真正的中断检测见下方 get_state() 逻辑。
    raise
except Exception as e:
    ...
```

`GraphInterrupt` 是 `GraphBubbleUp` 的子类。如果放在 `except Exception` 后面会被吞掉，图就无法暂停。这是个**必须严格遵守**的异常处理顺序约束。注意 docstring 强调：在 0.2.76 下这条 `except` 主要是兜底，真正的检测靠后面的 `get_state()`——因为该版本下 `interrupt` 通常表现为 stream 静默结束而非抛异常。

> **教训**：框架的 API 行为在不同小版本间会变。这里注释明确写了「已用诊断脚本验证 0.2.76 行为」——说明是**实测驱动**的修复，不是猜的。这种「版本行为差异」是使用 LangGraph 这类快速迭代框架的核心风险。

---

### 3.6 ReAct 循环检测 —— 两道防线（`graph.py` `_detect_tool_loop` / `_agent_llm_node`）

LLM 有时会陷入「调同一个工具 → 收到结果 → 再调同一个工具」的死循环。两道防线：

#### 防线 1：签名重复检测 `_detect_tool_loop`（`graph.py` `_detect_tool_loop`）

```python
def _detect_tool_loop(self, messages) -> bool:
    # 阈值可配置：tool_loop_threshold（config 默认 5）。0 = 关闭检测。
    # getattr 兜底默认值有意保留旧值 3（比配置默认更严格），作为最后防线；
    # 仅在旧 config.yaml 缺该键时触发。
    threshold = getattr(self.settings, "tool_loop_threshold", 3)
    if threshold <= 0:
        return False
    signatures = []
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.tool_calls:
            sig = tuple(sorted(
                (tc["name"], json.dumps(tc.get("args", {}), sort_keys=True, ensure_ascii=False))
                for tc in msg.tool_calls
            ))
            signatures.append(sig)
            if len(signatures) >= threshold:
                break
        elif isinstance(msg, AIMessage):
            break    # 遇到纯文本回复，不构成循环
    if len(signatures) >= threshold and len(set(signatures)) == 1:
        return True
    return False
```

**设计要点**：
- **签名 = (工具名, 参数JSON) 的有序元组**：只有「同名同参」才算重复。换了参数不算。
- **遇到纯文本 AIMessage 就 break**：说明 LLM 已经正常回复过，之前的工具调用是合理的前进，不是循环。
- **阈值 `tool_loop_threshold`（config 默认 5，docstring 已同步说明）**：agent_llm 的 docstring 明确写「最近 N 次相同调用（N 由 `tool_loop_threshold` 配置，默认 5）」。
- **`getattr` 兜底值刻意保留旧值 3**：这是**有意为之**——比配置默认更严格，仅在旧 `config.yaml` 缺该键时触发，作为「最后防线」。注释说明 `test_tool_loop_threshold` 场景 4 验证此行为。

#### 防线 2：最大迭代次数（`graph.py` `_agent_llm_node`）

```python
max_iter = self.settings.max_agent_iterations   # 默认 50
if iteration_count >= max_iter:
    return {"messages": [AIMessage(content="⚠️ Agent 已达到最大迭代次数...")]}
```

每经过一次 agent_llm，`iteration_count += 1`（`graph.py` `_agent_llm_node` 正常返回 `iteration_count + 1`，**超时分支也带 `iteration_count + 1`**，保持计数语义统一）。

> **两道防线的关系**：防线 1 针对「快速重复」（5 次相同调用就断），体验好；防线 2 是「兜底」（不管调没调工具，总共不许超过 max_iter 次）。两者互补，不能只留一个。

---

### 3.7 thinking 处理（`graph.py` `_agent_llm_node`）—— `ThinkSplitter` + `delta.reasoning`

主思考模式（`state["thinking"] == True`）下，agent_llm 做两件事：

**① bind 开启 Qwen3 thinking**：

```python
active_llm = self._llm_override if self._llm_override is not None else self.llm_with_tools
if thinking_mode:
    active_llm = active_llm.bind(
        extra_body={"chat_template_kwargs": {"enable_thinking": True}}
    )
elif self._llm_override is None:
    active_llm = None  # 无 override 非思考 → 用默认 llm_with_tools
```

注意：无论是否有 `_llm_override`（Web 路径），thinking 都要叠加 `enable_thinking`；只有「非 thinking 且无 override」才置 `None`，走 §3.3 默认路径（一次性 client）。

**② 两条并行的推理输出通路**（互斥，同一端点只输出一种格式）：

- **`<think>` 标签通路**：`ThinkSplitter` 把流式 `<think>...</think>` 拆成 `reasoning` / `content` 两路，分别推 `reasoning_token` / `token` 事件。
- **独立 reasoning 字段通路**（vLLM/Qwen3）：推理不放 `<think>` 标签，而是 `delta.reasoning`，经 `src.think.patch_langchain_reasoning` 的 monkeypatch 落到 `additional_kwargs["reasoning"]`：

```python
if splitter is not None and not has_tool_calls:
    rc = (chunk.additional_kwargs or {}).get("reasoning")
    if rc:
        writer({"type": "reasoning_token", "content": rc})
```

> **为什么需要 monkeypatch**：LangChain 0.3.x 流式解析时**丢弃** `delta.reasoning`，必须在首次 `stream()` 前打补丁（`__init__` 里 `patch_langchain_reasoning()`）把推理内容捞进 `additional_kwargs["reasoning"]`，否则 thinking 模式会静默丢推理。

> **空 content 的合法化**：thinking 模式下若 `content` 为空但 `reasoning` 非空（思考完直接结束/只给思考无结论），agent_llm 不视为错误，正常返回。这是 thinking 模式特有的合法状态。

---

### 3.8 token 阈值自动压缩（`graph.py` `_precheck_node` / `_compact_node`）

项目有两处压缩入口，**事件协议一致**（都推 `messages_snapshot`），但触发时机不同：

| 节点 | 触发 | 何时 |
|------|------|------|
| `_precheck_node` | **token 阈值自动**（每轮 agent_llm 前） | `count_tokens(messages) >= context_window × compact_threshold_pct` |
| `_compact_node` | **工具显式请求**（`compact_requested` 信号） | 工具执行后路由判定 |

#### precheck 的阈值解析（per-request 覆盖 > config 默认）

```python
pct = self._compact_threshold_pct              # Web per-request 注入
if pct is None:
    pct = self.settings.get("compact_threshold_pct", 80)  # config 默认 80
try:
    pct = int(float(pct))
except (TypeError, ValueError):
    pct = 80
if pct <= 0:
    return {}    # 0 / 负 = 关闭 token 压缩（透传）
```

达阈值后执行压缩并推两类事件：

```python
writer({
    "type": "auto_compact",
    "compacted_count": compact_result.compacted_count,
    "original_count": compact_result.original_count,
})
writer({
    "type": "messages_snapshot",
    "messages": compact_result.compressed_messages,
})
```

#### 为什么要 `messages_snapshot` 事件（修复压缩结果不落盘）

`add_messages` reducer 是**追加语义**。压缩节点返回 `RemoveMessage + 压缩消息` 后，graph state 内部 messages 变短了，但 `stream_invoke` 末尾的 `final[initial:]` 切片会得到空列表，导致调用方的 `session_messages` 原封不动、data 里仍存原始对话。`messages_snapshot` 让调用方**整体替换**而非增量拼接，绕开这个 bug。

> **设计权衡**：把自动压缩放在 precheck（agent_llm 之前）而非 tools 之后，是因为压缩要在「本轮 LLM 看到上下文之前」生效——否则 LLM 拿到的仍是超长 messages，压缩对当前轮毫无意义。而循环路径 `agent_llm ⇄ tools` 不经过 precheck，避免每个 ReAct 循环都重复压缩（压缩本身有 LLM 调用成本）。

---

### 3.9 `stream_invoke` 的参数与递归限制（`graph.py` `stream_invoke`）

`stream_invoke` 是对外的唯一入口，参数集反映了项目从 CLI 单进程到 Web 多会话的演进：

| 参数 | 用途 | 何时传 |
|------|------|--------|
| `user_id` / `user_input` / `session_messages` | 基本输入 | 总是 |
| `session_id` / `thread_id` | checkpoint 隔离 | `thread_id` 默认用 `session_id`，保证同会话可恢复 |
| `todos` / `virtual_fs` | 跨轮次传递状态 | CLI/Web 每轮回传 |
| `resume_payload` | HITL 恢复：非空时跳过 `initial_state`，直接 `Command(resume=...)` 唤醒 checkpoint | 用户批准/拒绝后 |
| `llm_override` | Web per-request LLM 覆盖（temperature/max_tokens 热更新） | Web 路径；CLI 不传 |
| `role` | 角色名（M1-3），注入对应角色卡人格；空=默认 assistant | 切换人格时 |
| `thinking` | 主思考模式开关 | 前端按钮 |
| `images` | 多模态图片 URL 列表，经 `build_user_content` 拼进 HumanMessage | 多模态输入 |
| `compact_threshold_pct` | per-request 自动压缩阈值（0–100，0=关闭） | Web 注入用户偏好；CLI 走 config |

#### `recursion_limit` 动态计算（`graph.py` `stream_invoke`）

```python
# 每次 ReAct 循环消耗 2 step（agent_llm + tools），compact 再 +1
# 新增 precheck 节点（每轮 +1 固定 step）。
# 公式：固定节点(retrieve_memory + precheck + store_memory = 3) + 迭代 × 3 + 安全余量(9)
# 原硬编码 50 会在 max_agent_iterations=25 时提前抛 GraphRecursionError，
# 导致 graph.py 的防线 2（iteration_count 检测）永远无法触发
recursion_limit = self.settings.max_agent_iterations * 3 + 12
```

**动机**：LangGraph 有自己的 `recursion_limit`（默认 25），超过就抛 `GraphRecursionError`。如果这个限制比业务层的 `max_agent_iterations` 先触发，那 agent_llm 里精心写的「防线 2」就**永远不会执行**——图会先被框架强制中断。

所以这里**主动把框架限制调到比业务限制更高**，让业务逻辑优先：

```
框架限制 = 业务限制 × 3 + 12  >  业务限制
```

`× 3` 是因为一轮 ReAct = agent_llm(1) + tools(1) + 可能的 compact(1) ≈ 3 步；`+12` 覆盖固定节点（retrieve_memory + precheck + store_memory = 3）+ 余量（9）。注释里的「3」是 precheck 引入后新增的固定 step 计数。

> **这是一个「业务兜底 vs 框架兜底」的优先级问题**。通用教训：当你在一个框架之上做自己的容错逻辑，一定要确认框架的硬限制不会抢先触发。

---

## 4. 设计权衡总结

### 4.1 优点

| 设计 | 价值 |
|------|------|
| 事件流通信（writer → CLI） | 解耦图内部时序与 UI 渲染，CLI/Web 复用同一套事件处理 |
| 工作线程 + Queue 消费 | 保证 Ctrl+C 始终有效，调试/运维友好 |
| 一次性 httpx.Client + close（commit 4391c01） | 超时即释放线程与连接，根治半开连接导致的连接池耗尽 |
| `copy_context()` 统一范式 | 一次性堵住 contextvars 跨线程丢失这一类 bug（含 sub_agent 的 depth 隔离） |
| precheck token 阈值自动压缩 | 上下文溢出由「用户手动 /compact」变成「系统自动」，体验与可靠性双赢 |
| `messages_snapshot` 全量快照事件 | 绕开 add_messages 追加语义，保证压缩结果正确落盘 |
| 业务防线优先于框架限制 | 让自己的容错逻辑真正生效，不被框架抢断 |
| `_safe_bind_tools` 逐个测试 | 一个坏 MCP schema 不会毁掉整次启动 |
| `store_memory` no-op 化 | 记忆从「系统强塞」变「agent 能力」，架构更干净 |

### 4.2 待优化 / 注意点

| 项 | 说明 |
|----|------|
| 一次性 client 牺牲连接复用 | 每个 agent_llm 调用新建 httpx 连接；在「半开高频」与「复用」间优先可靠性，单次握手开销可接受 |
| `_StreamWatchdog` 与内联逻辑同构 | 生产路径内联了等价双 deadline，类版本留给测试与未来复用（见 §3.2）；维护时注意两者思想同步 |
| 超时后留下 zombie daemon 线程 | close 一次性 client 后 worker 的 read 会抛异常退出，但极端情况下 worker 仍可能短暂残留，由 OS 在进程退出时清理 |

---

## 本章小结

`graph.py` 的复杂度几乎全部来自两个**真实生产环境的硬约束**：

1. **自托管 LLM 端点不可靠**（流式半开、负载抖动、连接泄漏）→ 催生了工作线程隔离 + 一次性 client 硬超时（commit `4391c01`）
2. **LangGraph 框架的版本行为差异 + 多线程 contextvar 陷阱** → 催生了 `get_state()` 中断检测 + `copy_context()` 范式

读懂这两点，就看懂了这个文件 80% 的「为什么」。剩下的 20% 是常规的 ReAct 循环控制（两道防线 + recursion_limit）、token 阈值自动压缩（precheck）、以及 thinking 双通路处理。

下一章将进入 `tools_registry.py`——它和本章共享同一套 contextvars / interrupt 透传的范式，但多了「串行 vs 并发执行策略」这个维度，坑更多。
