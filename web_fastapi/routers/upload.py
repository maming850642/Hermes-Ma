"""图片上传端点（多模态输入支持）。

前端粘贴/拖拽/📎选择图片 → POST /api/upload（FormData）→
写入 data/uploads/<user_id>/<uuid>.<ext> → 返回 image id + 预览 URL。

聊天请求里只传 image id（轻量字符串），不经 IPC 管道传 base64，
worker 侧 stream_invoke 再读盘转 data URI 发给 LLM（见 multimodal.py）。
"""
import uuid
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from fastapi.responses import FileResponse

from config import PROJECT_ROOT
from web_fastapi.dependencies import get_current_user_id
from web_fastapi.security import validate_user_id

logger = logging.getLogger("hermes.web.upload")
router = APIRouter()

# 上传根目录（与 multimodal.py 的 UPLOADS_DIR 保持一致）
UPLOADS_DIR = PROJECT_ROOT / "data" / "uploads"

# 大小上限 10MB（base64 后约 13MB，LLM 端点一般能接受）
_MAX_SIZE = 10 * 1024 * 1024
_ALLOWED_CONTENT_TYPES = {
    "image/png", "image/jpeg", "image/jpg", "image/gif",
    "image/webp", "image/bmp",
}
# MIME → 扩展名（用于落盘文件名）
_EXT_MAP = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


def _user_upload_dir(user_id: str) -> Path:
    """获取（并创建）用户的上传目录。"""
    d = UPLOADS_DIR / user_id
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.post("/upload")
async def upload_image(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user_id),
):
    """接收单张图片，写入 data/uploads/<user_id>/，返回 image id。"""
    ct = (file.content_type or "").lower()
    if ct not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"不支持的文件类型: {ct or '未知'}，仅支持图片",
        )

    # G1: 分块流式落盘（边读边限），超 10MB 立即中止 413——
    # 旧实现 await file.read() 整包进内存，超限检查在读完后才发生。
    ext = _EXT_MAP.get(ct, ".png")
    image_id = f"{uuid.uuid4().hex[:12]}{ext}"
    dest = _user_upload_dir(user_id) / image_id
    total = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail=f"图片过大（超过 {_MAX_SIZE // 1024 // 1024}MB 上限）",
                    )
                out.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="空文件")
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise

    logger.info(f"图片上传: user={user_id}, id={image_id}, size={total}, ct={ct}")
    return {
        "id": image_id,
        "url": f"/api/uploads/{user_id}/{image_id}",
        "filename": file.filename or image_id,
        "content_type": ct,
        "size": total,
    }


@router.get("/uploads/{user_id}/{image_id}")
async def get_uploaded_image(
    user_id: str,
    image_id: str,
):
    """读取并返回上传的图片（供前端预览缩略图）。

    注：此处不做严格鉴权（图片 id 为不可猜测的 uuid，且仅为预览用途）。
    """
    user_id = validate_user_id(user_id)
    # 防路径穿越：只取文件名
    safe_name = Path(image_id).name
    if safe_name != image_id:
        raise HTTPException(status_code=400, detail="非法 image id")
    path = UPLOADS_DIR / user_id / safe_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="图片不存在")

    # 复用标准 MIME 推断
    import mimetypes
    media_type, _ = mimetypes.guess_type(str(path))
    return FileResponse(path, media_type=media_type or "image/png")
