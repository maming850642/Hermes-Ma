"""
============================================
Cordis 服务基类（Service）
============================================
按 Cordis 理念，插件既可以是带 apply(ctx, config) 的普通函数，
也可以是 Service 子类——即「有生命周期的服务」。

生命周期约定：
- register 时：Context 调用 start(ctx)，服务在此完成初始化、
  挂事件监听、注册下游依赖等工作
- teardown 时：Context 调用 stop()，服务释放持有的资源

设计要点：
- name 是注册到 Context 的服务键名（ClassVar），子类必须覆写，
  否则只能靠 register 时显式传 key
- start/stop 默认 no-op，子类按需覆写
- start 抛异常时 Context 会回滚本次注册（键不残留），保证注册皆可逆
- stop 建议实现为幂等：Context 保证常规路径只调一次，但防御性
  实现可避免作用域嵌套卸载时的意外
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from src.cordis.context import Context


class Service:
    """服务基类：注册到 Context 仓库、具备 start/stop 生命周期的对象。"""

    #: 注册到 ctx 的键名（ctx.<name> 属性即取到本服务），子类必须覆写
    name: ClassVar[str] = ""

    def start(self, ctx: "Context") -> None:
        """register 时由 Context 回调，默认 no-op。

        Args:
            ctx: 注册本服务的上下文
        """

    def stop(self) -> None:
        """teardown 时由 Context 回调，默认 no-op；实现必须幂等。"""
