"""
============================================
网络搜索工具（必应，默认国内直连，可选代理）
============================================
给关键词，返回搜索结果（标题 + 摘要 + URL）。

使用必应(bing.com)网页版，国内可直连、完全免费、无需 API Key。
通过 httpx + BeautifulSoup 解析经典版搜索结果。
配置 web_proxy 非空时走代理（适合境外接口）。

2026-06-18: 初始创建，对标旧 web_search（DuckDuckGo，需代理）
2026-06-19: 修复 Bug 9 —— timeout 读 web_search_timeout 配置，与 web_fetch 风格统一
2026-06-24: 恢复 web_proxy 配置支持（空=直连，非空=走代理）
"""

import logging
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from config import get_settings

logger = logging.getLogger("hermes.tools.web_search")

# 必应搜索接口（cn.bing.com 国内直连）
_BING_SEARCH_URL = "https://cn.bing.com/search"

# 浏览器 User-Agent（完整版，避免被必应识别为机器人返回简化页）
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/115.0.0.0 Safari/537.36"
)


def web_search(query: str, max_results: int = 5) -> str:
    """
    使用必应(Bing)搜索网络，国内直连、无需代理、无需 API Key。

    适用于需要查找实时信息、新闻、技术文档等场景。

    Args:
        query: 搜索关键词
        max_results: 返回结果数量，默认 5 条，最多 10 条

    Returns:
        格式化的搜索结果列表字符串。每条结果包含序号、标题、URL 和内容摘要。
    """
    # 限制最大结果数
    max_results = min(max(max_results, 1), 10)

    logger.info(f"web_search: query='{query[:50]}', max_results={max_results}")

    # 2026-06-19: 修复 Bug 9 —— 读取 web_search_timeout 配置，与 web_fetch 风格统一
    settings = get_settings()
    timeout = getattr(settings, "web_search_timeout", 15)
    # 2026-06-24: 恢复 web_proxy 支持。空字符串=直连（国内默认），
    # 非空则走代理（用于搜索境外接口）。httpx ≥0.28 用 proxy=（单数），空值传 None。
    proxy = (getattr(settings, "web_proxy", "") or "").strip() or None

    # 默认请求 cn.bing.com（国内可直连）；web_proxy 非空时走代理
    url = f"{_BING_SEARCH_URL}?q={quote(query)}&count={max_results}"
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    try:
        # debug：精确定位 httpx 阶段（Client 构造 / get / 解码 text），排查卡死用
        logger.debug(f"web_search ENTER httpx stage, timeout={timeout}")
        import time as _t
        _h0 = _t.time()
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers=headers,
            proxy=proxy,
        ) as client:
            logger.debug(f"web_search Client 构造完成 {_t.time()-_h0:.2f}s")
            resp = client.get(url)
            logger.debug(f"web_search client.get 完成 {_t.time()-_h0:.2f}s, status={resp.status_code}")
            resp.raise_for_status()
            html = resp.text
            logger.debug(f"web_search resp.text 解码完成 {_t.time()-_h0:.2f}s, html_len={len(html)}")
    except httpx.TimeoutException:
        logger.error(f"web_search 超时: {query}")
        return "错误：搜索超时，请稍后重试。"
    except Exception as e:
        logger.error(f"web_search 异常: {e}", exc_info=True)
        return f"错误：搜索失败 - {str(e)}"

    # BeautifulSoup 解析必应经典版结果
    soup = BeautifulSoup(html, "html.parser")
    items = soup.find_all("li", class_="b_algo", limit=max_results)

    if not items:
        # 可能是必应返回了验证页或页面结构变化
        logger.warning(f"web_search 未解析到结果: query='{query[:50]}'")
        return f"未找到与 '{query}' 相关的搜索结果。"

    # 格式化结果
    lines = [f"搜索 '{query}' 的结果（必应）：\n"]
    count = 0
    for item in items:
        h2 = item.find("h2")
        if not h2:
            continue
        a = h2.find("a")
        if not a or not a.get("href"):
            continue

        title = a.get_text(strip=True)
        link = a["href"]

        # 摘要在 .b_caption > p
        caption = item.find("div", class_="b_caption")
        snippet = ""
        if caption:
            p = caption.find("p")
            if p:
                snippet = p.get_text(strip=True)
                if len(snippet) > 200:
                    snippet = snippet[:197] + "..."

        count += 1
        lines.append(f"{count}. {title}")
        lines.append(f"   URL: {link}")
        if snippet:
            lines.append(f"   摘要: {snippet}")
        lines.append("")

    if count == 0:
        return f"未找到与 '{query}' 相关的搜索结果。"

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# V3 PythonExecutor 入口
# ════════════════════════════════════════════════════════════════

def _execute_web_search(query: str, max_results: int = 5, *, ctx=None) -> str:
    """V3 PythonExecutor 入口。"""
    return web_search(query, max_results)