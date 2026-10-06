# 05 · 手写 ReAct 循环（HermesAgentV3）

> `HermesAgentV3` 是 V3 的 Agent 编排内核，取代 V2 的 LangGraph `StateGraph` 6 节点图。本章是 Agent 机制的主轴——Agent loop 的循环方式、防死循环手段、multi-step 状态保持全在这里。
>
> ⚠️ **混合态标注**：`HermesAgentV3` 内部 state 仍存 langchain 消息（为 `ContextManager` 兼容），是 Phase 5.5 待清理的边界。转换发生在边界：`_langchain_msg_to_dict` 出、`AIMsg → AIMessage` 入。

---

## 5.1 文件定位

| | |
|---|---|
| **文件** | `src/agent/agent_v3.py`（~782 行） |
| **取代** | `src/agent/graph.py` 的 `HermesAgent`（LangGraph `StateGraph`：6 节点 + 条件边 + `MemorySaver` + `interrupt()` / `Command(resume)` + `GraphBubbleUp`） |
| **⚠️ langchain 依赖** | `_import_langchain_messages()`（`agent_v3.py:60`）延迟 import；`_compact_messages` 内 `RemoveMessage`（`agent_v3.py:311`） |

### 核心方法

| 方法 | 行号 | 说明 |
|------|------|------|
| `stream_invoke()` | `agent_v3.py:152` | 主入口（签名兼容 V2 `HermesAgent.stream_invoke`） |
| `_retrieve_memory()` | `agent_v3.py:262` | 记忆检索节点 |
| `_precheck()` | `agent_v3.py:282` | token 阈值压缩节点 |
| `_compact_messages()` | `agent_v3.py:308` | 执行压缩 |
| `_react_loop()` | `agent_v3.py:344` | ★ 手写 ReAct 循环 |
| `_detect_tool_loop()` | `agent_v3.py:663` | 重复工具签名检测 |
| `_handle_interrupt()` | `agent_v3.py:509` | 存快照 + yield 审批事件 |
| `_handle_resume()` | `agent_v3.py:544` | 恢复快照 + 重新求值权限 + 继续 ReAct |
| `_langchain_msg_to_dict()` | `agent_v3.py:697` | langchain 消息 → OpenAI dict（边界转换） |

---

## 5.2 stream_invoke 主入口（`agent_v3.py:152-256`）

```python
def stream_invoke(self, user_id, user_input, session_messages=None, session_id="",
                  todos=None, virtual_fs=None, thread_id=None, resume_payload=None,
                  llm_override=None, role=None, thinking=False, images=None,
                  compact_threshold_pct=None) -> Generator[dict, None, None]:
    # 1. 设置 contextvars（user_id / vfs / depth）
    set_current_user_id(user_id)
    if virtual_fs is not None: set_current_vfs(virtual_fs)
    _set_current_depth(0)

    # 2. 组装工具
    ctx = ToolContext(permission_mode=self._permission_mode, caller_context="main")
    tools_openai = self._resolve_and_bind_tools(ctx)    # 动态拉取 + config_guard 过滤

    # 3. HITL resume 路径
    if resume_payload is not None:
        yield from self._handle_resume(thread_id, resume_payload, ...)
        return

    # 4. 正常路径：初始化 state
    session_messages.append(HumanMessage(content=user_input))
    state = {
        "user_id": user_id, "current_input": user_input, "session_id": session_id,
        "messages": session_messages, "retrieved_memories": [],
        "todos": todos or [], "iteration_count": 0, "current_role": role,
    }

    # 5. 记忆检索
    yield from self._retrieve_memory(state, ctx)

    # 6. precheck（token 阈值压缩）
    yield from self._precheck(state, compact_threshold_pct)

    # 7. ReAct 循环
    try:
        yield from self._react_loop(state, ctx, tools_openai, thinking, llm_override)
    except InterruptSignal as sig:
        yield from self._handle_interrupt(sig, thread_id, state, ctx)
        return

    # 8. 完成
    yield {"type": "turn_messages", "messages": final_messages, "partial": False}
    yield {"type": "complete", "content": full_response}
```

