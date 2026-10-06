# 第 6 章 · MCP 客户端 —— `src/mcp/`（外部工具接入）

> 承接第 1-5 章。本章覆盖 MCP（Model Context Protocol）客户端——让 Agent 能接入外部工具服务器（如 filesystem、sequential-thinking 等），是「手脚」的外延。
> MCP 是一个相对独立的子系统，和前几章的耦合点只有两个：`graph.py` 启动时 `_init_mcp` 连接、`get_all_tools()` 拉取 MCP 工具。

---

## 0. 一句话定位

`src/mcp/` 是一个 **MCP 客户端管理器**：管理所有 MCP Server 的连接生命周期，把外部 server 暴露的工具适配成 LangChain BaseTool，让 Agent 像调用内置工具一样调用它们。核心挑战是**异步 fastmcp 与同步 graph 的桥接**。

---

## 1. 模块全景

| 文件 | 角色 |
|------|------|
| `client.py` | `McpClientManager`：连接生命周期 + 异步桥接 + 运行时增删启停 |
| `adapters.py` | fastmcp Tool → LangChain BaseTool 适配 + per-server 锁 |
| `config.py` | `McpServerConfig` 数据模型 + **文件夹模式**配置加载（每 server 一个 json） |
| `__init__.py` | 对外导出（注意：其 docstring 仍写「mcp_servers.yaml」，已过期） |

**本章核心看点**：

1. 异步/同步桥接——独立事件循环线程 + `run_coroutine_threadsafe`
2. per-server 锁——防 stdio transport 的并发响应错位
3. JSON Schema → Pydantic 转换——动态生成 args_schema
4. 连接状态管理——长连接 + 资源清理（Bug 5）
5. 三种 transport（stdio/sse/streamable_http）与 Bug 8 死分支清理
6. **文件夹模式配置**——一个 server 一个 `mcp_<name>.json`，天然支持热加载/增删

---

## 2. 架构与数据流

### 2.1 MCP 工具从配置到执行的完整链路

```
启动时（graph.py HermesAgent._init_mcp）：
    mgr = get_client_manager()
    ├─ mgr.config_manager.ensure_default_file()   ← 只 mkdir 空目录，不写任何模板
    └─ mgr.connect_enabled_all()
        ├─ _ensure_config() → reload_config()
        │     └─ McpConfigManager.load() 扫描 mcp_servers/mcp_*.json
        ├─ 对每个 enabled server：connect(name)
        │     └─ loop.run_coro(_connect_async(cfg))
        │           ├─ _build_transport() 构造 stdio/sse/streamable_http transport
        │           ├─ Client(transport).__aenter__()  ← 手动进入，保持长连接
        │           ├─ list_tools() → 缓存 _raw_tools
        │           └─ 缓存 _client（长连接）到 _servers[name]
        └─ disabled 的 server 也记一条 McpConnectionState(connected=False)（供 /mcp 展示）

  get_all_tools()（tools/__init__.py）
    = BUILTIN_TOOLS + get_client_manager().get_langchain_tools()
        └─ 对每个已连接 server 的每个 raw_tool：
            └─ mcp_tool_to_langchain() → McpLangchainTool (mcp__<server>__<tool>)
                └─ _json_schema_to_pydantic() 动态生成 args_schema

运行时（LLM 调用 MCP 工具）：
  ToolRegistry.execute_tool → McpLangchainTool._run(**kwargs)
    └─ loop = _get_loop_thread(); with server_lock:  ← 串行化同一 server 的调用
          result = loop.run_coro(client.call_tool(tool_name, kwargs), timeout=60)
        └─ _format_result() 格式化 fastmcp CallToolResult

运行时变更（cli_mcp_menu）：
  set_enabled(name, bool) → 改配置 + save_single + connect/disconnect → agent.rebind_tools()
  remove_server(name)     → disconnect + del _configs + delete_single → agent.rebind_tools()
  add_server(config)      → validate + save_single + _configs[...] + connect → rebind
```

