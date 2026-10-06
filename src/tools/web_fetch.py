"""
============================================
网页抓取工具（默认国内直连，可选代理）
============================================
给 URL，返回网页正文（纯文本），截断到可控长度。

默认直连目标站点（适合国内站点和必应结果页）。
配置 web_proxy 非空时走代理（适合抓取 GitHub、StackOverflow 等境外站点）。

2026-06-18: 初始创建，对标旧 web_fetch（需代理），当时改为硬编码直连
2026-06-24: 恢复 web_proxy 配置支持（空=直连，非空=走代理），
            httpx ≥0.28 用 proxy= 单数参数
"""

import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

# trafilatura 是可选重型依赖（含二进制 lxml），顶层硬 import 会让整个 agent
# 在缺包/装残时无法构造（曾导致登录 500：tools/__init__ 无条件加载本模块）。
# 改为保护式 import：包在时四级降级链全量生效；包缺失时自动降级到 Tier3 bs4
# （下方 Tier1/Tier2 的 try/except 已能捕获 NoneType 调用并 warning 降级）。
try:
    import trafilatura
except ImportError:
    trafilatura = None


from config import get_settings

logger = logging.getLogger("hermes.tools.web_fetch")


# ============================================
# SSRF 防护
# ============================================
_MAX_REDIRECTS = 5


def _is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判断 IP 是否为私网/环回/链路本地/保留地址（禁止访问）。"""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _assert_safe_url(url: str) -> str | None:
    """校验 URL 安全性。合法返回 None；非法返回错误原因字符串。

    - scheme 必须是 http/https
    - 解析 host 的所有 A/AAAA 记录，任一落在私网/环回/链路本地即拒绝
      （防止访问 169.254.169.254 云元数据、127.0.0.1 本机、内网服务）
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "URL 解析失败"
    if parsed.scheme not in ("http", "https"):
        return f"仅允许 http/https（得到 {parsed.scheme or '空'}）"
    host = parsed.hostname
    if not host:
        return "URL 缺少 host"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return f"无法解析主机: {host}"
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_private_ip(ip):
            return f"目标主机解析到内网/保留地址 {ip}（已拒绝）"
    return None


# ============================================
# 正文提取：四级降级链
# ============================================
# trafilatura.extract() 在 JS 渲染页 / SPA / 重定向壳上常返回空，
# 旧逻辑直接报错丢失全部信息。这里逐级降级，尽量榨取可用内容。
# 每级失败才进下一级；任一级产出非空文本即返回（仍走统一截断）。
# 函数为纯函数（入参 html 字符串 + max_chars），便于不联网单测。


def _truncate(text: str, max_chars: int) -> str:
    """统一截断：超长加标记，保持原有 [...内容已截断...] 风格。"""
    if len(text) > max_chars:
        logger.debug(f"web_fetch 内容截断: {len(text)} -> {max_chars}")
        return text[:max_chars] + "\n\n[...内容已截断...]"
    return text


# SPA 壳判定阈值：trafilatura 抽出的正文短于此值（且 bs4 元数据更长）时，
# 判定为非真正正文（常见为 noscript 警告、空壳），改用元数据兜底。
_THIN_BODY_THRESHOLD = 80