> **V2 对照**：V2 的 `stream_invoke` 调 `self.app.stream(graph_input, stream_mode=["custom","values"], config=config)` 驱动 LangGraph 图，事件从 `writer({...})` 推送。V3 完全手写 generator，`yield` 直接推事件，没有图的 `custom` / `values` 双模式。事件协议与 V2 兼容（CLI/Web 调用方零改动）。

### stream_invoke 的两种模式

| 模式 | 触发 | 行为 |
|------|------|------|
| 首次模式 | `resume_payload is None` | 构建 initial_state → 记忆检索 → precheck → ReAct |
| 恢复模式 | `resume_payload is not None` | 跳过 initial_state → 直接 `_handle_resume` |

---

## 5.3 _react_loop —— 手写 ReAct（`agent_v3.py:344-503`）

这是 V3 的核心，取代 LangGraph 的 `agent_llm ⇄ tools` 条件路由循环。

### 循环结构

```python
def _react_loop(self, state, ctx, tools_openai, thinking, llm_override):
    client = self._get_llm_client()
    max_iter = int(getattr(self.settings, "max_agent_iterations", 15) or 15)
    timeout_s = int(getattr(self.settings, "llm_timeout", 120) or 120)

    while state["iteration_count"] < max_iter:
        # ── 防线 2：循环检测 ──
        if self._detect_tool_loop(state["messages"]):
            yield {"type": "token", "content": "⚠️ 检测到重复的工具调用，已终止循环。"}
            return

        # ── 构建 LLM 消息 ──
        llm_messages = self.context_manager.build_llm_messages(...)
        # 注入 mode guidance 到 system prompt
        mode_section = build_mode_prompt_section(ctx.permission_mode)
        llm_messages[0] = SystemMessage(content=llm_messages[0].content + "\n\n" + mode_section)
        # 转 dict 给 LLMClient
        llm_msgs_dict = [self._langchain_msg_to_dict(m) for m in llm_messages]

        # ── LLM 流式调用（双 deadline 超时）──
        chunks = []
        full_content = ""
        full_reasoning = ""
        try:
            stream = stream_with_hard_timeout(client, llm_msgs_dict, tools=tools_openai, timeout_s=timeout_s)
            for chunk in stream:
                chunks.append(chunk)
                if chunk.content_delta:
                    full_content += chunk.content_delta
                    yield {"type": "token", "content": chunk.content_delta}        # ★ 推 token
                if chunk.reasoning_delta:
                    full_reasoning += chunk.reasoning_delta
                    yield {"type": "reasoning_token", "content": chunk.reasoning_delta}
        except TimeoutError as e:
            # 超时：返回已收到部分 + 提示
            ...
            return
        except Exception as e:
            # 异常：错误回灌
            ...
            return

        # ── 累积成 AIMsg ──
        ai_msg = LLMClient.accumulate(chunks)
        state["iteration_count"] += 1

        # ── 无 tool_calls → 最终答复，结束循环 ──
        if not ai_msg.has_tool_calls:
            clean_content = strip_think_tags(ai_msg.content)   # 剥离 <think>
            state["messages"].append(AIMessage(
                content=clean_content,
                additional_kwargs={"reasoning": ai_msg.reasoning} if ai_msg.reasoning else {},
            ))
            return    # ★ 自然终止（防线 1）

        # ── 有 tool_calls → 执行工具 ──
        # 容错解析 arguments + 剥离 <think>
        lc_ai_msg = AIMessage(
            content=strip_think_tags(ai_msg.content),
            tool_calls=[{
                "id": tc.get("id", ""),
                "name": tc["function"]["name"],
                "args": safe_parse_tool_args(tc["function"]["arguments"]),
            } for tc in ai_msg.tool_calls],
            additional_kwargs={"reasoning": ai_msg.reasoning} if ai_msg.reasoning else {},
        )
        state["messages"].append(lc_ai_msg)

        # 执行（event_sink 收集事件，循环后 yield）
        _pending_events = []
        def event_sink(event): _pending_events.append(event)
        tool_msgs, state_updates = self.registry.process_tool_calls(
            tool_calls_for_registry, ctx, event_sink=event_sink,
        )
        for ev in _pending_events: yield ev

        # ToolMsg → langchain ToolMessage 存入 state
        for tm in tool_msgs:
            state["messages"].append(ToolMessage(content=tm.content, tool_call_id=tm.tool_call_id))

        # 合并 state_updates（todos / compact_requested）
        if "todos" in state_updates: state["todos"] = state_updates["todos"]
        if state_updates.get("compact_requested"):
            yield from self._compact_messages(state, auto=False)

        # 继续 ReAct 循环

    # 达到最大迭代
    yield {"type": "token", "content": f"\n⚠️ 已达最大迭代次数 {max_iter}"}
```

