# 全库深度敌意审查报告（不只是提交）

- **审查对象**：`open-source-prep` @ 32f6309，全部生产代码（~2 万行 Python + ~4.2 千行前端/模板）+ 配置 + 脚本 + 文档
- **方法**：6 路子系统首轮审查（agent 核心 / 工具层安全 / 记忆存储 / Web 后端 / 调度插件 / 前端文档）→ 全部 P1/P2 交独立复核组逐条验证可达性 → 关键项主审亲测复现
- **收录纪律**：只收真实缺陷——触发场景必须在当前 HEAD + 出厂配置（config.example.yaml / 仓库自带 config.yaml）下可达。防御纵深建议、纯风格项一律不收。复核中被证伪的 6 项列入文末误报清单，防止后续重复审查再报。
- **标注**：【实测】= 本机实际运行复现；【链证】= 完整调用链逐行核过

统计：**P0 × 3，P1 × 8，P2 × 24，P3 × 14**；误报剔除 6 项。

---

## P0（安全承诺失效 / 数据破坏级）

### P0-1 shell 分类器不识别换行与单个 `&`——任意命令拼接零审批，plan 模式只读承诺失效【实测】
- 位置：`src/tools/shell_safety.py:135-170`（分隔符集合 `{"&&","||",";","|"}`，:160）
- 实测（仓库自带 config.yaml，`shell_enabled: true`）：
  - `cat notes.txt\nwhoami` → `('auto','白名单内命令')`，`_split_subcommands` 把两行吞成 `['cat a.txt whoami']`。shlex 中换行是纯空白；Git Bash 与 cmd 都把换行当命令分隔符，第二条命令照跑。
  - `echo hi & python -c "print(1)"` → `auto`。单 `&` 不在分隔符集合，两段命令都执行。
- 后果：`shell_enabled: true` 的部署形态下，以白名单词开头 + 内嵌换行/单 `&` 即绕过全部审批（plan / before_changes 均放行非 destructive）。历史上修过无空格分号绕过（test_run_shell.py），但测试只覆盖 `&&` `;` `|`，漏了 `\n` 与 `&`。
- 修复方向：预处理把 `\n`/`\r` 归一为 `;`；单 `&` token 加入 separators。

### P0-2 `env` 在出厂白名单 = 无条件任意程序执行【实测】
- 位置：`src/tools/shell_safety.py:41`（默认白名单含 `env`/`printenv`）、`config.example.yaml:76` 同
- 实测：`env python -c "import os"` → `auto` 零审批。`env <任意程序>` 首 token 恒为 `env`，白名单命中即放行——等于白名单机制对这条路径整体失效。`bash.yaml` 向模型承诺"危险命令会等待人工审批"，与实际不符。
- 修复方向：`env`/`printenv` 移出白名单，或只放行无参/`-u NAME` 形态。

### P0-3 HITL 审批条属性注入 XSS——全应用唯一人工安全闸门可被 prompt 注入翻转【链证】
- 位置：`web_fastapi/static/js/chat.js:568-570`（`escapeHtml` 只转 `& < >`，不转 `"`）+ `chat.js:825`（`info.details` 进 `title="..."` 属性上下文）
- 链路：`details` 来自 LLM 工具参数（run_shell.py:144-149 `details=f"$ {command}"`、human_approval.py:25-38）→ agent_v3.py:1102-1130 原样透传 → worker `_drain_stream_events` → SSE → `showApproval`。LLM 读了含注入指令的网页/文件后提出破坏性调用（弹审批条的前提），`details` 携带 `" tabindex="1" autofocus onfocus="PUT /api/config/permission-mode → full_access"`——零交互（autofocus）在同源执行 JS，直接把后续所有 destructive 操作放行。注入点恰好长在全应用唯一的安全闸门上。
- 修复方向：`escapeHtml` 补 `"` `'`（项目内 memory.html:167 已有全字符集版本可对齐）；属性上下文禁止复用文本转义函数。

---

