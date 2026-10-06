"""
============================================
插件行校验与 boot/boot_file 单元测试
============================================
覆盖：validate_rows 各失败分支、拓扑排序（依赖方在前行序颠倒）、
依赖未就绪兜底、环检测、enabled=False 跳过、boot_file 从 yaml
启动、plugin 指向不存在模块/属性的报错消息。
"""

from __future__ import annotations

import pytest

from src.cordis.config_rows import validate_rows
from src.cordis.context import Context
from src.cordis.loader import boot, boot_file
from tests.cordis import fixture_plugins
from tests.cordis.fixture_plugins import APPLY_ORDER, SEEN_TOOLS, reset

REF_TOOLS = "tests.cordis.fixture_plugins:tools_plugin"
REF_CONSUMER = "tests.cordis.fixture_plugins:consumer_plugin"
REF_PLAIN = "tests.cordis.fixture_plugins:plain_plugin"
REF_LLM = "tests.cordis.fixture_plugins:llm_plugin"


@pytest.fixture(autouse=True)
def _clean_fixture_state():
    reset()
    yield
    reset()


# ----------------------------------------------------------------------
# validate_rows
# ----------------------------------------------------------------------


def test_rows_missing_id() -> None:
    with pytest.raises(ValueError, match="id"):
        validate_rows([{"plugin": REF_PLAIN}])


def test_rows_duplicate_id() -> None:
    rows = [
        {"id": "a", "plugin": REF_PLAIN},
        {"id": "a", "plugin": REF_TOOLS},
    ]
    with pytest.raises(ValueError, match="id 重复"):
        validate_rows(rows)


def test_rows_bad_plugin_format() -> None:
    with pytest.raises(ValueError, match="plugin"):
        validate_rows([{"id": "a", "plugin": "no-colon-here"}])
    with pytest.raises(ValueError, match="plugin"):
        validate_rows([{"id": "a", "plugin": "mod:attr:extra"}])
    with pytest.raises(ValueError, match="plugin"):
        validate_rows([{"id": "a", "plugin": ""}])


def test_rows_config_not_dict() -> None:
    with pytest.raises(ValueError, match="config"):
        validate_rows([{"id": "a", "plugin": REF_PLAIN, "config": [1, 2]}])


def test_rows_enabled_not_bool() -> None:
    with pytest.raises(ValueError, match="enabled"):
        validate_rows([{"id": "a", "plugin": REF_PLAIN, "enabled": "yes"}])


def test_rows_inject_not_str_list() -> None:
    with pytest.raises(ValueError, match="inject"):
        validate_rows([{"id": "a", "plugin": REF_PLAIN, "inject": "tools"}])


def test_rows_fill_defaults() -> None:
    rows = validate_rows([{"id": "a", "plugin": REF_PLAIN, "config": {"x": 1}}])
    assert rows == [
        {
            "id": "a",
            "plugin": REF_PLAIN,
            "enabled": True,
            "config": {"x": 1},
            "inject": [],
        }
    ]


# ----------------------------------------------------------------------
# boot：解析与拓扑
# ----------------------------------------------------------------------


def test_topo_orders_provider_before_consumer() -> None:
    # 行序 consumer 在前，但 inject ["tools"] 要 tools 插件先 apply
    rows = [
        {"id": "consumer", "plugin": REF_CONSUMER, "inject": ["tools"]},
        {"id": "tools", "plugin": REF_TOOLS},
    ]
    root = boot(rows)
    assert APPLY_ORDER == ["tools", "consumer"]
    # 兄弟插件经共享提升可见依赖服务（consumer 的子 ctx.get 命中根上的 tools）
    assert SEEN_TOOLS == [{"src": "fixture"}]
    # 返回的根 ctx 也能按稳定键取用
    assert root.get("tools") == {"src": "fixture"}
    assert root.tools == {"src": "fixture"}