### 2.2 线程模型

```
主线程（graph 同步调用链）
    │
    │ run_coroutine_threadsafe(coro, loop)
    ▼
mcp-asyncio 线程（_AsyncLoopThread，daemon）
    └─ asyncio 事件循环跑 fastmcp 异步 API
        ├─ stdio：子进程 stdin/stdout
        ├─ sse：HTTP 长连接
        └─ streamable_http：HTTP 流
```

---

## 3. 逐文件深潜

### 3.1 `client.py` —— 异步桥接与连接管理

#### 核心设计：独立事件循环线程（`_AsyncLoopThread`）

```python
class _AsyncLoopThread:
    def _run(self):
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()

    def run_coro(self, coro, timeout=30.0):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)
```

**为什么需要这个**（见模块 docstring 的「异步同步桥接」段）：

> Hermes 的 `graph.py` 是同步调用链；fastmcp 是异步 API。如果直接在同步代码里 `asyncio.run()`，会和已有的（或潜在的）事件循环冲突。

**解法**：启动一个**独立的 daemon 线程**（`name="mcp-asyncio"`）专门跑 asyncio 事件循环。同步代码通过 `run_coroutine_threadsafe(coro, loop)` 把协程提交到那个线程的循环里执行，然后 `future.result(timeout=)` 同步等结果。`_get_loop_thread()` 是这个线程的全局单例——懒启动（首次调用才 `start()`），全进程共享一个循环。

**Windows 特殊处理**：用 `WindowsSelectorEventLoopPolicy` 而非默认的 ProactorEventLoop——后者在 pipe（子进程 stdin/stdout）关闭时有问题。stdio transport 依赖子进程，所以这个选择是必要的。

> **对比第 1 章 graph.py 的线程隔离**：graph.py 是「把同步阻塞的 stream 丢进 worker 线程」；这里是「把异步 API 丢进专门的事件循环线程」。两者都是「在同步主线程里隔离不同性质的 I/O」，但隔离对象不同（同步阻塞 vs 异步循环）。

#### 长连接 + 资源清理（Bug 5，`_connect_async`）

```python
async def _connect_async(self, cfg):
    state = McpConnectionState(config=cfg)
    client = None
    entered = False                                    # 追踪是否已 __aenter__
    try:
        transport = self._build_transport(cfg)
        client = Client(transport)
        await client.__aenter__()                      # 手动进入（保持长连接）
        entered = True
        tools = await client.list_tools()
        state._client = client                         # 缓存 client 供后续调用
        state._raw_tools = tools                       # 缓存原始工具（供 adapters 转换）
        ...
    except Exception as e:
        ...
        # 已 __aenter__ 但后续抛异常 → 必须 __aexit__ 释放资源
        if entered and client is not None:
            try:
                await client.__aexit__(None, None, None)
            except Exception as cleanup_err:
                ...
    return state
```

**两个设计要点**：

1. **手动 `__aenter__` 而非 `async with`**：因为要保持**长连接**——连接一次后，后续所有 `call_tool` 复用这个 client。`async with` 会在退出时自动关闭，不符合长连接需求。
2. **`entered` 标志位**：如果 `__aenter__` 成功但 `list_tools` 失败，client 已经占用了资源（stdio 子进程已启动）。必须 `__aexit__` 释放，否则**子进程/连接泄漏**（Bug 5 修的就是这个）。

> **通用教训**：手动管理 `__aenter__`/`__aexit__` 时，必须用标志位追踪「是否已进入」，在异常路径补 `__aexit__`。这是「不用 context manager」的代价——你接管了它原本保证的资源清理职责。

#### 三种 transport 与 Bug 8 死分支（`_build_transport`）

