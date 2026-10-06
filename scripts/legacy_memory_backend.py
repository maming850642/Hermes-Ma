"""
legacy_memory_backend —— 旧文件记忆后端（已从 src 生产包退役，仅迁移/排查用）。

2026-09（P3-5）从 src 退役：生产记忆后端是 SQLiteProvider
（data/hermes.db，sqlite-vec），文件后端（<root>/profile.md jsonl）不再
参与生产路径——MemoryManager 的 workspace_root 文件兜底分支已删除，本
模块仅供 scripts/migrate_to_sqlite.py（读旧 profile.md 灌库）与测试
（回归保护，防迁移工具链腐化）使用。

本模块自包含：
  - FileMemoryStore：纯文件记忆后端（profile.md，原子追加写/全量替换）
  - FileMemoryProvider：无 user 维度协议适配器（绑定单一 user_id）
  - read_legacy_profile：读旧 profile.md（jsonl）→ list[Memory]

对 src 的依赖仅叶子层（src.memory.models 的 Memory/Hit/守卫函数、
src.constants.LOCAL_USER）；FileMemoryStore 空 workspace_root 时回退
paths.agent_home() 的旧语义保留（延迟 import，仅构造时触发）。

运行方式（任一）：
  - 从项目根：`import scripts.legacy_memory_backend`（pyproject
    pythonpath=["."] 已覆盖；tests 即此方式）
  - 独立脚本：本文件 import 时自行把项目根插入 sys.path
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

# 项目根入 sys.path（独立运行场景；从项目根导入时为幂等操作）
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.constants import LOCAL_USER
from src.memory.models import Hit, Memory, apply_baseline_guard, memory_visible_in

logger = logging.getLogger("hermes.legacy_memory_backend")

# 文件级写锁：profile.md 的并发 upsert/delete 串行化。
# jsonl 追加本可行，但 delete/重写需读-改-写全量，加锁避免交错覆盖。
# 单文件（单用户）用一把全局锁即可——记忆写非高频路径，竞争可忽略。
_WRITE_LOCK = threading.Lock()


class FileMemoryStore:
    """纯文件记忆存储。单用户：root 下单一 profile.md（jsonl）。"""

    def __init__(self, workspace_root: str = ""):
        """
        Args:
            workspace_root: 根目录（语义为 agent home 根）。记忆落在
                `<root>/profile.md`。为空时回退 paths.agent_home()。
        """
        if workspace_root:
            self._root = Path(workspace_root)
        else:
            from src.storage import paths
            self._root = paths.agent_home()

    # ---------- 路径 ----------

    def _profile_path(self, user_id: str = "") -> Path:
        """返回 profile.md 路径（父目录自动创建）。user_id 已废弃，仅保参。"""
        p = self._root / "profile.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _read_all(self, user_id: str) -> list[Memory]:
        """读取某用户全部记忆（jsonl 解析）。文件不存在返回 []。"""
        p = self._profile_path(user_id)
        if not p.exists():
            return []
        mems: list[Memory] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                mems.append(Memory(
                    id=d["id"],
                    user_id=d["user_id"],
                    content=d["content"],
                    source=d.get("source", "legacy"),
                    created_at=d.get("created_at", 0.0),
                    updated_at=d.get("updated_at"),
                    project=d.get("project", "") or "",
                ))
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"profile.md 行解析失败(跳过): {line[:80]}: {e}")
                continue
        return mems

    def _write_all(self, user_id: str, mems: list[Memory]) -> None:
        """全量重写 profile.md（原子：临时文件 + os.replace）。"""
        p = self._profile_path(user_id)
        lines = [
            json.dumps({
                "id": m.id, "user_id": m.user_id, "content": m.content,
                "source": m.source, "created_at": m.created_at,
                "updated_at": m.updated_at,
                "project": getattr(m, "project", "") or "",
            }, ensure_ascii=False)
            for m in mems
        ]
        text = "\n".join(lines) + ("\n" if lines else "")
        tmp = p.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)

    # ---------- 写入 ----------

    def upsert(self, memory: Memory) -> None:
        """插入或更新（按 memory.id 覆盖）。"""
        with _WRITE_LOCK:
            mems = self._read_all(memory.user_id)
            # 同 id 替换；否则追加
            replaced = False
            for i, m in enumerate(mems):
                if m.id == memory.id:
                    # 保留原 created_at（更新场景）
                    if memory.created_at == m.created_at or memory.updated_at is None:
                        pass
                    mems[i] = memory
                    replaced = True
                    break
            if not replaced:
                mems.append(memory)
            self._write_all(memory.user_id, mems)

    # ---------- 检索 ----------

    def search(self, user_id: str, query: str, limit: int = 5, min_score: float = 0.0,
               project: str | None = None) -> list[Hit]:
        """关键词匹配检索。返回按 score 降序的 Hit 列表（top-k）。

        score = 0.5 + 0.5 * (命中查询词数 / 查询词总数)。
        基准 0.5 保证任意命中的记忆都过 min_score=0.4(向量时代的阈值);
        命中越多分越高(全命中=1.0)。无语义召回——M0 有意退化。
        """
        mems = self._read_all(user_id)
        if project is not None:
            mems = [m for m in mems if memory_visible_in(m.project, project)]
        if not mems:
            return []
        query_terms = _tokenize(query)
        hits: list[Hit] = []
        for m in mems:
            if not query_terms:
                score = 0.5
            else:
                content_lower = m.content.lower()
                matched = sum(1 for t in query_terms if t.lower() in content_lower)
                if matched == 0:
                    continue
                score = 0.5 + 0.5 * (matched / len(query_terms))
            if score >= min_score:
                hits.append(Hit(memory=m, score=score))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    def search_candidates(self, user_id: str, query: str, limit: int = 5,
                          project: str | None = None) -> list[Hit]:
        """给 Decider 用的候选检索（不过滤 min_score）。与 search 等价。"""
        return self.search(user_id, query, limit=limit, min_score=0.0, project=project)

    # ---------- 读取 ----------

    def get_by_id(self, memory_id: str) -> Memory | None:
        """按 id 取单条。单用户拍平后直接读唯一 profile.md 线性找。
        Decider UPDATE 路径用。"""
        for m in self._read_all(""):
            if m.id == memory_id:
                return m
        return None

    def get_all(self, user_id: str) -> list[Memory]:
        """某用户全部记忆。"""
        return self._read_all(user_id)

    # ---------- 删除 ----------

    def delete(self, memory_id: str) -> bool:
        """按 id 删除单条。返回是否成功（不谎报成功）。"""
        with _WRITE_LOCK:
            target = self.get_by_id(memory_id)
            if target is None:
                return False
            mems = self._read_all(target.user_id)
            new_mems = [m for m in mems if m.id != memory_id]
            if len(new_mems) == len(mems):
                return False  # 未删到
            self._write_all(target.user_id, new_mems)
            return True

    def delete_all_by_user(self, user_id: str) -> bool:
        """清空某用户全部记忆。"""
        with _WRITE_LOCK:
            p = self._profile_path(user_id)
            if not p.exists():
                return True  # 本来就没有，视为成功
            self._write_all(user_id, [])
            return True

    # ---------- 聚合：全量替换 + 备份/恢复 ----------

    def replace_all(self, user_id: str, mems: list[Memory], backup: bool = True) -> str | None:
        """全量替换某用户的记忆（聚合后回写用）。

        流程（全程序在 _WRITE_LOCK 内，与 upsert/delete 串行）：
          1. 若 backup 且当前 profile.md 非空 → 复制为 profile.md.bak.{ts}（备份，不覆盖已有同名）
          2. _write_all 整体替换（tmp + os.replace 原子）

        Args:
            backup: 是否在替换前备份旧 profile.md。默认 True。

        Returns:
            备份文件名（相对文件名，如 "profile.md.bak.1784079811"），未备份时 None。
        """
        with _WRITE_LOCK:
            p = self._profile_path(user_id)
            backup_name: str | None = None
            if backup and p.exists():
                old_text = p.read_text(encoding="utf-8").strip()
                if old_text:
                    backup_name = self._unique_backup_name(p.parent)
                    shutil.copy2(p, p.parent / backup_name)
            self._write_all(user_id, mems)
            return backup_name

    @staticmethod
    def _unique_backup_name(parent: Path) -> str:
        """生成不与现有备份冲突的备份文件名。

        形如 profile.md.bak.{ts}；若同秒内已存在（快速连续备份），追加 -2/-3 后缀。
        """
        base = f"profile.md.bak.{int(time.time())}"
        candidate = base
        suffix = 2
        while (parent / candidate).exists():
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def list_backups(self, user_id: str = "") -> list[dict]:
        """列出全部聚合备份。

        扫描 profile.md 同目录下 profile.md.bak.* 文件，按修改时间倒序返回。
        每项 {name, mtime, size}。无备份返回 []。user_id 已废弃，仅保参。
        """
        p = self._profile_path(user_id)
        backs: list[dict] = []
        if not p.parent.exists():
            return backs
        for f in p.parent.iterdir():
            # 仅匹配 profile.md.bak.<数字>（聚合备份命名），避免误抓其他 .bak
            name = f.name
            if not name.startswith("profile.md.bak."):
                continue
            if not f.is_file():
                continue
            try:
                backs.append({
                    "name": name,
                    "mtime": int(f.stat().st_mtime),
                    "size": f.stat().st_size,
                })
            except OSError:
                continue
        backs.sort(key=lambda b: b["mtime"], reverse=True)
        return backs

    def restore_backup(self, user_id: str, backup_name: str) -> bool:
        """把指定备份恢复为 profile.md（覆盖当前）。

        恢复前会把当前 profile.md 也备份一份（避免恢复后后悔无法回退）。
        返回是否成功（备份不存在返回 False）。
        """
        # 防路径穿越：backup_name 必须是纯文件名
        if os.path.basename(backup_name) != backup_name:
            return False
        if not backup_name.startswith("profile.md.bak."):
            return False
        with _WRITE_LOCK:
            p = self._profile_path(user_id)
            bak = p.parent / backup_name
            if not bak.exists():
                return False
            # 当前 profile.md 先备份（若非空），作为"恢复前"快照
            if p.exists() and p.read_text(encoding="utf-8").strip():
                cur_bak_name = self._unique_backup_name(p.parent)
                shutil.copy2(p, p.parent / cur_bak_name)
            # 备份内容 → profile.md（原子替换）
            tmp = p.with_suffix(".md.tmp")
            tmp.write_text(bak.read_text(encoding="utf-8"), encoding="utf-8")
            os.replace(tmp, p)
            return True


class FileMemoryProvider:
    """MemoryStoreProtocol 适配器：转发到 FileMemoryStore（绑定单一 user）。"""

    def __init__(self, inner: FileMemoryStore, user_id: str = LOCAL_USER) -> None:
        """
        Args:
            inner: 被包装的文件记忆后端。
            user_id: 绑定的用户身份（转发到 inner 的所有带 user_id 方法）。
        """
        self._inner = inner
        self._user_id = user_id

    def _own(self, memory: Memory) -> Memory:
        """把 memory 的 user_id 归一为绑定值（不改调用方对象）。"""
        return replace(memory, user_id=self._user_id)

    # ---------- 写入 ----------

    def upsert(self, memory: Memory) -> None:
        self._inner.upsert(self._own(memory))

    # ---------- 检索 ----------

    def search(self, query: str, limit: int = 5, min_score: float = 0.0,
               project: str | None = None) -> list[Hit]:
        return self._inner.search(self._user_id, query, limit=limit,
                                  min_score=min_score, project=project)

    def search_candidates(self, query: str, limit: int = 5,
                          project: str | None = None) -> list[Hit]:
        return self._inner.search_candidates(self._user_id, query, limit=limit,
                                             project=project)

    # ---------- 读取 ----------

    def get_by_id(self, memory_id: str) -> Memory | None:
        # inner.get_by_id 本身无 user 参数（扫全用户，id 全局唯一）
        return self._inner.get_by_id(memory_id)

    def get_all(self) -> list[Memory]:
        return self._inner.get_all(self._user_id)

    # ---------- 删除 ----------

    def delete(self, memory_id: str) -> bool:
        return self._inner.delete(memory_id)

    def delete_all(self) -> bool:
        return self._inner.delete_all_by_user(self._user_id)

    # ---------- 全量替换 + 备份/恢复 ----------

    def replace_all(
        self,
        mems: list[Memory],
        backup: bool = True,
        protect_outside: set[str] | None = None,
        baseline: dict[str, float] | None = None,
    ) -> str | None:
        """转发到 inner.replace_all，并发守卫在适配层实现。

        baseline 行级"活写入者优先"规则与 SQLiteProvider 完全一致
        （共享 base.apply_baseline_guard）；protect_outside 仅保留 id
        不在集合内的行，baseline 非 None 时被忽略。守卫并入发生在
        inner 落盘锁外——与 protect_outside 时代同样的等价性边界：
        只可能多保留、不会丢。
        """
        final = [self._own(m) for m in mems]
        if baseline is not None:
            kept, survivors = apply_baseline_guard(
                final, self._inner.get_all(self._user_id), baseline
            )
            final_ids = {m.id for m in kept}
            final = kept + [self._own(s) for s in survivors if s.id not in final_ids]
        elif protect_outside is not None:
            final_ids = {m.id for m in final}
            survivors = [
                m for m in self._inner.get_all(self._user_id)
                if m.id not in protect_outside and m.id not in final_ids
            ]
            if survivors:
                final.extend(survivors)
        return self._inner.replace_all(self._user_id, final, backup=backup)

    def list_backups(self) -> list[dict]:
        """协议契约：[{name, mtime}]。inner 本就返回该形状，直接透传。"""
        return self._inner.list_backups(self._user_id)

    def restore_backup(self, label: str) -> bool:
        return self._inner.restore_backup(self._user_id, label)


def read_legacy_profile(path: Path) -> list[Memory]:
    """读旧 profile.md（每行一条 JSON）→ list[Memory]。

    跳过空行/坏行（json 解析失败或缺必需键，告警不中断）；文件不存在
    返回 []。迁移脚本用它把旧文件记忆灌入 SQLiteProvider。
    """
    if not path.exists():
        return []
    mems: list[Memory] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            mems.append(
                Memory(
                    id=d["id"],
                    user_id=d.get("user_id", LOCAL_USER),
                    content=d["content"],
                    source=d.get("source", "legacy"),
                    created_at=d.get("created_at", 0.0),
                    updated_at=d.get("updated_at"),
                )
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"legacy profile 行解析失败(跳过): {line[:80]}: {e}")
            continue
    return mems


# ============================================
# 分词（中英文混合，简单策略）
# ============================================

# 英文/数字词
_EN_WORD = re.compile(r"[A-Za-z0-9_]+")
# 中文字符（CJK 统一汉字范围）
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _tokenize(text: str) -> list[str]:
    """简单分词：英文按词、中文按字（无分词库依赖）。

    2026-07-03: 中文改为按字切分（之前把整段中文当一个 term，导致
    "用户叫张三" vs "姓名是张三" 这类近义改写完全不命中，去重候选恒空，
    Decider 短路 ADD，记忆无限重复）。按字后召回率显著提升，精度由
    Decider LLM 兜底。英文仍按整体词。
    "用户用 Python" → ["用", "户", "用", "Python"]
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
