# 第 3 章 · 上下文管理组 —— `context.py` + `memory_orch.py` + `tool_result.py` + `session_lifecycle.py` + `token_counter.py` + `context_window.py` + `multimodal.py`

> 承接第 1、2 章。本章覆盖 graph.py 和 tools.py 之外的七个「补给线」模块。它们各自不大（共约 990 行），但承担了 Agent 运转不可或缺的职能：消息怎么拼、记忆怎么取、工具结果怎么标准化、会话结束怎么收尾，以及支撑这些的 token 计数、上下文窗口探测、多模态输入处理。
> 这组模块比前两章清爽，是理解 Agent 全貌的「最后一公里」。

---

## 0. 一句话定位

这七个模块是 `HermesAgent` 拆分后的「辅助兵团」：

| 模块 | 一句话 |
|------|--------|
| `context.py` | **弹药装填**：把 SystemPrompt + 历史 + 记忆 + todos 拼成 LLM 能吃的消息列表，还负责压缩 |
| `memory_orch.py` | **侦察兵**：每轮回合开头，根据用户输入去纯文件记忆后端检索相关长期记忆 |
| `tool_result.py` | **传令兵**：定义 ToolResult/ResultHandler 协议（第 2 章已用过，这里补全定义） |
| `session_lifecycle.py` | **收尾官**：会话结束（/exit）时生成总结、提取长期事实、落盘 |
| `token_counter.py` | **计量员**：用 tiktoken 对消息列表做近似 token 计数，驱动按 token 阈值的自动压缩 |
| `context_window.py` | **勘测员**：探测当前模型的最大上下文 token 数（配置/探测/默认三级回落） |
| `multimodal.py` | **图像官**：把用户上传的图片 id 转 base64 data URI，构造 OpenAI Vision 兼容的多模态 content |

它们都是从原 `HermesAgent` 里按「单一职责」拆出来的（context/memory_orch/tool_result/session_lifecycle 文件头注释都写了「从 HermesAgent 中拆分出来」）；token_counter/context_window/multimodal 则是为 token 阈值压缩和多模态输入新增的支撑模块。

---

## 1. 模块全景

| 文件 | 行数 | 核心导出 | 被谁调用 |
|------|------|---------|---------|
| `context.py` | 255 | `ContextManager` / `CompactResult` | graph.py 的 agent_llm 节点 + compact 节点 + precheck 节点 |
| `memory_orch.py` | 106 | `MemoryOrchestrator` | graph.py 的 retrieve_memory 节点 |
| `tool_result.py` | 56 | `ToolResult` / `ResultHandler` | tools.py（第 2 章主角） |
| `session_lifecycle.py` | 163 | `on_session_end()` | cli.py（/exit /reset /switch）+ worker_process.py（后台总结） |
| `token_counter.py` | 84 | `count_tokens` / `count_text_tokens` | graph.py 的 `_precheck_node` |
| `context_window.py` | 119 | `get_context_window` / `detect_context_window` | graph.py 的 `_precheck_node` |
| `multimodal.py` | 143 | `build_user_content` / `extract_text` | graph.py 的 agent_llm + context.py 压缩 + cli.py 持久化 |

**本章核心看点**：

1. `build_llm_messages` 的四道防线（窗口截断、工具配对保护、SystemMessage 合并、Bug 11 重复注入修复）
2. `compact_messages` 的「SystemMessage 包装 + 显式 id」设计（commit aa87994 修「多次 compact 后旧摘要删不掉」），及它与 graph.py `_compact_node` 的 RemoveMessage 配合
3. **三条压缩路径**：LLM 调 compact_conversation 工具 / `/compact` 命令 / **precheck 节点按 token 阈值自动触发**（当前主路径，commit 7e1c03e）
4. token 计数与上下文窗口探测（token_counter/context_window 如何驱动 precheck 自动压缩）
5. 多模态输入：图片 id → base64 data URI 的构造与防穿越
6. 记忆检索为何放弃了 `build_enhanced_query`（一个「过度设计」的教训）
7. `tool_result.py` 的协议设计（补全第 2 章的伏笔）
8. 会话总结的超时隔离（`ThreadPoolExecutor` + `future.result(timeout=)`）

---

## 2. 架构与数据流

### 2.1 一轮回合里这七个模块的参与时机

