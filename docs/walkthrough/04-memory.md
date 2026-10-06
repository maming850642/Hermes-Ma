# 第 4 章 · 记忆层 —— `src/memory/`（纯文件后端 + LLM 去重决策）

> 承接第 1-3 章。本章是项目的**核心成果**：让「记忆」从「系统每轮无脑存的流水账」变成「Agent 能自主决定记什么、更新什么、忘什么」的能力。

---

## 0. 一句话定位

`src/memory/` 是一个**自研的、按用户隔离的长期记忆系统**：用**纯文件**（每用户一份 `profile.md`，jsonl 格式）存记忆、用**关键词匹配**召回、用 **LLM** 提取事实和做 ADD/UPDATE/DELETE/NOOP 去重决策。

它用 8 个文件、约 1175 行代码，实现了完整的「记忆即能力」——**不依赖 Qdrant，不依赖 embedding，不依赖任何外部向量库或网络记忆服务**。

> **重要变更**（2026-06-27，M0）：记忆后端已从 Qdrant 向量库彻底换成纯文件 `FileMemoryStore`。本章正文全部按**当前的纯文件实现**描述。如果你看到别处文档提到 Qdrant/embedding，那是迁移前的历史叙述（见本章末「演进脉络」）。本章不展开向量检索，因为代码里已经没有了。

---

## 1. 模块全景

### 1.1 文件清单

| 文件 | 行数 | 角色 |
|------|------|------|
| `__init__.py` | 17 | 包入口，只导出 `MemoryManager` |
| `models.py` | 88 | 数据模型（`Memory` / `Hit` / `Decision`） |
| `file_store.py` | 234 | **纯文件记忆后端**：jsonl 读写 + 关键词检索 + 原子写 |
| `extractor.py` | 98 | LLM 提取原子事实（含公共 `extract_json`） |
| `decider.py` | 87 | LLM 决策 ADD/UPDATE/DELETE/NOOP |
| `manager.py` | 259 | 门面，组合三者 + 双存储入口 + 兼容壳 |
| `summarizer.py` | 169 | 会话总结 + 事实提取 + 落盘（项目原创） |
| `prompts.py` | 223 | 4 套 LLM 提示词 |

> 合计约 1175 行（截至 2026-07-03）。注意：**没有 `store.py`**——Qdrant 版的存储层在 M0 已被 `file_store.py` 整体替换。

### 1.2 三层架构

```
                ┌─────────────────────────────────────┐
                │       MemoryManager（门面）          │  ← 对外唯一接口
                │  remember_fact / ingest_conversation │
                │  search_with_detail / get_all ...    │
                └──────────┬──────────────────────────┘
                           │ 组合
           ┌───────────────┼───────────────┐
           ▼               ▼               ▼
    ┌──────────────┐ ┌─────────────┐ ┌─────────────┐
    │FileMemoryStore│ │  Extractor  │ │   Decider   │
    │ （存储层）    │ │ （提取层）   │ │ （决策层）   │
    │ 纯文件 jsonl  │ │ LLM→事实    │ │ LLM→动作    │
    │ 关键词检索    │ │             │ │             │
    └──────────────┘ └─────────────┘ └─────────────┘
           │
           ▼
    Summarizer（会话总结，复用 Store + 调用 ingest_conversation）
```

存储层是 `FileMemoryStore`——一个**纯文件、零外部依赖**的后端。提取层和决策层仍然是 LLM 调用，但它们**不碰存储格式**，只产出事实文本和决策动作。这种解耦让「换后端」成为局部改动（见 §3.3 末尾）。

**本章核心看点**：

1. 为什么记忆全文件化、放弃了向量检索（§3.1 / §3.3）
2. 三层架构的职责切分（Store/Extractor/Decider 各管什么）
3. 双存储路径（`remember_fact` 跳过 extract vs `ingest_conversation` 全走）
4. ADD/UPDATE/DELETE/NOOP 决策逻辑 + 三处降级（去重的核心）
5. FileMemoryStore 的几个关键设计（关键词计分、模块级写锁、原子写、per-user 路径、2026-07-03 中文分词修复）
6. prompts.py 的「7 类信息分类 + few-shot + 边界处理」设计

---

## 2. 架构与数据流

### 2.1 记忆的「写入」双路径

```
路径 A（主）：LLM 主动调用 remember 工具
    user 说话 → LLM 判断"值得记" → 调 remember(content) 工具
        → MemoryManager.remember_fact(user_id, content)
            ├─ store.search_candidates(user_id, content, limit=5)  # 关键词召回旧记忆
            ├─ decider.decide(content, candidates)                 # LLM 决策（1 次）
            └─ _apply_decision → store.upsert/delete               # 执行
    特点：agent 已提炼好原子事实 → 跳过 extract，省 1 次 LLM

路径 B（兜底）：会话结束总结
    /exit → session_lifecycle.on_session_end
        → Summarizer.summarize_and_store
            ├─ Step 1（主产品）：SUMMARIZE_PROMPT → 总结 → store.upsert
            └─ Step 2（副产品）：ingest_conversation
                  ├─ extractor.extract_from_session(conv_text)      # LLM 提取事实（1 次）
                  └─ 对每条事实：decide + apply_decision            # LLM 决策（N 次）
    特点：从整段会话提取 → 必须走 extract
```