## P1

### P1-1 git / find / awk / sed 白名单放行破坏与执行形态【实测】
- 位置：`src/tools/shell_safety.py:39`（注释称"破坏性子命令由黑名单兜底"）、`:77-79`、`:88-95`
- 实测（真实 config）：`git reset --hard` → `auto`；`git clean -fdx`、`git apply evil.patch`、`git log --output=<工作区外路径>`、`find . -execdir rm {} ;`（`-exec\b` 不匹配 `-execdir`）、`find -fls <路径>`（绕过 `>` 重定向检测写文件）、awk `|"cmd"| getline`、sed `'e whoami'` → 全部 `auto` 零审批。
- 修复方向：git 只放行明确只读子命令枚举；`-exec(dir)?`、`-fls|-fprint|-fprintf`、awk getline 执行形态、sed `e/w/r` 入黑名单与写信号。

### P1-2 出厂黑名单整体替换代码默认表：一半保护丢失、一半过度匹配误杀【实测】
- 位置：`src/tools/shell_safety.py:121-132`（`load_blocked_patterns`：配置非空 → 整体替换，不合并）、`config.example.yaml:78`
- 实测：出厂表缺 fork 炸弹、`find -exec`、`awk system(`、`sed -i`、`C:\Users` 重定向等默认条目（fail-open，P1-1 的攻击在出厂配置下全部从 force_deny 变免审批放行）；同时 `curl|sh` 这条"正则"等价于"命令含子串 sh 即拦"——`git show HEAD` → `review 命中黑名单 curl|sh`，`du -sh` 同样被硬拒（deny 无审批出口，含 full_access 在内所有模式）。该拦的不拦，常规只读命令反而不可用。
- 修复方向：改为"默认表 + 配置追加"；`curl|sh` 写成真边界正则（`curl\b[^\n|]*\|\s*(ba|z)?sh\b`）。

### P1-3 批量工具执行中断丢前序结果：悬空 tool_calls 持久化，会话被钉死【链证】
- 位置：`src/agent/registry_v3.py:454-473`（串行批量中 InterruptSignal 冒泡，局部 `tool_messages` 连同前序结果丢失）、`src/agent/agent_v3.py:1041`（赋值点不可达）、`:1188-1195`（`_build_state` 只补 pending 一条）
- 触发：出厂默认 `before_changes` 模式，模型一轮并行发 `[read_file, write_file]`（并行工具调用是现代模型常态）→ read 成功 → write 触发审批 → approve 后消息序列为 `assistant(tool_calls=[cA,cB]) + tool(cB)`，cA 永久悬空 → 严格 OpenAI 兼容端点 400。**加重**：悬空结构经 turn_messages 落桶持久化（cli.py:1588、worker_process.py:882），此后每轮请求都携带——该会话从此每轮 400，直到压缩恰好整段剔除。
- 测试缺口：`tests/test_registry_v3.py:356` 构造了同场景但只断言 `pytest.raises(InterruptSignal)`，把结果丢失锁死为"符合预期"。
- 修复方向：`_execute_serial` 捕获 InterruptSignal 时把已完成 tool_messages 塞进 signal payload；`_build_state` 据此补齐配对；另建"assistant tool_calls 与 tool 消息一一配对"的贯穿性不变量测试。

### P1-4 `request_human_approval` 并行调用被吞：审批面板永不弹出【链证】
- 位置：`src/agent/registry_v3.py:515-522`（`_run_tool` 的 `except Exception` 吞掉 InterruptSignal）、`:602-617`（并发预判只看静态标签）
- 触发：`tools/request_human_approval.yaml` 静态 `destructive: false` 且无 evaluator → `_contains_approval_tool` 判 False → 模型一轮并行调 `[任意工具 + request_human_approval]` 走并发路径 → 执行器抛的 InterruptSignal 被吞成"工具执行错误"文本。用户请求人工介入的语义被无声破坏，模型还会把错误文本里的 payload 反复重试。出厂可达。
- 修复方向：`_run_tool` 对 InterruptSignal 单独 `raise`；或并发分支执行前先跑一次权限预检。

