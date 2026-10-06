# 02 · Tier 1 手搓核心引擎

> 这 8 个组件构成 V3 的 LLM 调用 + Agent 编排 + 工具执行 + 人机协作 完整引擎，**零 lang 家族依赖**。
> 每节结构：**取代了什么 → 文件 / 行号 → 核心方法 → 为什么这么写（V2 对照）**。

---

## 2.1 LLMClient —— 薄封装 openai SDK

| | |
|---|---|
| **文件** | `src/llm/client.py`（~350 行） |
| **取代** | `langchain_openai.ChatOpenAI`（`llm.invoke` / `llm.stream` / `llm.bind_tools` / `AIMessageChunk` 拼接） |
| **依赖** | `openai.OpenAI` + 自建 `AIMsg` / `Chunk` |

### 核心方法

| 方法 | 行号 | 说明 |
|------|------|------|
| `__init__` | `client.py:43` | 构造 `OpenAI(...)` 客户端，延迟读 config |
| `chat()` | `client.py:80` | 同步调用，返回 `AIMsg` |
| `invoke_simple()` | `client.py:112` | 便捷方法：prompt 字符串进、content 字符串出（记忆模块用） |
| `stream_chat()` | `client.py:148` | 流式调用，内置 `ThinkSplitter` 自动拆 `<think>` 标签 |
| `accumulate()` | `client.py:225` | 把流式 `Chunk[]` 拼成完整 `AIMsg`（取代 `AIMessageChunk` 的 `+` 拼接） |
| `_parse_chunk()` | `client.py:312` | 解析 openai chunk → `Chunk`，直接读 `delta.reasoning` / `delta.reasoning_content` |
| `_parse_response()` | `client.py:274` | 非流式解析，reasoning 优先 `msg.reasoning`，其次 `reasoning_content`，最后 `extract_think_content` 从 content 提取 |

### 构造（`client.py:43-74`）

```python
def __init__(self, api_key=None, base_url=None, model=None, temperature=0.7,
             max_tokens=2000, request_timeout=120, max_retries=0, extra_body=None):
    # 默认从 config 读
    # extra_body 默认 {"chat_template_kwargs": {"enable_thinking": False}}
    self.client = OpenAI(
        api_key=api_key, base_url=base_url, timeout=request_timeout,
        max_retries=max_retries,
    )
```

> **`max_retries=0`**：故意不自动重试——V2 的旧默认 `max_retries=2` 会让挂死时间 ×3 到 30 分钟。V3 自托管端点用双 deadline 硬超时兜底，失败立即让用户感知。

### stream_chat —— 核心方法（`client.py:148-219`）

```python
def stream_chat(self, messages, tools=None, **kwargs):
    # messages 是自建消息类型列表（SystemMsg/HumanMsg/AIMsg/ToolMsg）
    msg_dicts = [self._msg_to_dict(m) for m in messages]
    stream = self.client.chat.completions.create(
        model=self.model, messages=msg_dicts, tools=tools,
        stream=True, temperature=..., max_tokens=..., extra_body=self.extra_body,
    )

    splitter = ThinkSplitter()
    for raw in stream:
        chunk = self._parse_chunk(raw)
        if chunk is None:
            continue

        # ★ 两路互斥（client.py:197-213）
        if chunk.reasoning_delta:
            # 有原生 reasoning 字段（Qwen3 reasoning）→ 直接 yield，不走拆分
            yield chunk
        elif "<think>" in (chunk.content_delta or "") or "</think>" in ...:
            # content 含 <think> 标签（Ornith / DeepSeek-R1）→ 喂 splitter 拆分
            for piece in splitter.feed(chunk.content_delta):
                yield piece  # 拆成 reasoning_delta / content_delta
        else:
            yield chunk
    # flush 拆分器尾部残留
    for piece in splitter.flush():
        yield piece
```

> **两路互斥的设计**：不同模型把推理放在不同地方——Qwen3 放独立 `reasoning` 字段，Ornith / DeepSeek-R1 塞进 `content` 的 `<think>` 标签。`LLMClient` 自动适配，调用方无感知。换模型不需要改任何配置。

### accumulate —— 取代 AIMessageChunk 的 `+`（`client.py:225-259`）

