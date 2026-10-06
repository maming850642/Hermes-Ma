# 第 2 章 · 工具调度核心 —— `src/agent/tools_registry.py`

> 承接第 1 章。本章主角是 `ToolRegistry`：它在 graph.py 的 `tools` 节点里被调用，负责「LLM 想调工具 → 真正执行 → 把结果喂回 LLM」这一段。
> 它和 graph.py 共享同一套 contextvars / interrupt 透传范式，但多了一个核心维度：**串行 vs 并发执行策略**。坑更多。

---

## 0. 一句话定位

`ToolRegistry` 是工具的**调度中枢**：维护工具名→实例映射、决定单工具走串行还是多工具走并发、用 ResultHandler 注册表把异构的工具返回值统一成 `ToolResult`，并负责把会触发 HITL 的工具导向正确的执行路径。

如果说 graph.py 管的是「图的拓扑」，tools_registry.py 管的就是「图的 tools 节点内部到底发生了什么」。

> **改名说明**：commit `4e038ab` 把原 `src/agent/tools.py` 重命名为 `src/agent/tools_registry.py`（从 631 行演进到 652 行）。本章所有 `tools_registry.py` 指的都是这个文件。

---

## 1. 模块全景

| 文件 | 行数 | 角色 |
|------|------|------|
| `src/agent/tools_registry.py` | 652 | **本章主角**：ToolRegistry 注册/查找/执行/调度 |
| `src/agent/tool_result.py` | 56 | 配套：ToolResult 协议 + ResultHandler 类型别名（第 3 章详讲） |

`ToolRegistry` 内部的结构（截至 2026-07-03）：

| 组成部分 | 行号区间 | 职责 |
|---------|---------|------|
| `_INTERRUPT_TOOL_NAMES` 常量 | 47 | 声明哪些工具会触发 interrupt（影响执行路径选择） |
| 内置 ResultHandler（2 个） | 56-84 | `_handle_write_todos` / `_handle_compact_conversation` |
| `ToolRegistry` 类 | 87-653 | 注册表本体 |
| ├─ 注册/查找 | 104-165 | `__init__` / `register_handler` / `unregister_handler` / `get_tool` / `has_tool` |
| ├─ 结果处理 | 167-187 | `_process_result`（查注册表，无则走默认） |
| ├─ 单工具执行 | 189-256 | `execute_tool`（主线程同步） |
| ├─ 线程安全单工具 | 258-302 | `execute_tool_sync`（worker 线程用） |
| ├─ **调度核心** | 304-580 | `process_tool_calls`（tools 节点入口） |
| ├─ 子智能体适配 | 582-649 | `_execute_subagent_tool` |
| └─ todos 兼容 | 651-653 | `get_todos` |

**本章核心看点**（按重要性）：

1. ResultHandler 注册表 —— 用「注册」替代 if-elif 链
2. `process_tool_calls` 的执行路径决策 —— 串行 vs 并发 vs interrupt 强制串行
3. 并发执行的两层死锁修复 —— contextvars 重入 + executor shutdown 阻塞 + 整批超时
4. interrupt 透传的两条铁律 —— `GraphBubbleUp` 异常 + 主线程执行
5. 子智能体的双路径适配 —— 串行用 writer、并发用 event_queue
6. 参数强转防御 —— LLM tool_call 绕过 pydantic 校验的坑

---

## 2. 架构与数据流

### 2.1 tools 节点内部的执行决策树

```
graph.py: _tools_node(state, writer, config)
  └─ tool_registry.process_tool_calls(state, writer, config)   [tools_registry.py: process_tool_calls]
       │
       ├─ 取 last_msg.tool_calls，过滤空参数（Bug 10 修复）
       │
       ├─ 决策执行路径：
       │   if 单工具 OR 含 interrupt 工具:
       │       → 串行同步路径（主线程）
       │   else (多工具且无 interrupt):
       │       → 并发路径（显式 ThreadPoolExecutor + 整批超时）
       │
       ├─ 每个工具执行：
       │   if tool_name == "task":
       │       → _execute_subagent_tool（流式子智能体，token_sink 适配）
       │   else:
       │       → execute_tool / execute_tool_sync
       │           └─ tool_func.invoke(args, config=config)  ← config 透传给 interrupt 工具
       │           └─ _process_result → ToolResult
       │
       └─ 返回 {"messages": [ToolMessage...], **state_updates}
              ↑ state_updates 含 todos / compact_requested 等
```

