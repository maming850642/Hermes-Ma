# Cordis 重构落地指南（rollout）

> **⚠️ 历史快照**：本文是 Cordis 重构期的过程记录。文中"首次进入页面被门禁跳到 /workspace 引导页"
> 的 workspace 门禁已随 ADR-0005 D4 删除；"get_settings() 41 处引用"的数字也已过时（现约 96 处）。
> 现状以根 README 与 docs/architecture.md 为准。

> 本文面向部署/使用：怎么升级、迁移数据、双模式怎么用、哪些行为变了。项目现状以仓库根 README 为准；本文专注升级与迁移。

## 一、升级步骤

```bash
git pull                      # cordis 分支
pip install -r requirements.txt   # langgraph/langchain 系已移除，可 pip uninstall 清理
python scripts/migrate_to_sqlite.py --dry-run   # 先看迁移计划
python scripts/migrate_to_sqlite.py             # 确认后执行（幂等可重跑；多旧用户目录时加 --user <uid>）
python -m web_fastapi.main    # 启动 Web
```

迁移脚本做四件事（源文件改名 `.migrated` 保留不删）：
1. 旧 `users/<uid>/` 内容拍平进 `data/home/`（wakers/wakerflows/projects/persona）
2. `profile.md`（JSONL）→ SQLite `data/hermes.db` memories 表（打分/召回不变，有等价性测试）
3. `memory_config.yaml` → kv
4. 多旧用户目录时必须 `--user` 指定其一（单用户决策）

**不迁移也能跑**：sqlite 会从空库开始（记忆为空），旧会话 JSON 仍可直接加载（兼容两种格式）。

## 二、目录与进程（新基线）

```
data/
  hermes.db        # SQLite（WAL）：memories / events(会话事件流) / kv / snapshots / runs
  sessions/        # JSON 快照（UI 列表/预览；消息内容已事件优先）
  summaries/  uploads/  mounts/   # 会话总结 / 上传文件 / zip 解压工作区
  home/            # agent 家目录（未挂载 workspace 时 fs 工具的根）
    wakers/  wakerflows/  projects/
config.yaml        # LLM/端口等（workspace_root 键已废弃）
cordis.yaml        # 插件组合清单（8+2 插件行）
```

进程：FastAPI 主进程（cordis ctx + 调度服务 + router 直读 sqlite）＋ 每 turn 动态重绑工具的 worker 子进程 ＋ waker/flow 节点子进程（共用 `src/ipc.py` 样板）。旧"串台隔离三道闸"概念废除——单用户（恒 `local`），登录仅作 Web 端口安全边界。

## 三、Workspace 双模式（WebUI）

首次进入任何页面会被门禁跳到 `/workspace` 引导页，三选一：

| 模式 | 说明 | agent 工具面 |
|---|---|---|
| 挂载本地文件夹 | 输入绝对路径（须存在/可写；拒挂 hermes data 目录、系统目录、盘符根） | 全量 16 工具 + MCP；fs/shell 根=挂载目录，路径守卫拦截 `..` 逃逸 |
| 上传文件夹 | .zip（默认 ≤200MB，可配 `workspace_upload_max_mb`），解压成托管工作区 | 同上 |
| 仅对话 | 纯聊天 | 仅 write_todos / compact_conversation（可配 `workspace_chat_only_tools`） |

- 挂载状态存 DB（跨进程一致）；切换/卸载后**下一 turn 即生效**（工具每 turn 重解析）
- 边界如实声明：无 OS 级沙箱；约束=工具层路径守卫 + before_changes/plan 权限模式 HITL 审批；shell 仅 cwd 锚定，理论上可 cd 逃逸（bash.yaml 已注明）
- waker 的 tools 白名单=配置内允许列表；纯对话模式下需要 fs 的 waker 任务会因工具缺失失败（预期行为）

## 四、行为变化清单（相对旧版）