### P1-5 Web 免认证 + 零 Host/Origin 校验：回环绑定防不住浏览器侧攻击【链证】
- 位置：`web_fastapi/app.py:251-274`（零中间件，全后端 grep `CORS|TrustedHost|Origin` 零命中）、`web_fastapi/dependencies.py:17-24`（身份常数化）
- 攻击链（受害者仅需在服务运行时用浏览器开一个恶意网页）：① CSRF 简单请求打无 body 端点：`POST /api/workspace/mount_local`（Form 表单；黑名单对用户目录只做 `resolved == f` 精确匹配，`~/.ssh`、`Chrome User Data` 全放行，workspace/service.py）、`POST /api/projects/{slug}/activate`、`POST /api/memory/consolidate`（白烧 LLM 费用）等；② DNS rebinding（无 Host 校验）后全 API 同源读写：读全部会话/文件树，`PUT /api/config/system` 改 `openai_base_url` → 下一轮 chat 携带 `Authorization: Bearer <真实 key>` 和全部对话打到攻击者端点（llm/client.py:56-75 确认 base_url 全链路生效）。
- `main.py` 的安全声明只豁免"网络暴露"，未覆盖浏览器侧；`test_security_baseline.py` 把免认证固化为设计，但没声明这个盲区。
- 修复方向：全局 Host 白名单 + 非 GET 校验 Origin/Sec-Fetch-Site 两个中间件（几十行），`mount_local` 封用户目录整棵子树。

### P1-6 UPDATE 记忆删向量不现算：最常见的写路径静默摧毁语义检索【实测】
- 位置：`src/storage/sqlite_provider.py:388-409`（`_embed_missing` 判缺失 = 查 `memory_vecs` 行存在）、`:421` + `:429-431`（UPDATE 时旧行仍在 → 不重算，随后无条件 DELETE 向量且不回插）；调用源 `src/memory/manager.py:129-146`（Decider UPDATE 主路径）、`:337-344`（web 记忆页人工修正）
- 实测：insert 后 vec=1 → update 后 vec=0，检索只剩 keyword 通道。全仓唯一自动补救是 web 主进程启动时跑一次的 `_warm_embedder`；CLI-only 用户无任何补救。测试确无"同 id 二次 upsert × embedder"用例。
- 修复方向：缺失判定改为"行存在且向量对应内容版本一致"，UPDATE 行强制进入 embed 集合。

### P1-7 调度 tick 无逐条异常隔离：一条坏配置停摆其后所有任务【实测】
- 位置：`src/waker/scheduler.py:169-180`、`src/wakerflow/scheduler.py:142-165`（`compute_next_run`/`is_due` 均不在 try 内）、`src/waker/schedule_parse.py:57-60`
- 实测：`expire_at: 2026-01-01T00:00:00+08:00`（Web 创建接口对 expire_at 零格式校验，models.py:86）→ `now >= exp` 抛 `TypeError: can't compare offset-naive and offset-aware datetimes` → 异常穿出 `_tick` 被外层吞为一条日志，下一 tick 同处再炸——按目录名排序在坏条目**之后**的全部 waker/flow 永久停摆，用户视角"调度莫名停了"。专门写的 `validate_schedule` 生产零调用。
- 修复方向：tick 循环体逐条 try/except continue；创建/更新路径接线 `validate_schedule` 并拒绝带时区的解析结果。

### P1-8 前端 sanitizeHtml 可绕过：`javascript:` 链接从 LLM 输出直达点击执行【链证】
- 位置：`web_fastapi/static/js/chat.js:314-330`
- 绕过：检查在 DOMParser 解码**之后**做 `/^\s*javascript:/i`——`<a href="jav&#x09;ascript:...">` 解码为 `jav\tascript:`（tab 在词中）不匹配，属性保留；浏览器导航时按 URL 规范剥离原始 tab 后仍按 `javascript:` 执行。`xlink:href` 不在检查名单。marked v15.0.12 默认原样放行 raw HTML。所有 LLM 正文与 md 文件预览都走这条管线，间接 prompt 注入即可投毒。
- 修复方向：剥离 `\t\r\n` 后再判定或用 `new URL(v, location).protocol`；属性名单扩 `xlink:href|formaction|action`；根治换 DOMPurify。