graph.py 的 `_tools_node` 只是薄封装，核心是接收 `config` 并下传（Bug 12 修复，详见 3.5）：

```python
def _tools_node(self, state, writer, config: RunnableConfig) -> dict:
    updates = self.tool_registry.process_tool_calls(state, writer=writer, config=config)
    return updates
```

### 2.2 两条执行路径的差异

| 维度 | 串行路径（`execute_tool`） | 并发路径（`execute_tool_sync`） |
|------|--------------------------|------------------------------|
| 触发条件 | 单工具 **或** 含 interrupt 工具 | 多工具且**无** interrupt 工具 |
| 线程 | 主线程（Pregel task 线程） | ThreadPoolExecutor worker（最多 4） |
| 事件推送 | 直接 `writer({...})` | 经 `event_queue` 中转后主线程 `writer` |
| state 合并 | 实时 `state_updates.update()` | worker 各自收集，主线程串行合并 |
| interrupt 支持 | ✅（异常在主线程冒泡） | ❌（线程隔离会吞掉 interrupt） |
| contextvars | 天然在主线程上下文 | 必须 `copy_context()` 逐 worker 传播 |

---

## 3. 逐段深潜

### 3.1 ResultHandler 注册表 —— 消灭 if-elif 链

#### 改造前的问题（`tool_result.py` 模块 docstring 记录）

```
之前 write_todos 返回 dict、compact_conversation 返回特殊字符串，
ToolRegistry 中用 if-elif-else 链逐个硬编码处理，存在三个问题：
1. 隐式契约：返回格式只在 tools_registry.py 中硬编码，无类型约束
2. 静默失败：格式变化不报错但功能失效
3. 扩展性差：新工具需添加新的 if-elif 分支
```

#### 改造后的机制

核心是三个东西（`tool_result.py` + `tools_registry.py`）：

```python
# tool_result.py —— 协议定义
ResultHandler = Callable[[Any, str, dict], "ToolResult"]   # (原始结果, 工具名, 参数) -> ToolResult

@dataclass
class ToolResult:
    content: str                              # 返回给 LLM 的文本
    state_updates: dict[str, Any] = ...       # 要合并进 AgentState 的更新

    @property
    def has_state_updates(self) -> bool:      # 是否有状态更新
        return bool(self.state_updates)
```

```python
# tools_registry.py —— 两个内置 handler
def _handle_write_todos(result, tool_name, tool_args) -> ToolResult:
    # write_todos 返回 {"todos": [...], "summary": "..."}
    # → 把 todos 提到 state_updates，summary 作为 content
    return ToolResult(content=result.get("summary", ...),
                      state_updates={"todos": result["todos"]})

def _handle_compact_conversation(result, tool_name, tool_args) -> ToolResult:
    # compact_conversation 返回 "COMPACT_REQUESTED" 信号字符串
    # → 转成 compact_requested=True 的 state 更新
    if result == "COMPACT_REQUESTED":
        return ToolResult(content="上下文压缩已触发...",
                          state_updates={"compact_requested": True})
```

调度时（`_process_result`）：

```python
def _process_result(self, result, tool_name, tool_args) -> ToolResult:
    handler = self._result_handlers.get(tool_name)   # 查注册表
    if handler is not None:
        return handler(result, tool_name, tool_args)
    return ToolResult(content=str(result))            # 默认：纯转字符串
```

**为什么这么写：**

- **「需要更新 state 的工具」是少数特例**。绝大多数工具（web_search、virtual_fs 等）只返回文本，走默认 `str(result)` 即可。只有 `write_todos`（要更新 todos）和 `compact_conversation`（要翻转 compact_requested flag）需要副作用。
- **注册表把「特例处理」从核心流程里剥离**。新增一个有副作用的工具，只需 `registry.register_handler("new_tool", handler)`，`execute_tool` / `process_tool_calls` 一行都不用改。这就是 `tool_result.py` 模块 docstring 说的「扩展性」。配套还有 `unregister_handler`（返回 bool 表示是否成功取消），便于动态装卸。
- **ToolResult 用 dataclass 而非 dict**：有类型约束、有默认值、有 `has_state_updates` 属性，比裸 dict 安全。

