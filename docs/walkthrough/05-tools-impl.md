# 第 5 章 · 工具实现层 —— `src/tools/`（16 个内置工具）

> 承接第 1-4 章。本章覆盖 Agent 的「手脚」——所有具体工具的实现。
> 第 2 章讲了工具怎么被调度（ToolRegistry），本章讲每个工具本身怎么写。
> 其中 virtual_fs / run_shell / web_fetch 三个是安全与健壮性的重头戏，remember / sub_agent / human_approval 已在前几章铺垫，这里收口。
>
> **2026-09 更新（文件工具退役）**：7 个文件工具已删除，统一走 `bash` + 按需技能 `file-ops`。`run_shell.py` / `executors/fs.py` 已不存在，shell 执行在 `executors/shell.py`，分类仍在 `shell_safety.py`。下文是 V2 走读，路径与工具名以当前代码为准。MCP 可在 chat 内 `create_mcp` 接入。

---

## 0. 一句话定位

`src/tools/` 是 16 个 `@tool` 装饰的 LangChain 工具，给 Agent 提供文件操作、网络访问、子智能体、记忆、shell 执行、技能加载、人工审批等能力。每个工具都是**无状态函数**（除 remember/sub_agent 有模块级 holder），通过 `BUILTIN_TOOLS` 列表统一注册。（V3 已改为 YAML `ToolSpec` + 三层权限 + 4 执行器，本章走读的是 V2 的 `@tool` 实现细节。）

---

## 1. 模块全景

| 文件 | 行数 | 工具名 | 类别 | 核心看点 |
|------|------|--------|------|---------|
| `__init__.py` | 74 | — | 注册入口 | BUILTIN_TOOLS + get_all_tools（内置+MCP） |
| `virtual_fs.py` | 576 | ls/read/write/edit/copy/move/delete (7个) | 文件系统 | per-user 分层 + 路径穿越防护（四层校验） |
| `sub_agent.py` | 430 | task | 子智能体 | StreamingSubAgent 流式隔离 + 深度控制（contextvars） |
| `web_fetch.py` | 328 | web_fetch | 网络 | SSRF 防护 + 四级降级链 + SPA 壳陷阱 |
| `run_shell.py` | 203 | run_shell | Shell | 纵深防御六层 + per-user cwd + HITL interrupt |
| `shell_safety.py` | 221 | — (纯函数) | Shell 分类 | 白名单/黑名单/子命令拆分 |
| `web_search.py` | 137 | web_search | 网络 | 必应直连 + HTML 解析 |
| `compact.py` | 120 | compact_conversation + generate_summary | 上下文 | 「信号工具」模式 |
| `remember.py` | 112 | remember | 记忆 | contextvars 隔离 user_id |
| `use_skill.py` | 72 | use_skill | 技能 | 按需加载 + 资源清单 |
| `write_todos.py` | 62 | write_todos | 任务规划 | 全量替换 + 状态约束 |
| `human_approval.py` | 52 | request_human_approval | HITL | interrupt() 暂停图 |

> 全部 12 个文件共约 2387 行（截至 2026-07-03）。

**本章核心看点**（按重要性）：

1. virtual_fs 的 per-user 分层 + 路径穿越防护——四层校验 + 错误信息可读化
2. run_shell + shell_safety 的纵深防御安全模型 + per-user cwd 对齐
3. web_fetch 的 SSRF 防护（入口校验 + 每跳重定向校验）+ 四级降级链
4. 「信号工具」模式（compact_conversation 不干活，只发信号）
5. interrupt() 工具的统一范式（human_approval + run_shell）

---

## 2. 架构与数据流

### 2.1 工具注册与加载

```
src/tools/__init__.py
  ├─ BUILTIN_TOOLS = [write_todos, ls, read_file, ..., remember, run_shell]  (16 个)
  ├─ ALL_TOOLS = BUILTIN_TOOLS  (向后兼容别名)
  ├─ get_builtin_tools() → 纯内置
  └─ get_all_tools() → 内置 + MCP 工具（mcp__<server>__<tool>）
        └─ graph.py 的 HermesAgent.__init__ 和 rebind_tools 调用此函数
```

16 个内置工具的构成：7 个文件系统（ls/read_file/write_file/edit_file/copy_file/move_file/delete_file）+ task + compact_conversation + request_human_approval + web_fetch + web_search + use_skill + remember + run_shell。> 注：原第 17 个 `dispatch`（来自 `src/thinktank/dispatch.py`）随总参 / 智囊团运行模式废弃已移除（底层编排保留为 WakerFlow 节点复用）。`get_all_tools()` 在内置基础上叠加已连接的 MCP 工具；无 MCP 配置时零行为变化。

### 2.2 工具的安全分级

```
┌─────────────────────────────────────────────────────┐
│ 自动执行（无审批）                                    │
│   ls/read_file/write_file/edit_file/copy/move/delete │
│   web_search/web_fetch/compact/use_skill/write_todos │
│   remember/task                                       │
├─────────────────────────────────────────────────────┤
│ HITL 审批（interrupt 暂停图）                         │
│   request_human_approval（LLM 主动调用）              │
│   run_shell（非白名单命令时）                         │
└─────────────────────────────────────────────────────┘
```