### event_sink 的 hack（`agent_v3.py:469-481`）

```python
_pending_events: list[dict] = []
def event_sink(event: dict):
    _pending_events.append(event)

tool_msgs, state_updates = self.registry.process_tool_calls(
    tool_calls_for_registry, ctx, event_sink=event_sink,
)
for ev in _pending_events:
    yield ev    # ★ 循环后 yield
```

> **为什么用这个 hack？** Python generator 不能嵌套 `yield`——`event_sink` 是普通函数，不能在里面 `yield`。所以用「收集到列表 + 循环后 yield」的模式。代价是 tool_start / tool_end 事件会**批量**推（一轮工具执行完后一次性 yield 所有事件），而不是逐个实时推送。但这在实践上没问题——一轮工具执行很快，批量推送的延迟用户感知不到。

---

## 5.4 循环终止的四道防线

> 常见问题：「Agent loop 怎么防止死循环？」

### 防线 1：自然终止（`agent_v3.py:426`）

LLM 不再调用工具——`ai_msg.has_tool_calls == False` → 存 AIMessage → `return`。

### 防线 2：重复工具调用检测（`agent_v3.py:663-690`）

```python
def _detect_tool_loop(self, messages) -> bool:
    threshold = int(getattr(self.settings, "tool_loop_threshold", 3) or 3)
    if threshold <= 0: return False

    signatures = []
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            sig = tuple(sorted(
                (tc["name"], json.dumps(tc.get("args", {}), sort_keys=True, ensure_ascii=False))
                for tc in msg.tool_calls
            ))                       # ★ 签名 = (工具名, 参数JSON) 的有序元组
            signatures.append(sig)
            if len(signatures) >= threshold: break
        elif isinstance(msg, AIMessage): break    # 纯文本回复，不构成循环

    if len(signatures) >= threshold and len(set(signatures)) == 1:
        return True                 # 最近 N 次签名完全相同 = 死循环
```

检测最近 N 次（`tool_loop_threshold`，默认 3）`(工具名, 参数JSON)` 签名完全相同 → 强制终止。参数 JSON 用 `sort_keys=True` 保证顺序无关。

### 防线 3：最大迭代次数（`agent_v3.py:356, 499`）

`iteration_count >= max_agent_iterations`（默认 15，config 可调到 50），每个 LLM 调用后 `+1`。

### 防线对比 V2

> **V2 有四道防线**（防线 4 是框架级 `recursion_limit` 动态计算 `max_agent_iterations * 3 + 12`）。V3 是手写 while 循环，没有框架级 recursion_limit——防线 1/2/3 就够了，不存在「框架抢断」问题。V2 的防线 4 是为了防止 LangGraph 的 `GraphRecursionError` 提前触发吃掉防线 2/3，V3 不需要这个 workaround。

> **话术**：四道防线是「纵深防御」思想——最理想是自然终止，但模型可能陷入循环，所以加重复检测兜底；重复检测可能被绕过（参数微调），所以加最大迭代硬上限。而且要让防线之间不互相打架。

---

## 5.5 混合态边界转换

### 出：_langchain_msg_to_dict（`agent_v3.py:697-729`）

```python
@staticmethod
def _langchain_msg_to_dict(msg) -> dict:
    if isinstance(msg, SystemMessage): return {"role": "system", "content": msg.content}
    if isinstance(msg, HumanMessage): return {"role": "user", "content": msg.content}
    if isinstance(msg, ToolMessage): return {"role": "tool", "content": msg.content, "tool_call_id": ...}
    if isinstance(msg, AIMessage):
        d = {"role": "assistant", "content": msg.content or ""}
        if getattr(msg, "tool_calls", None):
            d["tool_calls"] = [{"id": ..., "type": "function", "function": {"name": ..., "arguments": json.dumps(...)}} for tc in msg.tool_calls]
        return d
```