> **设计模式**：这是经典的 **Strategy + Registry** 组合。每个 handler 是一个 strategy，`_result_handlers` dict 是 registry。对比 if-elif 链，它把「开闭原则」落到了实处——对扩展开放（注册新 handler），对修改关闭（不改 execute_tool）。

#### 一个遗留：`_last_todos` 缓存

```python
# 向后兼容：write_todos 的 _last_todos 缓存（execute_tool / execute_tool_sync 各一处）
if tool_name == "write_todos" and isinstance(raw_result, dict) and "todos" in raw_result:
    self._last_todos = raw_result["todos"]
```

注意这里**仍然是工具名硬编码判断**，和 ResultHandler 注册表的初衷相悖。注释写「向后兼容」——说明这是给 `get_todos()` 用的旧接口。ResultHandler 已经把 todos 放进了 `state_updates`，state 里也有 todos，`_last_todos` 是个**冗余副本**。graph.py 的 `rebind_tools` 还要特意迁移这个缓存（见第 1 章）——这是个技术债，第 8 章会汇总。

---

### 3.2 `process_tool_calls` 的执行路径决策 —— 三选一

这是本章最核心的函数。它的前半段是「参数校验」，后半段是「路径选择」。

#### 参数校验：空参数过滤（Bug 10 修复）

```python
for tc in last_msg.tool_calls:
    args = tc.get("args") or {}
    if args:
        valid_tool_calls.append(tc)       # 有参数，正常
        continue
    # args 为空：判断工具本身是否需要参数
    tool_obj = self.tool_map.get(tc.get("name", ""))
    if tool_obj is None:
        valid_tool_calls.append(tc)       # 工具不存在，归入 valid 让后续报错
    elif getattr(tool_obj, "args", None):
        empty_args_calls.append(tc)       # 工具有参数定义但传空 → 真缺失
    else:
        valid_tool_calls.append(tc)       # 工具本身无参数（如 compact_conversation），args={} 合法
```

**动机**（Bug 10）：无参数工具（如 `compact_conversation`）的 `args={}` 是**合法**的，早期一刀切把空 args 当「参数缺失」跳过，导致这些工具永远调不动。修复后区分「工具本就无参数」和「工具要参数却传空」两种情况。对后者，会构造一条明确的 `ToolMessage` 错误提示返回给 LLM，而不是静默丢弃。

#### 路径选择：interrupt 工具强制串行

```python
contains_interrupt = any(
    tc["name"] in _INTERRUPT_TOOL_NAMES for tc in valid_tool_calls
)
# 单工具 或 含 interrupt 工具：串行同步执行
if len(valid_tool_calls) == 1 or contains_interrupt:
    ...  # 串行路径
else:
    ...  # 并发路径
```

其中 `_INTERRUPT_TOOL_NAMES = frozenset({"request_human_approval", "run_shell"})`。

**为什么含 interrupt 工具就必须串行**（函数注释已详述）：

> `GraphInterrupt` 必须在 Pregel 的 task 线程（调用 stream/invoke 的线程）里冒泡，才会被图运行时识别为「暂停」。放进 ThreadPoolExecutor worker 线程执行时，**线程隔离会让异常无法传播**到图的 task 线程，interrupt 失效。

这是个硬约束。所以哪怕 LLM 一次并发请求了 3 个工具，只要其中有 1 个是 `request_human_approval` 或 `run_shell`，整批**降级为串行**。安全优先于性能。

> **对比第 1 章**：graph.py 的 contextvars 问题是「数据丢失」（callback 状态污染），这里是「控制信号丢失」（interrupt 异常被线程边界吞掉）。两者都是「跨线程边界」的代价，但表现不同。

---

### 3.3 串行路径 —— 简单但有几个细节

