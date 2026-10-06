# 04 · 异常式 HITL 与三层权限系统

> HITL（Human-in-the-Loop）是 V3 最有技术含量的部分——V2 用 LangGraph 的 `interrupt()` / `Command(resume)` / `MemorySaver`，踩了「框架行为和文档不一致」的坑；V3 改成**异常式 HITL**：权限决策为「需审批」时抛 `InterruptSignal` 异常冒泡，上层捕获后存快照、yield 审批事件，resume 时从快照恢复 + 重新求值权限。
>
> 本章记录异常式 HITL 与三层权限的完整设计。

---

## 4.1 HITL 三件套

| | |
|---|---|
| **文件** | `src/agent/hitl.py`（~125 行） |
| **取代** | `langgraph.types.interrupt()` / `Command(resume)` / `get_state().tasks[].interrupts` / `MemorySaver` |
| **依赖** | 仅 `copy` / `dataclasses` |

### 组件清单

| 组件 | 行号 | 说明 |
|------|------|------|
| `InterruptSignal` | `hitl.py:24` | **异常**——权限求值得 `requireApproval` 时抛出 |
| `InterruptSnapshot` | `hitl.py:41` | **内存快照**——存暂停时的完整状态 |
| `InterruptStore` | `hitl.py:94` | **内存存储**——`thread_id → InterruptSnapshot` dict |

### InterruptSignal（`hitl.py:24-38`）

```python
class InterruptSignal(Exception):
    """权限求值为 requireApproval 时抛出，携带审批 payload。

    取代 langgraph.interrupt()——但语义更干净：
    - interrupt() 是「执行到一半暂停」，resume 后从头重放
    - InterruptSignal 是「执行之前就抛」，executor 根本没跑
    """
    def __init__(self, payload: dict):
        self.payload = payload    # {"action", "details", "tool_name", "tool_args", "tool_call_id"}
```

**抛出时机**：`ToolRegistryV3.execute()`（`registry_v3.py:191`）里，`decide()` 返回 `requireApproval` 时。

**捕获时机**：`HermesAgentV3._react_loop` 外层的 `except InterruptSignal`（`agent_v3.py:232`）。

> **为什么用异常而不是返回值？** 因为工具执行可能在 `_execute_concurrent` 的 worker 线程里（`registry_v3.py:359`）。审批请求必须**立即**冒泡到 `_react_loop` 的调用线程，不能等批量执行结束。异常天然支持跨函数冒泡（即便穿过 ThreadPoolExecutor 的 `future.result()` 也会重新抛出，`registry_v3.py:272`）。

### InterruptSnapshot（`hitl.py:41-91`）

```python
@dataclass
class InterruptSnapshot:
    thread_id: str
    messages: list                 # ★ copy.deepcopy 深拷贝，防后续修改污染
    pending_args: dict             # 触发中断的工具调用参数
    pending_tool_call_id: str
    pending_tool_name: str
    pending_payload: dict          # {"action", "details"}
    permission_mode_at_interrupt: str    # ★ 记录暂停时的 mode，resume 检测切换

    @classmethod
    def create(cls, thread_id, messages, pending_args, tool_call_id, tool_name, payload, permission_mode):
        return cls(
            messages=copy.deepcopy(messages),    # ★ 深拷贝
            ...
        )
```

**三个关键字段**：
- `messages`：`deepcopy` 深拷贝。resume 时基于这份快照继续，原 messages 可能被其他逻辑修改。
- `pending_args` / `pending_tool_name` / `pending_tool_call_id`：resume 时重新求值权限 + 执行所需。
- `permission_mode_at_interrupt`：**resume 时检测 mode 是否切换**——用户切到 `full_access` 就自动放行（不用再 approve 一次）。

### InterruptStore（`hitl.py:94-125`）