```python
@staticmethod
def accumulate(chunks: list[Chunk]) -> AIMsg:
    content = ""
    reasoning = ""
    tool_calls = {}
    for chunk in chunks:
        content += chunk.content_delta or ""
        reasoning += chunk.reasoning_delta or ""
        for tc_delta in chunk.tool_call_deltas or []:
            idx = tc_delta.get("index", 0)
            if idx not in tool_calls:
                tool_calls[idx] = {"id": "", "function": {"name": "", "arguments": ""}}
            tool_calls[idx]["id"] += tc_delta.get("id", "") or ""
            tool_calls[idx]["function"]["name"] += tc_delta.get("function", {}).get("name", "") or ""
            tool_calls[idx]["function"]["arguments"] += tc_delta.get("function", {}).get("arguments", "") or ""
    return AIMsg(content=content, reasoning=reasoning,
                 tool_calls=list(tool_calls.values()))
```

> **V2 对照**：LangChain 的 `AIMessageChunk` 靠重载 `+` 运算符合并 `tool_call_chunks`。V3 手写 `accumulate`，按 `index` 合并 tool_calls，最后按 index 排序。逻辑等价但完全可控、可 grep。

### 为什么这么写（V2 对照）

| V2（LangChain） | V3（LLMClient） | 为什么改 |
|---|---|---|
| `ChatOpenAI(streaming=True, request_timeout=...)` | `LLMClient(request_timeout=...)` | 相同接口语义，但底层是裸 openai SDK |
| `llm.bind_tools(tools)` → 返回新 llm 实例 | `stream_chat(messages, tools=[...])` 参数传递 | V2 的 `bind_tools` 是原子的、返回不可变副本，V3 直接参数传更简单 |
| `ChatOpenAI` 默认读 `os.environ["OPENAI_API_KEY"]` | 构造时显式传 `api_key` | 显式优于隐式，config 反写 os.environ 只为兼容遗留 |
| `AIMessageChunk` 的 `+` 拼接 | `LLMClient.accumulate(chunks)` | 手写合并逻辑完全可控 |
| `additional_kwargs["reasoning"]` 间接层 + monkeypatch | `AIMsg.reasoning` 一等字段 + `_parse_chunk` 直接读 `delta.reasoning` | 零 monkeypatch，vLLM 的非标准字段零成本获取 |

---

## 2.2 自建消息类型 —— 取代 langchain_core.messages

| | |
|---|---|
| **文件** | `src/llm/messages.py`（~89 行） |
| **取代** | `langchain_core.messages`（`SystemMessage` / `HumanMessage` / `AIMessage` / `ToolMessage` / `AIMessageChunk`） |
| **依赖** | 仅 `dataclasses` + `typing` |

### 类型清单

| 类型 | 行号 | 取代 | 特点 |
|------|------|------|------|
| `SystemMsg` | `messages.py:24` | `SystemMessage` | 纯数据 + `to_dict()` |
| `HumanMsg` | `messages.py:33` | `HumanMessage` | 纯数据 + `to_dict()` |
| `AIMsg` | `messages.py:42` | `AIMessage` | 含 `tool_calls` + `reasoning`（vLLM 思考链）+ `has_tool_calls` 属性 |
| `ToolMsg` | `messages.py:66` | `ToolMessage` | 含 `tool_call_id` |
| `Chunk` | `messages.py:84` | `AIMessageChunk` | 增量字段：`content_delta` / `reasoning_delta` / `tool_call_deltas` |

### AIMsg 关键设计（`messages.py:42-65`）

```python
@dataclass
class AIMsg:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)  # [{"id","type":"function","function":{"name","arguments"}}]
    reasoning: str = ""  # vLLM 思考链

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def to_dict(self) -> dict:
        d = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        # ★ 不序列化 reasoning —— 回传给模型时不带推理
        return d
```

> **`to_dict()` 不序列化 `reasoning`**：与 `strip_think_tags` 哲学一致——推理内容是给人看（前端展示）的，回传给模型时应该剥离，避免污染历史上下文导致循环。V2 靠 `additional_kwargs` 间接层实现同样的剥离，V3 直接在 `to_dict` 里做。

### 为什么这么写

- langchain 的消息类是 `Serializable` 子类，带 Runnable 接口、校验逻辑、序列化开销。**实际只需要「纯数据容器 + `to_dict()` 产 OpenAI 格式」**。
- `AIMsg.reasoning` 一等字段，省去 V2 的 `additional_kwargs["reasoning"]` 间接层。

