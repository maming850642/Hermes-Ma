# 第 8 章 · 横切关注点

> 最后一章。前 7 章按模块纵向深潜，本章把散落在各层、不属于任何单一模块的「基础设施」横向收口。
> 这些横切模块体量都不大，但支撑着整个系统的运转。

---

## 0. 一句话定位

五个横切模块是 Agent 的「公共设施」：

| 模块 | 一句话 |
|------|--------|
| `prompts.py` | 助手/总参**双模板** System Prompt + 共享段拼接，控制 Agent 的行为与工具使用方式 |
| `skills.py` | 技能注册表——扫描多目录，兼容市面主流 skill 格式 |
| `health.py` | 启动自检——检查 LLM API 连通性（M0 已移除 Qdrant/Embedding） |
| `logging_config.py` | 分层日志——info.log（仅 hermes.*）+ error.log（全部）|
| `exceptions.py` | 异常体系——按功能域分类的语义化异常（三大族） |

---

## 1. 逐模块要点

### 1.1 `prompts.py` —— System Prompt 工程

这是**控制 Agent 行为的灵魂**。模块已重构为**双模板结构**——不再是一个 200+ 行的单一 `SYSTEM_PROMPT_TEMPLATE`，而是「两个角色人格/行为块 + 两段共享内容」拼接而成。

**两个角色模板**（每个 = persona 一句话 + body 行为准则）：

| 模板 | persona | body 主题 |
|------|---------|----------|
| `ASSISTANT_PERSONA` / `ASSISTANT_BODY` | 智能助手，主动推进用户需求 | 先做事后提问、一次把事做完、复杂任务先规划、网络/Shell/子任务使用指引、回复风格 |
| `CHIEF_OF_STAFF_PERSONA` / `CHIEF_OF_STAFF_BODY` | 智囊团总参，把模糊降成可拍板决策 | 黄金法则（少问多做/先派活再说话/容忍空状态）、项目结构、四步工作流（探查→dispatch→产出 open-questions.md→递交拍板）、行为边界 |

**两段共享内容**（助手/总参通用，由 `build_system_prompt` 拼接）：

| 共享段 | 内容 |
|--------|------|
| `_COMMON_HEAD` | 当前时间 · 记忆指引（`remember` 写 `profile.md`）· 文件系统（路径必须 `/` 开头、`ls /` 确认、不可逆操作调 `request_human_approval`）· 通用工具（`write_todos` / `compact_conversation` / `request_human_approval`） |
| `_COMMON_TAIL` | MCP 配置文件 · 当前用户 ID · 检索到的记忆 · 当前待办 · 技能目录 |

**`build_system_prompt` 注入的动态信息**：

| 来源 | 内容 |
|------|------|
| `datetime.now()` + `_WEEKDAY_MAP` | 当前日期 / 中文星期 / 时段 |
| `user_id`（`stream_invoke` 传入） | 当前用户 |
| `memories`（`retrieve_memory` 节点产出） | 检索到的长期记忆，按 `MEMORY_SECTION_WITH_DATA` / `EMPTY` 二选一 |
| `todos`（`state.todos`） | 当前待办事项，带状态图标（`[ ]`/`[>]`/`[x]`/`[-]`） |
| `SkillRegistry.build_catalog()` | 可用技能目录 |
| `get_mcp_config_path()` | MCP 配置文件路径 |

**值得注意的设计**：

1. **角色路由**（`build_system_prompt` 内）：`role="chief-of-staff"` 用总参模板；`role=None` 用助手模板；**其他 role** 从 `RoleRegistry` 加载自定义角色卡（取其 `description` 当 persona、`content` 当 body），加载失败回退助手模板。这就是第 7 章讲的角色切换「不 spawn 新 agent，只换 prompt」的落点。

2. **组装顺序防 lost-in-the-middle**（`build_system_prompt` 末尾，注释明确说明）：
   ```
   full_prompt = persona + body + common_head + common_tail
   ```
   这**调整自原 persona→head→body→tail 的顺序**——把最关键的行为准则/工作流（body）紧跟 persona 之后放进强注意力区，把参考信息（common_head 的工具说明、common_tail 的记忆/待办/技能）放后面按需查阅。目的是对抗大模型「中间段注意力稀释」的 lost-in-the-middle 效应。

3. **记忆使用指引**（`_COMMON_HEAD`）：明确教 LLM「自然利用记忆，不要刻意提及'根据我的记忆'」——这是体验设计，避免 Agent 变成机械的「我记得你叫 X」。

4. **文件系统的「反盲目试错」教育**（`_COMMON_HEAD`）：2026-09 起文件工具退役，prompt 改为「统一 bash + 先加载 `file-ops` 技能」、严禁 `ls /`/`find /`（MSYS 虚拟根）。代码层靠 shell 分类+审批，prompt 层教育路径规则。