```python
class InterruptStore:
    """thread_id → InterruptSnapshot 的内存 dict。

    取代 MemorySaver checkpointer——但简化：
    - 内存级，进程重启即丢
    - 单用户同一时刻只保留一个快照（save 覆盖同 thread_id）
    """
    def __init__(self):
        self._store: dict[str, InterruptSnapshot] = {}

    def save(self, snapshot): ...     # 覆盖
    def get(self, thread_id): ...     # 不删
    def pop(self, thread_id): ...     # 取并删（resume 用）
    def has_pending(self, thread_id): ...
    def clear(self): ...
```

> **线程安全说明**（`hitl.py:100-101` 注释）：调用方（`stream_invoke`）通常单线程驱动 generator，`InterruptStore` 不需要加锁。如果未来改成多线程驱动（不太可能——一个用户的对话流是顺序的），再加锁。

---

## 4.2 V2 的坑 → V3 的解法

### 坑 1：interrupt() 检测失效

**V2 问题**：`langgraph.interrupt()` 在我们用的 0.2.76 版本里，**不会**向 `stream_mode=["custom","values"]` 的 chunk 注入 `__interrupt__` 键——stream 只是静默结束。旧代码用 `if "__interrupt__" in chunk` 检测，永远不命中，HITL 完全失效。

**V2 绕过**：stream 结束后用 `get_state()` 查 checkpoint——`snapshot.next` 非空且 task 带 `interrupts` 才是真中断，payload 在 `task.interrupts[0].value`。但这依赖框架内部结构，升级时随时可能再坏。

**V3 解法**：异常式 HITL。`InterruptSignal` 是 Python 异常，`try/except` 100% 可靠，不依赖任何框架内部行为：

```python
try:
    yield from self._react_loop(...)
except InterruptSignal as sig:
    yield from self._handle_interrupt(sig, ...)
```

### 坑 2：interrupt() 的「执行两次」语义

**V2 问题**：`langgraph.interrupt()` 的语义是「执行到 `interrupt()` 暂停 → resume 后从头重放」。第一次执行到 `interrupt()` 之前的代码，resume 后会**再跑一遍**。如果权限求值有副作用（比如记日志、改状态），重放会导致重复。

**V3 解法**：权限求值放在 `executor.execute()` **之前**——`requireApproval` 时 executor 根本没跑。approve 后第一次执行，天然无重放问题：

```
V2 langgraph.interrupt():
  executor 跑到一半 → interrupt() → 暂停
  resume → 从头重放 → executor 再跑一遍前面的代码 → 才到 interrupt() 后面
  ↑ 重复执行

V3 InterruptSignal:
  decide() 在 executor 之前 → requireApproval → raise InterruptSignal（executor 没跑）
  resume → 重新 decide → allow → executor 第一次执行
  ↑ 无重放
```

### 坑 3：RunnableConfig 没透传

**V2 问题**：`interrupt()` 在 LangGraph 1.x 里读 `config["configurable"]["__pregel_scratchpad"]`——这个 key 由 Pregel 运行时注入到**节点的 config** 里。但工具节点拿到 config 后，必须**一路传到 `tool.invoke(args, config=config)`**，`interrupt()` 才能读到 scratchpad。如果中间某一层"忘了传 config"（Bug 12 就是这个——最初 `_tools_node` 签名里没有 config 参数），`interrupt()` 直接 `KeyError` 或静默失败。

**V3 解法**：没有 config 透传链。权限决策在 `ToolRegistryV3.execute` 里完成，`InterruptSignal` 直接抛出，不依赖任何框架注入的 config key。

---

## 4.3 HITL 完整流程（端到端）

### 暂停流程