### P1-9 MCP `trust` 配置全链路失效【亲核】
- 位置：`src/mcp/tool_factory.py:107-112`（`from src.mcp.config import get_config_manager` —— 该符号不存在，异常被 `except Exception: server_configs = {}` 静默吞）、`src/mcp/config.py`（`McpServerConfig` 无 `trust` 字段）
- 后果：`mcp_servers/mcp_<name>.json` 里的 `"trust"` 永不生效，所有 MCP 工具恒按默认 destructive 弹审批。方向保守（无安全破口），但属"承诺不生效"的死代码路径。
- 修复方向：`McpServerConfig` 增 `trust` 字段并校验取值；删掉对不存在符号的 import。

---

## P2

### 工具层
- **P2-1 copy_file / move_file schema 与实现参数错位，两工具全坏、move 假报成功**【实测】：yaml 声明 `src`/`dst`（copy_file.yaml:9-15、move_file.yaml:9-15），实现读 `path`/`dest`（executors/fs.py:62、85、87）→ 参数归空后操作对象落到工作区根：copy 恒报错，move 返回"已移动"但什么都没动。附带：copy 标 `destructive:false` 但语义是覆盖写。修：实现改读 `src/dst`。
- **P2-2 web_fetch SSRF：校验与连接两次解析 DNS（TOCTOU/rebinding），配代理后校验彻底失义**：`web_fetch.py:56-84` 本机 getaddrinfo 预检 vs `:282-302` httpx 再次解析；攻击者控 DNS（TTL<间隔）首解公网过检、连接时解到 `169.254.169.254`/内网。仓库 config.yaml 即配置了 `web_proxy`，此时真实解析在代理侧，本地校验形同虚设。修：连接层 pin 已校验 IP。
- **P2-3 未知工具名静默剔除破坏消息配对**：`registry_v3.py:356-366` 只 warning 不生成 tool 消息 → 模型幻觉工具名或 MCP 热刷新后，该 tool_call 永悬空 → 严格端点 400 会话卡死。修：未知工具合成错误 ToolMsg。
- **P2-4 JSON Schema 约束全不生效 + timeout=0 孤儿进程**：`registry_v3.py:75-127` coerce_args 无 min/max/required 处理；`bash.yaml:31-34` 的 `timeout: maximum 600` 是装饰；串行外层超时（registry_v3.py:398-411）`shutdown(wait=False)` 遗弃工作线程，bash 进程无人 kill。修：clamp + 超时杀进程树。

### Agent 核心
- **P2-5 resume 轮丢 todos**：`agent_v3.py:571-584` resume 分支不透传 todos，`_build_state:1202` 硬编码 `[]`；worker（worker_process.py:915-923）与 CLI（cli.py:855-860）都传了参——审批恢复轮系统提示丢待办，模型中途忘记任务清单。
- **P2-6 CLI 主循环 except 链顺序错误**：`cli.py:1591-1609`——EOFError 被 `except Exception` 先吃（显示"对话出错"而非干净退出）；两个含 `save_session` 的兜底分支永不可达；Ctrl+C 分支无 save_session，中断对话不落盘。
- **P2-7 `except TimeoutError` 在 Python 3.10 失效**：`registry_v3.py:404-409` 捕 builtin，而 `future.result(timeout=)` 在 3.10 抛 `concurrent.futures.TimeoutError`（3.11 才合一）；`pyproject.toml` 声明 `>=3.10`。工具超时保护变成整轮报错。同仓 session_lifecycle.py:18 已正确区分，此处漏。
- **P2-8 llm_stream"防线 C"未实现：停止/超时后继续烧流**：`llm_stream.py:15` docstring 声称 close() 断 socket，`:124-129` 实际只有 log+raise；用户中途停止后 daemon 线程继续读流到生成完（worker 侧 `stream.close()` 只关外层 generator），vLLM 槽位被白占、token 白烧。
- **P2-9 LLM 错误文本永久写入对话历史**：`agent_v3.py:946-953` "⚠️ LLM 调用失败：{e}" 作为 assistant 消息落桶持久化，进入后续所有轮的模型上下文。
- **P2-10 旧 config 缺 `max_short_term_messages` 键则每轮 AttributeError**：`context.py:96` 属性直取；项目明确支持手写精简 config（agent_v3.py:1322 用 getattr 兜底即是证明），此处缺键即每轮崩溃。

