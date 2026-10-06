"""
S2 Web 安全基线回归测试（免认证形态，ADR-0005 D5）。

背景演进：登录/cookie/WEB_SECRET_KEY 已整体移除；免认证下的最后防线
收敛为「默认仅回环监听 + 非回环显式警告」。

覆盖：
1. 默认 host 断言（127.0.0.1；env WEB_HOST / settings.web_host 可显式覆盖）；
2. 显式非回环时启动警告（无访问控制 + 局域网暴露风险提示）。
"""
import pytest


# ============================================
# 1. 默认 host
# ============================================

class TestDefaultHost:

    def test_default_is_loopback(self, monkeypatch):
        """无 env、无配置 → 默认 127.0.0.1（仅本机）。"""
        monkeypatch.delenv("WEB_HOST", raising=False)
        from web_fastapi.main import resolve_web_host
        assert resolve_web_host(settings={}) == "127.0.0.1"

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("WEB_HOST", "0.0.0.0")
        from web_fastapi.main import resolve_web_host
        assert resolve_web_host(settings={}) == "0.0.0.0"

    def test_settings_override(self, monkeypatch):
        monkeypatch.delenv("WEB_HOST", raising=False)
        from web_fastapi.main import resolve_web_host
        assert resolve_web_host(settings={"web_host": "192.168.1.5"}) == "192.168.1.5"

    def test_env_wins_over_settings(self, monkeypatch):
        monkeypatch.setenv("WEB_HOST", "127.0.0.1")
        from web_fastapi.main import resolve_web_host
        assert resolve_web_host(settings={"web_host": "0.0.0.0"}) == "127.0.0.1"


# ============================================
# 2. 免认证语义
# ============================================

class TestNoAuthForm:

    def test_create_app_has_no_secret_state(self):
        """create_app 不再挂 secret/verify_cookie/make_cookie（认证已移除）。"""
        import web_fastapi.app as app_mod
        app = app_mod.create_app()
        assert not hasattr(app.state, "secret")
        assert not hasattr(app.state, "verify_cookie")
        assert not hasattr(app.state, "make_cookie")


# ============================================
# 3. 暴露面警告
# ============================================

class TestExposedHostWarning:

    def test_wildcard_host_warns_no_access_control(self, capsys):
        """显式 0.0.0.0 → 打印显著警告（无任何访问控制 + 局域网暴露）。"""
        from web_fastapi.main import warn_if_exposed
        warned = warn_if_exposed("0.0.0.0", 8000)
        assert warned is True
        out = capsys.readouterr().out
        assert "0.0.0.0" in out
        assert "没有任何访问控制" in out, "警告应明示当前为无访问控制形态"

    def test_lan_host_warns(self, capsys):
        from web_fastapi.main import warn_if_exposed
        assert warn_if_exposed("192.168.1.10", 8000) is True

    def test_loopback_no_warn(self, capsys):
        """127.0.0.1 / localhost / ::1 不告警。"""
        from web_fastapi.main import warn_if_exposed
        for host in ("127.0.0.1", "localhost", "::1"):
            assert warn_if_exposed(host, 8000) is False
        assert capsys.readouterr().out == ""