### 2.2 记忆的「读取」路径

```
每轮回合开头（graph.py retrieve_memory 节点）
    → MemoryOrchestrator.retrieve_with_detail
        → MemoryManager.search_with_detail(user_id, query)
            ├─ store.search(user_id, query, min_score=0.0)   # 先拿原始 top-k（关键词计分）
            ├─ raw_results = 全部召回（未过滤）
            └─ filtered_results = score >= min_score 的子集
        → 返回 memories 列表
    → ContextManager 把 memories 拼进 system_prompt
```

### 2.3 一条记忆的生命周期

```
诞生：remember_fact / ingest_conversation
   → Decision(ADD) → store.upsert(Memory(id=uuid4.hex, content, source))
   → 追加一行 JSON 到 <workspace_root>/users/<user_id>/profile.md

检索：search
   → _tokenize(query) 后逐条记忆按词命中数计分 → Hit(memory, score)

更新：Decision(UPDATE)
   → store.get_by_id(target_id) 拿旧 Memory（保留 created_at）
   → 构造新 Memory(同 id, 新 content) → upsert 全量重写 profile.md

删除：Decision(DELETE)
   → store.delete(target_id) → 读-改-写全量重写 profile.md（临时文件 + os.replace）
```

注意「更新」和「删除」都是**全量重写**整个 `profile.md`，而非原地改一行——因为 jsonl 不支持行级随机写。记忆量小（项目型，单用户几百条以内）时这个代价完全可接受，且全量重写天然简单、可原子化（见 §3.3）。

---

## 3. 逐文件深潜

### 3.1 为什么要全文件化 —— 存储后端的演进动机

记忆层的存储后端经历过两次切换，理解这条演进线才能理解「为什么现在长这样」：

| 阶段 | 后端 | 问题 |
|------|------|------|
| 最初 | **Mem0**（封装的第三方库） | 每轮无脑 extract+store，记一堆寒暄流水账；库黑盒、prompt 不可控、依赖重 |
| 中期 | **Qdrant HTTP 直连**（自研记忆模块 v1） | 解决了 Mem0 黑盒/去重弱，但引入向量库运维负担：要起 Qdrant 服务、要 embedding 模型、换模型要重建 collection、维度不匹配会静默失效 |
| 现在（M0，2026-06-27） | **FileMemoryStore**（纯文件） | 去掉一切外部依赖：无 Qdrant、无 embedding、无网络。检索退化为关键词匹配，精度由 Decider LLM 兜底 |

放弃向量检索的关键判断是：**项目型记忆更依赖文件结构导航（per-user profile.md）而非语义召回**。检索精度不是靠向量相似度硬阈值保证，而是靠「关键词召回候选 + LLM 语义决策」两层——前者负责「大致找得到」，后者负责「真正判 ADD/UPDATE/DELETE」。这把「语义判断」从向量相似度（不可解释、调参玄学）转移到了 LLM（可读 prompt、可讲透）。

> **通用教训**：向量检索的运维成本（部署、维度契约、embedding 模型版本）经常被低估。当你的数据是「按用户天然分区、总量不大、检索精度可由下游 LLM 兜底」时，纯文件 + 关键词 + LLM 决策往往是更省心、更可调试的选择。前提是接受召回从「语义级」降级到「词级」。

---

### 3.2 `models.py` —— 数据模型

三个 dataclass：`Memory`、`Hit`、`Decision`。

#### Memory 的字段（`models.py`，`Memory` dataclass）

```python
@dataclass
class Memory:
    user_id: str
    content: str
    source: str = "tool:remember"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    updated_at: float | None = None
    prev_content: str | None = None   # 纯内存，不持久化
```

**没有 `access_count` / `last_accessed` 字段**。这两个字段曾出现在 Qdrant 时代的 payload 里（想做「记忆访问热度统计」），但**从未被真正更新**——是「设计了但没实现」的残留。`from_point` 的注释（`models.py` 第 51 行附近）明确写了「旧数据 payload 里可能残留 access_count/last_accessed 字段（已废弃，不再映射到 Memory），直接忽略即可」。M0 文件化后，`_read_all` 也根本不解析它们，所以新数据里彻底没有这两个字段。

#### source 字段（记忆可溯源）

```python
source: str = "tool:remember"   # 默认值就是主路径
# 注释列出三种来源：
# - "tool:remember": LLM 主动调用 remember 工具存入
# - "session_summary": 会话结束总结路径存入
# - "legacy": 从旧后端迁移而来
```

