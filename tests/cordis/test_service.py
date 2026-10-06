"""
============================================
Service 基类单元测试
============================================
覆盖：start/stop 生命周期钩子在 register/teardown 时被调用、
start 抛异常时的注册回滚、Service 子类经 loader 挂载。
"""

from __future__ import annotations

import pytest

from src.cordis.context import Context
from src.cordis.service import Service


class Recorder(Service):
    """记录生命周期调用的服务。"""

    name = "recorder"

    def __init__(self) -> None:
        self.started_with: list[Context] = []
        self.stop_calls = 0

    def start(self, ctx: Context) -> None:
        self.started_with.append(ctx)

    def stop(self) -> None:
        self.stop_calls += 1


def test_service_start_called_on_register() -> None:
    ctx = Context()
    svc = Recorder()
    ctx.register("recorder", svc)
    assert svc.started_with == [ctx]
    assert ctx.get("recorder") is svc


def test_service_stop_called_on_teardown() -> None:
    ctx = Context()
    svc = Recorder()
    ctx.register("recorder", svc)
    ctx.teardown()
    assert svc.stop_calls == 1


def test_service_registered_under_custom_key() -> None:
    ctx = Context()
    svc = Recorder()
    ctx.register("whatever", svc)
    assert ctx.whatever is svc
    assert svc.started_with == [ctx]


class Failing(Service):
    name = "failing"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self, ctx: Context) -> None:
        raise RuntimeError("start-exploded")

    def stop(self) -> None:
        self.calls.append("stop")


def test_service_start_failure_rolls_back_registration() -> None:
    ctx = Context()
    svc = Failing()
    with pytest.raises(RuntimeError, match="start-exploded"):
        ctx.register("failing", svc)
    assert ctx.try_get("failing") is None  # 键不残留
    ctx.register("failing", "fallback")  # 键可重新使用
    assert ctx.get("failing") == "fallback"


def test_base_service_defaults_are_noop() -> None:
    ctx = Context()
    svc = Service()
    svc.start(ctx)  # 不抛错
    svc.stop()


def test_service_as_plugin_via_loader() -> None:
    from src.cordis.loader import boot

    class ToolsService(Recorder):
        name = "tools"

    # 动态挂到可导入的模块属性上，供 loader 按 "模块:属性" 解析
    import tests.cordis.fixture_plugins as fixtures

    fixtures.BoundToolsService = ToolsService  # type: ignore[attr-defined]
    try:
        root = boot([{"id": "tools-svc", "plugin": "tests.cordis.fixture_plugins:BoundToolsService"}])
        assert isinstance(root.get("tools"), ToolsService)
        service = root.tools
        assert service.started_with  # start 已被调用
        root.teardown()
        assert service.stop_calls == 1  # 只由创建方子上下文 stop 一次
    finally:
        del fixtures.BoundToolsService
