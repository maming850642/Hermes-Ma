"""
SQLiteProvider —— 基于标准库 sqlite3 的存储实现，三协议（记忆/事件/KV）全实现。

职责：
- MemoryStoreProtocol: memories 表 CRUD + 关键词检索 + 聚合替换/备份恢复
- EventLogProtocol:    events 表 append-only 事件流（scope × session_id 隔离）
- KVProtocol:          kv 表 scope 隔离的 JSON 键值

设计要点：
- 单库文件（默认 data/hermes.db）。连接统一 PRAGMA：journal_mode=WAL +
  busy_timeout=5000 + synchronous=NORMAL；进程内再用模块级 RLock 粗粒度
  串行化所有操作——双实例同库（各自连接）与多线程并发均安全，跨进程
  依赖 WAL + busy_timeout 兜底。本项目规模下粗锁足够，不做读写锁分离
- 检索为纯向量打分：sqlite-vec 语义近邻（重标定余弦）× 新近度衰减 × 相对
  门槛；FTS5 关键词（预分词，英文按词/中文逐字）只作降级通道——向量通道
  整体不可用时兜底并标 detail.source="keyword"（FTS 索引照常写入）；file
  后端仍是旧关键词语义，两者一致性约定已解除（sqlite 为主后端）
- memories 表无 user_id 列（单用户架构），读回时 user_id 恒填 LOCAL_USER
- memories.project：项目 slug（""=全局；检索本项目 ∪ 全局）
- replace_all 单事务（BEGIN IMMEDIATE）：备份旧全量到 snapshots →
  DELETE 全部 → INSERT 新集（可经 baseline 行级版本守卫过滤）→
  幸存行回插（baseline 行级"活写入者优先"，或旧版 protect_outside
  仅护窗口内新增的 id；详见 base.apply_baseline_guard）
- 全部 JSON 落库 ensure_ascii=False（中文原文可读可 grep）
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from src.constants import LOCAL_USER
from src.memory.models import Hit, Memory, apply_baseline_guard, memory_visible_in
from src.storage import paths

logger = logging.getLogger("hermes.storage.sqlite")

_SCHEMA_VERSION = "6"

# 版本迁移链：旧版本号 → (迁移函数, 迁移后版本号)。幂等执行、持锁调用。
_MIGRATIONS: dict[str, tuple] = {}


def _migrate_1_to_2(conn: sqlite3.Connection) -> None:
    """v1→v2：新增 projects 表。表结构由 _DDL 的 CREATE IF NOT EXISTS 幂等
    建出，这里无需额外数据搬移，仅作版本推进的显式钩子。"""


def _migrate_2_to_3(conn: sqlite3.Connection) -> None:
    """v2→v3：memories 增加 project 列（空=全局，检索本项目 ∪ 全局）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    if "project" not in cols:
        conn.execute(
            "ALTER TABLE memories ADD COLUMN project TEXT NOT NULL DEFAULT ''"
        )


def _migrate_3_to_4(conn: sqlite3.Connection) -> None:
    """v3→v4：新增 llm_usage 表（LLM 用量记账，见 usage_store）。表结构由
    _DDL 的 CREATE IF NOT EXISTS 幂等建出（_init_db 先 executescript 再进
    迁移链，钩子执行时表必已在），无数据搬移，仅作版本推进的显式钩子。"""


_MIGRATIONS["1"] = (_migrate_1_to_2, "2")
_MIGRATIONS["2"] = (_migrate_2_to_3, "3")
_MIGRATIONS["3"] = (_migrate_3_to_4, "4")


def _migrate_4_to_5(conn: sqlite3.Connection) -> None:
    """v4→v5：llm_usage 增加调用详情三列（req_messages/reasoning/output，
    供看板明细 👀 弹窗回看单次调用的请求与输出；写入侧在 usage_store
    截断，见 DETAIL_*_MAX）。存量行三列留空串——历史调用没有留底，
    前端对空值显示「—」。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_usage)").fetchall()}
    for name in ("req_messages", "reasoning", "output"):
        if name not in cols:
            conn.execute(
                f"ALTER TABLE llm_usage ADD COLUMN {name} TEXT DEFAULT ''"
            )


_MIGRATIONS["4"] = (_migrate_4_to_5, "5")


def _migrate_5_to_6(conn: sqlite3.Connection) -> None:
    """v5→v6：llm_usage 增加 tools 列（该次调用发起的工具名，逗号分隔，
    供看板明细「工具」列；写入侧在 client.py 从 tool_calls 提取）。存量行
    留空串。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_usage)").fetchall()}
    if "tools" not in cols:
        conn.execute("ALTER TABLE llm_usage ADD COLUMN tools TEXT DEFAULT ''")


_MIGRATIONS["5"] = (_migrate_5_to_6, "6")

# R3-15：memory 快照保留上限（replace_all 成功后滚动清理；backup 与
# pre-restore 同属 kind='memory'，在同一个窗口内滚动）
MEMORY_SNAPSHOT_KEEP = 10

