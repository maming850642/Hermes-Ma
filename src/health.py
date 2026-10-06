"""
Hermes 启动自检模块
检查 LLM API 的连通性。

2026-06-27 (M0)：移除 Qdrant / Embedding 检查（记忆改纯文件，不再依赖二者）。
"""

import logging

import httpx

from config import get_settings

logger = logging.getLogger("hermes.health")


def check_llm_api() -> bool:
    settings = get_settings()
    base = settings.openai_base_url
    key = settings.openai_api_key
    # F3：空 key 与 run_health.py 语义统一——WARN 跳过而非 FAIL（本地无鉴权
    # 推理端点 key 可为空）
    if not key:
        logger.warning("LLM API Key 为空，跳过认证检查")
        return True
    url = f"{base.rstrip(chr(47))}/models"
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        r = httpx.get(url, headers=headers, timeout=10.0)
        # 对齐 run_health.py 的 WARN 矩阵：401 → FAIL（抛 ValueError），
        # 其余非 200（404/5xx 等）→ WARN（带状态码日志，返回 True 不阻塞
        # 启动）。此前非 401 一律 return True，端点异常被静默当 PASS。
        if r.status_code == 401:
            raise ValueError("LLM API Key invalid (401)")
        if r.status_code != 200:
            logger.warning(f"LLM API 返回非 200 状态码（WARN，不阻塞启动）: {r.status_code}")
        return True
    except httpx.ConnectError:
        raise ConnectionError(f"LLM API unreachable: {base}")
    except httpx.TimeoutException:
        raise ConnectionError(f"LLM API timeout: {base}")


def run_health_check(silent: bool = False) -> bool:
    from rich.console import Console
    console = Console()
    checks = [
        ("LLM API", check_llm_api),
    ]
    all_ok = True
    for name, fn in checks:
        try:
            fn()
            if not silent:
                console.print(f"  OK {name}")
        except Exception as e:
            all_ok = False
            if not silent:
                console.print(f"  FAIL {name}: {e}")
    return all_ok