```python
def _build_transport(self, cfg):
    if cfg.transport == "stdio":
        # 2026-06-19: 修复 Bug 8 —— 删除 NpxStdioTransport/UvStdioTransport 死分支。
        # 原条件 `cmd in ("npx") and not cfg.args` 要求 args 为空，
        # 但标准 npx/uvx MCP server 至少需要 args=[包名]，所以分支恒不命中。
        # 通用 StdioTransport 已完整支持 command + args，无需特化。
        return StdioTransport(command=cfg.command, args=cfg.args, env=cfg.env or None)
    elif cfg.transport == "sse":
        return SSETransport(cfg.url, headers=cfg.headers or None)
    elif cfg.transport == "streamable_http":
        return StreamableHttpTransport(cfg.url, headers=cfg.headers or None)
    else:
        raise ValueError(f"不支持的传输类型: {cfg.transport}")
```

**Bug 8 的教训**：早期有个「特化分支」，想给 `npx`/`uvx` 命令用专门的 transport。但条件写错了（`not cfg.args`），而 npx MCP server 必然带 `args=[包名]`，所以这个分支**永远不命中**——是死代码。通用 `StdioTransport` 本来就能处理。

> **通用教训**：「特化分支」如果条件写错，会变成静默死代码。这类 bug 不会报错，只是功能不生效。定期审查「特殊分支是否真的被触发」是有益的。

#### 运行时增删启停：管理器对外 API

`McpClientManager` 不只管启动连接，还提供运行时变更的同步 API，供 CLI `/mcp` 菜单调用：

| 方法 | 行为 |
|------|------|
| `connect(name) -> (bool, str)` | 单个连接；**先 disconnect 旧连接再重连**（见下方说明） |
| `disconnect(name)` | 关闭长连接（`__aexit__`），清状态 |
| `connect_enabled_all() -> dict` | 批量连接 enabled server；**disabled 的也登记一条 connected=False 状态**（供 `/mcp` 列表展示） |
| `set_enabled(name, bool)` | 改 `enabled` → `save_single` 落盘 → connect/disconnect |
| `add_server(config)` | `validate()` → `save_single` → 写入 `_configs` → 若 enabled 则 connect |
| `remove_server(name)` | disconnect → `del _configs[name]` → `delete_single` 删 json |
| `list_servers()` / `get_server(name)` | 状态查询（保证所有配置项都有状态记录） |
| `reload_config(force=False)` | 重新扫描配置文件夹；扫描后 disconnect 已被移除的 server |

**`connect` 重连先断旧连接**：

```python
# 先断开旧连接（注：重连窗口期 get_langchain_tools 会短暂少此 server 的工具，
# 单线程 CLI 场景无碍，并发路径理论上有微小竞态，当前可接受）
if name in self._servers and self._servers[name].connected:
    self.disconnect(name)
```

在单线程 CLI 场景无影响（重连期间不会并发调 `get_langchain_tools`）。但如果有并发路径，重连窗口期工具列表会不一致。CLI 的 `/mcp` 操作是用户主动触发、同步执行的，所以实际无碍，但逻辑上有微小竞态窗口。

**`reload_config(force=)` 的双语义**：默认（`force=False`）有 `_initialized` 守卫，只首次加载；`force=True` 绕过守卫重新扫描文件夹——这是热加载新 server 的入口（不断开已连接的 server，只 disconnect 被删除的）。

#### 全局单例（`get_client_manager`）

```python
_client_manager: McpClientManager | None = None

def get_client_manager() -> McpClientManager:
    global _client_manager
    if _client_manager is None:
        _client_manager = McpClientManager()
    return _client_manager
```

`McpClientManager` 是全局单例。**为什么单例**：MCP 连接是昂贵的（stdio 要启子进程），必须全局共享。如果每次 new，会反复启停子进程。

#### `shutdown`：退出清理（程序退出时调用）

`shutdown()`（由 `graph.py` 的 `shutdown_mcp` 调用）：先逐个 `disconnect`，再 `_loop_thread.stop()` 并置 `None`。