```python
if len(valid_tool_calls) == 1 or contains_interrupt:
    for tc in valid_tool_calls:
        if writer:
            writer({"type": "tool_start", ...})

        if tc["name"] == "task":
            # 子智能体特殊处理：流式 token
            token_sink = lambda tok, tid=tc["id"], sn=...: writer({...})
            result_str = self._execute_subagent_tool(tc, token_sink)
        else:
            result_str, updates = self.execute_tool(tc, state, config=config)
            state_updates.update(updates)

        if writer:
            writer({"type": "tool_end", ...})
            # write_todos 的结果含 todos → 推送结构化事件
            if "todos" in updates:
                writer({"type": "todos_update", "todos": updates["todos"]})
        tool_messages.append(ToolMessage(content=result_str, tool_call_id=tc["id"]))
```

**几个细节**：

1. **`task` 工具有专门分支**：因为它要流式输出 token，而普通工具只返回最终字符串。token_sink 是个适配器，把「子智能体吐 token」翻译成「writer 推 subagent_token 事件」。
2. **lambda 的默认参数陷阱已正确处理**：`lambda tok, tid=tc["id"], sn=...: ...` 用默认参数捕获循环变量，避免闭包晚绑定 bug（经典 Python 坑）。
3. **`config=config` 全程透传**：这是 interrupt 能工作的前提（见 3.5）。
4. **`todos_update` 结构化事件**：当 `write_todos` 的返回含 todos 时，除 `tool_end.result`（文字摘要）外，额外推送一条 `todos_update` 事件，让 web 前端常驻小窗能实时渲染清单。并发路径里也对称地有同样处理（见 3.4）。

---

### 3.4 并发路径与两层死锁修复 —— 本章精华

并发执行用 `ThreadPoolExecutor`，但这里经历了**两层修复**：第一层是 contextvars 重入死锁，第二层是 executor shutdown 阻塞死锁 + 整批超时。

#### 第一层修复：contextvars 重入死锁（commit 93f97c6 → 当前）

**错误写法**（所有 worker 共用同一个 ctx.run）：

```python
# 旧写法（commit 93f97c6）所有 worker 共用同一个 ctx.run
ctx = contextvars.copy_context()          # 在主线程 copy 一次
def _submit(tc_item):
    return executor.submit(ctx.run, _run_tool, tc_item)   # 所有 worker 用同一个 ctx
```

**为什么死锁**（源码注释完整记录）：

> CPython 的 `Context.run` **不能重入**。当第一个 worker 在 `ctx.run` 内阻塞（如 web_search 的 httpx I/O）时，其余 worker 立刻抛
> `RuntimeError: cannot enter context: ... is already entered`
> 导致它们的 `_run_tool` 体根本不执行：不 put tool_start、不 put tool_end → 主循环 `while completed_count < len` **永远等不到** → 永久卡死。
> 表象：N 个并发工具只有 1 个出现内部日志，其余无日志，agent 卡死。

这是个**极其隐蔽**的 bug：`Context.run` 的不可重入性在 Python 文档里写得很清楚，但「多个线程共用同一个 Context 对象」时才会触发，单线程代码永远不会撞上。

**当前写法**：每个 worker 各自 copy：

```python
def _submit(tc_item):
    ctx = contextvars.copy_context()                    # 每个 worker 独立 copy
    return executor.submit(ctx.run, _run_tool, tc_item)
```

每个 worker 拿到的是**独立的 Context 副本**，互不冲突。既传播了 contextvars（langchain callback 配置能传进去），又避免了重入。

#### 第二层修复：executor shutdown 阻塞 + 整批超时（commit 4391c01）

**旧写法的问题**（用 `with` 语句管理 executor）：

```python
# 旧写法：with ThreadPoolExecutor(...) as executor:
with ThreadPoolExecutor(max_workers=...) as executor:
    ...  # 提交任务
# with 块退出时，__exit__ 调用 executor.shutdown(wait=True)
```

`with` 语句的 `__exit__` 会调 `shutdown(wait=True)` —— **阻塞等待所有 worker 完成**。若某个 worker 卡在不可中断 I/O（无超时的网络/MCP 工具），`completed_count` 永远到不了，`__exit__` 会**永久阻塞**，整轮 agent 死锁。

**当前写法**（显式管理 executor + 总超时 + 非阻塞 shutdown）：

