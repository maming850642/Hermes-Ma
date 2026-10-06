# 现状审计报告

> 审计目标：历史会话无法加载。范围：会话持久化/加载全链路（前端 → 路由 → IPC → worker → 存储层）。
> 只读审计，未改任何业务代码。所有路径相对仓库根。

## 1. 执行模型复述（链路 + file:line）

一次"点击侧边栏历史会话"的完整链路：

1. 前端 `chat.js:930` `POST /api/sessions/{id}/load`；仅 503 会被 alert 拦下，**其余任何状态码（含 500/400）都直接 `location.reload()`**（`web_fastapi/static/js/chat.js:930-935`）。
2. 路由 `sessions.py:78-88`：`validate_id` 校验（拒 `/ \ .. : .开头 空字节`，`web_fastapi/security.py:19-27`）→ `worker.send("session_load", lock_wait=5s)`。
3. IPC：主进程拿 worker 锁写 stdin NDJSON，等 stdout 响应（`web_fastapi/worker_manager.py:91-139`）。错误类响应在此被抛成 `RuntimeError`（`worker_manager.py:129-130`），**不作为 events 返回**。
4. worker 主循环串行取命令，分发 `_op_session_load`（`web_fastapi/worker_process.py:806-825, 943-949`）。
5. 加载本体 `load_session_events_first`（`src/session_store.py:364-405`）：SessionLog 有事件 → `derive_messages` 投影；无事件回退 JSON `load_session`（`src/session_store.py:321-361`）。**投影为空时 worker 回 error「会话不存在或为空」（`worker_process.py:815-817`）**。
6. 成功则写入内存桶 `state.set_current(sid)` 并只返回 `{ok, message_count}`——历史内容由前端 reload 后调 `GET /api/sessions/current` 再拉（`worker_process.py:459-463`）。

发送消息的写链路：每轮 chat 结束后 `state._save_bucket(bucket)` 整文件覆盖 JSON 快照（`worker_process.py:756`, `src/session_store.py:244-318`）；同一轮内 durable 事件由 agent 循环写入 SQLite events 表（scope=chat，`src/agent/session_log.py:109-128`）。JSON 与事件流双轨并行生长。

## 2. 全局状态与进程边界

| 状态 | 位置 | 进程边界 |
|---|---|---|
| 会话桶 `_buckets`（messages/todos/vfs/waker 内存态） | `worker_process.py:288` | worker 重启全丢，只剩磁盘快照；多标签页共享一份 |
| `_current_sid`、permission_mode、prefs | `worker_process.py:289-302` | 重启回默认：新随机 sid + `before_changes` + prefs 复位 |
| InterruptStore 恢复件 | `worker_process.py:392-397` | 仅启动期从事件库重建一次 |
| MCP 连接 / skills / 工具注册表 / workspace service | `worker_process.py:384-399`, `src/workspace/state.py:21` | 各进程各持一份，靠同一 SQLite 对齐 |
| WorkerManager `_workers`、空闲回收线程 | `worker_manager.py:256-279` | 主进程独有；30min 无活动即杀 worker（`:32`） |
| `app.state.cordis_ctx`（主进程 SessionLog/storage 实例） | `app.py:88-91`; `sessions.py:103-119` | 与 worker 是**两个进程各自的 SQLite 连接** |
| `SQLiteProvider._LOCK` 进程级 RLock | `sqlite_provider.py:50` | 只保护进程内并发，跨进程仅靠 WAL+busy_timeout 兜底 |
| contextvars（user/vfs）、stdout 泵、命令队列 | `tools/remember.py:22`, `virtual_fs.py:36`, `worker_process.py:62-111` | 进程内 |

多实例部署必然坏点：整个 Web 层假设全局唯一 worker + 同机文件系统（SESSIONS_DIR 固定 `PROJECT_ROOT/data/sessions`，`session_store.py:38`）；所有 user_id 归一为 LOCAL_USER（`worker_manager.py:286`, `constants.py:13`）。重启丢失 = 未落盘的桶内消息（事件库可救回大半，但 todos/vfs/waker 只活在 JSON 快照）。

## 3. 单执行者假设清单