**动机**：source 字段让记忆可溯源——排查时能区分「这条是 agent 主动记的」还是「会话总结自动提取的」。`_read_all` 对缺 source 的旧行回退为 `"legacy"`，兼容历史存量数据。

#### prev_content 不持久化（纯内存字段）

```python
prev_content: str | None = None
"""UPDATE 时的旧内容，供事件展示；新增时为 None。
    不持久化到 payload，只在内存中临时存在。"""
```

注意 `_write_all`（`file_store.py`）**没有**写 prev_content，`_read_all` 也恢复不了它。这是个**纯内存字段**——只在 `_apply_decision`（`manager.py`）构造 UPDATE 的 Memory 时临时填上，供 UI 展示「旧内容→新内容」的对比，用完即弃。

> **设计哲学**：持久化层（jsonl）只存「重建 Memory 必需的」字段；临时展示用的字段不落库。这避免了文件膨胀，也让「旧内容」不会永久残留（下次检索时 prev_content 又是 None）。

#### id 用 uuid4 十六进制

```python
id: str = field(default_factory=lambda: uuid.uuid4().hex)
```

**为什么用 `.hex`（32 位无连字符）而非 `str(uuid4())`（带连字符）**：更紧凑，且 Decider 在 prompt 里引用 id 时 JSON 更干净。Decider 返回的 `target_id` 会原样传给 `store.get_by_id` / `store.delete`，所以 id 格式必须全程一致。由于文件化后不再有「point id」的概念，hex 纯粹是项目沿用的命名习惯。

> 注意：`to_payload` / `from_point` 这两个方法是 **Qdrant 时代的残留**——当前 `FileMemoryStore` 的读写走的是 `_read_all` / `_write_all`，**不调用**这两个方法。它们目前只被 `models.py` 的 docstring 引用，属于待清理的死代码（详见 §4.2）。

---

### 3.3 `file_store.py` —— 纯文件记忆后端（本章重头戏）

这是 M0 替换 Qdrant 的核心文件。用 jsonl 存记忆，关键词计分检索，原子写保证完整性。**不依赖 Qdrant、不依赖 embedding、不依赖网络。**

#### 设计 1：per-user profile.md 路径（`_profile_path`）

```python
def _profile_path(self, user_id: str) -> Path:
    p = self._root / "users" / user_id / "profile.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
```

每个用户一份 `<workspace_root>/users/<user_id>/profile.md`，文件格式是 **jsonl**（每行一条 JSON）。`workspace_root` 来自配置；为空时回退到 `PROJECT_ROOT / "data" / "memories"`（保持向后兼容）。

**为什么「每用户一个文件」而非「所有用户一个 collection + 过滤」**（Qdrant 时代的做法）：文件天然按目录分区，多用户隔离**靠文件系统路径**而不是查询时带 filter——**不可能串读**，因为根本读不到别人的文件。这比「同一个 collection 靠 payload filter 隔离」更稳（后者漏带 filter 就串用户）。代价是 `get_by_id` 需要扫所有用户目录（见设计 4）。

> **对比 remember 工具**：第 1-2 章讲过，remember 用 contextvars 隔离 user_id（防并发竞态）；这里用文件路径隔离（防存储串读）。两层隔离，前者管「当前请求的 user_id 是谁」，后者管「只取这个 user_id 的文件」。

#### 设计 2：关键词计分检索（`search`，**无 embedding**）

```python
def search(self, user_id, query, limit=5, min_score=0.0) -> list[Hit]:
    mems = self._read_all(user_id)
    query_terms = _tokenize(query)
    for m in mems:
        content_lower = m.content.lower()
        matched = sum(1 for t in query_terms if t.lower() in content_lower)
        if matched == 0:
            continue
        score = 0.5 + 0.5 * (matched / len(query_terms))   # ← 关键计分
        if score >= min_score:
            hits.append(Hit(memory=m, score=score))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]
```

**计分公式**：`score = 0.5 + 0.5 * (命中查询词数 / 查询词总数)`。

- 全部词命中 → `score = 1.0`
- 命中一半 → `score = 0.75`
- 命中一个词 → `score = 0.5 + 0.5*(1/N)`

**为什么基准是 0.5 而不是 0**：代码注释直说——「基准 0.5 保证任意命中的记忆都过 `min_score=0.4`（向量时代的阈值）」。M0 把阈值留在配置里沿用 `0.4`，所以计分基准必须 ≥0.4 才能让「有命中」的记忆不被误过滤掉。这是为了**在换后端时不动配置、不破坏现有召回行为**而做的有意对齐。

**为什么「无语义召回」可接受**：检索在这里只负责「找候选」，真正判重/合并/删除是 Decider 的 LLM 语义判断（§3.5）。召回从「语义级」降到「词级」，损失的是「近义词召回」（「姓名是张三」可能召不回「用户叫张三」），但 Decider 兜底了精度。项目型记忆里关键词重合度高，这个折中是划算的。

