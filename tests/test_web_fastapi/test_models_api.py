"""
模型档案 API（/api/models）测试。

不启动完整 create_app（那会 fork worker 子进程），而是构造一个仅挂
models router 的精简 FastAPI app，用 fastapi.testclient.TestClient 打。

覆盖：全端点 CRUD + 校验 400/404 + api_key 永不出现在任何响应体 +
test 端点 mock httpx（成功 / 401 / 超时 / 无 key 不带头）。
数据根指向 tmp（paths.set_data_root），不碰真实 data/。
"""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.model_registry import resolve_profile
from src.storage import paths
from web_fastapi.routers import models as models_router

KEY = "sk-secret-key-0123456789abcdef"


@pytest.fixture(autouse=True)
def _isolate_data_root(tmp_path):
    """每个用例的数据根都指向独立 tmp，绝不碰真实 data/。"""
    paths.set_data_root(tmp_path)
    yield tmp_path
    paths.set_data_root(None)


@pytest.fixture
def client():
    a = FastAPI()
    a.include_router(models_router.router, prefix="/api/models", tags=["models"])
    return TestClient(a)


def _create(client, **kw):
    body = {"display": "GPT-4o", "model": "gpt-4o",
            "base_url": "https://api.example.com/v1", "api_key": KEY}
    body.update(kw)
    r = client.post("/api/models", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ============================================
# POST / GET
# ============================================
def test_create_201_masked(client):
    j = _create(client)
    assert j["id"] == "gpt-4o"
    assert j["display"] == "GPT-4o"
    assert j["model"] == "gpt-4o"
    assert j["base_url"] == "https://api.example.com/v1"
    assert j["has_key"] is True
    assert j["context_window"] is None
    assert j["created_at"]
    assert "api_key" not in j  # 绝不回显


def test_create_empty_key_has_key_false(client):
    j = _create(client, api_key="")
    assert j["has_key"] is False


def test_create_missing_required_400(client):
    for body in ({}, {"display": "x"}, {"display": "x", "model": "m"}):
        r = client.post("/api/models", json=body)
        assert r.status_code == 400, r.text


def test_create_invalid_id_400(client):
    r = client.post("/api/models", json={"display": "x", "model": "m",
                                         "base_url": "https://x/v1",
                                         "id": "Bad Id"})
    assert r.status_code == 400


def test_create_duplicate_id_400(client):
    _create(client, id="dup")
    r = client.post("/api/models", json={"display": "y", "model": "m",
                                         "base_url": "https://x/v1", "id": "dup"})
    assert r.status_code == 400


def test_list_returns_masked_array(client):
    _create(client)
    _create(client, display="Local", model="qwen", base_url="http://127.0.0.1:11434/v1",
            api_key="")
    r = client.get("/api/models")
    assert r.status_code == 200
    arr = r.json()
    assert len(arr) == 2
    for item in arr:
        assert set(item) == {"id", "display", "model", "base_url",
                             "context_window", "has_key", "created_at"}
        assert "api_key" not in item
    assert [p["has_key"] for p in arr] == [True, False]


# ============================================
# PUT
# ============================================
def test_put_updates_fields(client):
    _create(client, id="p1")
    r = client.put("/api/models/p1", json={"display": "新名字", "model": "m2",
                                           "context_window": 16000})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["display"] == "新名字"
    assert j["model"] == "m2"
    assert j["context_window"] == 16000


def test_put_keeps_key_when_absent_or_empty(client):
    _create(client, id="p1")
    assert client.put("/api/models/p1", json={"display": "x1"}).status_code == 200
    assert resolve_profile("p1")["api_key"] == KEY  # 缺省保持
    assert client.put("/api/models/p1", json={"api_key": ""}).status_code == 200
    assert resolve_profile("p1")["api_key"] == KEY  # 空串保持
    # 传新值才替换
    assert client.put("/api/models/p1", json={"api_key": "sk-brand-new"}).status_code == 200
    assert resolve_profile("p1")["api_key"] == "sk-brand-new"


def test_put_null_context_window_clears(client):
    _create(client, id="p1", context_window=8000)
    assert client.put("/api/models/p1", json={"context_window": None}).status_code == 200
    assert resolve_profile("p1")["context_window"] is None


def test_put_404_unknown(client):
    r = client.put("/api/models/nope", json={"display": "x"})
    assert r.status_code == 404


def test_put_bad_field_400(client):
    _create(client, id="p1")
    r = client.put("/api/models/p1", json={"display": ""})
    assert r.status_code == 400


# ============================================
# DELETE
# ============================================
def test_delete_returns_deleted_profile(client):
    _create(client, id="p1")
    r = client.delete("/api/models/p1")
    assert r.status_code == 200
    j = r.json()
    assert j["id"] == "p1"
    assert "api_key" not in j  # 被删档案同样掩码
    assert client.get("/api/models").json() == []
    # 二次删 404
    assert client.delete("/api/models/p1").status_code == 404


def test_delete_404_unknown(client):
    assert client.delete("/api/models/nope").status_code == 404


# ============================================
# api_key 永不出现在任何响应体
# ============================================
def test_api_key_never_in_any_response_body(client, monkeypatch):
    """带 key 建档后遍历全部端点，断言响应文本不含 key 原文。

    test 端点带 confirm_send_key=true（UI 提示"将使用已存 Key 测试"后
    显式携带）——key 只出现在请求头，绝不进 URL/响应体。"""
    _create(client, id="p1")

    captured = {}

    def _fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers or {}
        return httpx.Response(401, json={"error": "bad key"},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(models_router.httpx, "get", _fake_get)

    responses = [
        client.post("/api/models", json={"display": "second", "model": "m",
                                         "base_url": "https://x/v1"}),
        client.get("/api/models"),
        client.put("/api/models/p1", json={"display": "改名"}),
        client.post("/api/models/p1/test?confirm_send_key=true"),
        client.delete("/api/models/p1"),
    ]
    for r in responses:
        assert KEY not in r.text, f"key 泄漏于 {r.request.method} {r.request.url}"
    # test 端点确实把 key 带在了请求头（而非 URL/响应）
    assert captured["headers"].get("Authorization") == f"Bearer {KEY}"
    assert KEY not in captured["url"]


# ============================================
# POST /{id}/test（mock httpx）
# ============================================
def _mock(client, monkeypatch, status=200, payload=None, exc=None):
    captured = {}

    def _fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers or {}
        captured["timeout"] = timeout
        if exc is not None:
            raise exc
        return httpx.Response(status, json=payload if payload is not None else {},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(models_router.httpx, "get", _fake_get)
    return captured


def test_test_endpoint_ok_lists_first_10_models(client, monkeypatch):
    """confirm_send_key=true：带 Bearer 测鉴权连通（UI 提示后显式确认）。"""
    _create(client, id="p1")
    payload = {"data": [{"id": f"model-{i:02d}"} for i in range(15)]}
    captured = _mock(client, monkeypatch, status=200, payload=payload)
    r = client.post("/api/models/p1/test?confirm_send_key=true")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["status"] == 200
    assert j["detail"] == "连接成功"
    assert j["models"] == [f"model-{i:02d}" for i in range(10)]  # 只取前 10
    # URL / 鉴权头 / 超时
    assert captured["url"] == "https://api.example.com/v1/models"
    assert captured["headers"].get("Authorization") == f"Bearer {KEY}"
    assert captured["timeout"] == 10.0


def test_test_endpoint_default_sends_no_key(client, monkeypatch):
    """Fix4 key 转发面收口：默认只测连通性，绝不带 Authorization 头——
    档案 base_url 可被更新指向任意主机，无条件带 Bearer 等于把存档 key
    转发给"改个 URL"就能指定的任意服务器。"""
    _create(client, id="p1")
    captured = _mock(client, monkeypatch, status=200, payload={"data": [{"id": "m"}]})
    j = client.post("/api/models/p1/test").json()
    assert j["ok"] is True
    assert "Authorization" not in captured["headers"]


def test_test_endpoint_401(client, monkeypatch):
    _create(client, id="p1")
    _mock(client, monkeypatch, status=401)
    j = client.post("/api/models/p1/test").json()
    assert j["ok"] is False
    assert j["status"] == 401
    assert j["models"] == []
    assert j["detail"]


def test_test_endpoint_timeout(client, monkeypatch):
    _create(client, id="p1")
    _mock(client, monkeypatch, exc=httpx.TimeoutException("timed out"))
    j = client.post("/api/models/p1/test").json()
    assert j["ok"] is False
    assert j["status"] is None
    assert "超时" in j["detail"]
    assert j["models"] == []


def test_test_endpoint_network_error(client, monkeypatch):
    _create(client, id="p1")
    _mock(client, monkeypatch, exc=httpx.ConnectError("refused"))
    j = client.post("/api/models/p1/test").json()
    assert j["ok"] is False
    assert j["status"] is None
    assert j["detail"]
    assert j["models"] == []


def test_test_endpoint_no_key_sends_no_auth_header(client, monkeypatch):
    _create(client, id="p1", api_key="")
    captured = _mock(client, monkeypatch, status=200, payload={"data": [{"id": "m"}]})
    j = client.post("/api/models/p1/test").json()
    assert j["ok"] is True
    assert "Authorization" not in captured["headers"]


def test_test_endpoint_404_unknown(client):
    r = client.post("/api/models/nope/test")
    assert r.status_code == 404


# ============================================
# Fix4：base_url scheme 校验（http/https only）
# ============================================
def test_create_rejects_non_http_scheme(client):
    for bad in ("ftp://x/v1", "file:///etc/passwd", "gopher://x", "x/v1", ""):
        r = client.post("/api/models", json={"display": "x", "model": "m",
                                             "base_url": bad})
        assert r.status_code == 400, bad


def test_update_rejects_non_http_scheme(client):
    _create(client, id="p1")
    r = client.put("/api/models/p1", json={"base_url": "file:///etc/passwd"})
    assert r.status_code == 400


def test_test_endpoint_rejects_non_http_scheme(client, monkeypatch):
    """存量档案 base_url 非常规 scheme（手写 JSON 绕过建档校验）→ 400，
    绝不作为 httpx 请求目标发出。"""
    _create(client, id="p1")

    def _fail_get(*a, **kw):
        raise AssertionError("非法 scheme 不应发出请求")

    monkeypatch.setattr(models_router.httpx, "get", _fail_get)
    monkeypatch.setattr(models_router, "resolve_profile",
                        lambda pid: {"id": pid, "display": "d", "model": "m",
                                     "base_url": "file:///etc/passwd",
                                     "api_key": "", "context_window": None})
    r = client.post("/api/models/p1/test?confirm_send_key=true")
    assert r.status_code == 400


# ============================================
# Fix9：update_profile 对缺字段档案的 KeyError 兜底
# ============================================
def test_update_profile_missing_field_gives_400_not_500(client):
    """手写/旧版 JSON 缺 display 等字段：.get 双兜底 → 可读 400（而非 KeyError→500）。"""
    import json as json_mod
    from src.storage import paths as p

    home = p.agent_home("model_profiles.json")
    home.parent.mkdir(parents=True, exist_ok=True)
    home.write_text(json_mod.dumps(
        {"profiles": [{"id": "broken", "model": "m",
                       "base_url": "https://x/v1", "api_key": ""}]}),
        encoding="utf-8")

    # 改 model（不动 display）→ 旧值兜底读到 None → 校验给可读 400
    r = client.put("/api/models/broken", json={"model": "m2"})
    assert r.status_code == 400
    assert "显示名" in r.json()["detail"]
