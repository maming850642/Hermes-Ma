# hermes_ma 结构治理路线图

> 2026-09-10 制定。合并两份评审（内部三路结构调查 + 外部 Grok 评审，后者硬主张已逐条核实）。
> 基线判断：**分层清晰、体积型债务**——不是大泥球；真正的大债是"双轨真相源"与几个上帝文件。

## P0 工程基线（2026-09-10 本轮已完成）

- [x] 依赖统一：pyproject.toml 补齐 11 个实际在用包（fastapi/pyyaml/jinja2/fastmcp/sqlite-vec/fastembed 等）；
      删除 3 个零引用死依赖（pydantic-settings、python-dotenv、itsdangerous）；requirements.txt 注释对齐 SQLite 现状
- [x] 最小 CI：GitHub Actions（ruff + pytest）
- [x] 根目录清理：`_tmp_wakerflow_inline.js` 移出仓库；删 `nul` 残留与空目录 `md/`；.gitignore 补 `*.db` 通配
- [x] 文案纠偏：run_health.py 记忆后端文案对齐 SQLite + sqlite-vec；walkthrough / 01-rollout 加历史快照横幅
- 决策：requirements.txt 保持官方安装路径（README/setup.sh 指向它）；uv.lock 维持忽略（未以 uv 为主流程）

## P1 双轨收口（行为不变，低风险）——2026-09-10 完成 1/2/3/5，4 部分完成

1. **worker op 注册表**（✅ 2026-09-10）：`web_fastapi/worker_process.py` 的 `handle_command`(:642起) 与热路径 `_handle_inline_cmd`(:230-271)
   对 7 个 op（permission_mode_set/get、waker_set/get、chat_stop、llm_params_set、settings_update）各持一份实现，
   分支体逐行相同——改为 op→handler 注册表，inline 热路径只保留 chat_stop 等真正急需的，其余禁止第二套分支。
2. **compact payload 三处收敛**（✅ 2026-09-10，工厂 build_compact_applied_payload 落在 session_log.py）：`src/cli.py:760-773`、`web_fastapi/worker_process.py:1237-1252`、
   `src/agent/agent_v3.py:872-881` 逐字重复同一段"剥头部 system + lc_to_dict + 4 字段 COMPACT_APPLIED payload"——
   在 `src/agent/session_log.py`（COMPACT_APPLIED 常量所在地）加工厂函数，三处改调用。
3. **get_settings 收口纪律**（✅ 2026-09-10 章节已写，存量 52 处分批收口中）：生产代码 34 文件 52 处直调绕过 Cordis（config_plugin 只是包装器）——
   在 docs/architecture.md 写 20 行纪律（什么必须经 ctx、什么允许直 import），新模块违反就拒，旧引用分批收。
4. **tests 建 conftest.py**（✅ 2026-09-10，两个 opt-in fixture：isolated_data_env / isolated_project_pointer）+ 按子系统归目录（顺延未做）：
   注意坑：SESSIONS_DIR 是模块级常量不吃 set_data_root，涉会话/项目的 fixture 要额外 monkeypatch。
5. **IPC op 协议 TypedDict 化**（✅ 2026-09-10，web_fastapi/ipc_ops.py 登记 33 op + 发送/接收双向契约测试）：每个 op 一个 TypedDict，测试断言"router 发出的键 ⊆ worker 读取的键"。

## P2 切块（可并行、行为不变）——2026-09-10 完成 1/2/3/4/5 与 6 之两件

1. **src/cli.py（2014 行）拆包**（✅ 2026-09-10，调用期再绑定保 patch 语义）→ src/cli/：commands.py（_cmd_* 族 ~680 行）、render.py（show_*/_build_* ~185 行）、
   chat_loop.py（chat() ~590 行）、main 入口保持 src.cli.main 签名不变。
2. **web_fastapi/routers/wakerflow.py（962 行）瘦身**（✅ 2026-09-10 降至 660，下沉 services/flow_service.py；waker.py 顺延）：_scan_flow_jsonl/_body_to_spec/_body_to_yaml 下沉 services/
   （激活名存实亡的服务层）；waker.py（540 行）次之。
3. **web_fastapi/worker_process.py（1365 行）三分**（✅ 2026-09-10：768 + worker_ops 450 + worker_state 286，契约测试改三文件扫描）：IPC 协议 / SessionBucket+WorkerState / _op_* 处理器。
   ⚠️ 前置：用户手头 git panel 三件套（git_ops.py/git_router.py/test_git_panel.py）未提交，先落盘避免冲突。