> **注意 min_score 的两层语义**：`search_candidates`（给 Decider 用）固定 `min_score=0.0`（不过滤，拿原始召回让 LLM 判断）；`search_with_detail`（给 retrieve_memory 用）用配置的 `memory_min_score`（默认 0.4）做最终过滤。同一个 `search` 方法，两种调用方式。

#### 设计 3：模块级写锁 + 原子写（`_WRITE_LOCK` / `_write_all`）

```python
_WRITE_LOCK = threading.Lock()   # 模块级单写锁

def _write_all(self, user_id, mems) -> None:
    """全量重写 profile.md（原子：临时文件 + os.replace）。"""
    ...
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)            # ← 原子替换
```

**为什么需要写锁**：`upsert` / `delete` / `delete_all_by_user` 都是「读-改-写全量」——先 `_read_all` 再 `_write_all`。如果两个线程并发 upsert 同一用户，可能出现「A 读 → B 读 → A 写 → B 写（覆盖 A）」的丢失更新。模块级 `threading.Lock` 把这些操作串行化。

**为什么是「模块级单锁」而非「每用户一把锁」**：注释解释得很清楚——「记忆写非高频路径，竞争可忽略」。per-user 锁更精细但要维护锁字典、防泄漏，复杂度不值。单锁在并发写不同用户时会互相等待，但记忆写入频率低（用户主动 remember + 会话结束总结），实际等待可忽略。

**为什么用「临时文件 + `os.replace`」做原子写**：`os.replace` 在 POSIX 上是原子的 `rename`，在 Windows 上也保证原子性。这样即使写到一半进程崩溃，`profile.md` 要么是完整的旧版、要么是完整的新版，**不会出现半截 JSON 行**。这比「直接 `write_text` 覆盖」安全得多——后者崩溃时会留下损坏的文件，导致整个用户的记忆全部解析失败。

> **通用教训**：任何「读-改-写全量」的文件操作都要加锁 + 原子替换。锁防并发覆盖，原子替换防崩溃损坏。两者缺一不可。

#### 设计 4：get_by_id 扫所有用户目录（`get_by_id`）

```python
def get_by_id(self, memory_id: str) -> Memory | None:
    """按 id 取单条。需扫描所有用户文件——M0 记忆量小可接受。"""
    users_dir = self._root / "users"
    for user_dir in users_dir.iterdir():
        for m in self._read_all(user_dir.name):
            if m.id == memory_id:
                return m
    return None
```

**为什么这么「笨」**：`get_by_id` 是给 `_apply_decision` 的 UPDATE/DELETE 路径用的——Decider 返回 `target_id`，但 `target_id` 不带 user_id 信息。per-user 文件结构下，要按 id 找记忆就得遍历所有用户目录。

**为什么可接受**：注释写「M0 记忆量小可接受；若未来量大可建索引」。UPDATE/DELETE 不是高频操作（只在 remember/会话总结时触发，且仅当 Decider 判定有重复时），单次扫几个用户的几百行 jsonl 是毫秒级。这是用「时间换简单」的有意取舍——建 id→user 索引要维护一致性（删除/更新时同步），当前规模不值得。

> **对比 Qdrant 时代**：那时 `get_by_id` 是 `GET /collections/{name}/points/{id}`，O(1)。文件化后退化成 O(所有用户记忆总数)。这是「去外部依赖」付出的代价之一，但在当前规模下被显式接受了。

#### 设计 5：2026-07-03 中文按字分词修复（`_tokenize`，对应 git log「记忆召回稳定性修复」）

```python
_EN_WORD = re.compile(r"[A-Za-z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")

def _tokenize(text: str) -> list[str]:
    """简单分词：英文按词、中文按字（无分词库依赖）。
    2026-07-03: 中文改为按字切分（之前把整段中文当一个 term，导致
    "用户叫张三" vs "姓名是张三" 这类近义改写完全不命中，去重候选恒空，
    Decider 短路 ADD，记忆无限重复）。"""
    terms = []
    terms.extend(m.group(0) for m in _EN_WORD.finditer(text))   # 英文整体词
    for m in _CJK.finditer(text):                                # 中文逐字
        terms.append(m.group(0))
    return terms
```

**这是个真 bug 修复**。修复前的中文分词把整段中文（或某段连续中文）当成**一个 term**，导致：

- query「用户叫张三」→ terms = `["用户叫张三"]`（一个整段 term）
- 旧记忆「姓名是张三」里没有「用户叫张三」这个完整子串 → **matched = 0** → 召回为空
- Decider 拿到空候选 → **短路 ADD**（§3.5）→ 同一事实被无限重复存储

修复后按字切分：query「用户叫张三」→ `["用","户","叫","张","三"]`，旧记忆「姓名是张三」含「张」「三」→ 命中 2 个词 → `score = 0.5 + 0.5*(2/5) = 0.7` → 召回成功 → Decider 能看到候选 → 正确判 NOOP/UPDATE。

