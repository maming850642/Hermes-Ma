"""
MemoryExtractor - 用 LLM 从对话中提取原子事实。

LLM 调用 #1（共两次中的第一次）。
JSON mode 输出 {"facts": [...]}，对非 JSON 响应优雅降级为空列表。

入口：
- extract_from_session(conversation_text): 整段会话提取（会话总结路径的副产品）
"""
import json
import logging
import re

from src.llm.client import LLMClient

from src.memory.prompts import EXTRACT_FROM_SESSION_PROMPT

logger = logging.getLogger("hermes.memory.extractor")


class SharedLLMProvider:
    """chat 侧共享客户端的薄包装（零参可调用对象）。

    记忆组件不再自建 LLMClient——worker 启动后经
    MemoryManager.set_llm_provider(agent.get_llm_client) 注入本包装，
    每次调用取当前实例（思考开关翻转会重建客户端，自动跟随）。
    用专门类而非裸函数：组件按 isinstance 区分"共享 provider"与
    "普通 client"，测试注入的 MagicMock 不受影响。
    """

    def __init__(self, provider_fn):
        self._fn = provider_fn

    def __call__(self) -> LLMClient:
        return self._fn()


def memory_client(provider):
    """取组件当前应使用的 LLMClient。"""
    return provider() if isinstance(provider, SharedLLMProvider) else provider


def extract_json(text: str) -> dict | None:
    """从可能带前后缀文本的 LLM 输出中提取最外层 JSON 对象。

    公共工具函数：LLM 偶尔在 JSON 前后加解释文字（"结果是：{...}以上"），
    这里用正则抓最外层 {...}，解析失败返回 None（调用方降级处理）。
    被 MemoryExtractor 和 MemoryDecider 共用。
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


class MemoryExtractor:
    """事实提取器。"""

    def __init__(self, api_key: str, base_url: str, model: str, llm=None):
        """
        Args:
            api_key/base_url/model: 兜底路径的 LLM 配置
            llm: 共享客户端（LLMClient 或 SharedLLMProvider）。传入则复用
                chat 侧同一 client；None 时兜底自建普通 client。
        """
        self.llm = llm if llm is not None else LLMClient(
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=0,
        )

    def _invoke(self, prompt: str) -> str:
        # temperature=0 + extra_body={}：确定性输出，且不带任何模型专属
        # kwarg（历史 qwen chat_template_kwargs 已移除——非 qwen 模型
        # 不识别该参数，thinking 输出还会撞客户端 token 上限致 content 为空）
        return memory_client(self.llm).invoke_simple(
            prompt, temperature=0, extra_body={})

    def extract_from_session(self, conversation_text: str) -> list[str]:
        """从整段会话文本提取长期事实（会话总结路径的副产品）。

        Args:
            conversation_text: 整段会话文本（"用户: ...\n助手: ..." 形式）

        Returns:
            事实字符串列表。
        """
        prompt = EXTRACT_FROM_SESSION_PROMPT.format(conversation_text=conversation_text)
        raw = self._invoke(prompt)
        data = extract_json(raw)
        if data is None:
            return []
        facts = data.get("facts", [])
        if not isinstance(facts, list):
            return []
        return [str(f) for f in facts if f]
