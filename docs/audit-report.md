# 现状审计报告 —— 记忆管理模块

> 审计方式：只读通读源码 + DDG 行业调研（2026-08-27）。
> 涉及 `src/memory/`（约 2,300 行）、`src/storage/`、`web_fastapi/routers|templates`、`web_fastapi/worker_process.py`、`src/agent/`。
> 行号说明：标注 file:line 为**审计时点**行号；其中本次开发分支已修复的项在 §6 标【已修复】。
> 本报告只描述现状与风险，不含解决方案；优化取舍留给下一步 /dev-adr。

## 1. 执行模型复述（链路 + file:line）

Web 入口：`web_fastapi/main.py:79-83`（uvicorn factory）→ `web_fastapi/app.py:235` create_app → 路由注册 `app.py:244-256`（memory=/api/memory）。Agent 执行全在 worker 子进程，进程内纯同步+threading；主进程是 asyncio loop + 多个后台线程。

**链路 A｜页面读改记忆**：memory.html fetch → `routers/memory.py:55-64` 调 `worker.send("memory_get"/"memory_clear")` → `worker_manager.py:91-139`（抢 per-worker 锁，lock_wait=5s）→ 子进程串行主循环 `worker_process.py:943-953` → op 分发 `:450-455` → `MemoryManager.get_all/delete_all`（manager.py:244-265）→ SQLiteProvider（sqlite_provider.py:268-286）。
本次新增的单条 PUT/DELETE `/api/memory/{id}` 与聚合/备份一样走主进程直连存储（routers/memory.py `_memory_manager()`），不经 worker IPC。

**链路 B｜对话中自动存取**：
- 取：POST /api/chat/stream（routers/chat.py:34-49）→ worker "chat" op（worker_process.py:691-757，设 contextvar :713-715）→ ReAct 首步置 memory_pending（agent_v3.py:753-758）→ pre-step 监听器（agent_v3.py:240-260）→ retrieve_with_detail（memory_orch.py:37-84）→ search_with_detail（manager.py:201-240，limit/min_score 取 config.yaml :218-220）→ **get_all 全表 + Python 线性打分**（sqlite_provider.py:246-251, 211-227）→ 注入 system prompt（agent_v3.py:771,785），SSE 发 memory_search 事件（前端 toast：chat.js:659-663）。
- 存主路径：LLM tool_call remember → tools/remember.py:81-106 → remember_fact（manager.py:135-160）→ search_candidates 5 条候选（manager.py:151）→ MemoryDecider 一次 LLM 决策（decider.py:37-87）→ _apply_decision upsert/delete（manager.py:98-131 → sqlite_provider.py:231-242）。

**链路 C｜会话总结兜底**：session_reset op 先 ack 再 fire-and-forget（worker_process.py:833-841）→ 单线程池 bg-summary（:93,177-191）→ on_session_end（session_lifecycle.py:63-163，120s 超时包装）→ Summarizer 两步写（summarizer.py:93-167）：整段总结存一条 source="session_summary"（:129-135）；原子事实逐条 extract+decide 的**串行 for 循环**（manager.py:183-190，TODO 自认性能问题）。

**链路 D｜聚合并发旁路**：app lifespan 常驻调度器 tick 每 300s（app.py:183-195 → scheduler.py:85-92）；自动门控 = auto && elapsed≥interval && count>threshold，每次 tick 新建独立 MemoryManager 只为数数（scheduler.py:159-168）；手动/自动都在**主进程**直跑 LLM 合并（consolidator.py:52-102）后 replace_all 单事务重建表（sqlite_provider.py:290-340）。

## 2. 全局状态与进程边界

| 对象 | 定义处 | 重启丢失 | 多进程后果 |
|---|---|---|---|
| `_LOCK` 模块级 RLock：同进程所有 Provider 实例（含不同库文件）全部互相串行 | sqlite_provider.py:50 | 是 | 跨进程仅靠 WAL+busy_timeout=5000 兜底（:159-160） |
| config_store 连接缓存不显式 close，WAL 句柄残留至进程退出 | config_store.py:44-54 | 是 | 各进程一份连接；与 kv RMW 叠加见 §3 |
| remember 工具的 manager 单例 dict + ContextVar，不跨线程池传播 | remember.py:22-32（注入点 agent_v3.py:194-196） | 是 | 同进程构造第二个 agent 会静默覆盖前者的 manager |
| worker 内 bg-summary 线程池(max=1)；被 reaper 杀掉时未跑完的总结直接丢 | worker_process.py:93（仅优雅 shutdown 排空 :901-906） | 是 | 总结丢失＝该会话事实不沉淀 |
| WorkerManager._workers + reaper 线程；user_id 一律归一 LOCAL_USER | worker_manager.py:255-286 | 是 | 双 uvicorn worker 会各起一套 scheduler/reaper/worker 子进程（注：reaper 已于审计后分支移除，worker 常驻） |
| RunRegistry：内存 dict 写穿 kv 同一 key，状态每次变更重写整个列表 | run_registry.py:72-78,190-199 | 部分 | 两进程内存副本互相覆盖丢运行记录 |
| app.state（cordis_ctx / 调度器 / worker_manager）lifespan 挂载 | app.py:79-195 | 是 | 多 worker 时自动聚合双重触发竞争同一 hermes.db |

