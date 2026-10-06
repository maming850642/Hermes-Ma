"""SSE 事件编码测试。"""
from web_fastapi.sse import encode_sse_event


def test_encode_single_event():
    data = encode_sse_event("token", {"content": "你好"})
    assert data == 'event: token\ndata: {"content": "\\u4f60\\u597d"}\n\n'


def test_encode_event_with_id():
    """可恢复续流：帧带 id: <seq> 行（chat_bus 的 per-session 单调序号，
    EventSource 重连时自动经 Last-Event-ID 回传给 GET 订阅端点）。"""
    data = encode_sse_event("token", {"content": "hi"}, seq=7)
    assert data == 'id: 7\nevent: token\ndata: {"content": "hi"}\n\n'


def test_encode_event_without_seq_keeps_legacy_format():
    """seq 缺省（busy 等请求私有帧 / 兼容路径）不产生 id 行，与旧格式逐字节一致。"""
    data = encode_sse_event("error", {"message": "x", "busy": True})
    assert data == 'event: error\ndata: {"message": "x", "busy": true}\n\n'