### Web 后端
- **P2-11 async 路由同步调 `worker.send` 冻结整个事件循环**：system.py 4 处 + sessions.py 7 处（memory.py:8-10 docstring 明令禁止此写法，system/sessions 违反）；主槽流式持锁期间，任一 `GET /api/system/tools`/`GET /api/sessions/current` 都让全站（含其他会话 SSE、stop）停摆 5s（锁超时）至 300s（响应超时）。修：改同步 def 或 run_in_threadpool。
- **P2-12 `get_or_create` 持全局 Manager 锁 spawn 最长 60s**：`worker_manager.py:414-457`——锁内 Popen + 轮询 ready；期间 stop/lookup/槽位申请全被卡。修：spawn 移出锁。
- **P2-13 新会话首条消息在事件循环内同步等 spawn**【复核新增】：chat.py:85/103、sessions.py:37 在 async handler 里直接触发 spawn+等 ready——Windows spawn + 重 import 以十秒计，app 启动后每会话首条消息必现全站冻结。
- **P2-14 main 槽错位：`/api/compact` 与 `PUT /api/config/waker` 不作用于当前会话**【复核新增】：system.py:25 与 config_router.py:114-124 恒取 main 槽——用户对当前会话点"压缩历史"压缩的是空/旧桶；切换 waker 对专属槽会话不生效。
- **P2-15 `chat/approve` 漏捕 `SlotsFullError` → 500**：chat.py:99-110（对照 chat_stream:82-87 转 429）；3 槽全忙时提交审批 → 500 且审批卡死。

### 记忆 / 存储
- **P2-16 sqlite_vec 扩展缺失 + embedder 可用 → 构造直接崩溃**【实测】：`sqlite_provider.py:217-231` 的 try 只包扩展加载，`_ensure_aux_tables`（:323-360）在 try 外执行 `CREATE VIRTUAL TABLE ... vec0` → `OperationalError: no such module: vec0`，启动链整体不可用——而日志刚打印完"仅关键词降级"。fastembed 轮子装失败/无 enable_load_extension 的 Python 构建都会命中。
- **P2-17 recency 衰减 × 相对门槛压制两周以上记忆**：`sqlite_provider.py:68`（0.995/小时：14 天=0.186、30 天=0.027）乘在融合分上（:565）且先于 0.35 相对门槛（:580-583）——旧记忆与任一较新命中竞争时需 ~1.9 倍原始融合分才能幸存，一个月前的事实实际检索不到。修：衰减基准改天级或下限钳制。
- **P2-18 `/exit` 与 worker 退出的超时保护被同步 join 击穿**：`session_lifecycle.py:136-141` `future.result(timeout=120)` 抛超时后 with 块 `shutdown(wait=True)` 仍 join 到 LLM 跑完；同型：`worker_process.py:1040-1045` 退出排空 `shutdown(wait=True, cancel_futures=False)` 无时限。总结卡住 = 退出卡住。

