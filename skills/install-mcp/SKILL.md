---
name: install-mcp
description: 用户丢来 MCP 链接或包名时，调研 README、识别安装形态、用 create_mcp 接入并验证——缺密钥先问、docker 型装不了要如实说
---

# 安装 / 接入 MCP Server

用户发来 GitHub 链接、npm 包名、或「帮我接上某某 MCP」时走本技能。
写入口只有三个工具：`list_mcps`（查现有）、`create_mcp`（校验+落盘+连接）、`remove_mcp`（删）。
**绝不要**用 bash 去改 `mcp_servers/` 或配置文件——那会绕过校验、信任级别和连接流程。

## 标准流程

1. **查现有**：`list_mcps` 看是否已接入同名/同类 server。已有就问用户要不要先 `remove_mcp`。
2. **抓文档**：用 `web_fetch` 读 README。GitHub 仓库页要先转 raw：
   - `https://github.com/org/repo` → `https://raw.githubusercontent.com/org/repo/HEAD/README.md`
   - blob 链接同样改 raw。README 不在根目录就按仓库里实际路径。
   - 抓失败再 `web_search` 找官方安装说明。
3. **识别安装形态**（按下面对照表），缺的信息一次问清。
4. **缺密钥先问**：README 要求 `API_KEY` / `TOKEN` 等环境变量时，**先问用户要值，绝不瞎填、不编造、不从记忆里猜**。用户说没有就停，说明接上了也跑不起来。
5. **创建**：`create_mcp`（系统会弹审批卡，把 command/args/env/url/trust 完整展示给用户）。
6. **验证**：`list_mcps` 看连接状态与工具清单。失败按「排查」节处理，不要沉默重试十几次。
7. **交付**：汇报工具全名（`mcp__<server>__<tool>`）、trust 语义、以及下次怎么用。

## 安装形态对照

| README 怎么写 | create_mcp 怎么填 | 说明 |
|---|---|---|
| `npx -y @org/server` / `npx foo` | `transport=command`，`command=npx`，`args=["-y","@org/server"]` | 最常见。含 `@` 的包名保持原样（不要拆） |
| `{PYTHON} -m package` / `uvx package` / `python -m` | `transport=command`，`command={PYTHON}`，`args=["-m","package"]` | `{PYTHON}` 会在启动时展开为当前解释器，换机器不用改路径 |
| 远程 SSE / HTTP URL | `transport=sse` 或 `streamable_http`，填 `url`，可选 `headers` | 有 Bearer token 放 headers，不要写进 command |
| `docker run …` / 要先装 Desktop 的 | **不要 create_mcp** | 如实告诉用户：当前环境接不了 docker 型 MCP，请改用 npx/python/远程 URL 形态，或用户自行在本机跑起来再给 URL |

`command` 传输在内部等于 stdio。`trust` 默认 `approval`（每个工具调用弹审批）；用户明确说「信任这个 server、别每次问」才用 `full`。不确定就保持 `approval`。

`name`：`^[a-zA-Z0-9_-]{1,64}$`，小写短横线。从仓库名/包名翻译，如 `@modelcontextprotocol/server-filesystem` → `filesystem`。重名必须先 `remove_mcp`。

`enabled` 由系统固定为 true，你不用传。

## 缺信息就问（一次问全）

- API key / token / 账号（有就要，没有就停）
- 远程 URL（SSE/HTTP 型）
- 本地路径类参数（filesystem 型的允许根目录等）——让用户给，不要默认 `D:\` 或 `/`
- trust 级别：没表态就 approval

## 验证与排查

`list_mcps` 应看到「已连接」和工具名。若失败：

- 返回里带 **stderr 尾部**——那是真因，读它。常见：包不存在、node/npx 不在 PATH、模块没装、密钥无效。
- 境外 npm registry / GitHub 超时：提醒用户在配置里设 `web_proxy`（MCP 子进程会继承为 `HTTP_PROXY`），然后 `remove_mcp` 再 `create_mcp`。
- 「已落盘、后台连接中」：等下一轮再 `list_mcps`，不要立刻重复 create。
- 连上但工具列表为空：告诉用户 server 可能握手成功却没暴露工具，贴 stderr。

不要用 bash 去重连；重连 = `remove_mcp` + `create_mcp`，或让用户去设置页 reload。

## 工具全名与 trust

连接成功后，LLM 可见的工具名是 `mcp__<server名>__<原工具名>`。教用户（和你自己）用这个全名。

| trust | 调用时 |
|---|---|
| `approval`（默认） | before_changes 下每次 MCP 工具调用弹审批 |
| `full` | 当普通网络工具，不再因「来源是 MCP」而弹审批 |
| `deny` | 不该拿来接入；用户要这个就劝他改 approval |

收件箱（未挂载工作区）里 `mcp__*` 也可见，但 `approval` 下调用仍弹审批。

## waker / 无人值守警告

waker 跑任务时没人点审批。若把 `trust=approval` 的 MCP 交给会调它的数字员工，**每次运行都会卡死在审批上**。

- 用户要「定时用这个 MCP」→ 必须说清楚：要么该 server `trust=full`（明确告知风险），要么 waker 的 `permission_mode=full_access` 且用户接受 MCP 工具不再逐次确认。
- 不要在没警告的情况下把新 MCP 写进 waker 的工具白名单。

## 对话边界

- `create_mcp` / `remove_mcp` 会弹审批卡——如实呈现配置，别在文本里替用户「默认同意」。
- docker / 需要付费账号却没给密钥 / 明显是恶意或要扫全盘的 server：拒绝接入并说明原因。
- 你自己在 subagent 上下文看不到这些工具，属正常（子代理不能改 MCP 配置）。
