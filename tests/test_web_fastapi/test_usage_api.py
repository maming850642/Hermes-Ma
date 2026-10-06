"""用量看板 API（/api/usage/summary + /api/usage/records + records/{id}/detail，
ADR-0005 D2）行为锁定。

仿 test_clone_runs_api.py / test_system_api.py：不启完整 create_app
（那会 boot 组合根 + fork worker），只挂 usage router 的精简 FastAPI +
TestClient；SQLiteProvider 指向 tmp 库经 _StubCtx 注入，UsageStore 造数。

- summary：本地日 × scope 聚合（含 error 计数）+ 窗口合计
- days 白名单 1/7/30，其余（含非数字）clamp 回 7
- records：ts 倒序分页、page/page_size clamp、字段白名单（带 id 供 👀
  详情拉取；不带大字段与 error 全文）
- records/{id}/detail：单条全文（详情三列 + error）；不存在 → 404
- 空库形状 + 无 cordis_ctx 回退默认 provider（数据根改道 tmp 隔离）
"""
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.storage import paths
from src.storage import usage_store as usage_store_mod
from src.storage.sqlite_provider import SQLiteProvider
from src.storage.usage_store import UsageStore
from web_fastapi.dependencies import get_current_user_id
from web_fastapi.routers import usage as usage_router

NOW = time.time()
DAY = 86400


class _StubCtx:
    def __init__(self, storage):
        self._s = storage

    def try_get(self, name):
        return self._s if name == "storage" else None


def _make_app(provider=None):
    a = FastAPI()
    if provider is not None:
        a.state.cordis_ctx = _StubCtx(provider)
    a.include_router(usage_router.router, prefix="/api/usage", tags=["usage"])
    a.dependency_overrides[get_current_user_id] = lambda: "local"
    return a


@pytest.fixture
def api(tmp_path):
    paths.set_data_root(tmp_path / "data")
    provider = SQLiteProvider(db_path=tmp_path / "data" / "p.db")
    a = _make_app(provider)
    yield {"client": TestClient(a), "provider": provider,
           "store": UsageStore(provider)}
    provider.close()
    paths.set_data_root(None)


def _seed_usage(store):
    """今天 3 行（含 1 error、1 tokens NULL）+ 前天 1 行（窗口外样本）。"""
    store.record(ts=NOW - 10, session_id="s1", scope="chat", caller="main",
                 model="m1", tokens_in=10, tokens_out=20)
    store.record(ts=NOW - 5, session_id="s1", scope="chat", caller="main",
                 model="m1", tokens_in=5, tokens_out=7, status="error", error="boom")
    store.record(ts=NOW - 1, session_id="s2", scope="waker", caller="employee",
                 model="m2")  # tokens NULL：计次不计 token
    store.record(ts=NOW - 2 * DAY, session_id="s3", scope="chat", caller="main",
                 model="m1", tokens_in=1, tokens_out=2)


# ============================================
# /api/usage/summary
# ============================================
def test_summary_aggregates_days_scope_and_errors(api):
    _seed_usage(api["store"])
    j = api["client"].get("/api/usage/summary?days=7").json()
    assert j["days"] == 7
    # 窗口合计 = 聚合行汇总：10+5+1 / 20+7+2 / 4 次 / 1 错
    assert j["totals"] == {"tokens_in": 16, "tokens_out": 29, "calls": 4, "errors": 1}
    today = time.strftime("%Y-%m-%d", time.localtime(NOW))
    old = time.strftime("%Y-%m-%d", time.localtime(NOW - 2 * DAY))
    by = {(r["day"], r["scope"]): r for r in j["rows"]}
    assert len(j["rows"]) == 3
    # error 行计入 calls 也计入 errors；NULL tokens 行计次不计 token
    assert by[(today, "chat")]["tokens_in"] == 15
    assert by[(today, "chat")]["tokens_out"] == 27
    assert by[(today, "chat")]["calls"] == 2
    assert by[(today, "chat")]["errors"] == 1
    assert by[(today, "waker")]["calls"] == 1
    assert by[(today, "waker")]["tokens_in"] == 0
    assert by[(old, "chat")]["calls"] == 1


def test_summary_days_whitelist_clamps_to_7(api):
    _seed_usage(api["store"])
    full = api["client"].get("/api/usage/summary?days=7").json()
    # 非白名单档位（3 / 0 / -5 / banana）一律 clamp 回 7，结果与 days=7 相同
    for raw in ("3", "0", "-5", "banana"):
        j = api["client"].get(f"/api/usage/summary?days={raw}").json()
        assert j["days"] == 7
        assert j["totals"] == full["totals"]
    # 白名单档位原样生效：今天不含前天那行
    j1 = api["client"].get("/api/usage/summary?days=1").json()
    assert j1["days"] == 1
    assert j1["totals"] == {"tokens_in": 15, "tokens_out": 27, "calls": 3, "errors": 1}
    j30 = api["client"].get("/api/usage/summary?days=30").json()
    assert j30["days"] == 30
    assert j30["totals"]["calls"] == 4


def test_summary_empty_shape(api):
    j = api["client"].get("/api/usage/summary").json()   # 缺省 days=7
    assert j["days"] == 7
    assert j["rows"] == []
    assert j["totals"] == {"tokens_in": 0, "tokens_out": 0, "calls": 0, "errors": 0}


