# Hermes·Ma 架构一览（ADR 汇总页）

> 本页是 `docs/adr/ADR-0001..0005` 的一页汇总，也是 /dev-plan 的输入基线。
> 决策目标 SLO＝**A「桌面单机档」**；审计事实基础＝`docs/audit-report.md`。
> 日期：2026-08-27。

## 目标 SLO（A 桌面单机档）

| 维度 | 指标 |
|---|---|
| 规模 | 单机 1 人；≤200 项目 × ≤2000 会话快照；events ≤10 万条（配 TTL/归档） |
| 延迟 | 历史加载/切项目 P95 ≤300ms；chat 首 token P95 ≤2s（不含 LLM 服务端）；千文件树渲染 ≤500ms |
| 故障隔离 | waker/git 克隆崩溃不影响前台 chat；worker 崩溃自动重启 RTO ≤10s，已落盘消息零丢失，盲写覆盖历史根除 |
| 恢复 | pending 审批跨重启可续；项目↔会话绑定持久；失败任务在 UI 有死信呈现 |
| 并发语义 | 1 前台 turn + 后台任务并行；第二个并发 chat 显式排队而非静默拒绝 |

## 进程拓扑

```
              ┌──────────────────────────────────────────────────┐
              │        浏览器（多页 Jinja2 + vanilla JS）           │
              │   入口页 / │ chat+树 │ 会话 │ 记忆 │ waker│ flow  │
              └─────────────────────┬────────────────────────────┘
                                    │ REST / SSE（公开访问）
┌───────────────────────────────────┴─────────────────────────────────┐
│ FastAPI 主进程                                                       │
│  ├ 路由层：validate_id 等安全校验在前；auth 闸门已移除（ADR-0005）      │
│  ├ WorkerManager：多槽位（main + 会话槽，亲和路由）+ 锁 + stdout 泵   │
│  ├ SchedulerService daemon：waker/flow/记忆聚合/WAL 卫生（0002/0004）  │
│  ├ RunRegistry 任务账本：git clone 等 → spawn 短命子进程（ADR-0003）   │
│  └ cordis ctx：SessionLog / SQLiteProvider（独立连接）                │
└────────────┬──────────────────────────────────┬──────────────────────┘
             │ stdin/stdout NDJSON（req_id 复用）│ fork + timeout
┌────────────┴───────────────────┐  ┌───────────┴───────────────────┐
│ 常驻 worker 子进程 ×N（多槽）  │  │ 短命任务子进程 ×N             │
│ 默认槽 main（杂项 op）+ 会话槽 │  │ waker run · flow node · clone │
│ 会话槽：每会话一槽（槽位亲和） │  │ 各带硬截止，退出即回收        │
│ agent ReAct / 工具执行 / HITL  │  │ 与前台对话互不阻塞            │
│ 会话桶 per-sid · MCP · prefs   │  │                               │
└────────────┬───────────────────┘  └───────────┬───────────────────┘
             └──────────────┬────────────────────┘
                            ▼
     SQLite data/hermes.db (WAL, 定期 checkpoint)
     ├ events（消息真相源）/ kv（mount·runs·索引）/ memories
     └ 文件面：data/sessions/*.json（tmp+os.replace 原子写）
              data/projects/spaces/<slug>/（托管工作区）· logs/
```

## 决策一览

| ADR | 决策 | 一句话理由 | 回滚成本 |
|---|---|---|---|
| 0001 执行隔离 | 双层模型：前台=唯一常驻 worker；任务=按需子进程+硬截止；WorkerManager 加有界 FIFO 排队 | 保住状态复用与爆炸半径隔离，排队消灭"AI 正在思考"秒拒 | 低（现状超集，开关可退） |
| 0002 调度模型 | 沿用单调度线程+到期扫描+注册项线程池；固化三条纪律 | ≤200 实体 tick O(n) 已最优，休眠/回拨语义已正确 | 零（保守决策） |
| 0003 任务状态 | RunRegistry 升格唯一账本（五态+error+时间戳）；重启标 interrupted，不做 heartbeat | 单机父子进程生命周期可控，心跳复杂度无对应故障场景 | 低（纯增量） |
| 0004 存储拓扑 | save_session 原子写；fork 写收敛单通道；WAL 定期 checkpoint；幽灵繁殖三点根治 | 用成熟 tmp+os.replace 拿掉最高危数据丢失路径，两进程拓扑不动 | 低（四项互相独立可单独回退） |
| 0005 项目模型 | projects 新表+快照 project 字段；入口页接管 `/`；gate/auth 移除（me 垫片保留）；只读树 | 归属走 meta 字段零迁移；登录本无安全价值，门禁与新入口冲突 | 数据层零不可逆；auth 重建为最大单向门（已知情接受） |