| 变化 | 说明 |
|---|---|
| 会话消息以事件流为准 | `session_load` 优先从 SQLite 事件投影（derive），无事件回退旧 JSON；`GET /api/sessions/{id}/events` 可看全量事件流，`POST .../fork` 可从任意事件点复制分支 |
| HITL 审批重启不丢 | 中断快照落 `interrupt/requested` 事件，worker 重启自动恢复待审批 |
| CLI 换引擎 | CLI 与 Web 同用 HermesAgentV3（手写 ReAct + 事件化循环）；LangGraph/langchain 依赖全移除 |
| 记忆/配置后端 | SQLite（consolidation 单事务 + snapshot 行备份，`.bak` 文件舞蹈退役；backups API 面不变） |
| waker/flow/记忆三调度器 | 共用统一调度服务（单守护线程 + 每注册项线程池 + 防重入）；运行登记表入库，重启后 running→interrupted 可见 |
| 登录 | 单用户（用户名输入已移除），无 /api/auth/switch |

## 五、保留决策与已知事项

- `get_settings()` 单例保留为**进程内实现细节**（41 处引用未强行收口，避免大 churn）；插件面统一走 `ctx.config`。后续若要多配置源再做收口。
- 会话 JSON 快照仍会写（列表/预览用），消息真源是事件流——双轨是有意的过渡态。
- 存量测试失败 4 个（基线）：test_bus 跨进程时序、test_tool_schema×2（工具数 17→16 的过期断言）、test_wakerflow_api 1 项时序；另有偶发 flaky（1/3 概率的第 5 失败）。均与重构无关，待单独清理。
- 文档对齐状态：现状以仓库根 README 为准；升级与迁移事项以本文为准。

## 六、后续候选（未做）

架构层：cordis.yaml 之上的 `data/patch.yaml` 单层覆盖；waker/flow run jsonl 迁 events 表（R3 已把 waker/flow 会话事件隔离进 scope="waker"，run 级 jsonl 仍在文件系统）；Trajectory 完整 UI（事件流时间轴/过滤）；`get_settings` 引用面收口；OS 级沙箱（如 Windows AppContainer / landlock）。（thinktank 已于 2026-08-16 整体退役。）

R1-R3 深度 review 后仍开放的 P3 项（已知、暂不修）：

- **CLI "approve:xxx" 前缀语义分歧**：CLI 把整段输入当 resume_payload 透传，`_resume_decision_reason` 按首段切 decision——`approve:备注` 会被当 decision="approve"（备注丢弃），与 `reject:原因` 的语义不对称；统一需要 CLI 输入协议改造。
- **shell timeout=0 无上限**：run_shell 的 timeout 配置为 0 时表示不限时（文档未显著声明）；极端情况下卡死任务只能靠 chat_stop/进程重启兜底。
- **CON/NUL 保留名与 NTFS ADS**：fs 工具的文件名校验未封 Windows 保留设备名（CON/PRN/AUX/NUL/COM1…）与备用数据流语法（`a:b`）；路径守卫 + `:` 封禁（R3）已挡大部分，但 create/write 直接落保留名仍会得到晦涩的 OS 错误。
- **8.3 短名 TOCTOU**：路径守卫的 realpath 包含检查与工具实际 open 之间存在窗口（检查后文件被替换成短名/链接）；无 OS 沙箱下的固有边界，靠 HITL 审批兜底。
- **fork-of-fork 链**：fork 可对 fork 出的会话再 fork（事件流逐条复制），多代后事件体积线性放大；R3 已做 sid 碰撞重生成与 64KB tool/result 截断缓解，代数上限未设。
- ~~**thinktank employee_worker 同型门禁缺口**~~：已随 thinktank 整体退役消除（src/thinktank/ 删除，projects.py 迁出为独立模块）。
- **workspace state 多 boot 串扰**：`src/workspace/state.py` 的进程内单例假设"每进程一次 boot"；若同一进程先后 boot 两个 Context（测试/嵌套场景），后者的 set_service 会覆盖前者。单 boot 模型下不触发。
