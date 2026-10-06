"""安全校验回归测试。

覆盖 web_fastapi.security 的 user_id / session_id 校验，防御路径穿越。
"""
import pytest
from web_fastapi.security import validate_user_id, validate_id


class TestValidateUserId:
    def test_plain_ascii_accepted(self):
        assert validate_user_id("alice") == "alice"

    def test_chinese_accepted(self):
        # 中文 user_id 应被允许（项目支持中文用户名）
        assert validate_user_id("张三") == "张三"

    def test_strips_whitespace(self):
        assert validate_user_id("  alice  ") == "alice"

    def test_dot_inside_accepted(self):
        # 中间的点（如 alice.bob）合法，仅拒绝路径语义危险的形态
        assert validate_user_id("alice.bob") == "alice.bob"

    def test_rejects_dotdot(self):
        with pytest.raises(Exception):
            validate_user_id("..")

    def test_rejects_dotdot_segment(self):
        with pytest.raises(Exception):
            validate_user_id("a/../../b")

    def test_rejects_slash(self):
        with pytest.raises(Exception):
            validate_user_id("a/b")

    def test_rejects_backslash(self):
        with pytest.raises(Exception):
            validate_user_id("a\\b")

    def test_rejects_leading_dot(self):
        with pytest.raises(Exception):
            validate_user_id(".hidden")

    def test_rejects_empty(self):
        with pytest.raises(Exception):
            validate_user_id("")

    def test_rejects_null_byte(self):
        with pytest.raises(Exception):
            validate_user_id("a\x00b")


class TestValidateId:
    def test_session_id_accepted(self):
        assert validate_id("sess-123", "会话 ID") == "sess-123"

    def test_rejects_traversal(self):
        with pytest.raises(Exception):
            validate_id("../../etc/passwd", "会话 ID")
