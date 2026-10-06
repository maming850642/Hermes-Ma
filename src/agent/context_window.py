"""
============================================
模型上下文窗口获取模块
============================================
确定当前模型的最大上下文 token 数（context window）。

由于自托管 OpenAI 兼容服务器（vLLM / one-api 等）对 /v1/models 响应字段
并不统一，标准 OpenAI 规范也不含上下文长度，故采用「配置项优先 + best-effort
自动探测 + 保守默认」三级回落：

    1. config.yaml 的 model_context_window > 0 → 直接用（可靠真相源）
    2. 自动探测 /v1/models 响应里的 max_model_len / context_length 等扩展字段
    3. 回落到保守默认（32768）

结果进程内 lru_cache：改配置需重启（与现有 system 配置语义一致）。
"""

import logging
from functools import lru_cache

import httpx

from config import get_settings

logger = logging.getLogger("hermes.agent.context_window")

# 探测时尝试读取的字段名（不同服务端命名不一）
_CANDIDATE_FIELDS = (
    "max_model_len",
    "context_length",
    "max_context_length",
    "max_position_embeddings",
    "max_seq_len",
)

# 自动探测失败时的保守默认
_DEFAULT_CONTEXT_WINDOW = 32768

# 服务端 400 context_length_exceeded 回报的真实窗口（低于配置时覆盖）
_server_context_window: int | None = None


def detect_context_window() -> int | None:
    """
    best-effort 自动探测模型上下文窗口。

    GET {base_url}/models，遍历响应里与当前 llm_model_name 匹配的对象，
    读取 max_model_len / context_length 等扩展字段。

    任何异常（网络/解析/字段缺失）都吞掉返回 None，绝不阻塞调用方。
    """
    settings = get_settings()
    base = settings.openai_base_url
    key = settings.openai_api_key
    target_model = settings.llm_model_name

    if not base:
        return None

    url = f"{base.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    try:
        r = httpx.get(url, headers=headers, timeout=10.0)
        if r.status_code != 200:
            logger.debug(f"自动探测 /models 返回 {r.status_code}，跳过")
            return None
        data = r.json()
    except Exception as e:
        logger.debug(f"自动探测上下文窗口失败（忽略）: {e}")
        return None

    # 响应通常是 {"data": [{"id": "...", ...}, ...]}，也兼容裸 list
    models = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(models, list):
        return None

    for m in models:
        if not isinstance(m, dict):
            continue
        # 模型 id 匹配（宽松：含子串即可，应对 "qwen2.5:32b" vs "qwen2.5:32b-instruct"）
        mid = m.get("id") or m.get("name") or ""
        if target_model and mid and target_model not in mid and mid not in target_model:
            continue
        for field in _CANDIDATE_FIELDS:
            val = m.get(field)
            if isinstance(val, (int, float)) and val > 0:
                logger.info(
                    f"自动探测到上下文窗口: {field}={int(val)} (model={mid})"
                )
                return int(val)

    logger.debug(
        f"/models 未暴露上下文窗口字段（model={target_model}），回落到配置/默认"
    )
    return None


def note_server_context_window(n: int) -> None:
    """服务端明确拒绝超长请求时记下真实窗口（只降不升）。"""
    global _server_context_window
    try:
        n = int(n)
    except (TypeError, ValueError):
        return
    if n <= 0:
        return
    if _server_context_window is None or n < _server_context_window:
        _server_context_window = n
        get_context_window.cache_clear()
        logger.warning(
            f"服务端上下文窗口={n}，低于配置；已覆盖本地窗口并清缓存"
        )


@lru_cache
def get_context_window() -> int:
    """
    组合解析模型上下文窗口（token 数）。结果进程内缓存。

    优先级：服务端拒绝回报 < config.yaml model_context_window > 0
    → 自动探测 → 保守默认。
    """
    settings = get_settings()
    configured = settings.get("model_context_window", 0)
    try:
        configured = int(float(configured or 0))
    except (TypeError, ValueError):
        configured = 0

    if configured > 0:
        logger.info(f"使用配置的上下文窗口: {configured}")
        window = configured
    else:
        detected = detect_context_window()
        if detected and detected > 0:
            window = detected
        else:
            logger.info(f"上下文窗口未知，回落保守默认: {_DEFAULT_CONTEXT_WINDOW}")
            window = _DEFAULT_CONTEXT_WINDOW

    if _server_context_window and _server_context_window < window:
        logger.warning(
            f"配置/探测窗口 {window} 被服务端真实窗口 {_server_context_window} 截断"
        )
        return _server_context_window
    return window
