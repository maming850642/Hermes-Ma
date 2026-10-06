# 全库深度审查与修复收尾报告

- **日期**：2026-09-06
- **对象**：`open-source-prep` @ 32f6309 起的全部工作（未提交，78 文件 +7017/−492）
- **流程**：第一轮全库审查（6 路并行，49 项确认/6 项误报剔除）→ 三波修复 → 对全部修复 diff 敌意复查 → 第二轮全库审查（含 CLI 终端实操 + 主 agent 浏览器实操）→ 第四/五/六波修复 → 终版验证
- **前置报告**：`docs/reviews/deep-review-20260905.md`（第一轮 49 项缺陷全录）

---

## 终版验证状态

- **全量测试**：`conda activate hermes_ma` → `pytest -q` → **1602 passed / 0 failed / 4 skipped**（4 个 skip 均为需显式开关的门控用例，如 HERMES_EMBEDDING_SMOKE）
- 测试从基线 1269 例增至 1602 例（+333 条回归测试，全部由修复产生）
- 双解释器验证：conda `hermes_ma` 与 `.venv` 均全绿（.venv 需 `uv pip install sqlite-vec`，requirements 已声明）

---

## 修复总账（六波，共 ~75 项）

### 安全类（全部实测验证）
| 缺陷 | 修复 |
|---|---|
| shell 分类器换行/单 `&` 绕过 | 换行归一 + 分隔符补 `&`/`|&` |
| `env` 白名单任意程序执行 | 形态检查（仅无参/`-0`/`-u NAME`） |
| **参数位命令替换 `$(...)`/反引号/`<(`/`<<<` 零审批 RCE** | 引号感知扫描 → review |
| **awk 三种管道执行形态 / sed 数字地址 `3e/1w/W/1r`/`s///eg` / find `-fprint0`** | 黑名单补齐 + sed 形态检查器化 |
| **`sed --in-place` 长选项绕过** | 黑名单补齐 |
| git 任意子命令（reset --hard/clean/alias `!`） | 只读子命令枚举 + 形态检查 |
| 出厂黑名单整体替换（漏拦+误拦 `git show`） | 默认表+配置追加，正则加边界 |
| **审批条属性注入 XSS（LLM 可控 details）** | escapeHtml 全字符集 |
| **sanitizeHtml `javascript:` 绕过 + noscript mXSS** | `new URL().protocol` 判定 + 三标签移除 |
| waker 下拉/flow 卡片/config 页注入 | DOM API 构造 + data-* 委托 + encodeURIComponent |
| **flow 名称 blocks 模式绕过字符集校验 → onclick 注入可达** | FlowStore.save 统一校验收口 |
| Web 免认证下 CSRF/DNS rebinding 全 API 裸奔 | Host 白名单 + Origin 同源中间件（scheme 感知端口） |
| 挂载黑名单只封用户目录根 | 敏感子树封禁（.ssh/.aws/.kube/.gnupg/AppData） |
| MCP `trust` 配置全链路失效 | trust 字段 + 正确配置读取 |

### 正确性类（核心链路）
| 缺陷 | 修复 |
|---|---|
| 批量工具中断丢前序结果 → 悬空 tool_calls 持久化钉死会话 | signal 携带已完成结果 + `_repair_interrupt_pairing` + 贯穿性不变量测试 |
| `request_human_approval` 并行被吞 / 串行丢身份 / resume 复跑误报 force_deny | payload 注入（双腿）+ resume 不重入直接闭合 |
| resume 后用户 reject 被静默翻成批准 | 显式拒绝优先 |
| copy_file/move_file schema 与实现错位（全坏 + move 假成功） | 实现改读 src/dst + copy 标 destructive + 错误脱敏 |
| MCP 工具执行 100% 失败（引用不存在属性） | `get_client` 正式接口 |
| UPDATE 记忆删向量不现算 / embed 失败留错误向量 | 内容版本感知强制重算 + 失败删旧可自愈 |
| sqlite_vec 缺失构造崩溃（与降级承诺相反） | `_ensure_aux_tables` 守卫降级 |
| recency 衰减压制两周以上记忆 | 天级化 + floor 钳制 |
| 调度 tick 无逐条隔离（单条坏配置停摆全部） | `_tick_one` 逐条 try + validate_schedule 接线 |
| ask_user 占死 2 线程池 24h | 挂起/续跑架构重做（watch/suspended/cancel） |
| flow 防重入只护 submit、双 flow_end、任务 argv 32k 限制 | 完成回调释放 / 去重 / stdin 传递 |
| `/exit` 与 worker 退出假超时 | daemon 线程 + 有界 join |
| async 路由同步 `worker.send`/spawn 冻结事件循环（12 处） | 改同步 def / run_in_threadpool / spawn 出锁 |
| `/api/compact`、waker GET/PUT、`/save` 错打 main 槽 | session_id 亲和路由（前端配套传参） |
| resume 丢 todos/compact_threshold_pct、CLI except 链死代码、py3.10 超时失效、llm_stream 假关闭、LLM 错误污染历史、未知工具悬空、多模态图片静默丢弃、compact 边界孤儿、CLI 审批事件不落库、审批处 EOF/Ctrl+C 崩溃 | 全部修复（详见各波 agent 报告） |