> **通用教训**：纯关键词检索对中文极不友好，因为中文没有天然空格分词。最简单的兜底是「按字切分」——牺牲一点精度（单字命中太宽泛），换来召回率。精度由下游 Decider LLM 兜底，所以「宁宽勿漏」是对的策略。这正是 git 提交 `4391c01`「记忆召回稳定性修复」修的核心问题。

#### 设计 6：检索失败不抛、单行解析失败跳过

`_read_all` 对每行 `json.loads` 失败时 `logger.warning` 后 `continue`（跳过坏行，不中断）；`search` 在文件不存在时返回空列表。这保证「一两条坏数据不会让整个用户的记忆都读不出来」——降级而非失败。

---

### 3.4 `extractor.py` —— 事实提取（LLM 调用 #1）

核心是 **`extract_json`（公共函数，无下划线）** 的容错：

```python
def extract_json(text: str) -> dict | None:
    """从可能带前后缀文本的 LLM 输出中提取最外层 JSON 对象。
    公共工具函数：被 MemoryExtractor 和 MemoryDecider 共用。"""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
```

**为什么需要这个**：prompt 虽然写了「只输出 JSON」，但 LLM（尤其自托管小模型）偶尔会加解释文字。直接 `json.loads(raw)` 会失败。正则抓最外层 `{...}` 提高成功率。解析仍失败返回 None，调用方降级为空列表。

**注意：函数名是 `extract_json`，没有下划线**。它是 extractor.py 里的**公共函数**，被 `MemoryExtractor.extract` / `extract_from_session` 和 `MemoryDecider.decide` 显式导入复用：

```python
# decider.py
from src.memory.extractor import extract_json   # 公共函数，跨模块复用
```

> 这是个干净的复用——两个 LLM 调用共享同一个 JSON 容错逻辑。（早期文档曾把它记成带下划线的「私有函数被跨模块导入」的坏味道，那个描述对应的是改名前的旧版本，现已不适用。decider.py 顶部 docstring 里还残留一行「复用 extractor._extract_json」的过期措辞，以实际 `import` 语句为准。）

#### 两个入口的区别

```python
def extract(self, user_msg, ai_msg):                  # 单轮对话
    prompt = EXTRACT_PROMPT.format(...)
def extract_from_session(self, conversation_text):    # 整段会话
    prompt = EXTRACT_FROM_SESSION_PROMPT.format(...)
```

两者用**不同的 prompt**（`prompts.py` 里 EXTRACT_PROMPT vs EXTRACT_FROM_SESSION_PROMPT）。单轮的 prompt 是「从一对 user/assistant 提取」；会话的是「从多轮合并文本提取，需通读全局、跨轮去重」。后者更难，prompt 特意强调了「跨轮去重」「最新为准」。

> **实际使用情况**：manager.py 的 `remember_fact`（主路径）**不调 extract**——因为 agent 已经提炼好原子事实了。只有 `ingest_conversation`（会话总结路径）调 `extract_from_session`。单轮的 `extract` 方法目前**没有生产调用方**，仅被 `tests/test_memory/test_extractor.py` 覆盖（保留供未来单轮路径复用，extractor.py docstring 有说明）。详见 §4.2。

---

### 3.5 `decider.py` —— 去重决策（LLM 调用 #2，本章灵魂）

Decider 是自研记忆的核心。它对每条新事实决策：ADD/UPDATE/DELETE/NOOP。

#### 短路优化：无候选直接 ADD（`decide`）

```python
# 无候选 → 直接 ADD，省一次 LLM 调用
if not candidates:
    return [Decision(action="ADD", content=new_fact)]
```

**动机**：`store.search_candidates` 返回空，说明记忆库里没有相似的，必是全新事实。无需调 LLM 决策，直接 ADD。这把「常见情况」（记全新东西）优化成 **0 次 LLM 调用**，只有可能重复时才花 1 次 LLM。

> **注意：这条短路的正确性依赖召回质量**。如果 `search_candidates` 因为分词问题恒返回空（正是 §3.3 设计 5 那个 bug），Decider 就会无脑 ADD，导致记忆无限重复。这正是 2026-07-03 分词修复要解决的症状——召回空了，短路就失灵了。

> **成本账**（`manager.py` 注释）：
> - `remember_fact` 主路径：通常 1 次 LLM（decide），无候选时 0 次
> - `ingest_conversation`：1 次 extract + N 次 decide（N=事实条数）

#### 安全降级：异常一律 ADD（`decide`）

```python
data = extract_json(raw)
if data is None:
    return [Decision(action="ADD", content=new_fact)]      # 解析失败 → ADD
...
except Exception as e:
    logger.error(f"决策失败，默认 ADD: {e}")
    return [Decision(action="ADD", content=new_fact)]      # 异常 → ADD
```

**动机**：「宁可多记也不丢信息」。LLM 决策失败时，最安全的选择是 ADD——即使造成重复，后续还能再 decide 去重；但如果因为决策失败而 NOOP 或丢弃，信息就真丢了。

