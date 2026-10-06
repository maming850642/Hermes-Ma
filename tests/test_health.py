"""src/health.check_llm_api 状态码语义回归。

背景（R2 review）：此前非 401 一律 return True——404/500/502 全部静默
PASS，与 run_health.py 的 WARN 矩阵不一致。

修复契约（对齐 run_health 语义）：
    200            → PASS（True）
    401            → FAIL（抛 ValueError）
    其余非 200     → WARN（带状态码 warning 日志，返回 True 不阻塞启动；
                     消费方 run_health_check / cli / web_fastapi 的 bool
                     契约不变：True=继续，异常=FAIL）
    空 key         → WARN 跳过（F3，维持 True）
    ConnectError/Timeout → FAIL（ConnectionError）
"""
import logging
from types import SimpleNamespace

import httpx
import pytest

import src.health as health_mod
from src.health import check_llm_api


def _settings(key="sk-test", base="http://unit.test/v1"):
    s = SimpleNamespace(openai_api_key=key, openai_base_url=base)
    return s


def _patch_env(monkeypatch, key="sk-test", base="http://unit.test/v1"):
    monkeypatch.setattr(health_mod, "get_settings", lambda: _settings(key, base))


def test_status_200_passes(monkeypatch):
    """200 → PASS（True），无告警。"""
    _patch_env(monkeypatch)
    monkeypatch.setattr(health_mod.httpx, "get",
                        lambda *a, **k: SimpleNamespace(status_code=200))
    assert check_llm_api() is True


def test_status_401_fails(monkeypatch):
    """401 → FAIL：抛 ValueError（run_health_check 捕获后记 FAIL）。"""
    _patch_env(monkeypatch)
    monkeypatch.setattr(health_mod.httpx, "get",
                        lambda *a, **k: SimpleNamespace(status_code=401))
    with pytest.raises(ValueError, match="401"):
        check_llm_api()


@pytest.mark.parametrize("status", [404, 500, 502, 503])
def test_other_non_200_warns_but_does_not_block(monkeypatch, caplog, status):
    """其余非 200 → WARN（带状态码日志），返回 True 不阻塞启动。

    回归核心：此前 404/5xx 一律静默 True，与 run_health.py 的
    "WARN - 状态码 xxx" 矩阵不一致，端点异常被当 PASS 掩盖。
    """
    _patch_env(monkeypatch)
    monkeypatch.setattr(health_mod.httpx, "get",
                        lambda *a, **k: SimpleNamespace(status_code=status))
    with caplog.at_level(logging.WARNING, logger="hermes.health"):
        result = check_llm_api()
    assert result is True, "WARN 语义 = 不阻塞启动（True）"
    assert any(status == r.args[0] if r.args else str(status) in r.getMessage()
               for r in caplog.records), f"状态码 {status} 必须出现在 WARN 日志中"
    assert "WARN" in caplog.text


def test_empty_key_skips_auth_check(monkeypatch, caplog):
    """空 key → WARN 跳过（F3 语义，维持 True，不发起请求）。"""
    _patch_env(monkeypatch, key="")

    def _fail(*a, **k):
        raise AssertionError("空 key 不应发起 HTTP 请求")

    monkeypatch.setattr(health_mod.httpx, "get", _fail)
    with caplog.at_level(logging.WARNING, logger="hermes.health"):
        assert check_llm_api() is True
    assert "跳过" in caplog.text


def test_connect_error_raises_connection_error(monkeypatch):
    _patch_env(monkeypatch)

    def _raise(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(health_mod.httpx, "get", _raise)
    with pytest.raises(ConnectionError, match="unreachable"):
        check_llm_api()


def test_timeout_raises_connection_error(monkeypatch):
    _patch_env(monkeypatch)

    def _raise(*a, **k):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(health_mod.httpx, "get", _raise)
    with pytest.raises(ConnectionError, match="timeout"):
        check_llm_api()


def test_run_health_check_401_marks_fail(monkeypatch):
    """消费方核对：run_health_check 对 401（ValueError）记 FAIL 且返回 False。"""
    _patch_env(monkeypatch)
    monkeypatch.setattr(health_mod.httpx, "get",
                        lambda *a, **k: SimpleNamespace(status_code=401))
    assert health_mod.run_health_check(silent=True) is False


def test_run_health_check_warn_still_ok(monkeypatch):
    """消费方核对：非 200 WARN 路径下 run_health_check 返回 True（不阻塞）。"""
    _patch_env(monkeypatch)
    monkeypatch.setattr(health_mod.httpx, "get",
                        lambda *a, **k: SimpleNamespace(status_code=503))
    assert health_mod.run_health_check(silent=True) is True
