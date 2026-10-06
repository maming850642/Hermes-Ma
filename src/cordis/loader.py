"""
============================================
Cordis 插件加载器（boot / boot_file）
============================================
从「插件行」列表（yaml 顶层 plugins: 段或直接传入的 dict 列表）
启动一组插件到 Context 上。

流程：
1. validate_rows 规范化校验
2. 每行 plugin 字符串 "模块路径:属性名" 经 importlib 解析出 apply
   可调用（模块/属性不存在 → 报错带行 id）；目标也可以是 Service
   子类——包一层 apply，实例化后按 Service.name 注册
3. 按 inject 对 enabled 行做拓扑排序（Kahn，环 → 报错列出成环行
   id）。「行提供哪些服务键」的静态推断约定：
   - 行 id 即该插件默认提供的服务键
   - Service 子类插件额外提供 Service.name
   - 函数插件可通过 apply.provides: list[str] 显式补充
   已在根 ctx（含祖先链）注册的键视为已满足，不参与排序约束
4. 按拓扑序对每个 enabled 行：apply 前先校验 inject 的服务
   ctx.try_get 可得，不可得 → RuntimeError 兜底；随后 ctx.plugin
   挂载，并把插件在子上下文注册的服务「共享提升」到 boot 根
   （直接写键、不重复触发 start），使后续兄弟插件与返回的根 ctx
   都能取到（ctx.tools 稳定键语义）。服务的生命周期（stop）仍归
   创建它的插件子上下文管理（根上标记 borrowed，teardown 不重复 stop）。
   同样把插件子上下文注册的事件监听器提升到 boot 根（R2-13：移动而非
   复制——子树内分发经祖先链仍命中且恰好一次；off 登记回子上下文，
   子上下文 teardown 时从根摘除）。否则 agent 等后代 ctx 分发事件时
   祖先链上看不到插件默认监听器（兄弟不可见），llm/tools 插件的默认
   监听器在 kernel 路径上是死代码。挂载中途失败 → root.teardown()
   回滚已挂服务/监听器后 re-raise（R2-12：调用方拿不到 root，防泄漏）。
"""

from __future__ import annotations

import importlib
import inspect
import logging
from collections import deque
from pathlib import Path
from typing import Callable

import yaml

from src.cordis.config_rows import validate_rows
from src.cordis.context import Context
from src.cordis.service import Service

logger = logging.getLogger("hermes.cordis.loader")

ApplyFn = Callable[["Context", dict], None]


def _resolve_apply(ref: str, row_id: str) -> ApplyFn:
    """把 '模块路径:属性名' 解析为 apply(ctx, config) 可调用。

    目标是 Service 子类时，包装成「实例化并按 Service.name（缺省用
    行 id）注册」的 apply，对应 Cordis「插件=函数或 Service 子类」；
    包装函数带 __wrapped_target__ 指回原类，供拓扑排序推断提供键。

    Raises:
        ValueError: 模块导入失败 / 属性不存在 / 目标不可调用，消息带行 id
    """
    module_path, _, attr = ref.partition(":")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ValueError(
            f"插件 {row_id} 的模块导入失败: {module_path!r}（来自 {ref!r}）: {exc}"
        ) from exc
    target = getattr(module, attr, None)
    if target is None:
        raise ValueError(f"插件 {row_id} 在模块 {module_path!r} 中不存在属性: {attr!r}")

    if inspect.isclass(target) and issubclass(target, Service):
        service_cls = target

        def _service_apply(ctx: Context, config: dict) -> None:
            instance = service_cls()
            ctx.register(instance.name or row_id, instance)

        _service_apply.__wrapped_target__ = service_cls  # type: ignore[attr-defined]
        return _service_apply
    if not callable(target):
        raise ValueError(f"插件 {row_id} 解析到的目标不可调用: {ref!r}")
    return target


def _provided_keys(row: dict, apply_fn: ApplyFn) -> list[str]:
    """静态推断一行插件提供的服务键（用于拓扑排序建边）。"""
    keys = [row["id"]]  # 约定：行 id 即默认提供的服务键
    target = getattr(apply_fn, "__wrapped_target__", None)
    if inspect.isclass(target) and issubclass(target, Service):
        if target.name:
            keys.append(target.name)
    else:
        extra = getattr(apply_fn, "provides", None)
        if isinstance(extra, (list, tuple)):
            keys.extend(key for key in extra if isinstance(key, str))
    return keys