1. **worker 主循环串行**（`worker_process.py:943-949`）：chat 进行中锁被占最长 300s（`worker_manager.py:36`），`session_load` 仅等 5s 即 503（`worker_manager.py:103-106`）→ **AI 回复期间点任何历史会话都是"加载不了"的一种形态**。
2. **覆盖式整文件保存的 check-then-act**：`save_session` 先读旧 created_at/name/waker 再整文件重写，非原子、非事务（`session_store.py:279-316`）。跨进程有两个写方：fork API 在**主进程**直接 `save_session(LOCAL_USER,...)`（`sessions.py:213-214`），worker 轮末也写同 sid 文件——并发下互覆。
3. **worker 重启后的盲写**：前端仍持旧 sid 发消息，`get_bucket` 自动建空桶（`worker_process.py:306-317`），轮末 `_save_bucket` 用残缺消息**覆盖同名历史快照**（`worker_process.py:756`）——真实历史被截断的数据丢失路径。
4. 启动期一次性清扫：pending 中断恢复只在 worker init 时跑一次（`worker_process.py:392-397`）；旧格式会话迁移 `_migrate_old_sessions` 有实现但全仓无调用方（`session_store.py:148-217`）。
5. stdout 常驻单读者泵假设行不可分片（`worker_manager.py:43-68`）；fork 的查重-生成是 check-then-act，撞车靠 5 次重试+409（`sessions.py:153-170`）。
6. `list_sessions` 逐文件裸读，读到半截 JSON 就静默塞一条空记录（`session_store.py:431-439`），与 2 的非原子写互相放大。

## 4. 失效配置清单

- `web_fetch_max_chars`：`config.example.yaml:53` 声明，全仓唯一消费点是函数默认参硬编码 8000（`src/tools/web_fetch.py:249`），settings 从未被读取。**死配置**。
- 其余抽查键均有消费者（经 settings 属性访问）：compact_keep_recent（`src/agent/context.py:217`）、tool_loop_threshold、sub_agent_default_timeout、web_search_timeout 等。
- 待确认：`max_short_term_messages` 仅 1 处引用（上下文窗口测算），是否真参与裁剪未逐行核。

## 5. 无界增长清单

1. **JSON 快照只增不减**：本次实测 `data/sessions/local/` 已 184 个文件，其中 **177 个 messages=[] 且全部产生于 2026-08**。来源链：每次 worker 启动建一个随机 sid 空桶（`worker_process.py:289-290`）、reset 也新建空桶（`worker_process.py:828-841`），退出时 `save_all_buckets` 把空桶照样落盘（`worker_process.py:424-430` → `session_store.py:299-318`）。这些幽灵会话全部出现在侧边栏，点击必然失败——是"历史会话无法加载"的最大可见面。
2. **events 表无 TTL**：purge 只有显式删除会话一条路径（`session_log.py:130-140`, `sqlite_provider.py:459-467`）；interrupt scope 的 resolved 事件永不清理（`session_log.py:238-267`）。
3. worker 内存 `_buckets` 随访问只增不清（`worker_process.py:288,306-317`）。
4. snapshots 表有界（keep=10，`sqlite_provider.py:44-46,337-339`）✅；logs 滚动 ✅（对比 logs 目录存在 .2026-08-18 分卷）。

## 6. 风险排序（出事概率 × 爆炸半径）

1. **加载失败的错误信号被三层吞掉**：worker error→RuntimeError→路由死代码 `events[0]["type"]=="error"` 判不到（`sessions.py:86-87` 对比 `worker_manager.py:129-130`）→变 500→前端除 503 外一律 reload（`chat.js:930-935`）。用户看到的是"点了没反应"，故障不可观测。（概率高·半径中）
2. **177/184 快照为空的幽灵会话持续繁殖**：空桶落盘机制（第 5 节 1）让侧边栏充满永远打不开的条目，且污染 list 排序与心智。（概率已发生·半径中）
3. **worker 重启盲写覆盖真实历史**（第 3 节 3）：一旦发生不可逆丢历史；尚未捕获现行证据，属最高危潜在项。（概率低频·半径大）
4. AI 回复期间点击历史会话必 503：锁等待 5s vs chat 最长 300s（`worker_manager.py:36,103`），体验上就是"无法加载"。（概率高·半径小）
5. fork API 主进程直写快照与 worker 写同文件竞态（`sessions.py:213-214`）；Windows 双进程 WAL 下另有 busy_timeout 5s 后直接抛错的可能（`sqlite_provider.py:160`）。（概率低·半径中）

## 7. 待人工核对的 3 条抽查项

1. 打开浏览器 DevTools Network，点一个具体打不开的历史会话，看 `POST /api/sessions/{id}/load` 的真实状态码（预期见 500「会话不存在或为空」或 503），确认风险 1/4 归因。
2. 挑一个侧边栏里你认为"应该有内容"的会话 id，核对 `data/sessions/local/{id}.json` 的 `message_count` 是否为 0（我实测 184 个里仅 7 个非空，且这 7 个离线模拟均可正常 load）。
3. 核对 GitHub 历史/备份里被覆盖前的快照是否存在"曾经非空、现在 messages=[]"的文件——验证风险 3 的覆盖丢失是否已经发生过（数据目录不在 git 内，只能外部取证）。
