"""
think.py 测试——推理/思考模式流式拆分。

验证 ThinkSplitter / emit_think_tokens / strip_think_tags 的拆分逻辑:
全标签模式、半开半闭模式(vLLM Qwen3)、标签截断、历史剥离。
"""
from src.think import emit_think_tokens, ThinkSplitter, strip_think_tags


# ============================================
# emit_think_tokens(底层拆分)
# ============================================


def test_emit_full_tag():
    """全标签 <think>r</think>ans → reasoning=r, content=ans。"""
    out = []
    in_think, pending = emit_think_tokens(
        lambda k, p: out.append((k, p)),
        "<think>推理</think>正文", False, ""
    )
    assert out == [("reasoning", "推理"), ("content", "正文")]
    assert in_think is False and pending == ""


def test_emit_think_only():
    """只有 <think>r</think> 无正文 → reasoning=r,无 content。"""
    out = []
    emit_think_tokens(lambda k, p: out.append((k, p)), "<think>思考</think>", False, "")
    assert all(k == "reasoning" for k, _ in out)


def test_emit_split_across_chunks():
    """标签跨 chunk:第一块含 <think>推理,第二块含 </think>正文。"""
    out = []
    in_think, pending = emit_think_tokens(
        lambda k, p: out.append((k, p)), "<think>推理", False, "")
    in_think, pending = emit_think_tokens(
        lambda k, p: out.append((k, p)), "</think>正文", in_think, pending)
    reasoning = "".join(p for k, p in out if k == "reasoning")
    content = "".join(p for k, p in out if k == "content")
    assert reasoning == "推理"
    assert content == "正文"


def test_emit_tag_split_mid():
    """开标签被拆到 chunk 边界(半个标签)。"""
    out = []
    in_think, pending = emit_think_tokens(
        lambda k, p: out.append((k, p)), "正文<thi", False, "")
    in_think, pending = emit_think_tokens(
        lambda k, p: out.append((k, p)), "nk>推理</think>", in_think, pending)
    flat = "".join(p for k, p in out)
    assert "正文" in flat and "推理" in flat


# ============================================
# ThinkSplitter(状态机,自动适配)
# ============================================


def test_splitter_full_tag_mode():
    """全标签模式:遇 <think> → THINK。"""
    out = []
    sp = ThinkSplitter(lambda k, p: out.append((k, p)))
    sp.feed("<think>思考内容</think>正式回答")
    sp.flush()
    kinds = [k for k, _ in out]
    assert kinds[0] == "reasoning"
    assert "content" in kinds


def test_splitter_half_open_mode():
    """半开半闭模式(vLLM Qwen3):遇 </think> 先收到思考 → CONTENT。

    模型不在输出里加 <think>(chat template 注入了),content 流是
    "思考内容</think>正式回答"。
    """
    out = []
    sp = ThinkSplitter(lambda k, p: out.append((k, p)))
    sp.feed("思考内容</think>正式回答")
    sp.flush()
    kinds = [k for k, _ in out]
    assert "reasoning" in kinds
    assert "content" in kinds
    # 思考内容应在正文前
    first_reasoning_idx = next(i for i, (k, _) in enumerate(out) if k == "reasoning")
    first_content_idx = next(i for i, (k, _) in enumerate(out) if k == "content")
    assert first_reasoning_idx < first_content_idx


def test_splitter_no_tags():
    """无标签的普通内容 → 全部 content。"""
    out = []
    sp = ThinkSplitter(lambda k, p: out.append((k, p)))
    sp.feed("普通回答没有思考标签")
    sp.flush()
    assert all(k == "content" for k, _ in out)


def test_splitter_flush_pending():
    """flush 时把未完成的思考/正文发出去。"""
    out = []
    sp = ThinkSplitter(lambda k, p: out.append((k, p)))
    sp.feed("<think>思考到一半被截断")
    sp.flush()
    kinds = [k for k, _ in out]
    assert "reasoning" in kinds


# ============================================
# strip_think_tags(历史剥离)
# ============================================


def test_strip_full_think():
    assert strip_think_tags("<think>思考</think>正文") == "正文"


def test_strip_no_think():
    assert strip_think_tags("普通文本") == "普通文本"


def test_strip_half_open():
    """半开半闭:只有 </think>(开标签被 template 吃了)。"""
    assert strip_think_tags("思考</think>正文") == "正文"


def test_strip_unclosed():
    """未闭合的 <think>。"""
    assert strip_think_tags("正文<think>未完") == "正文"
