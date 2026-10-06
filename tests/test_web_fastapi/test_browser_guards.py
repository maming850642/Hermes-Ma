"""P1-5 回归：浏览器侧攻击面收口（Host 白名单 + Origin 同源）。

免认证 + 回环绑定是被 ADR-0005 声明的设计（本机单用户零登录可用），
这里验证的收口只针对浏览器侧：
- DNS rebinding：恶意域名解析到 127.0.0.1 后带任意 Host 访问 → 403；
- CSRF 简单请求：非 GET/HEAD/OPTIONS 且带 Origin 头时必须与 Host 同源
  → 不同源 403；无 Origin 的请求（curl/测试）照常放行。
"""
import pytest
from fastapi.testclient import TestClient


def _build_app():
    from web_fastapi.app import create_app

    app = create_app()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.post("/ping")
    def post_ping():
        return {"ok": True}

    return app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("WEB_HOST", raising=False)
    return TestClient(_build_app())


class TestHostAllowlist:

    def test_loopback_and_testserver_hosts_pass(self, client):
        """回环名单 + TestClient 默认 Host 全放行（零登录可用不受影响）。"""
        for host in ("testserver", "localhost", "127.0.0.1",
                     "localhost:8000", "127.0.0.1:8000", "[::1]:8000"):
            r = client.get("/ping", headers={"Host": host})
            assert r.status_code == 200, host

    def test_foreign_host_rejected(self, client):
        """DNS rebinding：恶意域名的 Host 不在白名单 → 403。"""
        r = client.get("/ping", headers={"Host": "evil.com"})
        assert r.status_code == 403
        assert "Host" in r.json()["detail"]

    def test_empty_host_rejected(self, client):
        assert client.get("/ping", headers={"Host": ""}).status_code == 403

    def test_host_with_port_stripped_for_compare(self, client):
        """带端口的恶意 Host 同样按 hostname 比对拒绝。"""
        assert client.get("/ping",
                          headers={"Host": "evil.com:8000"}).status_code == 403

    def test_configured_web_host_allowed(self, monkeypatch):
        """显式 web_host 配置（局域网部署形态）→ 该 Host 放行。"""
        import config
        monkeypatch.delenv("WEB_HOST", raising=False)
        monkeypatch.setattr(config, "get_settings",
                            lambda: {"web_host": "192.168.7.7"})
        c = TestClient(_build_app())
        assert c.get("/ping", headers={"Host": "192.168.7.7"}).status_code == 200
        assert c.get("/ping",
                     headers={"Host": "192.168.7.7:8000"}).status_code == 200
        assert c.get("/ping", headers={"Host": "testserver"}).status_code == 200
        assert c.get("/ping", headers={"Host": "other.host"}).status_code == 403

    def test_wildcard_web_host_skips_check(self, monkeypatch):
        """0.0.0.0 通配监听（文档化局域网形态）跳过 Host 校验。"""
        monkeypatch.setenv("WEB_HOST", "0.0.0.0")
        c = TestClient(_build_app())
        assert c.get("/ping", headers={"Host": "any.lan.host"}).status_code == 200


class TestOriginSameSource:

    def test_post_without_origin_passes(self, client):
        """curl/测试语义：无 Origin 的非 GET 请求照常放行。"""
        assert client.post("/ping").status_code == 200

    def test_post_same_origin_passes(self, client):
        r = client.post("/ping", headers={"Origin": "http://testserver"})
        assert r.status_code == 200

    def test_post_cross_origin_rejected(self, client):
        """CSRF 简单请求（Form 无 preflight）：跨站 Origin → 403。"""
        r = client.post("/ping", headers={"Origin": "http://evil.com"})
        assert r.status_code == 403
        assert "Origin" in r.json()["detail"]

    def test_post_origin_null_rejected(self, client):
        """sandboxed iframe/file:// 发出的 Origin: null 同样拒。"""
        r = client.post("/ping", headers={"Origin": "null"})
        assert r.status_code == 403

    def test_post_origin_port_mismatch_rejected(self, client):
        assert client.post(
            "/ping", headers={"Origin": "http://testserver:9999"}
        ).status_code == 403

    def test_same_host_different_loopback_name_rejected(self, client):
        """http://127.0.0.1 与 Host testserver 不同源 → 拒（严格同源）。"""
        r = client.post("/ping", headers={"Origin": "http://127.0.0.1"})
        assert r.status_code == 403

    def test_safe_methods_with_cross_origin_pass(self, client):
        """GET/HEAD/OPTIONS 不做 Origin 校验（不构成 CSRF 写）。"""
        assert client.get("/ping",
                          headers={"Origin": "http://evil.com"}).status_code == 200
        # HEAD/OPTIONS 路由不存在 → 405（来自路由而非中间件 403）
        assert client.head("/ping",
                           headers={"Origin": "http://evil.com"}).status_code == 405
        assert client.options("/ping",
                              headers={"Origin": "http://evil.com"}).status_code == 405


class TestOriginSchemeAwarePort:
    """M2 回归：Host 侧缺省端口按请求 scheme 取，不再一律按 80。

    旧实现 Origin 侧 https 缺省按 443、Host 侧缺省一律按 80——https/
    反代部署下（Host 不带端口）所有非 GET 同源请求被误杀 403。
    """

    @pytest.fixture
    def https_client(self, monkeypatch):
        monkeypatch.delenv("WEB_HOST", raising=False)
        return TestClient(_build_app(), base_url="https://testserver")

    def test_post_https_same_origin_without_port_passes(self, https_client):
        """https 请求 + 无端口 Host + https 无端口 Origin 同源 → 放行。"""
        r = https_client.post("/ping", headers={"Origin": "https://testserver"})
        assert r.status_code == 200, r.text

    def test_post_https_origin_port_mismatch_rejected(self, https_client):
        """https 同 host 但端口不同（9999 ≠ 缺省 443）→ 仍拒。"""
        assert https_client.post(
            "/ping", headers={"Origin": "https://testserver:9999"}
        ).status_code == 403

    def test_post_https_cross_scheme_http_origin_rejected(self, https_client):
        """https 请求带 http Origin（80 ≠ 443）→ 拒（scheme 感知比对）。"""
        assert https_client.post(
            "/ping", headers={"Origin": "http://testserver"}
        ).status_code == 403

    def test_post_https_foreign_origin_rejected(self, https_client):
        assert https_client.post(
            "/ping", headers={"Origin": "https://evil.com"}
        ).status_code == 403
