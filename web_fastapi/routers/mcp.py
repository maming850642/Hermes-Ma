"""MCP 服务器管理 API。通过 IPC 发给 worker 子进程。

worker.send 会同步抢 per-worker 锁（最多等 lock_wait=5s，mcp_reload 触发
重连更久），凡直接调用它的 handler 一律声明为同步 def——FastAPI 把同步
路由丢进线程池执行；async def 里调它会把整个 uvicorn 事件循环卡住
（P2-11 同源，见 memory.py 模块头注释）。
"""
import logging
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from web_fastapi.dependencies import get_worker
from web_fastapi.worker_manager import WorkerProcess
from web_fastapi.models import EnabledBody

logger = logging.getLogger("hermes.web.mcp")
router = APIRouter()


class McpServerBody(BaseModel):
    """新建 MCP server 的完整配置(JSON 表单)。"""
    name: str
    enabled: bool = True
    transport: str = "stdio"
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    url: str = ""
    headers: dict[str, str] = {}


@router.get("/servers")
def list_servers(worker: WorkerProcess = Depends(get_worker)):
    # 同步 def：worker.send 阻塞等待须发生在线程池（见模块 docstring）
    events = worker.send("mcp_list")
    return events[0]["data"] if events else {"servers": [], "connected": 0, "total_tools": 0}


@router.post("/servers")
def add_server(body: McpServerBody,
               worker: WorkerProcess = Depends(get_worker)):
    """新建 MCP server(写一个 mcp_<name>.json 文件)。"""
    events = worker.send("mcp_add", config=body.model_dump())
    return events[0]["data"] if events else {"ok": False}


@router.post("/reload")
def reload_servers(worker: WorkerProcess = Depends(get_worker)):
    """重新扫描 mcp_servers/ 文件夹,加载新增的 server。"""
    events = worker.send("mcp_reload")
    return events[0]["data"] if events else {"ok": False}


@router.patch("/servers/{name}/enabled")
def set_enabled(name: str, body: EnabledBody,
                worker: WorkerProcess = Depends(get_worker)):
    events = worker.send("mcp_set_enabled", name=name, enabled=body.enabled)
    return events[0]["data"] if events else {"ok": False}


@router.delete("/servers/{name}")
def remove_server(name: str,
                  worker: WorkerProcess = Depends(get_worker)):
    events = worker.send("mcp_remove", name=name)
    return events[0]["data"] if events else {"ok": False}
