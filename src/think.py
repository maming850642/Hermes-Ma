"""
============================================
推理/思考模式流式拆分(从 stash 恢复)
============================================
处理 vLLM/Qwen3 的 <think>...</think> 思考标签,把流式 chunk 拆成
reasoning(思考) 和 content(正文) 两路,供前端分别渲染。

支持两种模型行为:
  - 全标签:模型输出 `<think>r</think>ans`
  - 半开半闭:chat template 注入开标签,content 里只有 `r</think>ans`
    (vLLM/SGLang 部署 Qwen3 常见)。ThinkSplitter 自动探测锁定。
"""
import contextvars
import re


var_subagent_thinking: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "hermes_subagent_thinking", default=False
)


def emit_think_tokens(emit, text: str, in_think: bool, pending: str):
    """流式拆分 <think> 标签。

    Args:
        emit: callable(kind, piece),kind 为 "reasoning"/"content"
        text: 本 chunk content
        in_think: 进入本 chunk 前是否已在思考区
        pending: 上一 chunk 遗留的半个标签尾巴

    Returns:
        (in_think, pending): 供下一 chunk 复用
    """
    OPEN, CLOSE = "<think>", "</think>"
    buf = pending + text
    pending = ""

    while buf:
        if in_think:
            idx = buf.find(CLOSE)
            if idx == -1:
                cut = _tail_safe(buf, CLOSE)
                if cut < len(buf):
                    emit("reasoning", buf[:cut])
                    pending = buf[cut:]
                else:
                    emit("reasoning", buf)
                    pending = ""
                break
            if idx > 0:
                emit("reasoning", buf[:idx])
            buf = buf[idx + len(CLOSE):]
            in_think = False
        else:
            idx = buf.find(OPEN)
            if idx == -1:
                cut = _tail_safe(buf, OPEN)
                if cut < len(buf):
                    emit("content", buf[:cut])
                    pending = buf[cut:]
                else:
                    emit("content", buf)
                    pending = ""
                break
            if idx > 0:
                emit("content", buf[:idx])
            buf = buf[idx + len(OPEN):]
            in_think = True

    return in_think, pending


def strip_think_tags(text: str) -> str:
    """从完整文本移除 <think>...</think> 块,保留正文。存入会话历史前剥离思考内容。"""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    idx = text.find("</think>")
    if idx != -1:
        text = text[idx + len("</think>"):]
    text = re.sub(r"<think>.*", "", text, flags=re.DOTALL)
    return text


def extract_think_content(text: str) -> tuple[str, str]:
    """从完整文本提取 <think> 推理内容 + 剩余正文。

    支持三种格式（与 strip_think_tags 一致）:
      - 全标签：`<think>推理</think>正文` → ("推理", "正文")
      - 半开：`推理</think>正文` → ("推理", "正文")
      - 未闭合：`<think>推理` → ("推理", "")

    若不含任何 <think> 痕迹，返回 ("", text)（原文不变）。

    Returns:
        (reasoning, content)
    """
    # 全标签 <think>...</think>
    m = re.search(r"<think>(.*?)</think>", text, flags=re.DOTALL)
    if m:
        reasoning = m.group(1)
        content = text[:m.start()] + text[m.end():]
        return reasoning.strip(), content.strip()

    # 半开：只有 </think>（chat template 注入了开标签）
    idx = text.find("</think>")
    if idx != -1:
        reasoning = text[:idx]
        content = text[idx + len("</think>"):]
        return reasoning.strip(), content.strip()

    # 未闭合：只有 <think>
    idx = text.find("<think>")
    if idx != -1:
        reasoning = text[idx + len("<think>"):]
        return reasoning.strip(), ""

    return "", text