---

## 3. 逐文件深潜

### 3.1 `virtual_fs.py` —— per-user 分层 + 路径穿越防护

这是安全设计的典范，也是多用户隔离的物理基础。文件系统工具有真实/虚拟双模式，真实模式下既要防止 LLM 越界读写，又要保证不同用户互不可见。

#### per-user 分层（`_get_workspace_root`）

这是 M1-1 阶段引入的多用户物理隔离机制。真实文件模式下，根目录按当前 user_id 分层：

```python
def _get_workspace_root() -> Path | None:
    settings = get_settings()
    root = settings.workspace_root
    if not (root and root.strip()):
        return None              # 空 → 虚拟 FS 模式
    base = Path(root.strip())
    from src.tools.remember import get_current_user_id
    uid = get_current_user_id()
    if not uid:
        return base              # 无 user_id（CLI 未登录）→ 全局根（向后兼容）
    user_root = base / "users" / uid
    user_root.mkdir(parents=True, exist_ok=True)   # 首次访问自动建目录
    return user_root
```

**为什么这么写**：
- **物理隔离优于逻辑隔离**：每个用户拥有独立的 `<workspace_root>/users/<uid>/` 子目录，用户 A 的 LLM 无论怎样调 ls/read/write，路径校验的锚点都被钉死在 A 自己的目录上，根本「看不到」用户 B 的文件。这比「共享目录 + 每条路径加前缀」更不易出错。
- **user_id 复用 remember 的 contextvar**：这里直接 `from src.tools.remember import get_current_user_id` 读 contextvar。这样 user_id 在整个请求生命周期内只有一个真相源，且天然随第 2 章 `copy_context()` 传播到 ThreadPoolExecutor worker。
- **向后兼容**：CLI 场景通常没有 user_id（未登录），回退全局根，老行为零变化。

> **跨机制对齐**：per-user 分层不止在 virtual_fs，`run_shell` 的 cwd 也做了同样的分层（见 3.2）。否则会出现「文件工具在用户目录写、shell 却在全局根跑」的隔离破功。这是为什么 `_get_workspace_root` 在两个文件里各写一份——它们必须同构。

#### 四层路径校验（`_resolve_real_path`）

```python
def _resolve_real_path(path: str) -> Path | None:
    workspace = _get_workspace_root()
    if workspace is None:
        return None
    # 校验 1：workspace 是否真实存在（防静默失败）
    if not workspace.exists():
        logger.error(f"workspace_root 不存在: {workspace}")
        return None
    # 校验 2：显式拒绝含盘符的系统绝对路径（Windows D:/ C:\）
    if re.match(r"^([A-Za-z]:[\\/]|\\\\)", path):
        logger.warning(f"拒绝系统绝对路径: {path}")
        return None
    # 校验 3：按路径组件检查 ".."
    if ".." in path.split("/"):
        logger.error(f"路径包含 '..' 路径组件，已拒绝: {path}")
        return None
    # 校验 4：resolve 后用 relative_to 确认在 workspace 内
    real_path = (workspace / relative).resolve()
    workspace_resolved = workspace.resolve()
    try:
        real_path.relative_to(workspace_resolved)
    except ValueError:
        return None
    return real_path
```

**为什么需要四层**（每层防不同攻击/误用）：

| 校验 | 防什么 | 动机 |
|------|--------|------|
| workspace.exists() | 配置坏了 | 避免后续所有操作静默失败（笼统报错） |
| 盘符拒绝 | Windows 绝对路径 | `Path(workspace / 'D:/foo')` 会丢弃 workspace，解析到越界 |
| `..` 组件检查 | 目录穿越 | `../../etc/passwd` 这类经典攻击 |
| relative_to 终检 | 符号链接等绕过 | resolve() 展开符号链接后的最终确认 |

> **关键细节**（校验 3）：用 `path.split("/")` 检查组件级 `..`，而非 `".." in path`。因为后者会误杀含 `..` 的合法文件名（如 `data..bak`）。**精确匹配路径组件**是正确做法。

#### 错误信息可读化（`_format_path_error`）

早期所有路径错误都返回笼统的「路径不安全或无效」，LLM 看不到真实原因，只能不停换路径重试。改进后**按根因分类报错**：

```python
def _format_path_error(path: str) -> str:
    # 最优先：workspace 本身是否健康（这是"所有路径都失败"的唯一原因）
    workspace = _get_workspace_root()
    if workspace is not None and not workspace.exists():
        raw = get_settings().workspace_root
        hint = ""
        # 检测最常见的误配置：YAML 里误写 Python 风格的 r"..." 前缀
        if isinstance(raw, str) and (raw.startswith("r\"") or raw.startswith("r'")):
            hint = "config.yaml 的 workspace_root 疑似误用了 Python 字符串前缀..."
        return f"错误：workspace 根目录不存在: {workspace}\n（配置值 workspace_root={raw!r}）{hint}"
    # 盘符路径 / 穿越路径 / 越界
    ...
```