### 调度器
- **P2-19 wakerflow 防重入只护 submit 不护运行期**：`wakerflow/scheduler.py:168-193` finally 在 submit 后立即释放键（对照 waker/scheduler.py:196-211 在完成回调释放）——interval 5min、运行 10min 的 flow 周期性并发重入，`run_count` 读改写丢更新。
- **P2-20 flow 任务经 argv 传子进程，Windows 32k 上限 spawn 失败**：`executor.py:516-526` `{{steps.x.result}}` 渲染链可达数万字符 → `WinError 206` → 节点笼统报错。修：改 stdin/临时文件传递。
- **P2-21 canvas 建块绕过调度校验 → flow 永久哑火**：`canvas.py:99-114` schedule 原样灌入（对照 parser.py:130-176 严格校验），`wakerflow.py:302-320` 保存不回读 parse_flow——`schedule_type:"weekly"`/负 interval 入库成功，tick 每次 parse 失败吞 warning，永不触发且无 UI 提示。
- **P2-22 ask_user 默认 24h 轮询占死 2 线程池**：`executor.py:811/853-878` + `app.py:131` `max_concurrent=2` 硬编码——两个没人理的审批冻结全部后续 flow 最长一天，无取消机制。

### 前端
- **P2-23 wakerflow 审批按钮 onclick 双重解码 JS 注入**：`wakerflow.html:1193-1195`——`esc()` 把 `'` 转 `&#39;`，但事件处理器属性先 HTML 实体解码再编译 JS，字符串照常闭合；`o.value` 无字符集校验（parser.py:370-375），经"从 YAML 导入"可达。
- **P2-24 config.html MCP 服务名裸拼注入**：`config.html:85-89` `${s.name}` 内容位 + `onclick="toggleMcp('${s.name}',...)"` 完全未转义；名来自用户粘贴的 MCP JSON（"照网上 Claude Desktop 格式粘贴"是代码注释自认的产品场景），落盘后每次进设置页即注入。

### 工程质量
- **P2-25 waker 更新接口跳过全字段校验**：`routers/waker.py:168-186` 仅 create 调 `validate()`，PUT 可写入 `schedule_type:"weekly"`/`interval_minutes:0` → 该 waker 在 `compute_next_run` 静默返回 None，永不调度无任何反馈。
- **P2-26 线上事故回归测试被装饰器错位静默跳过**：`tests/storage/test_hybrid_retrieval.py:207-209` 的 skipif 因中间隔注释块实际落在 216 行的另一个测试上——真 smoke 测试（:278）反而无门控，每次跑测试无条件触发 ~100MB 模型下载。

---

## P3