# ---- 检索打分参数（纯向量主通道 + FTS 关键词降级通道）----
# 召回通道宽度（每通道进入打分池的候选数）
CHANNEL_K = 20
# 降级通道（FTS 关键词，source="keyword"）的分数刻度：秩归一 × FTS_WEIGHT
# × 新近度。产品决策 2026-09：正常打分只看向量，关键词只在向量通道整体
# 不可用（sqlite-vec 未加载 / vec 表缺失 / 无 embedder / 查询 embed 失败）
# 时兜底；沿用旧融合时代的 0.3 权重刻度，min_score 语义与既有调用方一致。
FTS_WEIGHT = 0.3
RECENCY_DECAY_PER_HOUR = 0.995
# P2-17 衰减下限钳制。纯指数 0.995^小时 对旧记忆压得过狠：14 天=0.186、
# 30 天=0.027、90 天≈2e-6，而融合分还要先过 RELATIVE_KEEP_RATIO=0.35 的
# 相对门槛——一个月前的记忆与任一新命中竞争需 ~0.35/0.027≈13 倍原始融合分
# 才能幸存，实际等于检索不到。钳到下限 0.3（约 10 天后不再继续衰减）后，
# 30~90 天记忆与新命中竞争只需 0.35/0.3≈1.17 倍融合分——"相关性略强的
# 老事实仍可达"，同时保留近期记忆的时间排序梯度。
RECENCY_FLOOR = 0.3
# 双通道都空转（仅空 query）时的弱先验，按新近度兜底排序，
# 分数压低以示"弱召回"。非空查询零证据一律返回空——诚实显示无命中。
WEAK_RECALL_PRIOR = 0.2
# 语义相似度噪声地板：bge-small-zh 对无关文本的余弦普遍落在 0.5~0.7，
# 不做地板重标定的话"随手一个词"就全库命中。低于地板 → 语义证据归零；
# 地板以上重标定到 (0,1]。
# 2026-09-05 校准（主审实测 data/hermes.db，33 条真实记忆 + 三查询）：
#   floor=0.55（旧值）：正确答案原始余弦仅 ~0.56-0.63，重标定后 s_vec
#   压到 0.02-0.17、整轮只活 1-2 条——"检索找到了但全被门槛扔掉"；
#   floor=0.35（初审建议值）：三查询 gold 分别 #3 / #4 / #1——"OCR 项目
#   进展"差一位（新近度 0.9 的无关会话总结挤到前面）；
#   floor=0.40（采用）：三查询 gold 全部进 top3（#2 / #2 / #1），且每轮
#   3-5 条存活（0.45+ 又回落到 1-2 条的饥饿态）。长尾噪声由
#   RELATIVE_KEEP_RATIO 相对门槛压制，排序仍由原始余弦主导。
VEC_SIM_FLOOR = 0.40
# 相对门槛：融合分低于最高分该比例的命中丢弃（Generative Agents 式
# 归一化实践——砍掉弱相关的长尾并列，命中数才有"相关程度"的含义）
RELATIVE_KEEP_RATIO = 0.35

# 模块级粗粒度锁：同一进程内所有 SQLiteProvider 实例（不同连接、甚至不同
# db 文件）都串行。避免同库双实例的写冲突；代价是无跨库并行——可接受。
_LOCK = threading.RLock()

_DDL = """
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS memories(
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'legacy',
    created_at REAL NOT NULL,
    updated_at REAL,
    project TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_scope_sid ON events(scope, session_id, id);
CREATE TABLE IF NOT EXISTS kv(
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(scope, key)
);
CREATE TABLE IF NOT EXISTS snapshots(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS projects(
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT 'hosted',
    created_at REAL NOT NULL,
    last_opened_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_usage(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id TEXT DEFAULT '',
    scope TEXT DEFAULT '',
    caller TEXT DEFAULT '',
    model TEXT DEFAULT '',
    tokens_in INTEGER,
    tokens_out INTEGER,
    duration_ms REAL,
    status TEXT DEFAULT 'ok',
    error TEXT DEFAULT '',
    req_messages TEXT DEFAULT '',
    reasoning TEXT DEFAULT '',
    output TEXT DEFAULT '',
    tools TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);
"""

# ============================================
# 分词（英文按词、中文逐字；检索/FTS 索引共用）
# ============================================

# 英文/数字词
_EN_WORD = re.compile(r"[A-Za-z0-9_]+")
# 中文字符（CJK 统一汉字范围）
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _tokenize(text: str) -> list[str]:
    """简单分词：英文按词、中文按字（无分词库依赖）。

    中文按字切分（而非整段）是为了近义改写仍能命中（详见 file_store
    2026-07-03 注记）。"用户用 Python" → ["用", "户", "用", "Python"]
    """
    if not text:
        return []
    terms: list[str] = []
    # 英文/数字词
    terms.extend(m.group(0) for m in _EN_WORD.finditer(text))
    # 中文按字（每个汉字单独作为 term，提升近义召回）
    for m in _CJK.finditer(text):
        terms.append(m.group(0))
    return terms