def test_topo_chain_dependencies() -> None:
    # c 依赖 llm，llm 依赖 tools，行序完全颠倒
    def c_plugin(ctx: Context, config: dict) -> None:
        APPLY_ORDER.append("c")
        assert ctx.get("llm") is not None
        assert ctx.get("tools") is not None

    fixture_plugins.c_plugin = c_plugin  # type: ignore[attr-defined]
    rows = [
        {"id": "c", "plugin": "tests.cordis.fixture_plugins:c_plugin", "inject": ["llm"]},
        {"id": "llm", "plugin": REF_LLM, "inject": ["tools"]},
        {"id": "tools", "plugin": REF_TOOLS},
    ]
    try:
        boot(rows)
        assert APPLY_ORDER == ["tools", "llm", "c"]
    finally:
        del fixture_plugins.c_plugin


def test_inject_satisfied_by_preexisting_root_ctx() -> None:
    # 根上已有服务时，inject 不需要任何行提供
    root = Context(name="pre")
    root.register("tools", {"src": "preexisting"})
    boot([{"id": "consumer", "plugin": REF_CONSUMER, "inject": ["tools"]}], ctx=root)
    assert SEEN_TOOLS == [{"src": "preexisting"}]


def test_config_passed_through_to_plugin() -> None:
    root = boot([{"id": "tools", "plugin": REF_TOOLS, "config": {"extra": 42}}])
    assert root.get("tools") == {"src": "fixture", "extra": 42}


def test_dependency_not_ready_fallback_error() -> None:
    rows = [
        {"id": "orphan", "plugin": REF_PLAIN, "inject": ["no_such_service"]},
        {"id": "tools", "plugin": REF_TOOLS},
    ]
    with pytest.raises(RuntimeError, match=r"插件 orphan 依赖的服务未就绪: no_such_service"):
        boot(rows)


# ----------------------------------------------------------------------
# R2-12（内核 H2）：boot 中途失败 → root.teardown() 回滚已挂服务后 re-raise
# ----------------------------------------------------------------------


class TestBootFailureRollback:

    def test_mid_boot_failure_tears_down_root(self) -> None:
        """第二个插件 apply 抛错 → 第一个插件已挂的服务被 stop（无泄漏），
        异常原样上抛。"""
        from src.cordis.service import Service

        stopped: list[str] = []

        class RecordingService(Service):
            name = "rec"

            def start(self, ctx) -> None:
                pass

            def stop(self) -> None:
                stopped.append("rec")

        def ok_plugin(ctx: Context, config: dict) -> None:
            ctx.register("rec", RecordingService())

        def boom_plugin(ctx: Context, config: dict) -> None:
            raise RuntimeError("apply 炸了")

        fixture_plugins.ok_plugin = ok_plugin  # type: ignore[attr-defined]
        fixture_plugins.boom_plugin = boom_plugin  # type: ignore[attr-defined]
        try:
            with pytest.raises(RuntimeError, match="apply 炸了"):
                boot([
                    {"id": "ok", "plugin": "tests.cordis.fixture_plugins:ok_plugin"},
                    {"id": "boom", "plugin": "tests.cordis.fixture_plugins:boom_plugin"},
                ])
        finally:
            del fixture_plugins.ok_plugin
            del fixture_plugins.boom_plugin

        assert stopped == ["rec"], "boot 中途失败时已挂服务必须被 stop（无泄漏）"

    def test_mid_boot_failure_tears_down_caller_ctx(self) -> None:
        """调用方自带 ctx（能拿到 root 引用）：失败后该 ctx 已被 teardown——
        服务清空、不再可见（可验证无残留注册服务）。"""
        root = Context(name="caller")
        root.register("sentinel", {"src": "caller"})

        def boom_plugin(ctx: Context, config: dict) -> None:
            raise ValueError("boom")

        fixture_plugins.boom_plugin = boom_plugin  # type: ignore[attr-defined]
        try:
            with pytest.raises(ValueError, match="boom"):
                boot([{"id": "boom", "plugin": "tests.cordis.fixture_plugins:boom_plugin"}],
                     ctx=root)
        finally:
            del fixture_plugins.boom_plugin

        # root 已被 teardown：服务清空（幂等，二次 teardown 也不炸）
        assert root.try_get("sentinel") is None
        root.teardown()

    def test_boot_failure_via_mount_exception_rolls_back_children(self) -> None:
        """apply 中途抛错（root.plugin 保留子 ctx 待回滚）→ boot 兜底 teardown
        把该子 ctx 一并拆卸（服务 stop 被调）。"""
        from src.cordis.service import Service

        stopped: list[str] = []

        class HalfService(Service):
            name = "half"

            def start(self, ctx) -> None:
                pass

            def stop(self) -> None:
                stopped.append("half")

        def half_plugin(ctx: Context, config: dict) -> None:
            ctx.register("half", HalfService())
            raise RuntimeError("挂一半炸了")  # 子 ctx 已记录、服务已注册后抛错

        fixture_plugins.half_plugin = half_plugin  # type: ignore[attr-defined]
        try:
            with pytest.raises(RuntimeError, match="挂一半炸了"):
                boot([{"id": "half", "plugin": "tests.cordis.fixture_plugins:half_plugin"}])
        finally:
            del fixture_plugins.half_plugin

        assert stopped == ["half"]