```
用户输入（可能含图片 id）
  │
  ▼
[precheck 节点]（每轮一次，agent_llm 之前）       ← token_counter.py + context_window.py
  └─ count_tokens(messages) 算近似 token
  └─ get_context_window() 取上下文窗口
  └─ 若 tokens ≥ 窗口 × compact_threshold_pct：
       └─ ContextManager.compact_messages()        ← context.py
       └─ RemoveMessage + 压缩消息回写 state
       └─ 推送 auto_compact + messages_snapshot 事件
  └─ 否则透传（不改 state）
  │
  ▼
[retrieve_memory 节点]
  └─ MemoryOrchestrator.retrieve_with_detail()     ← memory_orch.py
       └─ MemoryManager.search_with_detail() → FileMemoryStore（纯文件关键词检索）
       └─ 返回 memories 列表 + 命中详情
  │
  ▼
[agent_llm 节点]
  └─ ContextManager.build_llm_messages()           ← context.py
       ├─ build_system_prompt(user_id, memories, todos, role)
       ├─ 窗口截断（max_short_term_messages × 2）
       ├─ 工具配对保护（跳过孤立 tool_calls）
       ├─ SystemMessage 合并
       └─ Bug 11 兜底（无 HumanMessage 才补）
       → 返回 [SystemMessage, ...历史, 当前输入]
  └─ LLM 流式调用（graph.py 负责）
  │   └─ 若有图片：build_user_content(text, user_id, images)  ← multimodal.py
  │       把 image id → base64 data URI，构造 Vision content
  │
  ▼ （三条压缩路径之一）
路径 A：precheck 已在上文按 token 阈值自动压缩（当前主路径）
路径 B：LLM 调 compact_conversation 工具 → compact 节点
路径 C：CLI /compact 命令 → 直接 compact_messages
  └─ ContextManager.compact_messages()             ← context.py
       ├─ extract_text(多模态 content) 提取纯文本   ← multimodal.py
       ├─ generate_summary(早期消息文本)
       └─ SystemMessage(摘要, id=compact-{uuid}) + 近期消息
  └─ graph 节点用 RemoveMessage 同步到 state
  │
  ▼ （会话结束 /exit）
[session_lifecycle]
  └─ on_session_end()                              ← session_lifecycle.py
       ├─ _messages_to_text()
       ├─ interactive 菜单（1总结展示/2总结不展示/3不总结）
       └─ ThreadPoolExecutor 超时隔离 → Summarizer.summarize_and_store()
```

### 2.2 贯穿全程的 ToolResult 协议

```
工具执行 (tools.py)
  └─ tool.invoke() → 原始返回值（dict / str / 任意）
  └─ ResultHandler(原始值, 工具名, 参数) → ToolResult   ← tool_result.py 定义
       ├─ content: 返回给 LLM 的文本
       └─ state_updates: 合并进 AgentState 的更新
```

---

## 3. 逐文件深潜

### 3.1 `context.py` —— 消息构建的四道防线

`build_llm_messages`（`ContextManager.build_llm_messages`）是 agent_llm 节点每次调用 LLM 前的必经之路。它看似只是「拼消息」，但有四道精心设计的防线。

#### 防线 1：窗口截断（`build_llm_messages` 窗口截断段）

```python
max_msgs = self.settings.max_short_term_messages * 2   # 默认 20×2=40 条
if len(messages) > max_msgs:
    truncated = messages[-max_msgs:]
```

**为什么 `× 2`**：`max_short_term_messages` 配置注释写「1 轮 = 1 次用户 + 1 次助手」，所以「20 轮」= 40 条消息。截断时保留最近的 40 条。

#### 防线 2：工具配对保护（窗口截断后的 while 循环）—— 最巧妙的一处

```python
# 避免切断"工具调用 → 工具结果"的配对：
# 若截断后首条是带 tool_calls 的 AIMessage，则其对应的 ToolMessage 可能被丢弃，
# 这会让 LLM 困惑（收到 tool_calls 但无对应结果）。因此向后跳过这类孤立消息。
while (
    truncated
    and isinstance(truncated[0], AIMessage)
    and getattr(truncated[0], "tool_calls", None)
):
    truncated = truncated[1:]
```

**动机**：ReAct 模式下，消息序列长这样：
```
... AIMessage(tool_calls=[web_search(...)])   ← 工具调用
   ToolMessage(content="搜索结果...")          ← 工具结果
   AIMessage(content="根据搜索...")            ← 基于结果的回复
```

如果窗口截断正好切在「工具调用」和「工具结果」之间，LLM 会看到一条「我调了工具但没看到结果」的孤儿消息。OpenAI 兼容 API 会**直接报错**（tool_call 必须有对应 tool_response），或让 LLM 困惑。

**解法**：截断后，如果第一条是带 tool_calls 的 AIMessage（说明它的 ToolMessage 被切掉了），就**继续往后丢**，直到第一条不再是孤儿。代价是可能多丢几条消息，但保证了配对完整性。

> **设计哲学**：宁可少几条历史，也不能给 LLM 喂语义破损的消息。这是「正确性优先于信息量」的取舍。

#### 防线 3：SystemMessage 合并（`extra_system_parts` 段）

