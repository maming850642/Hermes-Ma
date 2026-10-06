# 07 · Context 与 Memory 工程

> 本章覆盖 Context Window 管理 + 长期记忆工程。「Context 满了怎么办」「记忆怎么存」「多用户怎么隔离」全在这里。

---

## 7.1 ContextManager —— 短期记忆管理

| | |
|---|---|
| **文件** | `src/agent/context.py`（~256 行） |
| **⚠️ langchain 依赖** | 顶层 `from langchain_core.messages import ...`（混合态，Phase 5.5 待清理） |
| **核心方法** | `build_llm_messages()`（`context.py:64`）+ `compact_messages()`（`context.py:161`） |

### build_llm_messages —— 窗口截断 + 工具配对保护（`context.py:64-140`）

```python
def build_llm_messages(self, user_id, current_input, messages, memories, todos, role):
    max_msgs = self.settings.max_short_term_messages * 2    # 20*2=40 条
    if len(messages) > max_msgs:
        truncated = messages[-max_msgs:]
        # ★ 防切断 tool_call 配对
        while (truncated and isinstance(truncated[0], AIMessage)
               and getattr(truncated[0], "tool_calls", None)):
            truncated = truncated[1:]     # 跳过开头孤立的"带 tool_calls 无 ToolMessage"的 AIMessage
    else:
        truncated = messages
```

> **工具配对保护**：截断后如果首条是带 tool_calls 的 AIMessage，它对应的 ToolMessage 可能已被丢弃。LLM 看到「我调了工具但没看到结果」会困惑。所以向后跳过这类孤立消息。

### SystemMessage 合并（`context.py:110-123`）

```python
# 历史中的 SystemMessage（压缩摘要）合并进 system_prompt
# API 要求 SystemMessage 只有一个且在最开头
extra_system_parts = []
conversation_msgs = []
for msg in truncated:
    if isinstance(msg, SystemMessage):
        extra_system_parts.append(msg.content)
    else:
        conversation_msgs.append(msg)
final_system = system_prompt + "\n\n" + "\n\n".join(extra_system_parts)
```

> 压缩摘要用 `SystemMessage` 包装存入 history（见 compact_messages），这里合并到主 system_prompt，保证 API 只收到一个 SystemMessage。

### Bug 11 修复：用户消息重复注入（`context.py:125-137`）

**V2 问题**：旧逻辑 `if conversation_msgs[-1].content != current_input` 做位置判重，只在首轮生效。LLM 发起 tool_call 后末尾是 ToolMessage，判重恒 True，每次 agent_llm 迭代都追加 HumanMessage → N 次工具调用用户消息出现 N+1 次，LLM 抱怨「用户重复了请求」。

**V3 修复**：`current_input` 已由 `stream_invoke` 构造 initial_state 时作为 HumanMessage 进入 messages，`build_llm_messages` 不再补。**仅防御性兜底**：历史中完全没有 HumanMessage 时才补（冷启动）。

---

## 7.2 三路 compact 触发 + 实现

### 三种触发路径

| 路径 | 触发 | 代码位置 |
|------|------|---------|
| A（precheck 自动） | token 达 `context_window × compact_threshold_pct%`（默认 80%） | `agent_v3.py:282-306` |
| B（Agent 主动） | LLM 调 `compact_conversation` 工具 → `state_updates["compact_requested"]` | `agent_v3.py:494` |
| C（用户手动） | CLI `/compact` 命令 / Web 按钮 | 直接调 `context_manager.compact_messages` |

### compact_messages 实现（`context.py:161-256`）

```python
def compact_messages(self, messages) -> CompactResult:
    keep_count = self.settings.compact_keep_recent    # 默认 10
    if len(messages) <= keep_count: return None

    # 早期消息拼接文本 → LLM 摘要
    old_messages = messages[:-keep_count]
    recent_messages = messages[-keep_count:]
    summary = generate_summary(old_text)               # 独立小 LLM 调用

    # 防御空摘要（LLM 返回空则放弃）
    if not summary: return None

    # 摘要用 SystemMessage 包装 + 显式 id
    summary_msg = SystemMessage(content=f"...摘要...{summary}",
                                id=f"compact-{uuid.uuid4().hex[:8]}")    # ★ 显式 id

    # 原地修改：clear + 重填
    messages.clear()
    messages.append(summary_msg)
    messages.extend(recent_messages)
```

