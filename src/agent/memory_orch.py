"""
============================================
MemoryOrchestrator - 记忆编排模块
============================================
负责记忆检索的编排（retrieve_memory 节点调用）。

注：当前为薄转发层——存储方法已于 2026-06-22 移除（记忆改由 remember 工具
+ 会话总结两条路径触发），build_enhanced_query 也已废弃（直接用原始输入检索，
    见 retrieve_with_detail）。保留本类作为 graph.py 与 MemoryManager 之间的
解耦缓冲，便于未来重新加入检索增强逻辑，不直接让 graph 调 MemoryManager。
"""

import logging

from src.memory import MemoryManager

logger = logging.getLogger("hermes.agent.memory_orch")


class MemoryOrchestrator:
    """
    记忆编排器：管理记忆的检索和存储。

    Attributes:
        memory: MemoryManager 实例
    """

    def __init__(self, memory_manager: MemoryManager):
        """
        初始化记忆编排器。

        Args:
            memory_manager: 记忆管理器实例
        """
        self.memory = memory_manager

    def retrieve_with_detail(
        self, user_id: str, current_input: str, messages: list, session_id: str = "",
        project: str | None = None,
    ) -> dict:
        """
        检索长期记忆，并返回展示用的详情（增强查询、命中数、每条记忆预览）。

        2026-06-14: 新增方法，供 CLI/Web 向用户展示"记忆检索发生了"。

        Args:
            user_id: 用户 ID
            current_input: 当前用户输入
            messages: 对话历史消息
            session_id: 会话 ID（用于 session 级标识）
            project: 会话绑定项目（None = 回落全局激活指针）。
                记忆按项目隔离，多项目并发时必须传会话自身的归属，
                不能让"运行时刻顶栏停在哪"决定检索范围。

        Returns:
            dict: {
                "memories": list[str],     # 记忆文本列表
                "query": str,              # 实际用于检索的增强查询
                "raw_count": int,          # 原始召回数（未过滤相似度）
                "hit_count": int,          # 过滤相似度后的命中数
                "hits": list[dict],        # 命中详情 [{"memory": str, "score": float}]
            }
        """
        # 2026-06-15: 移除 build_enhanced_query，直接用原始用户输入检索。
        # 旧实现拼接历史用户消息会污染语义（如把"你是谁？"拼进来），导致召回不到相关记忆。
        enhanced_query = current_input
        logger.debug(f"retrieve: query='{enhanced_query[:80]}', session_id={session_id}")

        # 2026-06-14: 使用 search_with_detail 获取原始召回数与过滤后命中数
        detail = self.memory.search_with_detail(
            user_id=user_id, query=enhanced_query, session_id=session_id or None,
            project=project,
        )
        filtered = detail["filtered_results"]
        memory_texts = [r.get("memory", "") for r in filtered if r.get("memory")]

        logger.debug(
            f"检索到 {detail['hit_count']} 条相关记忆（原始召回 {detail['raw_count']} 条）"
        )
        return {
            "memories": memory_texts,
            "query": enhanced_query,
            "raw_count": detail["raw_count"],
            "hit_count": detail["hit_count"],
            "hits": [
                {
                    "memory": r.get("memory", ""),
                    "score": r.get("score", 0.0),
                    "detail": r.get("detail"),
                }
                for r in filtered
            ],
        }

    # 注：store / store_from_response / store_from_response_with_detail 三个方法
    # 已于 2026-06-22 移除（agent memory 重构）。
    # 记忆存储改由两条路径触发：
    #   1. LLM 自主调用 remember 工具（主路径，src/tools/remember.py）
    #   2. 会话结束 Summarizer 总结（src/agent/session_lifecycle.py → MemoryManager.ingest_conversation）
    # 旧的"每轮无脑存储"已被证明会记一堆没用的流水账，不再保留。
