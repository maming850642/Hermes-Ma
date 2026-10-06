"""认证 API —— 免认证形态下的兼容垫片（ADR-0005 D5）。

login/logout 已随认证移除（原语义：无密码门禁 + 签名 cookie）。
保留 GET /me 且恒返本地单用户——它是全部前端页面的登录态探测点，
公开可达让既有启动脚本零改动。worker 冷启动交给首次 API 调用的
get_worker 自动拉起。
"""
from fastapi import APIRouter, Depends

from src.constants import LOCAL_USER
from web_fastapi.dependencies import get_current_user_id

router = APIRouter()


@router.get("/me")
async def me(user_id: str = Depends(get_current_user_id)):
    # 兼容垫片：免认证后所有匿名访问也恒为本地单用户
    return {"user_id": LOCAL_USER}