def test_cycle_detected() -> None:
    rows = [
        {"id": "a", "plugin": REF_PLAIN, "inject": ["b"]},
        {"id": "b", "plugin": REF_PLAIN, "inject": ["a"]},
    ]
    with pytest.raises(ValueError, match=r"成环.*\ba, b\b"):
        boot(rows)


def test_disabled_row_skipped() -> None:
    rows = [
        {"id": "plain", "plugin": REF_PLAIN, "enabled": False},
        {"id": "tools", "plugin": REF_TOOLS},
    ]
    root = boot(rows)
    assert APPLY_ORDER == ["tools"]
    assert root.has("plain") is False


def test_disabled_provider_leaves_dependency_unready() -> None:
    rows = [
        {"id": "consumer", "plugin": REF_CONSUMER, "inject": ["tools"]},
        {"id": "tools", "plugin": REF_TOOLS, "enabled": False},
    ]
    with pytest.raises(RuntimeError, match="插件 consumer 依赖的服务未就绪: tools"):
        boot(rows)


# ----------------------------------------------------------------------
# boot：解析错误
# ----------------------------------------------------------------------


def test_plugin_module_not_found() -> None:
    rows = [{"id": "bad", "plugin": "tests.cordis.no_such_module:apply"}]
    with pytest.raises(ValueError, match="插件 bad") as exc_info:
        boot(rows)
    assert "no_such_module" in str(exc_info.value)


def test_plugin_attr_not_found() -> None:
    rows = [{"id": "bad", "plugin": "tests.cordis.fixture_plugins:no_such_attr"}]
    with pytest.raises(ValueError, match=r"插件 bad.*no_such_attr"):
        boot(rows)


def test_disabled_row_with_bad_module_still_fails_fast() -> None:
    # 所有行（含 disabled）都先解析，配置错误第一时间暴露
    rows = [{"id": "bad", "plugin": "tests.cordis.no_such_module:apply", "enabled": False}]
    with pytest.raises(ValueError, match="插件 bad"):
        boot(rows)


# ----------------------------------------------------------------------
# boot_file
# ----------------------------------------------------------------------


def test_boot_file_from_yaml(tmp_path) -> None:
    config = tmp_path / "plugins.yaml"
    config.write_text(
        """
plugins:
  - id: consumer
    plugin: tests.cordis.fixture_plugins:consumer_plugin
    inject:
      - tools
  - id: tools
    plugin: tests.cordis.fixture_plugins:tools_plugin
    config:
      from: yaml
""",
        encoding="utf-8",
    )
    root = boot_file(config)
    assert APPLY_ORDER == ["tools", "consumer"]
    assert root.get("tools") == {"src": "fixture", "from": "yaml"}


def test_boot_file_missing_file(tmp_path) -> None:
    with pytest.raises(ValueError, match="不存在"):
        boot_file(tmp_path / "ghost.yaml")


def test_boot_file_bad_structure(tmp_path) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text("not_a_mapping: true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="plugins"):
        boot_file(config)


def test_boot_file_mounts_onto_given_ctx(tmp_path) -> None:
    config = tmp_path / "plugins.yaml"
    config.write_text(
        "plugins:\n  - id: tools\n    plugin: tests.cordis.fixture_plugins:tools_plugin\n",
        encoding="utf-8",
    )
    root = Context(name="given")
    returned = boot_file(config, ctx=root)
    assert returned is root
    assert root.get("tools") is not None