5. **总参模板的强约束**（`CHIEF_OF_STAFF_BODY`）：四步工作流里明确「第 1-2 步只做探查和派活，提问留到第 4 步递交拍板」「dispatch 结果回来第一时间 `write_file` 落到 `open-questions.md`，而非口头总结」。把「总参的核心交付物是待决策清单」写进 prompt，是智囊团模式能否跑起来的关键。

> **设计哲学**：System Prompt 是「行为契约」。每一段都对应一个具体的、LLM 容易犯错的行为，用明确规则约束。这不是「花哨的提示词工程」，而是**把 Agent 的行为规范显式化**——可读、可改、可审计。

### 1.2 `skills.py` —— 技能注册表

兼容 Claude Code Skills、Cline Rules、Cursor Rules 等格式（`skills.py` 模块 docstring）。

**核心设计**：

1. **强制文件夹结构**（v2 重构，`SkillRegistry._scan_dir`）：每个技能 = 一个子文件夹，入口 md + 可选附属资源。**顶层散落的文件直接忽略**（`_scan_dir` 内 `entry.is_dir()` 判断）。

2. **入口 md 三级查找优先级**（`_find_entry_md`）：
   - `SKILL.md` / `skill.md`（Claude Code 约定）
   - 文件夹内唯一的 `.md`（仅顶层，不递归）
   - 与文件夹同名的 `.md`

3. **手写 frontmatter 解析**（`_parse_frontmatter`）：不依赖 PyYAML，只支持最简单的 `key: value` 格式，自动去引号。docstring 说「覆盖市面 99% 的 skill 文件」。这是**刻意减少依赖**的设计；不支持的复杂 YAML 安全跳过，回退为原文。

4. **三目录扫描**（`SkillRegistry.load`）：
   - 项目内 `./skills/`（source=`"project"`）
   - 用户 `~/.hermes/skills/`（source=`"user"`）
   - `extra_skills_dirs` 配置（source=`"extra"`，分号分隔）
   - 后者覆盖前者同名技能。

5. **附属资源收集**（`_collect_resources`）：递归收集技能文件夹内除入口 md 外的所有文件，排除 `__pycache__` / `.git` / `node_modules` 等目录、`.pyc` 等编译产物、隐藏文件。路径用正斜杠（跨平台一致，也方便 LLM 理解）。

6. **`build_catalog` 注入 prompt**：把所有技能的 `name` + `description` 拼成目录文本（「你可以通过 `use_skill` 工具加载某个技能…」），供 LLM 判断是否调用 `use_skill`。无技能时返回提示语，恒非空。

> **联系第 5 章**：use_skill 工具（第 5 章）返回技能正文 + 附属资源清单，LLM 用 `bash cat` 按需读取。skills.py 负责加载，use_skill 负责按需交付。这是「常驻目录（省 token）+ 按需全文加载」的两级设计。

### 1.3 `health.py` —— 启动自检

> **2026-06-27（M0）移除了 Qdrant / Embedding 检查**——记忆层改为纯文件后端（`FileMemoryStore`），不再依赖向量库，所以这两个连通性检查随之删除。现在**只剩 `check_llm_api` 一个检查**。

`run_health_check` 维护一个检查列表：

```python
checks = [("LLM API", check_llm_api)]
```

| 检查 | 方法 | 失败处理 |
|------|------|---------|
| `check_llm_api` | `GET {base}/models`，`401` = Key 无效 | `raise ValueError`（Key 无效）/ `raise ConnectionError`（不可达、超时） |

**设计要点**：
- `check_llm_api` 用 `httpx.get` 带超时（10s），区分 `401`（Key 问题，`ValueError`）与连接/超时（`ConnectionError`）。
- `run_health_check(silent)` 逐个跑检查、汇总 `all_ok`，`silent=False` 时用 Rich 打印 `OK` / `FAIL`。返回布尔供调用方决策。
- `cli.py` 在 `main()` 启动早期调用它，`all_ok=False` 即 `sys.exit(1)`——**启动期失败立即退出**，不带着残缺状态运行。

> 删 Qdrant/Embedding 检查后，health.py 的职责变纯粹了：只验「LLM 端点能通」这一个最硬的外部依赖。Web 端每用户独立 worker 子进程启动时也会做同样的连通性校验。

### 1.4 `logging_config.py` —— 分层日志

**分层设计**（模块 docstring）：

```
控制台（stderr）：WARNING（--debug 时 DEBUG）—— 保持终端安静
logs/info.log：INFO+，按天滚动 7 天，只收 hermes.* 自家日志
logs/error.log：ERROR+，按天滚动 7 天，收全部 logger 的错误
```

**关键设计**：