def _extract_with_fallback(html: str, max_chars: int) -> str:
    """
    四级降级提取正文。

    Tier 1: trafilatura.extract(favor_recall=True) —— 主力正文提取（更激进）。
    Tier 2: trafilatura.bare_extraction() —— 拿 Document 对象，正文空则
            至少返回 title/description/sitename 元数据。
    Tier 3: BeautifulSoup —— 兜底拿 <title> + <meta> + <noscript> + 可见文本
            （适合给爬虫喂静态壳的 SPA，如 Twitter/X 的 t.co）。
    Tier 4: 仍无内容则返回带 len(html) 的错误（辅助诊断，不再笼统）。

    Args:
        html: 已抓取的完整 HTML 字符串。
        max_chars: 返回文本的最大字符数。

    Returns:
        提取到的正文/元数据文本；四级全空时返回 "错误：..." 字符串。
    """
    if not html or not html.strip():
        return f"错误：无法从该页面提取正文内容（HTML 为空）。"

    # 预先抓一份 bs4 元数据（title + og/description meta）。
    # 两个用途：(a) Tier1 返回极短（非真正正文，如 SPA 的 noscript 壳）时
    # 优先用元数据；(b) Tier3 兜底。失败不致命。
    bs4_meta = ""
    try:
        bs4_meta = _extract_with_bs4(html)
    except Exception as e:
        logger.warning(f"web_fetch bs4 元数据预抓失败: {e}")

    # ---- Tier 1: trafilatura.extract（主力，favor_recall 更激进）----
    try:
        text = trafilatura.extract(
            html, include_links=False, include_tables=True, favor_recall=True
        )
        if text and text.strip():
            text = text.strip()
            # SPA 壳陷阱：trafilatura 在 favor_recall 下会把 <noscript> 警告
            # 当正文返回（如 Twitter/X 的"需要启用 JavaScript"）。此时若 bs4
            # 元数据（og:title/og:description）更长更具体，优先用元数据。
            if len(text) < _THIN_BODY_THRESHOLD and len(bs4_meta) > len(text):
                logger.info(
                    f"web_fetch Tier1 正文过短({len(text)}字符)，改用元数据兜底"
                )
                return _truncate(bs4_meta, max_chars)
            return _truncate(text, max_chars)
    except Exception as e:
        logger.warning(f"web_fetch Tier1 trafilatura.extract 失败: {e}")

    # ---- Tier 2: trafilatura.bare_extraction（元数据 Document 兜底）----
    try:
        doc = trafilatura.bare_extraction(
            html, include_links=False, include_tables=True, favor_recall=True
        )
        if doc is not None:
            # 有正文就先用正文
            doc_text = getattr(doc, "text", None) or ""
            if doc_text.strip():
                return _truncate(doc_text.strip(), max_chars)
            # 正文空但元数据可能在 —— 组装 title/description/sitename
            parts: list[str] = []
            title = (getattr(doc, "title", None) or "").strip()
            desc = (getattr(doc, "description", None) or "").strip()
            site = (getattr(doc, "sitename", None) or "").strip()
            if title:
                parts.append(f"标题: {title}")
            if desc:
                parts.append(f"摘要: {desc}")
            if site:
                parts.append(f"来源: {site}")
            if parts:
                return _truncate("\n".join(parts), max_chars)
    except Exception as e:
        logger.warning(f"web_fetch Tier2 trafilatura.bare_extraction 失败: {e}")

    # ---- Tier 3: BeautifulSoup 静态文本兜底（SPA 壳 / og meta / noscript）----
    # 复用顶部预抓的 bs4_meta；预抓失败时再算一次。
    text = bs4_meta
    if not text or not text.strip():
        try:
            text = _extract_with_bs4(html)
        except Exception as e:
            logger.warning(f"web_fetch Tier3 BeautifulSoup 失败: {e}")
    if text and text.strip():
        return _truncate(text.strip(), max_chars)

    # ---- Tier 4: 最终错误（带 len(html) 辅助诊断）----
    return (
        f"错误：无法从该页面提取正文内容（可能是 JS 渲染页面或纯图片页面，"
        f"HTML 长度 {len(html)}）。"
    )


def _extract_with_bs4(html: str) -> str:
    """
    BeautifulSoup 兜底提取：title + meta description + noscript + 可见文本。

    复用 web_search.py 的 BeautifulSoup(html, "html.parser") 风格。
    适合 t.co→Twitter 这类给爬虫喂静态 og/noscript 壳的站点。
    """
    soup = BeautifulSoup(html, "html.parser")

    parts: list[str] = []

    # 标题：<title> 或 <meta property="og:title">
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            title = og_title["content"].strip()
    if title:
        parts.append(f"标题: {title}")

    # 描述：<meta name="description"> 或 <meta property="og:description">
    desc = ""
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        desc = meta_desc["content"].strip()
    if not desc:
        og_desc = soup.find("meta", attrs={"property": "og:description"})
        if og_desc and og_desc.get("content"):
            desc = og_desc["content"].strip()
    if desc:
        parts.append(f"摘要: {desc}")

    # 可见正文：先移除非内容标签，再取可见文本
    for tag in soup(["script", "style"]):
        tag.decompose()
    visible = soup.get_text(separator="\n", strip=True)
    # 压缩连续空行
    lines = [ln.strip() for ln in visible.splitlines() if ln.strip()]
    if lines:
        parts.append("--- 页面文本 ---")
        parts.append("\n".join(lines))

    return "\n".join(parts)