**注意**：shutdown 后 `_loop_thread` 被置 None、`_servers` 被清空，**本单例不可复用**。若需重新使用 MCP，应重新 `get_client_manager()`（会新建单例）+ `connect_enabled_all()`。正常流程（程序退出时调）不会踩到此限制。

> **对比 remember 的 manager**：第 4 章 MemoryManager 也是单例（通过 holder 注入）。项目里「昂贵资源」统一用单例管理——MCP 连接、Qdrant client、LLM client 都是。

---

### 3.2 `adapters.py` —— per-server 锁（本章精华）

这是 MCP 模块最精彩的设计，解决了一个很隐蔽的并发 bug。

#### 问题：stdio transport 的并发响应错位（见模块 docstring）

> stdio transport 通过 stdin/stdout 顺序通信，多线程并发调用同一 server 的多个工具可能导致响应错位/JSON 解析错误（首次成功后续失败的现象）。

**根因**：stdio 协议是**请求-响应严格配对**的。一个 server 进程只有一个 stdin/stdout 管道。如果两个并发调用同时写 stdin、同时读 stdout，响应会错位——调用 A 拿到调用 B 的结果，解析失败。

**现象**：「首次成功后续失败」——因为第一次调用独占管道成功了，第二次并发时撞车。

#### 解法：per-server 锁（`_get_server_lock` + `_run`）

```python
_server_locks: dict[str, threading.Lock] = {}
_server_locks_guard = threading.Lock()

def _get_server_lock(server_name: str) -> threading.Lock:
    with _server_locks_guard:
        if server_name not in _server_locks:
            _server_locks[server_name] = threading.Lock()
        return _server_locks[server_name]

# 在 _run 里：
server_lock = _get_server_lock(server)
loop = _get_loop_thread()
with server_lock:                                     # 串行化同一 server 的调用
    result = loop.run_coro(_call(), timeout=60.0)
```

**为什么是 per-server 而非全局锁**：不同 server 是独立的子进程/连接，互不干扰，可以**跨 server 并发**。只有同一 server 内的调用需要串行。per-server 锁的粒度最合适——既防错位，又不过度序列化。

**双锁：同步锁 + 异步锁**：

```python
# 同步路径（_run）用 threading.Lock
_server_locks: dict[str, threading.Lock] = {}
# 异步路径（_arun）用 asyncio.Lock
_async_server_locks: dict[str, asyncio.Lock] = {}

def _get_async_server_lock(server_name: str) -> asyncio.Lock:
    with _async_server_locks_guard:
        if server_name not in _async_server_locks:
            _async_server_locks[server_name] = asyncio.Lock()
        return _async_server_locks[server_name]
```

**为什么要两套**：`_run`（同步）用 `threading.Lock`，`_arun`（异步）用 `asyncio.Lock`。`threading.Lock` 在 asyncio 里会阻塞整个事件循环，所以异步路径必须用 `asyncio.Lock`。但项目当前走的是同步 `_run`，`_arun` 是为未来准备的。

**`_arun` 当前未启用**：项目当前走同步调用链（graph.py 全同步），MCP 工具的 `_run`（同步）被调用，`_arun`（异步）**没有被调用**。为 `_arun` 准备的 `asyncio.Lock` 也空转。保留它是为 LangChain BaseTool 接口完整性 + 未来 graph 异步化时可直接复用。

> **注意 asyncio.Lock 的创建时机**（`_get_async_server_lock` docstring）：「必须惰性创建，首次调用时绑定到当前事件循环」。asyncio.Lock 绑定创建时的事件循环，如果在外部循环创建再在 mcp-asyncio 线程用，会报「different event loop」错误。所以惰性创建在 `_arun` 调用时（已在正确的循环里）。