```
1. LLM 流式输出 tool_calls
2. _react_loop 调 registry.process_tool_calls
3. process_tool_calls 调 execute(spec, args, ctx)
4. execute 调 decide(spec, args, ctx)
5. decide 返回 requireApproval
6. execute raise InterruptSignal(payload)                    ★
7. 异常冒泡穿过 _execute_serial / _execute_with_timeout
8. 异常冒泡到 process_tool_calls 的调用方（_react_loop）
9. _react_loop 不接，继续冒泡到 stream_invoke 的 try
10. stream_invoke except InterruptSignal → _handle_interrupt
11. _handle_interrupt:
    - InterruptSnapshot.create（deepcopy messages + 记 mode）
    - interrupt_store.save(thread_id)
    - yield human_approval_request 事件
12. generator 结束，控制权回 worker → 转 IPC → SSE → 前端弹窗
```

### 恢复流程

```
1. 用户点「批准」（或切 mode 到 full_access）
2. 前端 POST /api/chat/approve（resume_payload="approve" 或 mode）
3. worker 调 stream_invoke(resume_payload=...)
4. stream_invoke 走 resume 路径 → _handle_resume
5. _handle_resume:
    - snapshot = interrupt_store.pop(thread_id)
    - mode_changed = snapshot.permission_mode_at_interrupt != ctx.permission_mode
    - 重新 decide(spec, pending_args, ctx)   ★ 不无脑恢复
    - 允许路径:
        · decision.is_allow（mode 切 full_access）或
        · resume_payload == "approve"
        · → 绕过 decide，直接 coerce + executor.execute
        · → ToolMessage(结果) 喂回 messages
        · → _react_loop 继续
    - 拒绝路径:
        · → ToolMessage("未获批准") 喂回 messages
        · → _react_loop 继续（让 LLM 决定下一步）
```

### 关键代码：_handle_resume（`agent_v3.py:544-657`）

```python
def _handle_resume(self, thread_id, resume_payload, ctx, tools_openai, ...):
    snapshot = self.interrupt_store.pop(thread_id)
    if snapshot is None:
        yield {"type": "token", "content": "⚠️ 无待处理的审批（可能已过期）"}
        return

    # 重新求值权限（mode 可能已切换）
    spec = self.registry.get_spec(snapshot.pending_tool_name)
    if spec is None:
        # 工具已不存在（MCP 断开）→ 拒绝
        ...

    decision = decide(spec, snapshot.pending_args, ctx)

    # mode 切换到 full_access → is_allow=True；或用户显式 approve
    if decision.is_allow or resume_payload.strip().lower() == "approve":
        # ★ 绕过 registry.execute 的权限决策（避免再次抛 InterruptSignal 死循环）
        coerced = coerce_args(snapshot.pending_args, spec.parameters)
        result = spec.executor.execute(coerced, ctx)
        # ToolMessage(结果) 喂回 → _react_loop 继续
    else:
        # 拒绝
        # ToolMessage("未获批准") 喂回 → _react_loop 继续
```

> **为什么 resume 时要「绕过 decide 直接调 executor」？** 因为 `registry.execute()` 内部会再调一次 `decide()`。如果用户 approve 了但 mode 仍是 `before_changes`，`decide()` 会再次返回 `requireApproval` → 再次抛 `InterruptSignal` → 死循环。所以 resume 的允许路径**绕过 `registry.execute`**，直接 `coerce + executor.execute`。但 Layer 3 的 `force_deny`（如 fork bomb）仍通过 `try/except InterruptSignal` 兜底（`agent_v3.py:595-600`）。

---

## 4.4 mode 切换自动放行

V3 HITL 的一个亮点：**resume 时重新求值权限，mode 切换自动放行**。

```
用户在 before_changes 模式下调 run_shell(rm -rf ...)
  → decide 返回 requireApproval → InterruptSignal 暂停
  → 用户切到 full_access 模式（前端按钮）
  → 用户点「恢复」
  → _handle_resume 重新 decide
  → mode=full_access + destructive=True → allow
  → 自动放行执行，不需要再 approve
```

实现靠 `snapshot.permission_mode_at_interrupt`（`hitl.py:67`）记录暂停时的 mode，resume 时 `ctx.permission_mode` 是当前最新 mode，两者比较 + 重新 `decide()`。