def _topo_sort(resolved: list[tuple[dict, ApplyFn]], root: Context) -> list[tuple[dict, ApplyFn]]:
    """对 enabled 行（含解析结果）按 inject 做 Kahn 拓扑排序。

    Args:
        resolved: (行, apply) 列表，只含 enabled 行
        root: boot 根上下文；root 已可见（含祖先链）的 inject 键
              视为已满足，不建排序边

    Raises:
        ValueError: 依赖成环，消息列出成环行 id
    """
    providers: dict[str, list[str]] = {}
    for row, apply_fn in resolved:
        for key in _provided_keys(row, apply_fn):
            providers.setdefault(key, []).append(row["id"])

    rows = [row for row, _ in resolved]
    by_id = {row["id"]: (row, apply_fn) for row, apply_fn in resolved}
    indegree = {row["id"]: 0 for row in rows}
    edges: dict[str, list[str]] = {row["id"]: [] for row in rows}
    seen_edges: set[tuple[str, str]] = set()
    for row in rows:
        for key in row["inject"]:
            if root.try_get(key) is not None:
                continue  # 父链已有，无需排序约束
            for provider_id in providers.get(key, ()):
                if provider_id == row["id"]:
                    continue  # 自提供不算依赖
                edge = (provider_id, row["id"])
                if edge in seen_edges:
                    continue
                seen_edges.add(edge)
                edges[provider_id].append(row["id"])
                indegree[row["id"]] += 1

    # 初始零入度队列按行序入队，保证排序结果稳定可复现
    queue = deque(row_id for row_id in (row["id"] for row in rows) if indegree[row_id] == 0)
    ordered: list[tuple[dict, ApplyFn]] = []
    while queue:
        row_id = queue.popleft()
        ordered.append(by_id[row_id])
        for dependent_id in edges[row_id]:
            indegree[dependent_id] -= 1
            if indegree[dependent_id] == 0:
                queue.append(dependent_id)

    if len(ordered) != len(rows):
        cyclic = sorted(set(by_id) - {item[0]["id"] for item in ordered})
        raise ValueError(f"插件依赖成环，涉及行 id: {', '.join(cyclic)}")
    return ordered


def _share_services_to_root(child: Context, root: Context) -> None:
    """把插件在子上下文注册的服务共享提升到 boot 根。

    直接写键、不重复触发 start；标记为 borrowed，stop 仍由创建方
    子上下文负责（root teardown 时跳过，避免重复清理）。
    根上已有的键（用户预注册或先行插件提供）不覆盖。
    """
    with child._lock:
        fresh = list(child._services.items())
    for key, service in fresh:
        with root._lock:
            already = key in root._services
            if not already:
                root._services[key] = service
                root._borrowed_keys.add(key)
        if not already:
            logger.debug("共享插件服务 %r 到 boot 根（来源 ctx=%r）", key, child.name)


def _share_listeners_to_root(child: Context, root: Context) -> None:
    """把插件子上下文注册的事件监听器提升到 boot 根（R2-13 可见性修复）。

    问题：服务提升解决了「兄弟插件/调用方取得到服务」，但监听器仍留在
    插件子 ctx——agent 这类后代 ctx 分发事件时祖先链上没有它（兄弟不可见），
    llm/tools 插件的默认监听器在 kernel 路径上是死代码。

    语义：移动而非复制——子 ctx 桶清空、root 桶追加同一监听器对象：
    - 子树内分发（如 tools registry 的 kernel ctx）经祖先链（root）仍命中，
      且恰好一次，不会重复执行（复制会导致 executor 双跑）；
    - 生命周期归属创建方（借用语义）：root.on 返回的 off 同时登记回子 ctx
      disposers，子 ctx teardown 时自动从 root 摘除；root teardown 清理自身。
    """
    with child._lock:
        moved = [
            (event, list(bucket))
            for event, bucket in child._listeners.items()
            if bucket
        ]
        for event in list(child._listeners):
            child._listeners[event] = []
    for event, listeners in moved:
        for listener in listeners:
            off = root.on(event, listener)
            with child._lock:
                child._disposers.append(off)
            logger.debug("共享插件监听器 %r 到 boot 根（来源 ctx=%r）", event, child.name)


def boot(rows: list[dict], ctx: Context | None = None) -> Context:
    """按插件行列表启动插件。

    Args:
        rows: 插件行列表（结构见 config_rows.validate_rows）
        ctx: 挂载根上下文；None 时新建

    Returns:
        挂载根上下文（插件提供的服务已共享提升到该 ctx，可 ctx.<key> 取用）
    """
    validated = validate_rows(rows)
    root = ctx if ctx is not None else Context(name="boot")

    # 全部行（含 disabled）都先解析，让配置/拼写错误在启动第一时间暴露
    apply_fns = {row["id"]: _resolve_apply(row["plugin"], row["id"]) for row in validated}

    enabled = [(row, apply_fns[row["id"]]) for row in validated if row["enabled"]]
    try:
        for row, apply_fn in _topo_sort(enabled, root):
            for key in row["inject"]:
                if root.try_get(key) is None:
                    raise RuntimeError(f"插件 {row['id']} 依赖的服务未就绪: {key}")
            child = root.plugin(apply_fn, row["config"], name=row["id"])
            _share_services_to_root(child, root)
            _share_listeners_to_root(child, root)
            logger.debug("插件已挂载: %s", row["id"])
    except BaseException:
        # R2-12（内核 H2）：挂载中途失败时调用方拿不到 root，已挂的服务/
        # 监听器/线程会泄漏——整体 teardown 回滚后原样 re-raise
        logger.error("boot 中途失败，回滚已挂载的插件（root teardown）", exc_info=True)
        root.teardown()
        raise

    skipped = [row["id"] for row in validated if not row["enabled"]]
    if skipped:
        logger.info("跳过 disabled 插件: %s", ", ".join(skipped))
    return root


def boot_file(path: str | Path, ctx: Context | None = None) -> Context:
    """从 yaml 文件启动插件（顶层 plugins: 列表）。

    Raises:
        ValueError: 文件不存在 / 结构不是 'plugins:' 列表
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise ValueError(f"插件配置文件不存在: {file_path}")
    data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise ValueError(f"插件配置文件顶层必须是 'plugins:' 列表: {file_path}")
    return boot(data["plugins"], ctx=ctx)
