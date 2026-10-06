"""
memory 插件 —— MemoryManager 注册为 "memory"。

inject: [storage] —— 记忆存取走 storage 插件提供的 SQLiteProvider
（与 MemoryManager() 默认后端同为 data/hermes.db，行为不变，只是
共享同一 provider 实例）。
"""

from __future__ import annotations

from src.cordis.context import Context


def apply(ctx: Context, config: dict) -> None:
    from src.memory.manager import MemoryManager

    store = ctx.get("storage")
    # 语义通道：共享 provider 默认不带 embedder（core 存储不背模型加载），
    # 记忆侧在此补挂——聊天检索、记忆页与聚合链路共用同一实例，vec 通道
    # 随之全量生效。attach 幂等，懒加载包装保证挂载零成本、失败自动降级。
    attach = getattr(store, "attach_embedder", None)
    if attach is not None:
        from src.memory.embeddings import get_default_embedder

        attach(get_default_embedder())
    ctx.register("memory", MemoryManager(store=store))