```python
extra_system_parts = []
conversation_msgs = []
for msg in truncated:
    if isinstance(msg, SystemMessage):
        extra_system_parts.append(msg.content)        # 历史里的 SystemMessage（压缩摘要）
    elif isinstance(msg, (HumanMessage, AIMessage, ToolMessage)):
        conversation_msgs.append(msg)

final_system = system_prompt
if extra_system_parts:
    final_system = system_prompt + "\n\n" + "\n\n".join(extra_system_parts)

llm_messages = [SystemMessage(content=final_system)]   # 唯一一条 SystemMessage 在最前
llm_messages.extend(conversation_msgs)
```

**动机**：OpenAI API 要求 SystemMessage **只能有一个且在最开头**。但压缩后的摘要（3.2 会讲）是 SystemMessage，可能出现在历史中间。这里把所有散落的 SystemMessage 内容**合并进开头的 system_prompt**，保证 API 格式合法。

#### 防线 4：Bug 11 —— 用户消息重复注入修复（`if not any(isinstance(m, HumanMessage)...)` 段）

这是 `build_llm_messages` 里最长的一段注释，因为它修复了一个很恶心的 bug：

```python
# 旧逻辑用 `conversation_msgs[-1].content != current_input` 做位置判重，
# 只在首轮（末尾恰好是本轮 HumanMessage）生效。一旦 LLM 发起 tool_call，
# 后续 agent_llm 迭代里末尾是 ToolMessage，判重恒为 True，于是每次迭代
# 都额外追加一条 HumanMessage(current_input)。N 次工具调用 → 用户消息在
# LLM payload 里出现 N+1 次，LLM 因此抱怨"用户重复了请求"。
if not any(isinstance(m, HumanMessage) for m in conversation_msgs):
    llm_messages.append(HumanMessage(content=current_input))
```

**Bug 因果链**：

```
旧判重逻辑：if conversation_msgs[-1].content != current_input: 追加
    ↓
首轮：末尾是 HumanMessage(current_input)，判重为 False，不追加 ✅
    ↓
LLM 发起 tool_call → tools 节点执行 → 回到 agent_llm
    ↓
此时 messages 末尾是 ToolMessage（工具结果），不是 HumanMessage
    ↓
判重 `ToolMessage.content != current_input` 恒为 True
    ↓
每次迭代都追加一条 HumanMessage(current_input)
    ↓
N 次工具调用 → 用户消息出现 N+1 次 → LLM 困惑
```

**修复**：改为「历史中完全没有 HumanMessage 时才补」（冷启动兜底）。因为 `current_input` 在回合开始时已由 graph.py 的 `stream_invoke`（构造 initial_state 时）作为 HumanMessage 进入 messages，`build_llm_messages` 不应再补。

> **教训**：用「内容相等」做位置判重是脆弱的——消息类型会变（Human/Tool/AI），内容也可能巧合相等。正确的做法是理解「这条消息应该由谁负责注入」，避免重复注入。注释特意写明 graph.py 负责注入、`build_llm_messages` 只兜底——**职责边界清晰**才能避免这类 bug。

---

### 3.2 `compact_messages` —— SystemMessage 包装 + 显式 id + 三条触发路径

`compact_messages`（`ContextManager.compact_messages`）有三个值得讲的设计点。

#### 设计点 1：用 SystemMessage 包装摘要 + 显式 id（commit aa87994）

```python
# 2026-06-15: 用 SystemMessage 包装摘要，避免伪装成用户输入导致 LLM 困惑
# 2026-07-03: 给摘要赋显式 id，否则 graph 节点的
# `RemoveMessage(id=msg.id) for msg in messages if msg.id` 会跳过无 id 的摘要，
# 导致多次 compact 后旧摘要永远删不掉、上下文累积膨胀。
summary_msg = SystemMessage(
    content=f"以下是对话历史的压缩摘要，请基于此背景继续对话：\n{summary}",
    id=f"compact-{uuid.uuid4().hex[:8]}",
)
```

**为什么不用 HumanMessage**：早期可能用过 HumanMessage（「这是之前的对话摘要：...」），但这会让 LLM 把摘要误认为「用户刚说的话」，可能回应「好的，我知道了」之类无用回复。用 SystemMessage 标记为「系统背景信息」，LLM 不会当成需要回应的用户输入。

**为什么要有显式 id（commit aa87994 的关键修复）**：graph 的 `_compact_node` 和 `_precheck_node` 用 `RemoveMessage(id=msg.id) for msg in messages if msg.id` 删旧消息。LangGraph 的 `SystemMessage` 默认**不生成 id**（`msg.id is None`）。于是第一次 compact 后，摘要 SystemMessage 进了 state；第二次 compact 时，`if msg.id` 过滤掉了无 id 的旧摘要——**旧摘要删不掉**，新摘要又加进来，多次 compact 后上下文里堆了一堆过期摘要，累积膨胀。

