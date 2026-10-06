"""模型档案（Model Profile）管理 API。

落盘在 src/model_registry.py（data/home/model_profiles.json，单 JSON
文档 + 模块锁 + 原子写）。本路由只做门面：字段校验、api_key 掩码
（任何响应不回显 key 明文，只给 has_key 布尔）与连通性自测。

全部路由同步 def——文件 IO 与 test 端点的 httpx 阻塞调用都不进
async def（FastAPI 自动丢线程池，遵守本项目「凡阻塞调用不进 async
def」的规约）。
"""
import logging
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.model_registry import (
    API_KEY_UNCHANGED,
    ModelProfileError,
    add_profile,
    delete_profile,
    list_profiles,
    resolve_profile,
    update_profile,
)

logger = logging.getLogger("hermes.web.models")
router = APIRouter()


# ============================================
# 请求体（字段不给 pydantic 硬约束——缺省/空串交给 store 校验，
# 统一走 ModelProfileError → 400，避免 pydantic 422 语义漂移）
# ============================================
class ProfileCreateBody(BaseModel):
    """新建模型档案。display/model/base_url 必填；id 缺省从 display 推导。"""
    display: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    context_window: int | None = None
    id: str = ""


class ProfileUpdateBody(BaseModel):
    """更新模型档案。全字段可选；api_key 缺省/空串 = 保持原值。"""
    display: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    context_window: int | None = None


# ============================================
# 辅助
# ============================================
def _masked(p: dict) -> dict:
    """档案 → 对外视图：去掉 api_key 只给 has_key（绝不明文回显）。"""
    return {
        "id": p.get("id", ""),
        "display": p.get("display", ""),
        "model": p.get("model", ""),
        "base_url": p.get("base_url", ""),
        "context_window": p.get("context_window"),
        "has_key": bool(p.get("api_key")),
        "created_at": p.get("created_at", ""),
    }


def _validate_base_url_scheme(base_url: str, *, where: str) -> str:
    """base_url 只允许 http/https（防 file://、gopher:// 等被 test 端点当
    请求目标或被 SSRF 面利用的非常规 scheme 经档案固化）。违规 → 400。"""
    scheme = (urlsplit(base_url or "").scheme or "").lower()
    if scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail=f"base_url 必须以 http:// 或 https:// 开头（{where}: {base_url!r}）")
    return base_url


def _status_detail(status: int) -> str:
    """test 端点非 200 状态码的简短中文说明。"""
    if status == 401:
        return "认证失败（api_key 无效或缺失）"
    if status == 403:
        return "无访问权限（403）"
    if status == 404:
        return "端点不存在（检查 base_url 是否以 /v1 结尾）"
    return f"服务返回异常状态码: {status}"


# ============================================
# CRUD
# ============================================
@router.get("")
def list_items():
    """列出全部模型档案。api_key 不回显，只给 has_key。"""
    return [_masked(p) for p in list_profiles()]


@router.post("", status_code=201)
def create_item(body: ProfileCreateBody):
    """新建档案，返回掩码视图（201）。字段校验失败 → 400。"""
    _validate_base_url_scheme(body.base_url, where="新建档案")
    try:
        p = add_profile(
            display=body.display,
            model=body.model,
            base_url=body.base_url,
            api_key=body.api_key,
            context_window=body.context_window,
            profile_id=body.id,
        )
    except ModelProfileError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _masked(p)


@router.put("/{id}")
def update_item(id: str, body: ProfileUpdateBody):
    """更新档案。api_key 缺省/空串 = 保持原值；context_window 显式
    传 null = 清空（跟随全局）。不存在 → 404。"""
    # exclude_unset：只把请求里真出现的字段交给 store（区分「没传」
    # 与「显式 null 清空 context_window」）
    changes = body.model_dump(exclude_unset=True, exclude={"api_key"})
    if "base_url" in changes:
        _validate_base_url_scheme(changes["base_url"], where="更新档案")
    new_key = body.api_key or API_KEY_UNCHANGED
    try:
        p = update_profile(id, changes, api_key=new_key)
    except ModelProfileError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if p is None:
        raise HTTPException(status_code=404, detail=f"模型档案不存在: {id}")
    return _masked(p)


@router.delete("/{id}")
def delete_item(id: str):
    """删除档案，返回被删档案（掩码）。不存在 → 404。

    档案正被会话/waker 使用时的回退由调用方（worker 侧）处理，这里
    如实删除。
    """
    try:
        p = delete_profile(id)
    except ModelProfileError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if p is None:
        raise HTTPException(status_code=404, detail=f"模型档案不存在: {id}")
    return _masked(p)


# ============================================
# 连通性自测
# ============================================
@router.post("/{id}/test")
def test_item(id: str, confirm_send_key: bool = False):
    """连通性自测：GET {base_url}/models。

    默认（confirm_send_key=False）只测连通性，绝不带 Authorization 头——
    存档 key 只应发给"用户确认要用它"的请求（key 转发面收口：档案的
    base_url 可被更新指向任意主机，若 test 无条件带 Bearer，改个 URL 就
    能把 key 转发到攻击者服务器）。confirm_send_key=True 时带 Bearer key
    （key 空则不带头），前端 UI 提示"将使用已存 Key 测试"后显式携带。
    base_url 非 http/https → 400。

    HTTP 200 → ok=True + 前 10 个模型 id；其他状态码 → ok=False 带状态码；
    网络异常/超时 → ok=False、status=None、detail 带中文原因。
    """
    p = resolve_profile(id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"模型档案不存在: {id}")
    base_url = (p.get("base_url") or "").rstrip("/")
    _validate_base_url_scheme(base_url, where="连通性测试")
    url = f"{base_url}/models"
    headers = {}
    if confirm_send_key and p.get("api_key"):
        headers["Authorization"] = f"Bearer {p['api_key']}"
    try:
        r = httpx.get(url, headers=headers, timeout=10.0)
    except httpx.TimeoutException:
        return {"ok": False, "status": None,
                "detail": "连接超时（10 秒无响应）", "models": []}
    except httpx.HTTPError as e:
        return {"ok": False, "status": None,
                "detail": f"网络异常: {type(e).__name__}", "models": []}
    if r.status_code != 200:
        return {"ok": False, "status": r.status_code,
                "detail": _status_detail(r.status_code), "models": []}
    models: list[str] = []
    try:
        data = r.json().get("data") or []
    except ValueError:
        data = []
    for item in data:
        if isinstance(item, dict) and item.get("id"):
            models.append(str(item["id"]))
    return {"ok": True, "status": 200, "detail": "连接成功",
            "models": models[:10]}
