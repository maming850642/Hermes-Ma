# pi 编码代理开发史复盘——从 6703 个提交里学 harness 开发

> 研究日期：2026-10-03。对象：Mario Zechner（badlogic，libGDX 作者）的 pi，现居 `earendil-works/pi`（原 `badlogic/pi-mono`）。
> 方法：blobless 全历史克隆（本地 `%TEMP%\pi-mono`）+ 7 路子代理分阶段考古（5 个时间窗逐提交解读、1 路外部叙事调研、1 路工程纪律横切），关键结论均附 hash/tag 证据。
> 配套：官方 607KB CHANGELOG 副本在 `%TEMP%\pi-research\CHANGELOG.md`。

## 0. 一页总览

**基本盘**：6703 提交 / 323 tags / 14.5 个月（2025-08-09 → 2026-10-03，v0.5.1 → v1.0.0）/ 今日 13 个包（agent、ai、chord、client、codemode、coding-agent、durable、evals、mcp、protocol、server、telemetry、tui）。

**月度提交强度**（全史形状 = 两个高峰夹一段平台期）：

```
2025-08 ▍85      建仓
2025-09 ▍53      pi-ai 打磨
2025-10 █▏129    agent 重写、coding-agent 诞生
2025-11 ██▎280   成品化爆发、11-30 博文
2025-12 ████████▊872    架构大冲刺（tag 峰值 86 个/月）
2026-01 ████████████████ 1224  全史峰值：生态与集成
2026-02 ▍377 }   平台期：打磨+治理
2026-03 █▏418 }   （公司化酝酿）
2026-04 █▍461 }   04-08 "I've sold out"
2026-05 █▌481 }   05-07 仓库/scope 迁移
2026-06 █▍424    供应商兼容、trust 模型
2026-07 █████▍494    server/protocol 重组、evals 诞生
2026-08 █████████ 889   第二峰：harness v2 大重写
2026-09 ████▌457    1.0 冲刺（跳号 0.88~0.98）
2026-10 ▌59     10-01 v1.0.0，发布后 44% 是 fix
```

**一句话史纲**：私有 dogfood 成品（lemmy）更名灌入 monorepo → 先统一 LLM 层、再 agent 循环、再 TUI/CLI → 两个月架构大冲刺长出全部生态位 → 公司化瘦身聚焦 → 规格先行重写 harness v2 → 带着通用机制（而非立场）迎接 1.0。

## 1. 前史（2025-04 ~ 08-09）：pi 不是从零开始的

- **入坑**：2025-04 Peter Steinberger 鼓吹 "THE AGENTS, THEY WORK"，Mario 装了 Claude Code "停止了睡眠"；6 月与 Peter、Armin Ronacher 24 小时 hackathon 造 VibeTunnel。
- **前身仓库 `badlogic/lemmy`**（2025-05-23 建，08-13 停更）：本身就是 monorepo——`lemmy`（统一 LLM 接口：多 provider、手动工具执行、上下文序列化、Zod）+ `lemmy-tui`（差分渲染 TUI）+ apps。**pi-mono 是 lemmy 的改名升格**（Zod→TypeBox），所以建仓首日即 v0.5.x。
- **理念前史早于代码**：博文三部曲《Prompts are code》(06-02)、《MCP vs CLI》(08-15)、《What if you don't need MCP at all?》(11-02)——"CLI+文件"优先与上下文经济性思想先于 pi 落地。

**教训**：大项目的"第一天"往往是私有积累的"毕业典礼"。建仓首日 28 分钟即推 npm、同日 6 版全在修安装类问题（全局 CLI 跑不起来 `d304f377`、漏 scripts 目录 `3a9c3a2e`）——**发布链路先于功能**。

## 2. 阶段〇（2025-08，85 提交）：三包开局，8 天后抽出 pi-ai

- 首日三包：pi-tui（差分渲染终端 UI，text-editor 802 行）、pi-agent（Claude Code 式极简循环，tools/session-manager/三 renderer，约 2000 行）、pi pods（SSH 管 GPU pod 的 vLLM 部署）。
- **08-17 `f064ea0e` 转折点：从 agent 抽出统一 LLM 层 packages/ai**（OpenAI/Anthropic/Gemini）。此后模型目录（models.dev 集成 `02a9b4f0`）、全 provider E2E（`7a685208`）、成本追踪、181 个可工具调用模型的 models.generated.ts（`9c3f32b9`）全部寄生此层生长。
- 工程底座首月齐备：husky pre-commit（08-11）、Vitest（08-29 `3f36051b`）、锁步版本脚本 sync-versions.js + PUBLISHING.md。

## 3. 阶段一（2025-09 ~ 11-30，462 提交）：分层时序教科书

**严格按"LLM 层 → agent 循环 → TUI/CLI → 生态"顺序推进，每层稳定后才动上层。**