> **为什么摘要 SystemMessage 要显式 id？** V2 的 graph 用 `RemoveMessage(id=...)` 删旧消息。如果摘要没有 id，`RemoveMessage` 跳过无 id 摘要 → 多次 compact 旧摘要删不掉 → 上下文膨胀。V3 虽然不靠 `RemoveMessage` 了（手写替换），但保留显式 id 防御未来回退。

### precheck 的 token 计数（`agent_v3.py:294-303`）

```python
from src.agent.token_counter import count_tokens
from src.agent.context_window import get_context_window
current_tokens = count_tokens(messages)
context_window = get_context_window()
threshold = int(context_window * (pct / 100.0))
if current_tokens < threshold: return    # 未达阈值跳过
```

> token 计数 / 窗口解析失败时优雅跳过（只 warning 不崩）。

---

## 7.3 `<think>` 标签剥离防污染（`agent_v3.py:431, 443`）

```python
# 存入 state 前：剥离 <think> 标签（防下轮 LLM 看到残留标记导致循环）
# reasoning 存入 additional_kwargs（供前端页面刷新时恢复推理面板）
clean_content = strip_think_tags(ai_msg.content)
state["messages"].append(AIMessage(
    content=clean_content,
    additional_kwargs={"reasoning": ai_msg.reasoning} if ai_msg.reasoning else {},
))
```

> **为什么必须剥离？** 不剥离的话，思考内容会污染历史上下文。下轮 LLM 看到 `<think>...</think>正文` 的历史消息，可能模仿格式继续输出 think 标签，甚至循环只输出 think 不输出正文。这是 V2 踩过的坑（commit `b61b313`）。

---

## 7.4 FileMemoryStore —— 纯文件长期记忆后端

| | |
|---|---|
| **文件** | `src/memory/file_store.py`（~235 行） |
| **存储** | 每用户 `<workspace_root>/users/<user_id>/profile.md`（jsonl，每行一条 Memory JSON） |

### 演进脉络

```
Mem0 封装（V2 早期） → 自研 Store + Qdrant 向量库（V2 中期）
    → 纯文件 FileMemoryStore（V2 M0 / V3 沿用）
```

> **为什么去向量库？** 三个理由：
> 1. 项目型记忆更依赖**文件结构导航**（项目目录、文件名、时间线）而非语义召回。
> 2. 自托管场景不想增加 Qdrant / embedding 服务的运维负担。
> 3. 关键词匹配对「找某个项目 / 某次会议」这类精确查询够用。

### 路径隔离（`file_store.py:62-66`）

```python
def _profile_path(self, user_id) -> Path:
    return self._root / "users" / user_id / "profile.md"   # 父目录自动创建
```

> **per-user 物理隔离 = 文件级作用域**。不同 user 的记忆在不同文件，天然隔离。V2 的 Qdrant 靠 `filter(user_id)`，V3 靠「按 user_id 定位不同文件」。

### search 评分公式（`file_store.py:131-155`）

```python
score = 0.5 + 0.5 * (命中查询词数 / 查询词总数)
# top-k 截断 + min_score=0.4 过滤
```

- **0.5 基线**是刻意的：向量时代 `min_score=0.4`，改成关键词匹配后如果基线是 0，只要查询词没全部命中就过不了阈值——记忆「全消失了」。加 0.5 基线让**任意命中**（哪怕只匹配 1 个词）的记忆都过 0.4 阈值，全命中 = 1.0。

> **话术**：这不是「精确检索算法」，是「迁移兼容策略」——换后端时保证已有 config 阈值不用改、记忆不「消失」。工程决策的约束是兼容性而非算法精度。

### _tokenize —— 中文按字分词（`file_store.py:217-234`）

```python
_EN_WORD = r"[A-Za-z0-9_]+"       # 英文按词
_CJK = r"[\u4e00-\u9fff]"          # 中文按字（2026-07-03 改动）
```

> **为什么中文按字？** 之前整段中文当一个 term，导致近义改写不命中（如查询「会议纪要」匹配不到记忆「开会记录」）。按字后召回率提升——「会」字命中即可。

### upsert 原子写（`file_store.py:111-127`）

```python
def upsert(self, memory: Memory) -> None:
    with _WRITE_LOCK:                          # 全局写锁
        mems = self._read_all(memory.user_id)
        # 按 id 去重，存在则 UPDATE，不存在则追加
        ...
        self._write_all(memory.user_id, mems)
```