把 langchain 消息转 dict 给 `LLMClient`（它期望自建消息类型的 `to_dict()` 格式，等价于 OpenAI dict）。

### 入：AIMsg → AIMessage 重建（`agent_v3.py:432-455`）

```python
# 无 tool_calls
state["messages"].append(AIMessage(
    content=strip_think_tags(ai_msg.content),
    additional_kwargs={"reasoning": ai_msg.reasoning} if ai_msg.reasoning else {},
))

# 有 tool_calls
lc_ai_msg = AIMessage(
    content=strip_think_tags(ai_msg.content),
    tool_calls=[{"id": ..., "name": ..., "args": safe_parse_tool_args(...)} for tc in ai_msg.tool_calls],
    additional_kwargs={"reasoning": ai_msg.reasoning} if ai_msg.reasoning else {},
)
```

把 `LLMClient` 返回的 `AIMsg`（自建）转回 langchain `AIMessage` 存入 state。

### 为什么保留混合态

> 分阶段迁移策略——「只换图引擎，不换业务模块」。`ContextManager.build_llm_messages` / `compact_messages` 内部用 langchain 消息类型（`RemoveMessage` 等），如果同时换掉，改动面太大、引入难定位的 bug。当前混合态的代价是两个边界转换函数（`_langchain_msg_to_dict` + `AIMsg → AIMessage` 重建），可接受。Phase 5.5 目标：`ContextManager` 也改用自建消息类型，届时 `_import_langchain_messages()` 可删除。

### reasoning 存入 additional_kwargs

注意 `additional_kwargs={"reasoning": ai_msg.reasoning}`——这是为了**前端页面刷新时恢复推理面板**。langchain `AIMessage.additional_kwargs` 是一个自由 dict，V3 复用它存 reasoning。worker 序列化 history 时从这里取（`worker_process.py:254-261`）。

---

## 5.6 precheck 与 compact

### _precheck（`agent_v3.py:282-306`）

```python
def _precheck(self, state, compact_pct_override):
    messages = state.get("messages", [])
    if not messages: return

    pct = compact_pct_override or int(self.settings.get("compact_threshold_pct", 80) or 80)
    if pct <= 0: return

    try:
        current_tokens = count_tokens(messages)
        context_window = get_context_window()
        threshold = int(context_window * (pct / 100.0))
        if current_tokens < threshold: return
    except Exception:
        return    # token 计数失败不阻塞

    yield from self._compact_messages(state, auto=True)    # 达阈值 → 压缩
```

> **每轮一次**：precheck 在 ReAct 循环**之前**过一次，不在 `agent_llm ⇄ tools` 循环路径上。所以每轮最多压缩一次，避免循环内反复压缩。

### _compact_messages（`agent_v3.py:308-338`）

```python
def _compact_messages(self, state, auto=False):
    messages = state.get("messages", [])
    compact_result = self.context_manager.compact_messages(list(messages))

    # 模拟 add_messages reducer：删旧 + 追加新
    removals = [RemoveMessage(id=msg.id) for msg in messages if hasattr(msg, "id") and msg.id]
    new_messages = compact_result.compressed_messages

    if auto: yield {"type": "auto_compact", ...}
    yield {"type": "messages_snapshot", "messages": new_messages}

    state["messages"] = new_messages + [m for m in messages if not (hasattr(m, "id") and m.id)]
```

> **V2 对照**：V2 靠 LangGraph 的 `add_messages` reducer + `RemoveMessage` 自动处理删旧加新。V3 手写等价逻辑——`state["messages"]` 直接替换。`messages_snapshot` 事件让 worker 做全量替换（而非增量），修复了 V2 压缩不落盘的 bug。

### 三路 compact 触发

| 路径 | 触发 | 代码 |
|------|------|------|
| 路径 A（precheck 自动） | token 达 `context_window × compact_threshold_pct%` | `agent_v3.py:306` |
| 路径 B（Agent 主动） | LLM 调 `compact_conversation` 工具 → `state_updates["compact_requested"]` | `agent_v3.py:494` |
| 路径 C（用户手动） | CLI `/compact` 命令 / Web 按钮 | 直接调 `context_manager.compact_messages` |