**解法**：给摘要赋显式 id `compact-{uuid}`。这样 RemoveMessage 能识别它、删掉它，保证「每次 compact 只保留最新摘要」。

> **联系第 7 章**：这个 id 问题正是 `messages_snapshot` 事件存在的根因——当 graph 节点用增量 RemoveMessage 删旧消息时，前端/worker 的 session_messages 无法用增量切片同步（删了哪些不好追踪）。所以压缩后 graph 推送 `messages_snapshot`（全量快照），让消费端整体替换。第 7 章 CLI 和 Web worker 都消费这个事件。

#### 设计点 2：原地修改 + 防御空摘要

```python
# 防御空摘要——LLM 偶发返回空 content，会导致上下文全部丢失
if not summary or not summary.strip():
    logger.error(f"生成压缩摘要为空（old_text={len(old_text)} 字符），放弃压缩")
    return None                                    # ← 返回 None，调用方据此不压缩
...
messages.clear()                                   # 原地清空
messages.append(summary_msg)
messages.extend(recent_messages)
```

**关键细节**：
1. **空摘要防御**：如果 LLM 返回空 content，直接 `messages.clear()` 会**丢掉全部历史**——灾难性 bug。所以先检查，空则返回 None，调用方据此跳过压缩。
2. **原地修改**：注意 `compact_messages` 直接 `messages.clear()` + `append`，改的是传入的 list 本身。docstring 写了「messages: 消息列表（会被原地修改）」。这是为兼容 CLI `/compact` 命令直接传 `session_messages` 的路径——CLI 那边确实需要改 session 列表。但 graph 路径**不依赖**这个原地修改——它传入 `list(messages)` 副本，用 `RemoveMessage` + 新消息通过 `add_messages` reducer 更新 state。

> **双路径设计妥协**：`compact_messages` 同时服务 graph 内部（传副本，原地修改无害冗余）和 CLI/worker 外部（直接传 session_messages，依赖原地修改）。docstring 的「会被原地修改」是「有意承诺」——调用方必须知道并接受这个副作用。

#### 设计点 3：三条压缩触发路径

`compact_messages` 被三条路径调用：

```
路径 A（当前主路径）：precheck 节点按 token 阈值自动触发
    graph.py _precheck_node 每轮一次：
      count_tokens(messages) ≥ get_context_window() × compact_threshold_pct
      → compact_messages(list(messages))   ← 传副本
      → RemoveMessage + 压缩消息回写 state
      → 推送 auto_compact + messages_snapshot 事件
    （commit 7e1c03e 引入，是现在 Agent 上下文管理的核心机制）

路径 B：LLM 调 compact_conversation 工具
    LLM 主动调 → 返回 "COMPACT_REQUESTED"
    → tools.py 设 compact_requested=True
    → graph _after_tools_router 路由到 _compact_node
    → _compact_node 调 compact_messages

路径 C：用户手动触发
    CLI /compact 命令 / Web /api/compact → 直接调 compact_messages
```

**路径 A 为什么是主路径**：路径 B 依赖 LLM「自觉」调工具——但 LLM 不一定知道自己的上下文快满了，且不同模型的上下文窗口不同。路径 A 由 precheck 节点**主动**监测 token 用量，达到阈值就压缩，不依赖 LLM 配合。`compact_threshold_pct`（默认 80）是可配置的触发线——Web 端还能 per-user 热更新（WorkerState.prefs）。

> **token 阈值压缩的完整闭环**：precheck 用 token_counter 算 token、用 context_window 取窗口、用 context.py 压缩——这三个新模块（token_counter/context_window/multimodal 的 extract_text）正是为路径 A 服务的。下面 3.6/3.7/3.8 逐个讲它们。

---

### 3.3 `memory_orch.py` —— 一个「过度设计」的教训

`MemoryOrchestrator` 现在很薄（106 行，一半是注释），核心方法 `retrieve_with_detail` 只做一件事：调 `MemoryManager.search_with_detail`。但它有个重要的历史教训。

#### `build_enhanced_query` 的废弃（`retrieve_with_detail` 的 `enhanced_query` 赋值处）

```python
# 2026-06-15: 移除 build_enhanced_query，直接用原始用户输入检索。
# 旧实现拼接历史用户消息会污染语义（如把"你是谁？"拼进来），导致召回不到相关记忆。
enhanced_query = current_input
```

**旧设计的设想**（推测）：把历史用户消息拼进查询，做「上下文增强检索」，以为信息越多召回越准。

**实际灾难**：假设对话是
```
用户：你是谁？
用户：我昨天买了台 MacBook
```
旧实现会把查询拼成「你是谁？ 我昨天买了台 MacBook」，这个缝合怪查询的语义被「你是谁」严重污染，向量检索召回的全是无关记忆。

