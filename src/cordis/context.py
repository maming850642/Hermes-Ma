"""
============================================
Cordis 插件上下文（Context）
============================================
Context 是五理念中的「服务仓库 + 作用域 + 事件总线」三合一：

- 服务仓库：register/get 按稳定键（如 ctx.tools/ctx.llm）存取服务，
  查找沿 parent 链上溯；子上下文可同名遮蔽父服务
- 插件与作用域：plugin() 挂载插件（建子 ctx 后调用 apply），
  scope() 建只共享父服务的空作用域
- 可逆注册：effect() 立即执行 setup、返回的清理函数压入 disposer 栈，
  teardown 时 LIFO 回滚；on() 返回的 off 同样登记为可逆
- 类型化事件：emit/waterfall/parallel/serial 四种分发模式，
  分发前必须经 events.EventTable 校验

关键语义：
- 事件冒泡：在本 ctx 及其祖先链上收集监听器——本 ctx 注册序在前、
  逐级上溯父链在后；prepend 只影响本 ctx 内部顺序；
  子 ctx teardown 后其监听器不再参与
- teardown 顺序：先按挂载逆序 teardown 子插件 → 再 LIFO 执行本层
  disposer → 再 stop/清理本层服务与监听器；幂等可重入。
  loader 共享提升（borrowed）的服务本层不 stop，由创建方子上下文清理
- 线程安全：RLock 保护注册表变更；分发时锁内取监听器快照，
  锁外执行回调，避免死锁
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from src.cordis.events import require
from src.cordis.service import Service

logger = logging.getLogger("hermes.cordis.context")

#: 监听器类型：任意可调用（具体签名由事件的分发模式决定）
Listener = Callable[..., Any]
#: 清理函数类型：teardown 时无参调用
Disposer = Callable[[], None]
#: parallel 分发的线程池上限
_MAX_PARALLEL_WORKERS = 8


class Context:
    """插件上下文：服务仓库 + 作用域 + 事件总线。

    Attributes:
        name: 上下文名称（用于日志与调试）
        parent: 父上下文；服务查找与事件收集都沿此链上溯
    """

    def __init__(self, name: str = "", parent: "Context | None" = None) -> None:
        self.name = name
        self.parent = parent
        self._services: dict[str, Any] = {}
        # 经 loader 共享提升进来的键：生命周期归创建方子上下文，
        # 本层 teardown 时不再对其调用 stop（避免重复清理）
        self._borrowed_keys: set[str] = set()
        self._children: list["Context"] = []
        self._listeners: dict[str, list[Listener]] = {}
        self._disposers: list[Disposer] = []
        self._lock = threading.RLock()
        self._disposed = False

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        parent_name = self.parent.name if self.parent is not None else None
        return f"<Context name={self.name!r} parent={parent_name!r}>"

    # ------------------------------------------------------------------
    # 服务仓库
    # ------------------------------------------------------------------

    def register(self, key: str, service: Any) -> None:
        """向本 ctx 注册服务（仅检查本层键占用，子上下文可遮蔽父键）。

        若 service 是 Service 实例，注册后调用 service.start(self)；
        start 抛异常则回滚本次注册（键不残留）。

        Raises:
            RuntimeError: key 已在本 ctx 注册
        """
        with self._lock:
            if key in self._services:
                raise RuntimeError(f"服务键已被占用: {key}（ctx={self.name!r}）")
            self._services[key] = service
        if isinstance(service, Service):
            try:
                service.start(self)
            except BaseException:
                with self._lock:
                    self._services.pop(key, None)
                raise

    def get(self, key: str) -> Any:
        """取服务：先查自己，再沿 parent 链逐级上溯。

        Raises:
            KeyError: 服务未注册: <key>
        """
        ctx: "Context | None" = self
        while ctx is not None:
            with ctx._lock:
                if key in ctx._services:
                    return ctx._services[key]
            ctx = ctx.parent
        raise KeyError(f"服务未注册: {key}")

    def try_get(self, key: str) -> Any | None:
        """get 的不抛错版本，未找到返回 None。"""
        try:
            return self.get(key)
        except KeyError:
            return None

    def has(self, key: str) -> bool:
        """服务是否可见（含父链），不抛错。"""
        return self.try_get(key) is not None

    def __getattr__(self, name: str) -> Any:
        """属性访问委托 get(name)，使 ctx.tools 这类稳定键可用。

        仅在实际属性缺失时由 Python 触发，不会遮蔽方法；
        下划线开头的名字（内部状态/协议方法）直接 AttributeError，
        避免反序列化等场景下的无限递归。
        """
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self.get(name)
        except KeyError:
            raise AttributeError(f"Context 上不存在服务属性: {name}") from None

    # ------------------------------------------------------------------
    # 插件与作用域
    # ------------------------------------------------------------------

    def plugin(
        self,
        apply: Callable[["Context", dict], None],
        config: dict | None = None,
        name: str = "",
    ) -> "Context":
        """挂载插件：建子 Context 并调用 apply(child, config)。

        子上下文先记录到本层挂载列表再执行 apply——即使 apply 中途
        抛异常，子上下文（及其部分注册的 effect/服务）仍随后续
        teardown 一并回滚，保证挂载可逆。

        Args:
            apply: 插件入口，签名 (ctx, config)
            config: 传给插件的配置，None 视为 {}
            name: 子上下文名称，缺省取 apply 函数名

        Returns:
            插件所在的子 Context
        """
        child = Context(name=name or getattr(apply, "__name__", ""), parent=self)
        with self._lock:
            self._children.append(child)
        try:
            apply(child, config or {})
        except BaseException:
            logger.exception("插件挂载失败(name=%r)，保留子上下文待 teardown 回滚", child.name)
            raise
        return child

    def scope(self, name: str = "") -> "Context":
        """建作用域：共享父服务、独立注册的子上下文，不调用任何 apply。

        用于 waker run 这类「临时挂一批注册、跑完即卸」的场景。
        """
        child = Context(name=name, parent=self)
        with self._lock:
            self._children.append(child)
        return child

    def teardown(self) -> None:
        """卸载本上下文，幂等可重入。

        顺序：先按挂载逆序 teardown 子插件 → 再 LIFO 执行本层
        disposer → 再 stop 本层服务并清空本层监听器。
        清理回调（child.teardown/disposer/service.stop）中的异常
        会被记录但不中断整体回滚。
        """
        with self._lock:
            if self._disposed:
                return
            self._disposed = True

        # 从父上下文的挂载列表中摘除自己（只拿父锁，避免跨对象锁序问题）
        parent = self.parent
        if parent is not None:
            with parent._lock:
                for index, sibling in enumerate(parent._children):
                    if sibling is self:
                        del parent._children[index]
                        break

        with self._lock:
            children = list(self._children)
            self._children.clear()
            disposers = list(self._disposers)
            self._disposers.clear()
            services = list(self._services.items())
            self._services.clear()
            borrowed = set(self._borrowed_keys)
            self._borrowed_keys.clear()
            self._listeners.clear()

        for child in reversed(children):
            try:
                child.teardown()
            except Exception:
                logger.exception("子上下文 teardown 失败(name=%r)", child.name)

        for dispose in reversed(disposers):
            try:
                dispose()
            except Exception:
                logger.exception("disposer 执行失败(ctx=%r)", self.name)

        for key, service in reversed(services):
            if key in borrowed:
                continue  # 共享提升来的服务，stop 归创建方子上下文负责
            if isinstance(service, Service):
                try:
                    service.stop()
                except Exception:
                    logger.exception("服务 stop 失败(ctx=%r, key=%r)", self.name, key)

        logger.debug("Context teardown 完成(name=%r)", self.name)

    # ------------------------------------------------------------------
    # 可逆注册
    # ------------------------------------------------------------------

    def effect(self, fn: Callable[[], Callable[[], None] | None]) -> None:
        """可逆注册：立即执行 fn()，其返回值若可调用则作为 disposer。

        teardown 时按 LIFO 顺序调用 disposer；fn 返回 None 表示无清理。
        """
        dispose = fn()
        if callable(dispose):
            with self._lock:
                self._disposers.append(dispose)

    # ------------------------------------------------------------------
    # 事件（分发前经 EventTable 校验）
    # ------------------------------------------------------------------

    def on(self, event: str, listener: Listener, *, prepend: bool = False) -> Disposer:
        """注册事件监听器，返回幂等的 off 函数。

        off 同时登记为本层 disposer（teardown 自动清理）；
        prepend=True 时插到本 ctx 该事件队列头部（只影响本层内部顺序，
        不改变「本 ctx 在前、父链在后」的冒泡次序）。
        """
        with self._lock:
            bucket = self._listeners.setdefault(event, [])
            if prepend:
                bucket.insert(0, listener)
            else:
                bucket.append(listener)

        done = threading.Event()

        def off() -> None:
            if done.is_set():
                return
            done.set()
            with self._lock:
                bucket = self._listeners.get(event)
                if bucket is None:
                    return
                for index, fn in enumerate(bucket):
                    # 按身份删除，兼容同一函数被注册多次的情况
                    if fn is listener:
                        del bucket[index]
                        break

        with self._lock:
            self._disposers.append(off)
        return off

    def _collect(self, event: str) -> list[Listener]:
        """锁内快照收集监听器：本 ctx 注册序在前，逐级上溯父链在后。"""
        chain: list[Listener] = []
        ctx: "Context | None" = self
        while ctx is not None:
            with ctx._lock:
                bucket = ctx._listeners.get(event)
                if bucket:
                    chain.extend(bucket)
            ctx = ctx.parent
        return chain

    def emit(self, event: str, *args: Any) -> None:
        """emit 模式：注册序同步调用，仅观察，无返回值。"""
        require(event, "emit")
        for listener in self._collect(event):
            listener(*args)

    def waterfall(self, event: str, *args: Any) -> Any:
        """waterfall 模式：koa 洋葱链，监听器签名 (*args, next)。

        调 next() 委托给后续监听器（next 无参，返回值沿链向外传递）；
        不调 next 即短路，以该监听器返回值为最终值。
        链尾（或无监听器）时原样返回：单参返回 args[0]，其余返回 tuple。
        """
        require(event, "waterfall")
        listeners = self._collect(event)

        def passthrough() -> Any:
            return args[0] if len(args) == 1 else args

        def run(index: int) -> Any:
            if index >= len(listeners):
                return passthrough()

            def next_() -> Any:
                return run(index + 1)

            return listeners[index](*args, next_)

        return run(0)

    def parallel(self, event: str, *args: Any) -> list[Any]:
        """parallel 模式：线程池并发执行全部监听器并阻塞等待。

        workers = min(监听器数, 8)；任一异常也等全部结束后抛第一个
        （按注册序扫描到的首个异常）。返回各监听器返回值列表。
        """
        require(event, "parallel")
        listeners = self._collect(event)
        if not listeners:
            return []
        workers = min(len(listeners), _MAX_PARALLEL_WORKERS)
        results: list[Any] = []
        first_error: BaseException | None = None
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(listener, *args) for listener in listeners]
            for future in futures:
                try:
                    results.append(future.result())
                except BaseException as exc:  # noqa: BLE001 - 汇合后再统一抛出
                    if first_error is None:
                        first_error = exc
        if first_error is not None:
            raise first_error
        return results

    def serial(self, event: str, *args: Any) -> list[Any]:
        """serial 模式：注册序执行，收集返回值列表。"""
        require(event, "serial")
        return [listener(*args) for listener in self._collect(event)]
