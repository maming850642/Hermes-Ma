"""
web_fetch / web_search 的 web_proxy 配置传递测试。

背景（2026-06-24）：这两个工具此前硬编码直连，忽略 config 里的 web_proxy（死配置）。
本次恢复 web_proxy 支持：空=直连（传 None），非空=走代理（传该 URL 字符串）。
本测试用 mock 拦截 httpx.Client，断言 proxy= 参数被正确传入，不依赖真实网络。
httpx ≥0.28 已移除 proxies=（复数），必须用 proxy=（单数）。
"""

from unittest.mock import MagicMock, patch

import pytest

from src.tools.web_fetch import web_fetch
from src.tools.web_search import web_search


def _settings_with(proxy_value):
    """构造带指定 web_proxy（及必要超时键）的 settings 对象。"""
    s = type("S", (), {})()
    s.web_proxy = proxy_value
    s.web_fetch_timeout = 5
    s.web_fetch_max_chars = 1000
    s.web_search_timeout = 5
    return s


def _make_fake_client():
    """构造一个假的 httpx.Client 上下文管理器，返回假响应。"""
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = "<html>ok</html>"
    fake_resp.raise_for_status = MagicMock()
    fake_client = MagicMock()
    fake_client.get = MagicMock(return_value=fake_resp)
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    return fake_client


class TestWebFetchProxy:
    """web_fetch 应把 web_proxy 正确传给 httpx.Client。"""

    def test_empty_proxy_means_direct_none(self):
        """web_proxy 为空 -> 传 proxy=None（直连）。"""
        with patch("src.tools.web_fetch.get_settings",
                   return_value=_settings_with("")), \
             patch("src.tools.web_fetch.httpx.Client") as mock_client:
            mock_client.return_value = _make_fake_client()
            web_fetch(url="http://example.com")
            _, kwargs = mock_client.call_args
            assert kwargs.get("proxy") is None

    def test_whitespace_only_proxy_means_direct(self):
        """web_proxy 仅空白 -> 同样视为空，传 None。"""
        with patch("src.tools.web_fetch.get_settings",
                   return_value=_settings_with("   ")), \
             patch("src.tools.web_fetch.httpx.Client") as mock_client:
            mock_client.return_value = _make_fake_client()
            web_fetch(url="http://example.com")
            _, kwargs = mock_client.call_args
            assert kwargs.get("proxy") is None

    def test_nonempty_proxy_passed_through(self):
        """web_proxy 非空 -> 原样传给 proxy=。"""
        proxy_url = "http://127.0.0.1:7890"
        with patch("src.tools.web_fetch.get_settings",
                   return_value=_settings_with(proxy_url)), \
             patch("src.tools.web_fetch.httpx.Client") as mock_client:
            mock_client.return_value = _make_fake_client()
            web_fetch(url="http://example.com")
            _, kwargs = mock_client.call_args
            assert kwargs.get("proxy") == proxy_url


class TestWebSearchProxy:
    """web_search 应把 web_proxy 正确传给 httpx.Client。"""

    def test_empty_proxy_means_direct_none(self):
        with patch("src.tools.web_search.get_settings",
                   return_value=_settings_with("")), \
             patch("src.tools.web_search.httpx.Client") as mock_client:
            mock_client.return_value = _make_fake_client()
            web_search(query="test")
            _, kwargs = mock_client.call_args
            assert kwargs.get("proxy") is None

    def test_nonempty_proxy_passed_through(self):
        proxy_url = "http://127.0.0.1:7890"
        with patch("src.tools.web_search.get_settings",
                   return_value=_settings_with(proxy_url)), \
             patch("src.tools.web_search.httpx.Client") as mock_client:
            mock_client.return_value = _make_fake_client()
            web_search(query="test")
            _, kwargs = mock_client.call_args
            assert kwargs.get("proxy") == proxy_url
