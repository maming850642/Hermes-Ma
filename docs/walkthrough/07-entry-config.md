# 第 7 章 · 入口与配置 —— `cli.py` + `web_fastapi/` + `config.py` + `state.py`

> 承接第 1-6 章。本章覆盖 Agent 与用户/外部世界的界面层。
> `src/cli.py` 是项目最大的文件，承载了对话渲染、会话管理、命令分发等几乎所有终端交互。`web_fastapi/` 是它的 Web 等价物——但和早期 Streamlit 版本完全不同：现在是 **FastAPI + 每用户独立 worker 子进程** 的多进程架构。`config.py` 和 `state.py` 是支撑基础设施。

---

## 0. 一句话定位

入口与配置层由四个部分组成：

- **cli.py**：Rich CLI，事件消费 + Live 面板渲染 + 会话持久化 + 斜杠命令分发（项目最大文件）
- **web_fastapi/**：FastAPI Web 服务，每登录用户 fork 一个 worker 子进程跑完整 Agent，进程级隔离全局状态
- **config.py**（根）：配置加载器，yaml 唯一真相源 + env 覆盖 + 防坏注入
- **state.py**：AgentState TypedDict，图节点间传递的状态结构

> **重要变迁**：早期 Web 端是单进程 Streamlit（`web.py`），现已删除。本章所述 Web 全部指 `web_fastapi/`（多进程 FastAPI）。CLI 与 Web 不再共享渲染代码——CLI 用 Rich Live 面板，Web 用 SSE 流；两者通过 HermesAgent 的事件协议（第 1 章）保持一致。

---

## 1. 模块全景

| 文件 | 行数 | 角色 |
|------|------|------|
| `src/cli.py` | 1345 | **CLI 全部交互逻辑**：对话渲染、会话持久化、命令分发 |
| `web_fastapi/main.py` | 51 | Web 启动入口（uvicorn） |
| `web_fastapi/app.py` | 60 | FastAPI 应用工厂 + lifespan + 注册 10 个 router |
| `web_fastapi/worker_manager.py` | ~290 | WorkerManager：user_id → worker 子进程管理（创建/销毁；无空闲回收） |
| `web_fastapi/worker_process.py` | 624 | worker 子进程入口：跑完整 Agent + IPC 事件循环 |
| `web_fastapi/ipc.py` | 61 | NDJSON IPC 协议（encode/decode/request/event/result/done/error） |
| `web_fastapi/sse.py` | 20 | stream_invoke 事件 → SSE 字节流编码 |
| `web_fastapi/auth.py` | 31 | 无密码认证：itsdangerous 签名 cookie |
| `web_fastapi/dependencies.py` | 45 | 依赖注入：解析 user_id + 获取 worker |
| `web_fastapi/security.py` | 39 | 标识符校验，防路径穿越 |
| `web_fastapi/services/config_service.py` | 57 | 配置读写 + 热更新分类 + 敏感值掩码 |
| `web_fastapi/routers/`（10 个） | — | auth/chat/sessions/config/mcp/projects/upload/system/memory/pages |
| `config.py` | 87 | 配置加载（已在第 1 章深读，这里收口） |
| `src/state.py` | 140 | AgentState 定义 |

### cli.py 的内部结构

| 区块 | 职责 |
|------|------|
| 会话持久化 | `save_session` / `load_session` / `list_sessions` / `_migrate_old_sessions`（schema v2 + 旧格式迁移） |
| CLI 组件 | `show_logo` / `show_help` / `show_skills` / `show_memory` / `show_tools` / `compact_session` / `show_session_history` |
| 对话核心 | `chat()`（事件消费 + Live 面板渲染，最复杂） |
| 主入口 | `main()` 命令分发循环（14 条斜杠命令） |

### web_fastapi 的分层

```
HTTP 请求
  │
  ▼
[main.py] uvicorn 启动，host/port 均可配置（WEB_HOST/web_host、WEB_PORT/web_port）
  └─ [app.py] create_app() + lifespan(启动自检 + WorkerManager)
       ├─ [dependencies.py] get_current_user_id → validate_user_id
       ├─ [routers/*] 10 个 router（auth/chat/sessions/...）
       │    └─ [chat.py] StreamingResponse(SSE) → worker.send_stream
       └─ [worker_manager.py] WorkerManager.get_or_create(user_id)
            └─ fork [worker_process.py] 子进程
                 ├─ stdin 读命令（NDJSON）
                 ├─ stdout 写事件流（NDJSON，经写线程队列）
                 └─ 跑完整 MemoryManager + HermesAgent
```

**本章核心看点**：

1. CLI 侧：`chat()` 的事件消费 + Live 面板渲染（自动分段 + 并发降级 + HITL 双层循环）
2. Web 侧：每用户 worker 子进程的隔离架构——为什么不用线程、生命周期如何管理、cancel 如何插队
3. Web 侧：stdout 写线程防管道写满、`_cancel_event` 软中断、后台会话总结
4. 两端共用的会话持久化（schema v2）和 config/state 基础设施

---

## 2. 架构与数据流

### 2.1 CLI 主循环（`main()`）

```
main()
  ├─ show_logo() + run_health_check()（启动自检）
  ├─ MemoryManager() + HermesAgent() 初始化
  ├─ _migrate_old_sessions()（旧格式迁移）
  ├─ 用户登录
  └─ while True: 对话循环
       ├─ 读取 user_input
       ├─ if "/xxx": 命令分发
       │    /switch /resume [-a|<id>] /rename /skill /mcp /memory /clear
       │    /tools /compact /reset /save /help /exit
       └─ else: chat(agent, user_id, input, session_messages, ...)
            └─ for event in agent.stream_invoke(...): 消费事件 → Live 面板
       └─ 对话后：current_todos = agent.get_todos(); save_session(...)
```

### 2.2 Web 请求流（多进程）

```
浏览器
  │ POST /api/chat/stream (message, thinking, images)
  ▼
[chat.py chat_stream]
  └─ get_worker(user_id) → WorkerManager.get_or_create
       └─ 若无 worker：fork 子进程，等 ready 信号（60s 超时）
  └─ worker.send_stream("chat", message=..., thinking=..., images=...)
       └─ stdin.write(NDJSON 请求)
       └─ while: _readline_with_timeout(stdout) → yield 事件
  └─ _ipc_events_to_sse → encode_sse_event → StreamingResponse
  │
  ▼ (worker 子进程内)
[worker_process.py _op_chat]
  └─ _cancel_event.clear()
  └─ agent.stream_invoke(role=, thinking=, images=, compact_threshold_pct=)
  └─ _drain_stream_events: 事件 → _send → stdout 写线程队列
       ├─ turn_messages / messages_snapshot: 内部消化（回写 session_messages）
       └─ 其余事件转发（token/tool_start/complete/auto_compact...）
  └─ state._save_current()（落盘）
  └─ make_done
```

### 2.3 事件消费对照（CLI ↔ Web）

两端消费的是**同一套事件协议**（graph.py stream_invoke 产出），但渲染不同：

| 事件 | CLI（chat()） | Web（_drain_stream_events） |
|------|---------------|----------------------------|
| `token` | Live 面板流式 + 自动分段 | 转发为 SSE event → 前端增量渲染 |
| `tool_start`/`tool_end` | Live 工具面板 / 并发降级 | 转发 SSE，前端独立展示 |
| `human_approval_request` | Prompt.ask 弹窗 + 外层 while 重入 | 转发 SSE，前端弹审批框 → POST /approve |
| `turn_messages` | del[-N:] + extend 去重写 session_messages | **内部消化**（回写 state.session_messages） |
| `messages_snapshot` | 整体替换 session_messages | **内部消化**（整体替换） |
| `complete` | Markdown 最终渲染 | 转发 SSE done |

> **设计要点**：Web 端把 `turn_messages` / `messages_snapshot` 在 worker 内部消化（不转发到前端），因为这两类是「会话状态同步」事件，前端只需要渲染用的 token/tool/complete。CLI 则直接消费它们维护 `session_messages`。同一事件，两端因职责不同而处理方式不同。

---

## 3. 逐段深潜

### 3.1 `state.py` —— AgentState 与 add_messages reducer

```python
class AgentState(TypedDict):
    user_id: str
    session_id: str
    messages: Annotated[list[BaseMessage], add_messages]   # ← reducer
    retrieved_memories: list[str]
    current_input: str
    compact_requested: bool
    todos: list[Todo]
    virtual_fs: dict[str, str]
    iteration_count: int
    current_role: str          # M1-3/4: 角色模式（""=默认, "chief-of-staff"=总参）
    thinking: bool             # 推理模式开关（<think> 标签拆分）
```

#### `add_messages` reducer 的语义

```python
messages: Annotated[list[BaseMessage], add_messages]
```

`Annotated[type, reducer]` 是 LangGraph 的语法：声明这个字段用 `add_messages` reducer 合并。reducer 决定了「节点返回值如何合并进 state」。

`add_messages` 的语义是**追加**（而非覆盖）：节点返回 `{"messages": [new_msg]}` 时，new_msg 会被**追加**到现有 messages 列表。要「先删旧再添新」必须用 `RemoveMessage`（它会被 reducer 识别为「删除指令」）。

> **联系第 1、3 章**：graph.py 的 `_compact_node` 和 `_precheck_node` 都用 `removals + new_messages`（RemoveMessage 删旧 + 新消息添新），正是基于这个追加语义。第 3 章 context.py 的 `compact_messages` 给摘要赋显式 id `compact-{uuid}`，就是为了 RemoveMessage 能识别它（详见第 3 章 3.2）。

#### 不用 reducer 的字段

`iteration_count`、`current_role`、`thinking` 是**直接覆盖**语义（没有 Annotated + reducer）。因为它们要的是「最新值」而非「累加」——节点返回 `{"iteration_count": n+1}` 会替换旧值。

`current_role` 和 `thinking` 是 per-send 参数：由 `stream_invoke(role=..., thinking=...)` 在构造 `initial_state` 时注入（`create_initial_state` 支持 role/thinking 参数），贯穿整个回合。

> **设计要点**：选择哪个字段用 reducer 是有讲究的——messages 要累积（追加）、todos 要全量替换（write_todos 每次传完整列表）、iteration_count/role/thinking 要覆盖。每种语义匹配不同的业务需求。

---

### 3.2 `config.py`（根）—— 双访问 + lru_cache + 防坏注入

第 1 章已详述，这里收口要点。

#### `_Settings` 双访问（dict 子类）

```python
class _Settings(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(...)
    def __setattr__(self, name, value):
        self[name] = value
```

同时支持 `settings.llm_model_name`（属性）和 `settings["llm_model_name"]`（字典），兼容历史调用代码。**注意：这不是 pydantic-settings，也没有 .env 文件**——配置集中在 `config.yaml`，env 只覆盖 yaml 已有键。

#### `@lru_cache get_settings()`

```python
@lru_cache
def get_settings() -> _Settings:
    with open(PROJECT_ROOT / "config.yaml") as f:
        cfg = yaml.safe_load(f) or {}
    for key in list(cfg):                      # ← 只遍历 cfg 已有键
        env_val = os.environ.get(key.upper())
        if env_val is None: continue
        ...
```

三个设计：

1. **`lru_cache`**：单例，避免反复读 yaml。
2. **env 只覆盖 cfg 已有键**（Bug 7 修复）：早期反向扩展会把 PATH/TEMP/HOME 等全部塞进 settings，污染。现在 `for key in list(cfg)` 只处理 yaml 里存在的键。
3. **防坏注入**：父进程曾把 `EMBEDDING_MODEL` 注入成丢了命名空间前缀的值（`all-minilm-l6-v2-f32:latest` 而非 `tazarov/all-minilm-l6-v2-f32:latest`），覆盖 yaml 正确值导致 Ollama 404。检测「yaml 含 `/` 但 env 不含且是后缀匹配」判为坏注入，保留 yaml。

#### `_INT_KEYS` 强制转 int

```python
_INT_KEYS = {
    "llm_timeout", "max_short_term_messages", ..., "model_context_window",
    "compact_threshold_pct",
}
```

yaml 里若带引号（如 `llm_timeout: '120'`）会被解析成 str，传给 `socket.settimeout` / 比较时会 TypeError。这里对已知数值键强制 `int(float(...))`。`compact_threshold_pct` 和 `model_context_window` 都在此列——它们参与 token 阈值压缩计算（第 3 章）。

---

### 3.3 `cli.py` 的会话持久化 —— schema v2 + 迁移

#### schema v2（`save_session`）

```python
session_data = {
    "user_id": ..., "session_id": ..., "name": final_name,
    "created_at": existing_created_at or datetime.now().isoformat(),
    "updated_at": datetime.now().isoformat(),
    "message_count": ..., "preview": preview,
    "messages": serializable,
    "todos": todos or [],              # schema v2: 跨轮次持久化 todos
    "virtual_fs": virtual_fs or {},    # schema v2: 持久化虚拟文件系统
    "schema_version": 2,
}
```

**设计要点**：
- **`created_at` 保护**：每次 save 先读已有文件的 created_at，保持首次创建时间不变。
- **`preview` 冗余字段**：首条用户消息前 60 字符（用 `extract_text` 处理多模态 content）。反范式设计——让 `list_sessions` 不必读全量 messages 就能展示。
- **`schema_version: 2`**：`load_session` 对缺此字段的旧文件返回空 todos/vfs——向后兼容。

#### 旧格式迁移（`_migrate_old_sessions`）

会话结构从「每用户单文件 `data/sessions/{user_id}.json`」升级到「每用户多会话目录 `data/sessions/{user_id}/{session_id}.json`」。迁移在 `main()` 启动时自动执行，迁移后旧文件改 `.bak` 不删除（可回滚）。

#### ToolMessage 的 tool_call_id 持久化（F3 修复）

保存时记录 `tool_call_id`，加载时恢复。**动机**：ToolMessage 必须有 tool_call_id 才能和对应 AIMessage(tool_calls) 配对。早期不持久化会导致恢复后配对断裂，LLM 收到「工具调用无对应结果」会困惑。这也是第 3 章 context.py「工具配对保护」的关联——持久化层也要保证配对完整。

> **Web 端复用同一份持久化**：worker 子进程的 `WorkerState._save_current()` 直接调 `from src.cli import save_session`。所以 CLI 和 Web 的会话格式**完全兼容**（都 schema v2）——这是和早期 Streamlit 版本的关键区别（那时 web.py 用简化格式，不互通）。

---

### 3.4 `chat()` —— 事件消费与 Live 面板渲染（CLI 核心）

这是整个 cli.py 最复杂、最长的函数（`chat()`，约 300 行）。它消费 graph.py 的所有事件，用 Rich Live 面板做流式渲染。

#### 自动分段机制

```python
term_h = console.size[1]
max_panel_lines = max(term_h - 6, 10)    # 留 6 行给边框/标题/padding

# token 事件里，定期检查当前文本行数
need_check = (token_count % 8 == 0) or (segment_chars > 200 and token_count % 3 == 0)
if need_check and len(current_text) > 100:
    est = _estimate_lines(current_text, max(console_width - 6, 20))
    if est >= max_panel_lines:
        _freeze_and_start_new_segment()   # 冻结当前面板，开新段落
```

**动机**：如果 AI 回复很长（如代码生成），单个 Live 面板会超过终端高度，导致内容堆叠、滚动混乱。自动分段在「估算行数 ≥ 终端高度 - 6」时冻结当前面板（保留在终端），开新面板继续。

> **设计哲学**：这是「终端 UI 适配」的精细工作。Web 端有滚动条，不需要这种处理；但 CLI 必须考虑终端高度有限。`_estimate_lines` 手动估算换行，因为 Rich 不会告诉你面板真实占多少行。

#### 流式文本用 Text，完成用 Markdown

```python
def _make_text_panel(text, is_draft=True):
    content = Text(text, style="white") if is_draft else Markdown(text)
```

**动机**：流式过程中用 `Text`（行数精确，避免 Rich Live 残留边框）；完成时切 `Markdown`（带格式渲染）。流式就用 Markdown 会让未闭合标记导致渲染异常。

#### 并发工具的检测与降级

```python
if live is not None and live_mode and live_mode != "text":
    # 已经有一个工具在 Live 里，又来新工具 → 并发
    concurrent_mode = True
    _stop_live()
elif concurrent_mode:
    pass   # 并发模式下不再用 Live
else:
    live_mode = tool_id
    _start_live(_build_tool_panel(tool_info))
```

**动机**：多个工具并发时，Live 面板无法同时展示多个运行中工具。检测到第二个工具时切「并发模式」——冻结 Live，后续所有工具用静态 `console.print` 顺序打印。

> **联系第 2 章**：第 2 章讲 tools.py 的并发执行（ThreadPoolExecutor）。这里 cli.py 的并发模式是**消费端**的适配。

#### HITL 的弹窗 + 恢复循环

```python
# 外层 while 支持多次 stream（中断 → 恢复 → 继续）
while True:
    interrupted = False
    for event in agent.stream_invoke(..., resume_payload=resume_payload):
        if event_type == "human_approval_request":
            _stop_live()
            resume_payload = Prompt.ask(...)
            interrupted = True
            break                      # 跳出内层 for
    if not interrupted:
        break                          # 外层 while 退出
```

**设计**：外层 while + 内层 for 的双层循环。遇到 human_approval_request 就 break，外层 while 用 resume_payload 重新调 stream_invoke（走 `Command(resume=...)` 路径）。

> **联系第 1 章**：graph.py 的 stream_invoke 接受 `resume_payload`。CLI 的双层循环是它的调用方。Web 端的等价物是前端的审批弹框 + POST /approve（worker 侧的 `_op_chat_approve`）——同样是「中断后用 resume_payload 重入」，但中断-恢复由 HTTP 请求边界天然分隔，不需要双层循环。

#### turn_messages / messages_snapshot 的去重与整体替换

```python
elif event_type == "messages_snapshot":
    snapshot_msgs = event.get("messages", [])
    session_messages = list(snapshot_msgs)   # 整体替换
    last_turn_msg_count = 0
elif event_type == "turn_messages":
    turn_msgs = event.get("messages", [])
    if last_turn_msg_count > 0:
        del session_messages[-last_turn_msg_count:]   # 删掉上次写入的
    session_messages.extend(turn_msgs)                  # 写入新的
    last_turn_msg_count = len(turn_msgs)
```

**`messages_snapshot`**（commit aa87994）：压缩后 messages 变短，增量切片 `turn_messages` 会是空列表。用压缩后的全量快照整体替换，保证落盘的是摘要而非原始对话。同时清零 `last_turn_msg_count`，避免后续 `turn_messages` 的 `del[-N:]` 误删。

**`turn_messages` 去重**（Bug 2 修复）：HITL 中断和恢复时，stream_invoke 可能多次 yield turn_messages。用 `last_turn_msg_count` 记录上次写入数，先 del 再 extend，保证最终只写一次。

---

### 3.5 `web_fastapi/` —— 多进程架构深潜

这是本章相对前六章全新的部分。Web 端不再是单进程 Streamlit，而是 **FastAPI 主进程 + 每用户 worker 子进程** 的架构。

#### 3.5.1 为什么用多进程而非线程

早期 Streamlit 版本是单进程，所有用户共享一个 Agent 实例和全局状态（contextvar、virtual_fs、remember 的 current_user_id 等）。这导致：
- 多用户并发时 contextvar 串台（第 1 章的流式 contextvar 丢失问题）
- `set_current_user_id` / `set_current_vfs` 这类全局变量无法隔离

多进程方案（`WorkerManager`）让每个登录用户拥有独立子进程，跑完整的 `MemoryManager + HermesAgent`，进程级隔离所有全局状态。代价是内存（每进程一份 Agent），换来的是**零并发竞态**。

#### 3.5.2 WorkerManager —— 子进程生命周期

`WorkerManager` 维护 `user_id → WorkerProcess` 字典，核心职责是创建、销毁 worker。

> **2026-08 变更**：空闲回收（Reaper 守护线程）已整体移除。单用户架构下，
> 定时回收只有副作用——prefs/权限模式/current_sid 被周期重置、后台总结被
> 杀丢数据。worker 现为**常驻**：仅登出（`remove`）或应用关闭
> （`shutdown_all`）时优雅退出。当年 reaper 与并发触活之间的竞态复查设计
> （`last_active_ts` 刷新 + force 复查）随之一并退场。

**`get_or_create` 的 ready 握手**：

fork 子进程后，等它发 `ready` 信号（60s 超时）。worker 启动时要初始化 MemoryManager + HermesAgent（连 Qdrant、加载工具），可能慢。ready 之前若往 stdin 写命令会丢。失败（init 异常）时 worker 发 `error` 信号，`get_or_create` 据此抛 RuntimeError → 前端 500。

#### 3.5.3 WorkerProcess —— 锁获取超时 + 信号传播 cancel

`WorkerProcess` 封装单个子进程的通信。两个关键设计：

**锁获取带超时**（`send()`，`lock_wait=5.0`）：

```python
def send(self, op, timeout=DEFAULT_TIMEOUT, lock_wait=5.0, **kwargs):
    if not self._lock.acquire(timeout=lock_wait):
        raise TimeoutError(f"worker {self.user_id} 忙（chat 进行中？）...")
```

**动机**：chat 流式期间 `send_stream` 会长时间持锁（最长 DEFAULT_TIMEOUT/轮）。并发的小命令（切会话、查记忆）若无限等锁会卡死前端。拿不到锁立即抛 TimeoutError，调用方（`_busy_if_timeout`）降级为 HTTP 503「AI 正在思考，请稍候」。

**`request_cancel()` —— 绕过锁的插队取消**：

```python
def request_cancel(self):
    """紧急发送 chat_stop，不走 _lock（推理期间锁被 send_stream 占用）。
    直接写 stdin 插队。"""
    self.proc.stdin.write(encode_message(make_request("chat_stop")))
    self.proc.stdin.flush()
```

**动机**：用户点「停止」时，chat 的 `send_stream` 正持着锁。走 `send("chat_stop")` 会等满 lock_wait 超时。所以 cancel **绕过锁**直接写 stdin。线程安全靠 CPython GIL（write+flush 原子）+ worker 按行解析 NDJSON。

worker 侧收到 `chat_stop` 后设置 `_cancel_event`（见 3.5.4），`_drain_stream_events` 在事件间隙轮询它提前 break。chat.py 的 `/stop` 端点和 `/stream` 的 finally 都调 `request_cancel`——前者是用户主动停，后者是客户端断开（切会话/关标签页）时的兜底。

**`_readline_with_timeout` —— 防 stdout 永久阻塞**：

```python
def _readline_with_timeout(proc_stdout, timeout):
    q = _q.Queue()
    done_flag = threading.Event()
    def _reader():                       # 后台 daemon 线程
        line = proc_stdout.readline()
        if not done_flag.is_set():
            q.put(line)
    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    try:
        return q.get(timeout=timeout)    # 主线程带超时等
    except _q.Empty:
        done_flag.set()
        return None
```

**动机**：`proc.stdout.readline()` 是无限阻塞的。worker 卡住时前端会永久挂起。用后台 daemon 线程读 + 主线程 `queue.get(timeout)`，超时返回 None → 调用方据此抛 TimeoutError。注意 `_reader` 线程是 daemon——超时后它仍阻塞在 readline，随进程退出清理（无法真正中断 native read）。

#### 3.5.4 worker_process.py —— stdout 写线程 + 软中断 + 后台总结

worker 子进程（`web_fastapi.worker_process`）是 Agent 的实际运行环境。三个核心设计：

**stdout 写线程（防管道写满永久阻塞，commit 7327e41）**：

```python
_stdout_q = _q.Queue()        # 无界队列
_pipe_dead = threading.Event()

def _stdout_writer():
    while True:
        msg = _stdout_q.get()
        if msg is None: return            # 关闭信号
        if _pipe_dead.is_set(): continue  # 管道已死，丢弃
        try:
            sys.stdout.write(msg); sys.stdout.flush()
        except Exception:
            _pipe_dead.set()              # 标记管道死

def _send(msg, *, sync=False):
    encoded = encode_message(msg)
    if sync:
        sys.stdout.write(encoded)         # 启动握手用（ready/error）
    else:
        _stdout_q.put(encoded)            # 运行期事件走队列
```

**动机（卡死点 B）**：前端断开 SSE（切会话/关标签页）后 worker 无感知，继续往 stdout 写事件。管道 buffer（~64KB）写满后 `sys.stdout.write` **永久阻塞**，导致 worker 对所有后续请求无响应，而父进程还复用这个「活着但卡死」的 worker。

**解法**：`_send` 不直接写 stdout，而是 put 到无界队列；常驻 daemon 写线程从队列取消息写 stdout。管道满/关闭时，阻塞的只是后台写线程，**不再卡住处理命令的主循环**。`_pipe_dead` 标志让后续事件直接丢弃。

**`sync=True` 的例外**：启动期的 `ready` / init 失败的 `error` 信号必须走同步直写——这些是父子进程的同步点，走异步队列时 daemon 写线程可能被 OS 延迟调度，导致父进程 `get_or_create` 等满一个 10s 轮才收到 ready（实测启动从亚秒级退化到 10s+）。

**`_cancel_event` 软中断（commit 082754f）**：

```python
_cancel_event = threading.Event()

# _op_chat 开头清除上一轮残留
_cancel_event.clear()

# handle_command 收到 chat_stop：
elif op == "chat_stop":
    _cancel_event.set()
    _send(make_result(req_id, ok=True))

# _drain_stream_events 每个事件检查：
for event in stream:
    if _cancel_event.is_set():
        break                  # 停止消费
    ...
# finally: stream.close()      # 关闭 generator 释放 LangGraph 状态
```

**动机**：用户点「停止」时，要尽快终止当前推理。但单轮 LLM 流式内部（`_stream_llm_with_hard_timeout`）不可中断——只能靠 `llm_timeout` 间隙超时兜底。`_cancel_event` 在 **ReAct 多轮的间隙**生效：每收到一个事件就检查一次，检测到则 break 退出 stream 消费循环，`stream.close()` 关闭 generator。下一轮 agent_llm 不会启动。

**后台会话总结（`_bg_summarize_executor`）**：

```python
_bg_summarize_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bg-summary")

def _summarize_in_background(memory_manager, user_id, messages, session_id):
    def _task():
        on_session_end(memory_manager, user_id, messages, session_id, timeout=120.0)
    _bg_summarize_executor.submit(_task)
```

**动机**：`session_reset`（新建会话）时，要对旧会话做总结（调 LLM，耗时）。若同步等，用户点「新建会话」要等几十秒。所以 reset 立即返回新 session_id（用户零等待），总结 fire-and-forget 丢后台。`max_workers=1` 串行化，避免多次快速新建会话时并发 LLM 调用冲击。

#### 3.5.5 IPC（NDJSON 协议）

`ipc.py` 定义父子进程的通信协议——每行一个 JSON 对象（NDJSON）：

| 函数 | 方向 | 用途 |
|------|------|------|
| `make_request(id, op, **kw)` | 父→子 | 命令（chat/chat_stop/session_load/...） |
| `make_event(id, event, **data)` | 子→父 | 流式事件（token/tool_start/...） |
| `make_result(id, **data)` | 子→父 | 非流式结果 |
| `make_done(id)` | 子→父 | 该请求事件结束 |
| `make_error(id, message)` | 子→父 | 错误 |

**为什么用 NDJSON 而非 JSON-RPC/HTTP**：stdin/stdout 是最简单的跨进程通道，NDJSON 按行分隔天然支持流式（一行一事件）。`req_id` 让父进程在共享 stdout 上区分并发请求的事件（虽然 worker 串行处理，但 ready/error 等信号也需要 id 匹配）。

**stdout 专用 IPC**：worker 的日志走 stderr（`logging_config` 配置），**绝不写 stdout**——否则会污染 IPC 通道。

#### 3.5.6 SSE 转发 + auth + security

**SSE 转发**（`sse.py` + `chat.py`）：

```python
def _ipc_events_to_sse(worker, op, **kwargs):
    for data in worker.send_stream(op, **kwargs):
        mtype = data.get("type")
        if mtype == "event":
            yield encode_sse_event(data["event"], data["data"]).encode("utf-8")
        elif mtype == "result":
            yield encode_sse_event("complete", data["data"]).encode("utf-8")
        elif mtype == "error":
            yield encode_sse_event("error", {"message": ...}).encode("utf-8")
```

`encode_sse_event` 把事件编码为 SSE 格式（`event: xxx\ndata: {...}\n\n`）。`StreamingResponse` 把这个 generator 推给浏览器。前端用 `EventSource` 消费。

**无密码认证**（`auth.py`）：

```python
def create_session_cookie(user_id, secret):
    s = URLSafeSerializer(secret, salt="hermes-session")
    return s.dumps({"uid": user_id})     # 签名（非加密）
```

匹配 CLI「输入 user_id 即登录」语义，仅用于本地/内网。`URLSafeSerializer` 做**签名**（防篡改）不做加密——cookie 可读但不可伪造。secret 来自 `WEB_SECRET_KEY` 环境变量（默认 dev 值）。

**用户标识解析**（`dependencies.py`）：

```python
def get_current_user_id(request):
    # 1. 优先读 X-User-Id 头（前端 sessionStorage，每标签页独立）
    header_uid = request.headers.get("X-User-Id", "").strip()
    if header_uid:
        return validate_user_id(header_uid)
    # 2. 回退：签名 cookie
    cookie = request.cookies.get("session")
    ...
```

**两层标识**：`X-User-Id` 头优先（前端 sessionStorage 存，每标签页独立），cookie 回退。这样同一浏览器开两个标签页登录不同用户不会串（cookie 是共享的，但头是每标签页独立的）。

**防路径穿越**（`security.py`）：

```python
_DANGEROUS = re.compile(r"[\\/]\u0000|\.\.|[\\/]|^\.|\u0000")

def validate_user_id(value):
    if not _is_safe(value):
        raise HTTPException(400, "用户 ID 含非法字符")
    return value
```

`user_id` / `session_id` 会被直接拼入文件路径（`SESSIONS_DIR / user_id / f"{session_id}.json"`）或子进程命令行，必须拒绝含路径分隔符、`..`、空字节的值。允许中文等非 ASCII（兼容中文 user_id）。注意按整体字符串检查（`a/../b` 也要拒）。

#### 3.5.7 10 个 router 的职责

| router | prefix | 职责 |
|--------|--------|------|
| `auth` | `/api/auth` | login（fork worker）/ logout（force remove）/ me / switch |
| `chat` | `/api/chat` | stream（SSE 对话）/ approve（HITL 恢复）/ stop（cancel） |
| `sessions` | `/api/sessions` | list / current / save / reset / rename / delete / load / summary |
| `config` | `/api/config` | system（读写 yaml，需重启）/ prefs（per-user 热更新）/ role（角色 toggle） |
| `mcp` | `/api/mcp` | servers CRUD / reload / set_enabled（IPC→worker） |
| `projects` | `/api/projects` | thinktank 项目列/建/读 md（M1-5） |
| `upload` | `/api` | upload（图片→data/uploads）/ uploads（读图预览） |
| `system` | `/api` | tools / skills / compact / health |
| `memory` | `/api/memory` | get / clear（IPC→worker） |
| `pages` | `/` | 返回 HTML 模板（login/chat/sessions/memory/config） |

**两类 router**：大部分是「IPC 转发」（HTTP → worker.send → IPC → worker 处理 → result），如 sessions/memory/system/mcp。少数在前端直接处理，如 config 的 system 配置（读写 yaml，worker 不碰）、projects（直接读文件系统）、upload（写文件）。

#### 3.5.8 config_service —— 热更新分类

```python
HOT_RELOADABLE_KEYS = {
    "workspace_root", "web_proxy", "temperature",
    "max_tokens", "memory_min_score", "max_memory_results",
    "compact_threshold_pct",
}
SECURITY_KEYS = {"shell_enabled", "shell_allowed_commands", "shell_blocked_patterns"}
```

配置分两类：
- **系统配置**（openai_api_key/llm_model_name/...）：写 config.yaml，需重启（worker 进程持有 settings 快照）。
- **per-user 偏好**（temperature/max_tokens/compact_threshold_pct/...）：存 worker 的 `WorkerState.prefs`，热更新——下一轮 stream_invoke 立即生效（如 `compact_threshold_pct` 通过 `_op_chat` 的 `compact_threshold_pct=state.prefs.get(...)` 传入）。

`mask_secret` 对 openai_api_key 等敏感值掩码（保留前 4 位）后才返回前端。`SECURITY_KEYS` 永远不开放热更新。

#### 3.5.9 main.py —— host/port 均可配置

```python
host = os.environ.get("WEB_HOST", _settings.get("web_host", "0.0.0.0"))
port = int(os.environ.get("WEB_PORT", _settings.get("web_port", 8000)))
```

`host` 优先级 `WEB_HOST` env > yaml `web_host` > `0.0.0.0`；`port` 优先级 `WEB_PORT` env > yaml `web_port` > `8000`（前端用相对路径 `fetch('/api/...')`，改端口无需同步改前端）。启动时打印本机/局域网访问地址（Windows 上浏览器不能用 0.0.0.0 访问）。

---

## 4. 设计权衡总结

### 4.1 优点

| 设计 | 价值 |
|------|------|
| add_messages reducer | messages 自然追加，RemoveMessage 控制删除 |
| CLI 自动分段 | 终端 UI 适配，长回复不堆叠 |
| 流式 Text / 完成 Markdown | 兼顾流式稳定与最终格式 |
| 并发模式降级 | 多工具时自动切静态打印 |
| HITL 双层循环（CLI）/ HTTP 边界（Web） | 中断-恢复语义清晰 |
| messages_snapshot 整体替换 | 修复压缩不落盘（commit aa87994） |
| schema v2 + 迁移 | 向后兼容 + 自动升级；CLI/Web 格式统一 |
| 多进程隔离 | 零并发竞态，每用户独立 Agent |
| 常驻生命周期（回收已移除） | 仅登出/应用退出时优雅关闭 worker |
| stdout 写线程 | 防 SSE 断开后管道写满永久阻塞 |
| request_cancel 绕锁插队 | 「停止」不卡在锁等待 |
| 锁获取超时 → 503 | chat 进行中小命令友好降级 |
| 无密码签名 cookie | 匹配 CLI 登录语义，本地/内网够用 |
| 防路径穿越 | user_id/session_id 不注入文件路径 |
| 配置热更新分类 | per-user 偏好即时生效，系统配置重启生效 |

### 4.2 技术债 / 待优化

| 项 | 说明 |
|----|------|
| cli.py 过大（1345 行） | 会话持久化/组件/对话核心/入口全在一个文件 |
| chat() 函数过长（300+ 行） | 5 个闭包共享 nonlocal 变量，状态隐式 |
| _readline_with_timeout 的 reader 线程泄漏 | 超时后 daemon 线程仍阻塞在 readline，随进程退出清理 |
| 多进程内存开销 | 每用户一份 Agent，大量并发用户时内存压力大 |

---

## 本章小结

入口与配置层的复杂度分两端：

**CLI 侧**集中在 `chat()` 的对话渲染——它是事件流的**最终消费者**，要把 graph.py 产生的所有事件转成用户能看的终端 UI（自动分段 + 并发降级 + HITL 双层循环）。

**Web 侧**的核心是**多进程隔离架构**——FastAPI 主进程只做 HTTP↔IPC 转发，每用户一个 worker 子进程跑完整 Agent。这套架构解决了早期单进程的并发竞态问题，但引入了新的复杂度：管道写满的阻塞、cancel 的插队、锁获取超时的降级（空闲回收及其竞态已于 2026-08 移除）。

| 层面 | 内核 | 最值得记住的一点 |
|------|------|----------------|
| state.py | add_messages reducer | 追加语义决定了 compact 要用 RemoveMessage |
| config.py | 双访问 + lru_cache + 防坏注入 | env 只覆盖 yaml 已有键；命名空间前缀保护 |
| cli.py | 事件消费 + Live 渲染 | 自动分段 + 并发降级 + HITL 双层循环 |
| worker_manager | 常驻生命周期管理 | 无定时线程；仅登出/应用退出触发销毁 |
| worker_process | stdout 写线程 + _cancel_event | 无界队列防管道写满；软中断在 ReAct 间隙生效 |

**和前六章的呼应**：
- 第 1 章 graph.py 的 stream_invoke 是**生产者**，本章 chat()（CLI）和 `_drain_stream_events`（Web）是**消费者**——同一事件协议
- 第 1 章 HITL 的 interrupt，本章 CLI 用双层循环、Web 用 HTTP 请求边界呈现
- 第 3 章 compact_messages 的 messages_snapshot，本章两端都消费它做整体替换
- 第 3 章 token_counter/context_window/multimodal，本章 Web 的 `_op_chat` 通过 stream_invoke 参数触发它们
- 第 5 章 write_todos 的 todos，本章持久化它（schema v2）并跨轮次传递

下一章是最后一章——横切关注点（prompts/skills/health/logging_config/exceptions）+ 全书附录。

---

> **本章验收点**：① Web 多进程架构（worker_manager + worker_process）是否讲透 ② stdout 写线程防管道写满的动机是否清晰 ③ Reaper 竞态复查 + request_cancel 绕锁的设计是否到位 ④ CLI 与 Web 共用 schema v2 持久化这一关键改进是否呈现。