```python
executor = ThreadPoolExecutor(max_workers=min(len(valid_tool_calls), 4))
try:
    # ... 提交任务、消费 event_queue ...

    _TOTAL_TIMEOUT = 120.0  # 整批工具的总硬超时，避免单个卡死工具拖垮整轮
    _batch_deadline = _t.time() + _TOTAL_TIMEOUT

    while completed_count < len(valid_tool_calls):
        if _t.time() >= _batch_deadline:
            # 总超时：把未完成的工具标记为超时，跳出循环
            for fut, tid in futures.items():
                if tid not in results_map:
                    results_map[tid] = "工具执行超时（整批 120s 上限）"
                    completed_count += 1
            logger.warning(f"并发工具执行整批超时({_TOTAL_TIMEOUT}s)，剩余工具标记超时")
            break
        try:
            event = event_queue.get(timeout=0.1)
        except _queue.Empty:
            # 健壮性兜底：检查是否有 worker 异常（见下文）
            ...
finally:
    # 取消未启动的 future；不等待仍在运行的 worker（避免卡死 I/O 拖垮整轮）。
    # 运行中的 daemon 性质 worker 在进程退出时由 OS 回收。
    executor.shutdown(wait=False, cancel_futures=True)
```

**三个关键改动**：

1. **`_TOTAL_TIMEOUT = 120.0` 整批超时**：给整批工具设一个硬上限（120 秒）。超时后把未完成的工具标记为「超时错误」，跳出消费循环，不再死等。即使某个 MCP 工具的网络 I/O 没有自身超时，整轮也不会被它拖垮。
2. **`executor.shutdown(wait=False, cancel_futures=True)`**：放在 `finally` 里，用 `wait=False` 不阻塞、`cancel_futures=True` 取消尚未启动的任务。`with` 语句的 `wait=True` 是死锁根源，这里彻底绕开。
3. **不再用 `with` 语句**：把 executor 生命周期完全手动管理，才能在超时后立即 `break` + 非阻塞 shutdown，不留阻塞面。

#### 主循环的健壮性兜底

```python
except _queue.Empty:
    # 兜底：检查是否有 worker 异常
    for fut, tid in futures.items():
        if fut.done() and tid not in results_map:
            exc = fut.exception()
            results_map[tid] = f"工具执行错误: {exc}" if exc else "工具执行失败"
            completed_count += 1       # 防止永久 busy-spin
    continue
```

**动机**：如果 worker 抛异常而没 put tool_end，`completed_count` 永远到不了。这个兜底检查 done future，把异常当作该工具「完成」，避免主循环死等。这是「防御性编程」——即使 contextvars 和 shutdown 都修好了，仍要防 worker 内其它异常。

#### 替代方案对比：为什么不用 `asyncio.gather`

| 方案 | 为何不采用 |
|------|-----------|
| `asyncio.gather` | 整个 LangGraph 图是同步的，工具也多是同步 I/O，引入 async 改造成本过大 |
| `concurrent.futures.ProcessPoolExecutor` | 进程间不能共享 langchain contextvar、不能共享 LLM client，开销过大 |
| 顺序执行（不并发） | 多工具时延迟翻倍，体验差 |
| **当前方案**（显式 ThreadPoolExecutor + 每 worker copy_context + 整批超时） | ✅ 线程间可共享不可变状态，contextvars 可传播，超时有兜底 |

> **对比第 1 章 graph.py 的 contextvars 修复**：
> - graph.py 是「单 worker，主线程消费 Queue」——只需 copy 一次 context
> - tools_registry.py 是「多 worker 并发」——必须**每个 worker** 各 copy 一次，否则重入死锁
>
> 同一个范式（`copy_context()`），在不同并发模型下用法不同。这是读源码要领：**模式可以复用，但并发语义决定了具体写法**。

---

### 3.5 interrupt 透传的两条铁律

要让 `request_human_approval` / `run_shell` 的 `interrupt()` 真正能暂停图，必须同时满足两条：

#### 铁律 1：`GraphBubbleUp` 异常必须原样 raise，不能被 `except Exception` 吞

```python
# execute_tool 和 execute_tool_sync 都有：
except GraphBubbleUp:
    # interrupt() 的暂停信号（GraphInterrupt 是其子类），必须原样透传给 Pregel
    raise
except Exception as e:
    result_str = f"工具执行错误: {str(e)}"    # ← 若 GraphBubbleUp 排在后面，会被这里吞掉
```