**修复**：直接用 `current_input`（当前这一句）检索。简单粗暴但正确——用户当前关心什么，当前这句话最能代表。

> **教训**：检索增强（query enhancement）不是「信息越多越好」。拼接历史会引入噪声，稀释目标语义。除非做 query rewriting（用 LLM 重写查询），否则原始输入就是最好的查询。这是 RAG 系统的常见误区。

#### 存储方法已移除（文件末尾的「已移除」注释块）

```python
# 注：store / store_from_response / store_from_response_with_detail 三个方法
# 已于 2026-06-22 移除（agent memory 重构）。
# 记忆存储改由两条路径触发：
#   1. LLM 自主调用 remember 工具（主路径）
#   2. 会话结束 Summarizer 总结
# 旧的"每轮无脑存储"已被证明会记一堆没用的流水账，不再保留。
```

这是项目记忆架构演进的关键转折——从「每轮自动存」（流水账）到「LLM 自主存 + 会话总结兜底」。`MemoryOrchestrator` 因此从「检索+存储」瘦身为「只检索」。第 4 章讲 memory 层时会展开这个决策。

> **注释价值**：这个「已移除」注释非常有价值——它记录了「为什么这里少了东西」，避免后人疑惑「是不是漏了存储逻辑」或试图重新加回来。**好的删除注释比代码更重要**。

---

### 3.4 `tool_result.py` —— 协议定义（补全第 2 章伏笔）

这个文件只有 56 行，但定义了第 2 章反复使用的两个东西。完整结构：

```python
# 类型别名：handler 签名
ResultHandler = Callable[[Any, str, dict], "ToolResult"]
#                         ↑     ↑    ↑
#                     原始结果 工具名 参数

@dataclass
class ToolResult:
    content: str                                    # 返回给 LLM 的文本（必填）
    state_updates: dict[str, Any] = field(default_factory=dict)  # state 更新（可选）

    @property
    def has_state_updates(self) -> bool:
        return bool(self.state_updates)
```

**设计要点**：

1. **`ResultHandler` 是类型别名，不是抽象基类**：用 `Callable` 而非 ABC，让 handler 可以是任何函数（包括 lambda），降低注册门槛。对比 ABC 需要继承+实现，函数式更轻量。
2. **`ToolResult` 用 dataclass + 默认工厂**：`state_updates` 默认空 dict，让「无副作用工具」只需写 `ToolResult(content=str(result))`。`has_state_updates` 属性让调用方（tools.py 的 execute_tool）用 `if tool_result.has_state_updates` 简洁判断。
3. **`content: str` 是必填**：强制每个工具都必须返回文本给 LLM，避免「工具执行了但 LLM 不知道结果」。

> **与第 2 章的呼应**：第 2 章 3.1 讲了 ResultHandler 注册表「怎么用」，这里补全了「协议长什么样」。两者合起来才是完整的设计。

#### 文档里的「三个问题」陈述（模块 docstring 的「设计动机」段）

```
改造前（if-elif 链）的三个问题：
1. 隐式契约：返回格式只在 tools.py 中硬编码，无类型约束
2. 静默失败：格式变化不报错但功能失效
3. 扩展性差：新工具需添加新的 if-elif 分支
```

这是对「为什么要重构」的精准总结。第 2 点「静默失败」尤其重要——if-elif 链里，如果工具返回的 dict 结构变了（比如 `todos` 键没了），代码不会报错，只是静默地不再更新 state，bug 极难发现。ResultHandler + ToolResult 把这种隐式契约变成了**显式类型**。

---

### 3.5 `session_lifecycle.py` —— 会话收尾与超时隔离

`on_session_end` 是 /exit 时的收尾逻辑。两个看点。

#### 看点 1：交互菜单（`_ask_exit_choice` 函数）

```python
def _ask_exit_choice() -> str:
    _console.print("\n  [bold]📝 会话结束，是否生成总结？[/bold]")
    _console.print("    [cyan]1[/cyan] 生成总结并展示在屏幕上")
    _console.print("    [cyan]2[/cyan] 生成总结但不展示（直接保存）")
    _console.print("    [cyan]3[/cyan] 不总结，直接退出")
    while True:
        choice = Prompt.ask("  请选择", default="1", choices=["1", "2", "3"])
        if choice in ("1", "2", "3"):
            return choice
```

**设计动机**：会话总结要调 LLM（耗时几秒到几十秒），用户可能不想等。菜单把决定权交给用户：
- 选 1：总结 + 看一眼正文（最透明）
- 选 2：总结但不展示（后台静默）
- 选 3：直接退（最快）

