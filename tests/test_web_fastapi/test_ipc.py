"""IPC NDJSON 协议编解码测试。"""
import json
from web_fastapi.ipc import (
    encode_message, decode_message, make_request, make_event,
    make_result, make_done, make_error,
)


def test_encode_message_is_ndjson():
    """编码后的消息是单行 JSON + 换行（NDJSON 格式）。"""
    line = encode_message({"type": "event", "event": "token", "data": {"content": "hi"}})
    assert line.endswith("\n")
    parsed = json.loads(line)
    assert parsed["event"] == "token"


def test_decode_message_roundtrip():
    """编解码往返一致。"""
    original = {"id": "req-1", "type": "event", "event": "complete", "data": {"content": "done"}}
    line = encode_message(original)
    decoded = decode_message(line)
    assert decoded == original


def test_decode_invalid_json_returns_none():
    assert decode_message("not json\n") is None
    assert decode_message("\n") is None


def test_make_request():
    req = make_request("req-1", "chat", message="你好", session_id="abc")
    assert req["id"] == "req-1"
    assert req["op"] == "chat"
    assert req["message"] == "你好"
    assert req["session_id"] == "abc"


def test_make_event():
    evt = make_event("req-1", "token", content="hi")
    assert evt["id"] == "req-1"
    assert evt["type"] == "event"
    assert evt["event"] == "token"
    assert evt["data"] == {"content": "hi"}


def test_make_result():
    res = make_result("req-2", memories=[])
    assert res["id"] == "req-2"
    assert res["type"] == "result"
    assert res["data"] == {"memories": []}


def test_make_done():
    d = make_done("req-1")
    assert d["id"] == "req-1"
    assert d["type"] == "done"


def test_make_error():
    e = make_error("req-1", "出错了")
    assert e["id"] == "req-1"
    assert e["type"] == "error"
    assert e["message"] == "出错了"


def test_ready_message():
    """worker 启动后发的 ready 信号。"""
    line = encode_message({"type": "ready", "user_id": "alice"})
    parsed = json.loads(line)
    assert parsed["type"] == "ready"
    assert parsed["user_id"] == "alice"