## 3. 单执行者假设清单

1. **聚合长窗口读-改-写**：快照→LLM(~20s)→replace_all。原实现 protect_outside 只保护"新增 id"，窗口内对既有行的 UPDATE 被旧快照合出的内容静默覆盖（consolidator.py:63-86 + sqlite_provider.py:303-336 审计时点）。【已修复：baseline 行级版本守卫 storage/base.py apply_baseline_guard，两个后端共享】
2. **PUT /consolidate/config 无 CAS**：load→改字段→save 整字典（routers/memory.py:112-121 + config_store.py:114-118），任一并发即整字典 lost update（kv_put 无 CAS：sqlite_provider.py:480-487）。
3. legacy yaml 首载 check-then-write（config_store.py:95-103），双进程同时首载竞态双写。
4. FileMemoryStore 整文件 RMW 仅进程内锁（file_store.py:113-226；非默认后端，自述供测试/迁移）。
5. async def 里同步等 worker 锁最长 5s：卡死事件循环（routers/memory.py:55-64 及 config_router 同款四处）。【已修复：六处改同步路由走线程池】
6. 检索 O(N)：search 先 get_all 全量再线性打分（sqlite_provider.py:246-251），每轮 chat 首步都发生。
7. 迁移脚本一次性串行循环（scripts/migrate_to_sqlite.py:96-102,156,180，靠 .migrated 改名幂等，手动执行非启动期）。

## 4. 失效配置清单

1. **prefs 热更通道的两个键死了**：`memory_min_score`/`max_memory_results` 有五处定义面（models.py:35-36、config_service.py:11-15 HOT_RELOADABLE_KEYS、worker_process.py:299-300 与 :577-582 回写回显、config.html:12-13 输入框），消费面为零——全仓 grep 无 prefs 读取；chat 只下发 temperature/max_tokens/compact_threshold_pct（worker_process.py:706-708）。设置页改这两项**永不生效**；真实生效值来自 import 时 lru_cache 的 config.yaml（config.py:47 + manager.py:218-220）。
2. `.env` 七键无任何 loader 加载（仓库无 python-dotenv 引用）：MAX_MEMORY_RESULTS/MEMORY_MIN_SCORE（且 .env 的 0.7 与 config.yaml 的 0.4 互相矛盾）、EMBEDDING_PROVIDER/MODEL/BASE_URL、QDRANT_HOST/PORT/COLLECTION（.env:12-26；Qdrant 已移除见 health.py:5 与 tests/test_memory/test_no_qdrant.py）。
3. MemoryConsolidationScheduler(workspace_root=…) 保参废弃（scheduler.py:50-56 注释自认）；FileMemoryStore 各方法的 user_id 形参拍平无效（file_store.py 模块 docstring）。

## 5. 无界增长清单

