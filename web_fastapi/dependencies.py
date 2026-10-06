"""FastAPI 依赖注入：获取当前用户、worker 进程。

免认证形态（ADR-0005 D5）：登录/cookie 整体移除，信任边界收敛为
「默认回环绑定 + 部署者自觉」。get_current_user_id 保留签名并常数化，
让全部路由的 Depends 链零改动；身份恒为 LOCAL_USER，worker 全局唯一。
"""
from fastapi import Request, Depends

from src.constants import LOCAL_USER
from web_fastapi.worker_manager import WorkerProcess


def get_worker_manager(request: Request):
    return request.app.state.worker_manager


def get_current_user_id(request: Request) -> str:
    """兼容垫片（原为 cookie 鉴权闸门）：直接返回本地单用户。

    保留函数签名的意义——所有路由的 Depends(get_current_user_id)
    无需翻动；未来若重新引入访问控制，只改这一个出口。
    X-User-Id 等请求头依旧不是身份标识（与既有安全测试语义一致）。
    """
    return LOCAL_USER


def get_worker(
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> WorkerProcess:
    """获取全局唯一的 worker 子进程（自动创建）。"""
    return request.app.state.worker_manager.get_or_create(user_id)
