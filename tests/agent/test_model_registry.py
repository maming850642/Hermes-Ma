"""
模型档案注册表（src/model_registry.py）测试。

覆盖：CRUD 全路径 / id 校验与自动生成去重 / 原子写失败保旧 /
update 不带 key 保持原值 / resolve 缺失返回 None。
数据根统一指向 tmp（paths.set_data_root），不碰真实 data/。
"""
import json

import pytest

import src.model_registry as mr
from src.model_registry import (
    API_KEY_UNCHANGED,
    ModelProfileError,
    add_profile,
    delete_profile,
    get_profile,
    list_profiles,
    resolve_profile,
    slug_from_display,
    update_profile,
    validate_profile_id,
)
from src.storage import paths


@pytest.fixture(autouse=True)
def _isolate_data_root(tmp_path):
    """每个用例的数据根都指向独立 tmp，绝不碰真实 data/。"""
    paths.set_data_root(tmp_path)
    yield tmp_path
    paths.set_data_root(None)


def _file(tmp_path):
    return tmp_path / "home" / "model_profiles.json"


def _add(**kw):
    base = dict(display="GPT-4o", model="gpt-4o",
                base_url="https://api.example.com/v1")
    base.update(kw)
    return add_profile(**base)


# ============================================
# validate_profile_id / slug
# ============================================
def test_validate_profile_id_ok():
    assert validate_profile_id("a") == "a"
    assert validate_profile_id("gpt-4o_2") == "gpt-4o_2"
    assert validate_profile_id("  x9  ") == "x9"  # 首尾空白剥离


@pytest.mark.parametrize("bad", ["", "   ", "Bad", "has space", "中文",
                                 "-lead", "_lead", "a/b", "a:b", "x" * 33])
def test_validate_profile_id_bad(bad):
    with pytest.raises(ModelProfileError):
        validate_profile_id(bad)


def test_slug_from_display_ascii():
    assert slug_from_display("My Model!") == "my-model"
    assert slug_from_display("deepseek-chat v3") == "deepseek-chat-v3"


def test_slug_from_display_non_ascii_falls_back_to_random():
    s = slug_from_display("通义千问")
    assert s.startswith("mp-")
    assert validate_profile_id(s) == s  # 生成物本身必合法


def test_slug_long_display_truncated_to_32():
    s = slug_from_display("a" * 50)
    assert len(s) <= 32


# ============================================
# add / get / list
# ============================================
def test_add_and_get_roundtrip(tmp_path):
    p = _add(api_key="sk-abc", context_window=128000)
    assert p["id"] == "gpt-4o"  # 从 display 推导
    assert p["api_key"] == "sk-abc"
    assert p["context_window"] == 128000
    assert p["created_at"] == p["updated_at"]
    # 落盘位置 = data/home/model_profiles.json
    assert _file(tmp_path).exists()
    got = get_profile("gpt-4o")
    assert got is not None and got["api_key"] == "sk-abc"


def test_add_minimal_no_key(tmp_path):
    p = _add(display="local", model="qwen2.5", base_url="http://127.0.0.1:11434/v1")
    assert p["api_key"] == ""
    assert p["context_window"] is None  # 空 = 跟随全局


def test_add_explicit_id_and_duplicate(tmp_path):
    _add(profile_id="mine")
    with pytest.raises(ModelProfileError, match="已存在"):
        _add(profile_id="mine")


def test_add_invalid_explicit_id(tmp_path):
    with pytest.raises(ModelProfileError):
        _add(profile_id="Bad Id")


def test_add_required_fields(tmp_path):
    for kw in ({"display": ""}, {"display": "  "}, {"model": ""},
               {"base_url": ""}):
        with pytest.raises(ModelProfileError):
            _add(**kw)


def test_add_context_window_validation(tmp_path):
    with pytest.raises(ModelProfileError):
        _add(context_window="abc")
    with pytest.raises(ModelProfileError):
        _add(context_window=0)
    with pytest.raises(ModelProfileError):
        _add(context_window=-5)
    # 空串显式归一为 None（跟随全局）
    assert _add(context_window="")["context_window"] is None


def test_list_order_and_copy(tmp_path):
    _add(display="aaa", model="m")
    _add(display="bbb", model="m")
    lst = list_profiles()
    assert [p["display"] for p in lst] == ["aaa", "bbb"]
    # 返回副本：改动不落盘
    lst[0]["api_key"] = "tampered"
    assert get_profile("aaa")["api_key"] == ""


def test_get_missing_returns_none(tmp_path):
    assert get_profile("nope") is None