`interactive` 参数控制是否弹菜单——只有 `/exit` 传 True，`/reset` `/switch` 这种非用户主动退出的场景自动总结不问（`on_session_end` 的 interactive 参数注释）。

#### 看点 2：超时隔离（`on_session_end` 的 ThreadPoolExecutor 段，I6 修复）

```python
# I6 修复：超时保护，避免 /exit 被 LLM 长时间阻塞
try:
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_do_summarize)
        result = future.result(timeout=timeout)       # 默认 120s
except FuturesTimeoutError:
    logger.warning(f"会话结束总结超时（{timeout}s），跳过总结")
    return {"summary_stored": False, ...}             # 超时也不阻塞退出
```

**为什么不能直接调 `_do_summarize()`**：因为 Summarizer 内部调 LLM 生成总结，这是个**同步阻塞调用**。如果 LLM 端点卡住（第 1 章讲的流式半开），`/exit` 会**无限期挂住**，用户只能强杀进程。

**解法**：把总结丢进单线程池，主线程用 `future.result(timeout=)` 等待。超时则放弃总结（返回未存储），保证 `/exit` 一定能退出。

> **对比第 1 章**：graph.py 的 LLM 超时用「工作线程 + Queue + 看门狗」（因为要流式），这里用「ThreadPoolExecutor + future.result」就够了（因为总结不需要流式，等最终结果即可）。**同样是「同步阻塞调用的超时隔离」，根据是否需要流式，有两种解法**。

#### 看点 3：独立 Console（模块顶部的 `_console` 定义）

```python
# 交互菜单用一个独立 Console（不依赖 cli.py 的全局实例，保持模块解耦）
_console = Console(file=sys.stdout)
```

**动机**：cli.py 有自己的全局 Console（可能配置了特殊主题/重定向）。如果 session_lifecycle 依赖它，就产生了**模块耦合**——改 cli.py 的 Console 可能影响退出菜单。独立 Console 保持模块自洽。这是个小的解耦决策，但体现了「模块边界意识」。

> **联系第 7 章**：Web 端的 worker_process 也会调 `on_session_end`（后台会话总结，`_summarize_in_background`），但传 `interactive=False`——不弹菜单，静默总结。这样 CLI 和 Web 复用同一套收尾逻辑。

---

### 3.6 `token_counter.py` —— 近似 token 计数（驱动 precheck 自动压缩）

这个模块（84 行）是 commit 7e1c03e 引入的 precheck 自动压缩机制的「计量员」。核心就两个函数：

```python
def count_text_tokens(text: str) -> int:
    """单个字符串的 token 数。回落模式下按字符数 ÷ 3.5 粗估。"""
    enc = _get_encoding()
    if enc is None:
        return max(1, int(len(text) / 3.5))
    return len(enc.encode(text))

def count_tokens(messages: list) -> int:
    """对 LangChain 消息列表做近似 token 计数。"""
    total = 0
    for msg in messages:
        text = extract_text(content)      # ← 用 multimodal.extract_text 处理多模态
        total += count_text_tokens(text)
        total += 4                        # 每条消息固定开销（角色标签等）
    return total
```

#### 为什么用 tiktoken 而非精确 Qwen tokenizer

文件头注释写得很清楚：tiktoken 已在依赖中（零新依赖），`o200k_base` 编码对中文为主的 Qwen 文本约**偏高 10–20%**——意味着实际 token 比计数低，会略早触发 compact。对「阈值保护」用途这是**安全方向**（宁可早压缩，不要溢出报错）。

#### 三级健壮性设计

1. **模块级懒加载**：`_get_encoding()` 进程内只加载一次 encoding，复用。
2. **回落模式**：tiktoken 不可用时标记 `_fallback=True`，改用 `字符数 ÷ 3.5` 粗估。**绝不抛异常**阻塞主流程——precheck 计数失败只会导致「不压缩」（透传），不会让对话崩。
3. **多模态处理**：`count_tokens` 用 `multimodal.extract_text` 从可能的多模态 content（list）中提取纯文本，图片 token 不计（Qwen vision 的 image_token 占位难以精确估算，忽略后偏保守 = 提前压缩，安全方向）。

> **动机**：precheck 节点需要知道「当前上下文用了多少 token」才能判断是否该压缩。但没有精确的 tokenizer（Qwen 的 tokenizer 不在 Python 侧），只能近似。这个模块把「近似但够用」封装好，让 precheck 不用关心计数细节。

---

### 3.7 `context_window.py` —— 模型上下文窗口探测（三级回落）

这个模块（119 行）解决「当前模型的最大上下文 token 数是多少」——这是 precheck 算阈值（`窗口 × compact_threshold_pct`）的分母。

#### 三级回落（`get_context_window`，`@lru_cache`）