def _row_to_memory(row: sqlite3.Row) -> Memory:
    """memories 表行 → Memory（user_id 恒填 LOCAL_USER，单用户架构）。"""
    keys = row.keys()
    return Memory(
        id=row["id"],
        user_id=LOCAL_USER,
        content=row["content"],
        source=row["source"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        project=(row["project"] if "project" in keys else "") or "",
    )


def _mem_tuple(m: Memory) -> tuple[Any, ...]:
    """Memory → memories 表行元组（插入用）。"""
    return (m.id, m.content, m.source, m.created_at, m.updated_at,
            getattr(m, "project", "") or "")


def _mem_dict(m: Memory) -> dict[str, Any]:
    """Memory → 快照 payload 里的行 dict（备份序列化用）。"""
    return {
        "id": m.id,
        "content": m.content,
        "source": m.source,
        "created_at": m.created_at,
        "updated_at": m.updated_at,
        "project": getattr(m, "project", "") or "",
    }


class SQLiteProvider:
    """SQLite 存储实现：MemoryStoreProtocol + EventLogProtocol + KVProtocol。"""

    def __init__(
        self,
        db_path: str | Path | None = None,
        embedder: Any | None = None,
    ) -> None:
        """
        Args:
            db_path: 库文件路径。None 时用 paths.data_dir("hermes.db")。
                父目录自动创建。
            embedder: 向量客户端（src.memory.embeddings.EmbeddingClient 形状）。
                None = 不启用语义通道（纯关键词检索；生产入口由
                MemoryManager 注入 get_default_embedder()，测试注入 Fake）。
        """
        path = Path(db_path) if db_path is not None else paths.data_dir("hermes.db")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path: Path = path
        # isolation_level=None：autocommit 模式，多语句操作用显式 BEGIN IMMEDIATE
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._embedder = embedder
        self._vec_ready = False
        self._vec_warned = False
        self._kw_fallback_warned = False
        self._has_vec_table = False
        # sqlite-vec 是 loadable extension，必须先于虚表创建加载；
        # 失败只禁用语义通道，不阻断存储本身。
        # 注：sqlite_vec.load() 不会自己 enable_load_extension（Python sqlite3
        # 默认禁用扩展加载），须手动开→载→关。
        try:
            import sqlite_vec

            if hasattr(self._conn, "enable_load_extension"):
                self._conn.enable_load_extension(True)
            try:
                sqlite_vec.load(self._conn)
            finally:
                if hasattr(self._conn, "enable_load_extension"):
                    self._conn.enable_load_extension(False)
            self._vec_ready = True
        except Exception as e:
            logger.warning(f"sqlite-vec 扩展加载失败，语义通道禁用（仅关键词检索）: {e}")
        self._init_db()
        try:
            self._ensure_aux_tables()
        except Exception as e:
            # P2-16：辅助索引建失败（典型：sqlite-vec 扩展缺失时 CREATE
            # VIRTUAL TABLE ... vec0 抛 OperationalError）→ 降级纯关键词检索。
            # 不能让"扩展缺失但 embedder 在"的构造直接崩掉整个启动链。
            self._embedder = None
            self._has_vec_table = False
            logger.warning(f"辅助索引初始化失败，语义通道禁用（仅关键词检索）: {e}")
        logger.info(f"SQLiteProvider 就绪: {path} (schema_version={_SCHEMA_VERSION})")

    # ---------- 内部 ----------

    def _init_db(self) -> None:
        """幂等建表 + schema 版本推进/守门。重复 init（重开连接）无副作用。

        版本规则（ADR-0004 风险 13 的最小迁移机制）：
        - 全新库：直接写入当前版本号；
        - 旧库：沿 _MIGRATIONS 链逐步迁移并推进版本号，直到达到当前版本；
        - 无路径可达的未知版本：拒绝启动（保持旧守门语义）。
        """
        with _LOCK:
            self._conn.executescript(_DDL)
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (_SCHEMA_VERSION,),
                )
                return
            ver = str(row["value"] or "")
            while ver != _SCHEMA_VERSION:
                step = _MIGRATIONS.get(ver)
                if step is None:
                    raise RuntimeError(
                        f"不支持的 schema_version: {ver!r}（期望 {_SCHEMA_VERSION!r}）")
                migrate_fn, next_ver = step
                migrate_fn(self._conn)
                self._conn.execute(
                    "UPDATE meta SET value=? WHERE key='schema_version'", (next_ver,))
                logger.info(f"schema 迁移: v{ver} → v{next_ver}")
                ver = next_ver

    # ---------- 通用执行入口（仅供内聚存储模块如 projects_store 使用；业务代码勿用） ----------

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """通用只读查询。公开它是为了让 projects_store 这类独立小实体把 SQL
        收在自己模块里，而不是逼 SQLiteProvider 膨胀成万能层。"""
        with _LOCK:
            return self._conn.execute(sql, params).fetchall()

    def execute(self, sql: str, params: tuple = ()) -> int:
        """通用写执行，返回 rowcount（约束同 query）。"""
        with _LOCK:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """显式 BEGIN IMMEDIATE 事务（调用方须已持 _LOCK）。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def _unique_label(self, base: str) -> str:
        """生成不冲突的快照 label：{base}-{ts}，同秒冲突追加 -2/-3（语义对齐
        file_store._unique_backup_name）。"""
        ts = int(time.time())
        candidate = f"{base}-{ts}"
        suffix = 2
        while (
            self._conn.execute(
                "SELECT 1 FROM snapshots WHERE kind='memory' AND label=? LIMIT 1",
                (candidate,),
            ).fetchone()
            is not None
        ):
            candidate = f"{base}-{ts}-{suffix}"
            suffix += 1
        return candidate

    # ---------- 辅助索引：FTS5（预分词）+ sqlite-vec 语义 ----------

    def _fts_text(self, text: str) -> str:
        """预分词后灌入 FTS5（英文按词、中文逐字）。

        不用 trigram：它要求 ≥3 字符，中文双字词（爬虫/火锅）永远匹配不上。
        预分词方案让建索引与查询共用同一套 _tokenize 口径（延续与
        file_store 的分词一致性），unicode61 按空格还原 token 即可精确命中。
        """
        return " ".join(_tokenize(text))

    def _ensure_aux_tables(self) -> None:
        """幂等建辅助索引（构造时调用一次）。

        vec 表维度跟随当前 embedder；meta.memory_vec_dim 不符（换模型）
        时删表重建，旧向量作废、由回填脚本重建。
        """
        with _LOCK:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5("
                "content, memory_id UNINDEXED)"
            )
            emb = self._embedder
            if emb is None or not self._vec_ready:
                # P2-16：无向量通道，或 sqlite-vec 扩展未加载成功时不能往下走——
                # CREATE VIRTUAL TABLE ... vec0 会抛 OperationalError 并冒出构造函数。
                # 只建 fts，vec 表留给"扩展 + embedder 双就绪"的实例。
                return
            want_dim = emb.dim()
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='memory_vec_dim'"
            ).fetchone()
            cur_dim = int(row["value"]) if row else None
            if cur_dim == want_dim:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_vecs USING vec0("
                    f"memory_id TEXT PRIMARY KEY, embedding float[{want_dim}])"
                )
                self._has_vec_table = True
                return
            # 首次启用或换模型/维度：旧向量全部作废
            self._conn.execute("DROP TABLE IF EXISTS memory_vecs")
            self._conn.execute(
                "CREATE VIRTUAL TABLE memory_vecs USING vec0("
                f"memory_id TEXT PRIMARY KEY, embedding float[{want_dim}])"
            )
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('memory_vec_dim', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(want_dim),),
            )
            self._has_vec_table = True

    def attach_embedder(self, embedder: Any) -> None:
        """给构造时无 embedder 的实例补挂语义通道（memory 插件组合根用）。

        共享 provider 出于"core 存储不背模型加载"不带 embedder；记忆侧
        在此补挂。FastembedClient 是懒加载包装——挂载只构造对象，首次
        embed 才真正载模型。vec 表按 _ensure_aux_tables 的维度口径幂等
        就绪（可认领其他进程已建的表）；sqlite-vec 扩展未就绪时不挂载，
        建表失败则卸载并降级纯关键词。
        """
        if self._embedder is not None or not self._vec_ready:
            return
        self._embedder = embedder
        try:
            self._ensure_aux_tables()
        except Exception as e:
            self._embedder = None
            self._has_vec_table = False
            logger.warning(f"embedder 挂载失败，语义通道禁用（仅关键词检索）: {e}")

    @staticmethod
    def _vec_serialize(vec: list[float]) -> bytes:
        """float32 小端紧凑二进制（sqlite-vec 虚表的向量入参格式）。"""
        import struct

        return struct.pack(f"<{len(vec)}f", *vec)

    def _embed_missing(
        self, mems: list[Memory], force_ids: set[str] | None = None
    ) -> dict[str, list[float]]:
        """为尚无向量的记忆现算 embedding；失败返回空表（行保持 keyword-only）。

        force_ids：无论向量行是否存在都强制现算。upsert 内容有变的行必须进
        这个集合——只判"memory_vecs 行存在"的话，UPDATE 会因旧行仍在而不重算，
        随后 _sync_aux_rows 又无条件删向量且不回插（P1-6：最常见的写路径
        静默摧毁语义检索）。
        """
        if self._embedder is None or not self._vec_ready or not self._has_vec_table or not mems:
            return {}
        forced = force_ids or set()
        missing = [
            m
            for m in mems
            if m.id in forced
            or self._conn.execute(
                "SELECT 1 FROM memory_vecs WHERE memory_id=? LIMIT 1", (m.id,)
            ).fetchone()
            is None
        ]
        if not missing:
            return {}
        try:
            vecs = self._embedder.embed([m.content for m in missing])
        except Exception as e:
            if not self._vec_warned:
                self._vec_warned = True
                logger.warning(f"向量通道不可用，该批记忆降级 keyword-only: {e}")
            return {}
        return {m.id: v for m, v in zip(missing, vecs)}

    def _sync_aux_rows(
        self,
        mems: list[Memory],
        *,
        clear_all: bool = False,
        force_embed_ids: set[str] | None = None,
        pre_vecs: dict[str, list[float]] | None = None,
    ) -> None:
        """把一批记忆写入/刷新辅助索引（须与 memories 写入同一事务内调用）。

        clear_all=True 用于 replace_all/restore 的全量重建场景。
        force_embed_ids 透传给 _embed_missing（upsert 内容有变的行强制现算）。
        pre_vecs：调用方已在写事务外现算好的向量（replace_all/restore 全量
        重建用，P3：embed 不占 BEGIN IMMEDIATE），非 None 时跳过现算只落库。
        向量缺失的行保持 keyword-only，由预热线程/回填脚本补嵌。
        """
        if clear_all:
            self._conn.execute("DELETE FROM mem_fts")
            if self._has_vec_table:
                self._conn.execute("DELETE FROM memory_vecs")
        vecs = pre_vecs if pre_vecs is not None else self._embed_missing(mems, force_embed_ids)
        forced = force_embed_ids or set()
        for m in mems:
            self._conn.execute("DELETE FROM mem_fts WHERE memory_id=?", (m.id,))
            self._conn.execute(
                "INSERT INTO mem_fts(content, memory_id) VALUES(?, ?)",
                (self._fts_text(m.content), m.id),
            )
            if self._has_vec_table:
                # 只在算出新向量时才替换旧向量行（P1-6：内容未变的行其向量
                # 仍有效——无条件 DELETE 后不回插会静默丢向量）
                v = vecs.get(m.id)
                if v is not None:
                    self._conn.execute(
                        "DELETE FROM memory_vecs WHERE memory_id=?", (m.id,)
                    )
                    self._conn.execute(
                        "INSERT INTO memory_vecs(memory_id, embedding) VALUES(?, ?)",
                        (m.id, self._vec_serialize(v)),
                    )
                elif m.id in forced:
                    # embed 失败且内容有变 → 旧向量对应旧 content，留着就是
                    # "memories 新内容 / memory_vecs 旧向量"的永久错位：
                    # backfill_embeddings 只补"行不存在"，错误向量永不自愈。
                    # 删掉回到"无向量可回填"的可自愈状态（反向回归修复）。
                    self._conn.execute(
                        "DELETE FROM memory_vecs WHERE memory_id=?", (m.id,)
                    )

    def backfill_embeddings(self) -> int:
        """补嵌所有 keyword-only 记忆（向量缺失行），返回本次补嵌条数。

        供启动预热线程与 scripts/backfill_embeddings.py 共用；
        幂等可重复运行，失败行下次仍会重试。
        """
        with _LOCK:
            mems = self.get_all()
            vecs = self._embed_missing(mems)
            if not vecs:
                return 0
            with self._transaction():
                for mid, v in vecs.items():
                    self._conn.execute(
                        "DELETE FROM memory_vecs WHERE memory_id=?", (mid,)
                    )
                    self._conn.execute(
                        "INSERT INTO memory_vecs(memory_id, embedding) VALUES(?, ?)",
                        (mid, self._vec_serialize(v)),
                    )
            return len(vecs)

    def _embed_for_rebuild(self, mems: list[Memory]) -> dict[str, list[float]]:
        """全量重建（replace_all/restore_backup）的预嵌向量，须在写事务外调用。

        此前 embed 留在 _sync_aux_rows 里随 BEGIN IMMEDIATE + 模块锁执行，
        聚合期间 worker 子进程的 remember_fact 全部 database is locked 被
        静默丢弃。挪到事务外先整批算好、事务内只落库，写事务窗口只剩短写。
        失败返回空表（该批 keyword-only，由 backfill_embeddings 收敛）；
        崩溃/中断同样收敛——事务未提交则库未变，已提交则 fts/vec 由回填补齐。
        """
        if self._embedder is None or not self._vec_ready or not self._has_vec_table or not mems:
            return {}
        try:
            vecs = self._embedder.embed([m.content for m in mems])
        except Exception as e:
            if not self._vec_warned:
                self._vec_warned = True
                logger.warning(f"向量通道不可用，该批记忆降级 keyword-only: {e}")
            return {}
        return {m.id: v for m, v in zip(mems, vecs)}

    def _prune_orphan_aux(self) -> None:
        """防御性清理辅助索引里的无主行（与快照滚动清理同一收尾时机）。"""
        with _LOCK:
            if self._has_vec_table:
                self._conn.execute(
                    "DELETE FROM memory_vecs WHERE memory_id NOT IN (SELECT id FROM memories)"
                )
            self._conn.execute(
                "DELETE FROM mem_fts WHERE memory_id NOT IN (SELECT id FROM memories)"
            )

    # ---------- MemoryStoreProtocol：写入 ----------

    def upsert(self, memory: Memory) -> None:
        """插入或更新（按 id 覆盖）。更新时保留库中原 created_at，
        content/source/updated_at 以传入为准。辅助索引同步刷新。

        P1-6：content 与库中既有行不同（首插也算）→ 该 id 强制进入现算
        集合，UPDATE 路径不再复用旧向量（否则 _sync_aux_rows 会删掉旧向量
        且不回插，语义检索静默退化为 keyword-only）。
        """
        with _LOCK:
            with self._transaction():
                row = self._conn.execute(
                    "SELECT content FROM memories WHERE id=?", (memory.id,)
                ).fetchone()
                stale_vec = row is None or row["content"] != memory.content
                self._conn.execute(
                    "INSERT INTO memories(id, content, source, created_at, updated_at, project) "
                    "VALUES(?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "content=excluded.content, source=excluded.source, "
                    "updated_at=excluded.updated_at",
                    _mem_tuple(memory),
                )
                self._sync_aux_rows(
                    [memory],
                    force_embed_ids={memory.id} if stale_vec else set(),
                )

    def _any_doc_vectors(self) -> bool:
        """文档侧向量覆盖是否存在（结构性信号：memory_vecs 有任意行）。

        只在 vec 通道就绪（表已建）时调用；查询异常按"无向量"处理走
        keyword 降级（安全侧）。用 LIMIT 1 探测而非 COUNT(*)——vec0 虚表
        上聚合代价高且兼容性不如行扫描。
        """
        try:
            with _LOCK:
                row = self._conn.execute(
                    "SELECT memory_id FROM memory_vecs LIMIT 1"
                ).fetchone()
            return row is not None
        except sqlite3.Error:
            return False

    # ---------- MemoryStoreProtocol：检索 ----------

    def search(self, query: str, limit: int = 5, min_score: float = 0.0,
               project: str | None = None) -> list[Hit]:
        """纯向量检索：语义(重标定余弦) × 新近度衰减，相对门槛砍弱长尾。

        产品决策 2026-09：打分与排序只来自向量通道，FTS 关键词不再参与融合。
        - 语义通道带噪声地板（VEC_SIM_FLOOR）：地板以下视为无语义证据归零；
        - 分数过相对门槛（RELATIVE_KEEP_RATIO × 最高分），砍弱相关长尾；
        - 非空查询零证据 → 返回 []（诚实显示无命中）；
        - 仅空 query 走弱召回：score = WEAK_RECALL_PRIOR × 新近度；
        - 降级通道（依赖现实，非防御）：向量通道整体不可用（sqlite-vec 未
          加载 / vec 表缺失 / 无 embedder / 查询 embed 失败 / 库内一行向量
          都没有——keyword-only 库）时回落 FTS 关键词检索，detail.source=
          "keyword"，每实例只 warn 一次——否则未装 sqlite-vec 的环境记忆
          检索整体死亡；
        - 每条 Hit.detail 携带 {"source": "vector"|"keyword", 通道分量,
          recency}（弱召回为 {"weak": True}），供 CLI/Web 诊断面板分辨
          命中来源。FTS 索引照常写入，仅供降级通道读取。
        - content 完全相同的命中去重（保留 score 高/新的那条），重复记忆
          不再双条并列挤占 limit。
        """
        mems = self.get_all()
        if project is not None:
            mems = [m for m in mems if memory_visible_in(m.project, project)]
        if not mems:
            return []
        now = time.time()
        # P2-17：衰减带下限钳制（RECENCY_FLOOR），旧记忆不因纯指数衰减
        # 被相对门槛整体压出局
        recency = {
            m.id: max(
                RECENCY_FLOOR,
                RECENCY_DECAY_PER_HOUR
                ** (max(0.0, now - (m.updated_at or m.created_at)) / 3600.0),
            )
            for m in mems
        }
        s_vec = {m.id: 0.0 for m in mems}
        s_fts = {m.id: 0.0 for m in mems}

        query_empty = not query.strip()
        vec_ready = (
            self._vec_ready and self._has_vec_table and self._embedder is not None
        )
        qv: list[float] | None = None
        if vec_ready and not query_empty:
            try:
                # embed 是慢 IO（可能载模型/推理），留在模块锁外（P3-1）
                vec = self._embedder.embed([query])[0]
                # 零范数查询向量（词表外查询的退化输出）没有语义信号：
                # 下方余弦换算 cos=1-d²/2 假定 ‖q‖=1，‖q‖=0 时 d²=1 会给
                # 所有文档幻影 cos=0.5——地板 0.55 时代被无意归零，地板
                # 放宽到 0.5 以下后必须显式判零，按查询侧 embed 失败降级。
                if sum(x * x for x in vec) > 1e-12:
                    qv = vec
            except Exception:
                qv = None
        # 文档侧结构性降级：查询向量可用，但库内一行向量都没有
        # （memory_vecs 总行数==0——写入期无 embedder / embed 从未成功过
        # 的库，之后补挂 embedder 的场景）。判据是结构性信号（表空）而非
        # "本次 KNN 0 命中"：表里有行但都不相关（真·零相关）时必须诚实
        # 返回 []，不降级；表空时向量通道注定空转，回落 FTS 才能检索到
        # keyword-only 记忆（此前恒 [] ——"检索死亡"）。
        doc_side_no_vecs = False
        if qv is not None and not self._any_doc_vectors():
            doc_side_no_vecs = True
            qv = None
        # 向量通道不可用（查询侧 embed 失败 / 文档侧结构性缺向量）→ 降级
        # 关键词。无论静态缺失还是 embed 失败，每个实例只 warn 一次
        # （_kw_fallback_warned）。
        keyword_fallback = not query_empty and qv is None
        if keyword_fallback and not self._kw_fallback_warned:
            self._kw_fallback_warned = True
            if not vec_ready:
                logger.warning("向量通道不可用，检索降级为关键词（source=keyword）")
            elif doc_side_no_vecs:
                logger.warning("库内记忆均无向量（keyword-only 库），检索降级为关键词（source=keyword）")
            else:
                logger.warning("查询向量计算失败，检索降级为关键词（source=keyword）")

        if qv is not None:
            # vec0 约定：k=? 自带截断，不能再加 LIMIT
            with _LOCK:
                rows = self._conn.execute(
                    "SELECT memory_id, distance FROM memory_vecs "
                    "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (self._vec_serialize(qv), CHANNEL_K),
                ).fetchall()
            for r in rows:
                # 余弦换算：unit 向量 L2² = 2-2cos → cos = 1-d²/2
                #（旧写法 1-d/2 是错的换算，且未校准会人人 0.5 分起步）
                cos = 1.0 - float(r["distance"]) ** 2 / 2.0
                s_vec[r["memory_id"]] = max(
                    0.0, (cos - VEC_SIM_FLOOR) / (1.0 - VEC_SIM_FLOOR)
                )
        elif keyword_fallback:
            # 降级通道：FTS5 关键词（bm25 rank 越小越相关，秩归一到 (0,1]）。
            # 查询侧用 OR 语义：部分命中也进候选（与旧融合时代同口径）。
            terms = list(dict.fromkeys(_tokenize(query)))
            if terms:
                try:
                    with _LOCK:
                        rows = self._conn.execute(
                            "SELECT memory_id FROM mem_fts WHERE mem_fts MATCH ? "
                            "ORDER BY rank LIMIT ?",
                            (" OR ".join(terms), CHANNEL_K),
                        ).fetchall()
                    total = len(rows)
                    for i, r in enumerate(rows):
                        s_fts[r["memory_id"]] = 1.0 - i / (total + 1.0)
                except sqlite3.OperationalError:
                    pass  # 畸形查询串等：关键词通道本轮空转，不报错

        # 打分：主路径纯向量；降级时关键词按旧刻度（FTS_WEIGHT × 秩归一）。
        # 不再做线性融合——非降级路径 s_fts 恒为 0，不参与任何排序。
        if keyword_fallback:
            channel = {mid: FTS_WEIGHT * s_fts[mid] for mid in recency}
        else:
            channel = s_vec
        any_channel = any(v > 0.0 for v in channel.values())
        # 非空查询但零证据：诚实返回无命中。弱召回兜底只属于空 query——
        # 否则"随手发个 go"都会把全库按新近度塞回来（已发生过的线上现象）
        if not any_channel and query.strip():
            return []
        hits: list[Hit] = []
        for m in mems:
            if any_channel:
                score = channel[m.id] * recency[m.id]
                # 零通道证据（score=0）不作为召回结果——哪怕 min_score=0
                if score <= 0.0:
                    continue
                if keyword_fallback:
                    detail = {
                        "source": "keyword",
                        "fts": round(s_fts[m.id], 3),
                        "recency": round(recency[m.id], 3),
                    }
                else:
                    detail = {
                        "source": "vector",
                        "vec": round(s_vec[m.id], 3),
                        "recency": round(recency[m.id], 3),
                    }
            else:
                score = WEAK_RECALL_PRIOR * recency[m.id]
                detail = {"weak": True}
            if score >= min_score:
                hits.append(Hit(memory=m, score=score, detail=detail))
        # 排序：score 降序，并列时新近度高者胜（同分保留新的一条）
        hits.sort(
            key=lambda h: (h.score, h.memory.updated_at or h.memory.created_at or 0.0),
            reverse=True,
        )
        # 内容级去重：content 完全相同的命中（聚合残留 / 迁移双写 / 会话
        # 总结与 agent 记住撞内容）双条并列会挤占 limit——保留 score 高
        # （并列取新）的那条。dict 保插入序，去重后仍按分数降序。
        dedup: dict[str, Hit] = {}
        for h in hits:
            dedup.setdefault(h.memory.content, h)
        hits = list(dedup.values())
        # 相对门槛：只保留与最高分足够接近的命中（弱相关长尾并列砍掉）
        if hits:
            best = hits[0].score
            hits = [h for h in hits if h.score >= RELATIVE_KEEP_RATIO * best]
        return hits[:limit]

    def search_candidates(self, query: str, limit: int = 5,
                          project: str | None = None) -> list[Hit]:
        """候选检索（Decider 用），不过滤 min_score。"""
        return self.search(query, limit=limit, min_score=0.0, project=project)

    # ---------- MemoryStoreProtocol：读取 ----------

    def get_by_id(self, memory_id: str) -> Memory | None:
        with _LOCK:
            row = self._conn.execute(
                "SELECT id, content, source, created_at, updated_at, project "
                "FROM memories WHERE id=?",
                (memory_id,),
            ).fetchone()
            return _row_to_memory(row) if row is not None else None

    def get_all(self) -> list[Memory]:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT id, content, source, created_at, updated_at, project "
                "FROM memories ORDER BY rowid"
            ).fetchall()
            return [_row_to_memory(r) for r in rows]

    # ---------- MemoryStoreProtocol：删除 ----------

    def delete(self, memory_id: str) -> bool:
        with _LOCK:
            with self._transaction():
                cur = self._conn.execute(
                    "DELETE FROM memories WHERE id=?", (memory_id,)
                )
                self._conn.execute(
                    "DELETE FROM mem_fts WHERE memory_id=?", (memory_id,)
                )
                if self._has_vec_table:
                    self._conn.execute(
                        "DELETE FROM memory_vecs WHERE memory_id=?", (memory_id,)
                    )
            return cur.rowcount > 0

    def delete_all(self) -> bool:
        with _LOCK:
            with self._transaction():
                self._conn.execute("DELETE FROM memories")
                self._conn.execute("DELETE FROM mem_fts")
                if self._has_vec_table:
                    self._conn.execute("DELETE FROM memory_vecs")
            return True

    # ---------- MemoryStoreProtocol：全量替换 + 备份/恢复 ----------

    def replace_all(
        self,
        mems: list[Memory],
        backup: bool = True,
        protect_outside: set[str] | None = None,
        baseline: dict[str, float] | None = None,
    ) -> str | None:
        """单事务内全量替换。

        流程：old=现全量 → backup 时把 old 快照进 snapshots(kind='memory')
        → DELETE 全部 → INSERT 过滤后的 mems → 幸存行回插（OR IGNORE，
        幸存行撞 id 时以 mems 为准）。返回备份 label，未备份（backup=False
        或旧数据为空）返回 None。

        并发守卫（针对"快照→替换"窗口内的写入，二选一）：
        - baseline 非 None：行级版本守卫"活写入者优先"，规则见
          base.apply_baseline_guard；优先于 protect_outside。
        - 否则 protect_outside 非 None：仅回插 id 不在集合内的旧行
          （窗口内的新增），窗口内的 UPDATE/DELETE 不受保护（旧语义，
          consolidator 已迁移到 baseline）。
        - 都为 None：纯全量替换。
        """
        with _LOCK:
            old = self.get_all()
            keep_mems, survivors = apply_baseline_guard(mems, old, baseline)
            if baseline is None and protect_outside is not None:
                survivors = [m for m in old if m.id not in protect_outside]
            label: str | None = None
            final_mems = keep_mems + survivors
            # P3：全量重嵌在写事务外先算好（embed 是慢 IO，留在 BEGIN
            # IMMEDIATE 内会压住跨进程写者，worker 写入 database is locked
            # 被静默丢弃）；事务内只做落库，失败则该批 keyword-only 由回填收敛
            pre_vecs = self._embed_for_rebuild(final_mems)
            with self._transaction():
                if backup and old:
                    label = self._unique_label("backup")
                    self._conn.execute(
                        "INSERT INTO snapshots(kind, label, payload, created_at) "
                        "VALUES(?, ?, ?, ?)",
                        (
                            "memory",
                            label,
                            json.dumps([_mem_dict(m) for m in old], ensure_ascii=False),
                            time.time(),
                        ),
                    )
                self._conn.execute("DELETE FROM memories")
                if keep_mems:
                    self._conn.executemany(
                        "INSERT INTO memories(id, content, source, created_at, updated_at, project) "
                        "VALUES(?, ?, ?, ?, ?, ?)",
                        [_mem_tuple(m) for m in keep_mems],
                    )
                if survivors:
                    # INSERT OR IGNORE：幸存行撞上 mems 的 id 时以 mems 为准，
                    # 不让整个事务回滚
                    self._conn.executemany(
                        "INSERT OR IGNORE INTO memories"
                        "(id, content, source, created_at, updated_at, project) "
                        "VALUES(?, ?, ?, ?, ?, ?)",
                        [_mem_tuple(m) for m in survivors],
                    )
                # 辅助索引全量重建（clear_all + 事务外预嵌的 pre_vecs 落库）
                self._sync_aux_rows(final_mems, clear_all=True, pre_vecs=pre_vecs)
            # R3-15 增长治理：replace_all 成功后滚动保留最近 N 个 memory 快照
            # （backup 与 pre-restore 同 kind 一并滚动，防 snapshots 表无限增长）
            self._prune_memory_snapshots()
            self._prune_orphan_aux()
            return label

    def _prune_memory_snapshots(self) -> None:
        """snapshots(kind='memory') 只保留最近 MEMORY_SNAPSHOT_KEEP 个（调用方须持 _LOCK）。"""
        self._conn.execute(
            f"DELETE FROM snapshots WHERE kind='memory' AND id NOT IN ("
            f"SELECT id FROM snapshots WHERE kind='memory' "
            f"ORDER BY id DESC LIMIT {MEMORY_SNAPSHOT_KEEP})"
        )

    def list_backups(self) -> list[dict]:
        """全部聚合备份（kind='memory'），新→旧。

        返回 [{name, mtime}]，mtime 为快照 created_at 的整秒 Unix 时间戳
        （前端 /memory 恢复弹窗按 {name, mtime} 渲染）。
        """
        with _LOCK:
            rows = self._conn.execute(
                "SELECT label, created_at FROM snapshots WHERE kind='memory' "
                "ORDER BY id DESC"
            ).fetchall()
            return [{"name": r["label"], "mtime": int(r["created_at"])} for r in rows]

    def restore_backup(self, label: str) -> bool:
        """把指定备份恢复为当前全量。

        恢复前先把当前全量快照成 pre-restore-{ts}（防恢复后无法回退），
        再事务内替换为快照内容。未知 label / payload 损坏返回 False。
        """
        with _LOCK:
            row = self._conn.execute(
                "SELECT payload FROM snapshots WHERE kind='memory' AND label=? "
                "ORDER BY id DESC LIMIT 1",
                (label,),
            ).fetchone()
            if row is None:
                return False
            try:
                items = json.loads(row["payload"])
                mems = [
                    Memory(
                        id=d["id"],
                        user_id=LOCAL_USER,
                        content=d["content"],
                        source=d.get("source", "legacy"),
                        created_at=d.get("created_at", 0.0),
                        updated_at=d.get("updated_at"),
                        project=d.get("project", "") or "",
                    )
                    for d in items
                ]
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(f"备份 payload 解析失败(label={label}): {e}")
                return False
            current = self.get_all()
            # P3：与 replace_all 同理，全量重嵌挪到写事务外（见 _embed_for_rebuild）
            pre_vecs = self._embed_for_rebuild(mems)
            with self._transaction():
                if current:
                    pre_label = self._unique_label("pre-restore")
                    self._conn.execute(
                        "INSERT INTO snapshots(kind, label, payload, created_at) "
                        "VALUES(?, ?, ?, ?)",
                        (
                            "memory",
                            pre_label,
                            json.dumps([_mem_dict(m) for m in current], ensure_ascii=False),
                            time.time(),
                        ),
                    )
                self._conn.execute("DELETE FROM memories")
                if mems:
                    self._conn.executemany(
                        "INSERT INTO memories(id, content, source, created_at, updated_at, project) "
                        "VALUES(?, ?, ?, ?, ?, ?)",
                        [_mem_tuple(m) for m in mems],
                    )
                # 恢复后辅助索引全量重建（clear_all + 事务外预嵌的 pre_vecs 落库，
                # 与 memories 同事务，崩溃可整体回滚）
                self._sync_aux_rows(mems, clear_all=True, pre_vecs=pre_vecs)
                # 恢复本身也产生一个 kind='memory' 快照，与 backup 同一
                # 滚动窗口清点；不清理的话连续恢复会无限膨胀 snapshots 表
                self._prune_memory_snapshots()
            self._prune_orphan_aux()
            return True

    # ---------- EventLogProtocol ----------

    def append_event(self, scope: str, session_id: str, type_: str, payload: dict) -> int:
        """追加事件，返回自增 id。"""
        with _LOCK:
            cur = self._conn.execute(
                "INSERT INTO events(scope, session_id, type, payload, ts) "
                "VALUES(?, ?, ?, ?, ?)",
                (scope, session_id, type_, json.dumps(payload, ensure_ascii=False), time.time()),
            )
            return int(cur.lastrowid)

    def iter_events(
        self, scope: str, session_id: str | None = None, after_id: int = 0
    ) -> list[dict]:
        """按 scope（可选 session_id）读事件，id 升序，仅含 id > after_id。"""
        with _LOCK:
            sql = "SELECT id, scope, session_id, type, payload, ts FROM events WHERE scope=?"
            params: list[Any] = [scope]
            if session_id is not None:
                sql += " AND session_id=?"
                params.append(session_id)
            sql += " AND id>? ORDER BY id ASC"
            params.append(after_id)
            rows = self._conn.execute(sql, params).fetchall()
            return [
                {
                    "id": r["id"],
                    "scope": r["scope"],
                    "session_id": r["session_id"],
                    "type": r["type"],
                    "payload": json.loads(r["payload"]),
                    "ts": r["ts"],
                }
                for r in rows
            ]

    def last_event_id(self, scope: str, session_id: str | None = None) -> int:
        """该 scope（可选 session_id）下最大事件 id，无事件返回 0。"""
        with _LOCK:
            sql = "SELECT COALESCE(MAX(id), 0) AS max_id FROM events WHERE scope=?"
            params: list[Any] = [scope]
            if session_id is not None:
                sql += " AND session_id=?"
                params.append(session_id)
            row = self._conn.execute(sql, params).fetchone()
            return int(row["max_id"])

    def last_event_id_of_type(self, scope: str, session_id: str, type_: str) -> int:
        """该会话指定类型的最后一个事件 id（无则 0）。

        冷归档（P3，SessionLog.archive_compacted_events）用 O(log n) 定位
        最后一条 COMPACT_APPLIED，避免为判断"有没有可归档行"整流加载。
        """
        with _LOCK:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS max_id FROM events "
                "WHERE scope=? AND session_id=? AND type=?",
                (scope, session_id, type_),
            ).fetchone()
            return int(row["max_id"])

    def event_session_ids(self, scope: str) -> list[str]:
        """该 scope 下出现过事件的全部 session_id（DISTINCT，无序保证）。

        冷归档的 housekeeping 扫描入口（scope="chat" 按会话逐个检查）。
        """
        with _LOCK:
            rows = self._conn.execute(
                "SELECT DISTINCT session_id FROM events "
                "WHERE scope=? AND session_id<>''",
                (scope,),
            ).fetchall()
            return [r["session_id"] for r in rows]

    def delete_events_before(self, scope: str, session_id: str, before_id: int) -> int:
        """删除该会话 id < before_id 的事件行（冷归档后半程），返回删除行数。

        只在"导出冷文件并 flush/fsync 成功"之后调用（SessionLog 侧保证
        先冷后热次序）；id 全局自增单调，归档快照之后并发追加的行必然
        id >= before_id，不会被误删。
        """
        with _LOCK:
            cur = self._conn.execute(
                "DELETE FROM events WHERE scope=? AND session_id=? AND id<?",
                (scope, session_id, before_id),
            )
            self._conn.commit()
            return cur.rowcount

    def purge_events(self, scope: str, session_id: str) -> int:
        """删除某会话的全部事件（E1：会话删除时同步清理事件流），返回删除行数。"""
        with _LOCK:
            cur = self._conn.execute(
                "DELETE FROM events WHERE scope=? AND session_id=?",
                (scope, session_id),
            )
            self._conn.commit()
            return cur.rowcount

    # ---------- 维护（ADR-0004-D3 存储卫生，由 StorageHousekeeping 周期驱动） ----------

    def checkpoint_wal(self, mode: str = "TRUNCATE") -> None:
        """执行 WAL checkpoint。默认 TRUNCATE 把 -wal 文件截回零字节。

        busy（有活跃读者/跨进程写者）时静默放弃留待下个周期重试，
        绝不抛错影响调用方。
        """
        with _LOCK:
            try:
                self._conn.execute(f"PRAGMA wal_checkpoint({mode})")
            except sqlite3.Error:
                logger.warning(f"WAL checkpoint 失败（忽略，下周期重试）: {self.db_path}")

    def wal_size_bytes(self) -> int:
        """-wal 文件当前字节数（不存在返回 0）。"""
        p = Path(str(self.db_path) + "-wal")
        try:
            return p.stat().st_size
        except OSError:
            return 0

    def purge_resolved_interrupts(self, max_age_days: int = 7) -> int:
        """TTL 清理「已完结且陈旧」的中断事件对（scope="interrupt"）。

        判定（保守优先）：按 thread_id 取每个线程最后一个事件——只有最后
        一个是 interrupt/resolved 且其 ts 早于截止时间的线程才删除整个
        thread 的事件对（requested 快照里的 messages 占体积大头）。最后一
        个事件是 interrupt/requested 的线程无论多旧一律保留——pending 审批
        不能被 TTL 吃掉（重启恢复依赖它，见 SessionLog.pending_interrupts）。
        返回删除行数。事件类型字符串与 src/agent/session_log.py 常量对齐，
        此处不反向依赖 agent 模块。
        """
        cutoff = time.time() - max_age_days * 86400
        with _LOCK:
            # SQLite 特性：MAX(id) 聚合时裸列 type/ts 取自匹配该最大值的行
            rows = self._conn.execute(
                "SELECT session_id, type, ts, MAX(id) AS last_id "
                "FROM events WHERE scope='interrupt' GROUP BY session_id"
            ).fetchall()
            stale_ids = [
                r["session_id"]
                for r in rows
                if r["type"] == "interrupt/resolved" and r["ts"] < cutoff
            ]
            removed = 0
            for tid in stale_ids:
                cur = self._conn.execute(
                    "DELETE FROM events WHERE scope='interrupt' AND session_id=?",
                    (tid,),
                )
                removed += cur.rowcount
            if removed:
                logger.info(f"interrupt 事件 TTL 清理: 删除 {len(stale_ids)} 线程 {removed} 行")
            return removed

    # ---------- KVProtocol ----------

    def kv_get(self, scope: str, key: str, default: Any = None) -> Any:
        with _LOCK:
            row = self._conn.execute(
                "SELECT value FROM kv WHERE scope=? AND key=?", (scope, key)
            ).fetchone()
            if row is None:
                return default
            return json.loads(row["value"])

    def kv_put(self, scope: str, key: str, value: Any) -> None:
        with _LOCK:
            self._conn.execute(
                "INSERT INTO kv(scope, key, value, updated_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(scope, key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (scope, key, json.dumps(value, ensure_ascii=False), time.time()),
            )

    def kv_delete(self, scope: str, key: str) -> bool:
        with _LOCK:
            cur = self._conn.execute("DELETE FROM kv WHERE scope=? AND key=?", (scope, key))
            return cur.rowcount > 0

    def kv_list(self, scope: str) -> dict:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT key, value FROM kv WHERE scope=? ORDER BY key", (scope,)
            ).fetchall()
            return {r["key"]: json.loads(r["value"]) for r in rows}

    # ---------- 生命周期 ----------

    def close(self) -> None:
        """关闭连接（幂等：重复 close 由 sqlite3 自身容忍）。"""
        with _LOCK:
            try:
                self._conn.close()
            except sqlite3.Error:
                logger.exception(f"关闭 SQLite 连接失败: {self.db_path}")
