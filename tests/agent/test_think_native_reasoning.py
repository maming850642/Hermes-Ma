"""原生 reasoning 字段路径的流式拆分回归——正文"整段弹出"根因修复。

根因（2026-09-05）：模型经原生 delta.reasoning 字段返回推理时，reasoning
chunk 立即透出，但 content delta 仍被无条件喂给 ThinkSplitter 的 PROBE
探测——原生路径下 content 永远没有 <think> 标签，探测永不锁定，正文全
攒 _probe_buf（攒满 4000 字符或流结束 flush 才吐）→ 正文不流式、整段弹出。

修复契约：
  - ThinkSplitter.lock_content()：无条件进透传态，完整吐出 PROBE 已攒缓冲
    （无丢字），此后 feed() 逐段直通（保留 _tail_safe ≤6 字符尾部扣留）。
  - LLMClient.stream_chat：本流一旦出现非空 reasoning_delta 即锁定拆分器。
  - 兼容：内联 <think> 路径（无 reasoning 字段）行为不变；两路并存时
    字段优先，锁定后内联标签不再剥离（透传，取舍见 lock_content docstring）。
"""
from types import SimpleNamespace

from src.llm.client import LLMClient
from src.think import ThinkSplitter


def _chunk(content=None, reasoning=None):
    """构造 openai 流式 chunk（delta 带 content / reasoning 字段）。"""
    delta = SimpleNamespace(content=content, reasoning=reasoning, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _stream(chunks):
    """构造走假流的 LLMClient（不发网络请求）。"""
    c = LLMClient(api_key="k", base_url="http://unit.test", model="m")
    c._client.chat.completions.create = lambda **kw: iter(chunks)
    return c


_MSGS = [{"role": "user", "content": "hi"}]


# ============================================
# a) 原生 reasoning + content 交错：逐 delta 产出，无攒批
# ============================================


def test_native_reasoning_interleaved_content_yields_per_delta():
    """a) 原生 reasoning 与 content 交错流：每个 content delta 到达即产出。

    修复前：content 全被 PROBE 攒住，流结束 flush 才一次性吐出。
    """
    chunks = [
        _chunk(reasoning="想"),
        _chunk(reasoning="一"),
        _chunk(content="正"),
        _chunk(reasoning="再"),
        _chunk(content="文"),
        _chunk(content="流"),
    ]
    out = list(_stream(chunks).stream_chat(_MSGS))
    # 一进一出：6 个到达的 delta → 恰好 6 个输出 chunk，交错次序保持
    assert [(c.reasoning_delta, c.content_delta) for c in out] == [
        ("想", ""), ("一", ""), ("", "正"), ("再", ""), ("", "文"), ("", "流"),
    ]


# ============================================
# b) Qwen3 典型时序：reasoning 先行 N chunk，content 才开始
# ============================================


def test_qwen3_reasoning_first_then_content():
    """b) reasoning 先行 4 chunk 后 content 才开始：正文逐 delta 流出。"""
    reasoning_parts = ["思", "考", "三", "步"]
    content_parts = ["你", "好", "，", "世", "界", "！"]
    chunks = [_chunk(reasoning=p) for p in reasoning_parts]
    chunks += [_chunk(content=p) for p in content_parts]
    out = list(_stream(chunks).stream_chat(_MSGS))

    got_reasoning = [c.reasoning_delta for c in out if c.reasoning_delta]
    got_content = [c.content_delta for c in out if c.content_delta]
    assert got_reasoning == reasoning_parts
    # 逐 delta 产出：个数与来源一致，未被合并成一整段大块
    assert got_content == content_parts
    assert len(got_content) == len(content_parts)


# ============================================
# c) PROBE 缓冲在锁定时被完整吐出，无丢字
# ============================================


def test_lock_content_flushes_probe_buf_no_loss():
    """c) PROBE 缓冲在锁定时被完整吐出——无丢字、顺序不变。"""
    out = []
    sp = ThinkSplitter(lambda k, p: out.append((k, p)))
    sp.feed("正文开头")          # PROBE：0 输出，全攒缓冲
    sp.feed("，继续攒")          # 仍攒
    assert out == []
    sp.lock_content()            # 锁定 → 一次性完整吐出
    assert out == [("content", "正文开头，继续攒")]
    sp.feed("锁定后直通")        # 此后逐段透传
    assert out[-1] == ("content", "锁定后直通")
    sp.flush()
    assert "".join(p for _, p in out) == "正文开头，继续攒锁定后直通"


def test_lock_from_think_state_flushes_pending_no_loss():
    """c+) THINK 态（内联标签先到）锁定：扣留的半个标签尾巴按 reasoning
    吐出后再切透传，不丢字。"""
    out = []
    sp = ThinkSplitter(lambda k, p: out.append((k, p)))
    sp.feed("<think>推理</th")   # THINK：emit reasoning "推理"，扣留 "</th"
    sp.lock_content()
    sp.feed("正文")
    sp.flush()
    assert "".join(p for k, p in out if k == "reasoning") == "推理</th"
    assert "".join(p for k, p in out if k == "content") == "正文"


def test_client_lock_flushes_probe_content_before_reasoning():
    """c-client) content 先行、reasoning 后至的混流：锁定吐出的已攒正文
    先于 reasoning chunk 按序 yield，不丢、不重复。"""
    chunks = [
        _chunk(content="先到的正文"),
        _chunk(reasoning="后到的推理"),
        _chunk(content="后续正文"),
    ]
    out = list(_stream(chunks).stream_chat(_MSGS))
    seq = [(c.reasoning_delta, c.content_delta) for c in out]
    assert seq == [("", "先到的正文"), ("后到的推理", ""), ("", "后续正文")]


# ============================================
# d) 内联 <think> 路径（无 reasoning 字段）回归
# ============================================


def test_inline_think_path_regression():
    """d) 无 reasoning 字段的内联 <think> 混排路径：拆分行为与修复前一致。"""
    chunks = [
        _chunk(content="<think>推理"),
        _chunk(content="继续</think>"),
        _chunk(content="正文"),
    ]
    out = list(_stream(chunks).stream_chat(_MSGS))
    assert "".join(c.reasoning_delta for c in out) == "推理继续"
    assert "".join(c.content_delta for c in out) == "正文"


# ============================================
# e) 锁定后内联标签透传不剥离（取舍：字段优先）
# ============================================


def test_locked_splitter_passes_inline_tags_through():
    """e) 锁定后内联标签透传不剥离（字段优先，标签视为正文）。"""
    out = []
    sp = ThinkSplitter(lambda k, p: out.append((k, p)))
    sp.lock_content()
    sp.feed("正文嵌<think>伪标签</think>原样透传")
    sp.flush()
    assert out == [("content", "正文嵌<think>伪标签</think>原样透传")]


def test_client_native_reasoning_lock_keeps_inline_tags():
    """e-client) 原生 reasoning 锁定后，content 里的内联 <think> 不再剥离。"""
    chunks = [
        _chunk(reasoning="原生推理"),
        _chunk(content="答：<think>这不是推理</think>完毕"),
    ]
    out = list(_stream(chunks).stream_chat(_MSGS))
    assert "".join(c.reasoning_delta for c in out) == "原生推理"
    assert "".join(c.content_delta for c in out) == "答：<think>这不是推理</think>完毕"


# ============================================
# 锁定态 _tail_safe 尾部扣留 + 幂等
# ============================================


def test_locked_keeps_tail_safe_holdback_then_releases():
    """锁定态保留既有的 _tail_safe 尾部安全扣留（≤6 字符）：半个标签尾巴
    扣到下一 chunk / flush 再吐，不提前吐半个标签、无丢字。"""
    out = []
    sp = ThinkSplitter(lambda k, p: out.append((k, p)))
    sp.lock_content()
    sp.feed("正文<th")        # "<th" 是 <think> 前缀 → 扣留
    assert out == [("content", "正文")]
    sp.feed("x>")             # 前缀断掉 → 扣留部分连同新内容一起吐出
    assert out[-1] == ("content", "<thx>")
    sp.feed("尾</thin")       # "</thin" 是 </think> 前缀 → 扣到 flush
    sp.flush()
    assert "".join(p for _, p in out) == "正文<thx>尾</thin"


def test_lock_content_idempotent():
    """LOCKED 态重复 lock 无副作用。"""
    out = []
    sp = ThinkSplitter(lambda k, p: out.append((k, p)))
    sp.lock_content()
    sp.lock_content()
    sp.feed("a")
    assert out == [("content", "a")]
