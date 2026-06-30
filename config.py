"""
Hermes 配置加载器。

配置值集中在 config.yaml（唯一真相源），本文件只负责读取。
环境变量（大写下划线，如 OPENAI_API_KEY）可覆盖 yaml 同名键。

返回的 settings 对象同时支持属性访问（settings.llm_model_name）
和字典访问（settings["llm_model_name"]），兼容历史调用代码。
"""

import os
from pathlib import Path
from functools import lru_cache

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent


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
        "web_fetch_timeout", "web_max_chars",
    }
    for k in _INT_KEYS:
        if k in cfg and cfg[k] != "":
            try:
                cfg[k] = int(float(cfg[k]))
            except (TypeError, ValueError):
                pass

    return _Settings(cfg)


settings = get_settings()
