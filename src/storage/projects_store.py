"""
ProjectStore —— 项目空间实体（ADR-0005 D1）。

表 projects(slug PK, name, path, type, created_at, last_opened_at)：
- type ∈ hosted（托管于 data/projects/spaces/<slug>）| mounted（引用本地
  绝对路径）| inbox（内建「直接开聊」，无文件根）
- slug 是稳定标识（进会话快照的 project 字段），限 [a-z0-9][a-z0-9._-]{0,63}
  ——web 层 security.validate_id 封冒号/斜杠/前导点，复合 ID 无路可走，
  项目维度只能在独立命名域里表达
- name 自由文本（中文允许）；非 ASCII 名自动生成 p-<6hex> slug

激活指针存 kv(scope="projects", key="active")。激活语义＝叠在现有单挂载
槽上复用 WorkspaceService 流水线（见 routers/projects.py），本模块只记账。

运行时归属读取入口：get_active_project()——worker 每轮落盘时把当前激活
slug 随快照 stamp 进 project 字段（一次性绑定，存在即不再改写，
见 session_store._compose_session_data）。
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from typing import Any

logger = logging.getLogger("hermes.storage.projects")

INBOX_SLUG = "inbox"
TYPE_HOSTED = "hosted"
TYPE_MOUNTED = "mounted"
TYPE_INBOX = "inbox"
_PROJECT_TYPES = (TYPE_HOSTED, TYPE_MOUNTED, TYPE_INBOX)

_KV_SCOPE = "projects"
_KEY_ACTIVE = "active"

# slug 白名单：小写字母/数字开头，后续允许 . _ -；总长 ≤64。
# 与 web 层 validate_id 的禁用字符集完全错开（无斜杠/冒号/空格）。
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
#: slug 去重后缀上限（base, base-2 ... base-99）
_MAX_DEDUPE_SUFFIX = 99


class ProjectError(ValueError):
    """项目实体校验失败。消息中文、可直接展示给 UI（ValueError→400）。"""


def validate_slug(slug: str) -> str:
    """校验并返回规范 slug；非法抛 ProjectError。"""
    s = (slug or "").strip()
    if not _SLUG_RE.match(s):
        raise ProjectError(
            f"非法项目标识: {slug!r}（仅允许小写字母/数字开头，"
            f"字符集 a-z 0-9 . _ -，长度 ≤64）")
    return s


def default_slug_for(name: str) -> str:
    """由显示名推导默认 slug。

    ASCII 可映射 → 规范化（非白名单字符折叠为 '-'、去首尾 '-.'）；
    产出为空或原名含非 ASCII（中文名的信息无法无损进入 slug 域，
    ADR-0005 风险 4：宁可生成无语义短 id，也不做丢失语义的截断转写）
    → p-<6hex> 随机段。
    """
    raw = (name or "").strip()
    if raw and all(ord(ch) < 128 for ch in raw):
        folded = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw.lower()).strip("-.")
        if _SLUG_RE.match(folded):
            return folded
    return f"p-{uuid.uuid4().hex[:6]}"


class ProjectStore:
    """projects 表 + 激活指针的读写门面。provider = SQLiteProvider 实例
    （query/execute/kv_get/kv_put 已是公开入口）。"""

    def __init__(self, provider) -> None:
        self._db = provider

    # ── 基础 CRUD ──

    def ensure_default(self) -> None:
        """内建收件箱（幂等）。免项目直接聊的合法承载者，不可删除。"""
        if self.get(INBOX_SLUG) is None:
            now = time.time()
            self._db.execute(
                "INSERT INTO projects(slug, name, path, type, created_at, last_opened_at) "
                "VALUES(?, ?, '', ?, ?, ?)",
                (INBOX_SLUG, "收件箱", TYPE_INBOX, now, now),
            )

    def get(self, slug: str) -> dict | None:
        rows = self._db.query(
            "SELECT slug, name, path, type, created_at, last_opened_at "
            "FROM projects WHERE slug=?", (validate_slug(slug),))
        return dict(rows[0]) if rows else None

    def create(self, name: str, type_: str = TYPE_HOSTED, path: str = "",
               slug: str | None = None) -> dict:
        """创建项目。slug 缺省按 name 推导 + 冲突追加 -2..-99。"""
        name = (name or "").strip()
        if not name:
            raise ProjectError("项目名称不能为空")
        if type_ not in _PROJECT_TYPES or type_ == TYPE_INBOX:
            raise ProjectError(f"不支持的项目类型: {type_!r}")
        if slug is not None:
            clean = validate_slug(slug)
            if self.get(clean) is not None:
                raise ProjectError(f"项目标识已存在: {clean}")
        else:
            clean = self.unique_slug(default_slug_for(name))
        if type_ == TYPE_HOSTED and not path:
            from src.storage import paths
            path = str(paths.data_dir("projects", "spaces", clean))
        now = time.time()
        try:
            self._db.execute(
                "INSERT INTO projects(slug, name, path, type, created_at, last_opened_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (clean, name, path or "", type_, now, now),
            )
        except Exception as e:
            raise ProjectError(f"创建项目失败: {e}") from e
        logger.info(f"创建项目: {clean} (type={type_}, path={path!r})")
        return self.get(clean)

    def unique_slug(self, base: str) -> str:
        """base 不冲突即用；否则追加 -2..-99，耗尽抛错。"""
        candidate = validate_slug(base)
        for suffix in range(1, _MAX_DEDUPE_SUFFIX):
            test = candidate if suffix == 1 else f"{candidate}-{suffix}"
            if self.get(test) is None:
                return test
        raise ProjectError(f"项目标识空间耗尽: {candidate}")

    def list(self) -> list[dict]:
        """全部项目，最近打开在前。"""
        rows = self._db.query(
            "SELECT slug, name, path, type, created_at, last_opened_at "
            "FROM projects ORDER BY last_opened_at DESC")
        return [dict(r) for r in rows]

    def touch(self, slug: str) -> None:
        self._db.execute(
            "UPDATE projects SET last_opened_at=? WHERE slug=?",
            (time.time(), validate_slug(slug)))

    def delete(self, slug: str) -> bool:
        """删除项目（内建收件箱拒绝）。若删的是当前激活项，清空激活指针。"""
        clean = validate_slug(slug)
        if clean == INBOX_SLUG:
            raise ProjectError("内建收件箱不能删除")
        removed = self._db.execute("DELETE FROM projects WHERE slug=?", (clean,))
        if removed and self.active_slug() == clean:
            self.clear_active()
        return bool(removed)

    # ── 激活指针 ──

    def set_active(self, slug: str) -> None:
        self._db.kv_put(_KV_SCOPE, _KEY_ACTIVE, validate_slug(slug))

    def clear_active(self) -> None:
        try:
            self._db.kv_delete(_KV_SCOPE, _KEY_ACTIVE)
        except Exception:
            pass

    def active_slug(self) -> str:
        val = self._db.kv_get(_KV_SCOPE, _KEY_ACTIVE)
        return val if isinstance(val, str) else ""


#: get_active_project 的进程级默认连接缓存（懒构造；按解析后的库路径键控——
#: set_data_root 切换数据根后自动重建，绝不拿指向上一个根的旧连接。P3 与
#: session_state_store.default_provider 同款模式；手工注入式用法（测试
#: monkeypatch provider）请同步注入 _default_provider_path）
_default_provider: Any = None
_default_provider_path: str | None = None
_default_lock = threading.Lock()


def _get_default_provider():
    """默认库连接（路径键控缓存）：provider 未建或数据根已切换时重建。"""
    global _default_provider, _default_provider_path
    from src.storage import paths

    path = str(paths.data_dir("hermes.db"))
    with _default_lock:
        if _default_provider is None or _default_provider_path != path:
            from src.storage.sqlite_provider import SQLiteProvider

            old, _default_provider = _default_provider, SQLiteProvider()
            _default_provider_path = path
            if old is not None:
                try:
                    old.close()
                except Exception:
                    logger.debug("旧默认连接关闭失败（忽略）", exc_info=True)
    return _default_provider


def get_active_project(provider=None) -> str:
    """轻量读取当前激活 slug（worker 每轮落盘 stamp 会话归属用）。

    provider 为 None 时用进程级缓存的默认库连接（路径键控，见
    _get_default_provider），避免每次 save 都重开 SQLite 连接跑建表脚本。
    任何失败都返回 INBOX_SLUG（等价无项目归属语义），绝不阻断保存主路径。
    """
    try:
        if provider is None:
            provider = _get_default_provider()
        store = ProjectStore(provider)
        slug = store.active_slug()
        return slug or INBOX_SLUG
    except Exception:
        logger.warning("读取激活项目失败，按 inbox 归属处理", exc_info=True)
        return INBOX_SLUG
