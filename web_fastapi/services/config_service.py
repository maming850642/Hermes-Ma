"""
配置服务：读写 config.yaml + 掩码 + 热更新分类。

C 方案核心：把配置项分为「系统（需重启）」和「per-user 偏好（热更新）」。
"""
import yaml
from pathlib import Path
from config import PROJECT_ROOT, normalize_setting_value

# per-user 热更新键（对应 UserPrefs 字段）。
# memory_min_score 已移除（2026-09-05）：检索打分改纯向量相关度语义后
# manager 退役了该覆盖率阈值（search 恒以 min_score=0.0 调用），设置页
# 同步删除该输入行——不再展示/保存一个无消费方的死旋钮。
HOT_RELOADABLE_KEYS = {
    "workspace_root", "web_proxy", "temperature",
    "max_tokens", "max_memory_results",
    "compact_threshold_pct",
}

# 安全相关：永远不开放热更新
SECURITY_KEYS = {"shell_enabled", "shell_allowed_commands", "shell_blocked_patterns"}


def classify_keys() -> tuple[list[str], list[str]]:
    """返回 (system_keys, personal_keys)。"""
    return (
        ["openai_api_key", "openai_base_url", "llm_model_name", "llm_timeout",
         "model_context_window",
         "shell_enabled", "shell_allowed_commands", "shell_blocked_patterns",
         "shell_timeout", "shell_max_output_chars",
         "self_evolve_enabled"],
        list(HOT_RELOADABLE_KEYS),
    )


def is_hot_reloadable(key: str) -> bool:
    return key in HOT_RELOADABLE_KEYS


def mask_secret(value: str) -> str:
    """掩码敏感值：保留前缀，其余打 *。短值全掩。"""
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 4)


def load_yaml(path: Path | None = None) -> dict:
    p = path or (PROJECT_ROOT / "config.yaml")
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_yaml(updates: dict, path: Path | None = None) -> None:
    """合并写回 config.yaml（保留其他键）。

    写入前对已知布尔键做类型归一（normalize_setting_value）：API 层
    updates 是 dict[str,str]，'false' 字符串若原样落盘（yaml 会写成带引号
    的 'false'）则读取侧 bool 恒真、shell 开关关不掉。归一后 Python False
    落盘为不带引号的 false，读回是真布尔。
    """
    p = path or (PROJECT_ROOT / "config.yaml")
    cfg = load_yaml(p)
    cfg.update({k: normalize_setting_value(k, v) for k, v in (updates or {}).items()})
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
