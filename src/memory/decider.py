"""
MemoryDecider - 用 LLM 对比新事实与相似旧记忆，决定 ADD/UPDATE/DELETE/NOOP。

LLM 调用 #2（共两次中的第二次）。
对单条新事实做决策，输出 Decision 列表。

设计要点：
- 无候选旧记忆时短路返回 ADD，省一次 LLM 调用（成本优化）
- LLM 异常 / 非 JSON 响应 → FAIL（不是 NOOP：「已记过」会误导）
  会把解析失败的新事实与候选旧记忆重复入库（实测 8 分钟 26 条重复）；
  真实的新事实下一轮还会再次出现，宁缺勿重
- 输出严格 JSON，extract_json 之外叠加 parse_partial_json 兜底救畸形输出
"""
import json
import logging

from src.llm.client import LLMClient
from src.llm.json_fix import parse_partial_json

from src.memory.models import Hit, Decision
from src.memory.prompts import DECIDE_PROMPT
from src.memory.extractor import SharedLLMProvider, memory_client, extract_json

logger = logging.getLogger("hermes.memory.decider")


class MemoryDecider:
    """记忆决策器。"""

    def __init__(self, api_key: str, base_url: str, model: str, llm=None):
        """
        Args:
            api_key/base_url/model: 兜底路径的 LLM 配置
            llm: 共享客户端（LLMClient 或 SharedLLMProvider）。传入则复用
                chat 侧同一 client；None 时兜底自建普通 client。
        """
        # 历史版本在此独立建 client 且强制 qwen 专属 enable_thinking=True：
        # 非 qwen 模型不识别该参数，thinking 输出撞默认 2000 token 上限后
        # content 为空——正是"决策 JSON 解析失败"刷屏的根因。现复用共享
        # client，payload 差异（temperature=0、无任何模型专属 kwarg）在
        # 调用级表达。
        self.llm = llm if llm is not None else LLMClient(
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=0,
        )

    def decide(self, new_fact: str, candidates: list[Hit]) -> list[Decision]:
        """
        对单条新事实做决策。

        Args:
            new_fact: 新提取的事实文本
            candidates: store.search_candidates 返回的相似旧记忆列表

        Returns:
            list[Decision]: 通常只有 1 个元素（对这条新事实的决策）。
                            异常或解析失败时 FAIL（与「已记过」的 NOOP 区分）。
        """
        # 无候选 → 直接 ADD，省一次 LLM 调用
        if not candidates:
            return [Decision(action="ADD", content=new_fact)]

        # 构造旧记忆 JSON（供 prompt 引用）
        old_json = json.dumps(
            [{"id": h.memory.id, "content": h.memory.content} for h in candidates],
            ensure_ascii=False,
        )
        prompt = DECIDE_PROMPT.format(
            new_facts_json=json.dumps([new_fact], ensure_ascii=False),
            old_memories_json=old_json,
        )

        try:
            raw = memory_client(self.llm).invoke_simple(
                prompt, temperature=0, extra_body={})
            data = extract_json(raw)
            if data is None:
                # 兜底：救畸形/截断 JSON（未闭合括号、尾随逗号等）
                try:
                    repaired = parse_partial_json(raw)
                    data = repaired if isinstance(repaired, dict) else None
                except Exception:
                    data = None
            if data is None:
                logger.warning(
                    "决策 JSON 解析失败（FAIL）: len=%s raw=%r"
                    "（raw 为空通常是 thinking 输出耗尽 token 上限或输出被截断）",
                    len(raw), raw[:100],
                )
                return [Decision(action="FAIL", content=new_fact)]
            decisions_data = data.get("decisions", [])
            if not isinstance(decisions_data, list) or not decisions_data:
                # 模型显式给出空决策 = 判定无需变更
                return [Decision(action="NOOP", content=new_fact)]
            results: list[Decision] = []
            for item in decisions_data:
                if not isinstance(item, dict):
                    continue
                results.append(Decision(
                    action=item.get("action", "NOOP"),
                    content=item.get("content", ""),
                    target_id=item.get("target_id"),
                ))
            # 兜底：LLM 返回空 decisions 数组 → NOOP
            if not results:
                return [Decision(action="NOOP", content=new_fact)]
            return results
        except Exception as e:
            logger.error(f"决策失败（FAIL）: {e}")
            return [Decision(action="FAIL", content=new_fact)]