1. `search()` 是 sqlite_provider 全文件唯一不持模块锁的方法（:489-584），与同连接写事务交错可读瞬时不一致；无崩溃无损坏。
2. `replace_all`/首嵌在 `BEGIN IMMEDIATE` 事务内做全库 embedding（:665-696、:476-485）→ 跨进程写者 5s 后 BUSY 丢记忆；出厂 `auto_consolidate=False` 时不可达，手动聚合与 worker 写入重叠时可达。
3. summary 路由不水合目标会话，可把 A 会话消息总结记到 B 名下（sessions.py:137 + worker_process.py:623-627）；当前前端无调用者，仅手工/CSRF 可达。
4. wakerflow `run_id` 未过 `_safe_component`：`%2F` 穿越被 Starlette 路由挡住（404），但 `%5C` 反斜杠在 Windows 实测可穿越；读侧只回显统计，写侧仅对已存在合法 JSON 追加四键，危害有限。`FlowStore.approval_path`（store.py:93-95）补上校验即可对齐同文件 `run_jsonl_path`。
5. `tool_loop_threshold` 三方矛盾：出厂模板 3、agent 注释声称"config 默认 5"、测试桩自造 `_Cfg(5)` 庆祝"新默认救活误杀"——代码真实兜底就是 3，出厂即偏严误杀行为。
6. `request_human_approval` 在 full_access 下无人审批即回"已授权"，与 YAML 描述承诺矛盾（human_approval.py:32-35）。
7. 会话文件名不拒 Windows 保留名（con/nul）与超长名（session_store.py:232-234；summarizer.py:59-61 完全无校验）。
8. `_atomic_write_json` 无 fsync（session_store.py:314-334）：断电时 NTFS 只日志化元数据，"最多丢本次更新"注释偏乐观。
9. `_migrate_old_sessions` 中断重跑产生重复会话（session_store.py:211-219，`.bak` 已存在时 rename 抛错被吞）。
10. 灯箱 keydown 监听器泄漏（chat.js:144-146，非 Escape 关闭即累积）；busy 自动重试 2s 空窗 `isStreaming=false`（chat.js:770-812，可双发消息抢槽）。
11. 定时触发的 waker 不进 RunRegistry（async_runner.py:207-232），前端运行态不可见；`WakerStore.update` 整体写回与 `save_state` 竞态可回退 `run_count/next_run_at`（store.py:195-202）。
12. wakerflow jsonl 双 `flow_end`（runner.py:209-212 + executor.py:393-401）；returns 渲染失败静默置空（executor.py:385-389）。
13. highlight.min.js 是 405 字节空操作 shim，全仓无 CDN 加载也无 `hljs.*` 调用——代码高亮功能实际不存在（base.html:11 仍引入）。
14. 文档漂移：README.en.md 落后中文版一个能力代差（CLI 命令表缺 6 条、对比表三处结论相反）；workspace.py:11 docstring 仍声称"全部要求登录（签名 cookie）"（免认证已是现状）；architecture.md 进程拓扑停留在单 worker 时代。

---

## 误报剔除清单（复核证伪，后续审查勿再报）

| 声称 | 证伪理由 |
|---|---|
| `client.py:355` `chunk.choices[0].delta` 未判空会崩 | :353-354 已判 choices 空；delta=None 时三处 `hasattr` 全防御，无可触发崩溃 |
| app.js:91 waker 下拉 innerHTML 未转义 = 存储型 XSS | `<select>` 的 fragment 解析处于 in-select 插入模式：img/svg 被忽略、`</select>` 终止解析、script 经 innerHTML 从不执行——构造不出 JS 执行，仅 UI 渲染破坏 |
| home.js:33 项目路径文本注入 | 触发要求 path 含 HTML 元字符**且项目真实存在**；Windows 文件系统禁止 `<>` 字符，不可达 |
| wakerflow `if_cond` 求值失败默认执行是缺陷 | executor.py:1004 docstring 明示 fail-open 取舍，parse 期已校验 steps 引用；文档化设计 |
| `expire_at` 经 `%2F` 类编码穿越读 run jsonl | 实测 starlette 1.0.1：uvicorn unquote 后 `scope["path"]` 含 `/`，路由 `[^/]+` 不匹配 → 404 |
| tool_loop_threshold "代码默认 5 被配置打回 3" | 代码 getattr 兜底本来就是 3，"5"只存在于注释与测试桩；真问题是三方矛盾（已列 P3-5） |

---

## 修复优先级建议

1. **P0-1/P0-2（shell 分类器）**：一处模块内可完成（分隔符 + `env`），修完 shell 防线才算立起来；P1-1/P1-2（黑名单 + 白名单形态）紧随。
2. **P0-3 + P1-8（前端消毒）**：一次 PR——escapeHtml 补引号、消毒判定修边界、7 份手抄转义函数收敛为 1 份公共资源。
3. **P1-3 + P1-4（registry 异常路径）**：InterruptSignal 在 `_run_tool` 放行 + 批量中断携带已完成结果 + 配对不变量测试。
4. **P1-6 / P1-7（向量一致性 + tick 隔离）**：都是几行级修复，影响面却是静默数据损失与调度全停。
5. **P1-5（Host/Origin 中间件）+ P2-11/12/13（async 路由与 spawn 出锁）**：Web 侧一次性收拾。
6. 其余 P2 按模块顺手清；P3 与文档漂移安排在功能开发间隙。