> **压缩本身的实现**（保留最近 N 条 + 早期消息 LLM 摘要）见 [07 章 §1](07-Context与Memory工程.md)。

---

## 5.7 子智能体（task 工具）

> 子智能体是一个独立的 agentic loop，与主图解耦。这里只列要点，完整在 [03 章 task 工具](03-声明式三层工具.md)。

### task 工具（`src/tools/sub_agent.py`）

```python
# 子 agent 是独立 ReAct loop，max_iterations=10
# 内部跑：LLM → tool_call → 执行 → ToolMessage 回灌 → 重复
# 用 contextvar _current_depth 做递归深度限制（sub_agent_max_depth=2）
```

### 深度控制为什么用 contextvar 而非 threading.local

`task` 工具可以嵌套（agent 调 task，子 agent 再调 task），也可能在 `ThreadPoolExecutor` 的并发路径里执行。

`threading.local` 是 **per-thread** 的——新线程里是全新空状态（depth 重置为 0），不继承父线程的值。并发子 agent 各自看到 depth=0，全部绕过深度限制。

`contextvars` 经 `copy_context()` 正确传播父线程的值，且 **per-worker 独立可写**（一个 worker 改 depth 不影响另一个）。

> 详见 [06 章 contextvars 专题](06-流式韧性工程.md)。

---

## 5.8 兼容性方法（供 worker_process.py / cli.py 调用）

```python
def get_todos(self) -> list:
    """V3 的 todos 在 stream_invoke 的 state 里，不常驻 agent。
    worker_process.py 在 stream 结束后调此方法同步 todos。返回空列表——
    实际 todos 通过 todos_update 事件 + turn_messages 传递。"""
    return []

def rebind_tools(self) -> None:
    """V3 的工具在每次 stream_invoke 时通过 _resolve_and_bind_tools 动态组装，
    所以这里不需要预先 bind。但为了兼容 worker 的调用（它期望此方法存在），空实现。"""

def shutdown_mcp(self) -> None:
    """关闭 MCP 连接（与 graph.py:279 兼容）。"""

@property
def llm_with_tools(self):
    """兼容属性：worker _build_llm_override 依赖此属性。
    V3 不用 langchain 的 bind_tools 机制，返回 mock 让 .bind() 返回 None。"""
    class _MockBind:
        def bind(self, *a, **kw): return None
    return _MockBind()
```

> 这些方法保证 `HermesAgentV3` 与 V2 的 `HermesAgent` 接口兼容——`worker_process.py` / `cli.py` 切换 agent 实现时零改动。

---

## 本章小结

`HermesAgentV3` 是 V3 的编排内核，手写 ReAct 循环取代 LangGraph 图。以下三张表从「节点 / 边 / 核心机制」三个粒度做完整对照，第一张表就能讲清「LangGraph 的每一块在 V3 里变成了什么」。

### 对照表 A：LangGraph 图节点 → V3 方法（逐节点映射）

> LangGraph 的 6 节点 `StateGraph` 在 V3 里全部变成了 `HermesAgentV3` 的普通方法调用，顺序执行，无双模式 stream、无 checkpointer。

| LangGraph 节点（V2） | 职责 | V3 对应方法 | 行号 | 取代机制 |
|---|---|---|---|---|
| `retrieve_memory` | 记忆检索 | `_retrieve_memory()` | `agent_v3.py:262` | 普通 generator 方法，`yield memory_search` 事件 |
| `precheck` | token 阈值压缩 | `_precheck()` | `agent_v3.py:282` | 普通 generator 方法，达阈值调 `_compact_messages` |
| `agent_llm` | LLM 推理（流式） | `_react_loop()` 内联 | `agent_v3.py:392-436` | `stream_with_hard_timeout` → 逐 chunk `yield token` |
| `tools` | 工具执行 | `_react_loop()` 内联 | `agent_v3.py:458-496` | `registry.process_tool_calls` + `event_sink` |
| `compact` | 上下文压缩 | `_compact_messages()` | `agent_v3.py:308` | 普通 generator 方法，手写删旧加新取代 `RemoveMessage` reducer |
| `store_memory` | 存储记忆（V2 no-op） | （删除） | — | V3 不设此节点，记忆由 `remember` 工具 / 会话结束总结负责 |