def test_summary_fallback_default_provider_redirects_to_tmp(tmp_path):
    """无 cordis_ctx → 回退默认 provider；数据根改道 tmp 不碰真实 data/。
    模式同 test_system_api 的回退用例；结束后 reset 默认连接防串库。"""
    paths.set_data_root(tmp_path / "data")
    try:
        a = _make_app()   # 不注入 cordis_ctx
        j = TestClient(a).get("/api/usage/summary?days=7").json()
        assert j["rows"] == []
        assert j["totals"]["calls"] == 0
    finally:
        usage_store_mod.reset_default_provider()
        paths.set_data_root(None)


# ============================================
# /api/usage/records
# ============================================
def test_records_pagination_and_field_whitelist(api):
    store = api["store"]
    for i in range(25):
        store.record(ts=NOW - i * 60, session_id=f"s{i}", scope="chat",
                     caller="main", model="m1", tokens_in=i, tokens_out=i * 2,
                     status="error" if i == 0 else "ok",
                     error="secret-boom" if i == 0 else "")
    c = api["client"]
    j = c.get("/api/usage/records").json()   # 缺省 page=1 / page_size=20
    assert j["total"] == 25 and j["page"] == 1 and j["page_size"] == 20
    assert len(j["records"]) == 20
    assert j["records"][0]["session_id"] == "s0"     # ts 倒序，最新在前
    j2 = c.get("/api/usage/records?page=2").json()
    assert len(j2["records"]) == 5
    assert j2["records"][0]["session_id"] == "s20"
    # 字段白名单：带 id（👀 详情按它取全文），不带 error 全文与大字段
    assert set(j["records"][0]) == {"id", "ts", "session_id", "scope", "caller",
                                    "model", "tools", "tokens_in", "tokens_out",
                                    "duration_ms", "status"}


def test_records_param_clamp_and_out_of_range(api):
    store = api["store"]
    for i in range(3):
        store.record(ts=NOW - i, session_id=f"s{i}", scope="chat")
    c = api["client"]
    j = c.get("/api/usage/records?page=0&page_size=500").json()
    assert j["page"] == 1 and j["page_size"] == 100   # page>=1；size clamp 1..100
    assert len(j["records"]) == 3
    j2 = c.get("/api/usage/records?page=99").json()   # 越界页：空列表但 total 真实
    assert j2["records"] == [] and j2["total"] == 3
    j3 = c.get("/api/usage/records?page_size=0").json()
    assert j3["page_size"] == 1 and len(j3["records"]) == 1


def test_records_empty_shape(api):
    r = api["client"].get("/api/usage/records").json()
    assert r == {"records": [], "total": 0, "page": 1, "page_size": 20}


# ============================================
# /api/usage/records/{id}/detail
# ============================================
def test_record_detail_returns_full_fields(api):
    store = api["store"]
    store.record(ts=NOW, session_id="s1", scope="chat", caller="main",
                 model="m1", tokens_in=10, tokens_out=20,
                 req_messages='[{"role": "user", "content": "你好"}]',
                 reasoning="想一想", output="回答")
    rec_id = store.records(page=1, page_size=1)["items"][0]["id"]
    r = api["client"].get(f"/api/usage/records/{rec_id}/detail")
    assert r.status_code == 200
    rec = r.json()["record"]
    assert rec["id"] == rec_id
    assert rec["req_messages"] == '[{"role": "user", "content": "你好"}]'
    assert rec["reasoning"] == "想一想"
    assert rec["output"] == "回答"


def test_record_detail_404_for_missing_id(api):
    r = api["client"].get("/api/usage/records/424242/detail")
    assert r.status_code == 404


def test_summary_models_list_and_model_filter(api):
    """summary 响应恒带未过滤 models 列表（筛选下拉数据源）；model 参数
    过滤 rows/totals。"""
    _seed_usage(api["store"])   # m1×3 + m2×1
    j = api["client"].get("/api/usage/summary?days=7").json()
    assert j["models"] == ["m1", "m2"]
    assert j["totals"]["calls"] == 4

    jf = api["client"].get("/api/usage/summary?days=7&model=m1").json()
    assert jf["models"] == ["m1", "m2"]          # 列表不受筛选影响
    assert jf["totals"]["calls"] == 3
    assert all(r["model"] == "m1" for r in jf["rows"])


def test_records_model_filter(api):
    _seed_usage(api["store"])
    j = api["client"].get("/api/usage/records?model=m2").json()
    assert j["total"] == 1
    assert j["records"][0]["model"] == "m2"
    assert api["client"].get("/api/usage/records").json()["total"] == 4


def test_record_detail_has_detail_flag(api):
    """has_detail 区分「旧数据无留底」与「有留底」两种空 output 形态。"""
    store = api["store"]
    store.record(ts=NOW, session_id="s1", output="回答")
    store.record(ts=NOW, session_id="s2")        # 旧行形状：三列全空
    items = store.records(page=1, page_size=5)["items"]
    by_sid = {r["session_id"]: r["id"] for r in items}
    d1 = api["client"].get(
        f"/api/usage/records/{by_sid['s1']}/detail").json()["record"]
    d2 = api["client"].get(
        f"/api/usage/records/{by_sid['s2']}/detail").json()["record"]
    assert d1["has_detail"] is True
    assert d2["has_detail"] is False