> **对比第 2 章 tools.py 的并发**：tools.py 的 ThreadPoolExecutor 并发是「不同工具各自独立，可并发」；这里的 per-server 锁是「同一 server 的工具必须串行」。两者都是并发控制，但约束相反——tools.py 鼓励并发（不同工具），MCP 限制并发（同 server 工具）。理解 I/O 模型的差异（独立 HTTP vs 共享管道）才能选对并发策略。

#### JSON Schema → Pydantic（`_json_schema_to_pydantic`）

```python
def _json_schema_to_pydantic(schema: dict | None) -> type[BaseModel]:
    if not schema or not isinstance(schema, dict):
        return BaseModel

    properties = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    type_map = {"string": str, "integer": int, "number": float,
                "boolean": bool, "array": list, "object": dict}
    fields = {}
    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            fields[prop_name] = (Any, None)
            continue
        json_type = prop_schema.get("type", "string")
        py_type = type_map.get(json_type, Any)
        if prop_name in required:
            fields[prop_name] = (py_type, ...)           # 必填
        else:
            fields[prop_name] = (Optional[py_type], None)
    model = create_model("McpToolArgs", **fields)         # 动态生成
    model.model_config = ConfigDict(extra="allow")        # 允许额外字段
    return model
```

**动机**：MCP 工具的参数 schema 是 JSON Schema（从 server 拉取），但 LangChain BaseTool 需要 Pydantic 模型做 `args_schema`。这个函数把前者转成后者——用 `pydantic.create_model` **动态生成**模型类。

**`extra="allow"`**：允许传入 schema 未定义的额外字段。因为 MCP server 的 schema 可能不完整，宽松接收避免误拒。生成失败时 catch 异常退化为 `BaseModel`（不阻断工具注册）。

> **设计权衡**：JSON Schema 比 Pydantic 表达力弱（不支持嵌套模型、validator 等），所以转换是**有损**的——复杂 schema 会退化为 `Any`。但对 MCP 工具够用（参数多是简单类型）。

#### Pydantic v2 的 PrivateAttr 陷阱（`mcp_tool_to_langchain`）

```python
# Pydantic v2: 以 _ 开头的属性是 PrivateAttr，不能通过 __init__ 传参，
# 必须在构造后手动赋值，否则 _raw_tool 运行时为 None 导致 'NoneType' has no attribute 'name'
tool = McpLangchainTool(
    name=tool_name, description=description, args_schema=args_schema,
)
tool._mcp_client = mcp_client     # 构造后赋值
tool._raw_tool = raw_tool
tool._server_name = server_name
```

**Pydantic v2 的规则**：以下划线开头的属性是 `PrivateAttr`，`__init__` 不接受它们。如果尝试 `McpLangchainTool(_mcp_client=...)`，会报错或被忽略。必须在构造后手动赋值——这是踩过坑后留下的注释。

---

### 3.3 `config.py` —— 文件夹模式配置管理

这是与原文档差异最大的部分。**配置不是单个 yaml 文件，而是文件夹模式**：项目根下 `mcp_servers/` 目录，每个 server 一个 `mcp_<name>.json`。

> **为什么用文件夹模式而非单 yaml**：单文件（如 Claude Desktop 的嵌套 yaml）增删 server 要重写整个文件、易冲突；文件夹模式下「新建 server = 写一个 json 文件；删除 = 删文件；reload = 重扫文件夹」，**天然支持热加载，无需重启服务**。CLI `/mcp` 的 enable/disable/delete 直接操作单文件，互不干扰。

#### `get_mcp_config_path`：返回目录

```python
def get_mcp_config_path() -> Path:
    """返回 MCP 配置文件夹路径：mcp_servers/（项目根下）。"""
    raw = (settings.get("mcp_servers_file") or "").strip() if settings else ""
    if raw:
        p = Path(raw)
        p = p if p.is_absolute() else (PROJECT_ROOT / p)
        if p.is_dir():
            return p
    return PROJECT_ROOT / "mcp_servers"
```