**最精彩的一处**：检测 `workspace_root` 值是否以 `r"` 开头——这是用户在 YAML 里误写 Python 风格的 `r"..."` 原始字符串前缀，YAML 不认这个前缀，会把它当路径字面量（解析出一个带字面 `r`、`"` 字符的、根本不存在的目录）。这种配置错误极难排查，但这里**精准识别并给出修复建议**。

> **设计哲学**：错误信息是给 LLM 看的（让它能自我纠正），不是给终端用户看的。所以错误信息必须**根因明确、含修复提示**。笼统的「失败了」会让 LLM 陷入无效重试循环。

#### 「目录不存在」时引导回退（`ls` 工具内）

```python
if not real_path.exists():
    if path != "/":
        return (
            f"错误：目录 {path} 不存在\n"
            f"提示：不要继续猜测路径名。请用 `ls /` 查看实际可用的目录后再操作。"
        )
```

**动机**：LLM 有个坏习惯——路径不存在时，它会**猜测**各种相似路径反复重试（浪费 token 和轮次）。这里明确告诉它「别猜，回去 ls /」。根目录永远存在（=workspace 本身），所以只对非根路径加提示。这种「行为引导」比单纯报错更有效。

#### contextvar 隔离虚拟 FS（`_current_vfs`）

虚拟模式下，原本用模块级 `_virtual_fs: dict[str, str]` 这个全局 dict。Web 多用户场景下这会串数据，于是引入了 contextvar：

```python
_current_vfs: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "hermes_vfs", default=None,
)

def get_virtual_fs() -> dict[str, str]:
    v = _current_vfs.get()
    return v if v is not None else _virtual_fs
```

- **Web**：每请求 `set_current_vfs(session.vfs)` 注入 per-user dict → 走实例 dict（隔离）。
- **CLI**：不调 set → 回退模块全局 `_virtual_fs`（行为零变化）。

这与 remember 的 user_id contextvar 同一范式。

#### 双模式的实现模式

每个工具都是 `if workspace is not None: 真实模式 else: 虚拟模式` 的结构。虚拟模式用 `get_virtual_fs()` 拿到 per-user 或全局 dict，是个纯内存的 path→content 映射。

> **技术债**：双模式让每个工具函数都有两套逻辑分支，代码翻倍。虚拟模式现在可能用得少了（项目默认配真实 workspace），但为了兼容保留。这是「渐进演进」的代价。

---

### 3.2 `run_shell.py` + `shell_safety.py` —— 纵深防御安全模型

这是项目里**安全设计最严密**的工具。`run_shell.py` 模块文档列了六层防御：

```
1. 总开关 shell_enabled 默认关闭（opt-in）
2. 工作目录锁定到 workspace_root（per-user 分层，空/不存在则拒绝）
3. 命令分类：白名单内自动执行；其余 HITL 审批
4. 危险模式黑名单即使主程序在白名单也拦截
5. 硬超时 + 进程组 kill，防阻塞/死循环进程
6. 输出截断（防上下文爆炸 + prompt-injection）
```

#### per-user cwd 分层（`_get_workspace_root`）

shell 的工作目录必须与 virtual_fs 的分层**对齐**，否则隔离破功：

```python
def _get_workspace_root() -> str:
    settings = get_settings()
    root = getattr(settings, "workspace_root", "") or ""
    if not root.strip():
        return ""
    from src.tools.remember import get_current_user_id
    uid = get_current_user_id()
    base = root.strip().replace("\\", "/")
    if not uid:
        return base                      # 无 user_id → 全局根
    user_root = f"{base}/users/{uid}"    # per-user 分层
    return user_root
```

**为什么必须和 virtual_fs 同构**：如果文件工具写文件到 `/users/A/`，而 `cd` + `ls` 却在全局根执行，用户 A 通过 shell 就能看到所有人的文件——per-user 文件隔离就形同虚设。两处都读同一个 `get_current_user_id()`，保证锚点一致。

> 注意 virtual_fs 用 `Path`，这里用字符串拼接（`base/users/{uid}`）——因为 shell 的 cwd 最终要传给 `subprocess.run(cwd=...)`，字符串更直接。两份实现各取所需，但**分层规则一致**。

#### 安全声明：纵深防御 ≠ 完备沙箱

`run_shell.py` 与 `shell_safety.py` 的模块文档都明确写了这段声明（完整引用 `run_shell.py` 顶部）：

> 「这是"纵深防御"而非"完备沙箱"。shell=True 带注入风险；白名单程序仍可能被组合出危险操作（典型：python 在白名单时 `python -c "import os; os.system('任意')"` 可绕过——故默认白名单不含解释器）。真正的隔离边界应是容器/VM，本工具不提供。」