> **取舍**：这会导致「LLM 抽风时记忆重复」，但重复可清理，丢失不可恢复。对「记忆」这种 append-mostly 的数据，这是正确的偏好。

#### 决策的四个动作（`prompts.py` DECIDE_PROMPT）

prompt 用大量 few-shot 教 LLM 区分 UPDATE vs DELETE（最容易错的）：

| 动作 | 何时 | 示例 |
|------|------|------|
| ADD | 全新信息 | 无旧记忆 |
| UPDATE | 补充/细化，新旧可共存 | 旧"喜欢 Python" + 新"主要做数据分析" → 合并 |
| DELETE | 矛盾/推翻，新旧互斥 | 旧"喜欢爬山" + 新"不喜欢爬山" |
| NOOP | 语义完全等价 | "叫小明" vs "名字是小明" |

prompt 特意强调：「宁可 UPDATE 也不要误 DELETE」。因为 DELETE 不可恢复（虽然 `_apply_decision` 的 DELETE 失败会降级 NOOP，见 §3.6），UPDATE 至少保留信息。

> **设计精髓**：Decider 把「记忆去重」从「相似度硬阈值」升级成「LLM 语义判断」。关键词检索（search_candidates）只负责「找候选」，真正判 ADD/UPDATE/DELETE 是 LLM 理解语义后决定的。这比纯相似度 dedup 强得多——后者无法区分「补充」和「矛盾」。

#### enable_thinking 的差异

```python
# decider.py —— 决策用，开 thinking
extra_body={"chat_template_kwargs": {"enable_thinking": True}}
# extractor.py —— 提取用，关 thinking
extra_body={"chat_template_kwargs": {"enable_thinking": False}}
```

Decider 开启 thinking（决策需要推理 UPDATE vs DELETE 这种细微判断），Extractor 关闭 thinking（提取是模式匹配，不需要推理，关掉更快）。两个 LLM 客户端都 `temperature=0` 保证稳定输出。

---

### 3.6 `manager.py` —— 门面与双存储入口

#### `_apply_decision` 的三处降级

```python
def _apply_decision(self, user_id, source, decision) -> str:
    if decision.action == "ADD":
        self.store.upsert(mem)
        return "ADD"
    elif decision.action == "UPDATE":
        if not decision.target_id:
            # 降级 1：缺 target_id → 降级为 ADD（保持信息不丢）
            self.store.upsert(mem)
            return "ADD"
        prev = self.store.get_by_id(decision.target_id)
        updated = Memory(
            id=decision.target_id, ...,
            created_at=prev.created_at if prev else time.time(),   # 降级 2：保留 created_at
            updated_at=time.time(),
            prev_content=prev.content if prev else None,
        )
        self.store.upsert(updated)
        return "UPDATE"
    elif decision.action == "DELETE":
        if decision.target_id:
            if self.store.delete(decision.target_id):
                return "DELETE"
            # 降级 3：删除失败 → 降级为 NOOP（不向用户谎报已删除）
            return "NOOP"
        return "NOOP"
```

**三处降级**：

1. **UPDATE 缺 target_id → 降级 ADD**：LLM 决策 UPDATE 但忘了给 target_id。与其丢弃，降级成 ADD（信息不丢，虽会重复）。
2. **UPDATE 保留 created_at**：`get_by_id` 拿旧 Memory，保留它的 `created_at`，只更新 content 和 `updated_at`。这让 UPDATE 是「演进」而非「覆盖」——记忆的「诞生时间」不因内容更新而丢失。**注意：保留的是 `created_at`，不是 `access_count`**（那个字段已不存在，见 §3.2）。
3. **DELETE 失败 → 降级 NOOP**：`store.delete` 返回 False 时，不谎报 DELETE，而是 NOOP。注释：「不向用户谎报已删除」。

> **统一原则**：所有降级都遵循「信息不丢、不谎报」。ADD 是最安全的兜底，NOOP 是「诚实的不作为」。

#### 双存储入口的设计

```python
# 路径 A：remember_fact（agent 已提炼好事实）→ 跳过 extract
def remember_fact(self, user_id, content, source="tool:remember"):
    candidates = self.store.search_candidates(user_id, content, limit=5)
    decisions = self.decider.decide(content, candidates)        # 只 decide
    events = [self._apply_decision(user_id, source, d) for d in decisions]

# 路径 B：ingest_conversation（会话总结）→ extract + decide
def ingest_conversation(self, user_id, messages, session_id=None):
    facts = self.extractor.extract_from_session(conv_text)      # 先 extract
    for fact in facts:
        candidates = self.store.search_candidates(user_id, fact, limit=5)
        decisions = self.decider.decide(fact, candidates)       # 再 decide 每条
        for d in decisions:
            events.append(self._apply_decision(...))
```

