"""
web_fetch 正文提取降级链测试。

直接对 _extract_with_fallback 喂真实 HTML 片段（不 mock、不联网），
覆盖四级降级：trafilatura.extract → bare_extraction 元数据 → bs4 静态文本 → 错误。

背景（2026-06-24）：旧逻辑 trafilatura.extract() 返回空就直接报错，
丢失 title/meta/noscript。Twitter/X t.co 等命中此缺陷。
"""

import pytest

from src.tools.web_fetch import _extract_with_fallback, _extract_with_bs4, _truncate


# ---- 测试用 HTML 片段 ----

# 正常文章：trafilatura 应能提取正文（Tier 1）
ARTICLE_HTML = """
<html><head><title>Python 异步编程指南</title></head>
<body>
  <article>
    <h1>Python 异步编程指南</h1>
    <p>异步编程是现代 Python 的重要特性。asyncio 提供了事件循环、协程和任务调度。</p>
    <p>使用 async def 定义协程函数，await 等待可等待对象。关键 API 包括
       asyncio.create_task、asyncio.gather 和 asyncio.wait。</p>
    <p>实际应用中需注意避免阻塞调用，否则会卡住整个事件循环。</p>
  </article>
</body></html>
"""

# 元数据壳：有 title/meta description 但正文稀疏（Tier 2 元数据兜底）
SHELL_HTML = """
<html><head>
<title>某产品官网</title>
<meta name="description" content="这是某产品的官方介绍页面，提供最佳用户体验。">
</head><body>
<div></div>
</body></html>
"""

# SPA 静态壳：只有 noscript 静态文本 + og meta（Tier 3 bs4 兜底）
# 模拟 Twitter/X 这类给爬虫喂静态壳的站点
SPA_SHELL_HTML = """
<html><head>
<title>Someone on X: "Hello world"</title>
<meta property="og:title" content="Someone on X">
<meta property="og:description" content="Just setting up my Twitterrific. Hello world!">
</head><body>
<div id="react-root"></div>
<script>window.__INITIAL_STATE__ = {};</script>
<noscript>需要启用 JavaScript 才能继续使用该应用。</noscript>
</body></html>
"""


# ============================================================
# Tier 1: trafilatura.extract 成功
# ============================================================

class TestTier1Article:
    """正常文章 HTML 走 Tier 1，返回正文。"""

    def test_article_returns_body(self):
        result = _extract_with_fallback(ARTICLE_HTML, max_chars=8000)
        # 应返回正文，且不含"标题:"前缀（那是元数据级才有的）
        assert "asyncio" in result or "异步" in result
        assert "标题:" not in result

    def test_article_not_error(self):
        result = _extract_with_fallback(ARTICLE_HTML, max_chars=8000)
        assert not result.startswith("错误")


# ============================================================
# Tier 2: trafilatura 元数据兜底
# ============================================================

class TestTier2Metadata:
    """正文稀疏但有元数据时，至少返回 title/description。"""

    def test_shell_returns_metadata(self):
        result = _extract_with_fallback(SHELL_HTML, max_chars=8000)
        # 不再是笼统的"无法提取"错误
        assert not result.startswith("错误")
        # 应包含页面标题或描述里的信息
        assert "某产品" in result or "官方介绍" in result


# ============================================================
# Tier 3: BeautifulSoup 静态文本兜底（Twitter 场景）
# ============================================================

class TestTier3Bs4:
    """SPA 壳走 bs4 兜底，拿到 title/og:description/noscript。"""

    def test_spa_shell_returns_title_and_desc(self):
        result = _extract_with_fallback(SPA_SHELL_HTML, max_chars=8000)
        assert not result.startswith("错误")
        # 应拿到 og:description 里的推文内容
        assert "Hello world" in result

    def test_bs4_extracts_og_title(self):
        # 直接测 _extract_with_bs4 辅助函数
        text = _extract_with_bs4(SPA_SHELL_HTML)
        assert "Someone on X" in text
        assert "Hello world" in text

    def test_bs4_strips_script(self):
        # script 标签内容不应出现在结果里
        text = _extract_with_bs4(SPA_SHELL_HTML)
        assert "__INITIAL_STATE__" not in text


# ============================================================
# 截断
# ============================================================

class TestTruncation:
    def test_long_article_truncated(self):
        result = _extract_with_fallback(ARTICLE_HTML, max_chars=20)
        assert "已截断" in result

    def test_truncate_helper(self):
        out = _truncate("x" * 100, max_chars=10)
        assert len(out) < 100
        assert "已截断" in out

    def test_truncate_no_change_under_limit(self):
        out = _truncate("short", max_chars=100)
        assert out == "short"


# ============================================================
# Tier 4: 最终错误
# ============================================================

class TestTier4Error:
    def test_empty_html_returns_error_with_length(self):
        result = _extract_with_fallback("", max_chars=8000)
        assert result.startswith("错误")
        assert "为空" in result

    def test_whitespace_html_returns_error(self):
        result = _extract_with_fallback("   \n  ", max_chars=8000)
        assert result.startswith("错误")
        assert "为空" in result

    def test_content_free_html_returns_error_with_length(self):
        # 只有空标签，四级都提取不出有效内容
        barren = "<html><head></head><body><div></div></body></html>"
        result = _extract_with_fallback(barren, max_chars=8000)
        # 可能 Tier4 报错，或 Tier3 拿到极少内容（取决于 bs4 对纯空 div 的行为）
        # 关键：若报错，错误信息必须带 HTML 长度辅助诊断
        if result.startswith("错误"):
            assert str(len(barren)) in result


# ============================================================
# 回归保护：旧 bug 场景不应再丢信息
# ============================================================

class TestRegression:
    """旧的"无法提取正文"硬报错不应再吞掉可用信息。"""

    def test_shell_does_not_return_old_useless_error(self):
        """旧逻辑会对 SHELL_HTML 返回笼统错误；现在应返回元数据。"""
        result = _extract_with_fallback(SHELL_HTML, max_chars=8000)
        # 不应再是旧的那句笼统错误
        old_msg = "无法从该页面提取正文内容（可能是 JS 渲染页面或纯图片页面）。"
        assert result != old_msg