注意这里**只有函数，没有模块级常量**。默认返回 `PROJECT_ROOT / "mcp_servers"`。若 `settings.mcp_servers_file` 指向一个目录则用之（向后兼容旧配置项），否则回退默认。

#### `McpServerConfig`：数据模型 + 容错 from_dict

```python
@dataclass
class McpServerConfig:
    name: str
    enabled: bool = True
    transport: str = "stdio"        # stdio / sse / streamable_http
    command: str = ""
    args: list[str] = ...
    env: dict[str, str] = ...
    url: str = ""
    headers: dict[str, str] = ...

    @classmethod
    def from_dict(cls, name, data):
        # 兼容 type 字段（很多 MCP 配置用 type 而非 transport）
        transport = data.get("transport") or data.get("type") or "stdio"
        # type=http → streamable_http（fastmcp 的传输名）
        if transport == "http":
            transport = "streamable_http"
        return cls(...)
```

**两处兼容性设计**：

1. **`type` 字段兼容**：很多 MCP 生态配置用 `type` 而非 `transport`（这是 Claude Desktop 格式的遗留）。`from_dict` 同时接受两者，`transport` 优先。
2. **`http` → `streamable_http` 映射**：用户写 `http`（直觉），但 fastmcp 的 transport 类名是 `streamable_http`。这里做归一化，避免用户配置出错。

**校验**（`validate`）：按 transport 类型校验必填字段——stdio 要 `command`，sse/streamable_http 要 `url`，其余报「不支持的传输类型」。`load()` 时对每个 server 调 `validate()`，无效则跳过并 warning——**不因单个坏配置阻塞全部**。

#### `McpConfigManager`：文件夹扫描 + 单文件 CRUD

| 方法 | 行为 |
|------|------|
| `_server_file(name)` | 拼路径 `mcp_<name>.json`；name 做白名单清洗（只留字母数字和 `-_`） |
| `load()` | `glob("mcp_*.json")` 扫描，逐个解析 + 校验；目录不存在返回 `{}` |
| `save_single(config)` | **原子写**：写 `.json.tmp` → `os.replace` 重命名；先 `mkdir` |
| `delete_single(name)` | 删 `mcp_<name>.json`，返回是否删除成功 |
| `ensure_default_file()` | **只 `mkdir` 空目录，不写任何模板**（见下） |
| `save(servers)` | 向后兼容的批量保存：逐个 `save_single`，并删除不在 `servers` 里的旧文件（同步） |

```python
def load(self) -> dict[str, McpServerConfig]:
    """扫描文件夹，加载所有 mcp_*.json。文件夹不存在返回 {}。"""
    if not self.config_path.exists():
        return {}
    result = {}
    for f in sorted(self.config_path.glob("mcp_*.json")):
        try:
            data = json.load(open(f, encoding="utf-8"))
            ...
            name = data.get("name") or f.stem[4:]   # 文件名 mcp_<name> 去 "mcp_" 前缀
            cfg = McpServerConfig.from_dict(name, data)
            err = cfg.validate()
            if err:
                logger.warning(...); continue        # 单个坏配置不阻塞
            result[name] = cfg
        except Exception as e:
            logger.warning(f"解析 MCP 配置 {f.name} 失败: {e}")
    return result
```

**`save_single` 的原子写**（临时文件 + rename）：避免写到一半崩溃留下半截 json 导致 `load()` 解析失败。`os.replace` 在同文件系统下是原子的。

**`ensure_default_file` 不写模板**（重要修正）：

```python
def ensure_default_file(self) -> None:
    """确保配置目录存在（创建空目录）。"""
    self.config_path.mkdir(parents=True, exist_ok=True)
```

graph.py 的 `_init_mcp` 启动时调 `ensure_default_file()`——**只确保 `mcp_servers/` 目录存在，不写任何模板文件**。「零配置开箱可用」的体现是：目录不存在就建一个空的，`load()` 扫到零个 json → 零个 MCP server → `get_all_tools()` 退化为纯内置工具，零行为变化。用户想加 server，自己往目录里放一个 `mcp_<name>.json` 即可，或通过 CLI 添加。