### 文档与工程
README.en.md 漂移同步（逐条核对）、architecture.md M5 多槽更新、workspace/run_shell 过期 docstring、hitl 键常量收敛、`llm/error` 事件常量归位、测试装饰器错位修正（线上事故回归恢复必跑）、死 shim 文件删除。

---

## 实测验证（用户视角）

- **CLI（子代理，9 个真实终端会话 ~15 轮 LLM）**：13 项功能 10 PASS；核心用例（读码→审批→写文件→/events 核对→/fork→/compact→/resume）全链通过；发现的 3 个缺陷（审批事件不落库、审批处 EOF、Ctrl+C 崩溃）已修
- **Web（主 agent 真实浏览器，12 项功能）**：全部通过——复杂用例（读源码→总结→HITL 审批→批准→落盘→文件树刷新）、拒绝路径、停止按钮、记忆 CRUD、waker 全链、flow 运行渲染、密钥掩码、控制台零报错
- **安全红线全程遵守**：测试只批工作区良性写，非只读 shell 一律拒绝

---

## 已知残留（诚实清单）

1. **理论缺陷（出厂配置不可达或需罕见条件）**：web_fetch DNS rebinding TOCTOU（需攻击者控制 DNS）、waker store 读改写窄窗口、booted-kernel 路径流取消靠 finally 兜底（契约已对齐）、挂起审批文件残留（超时无 default 时）等——均已在代码注释或审查记录中标注
2. **既有设计取舍**：免认证+回环绑定的威胁模型（ADR-0005，浏览器侧攻击已由中间件封堵）；plan 模式 shell 无 OS 级沙箱（文档如实声明）；`6adbb957` 用户会话尾部混入 2 条本次测试轮次（无法单独摘除，可用 /reset 或忽略）
3. **未修 P3**：原子写无 fsync（断电窗口）、Windows 保留名会话文件、`_migrate_old_sessions` 重跑重复、cordis pre-execute 异常隔离不对称、waker 卡片 last_status 完成后不持久化等——均为低危，记录于各轮审查报告
4. **遗留观察**：`wakerflow.html` 块编辑器内部 onclick 均为静态字面量无数据拼接；sessions/waker 页残留 onclick 依赖后端字符集白名单（白名单真实有效）；`FlowRunner.cancel` 终态为 failed 未区分"已取消"

## 遗留待办（不阻塞运行）
- 用户本机 `config.yaml` 第 9 行 `tool_loop_threshold: 3`（出厂模板已改 5）——可按需手动改
- 旧 config.yaml 中 `curl|sh` 黑名单条目建议删除（新语义下默认表已含正解，该条会误杀 `git show`/`du -sh`）
- requirements 的 `sqlite-vec` 在 CI 需确认可安装（.venv 当初缺失即此因）

---

## 结论

两轮全库敌意审查（共确认 **~90 项真缺陷**，剔除 7 项误报）、六波修复（每项带回归测试）、CLI+浏览器双通道真实实操，全部收敛于 **1602/1602 全绿**。第一轮的安全承诺失效类（shell RCE 面、XSS、审批绕过族）与第二轮的修复回归类（MCP 执行断链、向量错配、noscript mXSS）均已闭合；已知残留全部为理论缺陷、文档化取舍或低危 P3。