- **9 月（53）= pi-ai 地狱打磨**：两天内 "Massive refactor of API"（`66cefb23`，四 provider 各削 500 行对齐）、Zod→TypeBox（`e8370436`，ajv 的 eval 在浏览器 MV3 直接不可用）、流式工具调用 partial-JSON 解析（`39c626b6`）。其余全是 provider 长尾怪癖补丁。**统一 API 的难点不在抽象而在长尾兼容，要预留 80% 工时。**
- **10 月（129）= agent 重写 + coding-agent 诞生**：前半月 web-ui 实验，10-06 `05dfaa11` 自定义消息扩展系统（日后扩展机制雏形）。10-17 `ffc9be88` agent 包按 transport 分层重写，**同 commit 诞生 packages/coding-agent：read/write/edit/bash 四工具 + session-manager，仅 ~600 行**；旧实现改名 agent-old 留对照，不直接删。
- **11 月（280）= 三天 TUI 重写 + 成品化爆发**：11-10~12 "minimal TUI rewrite"（`97c730c8` +1933 行）+ 一天 40 commit 组件化 + **abort 信号贯穿所有工具、bash 杀进程树**（`6e9fa8dd`，专门一整天）；11-12 一天 50 commit：更名 @mariozechner/pi-coding-agent、--resume 会话选择器、/export 自包含 HTML、text/json/rpc 三模式、分层 AGENTS.md 上下文、**工具错误改返回内容而非抛异常**（`f147109d`）。11-13 v0.6.0 首个 tag。11-21 一天 6 版。**11-29 只加只读 grep/find/ls 且配 --tools 白名单**——工具集扩张极克制。11-30 博文《What I learned building an opinionated and minimal coding agent》定调：radically simple、YOLO 默认、显式不做 to-dos/plan mode/MCP（README 白纸黑字 "pi does not support MCP" `60e4fcf0`——225 token 的 README 胜过 13.7k token 的 Playwright MCP 工具集）。

## 4. 阶段二（2025-12 ~ 2026-01，2096 提交）：全史最大冲刺

**12 月 = 内核月**：
- 12-02 Bun 单文件二进制 + **首个 CI workflow**（`c4a65ad8`）——前四个月纯本地纪律。
- **首个旗舰专项 = compaction**（12-03~06 #92，`c89b1ec3`）：/compact、自动触发、ASCII 图解文档，12-24 还在补。**生存级功能一次做透，不渐进。**
- **12-09 全史最大重构日**：前一天先写重构计划（`1507f8b7`），当日以 WP1~WP16 编号工作包把 main.ts 巨石拆成 AgentSession+core/+modes/（`83a6c269`），同日落地新 compaction、hooks 系统（`04d59f31`）、RPC 重写，**当日删旧实现**（`6c9a264b`），一天三版。
- 生态位逐日上线：skills 兼容 Claude SKILL.md（12-12）、自定义工具（12-17）、subagent（12-19）、**SDK 593 行一次成型**（12-22 `5482bf3e`）、session tree 从设计文档到 /tree 命令（12-25~29）。12-28 删 proxy 包。
- tag 峰值 86 个/月（单日 9 个）。

**1 月 = 生态月（1224 提交，全史峰）**：
- **01-04/05 第二场架构战役：统一扩展系统 1830 行一次落地**（`2846c7d1`），随即把 hooks+custom-tools **合并迁移**为 extensions（`c6fc0845`）——扩展点先并存后统一，双体系存活期仅 3 周。
- 社区洪峰：两天合入 Bedrock/Vercel Gateway/MiniMax/plan-mode v2；01-24 provider 注册重构 api-registry（`c725135a`，stream.ts -559 行）。
- **强度来源**：Mario 占 75%，其余 ~70 人长尾；**节假日反成峰值**（圣诞周 125，跨年周 501，单日峰 01-03:125）——全职单人 + integrator 模式（社区 PR squash 合并挂 Co-authored-by）。

## 5. 阶段三（2026-02 ~ 05，1737 提交）：公司化的工程学