### 对照表 B：LangGraph 边 / 路由 → V3 控制流（逐边映射）

> LangGraph 靠 `add_edge` / `add_conditional_edges` 声明路由，V3 全部变成 `while` 循环内的 `if/elif` + `yield/return`，顺序可控、可 grep。

| LangGraph 边（V2） | 路由条件 | V3 对应控制流 | 行号 |
|---|---|---|---|
| `START → retrieve_memory` | 无条件 | `stream_invoke` 顺序调用 | `agent_v3.py:222` |
| `retrieve_memory → precheck` | 无条件 | 顺序调用 | `agent_v3.py:225` |
| `precheck → agent_llm` | 无条件 | 进入 `_react_loop` while 循环 | `agent_v3.py:229` |
| `agent_llm → tools`（条件） | `_should_continue`：有 tool_calls | `if ai_msg.has_tool_calls:` 分支 | `agent_v3.py:438` |
| `agent_llm → store_memory`（条件） | `_should_continue`：无 tool_calls | `else: return`（退出循环） | `agent_v3.py:426` |
| `tools → agent_llm`（条件） | `_after_tools_router`：常规 | `while` 循环回顶部 | `agent_v3.py:497`（隐式 continue） |
| `tools → compact`（条件） | `_after_tools_router`：`compact_requested` | `if state_updates.get("compact_requested"):` | `agent_v3.py:494` |
| `compact → agent_llm` | 无条件 | `_compact_messages` 返回后继续循环 | `agent_v3.py:495` |
| `store_memory → END` | 无条件 | `yield turn_messages + complete` | `agent_v3.py:248-256` |
| `interrupt()` 暂停 | `request_human_approval` 工具触发 | `except InterruptSignal:` 捕获 | `agent_v3.py:232` |
| `Command(resume)` 恢复 | 外部调 `stream_invoke(resume_payload=)` | `_handle_resume()` | `agent_v3.py:197, 544` |

### 对照表 C：核心机制逐项对比