**关键**：`except` 的顺序。`GraphBubbleUp` 必须在 `except Exception` **之前**，否则会被吞。Python 异常匹配是按顺序的，先匹配先捕获。

#### 铁律 2：config 必须一路透传到 `tool_func.invoke`

```python
raw_result = tool_func.invoke(tool_args, config=config)   # ← config 不能省
```

**为什么**：`interrupt()` 在 LangGraph 1.x 依赖 `config["configurable"]["__pregel_scratchpad"]`。这个 key 由 Pregel 运行时注入到**节点**的 config，但只有把 config 传到 `tool.invoke(args, config=config)`，工具内部的 `interrupt()` 才能读到它。

graph.py 的 `_tools_node` 接收 config 并下传：

```python
def _tools_node(self, state, writer, config: RunnableConfig) -> dict:
    updates = self.tool_registry.process_tool_calls(state, writer=writer, config=config)
    return updates
```

完整的 config 传播链：

```
Pregel 运行时 → _tools_node(config) → process_tool_calls(config) 
  → execute_tool(config) → tool_func.invoke(args, config=config) → interrupt() 读到 scratchpad
```

**任何一环断了，HITL 就失效**。这是为什么 graph.py 第 1 章把接收并下传 config 列为 Bug 12 的修复内容。

> **通用教训**：当框架依赖「隐式上下文」（config 里的特殊 key），你必须像对待显式参数一样严格地透传它。漏传不会报错，只会让功能「静默失效」——最难排查的一类 bug。

---

### 3.6 子智能体的双路径适配

`task` 工具在串行和并发两条路径里都需要流式输出 token，但两条路径的「输出通道」不同：
- 串行：直接 `writer({...})`
- 并发：`event_queue.put({...})`（worker 不能直接调 writer，writer 非线程安全）

解法是 **token_sink 适配器**（策略模式的微型应用）：

```python
# 串行路径：sink 直接调 writer
token_sink = lambda tok, tid=tc["id"], sn=subagent_name: writer({
    "type": "subagent_token", "tool_id": tid, "tool_name": "task",
    "subagent_name": sn, "content": tok,
})

# 并发路径：sink 往 queue 放
def _subagent_sink(tok, tid=tool_id, sn=subagent_name, q=event_queue):
    q.put({
        "type": "subagent_token", "tool_id": tid, "tool_name": "task",
        "subagent_name": sn, "content": tok,
    })
```

`_execute_subagent_tool` 只认 `token_sink(token)` 这个统一接口，不关心 token 最终去哪。这是「依赖倒置」——把输出方式抽象成回调，调用方注入具体实现。

> 两条路径的 `subagent_token` 事件字段已**对齐**（都含 `tool_name: "task"`），消除早期版本字段不一致的小瑕疵。

#### 参数强转防御

```python
# LLM tool_call 的数字参数可能是 str（如 "60" 而非 60）
raw_timeout = tool_args.get("timeout")
timeout = int(raw_timeout) if raw_timeout is not None else None
raw_max_tokens = tool_args.get("max_tokens")
max_tokens = int(raw_max_tokens) if raw_max_tokens is not None else None
# inherit_tools 可能是 "true"/"false" 字符串
raw_inherit = tool_args.get("inherit_tools", False)
if isinstance(raw_inherit, str):
    inherit_tools = raw_inherit.strip().lower() in ("true", "1", "yes")
else:
    inherit_tools = bool(raw_inherit)
```

**动机**：LLM 的 tool_call 参数**绕过了 `@tool` 的 pydantic 校验**（因为这条路径是 `_execute_subagent_tool` 直接从 dict 取值，不走 langchain 的 invoke 校验链）。所以 `timeout` 可能是字符串 `"60"`，导致 `StreamingSubAgent` 里 `self._timeout > 0` 抛 `TypeError`。手动 `int()` 强转是兜底。

> **同类坑**：`sub_agent.py` 的 `StreamingSubAgent.__init__` **也**做了同样的强转（`max_tokens = int(max_tokens)`），注释说明「即使上层解析漏了也能兜住」。这是**双重防御**——两层都强转，任何一层漏了另一层兜住。合理，但也是「同一个逻辑写了两遍」的技术债。