## 依赖与配置访问纪律（ctx vs 直接 import）

服务类依赖（storage / memory / llm / scheduler / 工具注册表等）在插件与组合根代码中**必须经
Cordis ctx 获取**（`inject` 声明依赖，注册形状参照 `src/plugins/config_plugin.py:13-17`）；
禁止新模块顶层 import 服务实现类再自行构造——ctx 注册的是进程内同一份实例，顶层自建是第二套，
生命周期与替换点随即失守。

- **允许直接 import**：纯类型（dataclass/TypedDict）、常量、无状态纯函数。类型下沉
  （refactor-roadmap P3-2，src/types/）完成后这是默认方向——import 类型 ≠ import 服务。
- **配置访问**：新模块禁止直接 `from config import get_settings`。现存 34 文件 52 处为历史存量，
  按模块分批收口（refactor-roadmap P1-3）；`ctx.config` 是合规入口——但 `src/plugins/config_plugin.py:13-17`
  目前只是 lru_cache 单例的包装器，这是已登记的债而非"直连也没事"的许可。
- **审查规则**：新代码违反本节即拒（列入 docs/reviews/ 敌意审查的固定检查项）。

一句话口径：拿"能力"（连接、调度器、可变注册表）走 ctx；拿"知识"（类型、常量、纯函数）直接 import。

## 会话状态 kv 权威源（todos / virtual_fs / waker）

P3 双轨收口（提交 01018bd）：todos / virtual_fs / waker 的权威源从 JSON 快照迁到
kv——scope="session_state"、key=sid，value 含 schema_version:1
（src/storage/session_state_store.py）。迁移动机是收口双轨真相源：此前 JSON
快照既是权威写者又是唯一读源，状态没有独立于快照文件的生死。

- **写侧**：save_session / ensure_session_stub 经 _save_state_kv
  （src/session_store.py）同步双写，kv 先行——JSON 写失败时 kv 已持最新
  状态；kv 写失败仅告警不阻断（JSON 缓存兜底，读侧回退自愈）。
- **读侧**：load_session 经 _resolve_state 取 kv 优先；kv 缺失（旧会话 /
  历史写失败）回退 JSON 既有值并回填 kv——读一次旧会话即升级，形状非法的
  旧行同时被覆盖修复；JSON 快照也缺失时不回填（无值可迁移，不繁殖占位行）。
- **JSON 降级为列表预览缓存**：既有消费方照常工作，语义上可丢可重建——
  删掉 JSON 文件后三字段仍可从 kv 完整恢复。
- **生命周期联动**：fork 经 copy_state 复制 kv 行（源是旧会话无 kv 行时
  no-op，stub 透传值落盘时收敛进 kv）；会话/项目删除经 delete_state 级联
  清理（web_fastapi/routers/sessions.py、projects.py、worker_process.py）。
- **连接缓存按解析库路径键控**：default_provider 进程级惰性缓存；
  paths.set_data_root 改道数据根后首次 kv 操作自动按新路径重建（旧连接经
  模块锁关闭）——测试/迁移脚本无需感知。

## 事件冷归档（compact 前缀出热表）

提交 87b3050：COMPACT_APPLIED 的 payload 内嵌 kept_messages，derive_messages
在每条 compact 处重置投影 → 最后一条 COMPACT_APPLIED 之前的事件对消息重建
纯冗余。SessionLog.archive_compacted_events（src/agent/session_log.py）把这段
前缀导出到会话旁冷文件 {sid}.events-archive.jsonl（与 JSON 快照同目录）后删
热行；compact 事件本身及之后永远留热表。

- **先冷后热**：导出行整批 append + fsync 落定后才 delete_events_before 删
  热行；两步间崩溃的后果是冷热并存——id 全局唯一，合并读按 id 去重仍正确，
  下次归档按已有冷文件 id 去重后补删残留热行（自愈无需人工介入）。
- **读侧无感**：事件对话框（GET /api/sessions/{sid}/events）、fork 截断、
  CLI /events 三个消费方统一走 load_events_with_archive 冷+热合并视图；
  derive_messages 维持纯热表读——归档段对投影冗余，投影逐字不变
  （不变量测试锚点）。