```python
@lru_cache
def get_context_window() -> int:
    # 1. config.yaml 的 model_context_window > 0 → 直接用（可靠真相源）
    configured = settings.get("model_context_window", 0)
    if configured > 0:
        return configured

    # 2. 自动探测 /v1/models 响应里的扩展字段
    detected = detect_context_window()
    if detected and detected > 0:
        return detected

    # 3. 回落到保守默认
    return _DEFAULT_CONTEXT_WINDOW   # 32768
```

**为什么需要三级**：自托管 OpenAI 兼容服务器（vLLM / one-api 等）对 `/v1/models` 响应字段**并不统一**，标准 OpenAI 规范也不含上下文长度。所以：
- 配置最可靠（用户显式写 `model_context_window: 32768`）；
- 自动探测是 best-effort（`detect_context_window` 尝试读 `max_model_len`/`context_length`/`max_position_embeddings` 等多个候选字段）；
- 都没有就保守默认 32768。

#### `detect_context_window` 的容错

```python
def detect_context_window() -> int | None:
    # GET {base_url}/models，匹配 llm_model_name，读扩展字段
    r = httpx.get(url, headers=headers, timeout=10.0)
    ...
    # 模型 id 匹配（宽松：含子串即可，应对 "qwen2.5:32b" vs "qwen2.5:32b-instruct"）
```

任何异常（网络/解析/字段缺失）都吞掉返回 None，绝不阻塞调用方。模型 id 匹配用宽松的子串匹配——因为服务端注册名和配置名可能有 `-instruct` 等后缀差异。

**`@lru_cache` 的语义**：结果进程内缓存。改配置需重启（与 system 配置语义一致）。precheck 每轮都调 `get_context_window()`，缓存避免重复 HTTP 探测。

> **动机**：precheck 的阈值计算 `int(context_window * (pct / 100.0))` 需要一个可信的分母。这个模块把「配置/探测/默认」的复杂决策封装好，precheck 只管调 `get_context_window()`。

---

### 3.8 `multimodal.py` —— 图片输入构造与防穿越

这个模块（143 行）处理多模态输入：把用户上传的图片 id 转 base64 data URI，构造 OpenAI Vision 兼容的 content。两个公共接口：

#### `build_user_content` —— 图片 id → data URI

```python
def build_user_content(text, user_id, images) -> str | list[dict]:
    if not images:
        return text                          # 无图：纯文本（不影响原有路径）
    content = []
    for img_id in images:
        path = _resolve_upload_path(user_id, img_id)
        data_uri = _file_to_data_uri(path)   # base64 data URI
        content.append({"type": "image_url", "image_url": {"url": data_uri}})
    if not content:
        return text                          # 所有图都失败 → 退化为纯文本
    content.append({"type": "text", "text": text})   # 文本块放最后
    return content
```

**为什么必须 data URI 而非 URL**（文件头注释）：LLM 端点（远程 vLLM/Qwen）无法访问 Web 服务器的 localhost URL，所以图片必须以 base64 data URI **嵌入消息**。

**为什么 data URI 转换在 worker 子进程内完成**：图片不经 IPC 管道传 base64（会撑爆 64KB 的 stdout buffer）。Web 前端上传后只存文件（`/api/upload` → `data/uploads/<user_id>/<id>.<ext>`），聊天请求只传 image id（轻量字符串），worker 侧 `stream_invoke` 再读盘转 data URI 发给 LLM。

**图片在前、文本在后**：多数 VLM 期望此顺序。

#### 防穿越（`_resolve_upload_path`）

```python
def _resolve_upload_path(user_id, image_id) -> Path | None:
    safe_name = Path(image_id).name           # 去掉任何路径分隔符
    if safe_name != image_id:                 # 含路径 → 拒绝
        return None
    suffix = Path(safe_name).suffix.lower()
    if suffix not in _ALLOWED_EXTS:           # 白名单扩展名
        return None
    return UPLOADS_DIR / user_id / safe_name
```

`image_id` 来自前端（用户可控），会被拼入文件路径。用 `Path.name` 剥离路径部分 + 扩展名白名单（`.png/.jpg/...`），防 `../../etc/passwd` 之类穿越。

#### `extract_text` —— 多模态 content 的文本提取

```python
def extract_text(content) -> str:
    if isinstance(content, str):
        return content                        # 纯文本原样返回
    if isinstance(content, list):
        # 多模态：拼接所有 type=="text" 块
        return "\n".join(block["text"] for block in content if block.get("type") == "text")
    return str(content) if content else ""    # 兜底
```

**为什么需要这个函数**：很多旧代码路径（`compact_messages` 的消息转文本、`save_session` 的 preview、`token_counter`）假设 content 是 str。多模态后 content 可能是 list。`extract_text` 让这些旧路径不用改——遇到 list 就提取文本块，遇到 str 原样返回。