**为什么两条路径**（`manager.py` 顶部文档）：
- `remember_fact`：agent 调 remember 工具时，content 已经是原子事实（prompt 教过 LLM 拆分），不需要再 extract。**省一次 LLM**。
- `ingest_conversation`：会话总结拿的是整段对话文本，必须先 extract 拆成原子事实，再逐条 decide。

这是「agent memory」设计的核心——把 extract 的成本花在「真正需要」的地方（会话总结），而 agent 主动记的场景跳过它。

#### 兼容壳（保持旧接口签名）

```python
def add(self, user_id, messages, session_id=None) -> bool:
    return self.add_with_detail(...)["success"]
def add_with_detail(self, user_id, messages, session_id=None) -> dict:
    return self.ingest_conversation(user_id, messages, session_id)
def search(self, ...) -> list[dict]: ...        # 委托 search_with_detail
def search_with_detail(self, ...) -> dict: ...
def get_all(self, user_id) -> list[dict]: ...
def delete_all(self, user_id) -> bool: ...
```

`add` / `add_with_detail` / `search` 等是旧时期的接口，cli.py/web.py 还在用。这里委托给新的 `ingest_conversation` / `search_with_detail`，**保持签名兼容**。这是迁移期间的关键策略——内部全换（Qdrant→FileMemoryStore），对外接口不动（`manager.py` docstring：「cli.py/web.py/graph.py 零改动」）。

> **接口对齐的妙处**：`FileMemoryStore` 的方法签名（`upsert` / `search` / `search_candidates` / `get_by_id` / `get_all` / `delete` / `delete_all_by_user`）和旧 Qdrant 版 `MemoryStore` 完全一致。所以 `MemoryManager.__init__` 里把 `self.store = MemoryStore(...)` 换成 `self.store = FileMemoryStore(...)` 就完成了后端切换，其余代码零改动。这是「接口契约」隔离实现细节的经典收益。

---

### 3.7 `summarizer.py` —— 会话总结（项目原创）

Summarizer 做两件独立的事：

#### Step 1（主产品）：会话 → 结构化总结（`summarize_and_store`）

```python
prompt = SUMMARIZE_PROMPT.format(conversation_text=conversation_text)
resp = self.llm.invoke(prompt)
summary_text = (getattr(resp, "content", "") or "").strip()
if summary_text:
    mem = Memory(
        user_id=user_id,
        content=f"[会话总结 {session_id}]\n{summary_text}",
        source="session_summary",
    )
    self.manager.store.upsert(mem)                     # 直接用 store 写
```

**关键点**：总结作为一条 `Memory` 存入 profile.md，`source="session_summary"`，content 带 `[会话总结 {sid}]` 前缀。下次检索时，这条总结会和其它事实一起被召回，给 Agent 提供会话级上下文。

#### Step 2（副产品）：会话 → 长期事实（复用 ingest_conversation）

```python
# 复用 manager.ingest_conversation，不再手写复制逻辑
messages = [{"role": "user", "content": conversation_text}]
result = self.manager.ingest_conversation(user_id, messages, session_id)
```

**动机**（`summarizer.py` docstring，I4 修复）：早期 Step 2 手写了一遍 `extract_from_session` 调用，和 `ingest_conversation` 的逻辑重复。代码漂移后两边不一致。修复后复用 `ingest_conversation`，统一 extract+decide 路径。

> **两步独立性**：Step 1 失败不影响 Step 2 尝试，反之亦然。各自的 try-except 独立。这保证「即使总结生成失败，事实提取仍可能成功」——最大化记忆沉淀。

#### 落盘 Markdown（`save_summary_markdown`）

总结成功后，除了写 profile.md，还落盘一份 Markdown 到 `data/summaries/{user_id}/{session_id}.md`。让用户能用任意编辑器查看，不依赖记忆检索。落盘失败只记日志不影响主流程。返回结构为 `{summary_stored, facts_count, markdown_path, summary_text}`。

> **联系第 3 章**：session_lifecycle 的 `on_session_end` 传 `summaries_dir` 给 Summarizer，触发落盘。这是「会话结束 hook → 总结 → 双写（profile.md + summaries Markdown）」的完整链路。

---

### 3.8 `prompts.py` —— 提示词工程（可读、可改、可讲透）

`prompts.py` 的注释点明设计目标：「所有提示词可读、可改，每条规则都有业务理由」。4 套 prompt 各有侧重：

| Prompt | 用途 | 设计亮点 |
|--------|------|---------|
| EXTRACT_PROMPT | 单轮提取 | 7 类该记 + 6 类不该记 + 原子性/陈述句/去上下文化原则 |
| EXTRACT_FROM_SESSION_PROMPT | 会话提取 | 在单轮基础上加「跨轮去重」「最新为准」 |
| DECIDE_PROMPT | 去重决策 | 重点教 UPDATE vs DELETE 区分 + few-shot + 「宁 UPDATE 勿 DELETE」 |
| SUMMARIZE_PROMPT | 会话总结 | 4 维度（诉求/探索/结论/遗留）+ 按对话类型调整侧重 |