> **混合态说明**：`HermesAgentV3` 内部 state 仍存 langchain 消息（为 `ContextManager` 兼容），所以 `agent_v3.py` 在边界做转换：
> - **出**：`_langchain_msg_to_dict()`（`agent_v3.py:697`）把 langchain 消息转 dict 给 `LLMClient`
> - **入**：`LLMClient` 返回 `AIMsg`，再重建 langchain `AIMessage` 存入 state（`agent_v3.py:432-455`）
>
> Phase 5.5 目标：`ContextManager` 也改用自建消息类型，届时 `_import_langchain_messages()`（`agent_v3.py:60`）可删除。

---

## 2.3 stream_with_hard_timeout —— 双 deadline 硬超时

| | |
|---|---|
| **文件** | `src/agent/llm_stream.py`（~141 行） |
| **取代** | V2 `graph.py:_stream_llm_with_hard_timeout`（底层 langchain `ChatOpenAI.stream`，现为 `LLMClient.stream_chat`） |
| **依赖** | `contextvars` / `queue` / `threading` |

> 这是 V3 最核心的韧性组件，包含三道防线。

### 核心函数

| 函数 | 行号 | 说明 |
|------|------|------|
| `stream_with_hard_timeout()` | `llm_stream.py:40` | 双 deadline 流式包装器 |
| `_worker()` | `llm_stream.py:72` | daemon 工作线程，跑 `stream_chat`，把 chunk 推入 queue |
| `stream_llm_simple()` | `llm_stream.py:135` | 无超时变体（测试用） |

### 三道防线

| 防线 | 机制 | 行号 | 防的是什么 |
|------|------|------|------------|
| A. 间隙超时（gap deadline） | `timeout_s` 秒内无新 chunk → `TimeoutError` | `llm_stream.py:92-116`（每 chunk reset） | 流式半开连接（完全不发字节） |
| B. 总时长上限（total deadline） | `timeout_s * 3` 秒必触发，**不 reset** | `llm_stream.py:88` | trickle-hang（每 119s 吐一个 token） |
| C. contextvars 传播 | worker 线程 `copy_context()` | `llm_stream.py:70` | 跨线程 user_id / vfs / depth 丢失 + openai SDK callback state 污染 |

### 核心实现（`llm_stream.py:40-132`）

```python
def stream_with_hard_timeout(llm_client, messages, tools=None, timeout_s=120):
    _chunks_q = _queue.Queue()
    _worker_exc = []

    ctx = contextvars.copy_context()                    # ★ 防线 C
    def _worker():
        try:
            for chunk in llm_client.stream_chat(messages, tools=tools):
                _chunks_q.put(chunk)
        except Exception as e:
            _worker_exc.append(e)
        finally:
            _chunks_q.put(_SENTINEL_END)

    t = threading.Thread(target=ctx.run, args=(_worker,), daemon=True)  # ★ ctx.run
    t.start()

    deadline = time.time() + timeout_s                  # 间隙 deadline（reset-able）
    total_deadline = time.time() + timeout_s * 3        # ★ 总时长硬上限（不 reset！）

    while True:
        now = time.time()
        wait = min(timeout_s, deadline - now, total_deadline - now)
        if wait <= 0:
            raise TimeoutError(f"LLM 流式总时长超过 {timeout_s*3:.0f}s")
        try:
            chunk = _chunks_q.get(timeout=wait)         # ★ 主线程用 queue.get 做超时
        except _queue.Empty:
            raise TimeoutError(f"LLM 流式 {timeout_s:.0f}s 内无新 chunk（疑似半开连接）")
        if chunk is _SENTINEL_END:
            if _worker_exc: raise _worker_exc[0]
            return
        yield chunk
        deadline = time.time() + timeout_s              # 间隙 deadline 重置
```

### 双重超时的精髓

- **间隙超时**（`deadline`）：`timeout_s` 内无新 chunk → 判死。但这个会被心跳字节重置。
- **总时长硬上限**（`total_deadline = timeout_s * 3`）：从开始累计，**reset 不重置它**。专克 trickle-hang——端点慢慢滴字节但永远不结束，间隙超时被反复重置，但总时长到点必斩断。

```
正常流式:    chunk─chunk─chunk─chunk─END    （gap timer 不断 reset，正常结束）
半开连接:    chunk───────────────────────▶   （gap timer 触发，timeout_s 后超时）
trickle-hang: chunk────119s────chunk────119s────chunk────▶
              （gap timer 被 119s 的 chunk 重置，永远不触发）
              ★ 但 total_deadline 不 reset，timeout_s*3 后强制斩断
```