**为什么必须写明**：避免任何读者（或未来的贡献者）误以为这个工具有真正的隔离保证。`shell=True` 意味着命令字符串交给 shell 解析，本身就是注入风险面。白名单只是「提高门槛」，不是「绝对安全」。HITL 是最终的人类把关。

> **通用教训**：安全工具必须**显式声明其安全边界**。不说清楚，就会有人把它当沙箱用，出了事才发现根本没隔离。这种「诚实的局限性声明」比夸大的安全承诺有价值得多。

#### shell_safety 的分类逻辑（纯函数）

```python
def classify_command(command, allowed=None, blocked_patterns=None) -> tuple[str, str]:
    # 1. 黑名单优先（即使主程序在白名单也拦）
    hit = _matches_blocked(command_lowered, blocked_patterns)
    if hit:
        return ("review", f"命中危险模式黑名单: {hit}")
    # 2. 拆子命令，逐个检查首 token 是否在白名单
    subcmds = _split_subcommands(command)
    for sc in subcmds:
        ft = _first_token(sc)
        if ft not in allowed:
            non_allowed.append(ft)
    if non_allowed:
        return ("review", f"包含非白名单程序: {', '.join(non_allowed)}")
    return ("auto", "白名单内命令")
```

**设计要点**：

1. **黑名单优先于白名单**：`rm -rf /` 即使 `rm` 在白名单也拦（实际 `rm` 不在默认白名单，但即便在也会被黑名单的「`rm -rf /` 模式」拦）。因为黑名单匹配的是「危险模式」（参数组合），不是程序名。
2. **拆子命令逐个检查**（`_split_subcommands`）：`ls && rm -rf /` 这种组合，两个子命令都要在白名单。用 `shlex(punctuation_chars=';&|')` 解析，正确处理引号内的分隔符（`echo "a && b"` 不被拆开），且把 `log;`（无空格）也识别为独立分隔符。
3. **首 token 归一化**（`_first_token`）：`/usr/bin/git` → `git`，`CC=gcc gcc` → `gcc`（跳过环境变量赋值），`git.exe` → `git`（去 Windows 后缀），`$(...)` / `` `...` `` / `(...)` 开头保守返回原串（几乎必然不在白名单 → review）。
4. **纯函数 + 无 I/O**：刻意保持纯函数，便于单测覆盖。测试不需要 mock 任何外部依赖。

> **对比 virtual_fs**：virtual_fs 的防护是「阻止越界访问」（路径层面），shell_safety 的防护是「阻止危险执行」（命令层面）。两者都是「纵深防御」，但防护对象不同。

#### 默认白名单的设计（`DEFAULT_ALLOWED_COMMANDS`）

默认白名单是**只读/查询类**命令（ls/cat/grep/find/git/wc/du 等），**刻意不含解释器**（python/node/ruby）。因为解释器可执行任意代码——`python -c "import os; os.system('rm -rf /')"` 能绕过一切白名单检查。这是「纵深防御 ≠ 完备沙箱」声明里点名的典型绕过。

#### 默认黑名单的设计（`DEFAULT_BLOCKED_PATTERNS`）

```python
DEFAULT_BLOCKED_PATTERNS = (
    r"\brm\s+(-\w*)?rf?\s+/(?:\s|$)",        # rm -rf /
    r"\brm\s+(-\w*)?rf?\s+[A-Za-z]:[\\/]",   # rm -rf C:\
    r"\bmkfs\b",                              # 格式化
    r"\bdd\b.*\bof\s*=\s*/dev/",             # dd 写裸设备
    r":\(\)\s*\{\s*:\|:\&\s*\}\s*;\s*:",     # fork 炸弹 :(){:|:&};:
    ...
    r"curl\b.*\|\s*(sh|bash|zsh|python)\b",  # 管道执行远程脚本
    r"\bfind\b.*\s+-exec\b",                  # 白名单程序的危险参数形态
    r"\bawk\b.*\bsystem\s*\(",                # awk system() 任意命令执行
    r"\bsed\b.*\s+-i\b",                      # sed -i 原地改写
    r"\bredis\b|\bmysql\b|\bpsql\b|\bmongo\b",# 数据库客户端
)
```

每条都是正则，覆盖一类危险模式。三个层次尤其值得注意：
- **破坏性系统操作**（rm -rf /、mkfs、dd 写裸设备、fork 炸弹）
- **远程代码执行**（`curl|sh` 这类经典 RCE 向量）
- **白名单程序的危险参数形态**（`find -exec`、`awk system()`、`sed -i`——程序本身在白名单，但参数可执行任意代码/改文件，必须拦）

#### HITL interrupt 的实现

```python
if decision != "auto":
    user_response = interrupt({
        "action": "执行 shell 命令",
        "details": f"$ {command}\n分类原因: {reason}",
    })
    if not (isinstance(user_response, str) and user_response.strip().lower() == "approve"):
        return f"命令未获批准：{reason_back}"
```

