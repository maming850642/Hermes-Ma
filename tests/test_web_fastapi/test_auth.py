"""认证路由测试（免认证形态，ADR-0005 D5）。

login/logout 与签名 cookie 已随认证整体移除；保留的对外语义：
- GET /api/auth/me 公开可达且恒返 LOCAL_USER（全站前端探测点兼容垫片）
- X-User-Id 等请求头依旧不是身份来源
- /api/auth/switch 保持 404（历史已删端点不复活）
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.constants import LOCAL_USER
from web_fastapi.routers import auth as auth_router


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(auth_router.router, prefix="/api/auth", tags=["auth"])
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


# ============================================
# /me：公开可达，恒返本地单用户
# ============================================
def test_me_public_and_always_local(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 200, r.text
    assert r.json() == {"user_id": LOCAL_USER}


def test_me_ignores_cookies_and_headers(client):
    """携带任意 cookie / X-User-Id 头都不改变身份——头部不是身份来源。"""
    client.cookies.set("session", "tampered-value")
    r = client.get("/api/auth/me", headers={"X-User-Id": "someone"})
    assert r.status_code == 200
    assert r.json()["user_id"] == LOCAL_USER


# ============================================
# 已删除端点保持删除状态
# ============================================
def test_login_endpoint_removed(client):
    r = client.post("/api/auth/login", json={})
    assert r.status_code == 404


def test_logout_endpoint_removed(client):
    r = client.post("/api/auth/logout")
    assert r.status_code == 404


def test_switch_endpoint_removed(client):
    """单用户坍缩：/api/auth/switch 早已删除 → 404（免认证下同样如此）。"""
    r = client.post("/api/auth/switch", json={"user_id": "bob"})
    assert r.status_code == 404