### _write_all —— 全量重写原子性（`file_store.py:93-107`）

```python
def _write_all(self, user_id, mems):
    path = self._profile_path(user_id)
    tmp = path.with_suffix(".md.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for m in mems: f.write(json.dumps(...) + "\n")
    os.replace(tmp, path)    # ★ 原子替换
```

> **`os.replace` 原子性**：临时文件写完后原子替换，避免半写状态。POSIX 保证 `rename` 原子；Windows 上 `os.replace` 也保证原子。

### _WRITE_LOCK 分桶？实际是全局单锁（`file_store.py:36-40`）

```python
_WRITE_LOCK = threading.Lock()    # 全局单锁（非分桶）
```

> **为什么全局单锁而非 per-user 分桶？** 注释说明（`file_store.py:36-39`）：`profile.md` 路径由 user_id 唯一确定，不同 user 不冲突，但为简单用单锁——记忆写非高频路径竞争可忽略。如果未来记忆操作变高频，再改成按 user_id 分桶。

---

## 7.5 记忆写入双路径

| 路径 | 入口 | LLM 调用次数 | 场景 |
|------|------|------------|------|
| **主路径** `remember_fact` | agent 调 `remember` 工具 | 通常 1 次（仅 decide） | agent 已提炼好原子事实，跳过 extract |
| **兜底路径** `ingest_conversation` | 会话结束 → `SessionLifecycle.on_session_end` → `Summarizer` | extract + per-fact decide | 从对话提取事实 + 去重 |

### remember_fact —— 跳过 extract（`manager.py:109-134`）

```python
def remember_fact(self, user_id, content, source="tool:remember") -> dict:
    """agent 已提炼好事实 → 跳过 extract，只 decide + upsert（通常 1 次 LLM）"""
    candidates = self.store.search_candidates(user_id, content, limit=5)
    decisions = self.decider.decide(content, candidates)
    events = [self._apply_decision(user_id, source, d) for d in decisions]
    return {"success": True, "events": events, "item_count": ...}
```

> **关键：存储入口拆分**。`remember_fact` 跳过 extract（agent 已提炼好事实，不该再被 extract 一次），只走 decide。如果没有相似候选（短路 1），**零次 LLM 调用**直接写入——agent 的 remember 工具调用本身不触发额外 LLM。

### MemoryDecider —— 两条省 LLM 的短路（`decider.py:36-87`）

```python
def decide(self, new_fact, candidates) -> list[Decision]:
    if not candidates:                          # 短路 1：无相似旧记忆 → 直接 ADD
        return [Decision(action="ADD", content=new_fact)]
    # ... 有候选才调 LLM 判断 ADD/UPDATE/DELETE/NOOP ...
    except Exception:
        return [Decision(action="ADD", ...)    # 短路 2：LLM 挂了/返回非 JSON → 安全降级 ADD
```

**两条短路的设计哲学**：**宁可多记也不丢信息**。无候选时不需要 LLM 判断（省一次调用）；LLM 出错时不纠结——ADD 是最安全的选择（多记了最多冗余，丢记了不可恢复）。

### 四种 action

| action | 含义 | 何时触发 |
|--------|------|---------|
| `ADD` | 全新事实 | 无候选短路 / LLM 判断为新 |
| `UPDATE` | 合并旧记忆 | 语义相关但需更新 |
| `DELETE` | 语义矛盾 | 新旧事实矛盾（如「喜欢咖啡」→「喜欢茶」） |
| `NOOP` | 完全相同 | 已存在相同事实，不重复记 |

---

## 7.6 记忆聚合 Consolidation

> 长期记忆随时间累积会碎片化——同一主题散成多条原子事实、重复表述、上下文化冗余。`MemoryConsolidator` 把它们喂给 LLM 合并去重，原地替换 `profile.md`，让记忆条数变少、密度变高。这是记忆工程在「写入」之外补的「整理」层。

| | |
|---|---|
| **文件** | `src/memory/consolidator.py`（~158 行）+ `scheduler.py`（~278 行）+ `config_store.py`（~111 行） |

### consolidate 流程（`consolidator.py:50-`）

