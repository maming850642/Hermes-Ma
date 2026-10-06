"""
IPC NDJSON 协议：前端（FastAPI）与 worker 子进程之间的通信。

格式：每行一个 JSON 对象（NDJSON，Newline-Delimited JSON）。
- 前端 → worker（stdin）：请求命令（make_request）
- worker → 前端（stdout）：事件流（make_event）、结果（make_result）、
  完成信号（make_done）、错误（make_error）、就绪信号（ready）

stdout 专用于 IPC；日志走 stderr（见 logging_config.py）。
"""
import json
import uuid


def encode_message(msg: dict) -> str:
    """编码为 NDJSON 行（单行 JSON + \\n）。"""
    return json.dumps(msg, ensure_ascii=False) + "\n"


def decode_message(line: str) -> dict | None:
    """从 NDJSON 行解码。无效行返回 None。"""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None


def make_request(req_id: str, op: str, **kwargs) -> dict:
    """前端 → worker 的请求命令。"""
    msg = {"id": req_id, "op": op}
    msg.update(kwargs)
    return msg


def make_event(req_id: str, event: str, **data) -> dict:
    """worker → 前端：流式事件（chat 的 token/tool_start/complete 等）。"""
    return {"id": req_id, "type": "event", "event": event, "data": data}


def make_result(req_id: str, **data) -> dict:
    """worker → 前端：非流式操作的返回结果。"""
    return {"id": req_id, "type": "result", "data": data}


def make_done(req_id: str) -> dict:
    """worker → 前端：该请求的所有事件结束。"""
    return {"id": req_id, "type": "done"}


def make_error(req_id: str, message: str) -> dict:
    """worker → 前端：错误。"""
    return {"id": req_id, "type": "error", "message": message}


def new_request_id() -> str:
    """生成唯一请求 ID。"""
    return f"req-{uuid.uuid4().hex[:8]}"