非白名单命令 → `interrupt()` 暂停图 → CLI/Web 弹窗 → 用户回复。**严格 "approve" 才放行**，其余一律拒绝并回传原因。这就是 run_shell 被加入 `_INTERRUPT_TOOL_NAMES`（第 2 章）的原因。

> **与 human_approval 的统一范式**：两者都用 `interrupt(dict)` + 等 `Command(resume=...)`。差别是 human_approval 是通用审批（LLM 自主决定何时用），run_shell 是特定审批（非白名单命令强制触发）。第 2 章讲的「interrupt 工具强制串行」对两者都适用。

---

### 3.3 `web_fetch.py` —— SSRF 防护 + 四级降级链

这是项目里**降级设计**最精彩、也是 git log 里「SSRF 防护」核心所在的一个工具。它要同时应对两类问题：① LLM 被诱导访问内网/元数据端点（SSRF）；② trafilatura 在 JS 渲染页/SPA 上常返回空（信息丢失）。

#### SSRF 防护（`_assert_safe_url` + 手动重定向每跳校验）

SSRF（Server-Side Request Forgery）的核心威胁：LLM 可能被 prompt-injection 诱导去访问 `http://169.254.169.254/`（云元数据，泄露 AWS/GCP 凭证）、`http://127.0.0.1:port/`（本机服务）、内网 IP 段。这些请求是从**服务器侧**发出的，能访问到外部访问不到的资源。

**第一道：入口 URL 校验**（`_assert_safe_url`）

```python
def _assert_safe_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"仅允许 http/https（得到 {parsed.scheme}）"
    host = parsed.hostname
    infos = socket.getaddrinfo(host, None)   # 解析所有 A/AAAA 记录
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _is_private_ip(ip):              # 私网/环回/链路本地/保留/多播/未指定
            return f"目标主机解析到内网/保留地址 {ip}（已拒绝）"
    return None
```

`_is_private_ip` 检查 6 类禁止地址：`is_private` / `is_loopback` / `is_link_local` / `is_reserved` / `is_multicast` / `is_unspecified`。关键是**遍历 host 的所有 DNS 记录**——一个 host 可能同时解析到公网和内网 IP，任一落进私网就拒绝（防 DNS rebinding）。

**第二道：每跳重定向重新校验**

这是最容易被忽略的一环。httpx 的 `follow_redirects=True` 会自动跟随重定向，但**不校验重定向目标**——攻击者可以让入口 URL 指向正常站点，该站点再 302 跳转到 `169.254.169.254`。所以 web_fetch **关闭自动跟随，手动处理重定向**：

```python
with httpx.Client(follow_redirects=False, ...) as client:
    resp = client.get(url)
    hops = 0
    while resp.is_redirect and hops < _MAX_REDIRECTS:   # 最多 5 跳
        loc = resp.headers.get("location", "")
        next_url = str(httpx.URL(url).join(loc))
        err = _assert_safe_url(next_url)                # 每跳重新 SSRF 校验！
        if err:
            return f"错误：重定向目标安全校验失败 - {err}"
        url = next_url
        resp = client.get(url)
        hops += 1
```

**为什么这么写**：HTTP 重定向是 SSRF 攻击的经典载体。只校验入口 URL 是不够的——必须对每一跳的目标重新做 DNS 解析 + 私网检查。`_MAX_REDIRECTS = 5` 防无限重定向循环。

> **这是项目里安全意识最强的一处**。很多 web 抓取实现只校验入口 URL（甚至完全不校验），重定向链是敞开的。web_fetch 的双道校验（入口 + 每跳）把 SSRF 面收窄到了合理的程度。

#### trafilatura 保护式 import

```python
try:
    import trafilatura
except ImportError:
    trafilatura = None
```

trafilatura 是重型依赖（含二进制 lxml），顶层硬 import 会让整个 agent 在缺包/装残时无法构造（曾导致登录 500：`tools/__init__` 无条件加载本模块）。保护式 import 让包缺失时自动降级到 Tier3 bs4——下面的 Tier1/Tier2 的 try/except 能捕获 `NoneType` 调用并 warning 降级。

#### 四级降级链（`_extract_with_fallback`）

| Tier | 方法 | 拿什么 | 适用场景 |
|------|------|--------|---------|
| 1 | `trafilatura.extract(favor_recall=True)` | 主力正文（更激进） | 正常文章页 |
| 2 | `trafilatura.bare_extraction()` | Document 对象（含 title/desc） | Tier1 失败但元数据可用 |
| 3 | `BeautifulSoup` | title + meta + noscript + 可见文本 | SPA 壳（Twitter/X 的 t.co） |
| 4 | 错误信息（带 len(html)） | 诊断辅助 | 全部失败 |

**每级失败才进下一级，任一级产出非空即返回**（仍走统一截断 `_truncate`）。函数为纯函数（入参 html 字符串 + max_chars），便于不联网单测。

#### SPA 壳陷阱（`_THIN_BODY_THRESHOLD = 80`）