```
1. 快照   store.get_all(user_id)           聚合前全部记忆
2. 跳过   before_count <= 1 → too_few      太少不值得聚合（省 LLM）
3. 合并   CONSOLIDATE_PROMPT 喂 LLM        同主题合并 / 补充归并 / 去重 / 矛盾取最新 / 去上下文化
4. 乐观并发  store.get_all 再读一次         把 LLM 期间（~20s）worker/waker 新写的记忆
            → id 不在原快照的「新增记忆」追加到合并结果末尾（避免丢写）
5. 备份   replace_all(backup=True)         旧 profile.md → profile.md.bak.<ts>
6. 替换   _write_all 原子写                 os.replace 原子替换
```

> **为什么不引入跨进程文件锁（msvcrt/fcntl）？** 聚合是 LLM 长任务，期间 per-user worker / waker 子进程可能 `remember_fact` 新写记忆。粗暴替换会丢它们。解法是「替换前再读一次，把新记忆追加」——覆盖 99.9% 场景；残留的「再读→os.replace 几微秒」窗口由备份兜底。**不引入锁 = 不阻塞 chat、零平台相关代码**，代价是极小概率的丢写（有备份可恢复）。这是「乐观并发」而非「悲观锁」的工程权衡。

### 自动调度（`scheduler.py`）

`MemoryConsolidationScheduler` 是主进程 daemon 线程，骨架照搬 WakerScheduler：`_stop_event.wait(tick)` + ThreadPoolExecutor + 防重入集合。

**三条件全满足才自动触发**（`_should_auto_run`）：
1. `auto_consolidate == True`（默认 **False**，关）
2. 距 `last_run_at` ≥ `interval_hours`（默认 24h）
3. 记忆条数 > `threshold`（默认 20）

> `last_run_at` **仅成功才推进**——失败的下次 tick 会重试。聚合在主进程线程池跑，**新建独立 MemoryManager**（只需文件 + 1 次 LLM，不需要 agent），不抢 per-user worker IPC 锁、不 fork 子进程、不阻塞 chat。

**手动触发**：`submit_now(user_id) -> run_id` 立即跑（**不查开关/阈值**），返回 run_id 供前端 `get_status` 轮询（submit-and-poll 模式）。

### per-user 配置（`config_store.py`）

每用户一份 `<workspace>/users/<uid>/memory_config.yaml`（与 `profile.md` 同目录），4 字段：

| 字段 | 默认 | 含义 |
|------|------|------|
| `auto_consolidate` | False | 是否后台自动聚合 |
| `interval_hours` | 24 | 自动聚合间隔（小时） |
| `threshold` | 20 | 记忆 ≤ 此数不触发（省 LLM） |
| `last_run_at` | "" | 上次自动聚合时间（调度器回写） |

### file_store 新增方法（聚合所需）

| 方法 | 作用 |
|------|------|
| `replace_all(user_id, mems, backup=True)` | 全量替换：锁内备份旧 `profile.md` → `_write_all`。返回备份文件名 |
| `list_backups(user_id)` | 扫 `profile.md.bak.<数字>`，按 mtime 倒序 |
| `restore_backup(user_id, backup_name)` | 恢复前先把当前 `profile.md` 也备份一份（可反悔），含**路径穿越防护**（basename + 前缀校验） |

> 备份命名 `profile.md.bak.{timestamp}`，同秒冲突追加 `-2/-3`。Web 记忆页提供「聚合记忆 / 聚合设置 / 恢复备份」入口（路由在主进程直跑，不走 worker IPC）。

---

## 7.7 多用户隔离

### 两层隔离

**① 进程级隔离**（Web 路径）：每个登录用户一个独立 worker 子进程（`worker_manager.py`），跑完整的 MemoryManager + HermesAgentV3。`InterruptStore`、contextvar、MCP 客户端的 event loop 线程——这些进程级全局状态天然隔离。

**② 文件系统物理隔离**：记忆和文件都按 user_id 分层：
```
<workspace_root>/users/<user_id>/profile.md        # 记忆
<workspace_root>/users/<user_id>/                  # 虚拟文件系统根
data/sessions/<user_id>/<session_id>.json          # 会话历史
```

`user_id` 来自 contextvar（`set_current_user_id`），每轮请求入口设置，工具内通过 `get_current_user_id()` 读取——保证并发请求不串号。

### 一个 contextvar 贯穿四个子系统

```
set_current_user_id(user_id)   # stream_invoke 入口设置
   │
   ├─ virtual_fs._get_workspace_root()     文件操作隔离
   ├─ ShellExecutor workspace 锚点         shell 操作隔离
   ├─ FileMemoryStore._profile_path()      记忆隔离
   └─ waker / wakerflow 文件作用域          数字员工 / 工作流文件隔离（thinktank bus 为 WakerFlow 复用底座）
```