1. **root 设 DEBUG，各 handler 各自过滤**（`setup_logging`）：而非全局压制。这样 info.log 能收 INFO、error.log 能收 ERROR，互不影响。

2. **info.log 加 HermesFilter**（`HermesFilter`）：只放行 `hermes.*` 的日志，挡住第三方 INFO。**双保险**——即使有第三方 logger 漏设 CRITICAL，filter 也会挡住。

3. **error.log 不加 filter**：收**所有** logger 的 ERROR，包括第三方的致命错误。因为排查时第三方报错（如 httpx 连接失败）往往关键。

4. **噪音库静默**（`_NOISY_LOGGERS`）：`httpx` / `httpcore` / `urllib3` / `sentence_transformers` / `chromadb` / `langgraph` / `langchain` / `markdown_it` 强制设为 `CRITICAL`。这些库默认 INFO 会刷屏。

5. **按天滚动 + 自动清理**：`TimedRotatingFileHandler(when="midnight", backupCount=7)`，每天 0 点滚动、保留 7 天。

> **联系各章**：各模块的调试/诊断日志都用 `logger.info`（`hermes.*` logger）写，会进 info.log。因为只收 `hermes.*`，所以不会和第三方日志混在一起。

### 1.5 `exceptions.py` —— 异常体系

按功能域分类的语义化异常。记忆层改纯文件后，异常体系做了**精简**——移除了原先的 `MemoryError` 子树（`MemoryConnectionError` / `MemoryDimensionError` 等），现在只剩**三大族**：

```
HermesError（基类，带 message + detail）
├── ToolError
│   ├── ToolNotFoundError        （带 tool_name）
│   ├── ToolExecutionError       （带 tool_name + cause）
│   └── ToolTimeoutError         （带 tool_name + timeout）
├── ConfigError
│   ├── ConfigMissingError       （带 config_key + hint）
│   └── ConfigInvalidError       （带 config_key + invalid_value + reason）
└── HealthCheckError
    └── HealthCheckFailedError   （带 service_name + cause）
```

**设计要点**：
- **每个异常携带上下文**：如 `ToolTimeoutError` 带 `timeout`、`ToolExecutionError` 带 `cause`、`ConfigMissingError` 带 `hint`，不只是字符串。
- **message + detail 双层**（`HermesError.__init__`）：`message` 面向用户（摘要），`detail` 面向开发者（详细上下文），最终拼成 `{message}\n  详情: {detail}`。
- **精简动机**：原先的 `Memory*` 异常是向量库时代的产物（连接失败、维度不匹配等）。改纯文件后端后这些场景消失，异常树随之瘦身，避免「定义了但永不触发」的死代码。

> **小结**：这套三大族异常覆盖了 Tool / Config / Health 三个真实需要语义化区分的功能域，结构清晰、上下文充分。随着记忆改文件、Qdrant 退场，异常体系同步收窄，保持与实际代码一一对应。

---

## 全书结语

Hermes-Ma 是一个**设计动机极其清晰**的项目。它的复杂性不是"为复杂而复杂"，而是每一处都对应一个真实的生产问题：

| 真实问题 | 催生的设计 | 章节 |
|---------|-----------|------|
| 自托管 LLM 端点流式半开 | 同步阻塞隔离 + 工作线程 | 1 |
| 多线程 contextvars 丢失 | copy_context() 统一范式 | 1,2 |
| LangGraph 版本行为差异 | get_state() 中断检测 | 1 |
| Mem0 黑盒 + 流水账记忆 | 自研三层架构（Store/Extractor/Decider）+ 纯文件后端 | 4 |
| stdio MCP 并发响应错位 | per-server 锁 | 6 |
| 终端 UI 长回复堆叠 | 自动分段 + 并发降级 | 7 |
| LLM 盲猜路径 | 代码层 + prompt 层双教育 | 5,8 |
| 大模型 lost-in-the-middle | prompt 组装顺序调整（行为准则紧跟 persona） | 8 |

**读懂这些"为什么"，比读懂代码本身更重要。** 代码会变，但解决问题的思路（同步阻塞隔离、contextvars 传播、纵深防御、降级链、信号工具、reducer 语义）是可迁移的知识。

全书 8 章覆盖了 `src/` 全部代码（约 9000 行）+ 根目录入口。每个模块都讲了架构动机、关键代码走读、踩坑历史、设计权衡。希望这份梳理能帮你真正吃透这个项目。

---

> **本章验收点**：① 五个横切模块的深度是否合适（比前几章浅，因为是收口章）② 核心变化是否讲清——`prompts.py` 的双模板/组装顺序调整、`health.py` 的单检查、`exceptions.py` 的三族精简 ③ 这是全书最后一章，确认后整个 walkthrough 系列就完成了。