> **跨模块协作**：multimodal 是这组模块里被调用最广的——graph 的 agent_llm 用 `build_user_content` 构造多模态输入；context 的 `compact_messages` 和 cli 的 `save_session` 用 `extract_text` 处理多模态 content；token_counter 用 `extract_text` 算 token。它是多模态能力的「基础设施」。

---

## 4. 设计权衡总结

### 4.1 优点

| 设计 | 价值 |
|------|------|
| 工具配对保护（context.py 防线 2） | 宁可少几条历史，不让 LLM 收到破损消息 |
| SystemMessage 合并（防线 3） | 保证 OpenAI API 格式合法（只一个 SystemMessage 在前） |
| Bug 11 修复（防线 4） | 清晰的职责边界杜绝重复注入 |
| 显式 id `compact-{uuid}`（compact） | 修「多次 compact 后旧摘要删不掉」，RemoveMessage 能识别 |
| 空摘要防御（compact） | 防止压缩失败导致上下文全丢 |
| precheck token 阈值自动压缩 | 不依赖 LLM 自觉，主动监测 token 用量，当前主路径 |
| tiktoken 近似计数 + 安全偏高 | 零新依赖，偏早触发 = 安全方向 |
| context_window 三级回落 | 配置/探测/默认，适配各家自托管服务端 |
| multimodal data URI + 防穿越 | 图片嵌入消息；image_id 白名单防路径穿越 |
| 放弃 build_enhanced_query | 「少即是多」，原始查询最准 |
| 会话总结超时隔离 | /exit 永不挂死 |
| 「已移除」注释 | 记录架构演进，防止后人误加回 |

### 4.2 技术债 / 待优化

| 项 | 说明 |
|----|------|
| compact_messages 的原地修改副作用 | 与 graph 的 RemoveMessage 路径冗余，双重调用路径导致妥协 |
| `MemoryOrchestrator` 现在很薄 | 只剩一个转发方法，是否还有存在必要可商榷 |
| 超时后 worker 线程泄漏 | 同第 1 章，daemon 线程随进程退出清理 |
| tiktoken 与 Qwen 实际 token 有偏差 | 偏高 10-20%，对阈值保护是安全方向，但精确度有限 |

---

## 本章小结

这七个「补给线」模块体量不大，但每个都解决了 Agent 运转的具体问题：

| 模块 | 核心贡献 | 最值得记住的一点 |
|------|---------|----------------|
| context.py | 消息构建四道防线 + 压缩 | **显式 id + 工具配对保护**——宁可丢历史，不喂破损消息 |
| memory_orch.py | 记忆检索编排 | **build_enhanced_query 的废弃**——检索不是信息越多越好 |
| tool_result.py | 工具返回协议 | **显式类型消灭静默失败**——if-elif 链的最大危害 |
| session_lifecycle.py | 会话收尾 | **超时隔离**——/exit 永不挂死 |
| token_counter.py | 近似 token 计数 | **tiktoken 偏高 = 安全方向**——宁可早压缩 |
| context_window.py | 上下文窗口探测 | **三级回落**——配置/探测/默认适配各家服务端 |
| multimodal.py | 图片输入构造 | **data URI + 防穿越**——图片嵌入消息，image_id 白名单 |

**和前两章的呼应**：
- 第 1 章 graph.py 的 `_compact_node` 用 RemoveMessage 更新 state，本章 context.py 的 `compact_messages` 提供「实际压缩逻辑」——两者是「state 管理者」和「压缩执行者」的分工
- 第 1 章 graph.py 的 `_precheck_node` 是 token 阈值自动压缩的**调用方**，本章 token_counter/context_window/multimodal 是它的**支撑模块**
- 第 2 章 tools.py 的 ResultHandler 注册表，本章 tool_result.py 提供协议定义——「使用方」和「定义方」
- 第 1 章的「同步阻塞超时隔离」（工作线程+Queue）和本章 session_lifecycle 的（ThreadPoolExecutor+future）是同一问题的两种解法，选哪个取决于是否需要流式

至此，`src/agent/` 目录的 9 个文件（graph / tools / context / memory_orch / tool_result / session_lifecycle / token_counter / context_window / multimodal）全部讲完。下一章进入 `src/memory/`——自研记忆三层架构（Store/Extractor/Decider），这是项目「2026-06-22 从 Mem0 迁移」的核心成果，设计动机最密集。

---

> **本章验收点**：① 四道防线（尤其工具配对保护）是否讲透 ② compact 的显式 id 修复（commit aa87994）是否清晰 ③ 第三条压缩路径（precheck token 阈值）和它的三个支撑模块是否到位 ④ build_enhanced_query 废弃的教训是否到位 ⑤ 这组「补给线模块」的深度是否合适。确认后推进第 4 章 memory 三层架构。
