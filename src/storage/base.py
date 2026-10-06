"""
存储协议 —— 可插拔存储服务的接口契约（typing.Protocol）。

三协议覆盖 Hermes 的全部持久化需求，全部无 user 维度（单用户架构，
身份恒为 src.constants.LOCAL_USER，由实现方收口）：

  - MemoryStoreProtocol: 原子事实记忆（CRUD + 关键词检索 + 聚合替换/备份恢复）
  - EventLogProtocol:    append-only 事件日志（scope × session_id 隔离）
  - KVProtocol:          scope 隔离的 JSON 键值存储

实现方：SQLiteProvider（默认，三协议全实现）。旧文件适配器
FileMemoryProvider 已退役出生产包（scripts/legacy_memory_backend.py，
迁移脚本用）。

消费方一律走这些协议面，实现存储可插拔（Cordis 架构 T2a 存储接缝）。
"""
from __future__ import annotations

from typing import Any, Protocol

# 守卫函数 memory_version / apply_baseline_guard 的实现在 models（叶子层，
# 避免 storage ←→ memory 包初始化循环）；此处再导出维持旧导入路径可用。
from src.memory.models import (  # noqa: F401
    apply_baseline_guard,
    memory_version,
)
from src.memory.models import Hit, Memory


class MemoryStoreProtocol(Protocol):
    """原子事实记忆存取（单用户，无 user 参数）。"""

    def upsert(self, memory: Memory) -> None:
        """插入或更新（按 memory.id 覆盖）。更新语义保留库中原 created_at。"""
        ...

    def search(self, query: str, limit: int = 5, min_score: float = 0.0,
               project: str | None = None) -> list[Hit]:
        """关键词检索，score 降序 top-k；score < min_score 的条目过滤掉。
        project 非 None 时仅本项目 ∪ 全局（空 project）。"""
        ...

    def search_candidates(self, query: str, limit: int = 5,
                          project: str | None = None) -> list[Hit]:
        """候选检索（Decider 去重用），不过滤 min_score。"""
        ...

    def get_by_id(self, memory_id: str) -> Memory | None:
        """按 id 取单条，不存在返回 None。"""
        ...

    def get_all(self) -> list[Memory]:
        """全部记忆。"""
        ...

    def delete(self, memory_id: str) -> bool:
        """按 id 删单条，返回是否真的删到。"""
        ...

    def delete_all(self) -> bool:
        """清空全部记忆。"""
        ...

    def replace_all(
        self,
        mems: list[Memory],
        backup: bool = True,
        protect_outside: set[str] | None = None,
        baseline: dict[str, float] | None = None,
    ) -> str | None:
        """全量替换（聚合后回写用）。

        Args:
            mems: 替换后的完整集合。
            backup: 替换前是否备份旧全量。
            protect_outside: 非 None 时，旧数据中 id 不在该集合内的行
                视为并发写入，替换后保留（保守不丢）。baseline 非 None
                时本参数被忽略（baseline 的守卫是其超集）。
            baseline: 非 None 时的并发版本守卫：{id: 快照时版本号}，
                版本号取 updated_at（None 则 created_at），由调用方在
                快照时刻计算。行级规则（活写入者优先）：
                  - id 在基线中、替换时刻仍在且版本号变化 ⇒ 窗口内被
                    UPDATE ⇒ 当前活值胜出，丢弃 mems 中对应条目；
                  - id 在基线中、替换时刻已消失 ⇒ 窗口内被 DELETE ⇒
                    不从 mems 复活；
                  - id 不在基线中（窗口内新增）⇒ 一律保留。
                仅传 baseline 中不存在的键视为"无该行快照"。

        Returns:
            备份 label（未备份/旧数据为空时 None）。
        """
        ...

    def list_backups(self) -> list[dict]:
        """列出全部聚合备份，按时间倒序（新→旧）。

        每项形如 {"name": 备份名（restore_backup 的入参）,
                  "mtime": int Unix 秒}。
        """
        ...

    def restore_backup(self, label: str) -> bool:
        """把指定备份恢复为当前全量（恢复前当前内容也会先备份）。未知 label 返回 False。"""
        ...


class EventLogProtocol(Protocol):
    """append-only 事件日志，scope（子系统）× session_id（会话）二级隔离。"""

    def append_event(self, scope: str, session_id: str, type_: str, payload: dict) -> int:
        """追加一条事件，返回自增 id。"""
        ...

    def iter_events(
        self, scope: str, session_id: str | None = None, after_id: int = 0
    ) -> list[dict]:
        """读事件，id 升序。

        session_id 为 None 时取该 scope 下全部会话；仅返回 id > after_id 的行。
        每行形如 {"id","scope","session_id","type","payload"(已解析为 dict),"ts"}。
        """
        ...

    def last_event_id(self, scope: str, session_id: str | None = None) -> int:
        """该 scope（可选 session_id）下最大的事件 id，无事件返回 0。"""
        ...

    # ── 冷归档原语（P3 会话事件冷归档；SessionLog 经 getattr 探测，
    #    自定义 provider 缺省时归档自动禁用、合并读退化为纯热读）──

    def last_event_id_of_type(self, scope: str, session_id: str, type_: str) -> int:
        """该会话指定类型的最后一个事件 id（无则 0）。"""
        ...

    def event_session_ids(self, scope: str) -> list[str]:
        """该 scope 下出现过事件的全部 session_id。"""
        ...

    def delete_events_before(self, scope: str, session_id: str, before_id: int) -> int:
        """删除该会话 id < before_id 的事件行，返回删除行数。"""
        ...


class KVProtocol(Protocol):
    """scope 隔离的 JSON 键值存储（value 任意可 json 序列化对象）。"""

    def kv_get(self, scope: str, key: str, default: Any = None) -> Any:
        """取值，不存在返回 default。"""
        ...

    def kv_put(self, scope: str, key: str, value: Any) -> None:
        """写入（同 scope+key 覆盖）。"""
        ...

    def kv_delete(self, scope: str, key: str) -> bool:
        """删除，返回是否真的删到。"""
        ...

    def kv_list(self, scope: str) -> dict:
        """列出该 scope 全部键值（{key: value}）。"""
        ...