| 维度 | V2（LangGraph / langchain） | V3（手写） | 为什么改 |
|---|---|---|---|
| **编排骨架** | `StateGraph` 6 节点 + 条件边 | `while iteration_count < max_iter` | 顺序控制可 grep，无框架黑盒 |
| **节点定义** | `graph.add_node(name, fn)` 注册 | 普通方法 `def _retrieve_memory(self, state)` | 不需要图编译，IDE 跳转直达 |
| **事件推送** | `writer({...})` + `stream_mode=["custom","values"]` 双模式 | generator `yield {...}` 单通道 | 双模式（展示用 + 累加用）合并，逻辑简化 |
| **状态结构** | `AgentState(TypedDict)` + `add_messages` reducer | `state: dict` + 手写追加 | reducer 魔法去掉，消息累积逻辑可见 |
| **消息追加** | `{"messages": [msg]}` → reducer 自动追加 | `state["messages"].append(msg)` | 显式优于隐式 |
| **消息删除（压缩）** | `RemoveMessage(id=...)` → reducer 自动删 | `state["messages"] = new + [保留的]` 手写替换 | 无 reducer 依赖，可控 |
| **状态持久化** | `MemorySaver` checkpointer（进程内） | `InterruptSnapshot`（仅 HITL 时 deepcopy） | 只在需要时存快照，非每节点 checkpoint |
| **HITL 暂停** | `langgraph.interrupt(payload)` | `raise InterruptSignal(payload)` | 异常 `try/except` 100% 可靠 |
| **HITL 检测** | `get_state().tasks[].interrupts[0].value`（0.2.76 不注入 `__interrupt__`） | `except InterruptSignal as sig:` | 不依赖框架内部行为 |
| **HITL 恢复** | `Command(resume=...)` + 同 `thread_id` | `stream_invoke(resume_payload=...)` → `_handle_resume` | 重新求值权限，mode 切换自动放行 |
| **重放问题** | `interrupt()` resume 从头重放（executor 前代码跑两遍） | 权限决策在 executor 前，approve 后首次执行 | 天然无重放 |
| **循环终止防线 1** | `_should_continue` 无 tool_calls → store_memory | `if not ai_msg.has_tool_calls: return` | 语义相同 |
| **循环终止防线 2** | `_detect_tool_loop` 签名检测 | `_detect_tool_loop`（搬自 graph.py:325-369） | 零变化 |
| **循环终止防线 3** | `iteration_count >= max_agent_iterations` | 同 | 零变化 |
| **循环终止防线 4** | 框架级 `recursion_limit = max_iter*3+12` | （删除） | V3 无框架抢断，防线 1-3 足够 |
| **config 透传** | `RunnableConfig` 必须手动透传到 `tool.invoke(args, config=config)` | 无 config 透传链 | HITL 不依赖 `__pregel_scratchpad` |
| **异常透传** | `except GraphBubbleUp: raise`（必须先于 `except Exception`） | `except InterruptSignal: raise`（同理） | 语义相同，异常类型不同 |
| **LLM 客户端** | `ChatOpenAI(streaming=True, request_timeout=...)` | `LLMClient(request_timeout=...)`（openai SDK） | 零 langchain-openai 依赖 |
| **工具绑定** | `llm.bind_tools(tools)` 返回新实例 | `stream_chat(messages, tools=[...])` 参数传 | 无不可变副本开销 |
| **工具 schema** | `convert_to_openai_tool(@tool)` 自动生成 | `ToolSpec.to_openai()`（YAML 定义） | schema 完全可控，不抛 KeyError |
| **工具执行** | `tool.invoke(args, config=config)` | `registry.execute(spec, args, ctx)` | `ToolResult` 统一返回，无 ResultHandler 注册表 |
| **工具权限** | 硬编码 `_INTERRUPT_TOOL_NAMES` / `BLOCKED` | `decide()` 三层叠加 | 声明式，加工具不改代码 |
| **消息类型** | `langchain_core.messages.*` | ⚠️ 混合态（内部 langchain，边界 `_langchain_msg_to_dict` 转 dict） | Phase 5.5 彻底换 |
| **流式超时** | `_stream_llm_with_hard_timeout`（底层 langchain stream） | `stream_with_hard_timeout`（底层 `LLMClient.stream_chat`） | 相同双 deadline，底层换 SDK |
| **依赖** | langchain-core + langchain-openai + langgraph（三包） | openai SDK + 自建组件 | 依赖瘦身 |

### 一图看懂控制流映射

```
V2 LangGraph StateGraph                         V3 HermesAgentV3 stream_invoke
═══════════════════════                         ══════════════════════════════

START                                            stream_invoke() 入口
  │ add_edge                                       │ set contextvars + 组装工具
  ▼                                                ▼
retrieve_memory ──edge──> precheck                _retrieve_memory()    _precheck()
                                            │                            
                                            ▼  进入 while 循环
  ┌─ conditional_edges ─┐                         ┌─ if/elif ──────────┐
  │                     │                         │                    │
  │   agent_llm ◄───────┼─ tools                  │  LLM 流式 ─────────┼─ 工具执行
  │      │              │  ▲                      │     │              │  ▲
  │      │ tool_calls ──┘  │ no_tool_calls        │     │ has_calls ──┘  │ no_calls
  │      │                 │                      │     │                 │
  │      ▼                 ▼                      │     ▼                 ▼
  │  (循环)            store_memory              │  continue while     退出 while
  │                        │                      │                        │
  └─ interrupt()           ▼ END                  └─ except              yield turn_messages
    ▲                          │                    InterruptSignal          + complete
    │                          │                       │                        │
    └── Command(resume) ───────┘                  _handle_resume()          END
```

**混合态是当前唯一的妥协**——Phase 5.5 目标是 `ContextManager` 也脱离 langchain 消息，届时整个 `HermesAgentV3` 零 langchain 依赖。

---

> **下一章**：[06-流式韧性工程](06-流式韧性工程.md) —— stdout 写线程 / per-user 子进程 / IPC 超时 / worker 锁 503 / contextvars 三坑。