> **话术**：一个 contextvar 贯穿四个子系统（文件 / shell / 记忆 / waker 作用域），这是「统一用户标识传播层」的设计——不子系统各自管理用户 ID，而是一个 contextvar 全局铺底，配合 `copy_context()` 保证并发安全。

---

## 7.8 System Prompt 组装

### 拼装顺序（`src/prompts.py` 的 `build_system_prompt`）

```
full_prompt = persona + _COMMON_HEAD + body + _COMMON_TAIL + mode_section
```

| 段 | 内容 | 优先级考量 |
|---|---|---|
| `persona` | 角色人格（默认 assistant / waker 人格 / 自定义角色卡） | 最高，定义身份 |
| `_COMMON_HEAD` | 当前时间(日期/星期/时段) + 记忆说明 + 文件系统约定 + 通用工具用法 | 共享前置约束 |
| `body` | 角色专属能力 | 角色差异 |
| `_COMMON_TAIL` | MCP 配置说明 + 当前 user_id + `memory_section`(检索到的记忆，编号列表) + `todos_section`(待办，带状态图标) + `skills_catalog`(技能目录) | 动态上下文，每次都变 |
| `mode_section` | 权限模式提示（V3 新增，`mode_guidance.py`） | 行为约束 |

> **组装顺序的考虑**：关键行为准则紧跟 persona 避免 lost-in-the-middle 效应。动态上下文（记忆/待办/技能目录）放尾部，因为每次都变、且模型对尾部的新增内容敏感。

### 记忆注入（`prompts.py`）

记忆以**纯文本编号列表**形式拼入 system_prompt 的 `{memory_section}` 占位符：

```markdown
## 检索到的用户记忆
以下是关于这位用户的已知信息，请在回复时自然地参考：
  1. 用户偏好深色主题
  2. 用户在做 Hermes-Ma 项目
  3. ...
```

> 不使用 `<memory>` XML 标签或特殊分隔符，直接是 Markdown 列表。

### 检索策略

检索直接使用用户的原始输入作为 query，**不拼接历史消息**。之前的 `build_enhanced_query` 函数（拼接最近 3 条历史消息）已移除——拼接历史用户消息会污染语义（如把「你是谁？」拼进来），导致召回不到相关记忆。

---

## 本章小结

V3 的 Context / Memory 工程三层设计：

### 短期记忆（Context Window）

| 机制 | 实现 | 触发 |
|------|------|------|
| 滑动窗口截断 | `max_short_term_messages * 2 = 40` 条 | 每次构建 LLM 消息 |
| 工具配对保护 | 截断后跳过孤立 tool_calls AIMessage | 窗口截断时 |
| SystemMessage 合并 | 历史摘要合并进主 system_prompt | 每次构建 |
| `<think>` 剥离 | `strip_think_tags` 存入前清理 | 每次 AIMessage 入 state |
| precheck token 阈值压缩 | `context_window × 80%` | 每轮 ReAct 前 |
| compact 工具 | LLM 自主调 `compact_conversation` | Agent 判断 |
| 手动 compact | CLI `/compact` / Web 按钮 | 用户主动 |

### 长期记忆（FileMemoryStore）

| 机制 | 实现 |
|------|------|
| 载体 | 纯文件 `profile.md`（jsonl），无向量库 |
| 检索 | 关键词分词匹配，`score = 0.5 + 0.5 * 命中率` |
| 写入主路径 | `remember` 工具 → `remember_fact`（跳过 extract，0-1 次 LLM） |
| 写入兜底 | 会话结束 → `Summarizer`（extract + decide） |
| 去重 | `MemoryDecider` ADD/UPDATE/DELETE/NOOP，无候选短路 + 异常降级 ADD |
| 聚合 Consolidation | `MemoryConsolidator` LLM 合并去重 + 乐观并发 + 备份可恢复；`MemoryConsolidationScheduler` 定时触发 |
| 原子写 | `os.replace` + `_WRITE_LOCK` |
| 隔离 | per-user 物理分文件 + contextvar |

---

> **下一章**：[08-配置与部署](08-配置与部署.md) —— config.yaml 全表 / 双访问 / env 覆盖 / _INT_KEYS。
