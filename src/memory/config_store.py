"""
MemoryConfigStore —— 聚合配置存储（T2b-② 入 kv）。

配置存 SQLiteProvider kv（scope="memory", key="config"，库 data/hermes.db），
不再落 users/<uid>/memory_config.yaml。字段：
  auto_consolidate: false   # 是否后台自动聚合（默认关）
  interval_hours: 24        # 自动聚合间隔（小时）
  threshold: 20             # 活跃记忆少于此数不触发（省 LLM）
  last_run_at: ""           # 上次自动聚合时间（ISO），调度器回写

模块级 API 面（load/save/touch_last_run）签名不变，workspace_root/user_id
参数保留但被忽略（单用户架构，调用方零改动）。

## 旧 yaml 兼容（一次性兜底迁移）
若 kv 尚无值且 agent_home（data/home）下存在旧 memory_config.yaml，
首次 load 自动把 yaml 内容灌入 kv（不删源文件；正式迁移由
scripts/migrate_to_sqlite.py 负责，它会改名 .migrated）。
"""
import logging
from datetime import datetime
from pathlib import Path

import yaml

from src.storage import paths
from src.storage.sqlite_provider import SQLiteProvider

logger = logging.getLogger("hermes.memory.config_store")

# kv 坐标
_SCOPE = "memory"
_KEY = "config"

# 默认配置（与上方文档一致）
DEFAULTS = {
    "auto_consolidate": False,
    "interval_hours": 24,
    "threshold": 20,
    "last_run_at": "",
}

# 模块级 provider 缓存（keyed by db 路径）：避免每次 load/save 都重开连接；
# set_data_root 换根后路径变化 → 自动重建（测试隔离依赖这点）。
_provider: SQLiteProvider | None = None
_provider_path: Path | None = None


def _store() -> SQLiteProvider:
    global _provider, _provider_path
    p = paths.data_dir("hermes.db")
    if _provider is None or _provider_path != p:
        _provider = SQLiteProvider()
        _provider_path = p
    return _provider


def _legacy_yaml_path() -> Path:
    """旧 yaml 在 agent_home 下的兜底位置（目录迁移后、入 kv 前）。"""
    return paths.agent_home("memory_config.yaml")


def _parse_legacy_yaml(path: Path) -> dict | None:
    """解析旧 memory_config.yaml → 只保留 DEFAULTS 内的键。失败返回 None。"""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning(f"读取旧 memory_config.yaml 失败（忽略）: {path}: {e}")
        return None
    if not isinstance(data, dict):
        return None
    return {k: v for k, v in data.items() if k in DEFAULTS}


def _normalize(cfg: dict) -> dict:
    """合并 DEFAULTS + 类型纠偏（kv/yaml 解出来可能是任意 JSON 类型）。"""
    out = dict(DEFAULTS)
    out["auto_consolidate"] = bool(cfg.get("auto_consolidate", DEFAULTS["auto_consolidate"]))
    try:
        out["interval_hours"] = int(cfg.get("interval_hours", DEFAULTS["interval_hours"]))
    except (TypeError, ValueError):
        out["interval_hours"] = DEFAULTS["interval_hours"]
    try:
        out["threshold"] = int(cfg.get("threshold", DEFAULTS["threshold"]))
    except (TypeError, ValueError):
        out["threshold"] = DEFAULTS["threshold"]
    out["last_run_at"] = str(cfg.get("last_run_at", "") or "")
    return out


def load(workspace_root: str = "", user_id: str = "") -> dict:
    """读取聚合配置。kv 无值时兜底导入旧 yaml；始终返回全字段 dict。

    workspace_root/user_id 保参但被忽略（单用户）。
    """
    raw = _store().kv_get(_SCOPE, _KEY)
    if raw is None:
        legacy = _legacy_yaml_path()
        if legacy.exists():
            imported = _parse_legacy_yaml(legacy)
            if imported is not None:
                logger.info(f"memory_config 旧 yaml 兜底迁入 kv: {legacy}")
                raw = _normalize(imported)
                _store().kv_put(_SCOPE, _KEY, raw)  # 一次性迁入，后续 load 走 kv
    cfg = _normalize(raw if isinstance(raw, dict) else {})
    return cfg


def save(workspace_root: str = "", user_id: str = "", cfg: dict | None = None) -> None:
    """写入聚合配置（合并 DEFAULTS 保证字段完整）。"""
    out = _normalize(cfg or {})
    _store().kv_put(_SCOPE, _KEY, out)


def touch_last_run(workspace_root: str = "", user_id: str = "") -> None:
    """更新 last_run_at 为当前时间（保留其它字段）。调度器聚合后调用。"""
    cfg = load(workspace_root, user_id)
    cfg["last_run_at"] = datetime.now().isoformat(timespec="seconds")
    save(workspace_root, user_id, cfg)