```python
# SPA 壳陷阱：trafilatura 在 favor_recall 下会把 <noscript> 警告
# 当正文返回（如 Twitter/X 的"需要启用 JavaScript"）。此时若 bs4
# 元数据（og:title/og:description）更长更具体，优先用元数据。
if len(text) < _THIN_BODY_THRESHOLD and len(bs4_meta) > len(text):
    logger.info(f"web_fetch Tier1 正文过短({len(text)}字符)，改用元数据兜底")
    return _truncate(bs4_meta, max_chars)
```

trafilatura 在 `favor_recall` 模式下会把 `<noscript>` 里的「请启用 JavaScript」警告当正文返回。判据：抽出的正文短于 80 字符且 bs4 元数据更长 → 判定为「抓到了 noscript 壳而非真正正文」，改用元数据。这是对 Twitter/X 这类 SPA 的精准应对。

> **设计哲学**：网页抓取没有「银弹」。不同站点结构差异巨大，单一提取器必然在某些站点失败。**降级链**比「单一提取器 + 报错」健壮得多——总能榨出点信息。Tier 4 连失败都带上 `len(html)` 辅助诊断，不笼统报错。

#### 代理支持的恢复

```python
# httpx ≥0.28 已移除 proxies=（复数），必须用 proxy=（单数），
# 且空值必须传 None 而非 ""（否则 httpx 把 "" 当无效 URL）。
proxy = (getattr(settings, "web_proxy", "") or "").strip() or None
```

注意 httpx 0.28 的 API 变更：`proxies=`（复数）被移除，改用 `proxy=`（单数）。且空字符串必须转成 `None`。这种**库版本兼容**细节如果不注释，升级时会踩坑。

---

### 3.4 `web_search.py` —— 必应直连（无 API Key）

设计很务实（模块文档）：用必应网页版（`cn.bing.com`），国内可直连、完全免费、无需 API Key。通过 httpx + BeautifulSoup 解析经典版搜索结果的 `li.b_algo` 元素。

几个值得注意的点：
- **web_proxy 支持**：与 web_fetch 同款逻辑，空=直连，非空走代理（`proxy = (... or "").strip() or None`）。
- **timeout 读 `web_search_timeout` 配置**（不再硬编码），与 web_fetch 风格统一。
- **诊断日志**：抓取流程里有 4 处 `logger.debug` 打点，精确定位 httpx 各阶段（Client 构造 / get / 解码 text）的耗时。用 debug 级，正常搜索不刷屏，排查「卡死」时可开 DEBUG 复现。解析逻辑提取每个 `li.b_algo` 里的 `h2 > a`（标题+URL）和 `.b_caption > p`（摘要，超 200 字截断）。

> 这几处打点用 `logger.debug`（消息文本无特殊标记前缀），正常搜索不刷屏，排查「卡死」时开 DEBUG 即可复现——属正常的 debug 级诊断，不必清理。

---

### 3.5 「信号工具」模式 —— `compact_conversation`

`compact_conversation` 是个特殊的工具：**它不执行任何实际操作，只返回信号字符串**：

```python
@tool
def compact_conversation() -> str:
    """...此工具仅发出压缩请求信号，实际压缩由 ContextManager 完成。"""
    logger.debug("compact_conversation 被调用（发出压缩请求信号）")
    return "COMPACT_REQUESTED"
```

然后 graph.py 的 `_after_tools_router` 把这个信号转成 `compact_requested=True` 的 state 更新，据此路由到 compact 节点，由 `ContextManager.compact_messages()` 执行实际压缩。`compact.py` 模块文档已正确写明这条触发路径（`_after_tools_router` 检测信号 → compact 节点 → ContextManager）。

**为什么这么设计**（模块文档的「关键设计决策」）：
- 工具本身不执行压缩，只作为 LLM 的「信号」
- 实际压缩逻辑统一在 `ContextManager.compact_messages()`
- 这样压缩逻辑只有一份，工具和 CLI（`/compact` 命令）都调同一个

> **设计模式**：这是「**信号工具**」模式——工具的返回值不是结果，而是控制信号。LLM 通过调用工具表达「我想压缩」，但真正的执行延迟到图的下一个节点。类似的设计在 LangGraph 生态里很常见（工具作为路由触发器）。

`generate_summary` 是底层函数，被 ContextManager 调用。它**每次都新建 ChatOpenAI 实例**，不复用 graph.py 的 self.llm——因为压缩用的参数不同（`temperature=0.1`，`max_tokens=compact_summary_max_tokens`，且关闭 Qwen3 思考模式）。每次 new 有开销，但压缩不频繁，可接受。

---

### 3.6 `remember.py` —— contextvars 隔离（收口）

第 1-4 章已多次提及，这里收口。核心机制：

```python
_current_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(...)
_manager_holder: dict = {}                          # 单例 holder

def set_memory_manager(manager): _manager_holder["manager"] = manager
def set_current_user_id(user_id): return _current_user_id.set(user_id)

@tool
def remember(content: str) -> str:
    manager = _get_manager()                         # 全局单例
    user_id = _get_current_user_id()                 # 每请求隔离
    result = manager.remember_fact(user_id, content, ...)
```

