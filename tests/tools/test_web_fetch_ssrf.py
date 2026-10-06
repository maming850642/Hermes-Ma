"""web_fetch SSRF 防护回归测试。

覆盖 _assert_safe_url：拒绝私网/环回/链路本地/云元数据地址，
拒绝非 http(s) scheme。
"""
import ipaddress
from unittest.mock import patch

import pytest

from src.tools.web_fetch import _assert_safe_url, _is_private_ip


class TestIsPrivateIp:
    def test_loopback_v4(self):
        assert _is_private_ip(ipaddress.ip_address("127.0.0.1"))

    def test_private_10(self):
        assert _is_private_ip(ipaddress.ip_address("10.0.0.1"))

    def test_private_192168(self):
        assert _is_private_ip(ipaddress.ip_address("192.168.1.1"))

    def test_link_local_metadata(self):
        # 169.254.169.254 云元数据
        assert _is_private_ip(ipaddress.ip_address("169.254.169.254"))

    def test_loopback_v6(self):
        assert _is_private_ip(ipaddress.ip_address("::1"))

    def test_public_not_private(self):
        assert not _is_private_ip(ipaddress.ip_address("8.8.8.8"))


def _mock_resolve(host, ips):
    """构造一个 getaddrinfo mock，让 host 解析到给定 IP 列表。"""
    def fake_getaddrinfo(hostname, *args, **kwargs):
        return [(0, 0, 0, "", (ip, 0)) for ip in ips]
    return fake_getaddrinfo


class TestAssertSafeUrl:
    def test_rejects_metadata_endpoint(self):
        with patch("src.tools.web_fetch.socket.getaddrinfo",
                   _mock_resolve("169.254.169.254", ["169.254.169.254"])):
            err = _assert_safe_url("http://169.254.169.254/latest/meta-data/")
        assert err is not None
        assert "内网" in err or "保留" in err

    def test_rejects_localhost(self):
        with patch("src.tools.web_fetch.socket.getaddrinfo",
                   _mock_resolve("127.0.0.1", ["127.0.0.1"])):
            err = _assert_safe_url("http://127.0.0.1:8000/admin")
        assert err is not None

    def test_rejects_internal_192168(self):
        with patch("src.tools.web_fetch.socket.getaddrinfo",
                   _mock_resolve("192.168.1.1", ["192.168.1.1"])):
            err = _assert_safe_url("http://192.168.1.1/")
        assert err is not None

    def test_rejects_non_http_scheme(self):
        err = _assert_safe_url("file:///etc/passwd")
        assert err is not None
        assert "http" in err

    def test_rejects_ftp_scheme(self):
        err = _assert_safe_url("ftp://example.com/file")
        assert err is not None

    def test_accepts_public_url(self):
        with patch("src.tools.web_fetch.socket.getaddrinfo",
                   _mock_resolve("example.com", ["93.184.216.34"])):
            err = _assert_safe_url("https://example.com/")
        assert err is None

    def test_rejects_if_any_ip_private(self):
        # 一个公网 + 一个私网 → 任一私网即拒
        with patch("src.tools.web_fetch.socket.getaddrinfo",
                   _mock_resolve("evil.com", ["93.184.216.34", "10.0.0.5"])):
            err = _assert_safe_url("http://evil.com/")
        assert err is not None