> **为什么这么设计？** 用户体验考虑——用户既然主动切到了 `full_access`，就是表达「我信任接下来的操作」，不应该再被审批卡住。V2 的 LangGraph resume 不支持这个——resume 只是恢复 checkpoint，不重新求值权限。

---

## 4.5 mode_guidance —— 权限模式注入 LLM

| | |
|---|---|
| **文件** | `src/agent/mode_guidance.py`（~79 行） |

### build_mode_prompt_section（`mode_guidance.py:65-79`）

```python
def build_mode_prompt_section(mode: str) -> str:
    guidance = get_mode_guidance(mode)
    display_name = get_mode_display_name(mode)
    return f"\n\n## 当前权限模式：{display_name}\n{guidance}"
```

注入到 system prompt 末尾（`agent_v3.py:377-381`）：

```python
mode_section = build_mode_prompt_section(ctx.permission_mode)
llm_messages[0] = SystemMessage(content=llm_messages[0].content + "\n\n" + mode_section)
```

### 三种 mode guidance（`mode_guidance.py:38-52`）

| mode | 显示名 | guidance |
|------|--------|----------|
| `full_access` | 完全访问 | 所有工具可直接调用，无需顾虑 |
| `before_changes` | 变更前访问 | 修改/删除/副作用命令前系统暂停确认，可正常调用不必犹豫 |
| `plan` | 计划模式 | 不能执行破坏性操作；**显式点名 remember/write_todos/compact_conversation「可以」** |

> **为什么 plan 模式要显式点名？** 防止模型过度泛化把 `remember`（记忆）当"修改文件"拒绝。`remember` 的 `side_effects.writes_state=True` 但 `destructive=False`，权限层不会拦截，但 LLM 可能因为 plan 模式提示而自我审查过度。显式点名消除歧义。

### 两层兜底（`mode_guidance.py:16-19`）

- **Layer 1（prompt）**：mode guidance 减少无谓的 destructive 工具调用
- **Layer 2（执行层）**：`decide()` 保证即使模型在 plan 下真的调了 destructive 工具，也会被 deny/requireApproval 拦截

> **话术**：权限是「prompt 引导 + 执行层硬保证」双层设计。prompt 减少摩擦（模型不乱调），执行层兜底安全（真调了也不会误伤）。不依赖模型的「听话」。

---

## 4.6 工具执行串行/并发决策（含 InterruptSignal 为什么必须串行）

> 这是一处容易踩坑的细节，单独详解。

### 决策树（`registry_v3.py:238-245`）

```
valid_calls = [tc for tc in tool_calls if tc["name"] in self._specs]
contains_approval = self._contains_approval_tool(valid_calls)

if len(valid_calls) == 1 or contains_approval:
    ──→ 串行同步执行（_execute_serial）
else:
    ──→ 并发执行（_execute_concurrent，ThreadPoolExecutor max=min(N,4)）
```

### _contains_approval_tool 预判（`registry_v3.py:454-469`）

```python
def _contains_approval_tool(self, tool_calls):
    for tc in tool_calls:
        spec = self._specs.get(tc["name"])
        if spec.side_effects.destructive or spec.se_evaluator is not None:
            return True
    return False
```

> **注意**：这是「保守预判」——`needs_approval` 不是静态 flag，而是由 `decide()` 动态求值（取决于 mode + args）。预判只要可能需要审批（destructive 或有 se_evaluator），就强制串行。真正的审批决策在 `execute()` 里由 `decide()` 做。

### InterruptSignal 为什么必须串行

`InterruptSignal` 是异常。异常必须在**调用线程**（`_react_loop` 所在的主线程）里冒泡，才会被 `stream_invoke` 的 `except InterruptSignal` 捕获。

如果放进 `ThreadPoolExecutor` 的 worker 线程：

