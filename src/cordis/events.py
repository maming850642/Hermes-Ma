"""
============================================
Cordis 事件契约表（EventTable）
============================================
模块级单表维护「事件名 → 分发模式」的映射。

分发模式是事件的公共契约（照搬 Cordis @mode 语义）：
监听器签名与返回值语义都由模式决定，因此任何分发动作发生前
必须先经 require() 校验，避免同一事件被按不同语义消费。

四种模式：
- emit      纯观察，无返回值，注册序同步调用
- waterfall koa 洋葱链，监听器签名 (*args, next)
- parallel  线程池并发执行全部监听器后阻塞汇合
- serial    注册序执行并收集返回值列表

设计要点：
- 单表全局共享：所有 Context 实例共用一张表，声明即全局生效
- declare 幂等：重复声明同 mode 直接通过，改 mode 视为契约冲突报错
- 表只增不减：事件契约视为全生命周期稳定，不提供 undeclare
"""

from __future__ import annotations

_MODES: tuple[str, ...] = ("emit", "waterfall", "parallel", "serial")

_TABLE: dict[str, str] = {}


def declare(event: str, mode: str) -> None:
    """声明事件的分发模式。

    Args:
        event: 事件名
        mode: 分发模式，必须是 emit/waterfall/parallel/serial 之一

    Raises:
        ValueError: mode 非法，或事件已按其他模式声明（契约冲突）
    """
    if mode not in _MODES:
        raise ValueError(f"非法分发模式: {mode!r}，合法值: {', '.join(_MODES)}")
    existing = _TABLE.get(event)
    if existing is not None and existing != mode:
        raise ValueError(f"事件 {event!r} 已声明为 {existing!r} 分发模式，不能改为 {mode!r}")
    _TABLE[event] = mode


def declare_events(mapping: dict[str, str]) -> None:
    """批量声明事件模式，mapping 为 事件名 → 模式。"""
    for event, mode in mapping.items():
        declare(event, mode)


def mode_of(event: str) -> str | None:
    """查询事件的分发模式，未声明返回 None。"""
    return _TABLE.get(event)


def require(event: str, mode: str) -> None:
    """分发前校验：事件必须已声明且模式匹配。

    Raises:
        ValueError: 事件未声明，或声明的模式与分发方式不一致
    """
    existing = _TABLE.get(event)
    if existing is None:
        raise ValueError(f"事件未声明分发模式: {event}")
    if existing != mode:
        raise ValueError(f"事件 {event!r} 的分发模式是 {existing!r}，不能按 {mode!r} 分发")
