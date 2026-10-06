"""
SessionStateStore —— 会话状态（todos / virtual_fs / waker）的 kv 权威层（P3）。

## 背景

会话状态三字段此前只活在 JSON 快照（src/session_store.py）里：worker 每轮末
`_save_bucket` → save_session 整文件覆盖写，JSON 兼具"权威写者 + 唯一读源"
双重身份。P3 双轨收口把权威身份迁到 kv：

- kv 布局：scope="session_state"，key=session_id，
  value = {"todos": [...], "virtual_fs": {...}, "waker": "...", "schema_version": 1}
- 写侧：save_session / ensure_session_stub 与 JSON 快照同步双写（kv 先行；
  kv 失败仅告警不阻断——JSON 缓存仍在，读侧回退 + 回填自愈）
- 读侧：load_session 的状态字段 kv 优先；kv 缺失（旧会话/写失败）回退
  JSON 既有值并回填 kv（读一次旧会话即升级，迁移自愈）
- JSON 降级为缓存：继续写（列表预览等既有消费方不破坏），语义上可丢——
  删掉 JSON 文件后状态仍可从 kv 完整恢复

## 与 run_registry / projects_store 的关系

复用同一套 kv 原语（KVProtocol：kv_get/kv_put/kv_delete/kv_list，
SQLiteProvider 实现），不新增存储原语。默认连接沿用 projects_store 的
"进程级懒构造缓存"模式，但按**解析后的库路径**做缓存键：测试经
paths.set_data_root(tmp) 改道数据根后，首次 kv 操作自动按新路径重建连接
（旧连接关闭），无需调用方感知。

## 并发约定

与 events 写同一套约束：所有操作经 SQLiteProvider 的模块级 RLock 串行
（同进程多实例互斥），跨进程靠 WAL + busy_timeout 兜底——与 SessionLog
事件追加、RunRegistry 写穿同一约定。
"""
from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger("hermes.storage.session_state")

# 所有会话状态统一的 kv scope
SCOPE = "session_state"

# value 结构版本（未来字段演进用；读侧不校验版本，只按字段取值）
SCHEMA_VERSION = 1

# 进程级默认连接缓存（模式同 projects_store._default_provider，但键为库路径）
_provider: Any = None
_provider_path: str | None = None
_provider_lock = threading.Lock()


def default_provider():
    """默认库（data_dir("hermes.db")）的进程级缓存连接，按路径键惰性重建。

    paths.set_data_root 改道后（测试/迁移脚本），解析路径变化会在下次调用
    时自动重建连接并关闭旧连接——旧连接的关闭经 SQLiteProvider 的模块锁，
    与在途操作天然互斥。构造/关闭失败按异常上抛（调用方各有降级策略）。
    """
    global _provider, _provider_path
    from src.storage import paths

    path = str(paths.data_dir("hermes.db"))
    with _provider_lock:
        if _provider is None or _provider_path != path:
            from src.storage.sqlite_provider import SQLiteProvider

            old, _provider = _provider, SQLiteProvider()
            _provider_path = path
            if old is not None:
                try:
                    old.close()
                except Exception:
                    logger.debug("旧默认连接关闭失败（忽略）", exc_info=True)
        return _provider


def reset_default_provider() -> None:
    """清空默认连接缓存并关闭现连接（测试隔离用，conftest 调用）。"""
    global _provider, _provider_path
    with _provider_lock:
        old, _provider, _provider_path = _provider, None, None
    if old is not None:
        try:
            old.close()
        except Exception:
            logger.debug("默认连接关闭失败（忽略）", exc_info=True)


# ============================================
# value 规范化 / 校验
# ============================================
def _normalize(todos: list | None, virtual_fs: dict | None, waker: str | None) -> dict:
    """状态三字段 → kv value（默认值口径与 JSON 快照 _compose_session_data 一致）。"""
    return {
        "todos": list(todos or []),
        "virtual_fs": dict(virtual_fs or {}),
        "waker": str(waker or ""),
        "schema_version": SCHEMA_VERSION,
    }


def _parse(raw: Any) -> tuple[list, dict, str] | None:
    """kv 原始值 → (todos, vfs, waker)；缺失/形状非法返回 None（读侧回退 JSON）。"""
    if not isinstance(raw, dict):
        return None
    todos = raw.get("todos")
    vfs = raw.get("virtual_fs")
    waker = raw.get("waker")
    if not isinstance(todos, list) or not isinstance(vfs, dict) or not isinstance(waker, str):
        return None
    return todos, vfs, waker


# ============================================
# 读写（provider=None 时走默认库缓存连接）
# ============================================
def save_state(session_id: str, todos: list | None, virtual_fs: dict | None,
               waker: str | None, provider=None) -> None:
    """写入/覆盖该会话的状态行（save_session / ensure_session_stub 的 kv 侧）。"""
    (provider or default_provider()).kv_put(
        SCOPE, session_id, _normalize(todos, virtual_fs, waker))


def load_state(session_id: str, provider=None) -> tuple[list, dict, str] | None:
    """读该会话状态行 → (todos, vfs, waker)。

    行不存在或形状非法（旧版本/损坏）返回 None——调用方回退 JSON 快照并
    回填（形状非法时回填同时完成修复）。
    """
    raw = (provider or default_provider()).kv_get(SCOPE, session_id)
    return _parse(raw)


def copy_state(src_session_id: str, dst_session_id: str, provider=None) -> bool:
    """把源会话的状态行复制到目标会话（fork 语义对齐）。源无行返回 False。

    fork 场景与 JSON stub 的字段透传互为兜底：源是旧会话（只有 JSON 无 kv）
    时本函数 no-op，stub 落盘时会把透传值写进 kv（同一份状态，两条路都收敛）。
    """
    state = load_state(src_session_id, provider)
    if state is None:
        return False
    todos, vfs, waker = state
    save_state(dst_session_id, todos, vfs, waker, provider)
    return True


def delete_state(session_id: str, provider=None) -> bool:
    """删除该会话的状态行（会话删除联动清理），返回是否真的删到。"""
    return bool((provider or default_provider()).kv_delete(SCOPE, session_id))