```
主线程（_react_loop）
  └─ ThreadPoolExecutor
       ├─ worker 线程 1: web_search ✓
       └─ worker 线程 2: run_shell → decide → requireApproval
                         → raise InterruptSignal
                         ↑ 异常困在 worker 线程内
                         → _execute_concurrent 的 except Exception 接住
                         → 转成 "工具执行错误" 字符串
                         → 主线程永远收不到 InterruptSignal
                         → HITL 彻底失效
```

所以只要 tool_calls 里**混了一个可能审批的工具**（destructive 或有 se_evaluator），整批强制走串行路径。

> **V3 比 V2 更优雅的地方**：V2 的 `GraphBubbleUp`（langgraph 的暂停信号）也是异常，同样必须串行——但 V2 靠硬编码集合 `_INTERRUPT_TOOL_NAMES = {"request_human_approval", "run_shell"}` 判断，加新审批工具要改代码。V3 靠 `side_effects.destructive` 声明式判断，新工具只要在 YAML 里标 `destructive: true` 就自动走串行。

### 并发执行的 contextvars 修复

并发执行时，每个 worker 必须**各自 `copy_context()`**（`registry_v3.py:379-381`）：

```python
def _submit(tc_item):
    cvars = contextvars.copy_context()           # ★ 每个 worker 自己 copy
    return executor.submit(cvars.run, _run_tool, tc_item)
```

> **为什么不能共用一个 ctx？** CPython 的 `Context.run()` **不可重入**——第一个 worker 在 `ctx.run` 内阻塞（如 httpx I/O）时，其余 worker 立刻抛 `RuntimeError: cannot enter context: ... is already entered`。详见 [06 章 contextvars 三坑](06-流式韧性工程.md)。

---

## 4.7 ToolResult —— 统一工具返回协议

| | |
|---|---|
| **文件** | `src/agent/tool_result.py`（~57 行） |
| **取代** | V2 的隐式返回约定（dict / magic string / 硬编码 if-elif 分流） |

```python
@dataclass
class ToolResult:
    content: str                              # 返回给 LLM 的文本（必填）
    state_updates: dict[str, Any] = ...       # 需合并到 AgentState 的更新（可选）

    @property
    def has_state_updates(self) -> bool: ...
```

> **V2 对照**：V2 里 `write_todos` 返回 dict、`compact_conversation` 返回 magic string `"COMPACT_REQUESTED"`、其他工具返回 str，`ToolRegistry` 用一串 if-elif 判断类型（ResultHandler 注册表）。V3 把返回值标准化：所有工具返回 `ToolResult`，`content` 给 LLM、`state_updates` 给 state。消除隐式契约。

### 使用示例

**write_todos**（需要更新 state）：
```python
return ToolResult(
    content=f"已更新 {len(todos)} 条待办",
    state_updates={"todos": todos},
)
```

**compact_conversation**（只发信号）：
```python
return ToolResult(
    content="上下文压缩已触发。",
    state_updates={"compact_requested": True},
)
```

**web_search**（纯文本）：
```python
return ToolResult(content=formatted_results)
```

---

## 本章小结

V3 的 HITL 系统三个核心设计：

1. **异常式 HITL**——`InterruptSignal` 是 Python 异常，取代 `langgraph.interrupt()`。权限决策在 `executor.execute()` 之前，无重放问题。`try/except` 100% 可靠，不依赖框架内部行为。

2. **内存快照**——`InterruptSnapshot` 深拷贝 messages + 记录 mode，取代 `MemorySaver` checkpointer。内存级，重启即丢，但单用户对话流是顺序的，够用。

3. **三层权限 + mode 切换**——`decide()` 三层叠加做决策；resume 时**重新求值权限**，用户切 `full_access` 自动放行。prompt 引导（mode_guidance）+ 执行层硬保证（decide）双层兜底。

---

> **下一章**：[05-手写ReAct循环](05-手写ReAct循环.md) —— HermesAgentV3 的 stream_invoke / _react_loop / 循环检测 / 混合态边界转换。
