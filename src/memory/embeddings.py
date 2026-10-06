"""
本地向量客户端 —— 混合记忆检索的语义通道（fastembed / BGE-small-zh）。

用户无感设计（验收硬标准）：
- fastembed 的 import 全部延迟到单例内部：包未安装 / 模型加载失败时，
  其余功能零影响，检索自动退化为纯关键词路（调用方捕获
  EmbeddingUnavailable 判定降级）。
- 模型缓存目录在 data/models/（项目内、可随 set_data_root 隔离）；
  首次使用自动下载 ~100MB，之后离线可用。测试注入 Fake 实现，永不下载。
- 进程级单例：聚合调度器每次 tick 新建的 MemoryManager 复用同一份
  已加载模型，不重复载内存。
"""
from __future__ import annotations

import logging
import threading
from typing import Protocol

from src.storage import paths

logger = logging.getLogger("hermes.memory.embeddings")

# 模型身份（换模型 = 换维度 = 全量重嵌；由 meta 表的 embedding_model_tag 驱动重建）
MODEL_ID = "BAAI/bge-small-zh-v1.5"
MODEL_TAG = "bge-small-zh-v1.5"
DIM = 512


class EmbeddingUnavailable(RuntimeError):
    """向量通道不可用——调用方应降级为纯关键词检索，而非报错中断。"""


class EmbeddingClient(Protocol):
    """向量客户端协议（测试用 Fake 实现同一形状）。"""

    def model_tag(self) -> str: ...

    def dim(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastembedClient:
    """fastembed BGE-small-zh 封装：首次 embed 时才真正加载/下载模型。"""

    def __init__(self) -> None:
        self._model = None
        self._failed = False
        self._lock = threading.Lock()

    def model_tag(self) -> str:
        return MODEL_TAG

    def dim(self) -> int:
        return DIM

    def _ensure_loaded(self):
        if self._model is not None:
            return self._model
        if self._failed:
            raise EmbeddingUnavailable("本地向量模型此前加载失败，已降级为纯关键词")
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                from fastembed import TextEmbedding

                cache_dir = paths.data_dir("models")
                cache_dir.mkdir(parents=True, exist_ok=True)
                logger.info(
                    f"初始化本地向量模型 {MODEL_ID}（首次自动下载 ~100MB → {cache_dir}）"
                )
                self._model = TextEmbedding(
                    model_name=MODEL_ID, cache_dir=str(cache_dir), threads=1
                )
                logger.info("本地向量模型就绪")
            except Exception as e:
                self._failed = True
                raise EmbeddingUnavailable(f"本地向量模型加载失败: {e}") from e
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure_loaded()
        vecs = model.embed(texts, batch_size=32)
        return [[float(x) for x in v] for v in vecs]


_default: FastembedClient | None = None
_default_lock = threading.Lock()


def get_default_embedder() -> FastembedClient:
    """进程级单例。worker 子进程 / 主进程聚合调度器 / CLI 各持一份（进程内共享）。"""
    global _default
    with _default_lock:
        if _default is None:
            _default = FastembedClient()
        return _default