- **活跃会话双保险**（src/storage/housekeeping.py:_archive_compacted_sessions）：
  housekeeping tick 逐会话判定，注入的 is_session_active 回调（查 worker
  槽位）与 15 分钟静默门槛（ARCHIVE_QUIET_SECONDS）任一命中即跳过——宁可
  晚归档，不与在途写入抢行。归档是纯优化，失败只降级不留患。

## 你可能没想到的风险清单

1. **公网/tunnel 暴露 = bash 完全控制权**（运营风险之首）：免认证后任何能访问端口者即持有
   shell/FS 工具权。缓解只剩默认回环绑定与非回环警告——把端口交给 cloudflare tunnel 这类
   服务之前必须想清楚。未来可选 env 开关加访问令牌，本期不承诺。
2. **断电窗口与丢失模型要明示**：synchronous=NORMAL 下最近事务可能丢；聊天场景的现实承诺是
   "已确认落盘的消息零丢失"，正在流式中的最后一轮以事件库投影恢复为准——README 应如实写。
3. **D2 收敛写通道≠消灭逻辑竞态**：read-modify-write 的语义竞态（两方同时读旧值各写各的）
   在通道收敛后仍需并发压测证明，不能当作已被原子写顺手解决。
4. **中文项目名→slug 的转换漂移**：同名项目 slug 碰撞、全中文长名的截断可读性差，
   重名策略（追加 -N）需在 /dev-plan 定稿并在 UI 给出冲突提示。
5. **path_guard 是应用层守卫不是 OS 沙箱**：bash 工具可以 cd 逃逸出项目根（代码 docstring
   如实承认）；树预览遵循同一信任模型，不要在文档里暗示沙箱强度。
6. **shell_timeout 可被模型传参放大（0=不限）**：git clone 若走 bash 工具则失去 600s 截止
   保护——clone 必须走任务子进程通道（ADR-0003），这是硬性实现约束不是建议。
7. **SSE 取消语义（M5 多槽下已收敛）**：客户端断开 ≠ 取消推理——relay 的 finally 安全网
   取消仅在「本请求已上车且死于泵超时（worker 疑似卡死）」时触发；忙超时（从未上车）与
   正常结束/EOF 均不发 chat_stop。显式停止走 /api/chat/stop → cancel_for(session_id)，
   槽位亲和保证只写该会话自己槽位的 stdin，不误伤其他槽；取消是 best-effort
   （worker 侧 _cancel_event 轮询使其在执行期即生效），失败不影响主流程。
8. **大目录 listing DoS**：node_modules / 万级文件目录一次列全会打爆前端 DOM；
   树接口的深度/条目/大小设限是 SLO 达成前提，参数必须有基准测试背书。
9. **_StdoutPump 行队列无界**：worker 若被日志型工具狂灌输出，主进程内存随队列膨胀；
   排队机制让请求等更久，更容易观察到这个积压（纳入观察指标而非本轮修复）。
10. **events ≤10 万条 SLO 依赖 TTL 真实落地**：目前 purge 只有显式删会话一条路；
    ADR-0004-D3 不实施则该 SLO 数字静默失守。
11. **WEB_SECRET_KEY 移除的连带面**：随机 secret 生成的警告逻辑删除时，注意非回环监听警告
    是独立逻辑，别连带消失（security_baseline 保留断言会兜底）。
12. **schema_version 只有守门没有迁移**（version≠1 直接 RuntimeError）：本决策集刻意未引入
    迁移框架；projects 新表用幂等 CREATE IF NOT EXISTS 过渡可行，但连续多次 schema 变更
    后应补迁移机制（记入演进方向）。
13. **开源发布备忘（沿用既有结论）**：git 历史含私人邮箱与面试相关文档——公开推送前需
    squash/filter-repo 清理并换 noreply 身份；本决策集不含此操作。

## 与已确认草图的对应

| 草图 | 承载 ADR |
|---|---|
| 入口页（项目卡片/新建/打开目录/克隆/直接开聊） | ADR-0005 D1/D3、ADR-0003（clone 任务化+死信徽标） |
| chat 页右侧只读树（收件箱隐藏） | ADR-0005 D6/D4 |
| 数据流（projects 表+快照字段+兼容垫片） | ADR-0005 D2/D4、ADR-0004 D1（字段载体原子化） |

## 下一步

本文档集完成后停在 /dev-plan 之前：由用户审阅确认后进入实施规划阶段；
实施顺序建议 0004 → 0005 → 0001 → 0003 → 0002（先地基后体验），最终排序归 /dev-plan 决定。
