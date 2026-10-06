"""virtual_fs contextvar 注入测试。

验证：Web 通过 set_current_vfs 注入独立 dict；
CLI 不调 set_current_vfs 时回退到模块级全局 _virtual_fs（行为零变化）。
"""
import contextvars

# 哨兵导入：先完整加载 src.agent（历史循环导入兜底，T7 后链条已断，保留无害）
import src.agent  # noqa: F401
from src.tools.virtual_fs import (
    get_virtual_fs, set_current_vfs, reset_virtual_fs, _current_vfs,
)


def test_cli_fallback_uses_global_when_no_contextvar_set():
    """CLI 模式：未注入 contextvar → 返回模块全局 _virtual_fs。"""
    ctx = contextvars.copy_context()
    def _run():
        reset_virtual_fs()
        fs = get_virtual_fs()
        fs["/cli.txt"] = "hello"
        assert get_virtual_fs()["/cli.txt"] == "hello"
    ctx.run(_run)


def test_web_injection_isolates_per_context():
    """Web 模式：注入 contextvar → 返回注入的 dict，与全局隔离。"""
    ctx = contextvars.copy_context()
    def _run():
        reset_virtual_fs()
        global_fs = get_virtual_fs()
        global_fs["/global.txt"] = "G"

        # 模拟 Web 请求：注入独立 vfs
        user_vfs = {"/user.txt": "U"}
        token = set_current_vfs(user_vfs)

        assert get_virtual_fs() is user_vfs
        assert get_virtual_fs()["/user.txt"] == "U"
        # 全局未被污染
        assert "/global.txt" in global_fs
        assert "/user.txt" not in global_fs

        # reset 后回退全局
        _current_vfs.reset(token)
        assert "/global.txt" in get_virtual_fs()
    ctx.run(_run)


def test_two_users_isolated():
    """两个 context 各自注入，互不干扰。"""
    def user_a():
        a_fs = {}
        set_current_vfs(a_fs)
        a_fs["/a.txt"] = "A"
        assert get_virtual_fs() is a_fs
        assert "/b.txt" not in get_virtual_fs()

    def user_b():
        b_fs = {}
        set_current_vfs(b_fs)
        b_fs["/b.txt"] = "B"
        assert get_virtual_fs() is b_fs
        assert "/a.txt" not in get_virtual_fs()

    import contextvars
    contextvars.copy_context().run(user_a)
    contextvars.copy_context().run(user_b)
