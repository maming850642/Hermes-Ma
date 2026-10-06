"""
SSE（Server-Sent Events）编码工具。

把 agent.stream_invoke 的事件 dict 编码为 SSE 字节流，
供 FastAPI StreamingResponse 推送。

可恢复续流：帧可带 `id: <seq>` 行（chat_bus 的 per-session 单调序号）。
EventSource 断线重连时自动经 Last-Event-ID 请求头回传最后收到的 seq，
GET /api/chat/stream/{sid} 据此补差（seq > after 的缓冲帧）。
"""
import json


def encode_sse_event(event_type: str, data: dict,
                     seq: int | None = None) -> str:
    """编码单个 SSE 事件（ensure_ascii 保持中文字节对齐测试稳定）。

    seq：可选帧序号 → `id:` 行（POST 主路径与 GET 订阅共用总线 seq
    空间；busy 等请求私有帧无 seq，不带 id 行）。缺省 None 保持旧格式。
    """
    id_line = f"id: {seq}\n" if seq is not None else ""
    return (f"{id_line}event: {event_type}\n"
            f"data: {json.dumps(data, ensure_ascii=True)}\n\n")