**两种依赖注入策略**（第 4 章已详述）：
- manager：全局单例，用 holder dict 注入（无竞态——Qdrant HTTP 调用无共享可变状态）
- user_id：每请求隔离，用 contextvars（防并发串数据）

> **不止 remember 用**：`get_current_user_id()` 被提为**公开接口**，供 virtual_fs 和 run_shell 做 per-user workspace 分层使用（M1-1）。它是「当前用户是谁」的跨模块唯一真相源。

> **联系第 2 章**：contextvars 的值之所以能在 ThreadPoolExecutor worker 里正确传播，正是因为第 2 章的 `copy_context()` 修复。remember 工具（以及依赖它的 virtual_fs/run_shell 分层）在并发路径里执行时能拿到正确的 user_id，全靠那个修复。

---

### 3.7 `sub_agent.py` —— 流式子智能体（收口）

第 2 章讲了 task 工具的调度（双路径 token_sink），这里补 StreamingSubAgent 本身的设计。

#### 流式 + 超时控制（`stream`）

```python
def stream(self, instruction: str) -> Generator[str, None, None]:
    timer = None
    if self._timeout and self._timeout > 0:
        def on_timeout():
            self._timed_out = True
            self.cancel()
        timer = threading.Timer(self._timeout, on_timeout)
        timer.daemon = True
        timer.start()
    try:
        ...
        yield from self._stream_with_tools(messages) if self.llm_with_tools else self._stream_plain(messages)
        if self._timed_out:
            yield f"\n\n[子智能体执行超过 {self._timeout} 秒被终止...]"
    finally:
        if timer: timer.cancel()
```

**超时机制**：Timer 触发时设 `_timed_out=True` + `cancel()`，流式循环里检查这俩 flag 中断。这是**协作式取消**（cooperative cancellation）——不能强杀线程，靠 flag 让循环自己退出。超时控制下沉在 `stream()` 里，`run()` 只消费输出并检查 `_timed_out` 标志，保证两条消费路径（生成器直驱 / 回调）都受超时保护。

**带工具的流式 agentic loop**（`_stream_with_tools`）：用 `AIMessageChunk` 累积整个响应（同 graph.py 的做法），让 LangChain 自动按 `index` 合并 `tool_call_chunks`。流结束后从 `accumulated.tool_calls` 拿完整调用列表，执行后把 `ToolMessage` 回传 LLM，最多迭代 10 轮。这个累积写法修复了早期「把每个带 name 的 chunk 当独立调用」导致多轮工具参数解析失败的 bug。

> **对比第 1 章**：graph.py 的超时是「主线程放弃 worker 线程」（daemon 线程随进程清理）；子智能体的超时是「协作式 flag 取消」。两种超时策略，前者破 native 阻塞，后者适用于可控的 Python 循环。

#### 深度控制用 contextvars（`_current_depth`）

```python
# 子智能体嵌套深度：用 contextvars 而非线程本地存储。
_current_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "hermes_subagent_depth", default=0
)
```

深度控制**已经改用 contextvars**，与项目其它隔离（remember 的 user_id）范式统一。`task` 工具进深度检查 + `_set_current_depth(+1)` / finally 里 `-1`：

```python
@tool
def task(...) -> str:
    max_depth = settings.sub_agent_max_depth
    if _get_current_depth() >= max_depth:           # 深度检查
        return SubAgentResult(success=False, error_type="depth_exceeded", ...).to_text()
    _set_current_depth(_get_current_depth() + 1)
    try:
        if inherit_tools:
            from src.tools import ALL_TOOLS         # 工具继承 = 全部内置工具
            tools_to_inject = ALL_TOOLS
        sub_agent = StreamingSubAgent(name=..., tools=tools_to_inject, ...)
        return sub_agent.run(instruction).to_text()
    finally:
        _set_current_depth(_get_current_depth() - 1)
```

**为什么是 contextvars 而非线程本地存储**（代码注释明确写了理由）：task 工具可能在 ToolRegistry 的 ThreadPoolExecutor 并发路径里执行（见 `src/agent/tools_registry.py` 的 `_submit → copy_context`）。线程本地存储在每个 worker 线程各自独立、主线程的深度修改不传播，会导致并发子智能体各自看到 `depth=0`，突破 `sub_agent_max_depth` 限制。contextvars 会随 `copy_context()` 正确传播到 worker 且各自独立可写，与 remember 的 user_id 隔离完全同构。

> **工具继承**：`inherit_tools=True` 时注入 `ALL_TOOLS`（= BUILTIN_TOOLS），子智能体获得与父 agent 等同的工具集，可继续调 task（受深度限制）。

---

### 3.8 其余小工具的亮点

#### `write_todos` 的状态约束

```python
# 校验 in_progress 只能有一个
in_progress_count = sum(1 for t in todos if t.get("status") == "in_progress")
if in_progress_count > 1:
    # 只保留第一个为 in_progress，其余改为 pending
    ...
```