#### 深度控制：用 contextvars 与项目范式统一

```python
# sub_agent.py
# 子智能体嵌套深度：用 contextvars 而非 threading.local。
_current_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "hermes_subagent_depth", default=0
)
```

`_execute_subagent_tool` 里配合 `_get_current_depth()` / `_set_current_depth()` 做嵌套深度检查与递增/递减：

```python
if _get_current_depth() >= max_depth:
    return f"子智能体执行失败 [depth_exceeded]: 已达到最大子智能体嵌套深度 ({max_depth})..."
_set_current_depth(_get_current_depth() + 1)
try:
    ...
finally:
    _set_current_depth(_get_current_depth() - 1)
```

**为什么用 contextvars 而非 threading.local**（源码注释已说明）：

> task 工具可能在 ToolRegistry 的 ThreadPoolExecutor 并发路径里执行。`threading.local` 在每个 worker 线程各自独立、主线程的深度修改**不传播**，会导致并发子智能体各自看到 depth=0，突破 `sub_agent_max_depth` 限制。contextvars 会随 `copy_context()` 正确传播到 worker 且各自独立可写，与项目其它隔离范式（remember 的 user_id）统一。

这正是早期用 `threading.local` 时的隐患，现已消除：并发路径的每个 worker 经 `copy_context()` 拿到独立的深度副本，主线程写入的深度值会正确传播，深度限制不再被并发绕过。

---

## 4. 设计权衡总结

### 4.1 优点

| 设计 | 价值 |
|------|------|
| ResultHandler 注册表 | 消灭 if-elif 链，新增副作用工具零改核心流程 |
| interrupt 工具强制串行 | 用最简单的方式（降级并发）保证 HITL 可靠 |
| 每 worker 独立 copy_context | 既传播 contextvars 又避免 Context.run 重入死锁 |
| 显式 executor + 整批超时 + 非阻塞 shutdown | 绕开 `with` 语句的 `wait=True` 阻塞，给卡死 I/O 设硬上限 |
| event_queue 中转 | 让 worker 线程不直接碰非线程安全的 writer |
| token_sink 适配器 | 子智能体流式输出对串行/并发路径透明 |
| future.done() 兜底 | 防止 worker 异常导致主循环永久 busy-spin |

### 4.2 技术债 / 待优化

| 项 | 说明 |
|----|------|
| `_last_todos` 冗余缓存 | 与 state_updates.todos 重复，且 graph.py 要专门迁移它 |
| 参数强转双重防御 | tools_registry.py 和 sub_agent.py 各强转一遍，逻辑重复 |
| 串行/并发两套子智能体适配代码 | token_sink 思路对，事件字段已对齐，但串行的 lambda 和并发的闭包仍是两份 |

---

## 本章小结

`tools_registry.py` 的复杂度集中在**并发与线程安全**这一个维度上，衍生出三条相互关联的设计线：

1. **执行路径三选一**（串行 / 并发 / interrupt 强制串行）——核心约束是「interrupt 异常不能跨线程边界」
2. **并发死锁的两层修复**——从「共用 ctx 重入死锁」到「每 worker 独立 copy」，再到「绕开 `with` 语句的 shutdown 阻塞 + 整批 120s 超时」，体现了 `Context.run` 不可重入与 executor 生命周期管理两个 Python 冷知识
3. **双通道事件输出**——writer（主线程）vs event_queue（worker 线程），用 token_sink 适配器统一

加上 ResultHandler 注册表（消灭 if-elif 链）和 config 全程透传（interrupt 生效前提），这五点构成 tools_registry.py 的全部设计内核。

**和第 1 章的呼应**：
- 第 1 章的「contextvars 跨线程」是单 worker 场景；本章是**多 worker** 场景，解法更复杂（每 worker copy）
- 第 1 章的「HITL 中断检测」是图层面的（get_state 查 interrupts）；本章是**工具层面**的（config 透传 + GraphBubbleUp 异常透传）——两者必须同时成立，HITL 才能端到端工作

下一章进入「上下文管理」——`context.py`（消息构建与压缩）、`memory_orch.py`（记忆检索编排）、`tool_result.py`、`session_lifecycle.py`。这组模块相对清爽，是 graph.py 和 tools_registry.py 之外的「补给线」。
