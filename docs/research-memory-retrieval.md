# 记忆检索/匹配机制调研 —— 业界做法与本项目差距

> 调研日期：2026-08-27 · 工具：DDG。目的：回答"当前关键词覆盖率匹配是否落后"，为 /dev-adr 检索选型提供事实底座。
> 本文只陈述事实与代价，不做选型决策。

## 0. 本项目现状（对照基准）

检索打分公式（sqlite_provider.py `_score_memories`，与 file_store 逐行一致）：

```
查询分词：英文按词、中文逐字切分
score = 0.5 + 0.5 × (命中查询词数 ÷ 查询总词数)   # 任一单字命中即 >0.5
阈值：memory_min_score（语义上=覆盖率门槛，0.7 ≈ 要求覆盖 ≥40% 查询词）
排序：纯分数降序，top-k（默认 5 条）；同分无次级信号
执行方式：get_all() 全表载入后 Python 线性打分（O(N)，无索引）
```

已知缺陷（有实测佐证）：换措辞即漏召回（无语义泛化）；中文逐字切分引入大量单字噪声；每轮对话首步全表扫描；纯相似度排序缺新近度权重；"全命中"曾长期出现（出厂阈值落在数学失效区）。

## 1. 业界做法

### 1.1 Mem0 —— 与本项目写入侧同构，检索侧向量为主
- 写入：LLM 提取原子事实 → embedding → 与存量记忆算余弦相似度路由 ADD/UPDATE/DELETE/NOOP（本项目 Decider 流程与此高度同构，差异只在相似度来源是关键词而非向量）。
- 检索：query 向量化 → 向量库 ANN 余弦召回 top-k → 可选第二遍重排（LLM 或交叉编码器 Reranker），按相关度输出。
- 要点：**语义向量承担主匹配，关键词只作过滤兜底**；重排器解决"语义近但不含答案"的错排。

### 1.2 Letta/MemGPT —— 分层，把"最重要的记忆"移出检索问题
- 核心记忆块常驻 system prompt、随时可编辑（不存在召回准确性问题）；archival/recall 层才按需做 embedding 语义检索。
- 启示：先用信息分级决定"哪些根本不用检索"，剩下的再做语义匹配；检索范围越小，匹配质量越不敏感。

### 1.3 Generative Agents（斯坦福）—— 三信号加权已成为通用启发式
- score = recency（指数时间衰减）+ relevance（embedding 相似度）+ importance（LLM 对事件打的 1-10 分），归一化加权取 top-k。
- 配合周期性 reflection 把原始观察合成为高阶洞察。生产系统普遍按用途调三权重（事实回忆偏 relevance，对话连续性偏 recency）。
- 启示：**新近度与重要性是本项目完全缺失的两个正交信号**，可与任何相关性算法组合。

### 1.4 主流工程形态 —— 两段式混合召回
- retrieve-then-rerank 是通行范式：第一阶段宽召回（向量 ANN 与 BM25 关键词并行融合），第二阶段精排（交叉编码器/LLM）。AgentGal 等实现均为"向量 + BM25 + recency/importance 启发分"三者融合。
- 本地化趋势：sqlite-vec 在 SQLite 进程内做 kNN 向量检索，配 sentence-transformers/llama.cpp 本地小模型生成 embedding，单文件依赖获得语义检索；多篇基准文章结论指向"绝大多数 agent 场景不需要外部向量库"。与本仓历史决策（主动移除 Qdrant、坚持本地优先、零外部服务）完全兼容。

## 2. 差距 → 三条递进路径（仅列事实与代价）

| 路径 | 内容 | 得到什么 | 代价 |
|---|---|---|---|
| A 廉价档 | SQLite FTS5 全文索引替代逐字覆盖率 + 新近度加权排序 | O(logN) 检索；消除单字噪声；近期记忆优先；零新依赖 | 仍无语义泛化 |
| B 语义档 | sqlite-vec + 本地小模型 embedding，FTS5 关键词 + 向量双路召回融合（含 recency 权重） | 真正的语义泛化；保持单文件本地架构；混合召回是业界主流形态 | 引入一个 Python 包 + 一个几百 MB 级本地模型；需建向量回填机制 |
| C 管理档 | Mem0 化：外部服务或 LLM 重排阶段接入现链路（Decider 已具备同构基础） | 效果上限最高、多级 scope/图记忆等能力 | 外部服务或每轮额外 LLM 成本/延迟 |

## 3. 选型待决点（/dev-adr 输入）
- 是否愿意为语义检索接受本地小模型的存在与首载耗时；
- 三信号里 recency/importance 各自要多少权重、importance 由谁评（写入时 LLM 打分 vs 用 source 区分静态权重）；
- 核心高频画像（Letta 式常驻块）是否值得从现有 memories 表中拆出固定注入。

## 附注：本次同步进行的空闲回收移除的行为影响
worker 子进程改为**常驻**（仅登出/应用退出时优雅关闭）：prefs、权限模式、current_sid 不再被周期性重置；`_buckets` 会话桶驻留内存至进程退出（单用户可接受）；后台总结在进程被强杀时的丢失窗口与改前一致，非本次处理项；MCP/技能长连接寿命随之变长，可经既有 `/api/system/health` 观察。

## 来源
- Mem0 Search / Reranker 文档：https://docs.mem0.ai/core-concepts/memory-operations/search ・ https://docs.mem0.ai/open-source/features/reranker-search
- Mem0 架构解析：https://deepwiki.com/mem0ai/mem0/1.1-system-architecture ・ https://www.emergentmind.com/topics/mem-0
- Letta Memory blocks / Archival memory：https://docs.letta.com/v1-sdk/memory/memory-blocks ・ https://docs.letta.com/v1-sdk/memory/archival-memory
- Generative Agents 检索评分：https://github.com/adamcelove/context-efficient-wiki/blob/main/concepts/ai-memory/recency-importance-relevance-memory-scoring.md ・ https://www.agentpatterns.ai/patterns/agent-design/generative-agents-memory-stream/
- 混合召回示例：https://deepwiki.com/huccihuang/AgentGal/4.1-long-term-memory-retrieval ・ 推理感知重排研究 https://arxiv.org/abs/2605.06132
- sqlite-vec 本地方案：https://github.com/asg017/sqlite-vec ・ https://deepwiki.com/asg017/sqlite-vec/6-use-cases-and-examples ・ https://dev.to/hypernexus/sqlite-vector-search-the-dependency-free-ai-memory-stack-that-outperforms-pinecone-5d27