def web_fetch(url: str, max_chars: int = 8000) -> str:
    """
    抓取指定 URL 的网页内容，提取正文文本。国内直连，不使用代理。

    适用于获取文章、文档、API 响应等网页内容。
    自动提取正文，去除导航栏、广告等无关内容。

    注意：能否抓取成功取决于目标站点本身是否国内可直连。

    Args:
        url: 要抓取的网页 URL（必须包含 http:// 或 https://）
        max_chars: 返回内容的最大字符数，默认 8000。超出部分截断。

    Returns:
        网页正文文本。如果抓取失败返回错误信息。
    """
    settings = get_settings()
    timeout = settings.web_fetch_timeout
    # 2026-06-24: 恢复 web_proxy 支持。空字符串=直连（国内默认），
    # 非空则走代理（用于抓取境外站点）。httpx ≥0.28 已移除 proxies=（复数），
    # 必须用 proxy=（单数），且空值必须传 None 而非 ""（否则 httpx 把 "" 当无效 URL）。
    proxy = (getattr(settings, "web_proxy", "") or "").strip() or None

    logger.info(f"web_fetch: url={url[:80]}, max_chars={max_chars}, proxy={'on' if proxy else 'direct'}")

    # SSRF 防护：先校验入口 URL，再手动跟随重定向并对每跳目标重新校验
    # （httpx follow_redirects=True 不会校验重定向目标，可被引导到内网）。
    err = _assert_safe_url(url)
    if err:
        logger.warning(f"web_fetch SSRF 拒绝: {url[:80]} -> {err}")
        return f"错误：URL 安全校验失败 - {err}"

    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": _get_user_agent()},
            proxy=proxy,
        ) as client:
            resp = client.get(url)
            # 手动重定向循环：每跳重新做 SSRF 校验
            hops = 0
            while resp.is_redirect and hops < _MAX_REDIRECTS:
                loc = resp.headers.get("location", "")
                if not loc:
                    break
                next_url = str(httpx.URL(url).join(loc))
                err = _assert_safe_url(next_url)
                if err:
                    logger.warning(f"web_fetch 重定向 SSRF 拒绝: {next_url[:80]} -> {err}")
                    return f"错误：重定向目标安全校验失败 - {err}"
                url = next_url
                resp = client.get(url)
                hops += 1
            resp.raise_for_status()
            html = resp.text
    except httpx.TimeoutException:
        logger.error(f"web_fetch 超时: {url}")
        return f"错误：抓取超时（{timeout}秒），目标站点可能无法直连，请稍后重试。"
    except httpx.HTTPStatusError as e:
        logger.error(f"web_fetch HTTP 错误: {e.response.status_code} {url}")
        return f"错误：HTTP {e.response.status_code}，目标页面返回错误。"
    except Exception as e:
        logger.error(f"web_fetch 异常: {e}", exc_info=True)
        return f"错误：抓取失败（目标站点可能无法直连）- {str(e)}"

    # 2026-06-24: 正文提取改为四级降级链（见 _extract_with_fallback）。
    # 之前 trafilatura.extract() 返回空就直接报错，丢失了 title/meta/noscript
    # 等可用信息（JS 渲染页、Twitter/X t.co 重定向命中此缺陷）。
    return _extract_with_fallback(html, max_chars)


def _get_user_agent() -> str:
    """获取 User-Agent 字符串（用完整浏览器 UA 提高命中率）"""
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/115.0.0.0 Safari/537.36"
    )


# ════════════════════════════════════════════════════════════════
# V3 Layer 3 evaluator + PythonExecutor 入口
# ════════════════════════════════════════════════════════════════

def check_ssrf(args: dict, ctx) -> "SideEffectsOverride":
    """V3 Layer 3 evaluator：SSRF 防护。

    签名遵循 SideEffectsEvaluator 协议：(args, ctx) -> SideEffectsOverride。
    复用现有 _assert_safe_url()，URL 指向内网时返回 force_deny=True。
    """
    from src.tools.schema import SideEffectsOverride

    url = args.get("url", "") or ""
    err = _assert_safe_url(url)
    if err:
        return SideEffectsOverride(force_deny=True)
    return SideEffectsOverride()


def _execute_web_fetch(url: str, max_chars: int = 8000, *, ctx=None) -> str:
    """PythonExecutor 入口：包装 web_fetch 业务逻辑。

    业务逻辑（SSRF 防护 + httpx 抓取 + 四级正文提取）在 _do_fetch 函数体。
    """
    return _do_fetch(url, max_chars)


def _do_fetch(url: str, max_chars: int = 8000) -> str:
    """实际抓取逻辑（web_fetch 的函数体）。

    SSRF 防护已在 Layer 3 check_ssrf 完成，这里不再重复检查。
    """
    settings = get_settings()
    timeout = settings.web_fetch_timeout
    proxy = (getattr(settings, "web_proxy", "") or "").strip() or None

    logger.info(f"web_fetch: url={url[:80]}, max_chars={max_chars}")

    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": _get_user_agent()},
            proxy=proxy,
        ) as client:
            resp = client.get(url)
            hops = 0
            while resp.is_redirect and hops < _MAX_REDIRECTS:
                loc = resp.headers.get("location", "")
                if not loc:
                    break
                next_url = str(httpx.URL(url).join(loc))
                err = _assert_safe_url(next_url)
                if err:
                    return f"错误：重定向目标安全校验失败 - {err}"
                url = next_url
                resp = client.get(url)
                hops += 1
            resp.raise_for_status()
            html = resp.text
    except httpx.TimeoutException:
        return f"错误：抓取超时（{timeout}秒），目标站点可能无法直连，请稍后重试。"
    except Exception as e:
        return f"错误：抓取失败（目标站点可能无法直连）- {str(e)}"

    return _extract_with_fallback(html, max_chars)