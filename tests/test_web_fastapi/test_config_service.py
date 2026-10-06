"""config_service 测试：读写 + 掩码 + 热更新分类。"""
import pytest
from web_fastapi.services.config_service import (
    mask_secret, classify_keys, is_hot_reloadable,
)


def test_mask_secret_partial():
    # "sk-abcdef1234567890" 长度19，保留前4字符 "sk-a"，其余15个 *
    assert mask_secret("sk-abcdef1234567890") == "sk-a" + "*" * 15
    assert mask_secret("") == ""


def test_mask_secret_full_hidden_when_short():
    assert mask_secret("ab") == "**"


def test_classify_keys():
    system, personal = classify_keys()
    assert "openai_api_key" in system
    assert "workspace_root" in personal
    assert "temperature" in personal
    # M0：qdrant/embedding 已移除，不再是 system key
    assert "qdrant_host" not in system
    assert "embedding_model" not in system
    assert "shell_enabled" in system


def test_is_hot_reloadable():
    assert is_hot_reloadable("workspace_root") is True
    assert is_hot_reloadable("temperature") is True
    assert is_hot_reloadable("openai_api_key") is False
    assert is_hot_reloadable("shell_enabled") is False