### 防线 C：contextvars 跨线程传播

**问题**：把 `stream_chat()` 放进裸 `threading.Thread` 后，CPython **不会**传播 contextvars。而我们的 `user_id` / `vfs` / `sub_agent_depth` 全是 contextvar——裸线程拿不到 → 下一轮工具节点读 `get_current_user_id()` 拿到 None → 记忆/文件操作串号或崩溃。V2 还踩过一个隐蔽坑：LangChain 的 `var_child_runnable_config`（callback/tracer 配置）拿不到 → 污染共享 callback 状态 → 下一轮工具节点直接卡死（实测：web_search 解码完 HTML 后整图无后续日志）。

**解法**：起线程前 `ctx = contextvars.copy_context()`，线程入口 `ctx.run(_worker)`。

> **话术**：contextvars 跨线程必须显式 `copy_context() + ctx.run`，这是 Python 多线程很容易踩的坑。裸 `threading.Thread` 不传播 contextvar 是 CPython 的「特性」不是 bug——设计上避免共享可变状态，但实际工程需要时必须显式传播。

### 为什么 Python 无法强杀线程

超时后无法 `Thread.kill()`（Python 无此 API），但 openai SDK 的 stream 是同步迭代器，daemon 线程会因 queue 满或 socket 断开自然退出。最坏情况是 daemon 线程在 native socket read 阻塞到端点最终断开——但主线程已经恢复，不影响用户体验。

> **V2 对照**：V2 还有一个额外的连接泄漏修复——超时后构造一次性 `httpx.Client` 包装的 `ChatOpenAI`，`close()` 强制断 socket 让 worker 阻塞 read 抛异常退出。V3 用 openai SDK 自带的 `OpenAI(timeout=...)` 客户端，超时后底层 httpx 连接随客户端生命周期管理，连接泄漏问题大幅缓解。但 daemon 线程的阻塞 read 仍无法主动中断——这是 Python 的固有限制。

---

## 2.4 ThinkSplitter —— 流式 `<think>` 标签拆分状态机

| | |
|---|---|
| **文件** | `src/think.py`（~254 行） |
| **取代** | V2 的 `patch_langchain_reasoning` monkeypatch（V3 路径不用，遗留 `graph.py` 仍用） |
| **依赖** | 仅 `contextvars` / `re` |

### 为什么需要

不同模型 / 部署方式的 `<think>` 标签行为不一致，调用方不应该关心用哪种：

| 格式 | 模型 | content 内容 | ThinkSplitter 行为 |
|------|------|-------------|-------------------|
| 全标签 | 标准 Qwen3 | `<think>推理</think>正文` | 遇 `<think>` → 锁定 THINK 模式 |
| 半开 | vLLM/SGLang Qwen3 + Ornith | `推理</think>正文`（chat template 注入了开标签） | 遇 `</think>` → 锁定 CONTENT 模式 |

### 状态机三态（`think.py:128`）

```
PROBE ──遇 <think>──> THINK ──遇 </think>──> CONTENT
  │                                                
  └──遇 </think>──> CONTENT（半开格式）            
```

| 状态 | 行号 | 说明 |
|------|------|------|
| `PROBE` | `think.py:154-182` | 初始态，累积到锁定。比较 `<think>` 和 `</think>` 谁先出现决定模式。防爆缓冲：`len > 4000` 强制锁 CONTENT |
| `THINK` | `think.py:136-140` | 推理内容，emit `reasoning_delta` |
| `CONTENT` | `think.py:142` | 正文内容，emit `content_delta` |

### 组件清单

| 组件 | 行号 | 说明 |
|------|------|------|
| `ThinkSplitter` | `think.py:119` | 状态机拆分器 |
| `emit_think_tokens()` | `think.py:22` | 底层逐 chunk 拆分（含跨 chunk 半标签 `pending` 尾巴） |
| `strip_think_tags()` | `think.py:73` | 非流式剥离（存入会话历史前清理） |
| `extract_think_content()` | `think.py:83` | 非流式提取 `(reasoning, content)` 两路 |
| `feed()` / `flush()` | `think.py:136/144` | 状态机入口 / 尾部 flush |
| `_probe()` | `think.py:154` | PROBE 状态自动检测全标签 vs 半开 |
| `_tail_safe()` | `think.py:190` | 安全切割点，防半个标签尾巴被误发 |