4. **chat.js（3082 行）分块外置**（✅ 2026-09-10 首批四块 todo/事件对话框/图片/markdown，降至 2406；侧栏与枢纽顺延）：按域拆——todo 面板/会话事件对话框/会话侧栏/图片上传/markdown 渲染可先拆
   （状态内聚），枢纽 streamMessage/send 最后；沿用现有"多 script 标签 + 全局函数"模式，每拆一块升 ?v=。
   ⚠️ 前置：chat.js 当前在用户工作区是 M 状态，需用户先提交。
5. **模板内联 JS 外置**（✅ 2026-09-10：wakerflow.js 1229 行 + waker.js 522 行，sha256 逐字校验）：wakerflow.html 内联 ~1232 行 → static/js/wakerflow.js；waker.html ~523 行次之。
6. **数据面三小件**（✅ 泵有界+ops 心跳 2026-09-10；events cap 调查后并入 P3-1——pre-compact 事件对投影冗余但被事件对话框/fork 按事件 id/CLI /events 消费，需先配冷文件回退读）：events 按会话条数/天数 cap；_StdoutPump 队列（worker_manager.py:73 无界）设上限满则丢日志型事件；
   启动打一条 ops 心跳（槽位数/WAL 字节/events 行数/memories 行数）。

## P3 架构治理（多会话长跑）——2026-09-10 全部收官（1 双轨收口完成、6 用户拍板落地）

1. **会话双轨真相源收口（最大架构债）**（✅ 2026-09-10 全部完成：events 冷归档+回退读；todos/vfs/waker 迁 kv 权威源 scope=session_state，JSON 降级缓存+旧会话回读自愈，fork/purge 联动，见 test_session_state_kv.py）：消息已由事件流投影（session_store.py:540），但 todos/vfs/waker 只存在于
   JSON 快照（:541-542），每 turn 两端都写，JSON 自认"权威写者"（:422-424）——把 todos/virtual_fs/waker 做成
   session 级事件或 kv 一行，JSON 降级为可重建的列表预览缓存。
2. **公共类型下沉 src/types/**（✅ 2026-09-10：四类型+SideEffects 族落位，原处 re-export，tools→agent 类型 import 清零）：ToolResult(src/agent/tool_result.py:34)、ToolSpec(src/tools/schema.py:126)、
   PermissionDecision(src/tools/permissions.py:61)、InterruptSignal(src/agent/hitl.py:37) 分居两个互依包，
   12+ 处函数内延迟 import 续命——下沉后原位置留 re-export，逐步消灭 agent↔tools 双向依赖。
3. **SchedulerService 迁 src/scheduling/**（✅ 2026-09-10：无环，scheduler_plugin 287→27 行，两处惰性 import 转正）：现居 src/plugins/scheduler_plugin.py:73，导致 storage/housekeeping.py:48
   与 memory/scheduler.py:61 向上依赖 plugins 层。
4. **agent_v3.py（1715 行）轻拆**（✅ 2026-09-10：listeners.py+stream_consumer.py 外提，_react_loop/_handle_* 逐字未动）：只把监听器注册与 stream 消费挪出，不动 _react_loop 不变量核心。
5. **file 后端退役**（✅ 2026-09-10：移 scripts/legacy_memory_backend.py，manager 兜底分支删除）：FileMemoryStore/FileMemoryProvider（manager.py:90-94 兜底分支）移 scripts/ 或 tests/fakes/。
6. **self_evolve 出厂默认**（✅ 2026-09-10 用户拍板：默认关+设置页开关+开启注入工具、下一轮热生效，复刻 shell_enabled 链路；force_approval 护栏常开）：开源发行版默认不进工具面（护栏 force_approval 已有，属产品决策）。
7. **不做**：sqlite_provider 拆分（三协议同库同锁是有意设计）、更深的插件市场、OS 沙箱、多用户鉴权
   （与 ADR-0005 桌面单机免登录直接冲突）、前端框架重写。

## 纪律（全程适用）

- 提交一律显式路径，避开用户并行会话的未提交文件
- 每阶段全量 pytest 回归（必须 conda env hermes_ma）；prelog 顺序污染根因已修（projects_store 缓存路径键控）；"trafilature 缺失"系解释器乌龙
- 行为不变的拆分以"测试全绿 + git diff 只有搬移"为验收线

## 顺延尾巴（2026-09-10 全部清账）

- [x] tests 目录归位（tests/agent/、tests/tools/、tests/cli/；双份 wakerflow API 测试合并）
- [x] waker.py 路由瘦身（540→304，下沉 services/waker_service.py）；chat.js 侧栏+会话恢复外置（2406→1479）——流式枢纽（streamMessage/send/HITL/历史投影）定为 chat.js 核心，不再拆
- [x] waker/wakerflow scheduler 惰性 import 转顶层指 src.scheduling（依赖面验证无环）
- [x] docs/architecture.md 补 session_state kv 权威源与事件冷归档两节
