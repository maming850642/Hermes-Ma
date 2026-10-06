"""
存量记忆 embedding 回填脚本。

用途：
- 首次启用语义检索后，把库中所有 keyword-only（无向量）记忆补嵌；
- 换 embedding 模型（维度变化）后重建向量表后全量重嵌。

用法（在项目根，conda 环境 hermes_ma）：
    python scripts/backfill_embeddings.py [--db 路径]

幂等：只补缺失行，可重复运行。fastembed 不可用时脚本报错退出
（与在线路径的静默降级不同——手动运维场景应把失败显式暴露）。
"""
from __future__ import annotations

import argparse
import logging
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from src.memory.embeddings import get_default_embedder  # noqa: E402
from src.storage.sqlite_provider import SQLiteProvider  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("hermes.scripts.backfill")


def main() -> int:
    parser = argparse.ArgumentParser(description="补嵌 keyword-only 记忆向量")
    parser.add_argument("--db", default=None, help="库文件路径（默认 data/hermes.db）")
    args = parser.parse_args()

    try:
        embedder = get_default_embedder()
    except Exception as e:
        logger.error(f"向量客户端初始化失败: {e}")
        return 1

    prov = SQLiteProvider(db_path=args.db, embedder=embedder)
    try:
        n = prov.backfill_embeddings()
        total = len(prov.get_all())
        logger.info(f"回填完成：本次补嵌 {n} 条，库内共 {total} 条记忆")
        return 0
    finally:
        prov.close()


if __name__ == "__main__":
    raise SystemExit(main())