# ============================================
# update
# ============================================
def test_update_fields(tmp_path):
    _add(profile_id="p1", context_window=8000)
    p = update_profile("p1", {"display": "新名字", "model": "m2",
                              "base_url": "https://new/v1",
                              "context_window": 16000})
    assert p["display"] == "新名字"
    assert p["model"] == "m2"
    assert p["base_url"] == "https://new/v1"
    assert p["context_window"] == 16000
    assert p["updated_at"] >= p["created_at"]
    # 只改传入字段：未动的保持原值
    p2 = update_profile("p1", {"model": "m3"})
    assert p2["display"] == "新名字"
    # 落盘确认
    assert get_profile("p1")["model"] == "m3"


def test_update_context_window_clear_to_none(tmp_path):
    _add(profile_id="p1", context_window=8000)
    p = update_profile("p1", {"context_window": None})
    assert p["context_window"] is None


def test_update_keeps_key_without_new_key(tmp_path):
    """update 不带 key（哨兵/None/空串）一律保持原值。"""
    _add(profile_id="p1", api_key="sk-keep-me")
    # 缺省哨兵
    update_profile("p1", {"display": "x1"})
    assert get_profile("p1")["api_key"] == "sk-keep-me"
    # 显式传哨兵
    update_profile("p1", {"display": "x2"}, api_key=API_KEY_UNCHANGED)
    assert get_profile("p1")["api_key"] == "sk-keep-me"
    # None / 空串
    update_profile("p1", {"display": "x3"}, api_key=None)
    assert get_profile("p1")["api_key"] == "sk-keep-me"
    update_profile("p1", {"display": "x4"}, api_key="")
    assert get_profile("p1")["api_key"] == "sk-keep-me"
    # 传新值才替换
    update_profile("p1", {}, api_key="sk-new")
    assert get_profile("p1")["api_key"] == "sk-new"


def test_update_changes_cannot_carry_api_key(tmp_path):
    _add(profile_id="p1", api_key="sk-old")
    with pytest.raises(ModelProfileError, match="api_key"):
        update_profile("p1", {"api_key": "sk-sneak"})


def test_update_validation_and_missing(tmp_path):
    _add(profile_id="p1")
    with pytest.raises(ModelProfileError):
        update_profile("p1", {"display": ""})
    with pytest.raises(ModelProfileError):
        update_profile("p1", {"context_window": 0})
    assert update_profile("nope", {"model": "m"}) is None
    with pytest.raises(ModelProfileError):
        update_profile("BAD ID", {"model": "m"})


# ============================================
# delete / resolve
# ============================================
def test_delete_returns_profile(tmp_path):
    _add(profile_id="p1", api_key="sk-doomed")
    removed = delete_profile("p1")
    assert removed["id"] == "p1"
    assert removed["api_key"] == "sk-doomed"  # 调用方凭它做回退
    assert get_profile("p1") is None
    assert delete_profile("p1") is None  # 二次删
    assert list_profiles() == []


def test_resolve_returns_full_tuple(tmp_path):
    _add(profile_id="p1", model="m1", base_url="https://x/v1", api_key="sk-r")
    p = resolve_profile("p1")
    assert (p["model"], p["base_url"], p["api_key"]) == ("m1", "https://x/v1", "sk-r")


def test_resolve_missing_returns_none(tmp_path):
    assert resolve_profile("nope") is None
    assert resolve_profile("BAD ID") is None
    assert resolve_profile("") is None


# ============================================
# 原子写
# ============================================
def test_atomic_write_is_json_document(tmp_path):
    _add(profile_id="p1", display="名字")  # 非 ASCII 落盘不被转义
    raw = _file(tmp_path).read_text(encoding="utf-8")
    data = json.loads(raw)
    assert [p["id"] for p in data["profiles"]] == ["p1"]
    assert "名字" in raw


def test_write_failure_keeps_old_file(tmp_path, monkeypatch):
    """写失败（rename 阶段抛错）时旧文件原样保留，且无 tmp 残留。"""
    _add(profile_id="p1", api_key="sk-old")
    before = _file(tmp_path).read_text(encoding="utf-8")

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(mr.os, "replace", _boom)
    with pytest.raises(OSError):
        _add(profile_id="p2")
    monkeypatch.undo()

    assert _file(tmp_path).read_text(encoding="utf-8") == before
    assert [p["id"] for p in json.loads(before)["profiles"]] == ["p1"]
    # finally 清理：无 .tmp 残留
    assert list(tmp_path.glob("home/*.tmp")) == []


def test_load_corrupt_file_returns_empty(tmp_path):
    _file(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _file(tmp_path).write_text("{not json", encoding="utf-8")
    assert list_profiles() == []
    assert get_profile("p1") is None