实测 data/hermes.db：memories=3 条 vs events=1388 条——比例本身即证据。
- **memories 表**：插入面广（会话总结每次必插一条 summarizer.py:129-135；remember ADD manager.py:102-108），删除面窄（Decider 概率删 manager.py:122-128、手动清空、replace_all），自动聚合默认关（config_store.py:35-40），无 TTL/cap。【页面已补逐条删除入口；总量治理策略待定】
- **snapshots 表**：滚动保留 10 个只在 replace_all 后触发（sqlite_provider.py:337-348），restore 插 pre-restore 快照但从不清理（:388-409 审计时点）。【已修复：恢复路径同入滚动窗口 + 回归测试】
- **events 表**：append-only，唯一删除入口 purge_events（sqlite_provider.py:459-467）仅手动 session_delete 触发。
- 文件类：data/sessions/*.json 只增不删且列表接口全量 glob 解析（session_store.py:414-430）；data/summaries/**.md 无清理逻辑（写入点 cli.py:1040-1043）；FileMemoryStore .bak.{ts} 无保留上限（file_store.py:228-241，与 SQLite 侧 KEEP=10 不对称）。
- 内存：worker _buckets 按 session_id 只增不减（worker_process.py:306-317），原靠 30min reaper 释放（该机制已移除，现为常驻至进程退出）。
- 有界对照（不算问题）：RunRegistry MAX_RECORDS=200（run_registry.py:42）、snapshots KEEP=10。

## 6. 风险排序（出事概率 × 爆炸半径）

1. **备份恢复功能确定性损坏**：前端读 b.mtime/b.name，后端返回字符串数组 → Invalid Date + 恢复发字面量 "undefined" 必 404，唯一数据回退手段失效（memory.html:161-166 vs sqlite_provider.py:350-356 审计时点）。【已修复：契约统一 [{name,mtime}] + 事件委托渲染】
2. **async 阻塞事件循环**：chat 流式期间锁被长期持有，打开记忆页整站冻结 ~5s 或 503（routers/memory.py:55-64、worker_manager.py:102-106 注释自证）。【已修复：6 处路由改同步】
3. **记忆只增不删 × O(N) 检索**（未修）：时间越久注入上下文越脏、首 token 越慢；页面逐条删除只是缓解，上限/TTL/聚合频率属产品决策。证据链见 §5 第 1 条。
4. **聚合窗口静默覆盖并发 UPDATE**（consolidator.py + replace_all 审计时点）。【已修复：baseline 行级守卫，ADD/UPDATE/DELETE 三种交错均有专门单测】
5. **restore 导致 snapshots 无界膨胀**。【已修复】
6. **设置页死配置**（§4.1，未修）：用户在 UI 改参数毫无效果，属"静默失信"；接线或下线留 /dev-adr。
7. 页面体验类遗留（未修）：escapeHtml 双实现且 chat.js 弱版（不转义引号）用于 innerHTML（chat.js:498-500 vs memory.html 强版）；1500ms 轮询遇持续网络错误永不清除（pollStatus catch 分支）。
8. CLI 在 /exit 触发总结而 Web 靠 session_reset 触发，触发时机不一致（cli.py:1036-1044 vs worker_process.py:839-841），是否统一待确认。
9. **检索阈值落在数学失效区**：score = 0.5 + 0.5×(查询词覆盖率)，任一单字命中即 >0.5（sqlite_provider.py:211-227），而出厂默认 `memory_min_score=0.4` 低于这个硬底 → 阈值结构性无效，每轮对话全部候选必然通过（toast 恒显示"N/N 全中"）。另发现同会话总结重复落库：Summarizer 随机 uuid 主键无幂等 + 空总结照单入库（summarizer.py:129-135 审计时点）。【均已修复：默认阈值提至 0.7≈要求覆盖 ≥40% 查询词并对 ≤0.5 配置告警；总结改固定 id 幂等覆盖 + "无可沉淀"标记句直接跳过存储 + worker 进程内按消息数去重】

## 7. 待人工核对的 3 条抽查项

1. 启动服务打开「记忆」页：点“↩️ 恢复备份”应看到正常日期列表；选一个备份恢复不再 404（验证修复 1，对照修复前的 Invalid Date/"undefined"）。
2. 让 agent 生成一段长回复期间刷新 /memory 与 /config 偏好区：不应整体冻结或报“AI 正在思考”503（验证修复 2）；顺手把设置页 min_score 改值再对话，确认确实不生效（复核死配置 §4.1）。
3. 连续两次“恢复备份”后查库：`SELECT COUNT(*) FROM snapshots WHERE kind='memory'` 应恒 ≤10（验证修复 5）；同时观察 memories/events 行数增速，评估风险 3 是否需要尽快排期。

## 附：行业做法对标（DDG 调研）与本项目差距

调研对象：ChatGPT（Settings→Personalization→Manage memories）、Mem0（CRUD/batch_delete/filter delete_all/mem0-studio Explorer GUI）、Letta-MemGPT（core memory block UI 直编 + archival 分层 + read_only 标记）、星火智能体平台/GPTBots 中文产品、PingCode 记忆机制设计文章。

| 能力 | 业界通行做法 | 本项目现状与差距处理 |
|---|---|---|
| 逐条管理 | 单条查看/编辑/删除是标配 | 原本只有全量 GET/clear-all。【本次补齐：PUT/DELETE /api/memory/{id} + 页面 ✏️ 编辑/🗑️ 删除】 |
| 元数据展示 | 每条带类别/来源/最近更新时间（星火即如此） | schema 有 source/created_at/updated_at 但页面从未展示。【本次补齐：来源徽标 + 相对时间列】 |
| 查找筛选 | 关键词搜索 + 来源/类别筛选（Mem0 Explorer、星火） | 原本无搜索无筛选。【本次补齐：实时搜索 + 来源筛选 + 排序 + 可点击统计标签】 |
| 危险操作分区 | 清空类操作与日常操作分离 | 原本四个按钮平铺一排。【本次补齐：清除全部收进独立危险区 details】 |
| 总开关 | ChatGPT/星火支持整体关闭记忆 | 无此能力（只有自动聚合开关）——涉及 agent 运行时行为变更，留 /dev-adr |
| 记忆分层可见 | Letta 区分 core/archival 且 block 可直编 | 会话总结与原子事实混铺一列（summarizer.py 两步写的产物），是否分层展示待确认 |
| 召回透明度 | “为什么记住我”可解释、可对话式纠正（forget X） | Web 端召回仅 toast 一闪（CLI 反有完整面板 cli.py:820-858），未修 |

一句话结论：本次开发分支消除了全部“功能性损坏”级风险（第 1/2/4/5 条）并补齐页面逐条治理能力；剩余风险集中在**增长治理策略（风险 3）、死配置处置（风险 6）、召回透明度与总开关**三类产品决策上——正是 /dev-adr 该回答的问题。
