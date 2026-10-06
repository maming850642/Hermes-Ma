"""
Hermes 配置加载器。

配置值集中在 config.yaml（唯一真相源），本文件只负责读取。
环境变量（大写下划线，如 OPENAI_API_KEY）可覆盖 yaml 同名键。

返回的 settings 对象同时支持属性访问（settings.llm_model_name）
和字典访问（settings["llm_model_name"]），兼容历史调用代码。
"""

import os
import threading
from pathlib import Path
from functools import lru_cache

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent

# 已知布尔键：全库以真值判断消费（resolve.py config_guard / run_shell.py
# / session_store.save_session / logging setup 等）。API 层 updates 是
# dict[str,str]，'false' 字符串恒真——写入/合并前必须归一（见
# normalize_setting_value）。清单来自全库 grep bool 消费键：
#   shell_enabled       → tools/bash.yaml config_guard / run_shell.py / schema.py
#   self_evolve_enabled → tools/self_backup.yaml 等三件套 config_guard
#                         （自进化工具面出厂默认关，设置页可开；缺省键
#                          resolve_tools 按 getattr(..., False) 视为关）
#   session_persist     → session_store.save_session / ensure_session_stub
#   log_to_file         → web_fastapi/main.py / worker_process.py（logging setup）
_BOOL_KEYS = {"shell_enabled", "self_evolve_enabled", "session_persist", "log_to_file"}


def normalize_setting_value(key: str, value):
    """已知类型键的值归一（写入前统一咽喉，读/写两侧共用）。

    布尔键：'true'/'1'/1/True → True，'false'/'0'/0/False → False；
    其余值原样返回（未知字符串不猜，交给消费方默认语义）。
    非 _BOOL_KEYS 键原样直通——整数键归一仍由 get_settings 的 _INT_KEYS
    负责（yaml 读取侧），避免双处维护数值清单。
    """
    if key not in _BOOL_KEYS:
        return value
    if isinstance(value, bool):
        return value
    if value in ("true", "1", 1):
        return True
    if value in ("false", "0", 0):
        return False
    return value


class _Settings(dict):
    """支持属性访问的 dict。"""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"config key not found: {name}") from None

    def __setattr__(self, name, value):
        self[name] = value


@lru_cache
def get_settings() -> _Settings:
    """读取 config.yaml 返回 _Settings 对象。环境变量可覆盖同名键。"""
    with open(PROJECT_ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    # 环境变量覆盖同名键。
    # 2026-06-19: 修复 Bug 7 —— 原逻辑用 os.environ 反向扩展键集合，
    # 会把 PATH/TEMP/HOME/PYTHONPATH 等全部以小写键塞进 settings，
    # 导致 settings 被无关键污染、排查时容易误导。现在只对 cfg 已有键做覆盖。
    #
    # 2026-06-22: 防御外部进程注入截断过的值。父进程（ZCode 启动器）曾把
    # EMBEDDING_MODEL 注入为 "all-minilm-l6-v2-f32:latest"（丢了 tazarov/
    # 命名空间前缀），覆盖 yaml 里的正确值导致 Ollama 404。当 yaml 值是
    # "ns/name" 形式、环境变量值正好是去掉前缀后的部分时，判为坏注入，
    # 保留 yaml 作为真相源。用户级注册表项已删除，此处为进程内兜底。
    for key in list(cfg):
        env_val = os.environ.get(key.upper())
        if env_val is None:
            continue
        yaml_val = cfg[key]
        if (
            isinstance(yaml_val, str)
            and isinstance(env_val, str)
            and "/" in yaml_val
            and "/" not in env_val
            and yaml_val.endswith("/" + env_val)
        ):
            continue  # 坏注入，保留 yaml 值
        cfg[key] = env_val

    os.environ.setdefault("OPENAI_API_KEY", cfg.get("openai_api_key", ""))
    os.environ.setdefault("OPENAI_BASE_URL", cfg.get("openai_base_url", ""))

    # 已知数值键:yaml 里若带引号(如 llm_timeout: '120')会被解析成 str,
    # 传给 socket.settimeout / timeout 比较时会 TypeError。这里强制转 int。
    _INT_KEYS = {
        "llm_timeout", "max_short_term_messages", "max_memory_results",
        "max_agent_iterations", "tool_loop_threshold", "compact_keep_recent",
        "compact_min_messages", "compact_summary_max_tokens", "fs_max_file_size",
        "shell_timeout", "shell_max_output_chars", "web_search_timeout",
        "web_fetch_timeout", "model_context_window",
        "compact_threshold_pct", "tool_timeout", "max_tokens",
        # ADR-0005/M3 产品面限流键（缺席时用 getattr 默认值，不强制写 yaml）
        "web_tree_max_entries", "web_tree_preview_max_bytes",
        "git_clone_timeout_seconds", "web_chat_queue_wait_seconds",
        "web_max_parallel_sessions",
    }
    for k in _INT_KEYS:
        if k in cfg and cfg[k] != "":
            try:
                cfg[k] = int(float(cfg[k]))
            except (TypeError, ValueError):
                pass

    # 布尔键归一（同 _INT_KEYS 原因：yaml 带引号 → str）。历史 config.yaml
    # 存量 shell_enabled: 'true' 字符串在此被纠正为真布尔；'false' 字符串
    # 此前恒真（bool('false')==True）导致开关关不掉——这是读取侧兜底，
    # 写入侧归一见 config_service.save_yaml / worker _apply_settings_update。
    for k in _BOOL_KEYS:
        if k in cfg:
            cfg[k] = normalize_setting_value(k, cfg[k])

    # ── waker（数字员工）子系统默认配置 ──
    # yaml 未配置 waker 段时注入默认值，让 scheduler / app 能直接读取。
    # 用户在 config.yaml 写 waker 段则整体覆盖默认。
    _WAKER_DEFAULTS = {
        "enabled": True,
        "tick_seconds": 30,
        "max_concurrent": 2,
    }
    waker_cfg = cfg.get("waker")
    if not isinstance(waker_cfg, dict):
        waker_cfg = {}
    for k, default_v in _WAKER_DEFAULTS.items():
        if k not in waker_cfg:
            waker_cfg[k] = default_v
    # 强转数值键（同 _INT_KEYS 原因：yaml 引号 → str）
    for k in ("tick_seconds", "max_concurrent"):
        v = waker_cfg.get(k)
        if isinstance(v, str) and v != "":
            try:
                waker_cfg[k] = int(float(v))
            except (TypeError, ValueError):
                pass
    cfg["waker"] = waker_cfg

    return _Settings(cfg)


# reload_settings 的重建锁（线程安全：并发保存配置时保证单例完整重建）
_settings_lock = threading.Lock()


def reload_settings() -> _Settings:
    """重读 config.yaml 重建模块级 settings 单例（模型热切换用）。

    get_settings 是 lru_cache 单例：原位改对象会留下半更新状态。这里在
    模块锁内清缓存重建、把模块级 settings 指向新对象——旧引用（如
    agent.settings）保持旧值快照，新取用（get_settings()）拿到新值。
    worker 子进程不调用本函数：它们的模型参数统一经 llm_params_set
    注入（避免与主进程各读各的文件造成双源不一致）。
    """
    global settings
    with _settings_lock:
        get_settings.cache_clear()
        settings = get_settings()
    return settings


settings = get_settings()