### 集成点

`ThinkSplitter` 已集成进 `LLMClient.stream_chat()`（`client.py:167-188`），对所有调用方透明。`_react_loop` 里拿到的 `chunk.reasoning_delta` / `chunk.content_delta` 已经是拆分后的结果。

非流式版本（`extract_think_content` / `strip_think_tags`）用于：
- `_parse_response()`（`client.py:300`）：非流式响应从 content 提取 reasoning
- `_react_loop`（`agent_v3.py:431, 443`）：存入 state 前剥离 `<think>` 标签，防止下轮 LLM 看到残留标记导致循环

### V2 对照：为什么 V3 不用 monkeypatch

V2 的 `patch_langchain_reasoning()`（`think.py:215-253`）monkeypatch `langchain_openai._convert_delta_to_message_chunk`，把 `delta.reasoning` 累加进 `additional_kwargs["reasoning"]`。背景：LangChain 0.3.x 不读 `delta.reasoning` 导致推理内容丢失。

V3 用 `LLMClient`（openai SDK）后，`_parse_chunk`（`client.py:312`）直接读 `delta.reasoning`，零 monkeypatch。`patch_langchain_reasoning` 仍保留给遗留 `graph.py` 路径用，V3 路径不调它。

---

## 2.5 json_fix —— 容错 JSON 解析

| | |
|---|---|
| **文件** | `src/llm/json_fix.py`（~135 行） |
| **取代** | `langchain_core.utils.json.parse_partial_json` |
| **依赖** | 仅 `json` / `typing` |

### 函数

| 函数 | 行号 | 说明 |
|------|------|------|
| `parse_partial_json()` | `json_fix.py:30` | 容错解析可能不完整的 JSON |
| `safe_parse_tool_args()` | `json_fix.py:102` | 对齐 langchain `init_tool_calls`：先容错 → 再严格 → 全失败返回 `{}` |

### 为什么需要

流式输出中 LLM 的 `tool_calls.function.arguments` 可能是半截 JSON（流式还没吐完）。`_react_loop` 在 `accumulate` 之后拿到的 arguments 字符串需要容错解析。

`safe_parse_tool_args` 的三段式（`json_fix.py:102`）：
1. 先用 `parse_partial_json` 容错尝试（补全未闭合的 `{}`/`[]`、修尾逗号、修字符串内未转义换行）
2. 再用 `json.loads` 严格解析
3. 全失败返回 `{}`（而非抛异常），让工具用默认参数跑——比崩溃更安全

> **V2 对照**：`parse_partial_json` 是 langchain-core 的 vendored 源码（MIT，见文件头注释）。V3 直接拷出来避免引入 langchain-core 只为一个函数。`safe_parse_tool_args` 对齐 langchain `init_tool_calls` 的「容错 → 严格 → 默认」三段式语义。

### 使用点

`agent_v3.py:450`：`_react_loop` 把 `ai_msg.tool_calls` 里的 `tc["function"]["arguments"]`（字符串）用 `safe_parse_tool_args` 解析成 dict，存入 langchain `AIMessage.tool_calls`。

---

## 本章小结

Tier 1 的 8 个组件构成了 V3 的 LLM 调用完整链路：

```
HermesAgentV3._react_loop
   │
   ├─ 构造 messages（自建 → 边界转 dict）
   ├─ stream_with_hard_timeout(LLMClient.stream_chat)     [2.3 + 2.1]
   │     │
   │     ├─ daemon 线程 + copy_context                     [防线 C]
   │     ├─ LLMClient.stream_chat 内置 ThinkSplitter       [2.4]
   │     └─ 双 deadline 超时                                [防线 A+B]
   │
   ├─ yield token / reasoning_token
   ├─ accumulate(chunks) → AIMsg                           [2.1]
   ├─ safe_parse_tool_args(arguments)                      [2.5]
   └─ 转 langchain AIMessage 存入 state（混合态）
```

**零 langchain 依赖**——这 8 个文件顶层 import 里没有任何 `langchain` / `langgraph` 字样。混合态的 langchain import 只在 `HermesAgentV3`（Tier 3）和 `ContextManager`（Tier 3）里。

---

> **下一章**：[03-声明式三层工具](03-声明式三层工具.md) —— ToolSpec / SideEffects / decide 三层叠加 / 4 种执行器 / YAML 加载。