> **`save` 的孤儿清理**：批量 `save(servers)` 不只写入传入的 server，还会**删除目录里 `servers` 没有的旧 json 文件**（基于 `glob` 出的现有文件名与传入 keys 的差集）。这让 `save` 的语义是「把文件夹同步成这个集合」，而非「追加」。调用方需注意这个破坏性语义。

#### 与 client.py 的协作

`McpClientManager.__init__` 里 `self.config_manager = McpConfigManager(get_mcp_config_path())`。运行时变更时：

- `set_enabled` / `add_server` → `config_manager.save_single(...)` 落盘
- `remove_server` → `config_manager.delete_single(name)` 删盘
- `reload_config` → `config_manager.load()` 重扫

所以「内存 `_configs` + 磁盘 json」是双写一致的：变更操作既改内存又落盘，重启后状态可恢复。

---

## 4. 设计权衡总结

### 4.1 优点

| 设计 | 价值 |
|------|------|
| 独立事件循环线程 | 干净桥接异步 fastmcp 与同步 graph，避免循环冲突 |
| Windows SelectorEventLoop | 解决 stdio pipe 关闭问题 |
| 长连接（手动 `__aenter__`） | 避免反复启停子进程 |
| `entered` 标志位 | 异常路径正确释放资源（Bug 5） |
| per-server 锁 | 精准串行化同 server 调用，不阻塞跨 server 并发 |
| 双锁（同步+异步） | 为未来异步路径预留，不阻塞当前同步 |
| 动态 Pydantic 模型 | 适配任意 MCP 工具的 schema |
| 配置校验逐条 | 单个坏配置不阻塞全部 |
| 文件夹模式配置 | 天然热加载，增删 server 互不干扰，原子写防半截文件 |

### 4.2 技术债 / 待优化

| 项 | 说明 |
|----|------|
| `_arun` 异步路径当前未用 | 双锁的第二把（asyncio.Lock）暂时空转 |
| JSON Schema 转 Pydantic 有损 | 复杂 schema 退化为 Any |
| `connect` 重连先断旧连接 | 重连窗口期工具列表短暂不一致（单线程场景无碍） |
| `shutdown` 后单例不可复用 | 需重新 `get_client_manager()` + `connect_enabled_all()` |
| `__init__.py` docstring 过期 | 仍写「配置文件: mcp_servers.yaml」，实际是文件夹模式 |

---

## 本章小结

MCP 客户端的核心挑战是 **「异步外部库 + 同步主架构」的桥接**，衍生出三条设计线：

| 设计线 | 解决什么 |
|--------|---------|
| 独立事件循环线程 | 异步 fastmcp 嵌入同步 graph，不冲突 |
| per-server 锁 | stdio transport 的共享管道并发安全 |
| 动态适配（schema→Pydantic）+ 文件夹配置 | 任意 MCP 工具自动转成 LangChain 工具，且可热增删 |

**和前五章的呼应**：
- 第 1 章 graph.py 的 `_init_mcp` / `rebind_tools` 是 MCP 的**消费方**——启动连接、`/mcp` 变更后热刷新工具
- `tools/__init__.py` 的 `get_all_tools()` 把 MCP 工具和内置工具**合并**进 Agent 的工具列表
- 第 2 章 tools.py 的并发执行对 MCP 工具同样适用，但 per-server 锁在更底层加了第二道串行约束

MCP 模块相对独立，和 Agent 核心的耦合点很少（只有 `get_all_tools` 和生命周期管理）。这是一个**边界清晰的子系统**——它的复杂度内聚在自身（异步桥接 + 并发控制 + 文件夹配置），不向外扩散。

下一章进入「入口与配置」——`cli.py`、`web.py`、根 `config.py`、`state.py`。这是 Agent 与用户/外部世界的界面层。