#### EXTRACT_PROMPT 的「不该记什么」

```
不该记什么：
- 寒暄客套（"你好"、"谢谢"、"在吗"）
- 一次性情绪/临时状态（"今天好烦"、"很开心"、"有点累"）
- 通用常识（"Python 是解释型语言"、"水 100 度沸腾"）
- 本轮一次性任务的具体执行过程/中间步骤（只记结论，不记过程）
- 推测、假设、尚未确定的内容
- 已被用户明确否定或推翻的旧信息
```

**动机**：这正是「不记流水账」原则的具体化。从源头控制记忆质量——这些 prompt 每条规则都有业务理由，不是玄学调参。

#### DECIDE_PROMPT 的 few-shot

三个示例覆盖三种典型：DELETE（矛盾）、UPDATE（补充）、NOOP（等价）。这让 LLM 学会区分的关键信号词：「也/还/主要」 → UPDATE；「不/改用/换成了」 → DELETE。

---

## 4. 设计权衡总结

### 4.1 优点

| 设计 | 价值 |
|------|------|
| 纯文件后端（FileMemoryStore） | 零外部依赖：无 Qdrant、无 embedding、无网络；部署只需一个目录 |
| per-user profile.md | 多用户隔离靠文件路径，不可能串读 |
| 三层架构（Store/Extractor/Decider） | 职责清晰，Store 换后端是局部改动（接口对齐即零改动切换） |
| 双存储路径 | agent 自主记（精）+ 会话总结（兜底），避免流水账 |
| Decider 四态决策 | 语义级去重，远强于相似度 dedup |
| 无候选短路 ADD | 常见情况 0 次 LLM，成本最优 |
| 异常降级 ADD | 宁可重复不可丢失 |
| UPDATE 保留 created_at | 记忆是演进而非覆盖 |
| 原子写 + 模块级写锁 | 防并发覆盖、防崩溃损坏 |
| source 字段溯源 | 区分记忆来源，便于排查 |

### 4.2 技术债 / 待优化

| 项 | 说明 |
|----|------|
| `extract` 单轮方法无生产调用方 | 仅 tests 覆盖；保留供未来单轮路径复用 |
| 每次 decide 独立 LLM 调用 | ingest_conversation 的 N 条事实 = N 次 LLM，成本高（有 session_lifecycle 超时兜底） |
| 无批量 decide | 可优化为一次 LLM 处理多条事实 |
| `get_by_id` 扫所有用户目录 | O(所有用户记忆数)，当前规模可接受，量大需建索引 |
| `models.py` 的 `to_payload`/`from_point` | Qdrant 时代残留，文件化后无调用方，待清理 |
| `decider.py` docstring 残留 `_extract_json` 措辞 | 函数已改名 `extract_json`，docstring 措辞未同步（以实际 import 为准） |
| 关键词检索无语义召回 | 中文按字分词已大幅改善，但近义改写仍可能漏召；精度靠 Decider 兜底 |

---

## 本章小结

记忆层是 Hermes-Ma 最深思熟虑的模块，它的复杂性全部服务于一个目标：**让记忆从"系统强塞的流水账"变成"Agent 自主控制的、去重的、可溯源的能力"**。

**三个层次的设计内核**：

| 层 | 内核 | 最值得记住的一点 |
|----|------|----------------|
| Store (FileMemoryStore) | 纯文件 jsonl + 关键词计分 + 原子写 + per-user 路径 | **零外部依赖**，隔离靠文件路径，精度由下游兜底 |
| Extractor | LLM 提取 + JSON 容错 | **不该记什么**比「该记什么」更重要 |
| Decider | 四态决策 + 短路 + 降级 | **语义级去重**（UPDATE vs DELETE）是核心能力 |

**演进脉络（历史）**：存储后端经历了 **Mem0（第三方库，黑盒+流水账）→ Qdrant HTTP 直连（自研记忆 v1，引入向量库运维负担）→ FileMemoryStore（纯文件，零依赖）** 三代。当前是第三代，关键词检索 + LLM 决策替代了向量检索 + 相似度 dedup。这条演进线的驱动力始终是「减少运维负担、提升可调试性、让语义判断回归 LLM」。

**和前三章的呼应**：
- 第 1 章 graph.py 的 `store_memory` 节点 no-op 化，正是因为本章的「双存储路径」接管了记忆写入
- 第 2 章 tools.py 的 ResultHandler 注册表，和本章的「门面 + 三层」是同一种「组合优于继承」的架构思想
- 第 3 章 memory_orch 的「build_enhanced_query 废弃」，和本章 Decider 的「检索只找候选、LLM 才决策」一脉相承——**不要让检索相似度承担语义判断的责任**

下一章进入 `src/tools/`——Agent 「手脚」所在，涵盖文件系统、子智能体、网页抓取、shell 执行、记忆工具等。