class ThinkSplitter:
    """流式 <think> 拆分器,自动适配全标签/半开半闭。

    状态机:
      PROBE   初始,累积到锁定。遇 <think> → THINK(全标签);遇 </think> → CONTENT(半开)
      THINK   思考区,等 </think>
      CONTENT 正文区
      LOCKED  外部锁定的正文透传区(原生 reasoning 字段路径,见 lock_content)
    """

    PROBE, THINK, CONTENT, LOCKED = 0, 1, 2, 3

    def __init__(self, emit):
        self._emit = emit
        self._mode = self.PROBE
        self._pending = ""
        self._probe_buf = ""

    def feed(self, text: str) -> None:
        if not text:
            return
        if self._mode == self.PROBE:
            self._probe(text)
        elif self._mode == self.LOCKED:
            self._emit_locked(text)
        else:
            self._emit_think(text)

    def lock_content(self) -> None:
        """无条件锁定为正文透传(原生 reasoning 字段路径专用入口)。

        背景:模型经 delta.reasoning / delta.reasoning_content 字段返回推理
        时 content 不含 <think> 标签;若仍走 PROBE 探测,正文会被无限期攒进
        _probe_buf(直到攒满 4000 字符或 flush),表现为正文"整段弹出"不流
        式。上游在本流中收到首个非空 reasoning_delta 时应立即调用本方法:
        把 PROBE 已攒缓冲按原顺序完整吐给 content 流(无丢字),此后 feed()
        逐段直通 content(仅保留 _tail_safe ≤6 字符尾部安全扣留,flush 兜底)。

        取舍(两路并存时字段优先):锁定后 content 里再出现内联
        <think>...</think> 标签也不剥离,原样透传——推理已从原生字段拿到,
        content 里的标签视为正文本身。THINK 态(内联标签先到)下锁定同理:
        先把思考区扣留的尾巴按 reasoning 吐出(无丢字),再切换透传。

        幂等:LOCKED 态重复调用无副作用。
        """
        if self._mode == self.LOCKED:
            return
        if self._mode == self.PROBE:
            if self._probe_buf:
                self._emit("content", self._probe_buf)
            self._probe_buf = ""
        elif self._mode == self.THINK:
            if self._pending:
                self._emit("reasoning", self._pending)
        self._pending = ""
        self._mode = self.LOCKED

    def flush(self) -> None:
        if self._mode == self.PROBE:
            if self._probe_buf:
                self._emit("content", self._probe_buf)
            self._probe_buf = ""
        elif self._mode == self.THINK:
            if self._pending:
                self._emit("reasoning", self._pending)
            self._pending = ""
        elif self._mode == self.LOCKED:
            # 透传态扣留的半个标签尾巴,流结束兜底吐出(无丢字)
            if self._pending:
                self._emit("content", self._pending)
            self._pending = ""

    def _probe(self, text: str) -> None:
        OPEN, CLOSE = "<think>", "</think>"
        self._probe_buf += text
        buf = self._probe_buf
        open_idx = buf.find(OPEN)
        close_idx = buf.find(CLOSE)

        if open_idx != -1 and (close_idx == -1 or open_idx < close_idx):
            if open_idx > 0:
                self._emit("content", buf[:open_idx])
            self._mode = self.THINK
            self._pending = ""
            rest = buf[open_idx + len(OPEN):]
            self._probe_buf = ""
            if rest:
                self._emit_think(rest)
        elif close_idx != -1:
            if close_idx > 0:
                self._emit("reasoning", buf[:close_idx])
            self._mode = self.CONTENT
            self._pending = ""
            rest = buf[close_idx + len(CLOSE):]
            self._probe_buf = ""
            if rest:
                self._emit_think(rest)
        elif len(self._probe_buf) > 4000:
            self._emit("content", self._probe_buf)
            self._probe_buf = ""
            self._mode = self.CONTENT

    def _emit_think(self, text: str) -> None:
        in_think = self._mode == self.THINK
        new_in, self._pending = emit_think_tokens(self._emit, text, in_think, self._pending)
        self._mode = self.THINK if new_in else self.CONTENT

    def _emit_locked(self, text: str) -> None:
        """LOCKED 态透传:不扫描/不剥离标签,仅保留 _tail_safe 尾部安全
        扣留(≤len(tag)-1=6 字符,防半个标签提前吐出;flush 兜底吐出)。"""
        buf = self._pending + text
        self._pending = ""
        cut = min(_tail_safe(buf, "<think>"), _tail_safe(buf, "</think>"))
        if cut < len(buf):
            self._pending = buf[cut:]
        if cut > 0:
            self._emit("content", buf[:cut])


def _tail_safe(buf: str, tag: str) -> int:
    """返回安全切割点:buf[:cut] 可发出,buf[cut:] 是可能是半个标签的尾巴。"""
    max_hold = min(len(buf), len(tag) - 1)
    for k in range(max_hold, 0, -1):
        if buf.endswith(tag[:k]):
            return len(buf) - k
    return len(buf)

