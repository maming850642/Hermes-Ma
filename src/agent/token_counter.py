"""
============================================
Token 计数模块（近似）
============================================
用 tiktoken 对消息列表做近似 token 计数，用于按 token 阈值触发
上下文压缩（compact）。消息为 OpenAI dict 格式（T7 全面 dict 化），
tool_calls.function.arguments 一并计入。

为何用 tiktoken 而非精确 Qwen tokenizer：
    - tiktoken 已在依赖中（pyproject / requirements），零新依赖；
    - o200k_base 编码对中文为主的 Qwen 文本约偏高 10–20%，意味着实际 token
      比计数低 → 会略早触发 compact。对"阈值保护"用途这是安全方向
      （宁可早压缩，不要溢出报错）。

设计：模块级懒加载 encoding，进程内复用；tiktoken 不可用时回落到字符数粗估，
绝不抛异常阻塞主流程。
"""

import logging

logger = logging.getLogger("hermes.agent.token_counter")

_encoding = None
_fallback = False


def _get_encoding():
    """懒加载 tiktoken encoding，进程内复用。失败则标记回落模式。"""
    global _encoding, _fallback
    if _fallback:
        return None
    if _encoding is not None:
        return _encoding
    try:
        import tiktoken
        _encoding = tiktoken.get_encoding("o200k_base")
        logger.debug("token_counter: 已加载 tiktoken o200k_base")
    except Exception as e:
        logger.warning(f"tiktoken 不可用，token 计数回落到字符粗估（÷3.5）: {e}")
        _fallback = True
        return None
    return _encoding


def count_text_tokens(text: str) -> int:
    """单个字符串的 token 数。回落模式下按字符数 ÷ 3.5 粗估。"""
    if not text:
        return 0
    enc = _get_encoding()
    if enc is None:
        return max(1, int(len(text) / 3.5))
    try:
        return len(enc.encode(text))
    except Exception:
        return max(1, int(len(text) / 3.5))


def count_tokens(messages: list) -> int:
    """
    对消息列表做近似 token 计数（T7 起 state["messages"] 为 OpenAI dict）。

    每条消息取 content（dict 用 msg["content"]，对象用 getattr 兜底），
    用 multimodal.extract_text 提取纯文本（多模态 content 取 text 块，
    忽略 image），累加 token。assistant 消息的 tool_calls.function.arguments
    （JSON 字符串）一并计入——工具调用密集的会话不能被低估。

    图片 token 不计（Qwen vision 的 image_token 占位难以精确估算，
    忽略后偏保守 = 提前压缩，安全方向）。

    Args:
        messages: OpenAI dict 消息列表（{"role", "content", ...}），
            或带 .content 属性的旧消息对象（向后兼容）。

    Returns:
        int: 近似 token 总数（≥0）
    """
    if not messages:
        return 0

    from src.agent.multimodal import extract_text

    total = 0
    for msg in messages:
        # F1 修复：dict 消息恒走 getattr 死路径（恒空串 → 计数≈0 →
        # 阈值压缩永不触发）。dict 用 .get 取，对象保留 getattr 兜底。
        if isinstance(msg, dict):
            content = msg.get("content", "")
            # tool_calls 的 function.arguments 也是真实上下文（LLM 生成的
            # JSON 串回传在后续请求里），计入计数
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                args = fn.get("arguments", "")
                if args:
                    total += count_text_tokens(str(args))
        else:
            content = getattr(msg, "content", "")
        text = extract_text(content)
        total += count_text_tokens(text)
        # 每条消息固定开销（角色标签等），与 OpenAI 计费口径一致地加一点余量
        total += 4

    return total