**动机**：待办列表里同时进行多个任务会混乱。强制约束「同时只一个 in_progress」，多余的自降为 pending。这是给 LLM 的行为护栏。工具是**全量替换**语义（传入完整列表，非增量），返回 dict 带 todos + summary。

#### `use_skill` 的按需加载

system prompt 只带技能的 name + description 摘要（省 token），LLM 判断需要时调 `use_skill(name)` 加载完整指令。返回值还附带**附属资源清单**（技能目录下的脚本/模板/参考文档），引导 LLM 用 read_file 按需读取。这与 Claude Code Skills 机制一致——skill 全文按需加载，不占常驻 token。

#### `human_approval` 的简洁

整个工具就一个 `interrupt(dict)` + 返回用户输入。但它的模块文档记录了重要历史：删除了从未被调用的 `ApprovalManager` 死代码，审批统一由 LLM 主动调用此工具触发——比「按工具名硬拦截」灵活（避免每次 write_file 都弹窗）。约定：用户输入 "approve" 视为通过，其他内容作为反馈传递给 LLM 继续处理。

---

## 4. 设计权衡总结

### 4.1 优点

| 设计 | 价值 |
|------|------|
| virtual_fs per-user 分层 | 多用户物理隔离，文件操作天然分目录 |
| virtual_fs 四层路径校验 | 多重防御目录穿越，每层防不同攻击 |
| 错误信息可读化 | 让 LLM 能自我纠正，而非无效重试 |
| run_shell per-user cwd 对齐 | shell 与文件工具隔离同构，不破功 |
| run_shell 六层纵深防御 | 诚实的安全模型，HITL 把关 |
| shell_safety 纯函数 | 可独立单测，无 I/O 依赖 |
| web_fetch SSRF 双道校验 | 入口 + 每跳重定向，防内网/元数据探测 |
| web_fetch 四级降级 | 榨干每个页面，总不空手而归 |
| 信号工具模式（compact） | 工具只发信号，执行逻辑单一入口 |
| remember/sub_agent contextvars 隔离 | 多用户并发安全 + 深度控制并发正确 |
| write_todos 状态约束 | 给 LLM 的行为护栏 |

### 4.2 技术债 / 待优化

| 项 | 说明 |
|----|------|
| virtual_fs 双模式代码翻倍 | 每个工具两套逻辑分支，虚拟模式可能用得少但为兼容保留 |
| run_shell 超时只 kill shell 本身 | shell=True 下子进程可能成孤儿继续运行（Windows 无进程组概念尤甚）。docstring 已诚实写明此局限；彻底解决需 `start_new_session=True`（posix）+ `os.killpg`，跨平台复杂，暂未做 |
| run_shell/virtual_fs 各有一份 `_get_workspace_root` | 两份实现需手动保持同构（一个用 Path、一个用字符串），分层规则改动需同步两处 |

---

## 本章小结

工具实现层的复杂度集中在**安全与隔离、健壮性**两个维度，衍生出三条设计主线：

| 主线 | 代表工具 | 核心思想 |
|------|---------|---------|
| 路径/访问安全 + 多用户隔离 | virtual_fs、run_shell | per-user 分层 + 多层校验 + 诚实声明边界 + HITL 把关 |
| 网络安全 + 提取健壮性 | web_fetch | SSRF 双道校验 + 四级降级，榨干每个输入 |
| 控制信号 | compact、human_approval、run_shell | interrupt() 统一范式 + 信号工具模式 |

**和前四章的呼应**：
- 第 1 章 graph.py 的 HITL 中断检测（get_state 查 interrupts），本章 human_approval / run_shell 是中断的**触发源**
- 第 2 章 tools.py 的 interrupt 工具强制串行，本章 run_shell 是 `_INTERRUPT_TOOL_NAMES` 的成员之一
- 第 2 章 tools.py 的 contextvars `copy_context()` 修复，本章 remember（user_id）、virtual_fs（vfs + workspace 分层）、run_shell（cwd 分层）、sub_agent（深度）全部是受益者
- 第 3 章 context.py 的 compact_messages，本章 compact_conversation 是它的**触发信号**
- 第 4 章 memory 的 remember_fact，本章 remember 工具是它的**唯一调用入口**

至此，Agent 的「脑（graph）、手（tools）、记忆（memory）」三层全部讲完。下一章进入 `src/mcp/`——MCP 客户端，让 Agent 能接入外部工具服务器，是「手脚」的外延。

---

> **本章验收点**：① virtual_fs per-user 分层（3.1）+ 四层校验是否讲透 ② run_shell per-user cwd 对齐 + 六层纵深防御（3.2）是否清晰 ③ web_fetch SSRF 双道校验 + 四级降级（3.3）是否到位 ④ sub_agent 已改用 contextvars（3.7）是否更新 ⑤ 本章覆盖 12 个文件、16 个工具，深度配比是否合适（重点工具深潜、小工具收口）。确认后推进第 6 章 mcp。