- **2~3 月**：作者宣布 "OSS vacation"（全职写 OSS、社区窗口全开），主题=平台兼容（Windows VT、Termux）+ 贡献治理实验（"OSS weekend" 时间窗制）。
- **4 月 = 出售月**：04-08 博文《I've sold out》当天发 v0.66.0 内嵌 Earendil 公告（`6d2d03dc`），因社区反弹次日改为隐藏彩蛋 `/dementedelves`（`cca5a3a1`）——商业动作快进快退；04-14 **贡献闸门产品化**（`d62d2217` 删时间窗制，改 APPROVED_CONTRIBUTORS 名单+机器人自动审批，应对 AI slop PR 洪流）；04-22 域名玩笑 shittycodingagent.ai→pi.dev（`df84e3d2` "corporate said we're professionals"）；**04-30 大裁剪**：删 mom+pods 包（`0ed0d434`，指明迁往 pi-chat——砍包给出口）、删 google-gemini-cli+antigravity 逆向 OAuth 通道（约 2500 行，合规需求）。**LICENSE 全程未动（MIT 一行不改是谈判底线）。**
- **5 月 = 迁移月**：bigrefactor 长分支两周（每日 merge main，重构不冻结社区）；**05-07 scope 迁移教科书**：先发 v0.73.1 把"自更新跟随端点返回的包名"做进老包（`5e1e4c3c`）→ 用户 `pi update` 平滑跳到 @earendil-works/* → 当天 v0.74.0。迁移拆两笔提交（实改 `3e5ad67e` + 纯 merge `551385e4`），零功能混入。05-28 发布全面 CI 化（npm trusted publishing）。
- **团队转身**：Mario 从 90%（2025）→ 63%（2026H1）→ 2026H2 被 Armin（478:393）反超，13 个月完成个人项目→团队项目。

## 6. 阶段四（2026-06 ~ 10-03，2323 提交）：冲向 1.0

- **6~7 月**：供应商兼容潮 + project trust 安全模型（`89a92207`）+ server/protocol 重组 + **evals 包诞生**（07-25 `eafe11fb` vitest eval harness → 07-30 对比评测）。
- **8 月（881，次峰）= harness v2 大重写**：内存 harness v2（08-04 `1d0c9747`）→ durable drive（08-26）→ **Chord runtime**（08-28 `28b49a6b`）+ delta 状态复制；telemetry 包（08-05）；08-13 官方博文《How Compaction Works in Pi》（compaction="换班交接简报"、20k token 保留预算、独立总结请求可用不同模型、会打断 prompt cache）。
- **9 月 = 规格先行冲刺**：Pico→Pico5 在 pico 分支规格化重写（`729d5cb7` 定稿规格）→ durable 独立成包（09-18 `08016016`）。**冲刺周亮点：`docs(durable): specify Package N` → `feat Package N` 成对提交，23 个规格包 4 天推完**——大重写拆成可独立验收的编号里程碑。
- **版本号是叙事工具**：v0.87.1（09-22）→ **直跳** v0.99.0（09-29）——"一周 11 个 minor"实为跳号（0.88~0.98 不存在），制造临门感；09-29 决战日：**codemode + MCP 整包落地**（`8562bcf6`）、同日两发 v0.99.x；09-30 v0.99.2；10-01 `a13d35a7` v1.0.0。
- **MCP 反转的机制本质**：批评了一年的 MCP（"21 工具吃掉 7~9% 上下文"），反转靠的是**先造通用机制**——tool exposure 五档（direct/model-only/codemode/deferred/hidden）、prepareLoadout()、ctx.executeTool() 嵌套调用、QuickJS 沙箱 codemode（模型写 JS 调工具，结果不进上下文）。MCP/codemode/tool_search 全是可替换的内置扩展。官方理由：7 月 MCP 规范更无状态 + 沙箱本就为非 LLM 工具而建。发布日即上 MCP 官方 conformance suite 进 CI（`a4715ec9`）。**立场可以反转，前提是通用机制先行，新协议只是第一个消费者。**
- **1.0 不是终点**：发布后 3 天 54 提交中 24 个 fix（44%）：渲染内存砍至 1/5（`54c19a25`）、codemode 防内存耗尽、去 shrinkwrap 换 managed installer；10-03（今天）仍在修（`a276dabe`）。

## 7. 横切画像：工程纪律量化

| 维度 | 数据 | 解读 |
|---|---|---|
| 提交规范 | Conventional commits 占比 23.5%（2025）→ 70.2%（2026H1）→ 88.4%（2026H2） | 规范是学出来的，随团队化必要性上升 |
| 发布机制 | 323 tags；295 条 Release 提交；sync-versions.js 版本不一致直接 exit 1；coding-agent CHANGELOG 被 2004 次提交维护，永留 [Unreleased] 区 | 锁步版本 + changelog 纪律全自动化 |
| 发布节奏 | 86 tags/月（2025-12 峰值）→ 主动回落 5~9/月 | 纪律不是恒定高频，是找到可持续速率 |
| 测试 | 604 个测试文件；test.sh = hermetic 范本（env -i、假 HOME、TZ=UTC、清空 30+ 密钥）；pi-test.sh 用 pi 跑 pi | 密封测试 + 自举测试双轨 |
| AI 辅助痕迹 | 显式 AI 共同署名仅 17 条；但 .pi/skills（release.md、interactive-testing.md）、issue-analysis.yml（用 pi 做 issue triage）、AGENTS.md 106 次修订 | "用 pi 造 pi"主要沉淀在 skills/CI 而非署名仪式 |
| CI | 2025-12-02 才有第一个 workflow；现 11 个；actions 全按 commit SHA 锁定 | CI 晚于发布四个月——先跑起来再上护栏 |
| 删除事件 | proxy（12-28）、mom/pods（04-30）、web-ui（05-20）、google 逆向通道（04-30）；包数 3→14→13 | 敢整包删除，砍的全是"个人时代良心附带品" |

## 8. 经验总纲：下次做 harness 的直觉清单

**架构律**
1. **分层时序锁死**：统一 LLM 层 → 会话循环 → UI/CLI → 生态位（extensions/skills/SDK）→ 耐久层。每层稳定前不开上层战场；provider 长尾兼容预留 80% 工时。
2. **核心抽象的 Big Bang 重修要用"计划文档 + 编号工作包 + 当日删旧"**（12-09 模式），且旧实现改名留对照、新实现 "-new" 并行，重写窗口压到 3 天内。
3. **扩展点先并存后统一**：hooks、custom tools 各自跑通 → 一举合并为 extensions + 迁移指南；双体系存活期 ≤3 周，拖久了就是永久债务。
4. **会话即数据**：JSONL 落盘、HTML 导出、RPC 模式、树状分支——一切"可后处理、可换 UI"的能力都从数据格式免费长出来。
5. **生存级功能一次做透**：compaction、abort 贯穿、进程树清理这类功能不做渐进——它们决定项目能不能活。

**上下文经济律**（pi 的核心命题，与 hermes 事件溯源路线同源）
6. **系统提示+工具定义 <1000 token 是可达到的**；工具集克制（4 核心工具起步，只读工具后来才加，配 --tools 白名单）。
7. **工具结果不必然进上下文**：exposure 分档 + codemode 式"模型写代码调工具、结果留在沙箱"是比 MCP 更根本的答案。
8. **立场写在 README，反转靠机制**：先造通用机制，协议只是消费者——"说不"积累的信任让一年后的"说是"更有分量。

**节奏律**
9. **冲刺-回落曲线**：两个高峰（1224/月、889/月）都由明确的战役目标驱动（内核重构、harness v2），峰值不可持续也不必可持续——可持续的是回落后的 5~9 版/月。
10. **规格先行成对提交**：spec→feat 编号推进（Package N 模式），4 天 23 个规格包；大重写在分支上长跑、每日 merge main，不冻结社区。
11. **发布链路优先于功能**，且高频小步发版是单人吃下 70 人社区贡献的杠杆。

**治理与商业化律**
12. **自举三件套**：仓库级 agent 配置入库（.pi/skills 而非堆 prompt）、自家 agent 跑自家测试（pi-test.sh）、自家 agent 做 issue triage。
13. **贡献闸门要产品化演进**：时间窗制 → 名单+机器人审批 → lgtm 信任等级；用机制挡 slop，不用人力。
14. **商业化过渡的工程动作**：改名前先发带"自更新桥"的兼容版；迁移拆纯提交零混入；砍包给 fork 出口；LICENSE 一行不动；商标而非 license 做护城河。
15. **hermetic 测试 + conformance suite**：env 隔离跑测试，发布日就上官方一致性套件——1.0 的质量感是这么来的。

## 9. 对照 hermes_ma 的三条速记

1. hermes 已有事件溯源/会话即数据（领先项）——pi 的下一步（durable/checkpoint+replay、15k 行可整读上下文）印证这个方向的战场价值。
2. pi 的 MCP 姿态演变（拒绝→codemode 机制收编）与 hermes 当前"按需加载/大结果先压缩"调研（P0 未拍板项）互为印证：**通用机制先行，协议后置**。
3. pi 用 14.5 个月从个人 dogfood 到 1.0，其中 2/3 时间在做"减法与纪律"（工具克制、整包删除、changelog 2004 次维护）——harness 项目的成熟度标志不是功能数，是可控的上下文预算与可持续的发布速率。

## 附：主要一手来源

- 仓库考古：本地克隆 `%TEMP%\pi-mono`（badlogic/pi-mono → earendil-works/pi，全历史 6703 提交）
- 前身：github.com/badlogic/lemmy（2025-05-23 ~ 08-13）
- 博文：mariozechner.at —— Prompts are code(06-02) / MCP vs CLI(08-15) / What if you don't need MCP(11-02) / What I learned(11-30) / Year in Review(12-22) / slowing down(2026-03-25) / I've sold out(2026-04-08)
- Earendil rfc 邮件系：earendil.com/posts（15 篇，含 What is a Harness?、How Compaction Works in Pi、Pi 1.0、Pi Durable）
- 版本史：pi.dev/changelog；官方 CHANGELOG.md 副本 `%TEMP%\pi-research\CHANGELOG.md`
- 社区：HN Pi 1.0 讨论帖（1200+ 分）、The Register "pulls a 180"、Pragmatic Engineer 访谈
