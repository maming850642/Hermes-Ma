"""
============================================
多模态消息构造（图片输入）
============================================
把用户上传的图片 id 列表 + 文本，构造为 OpenAI Vision 兼容的 content 列表，
供 user 消息 content 使用。

架构背景：
    LLM 端点（远程 vLLM/Qwen）无法访问 Web 服务器的 localhost URL，
    所以图片必须以 base64 data URI 形式嵌入消息。data URI 转换在
    worker 子进程内完成（不经 IPC 管道，避免 64KB buffer 压力）。

    上传文件存放于 data/uploads/<user_id>/<image_id>.<ext>，
    由 FastAPI 主进程的 /api/upload 端点写入（见 routers/upload.py）。

公共接口：
    build_user_content(text, user_id, images) → str | list[dict]
    extract_text(content) → str          # 从可能的多模态 content 中提取纯文本
"""
import base64
import logging
from pathlib import Path

from config import PROJECT_ROOT

logger = logging.getLogger("hermes.agent.multimodal")

# 上传根目录（与 data/sessions 同级，由 upload router 写入）
UPLOADS_DIR = PROJECT_ROOT / "data" / "uploads"

# 允许的图片扩展名（白名单，防路径穿越/恶意文件）
_ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# MIME 类型映射
_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def _resolve_upload_path(user_id: str, image_id: str) -> Path | None:
    """根据 user_id 和 image_id 定位上传文件。

    image_id 形如 "a1b2c3d4.png"（uuid + 扩展名）。只允许在
    UPLOADS_DIR/<user_id>/ 下查找，严格禁止 .. / 绝对路径等穿越手段。
    """
    if not image_id or not user_id:
        return None
    # 防穿越：去掉任何路径分隔符，只保留文件名部分
    safe_name = Path(image_id).name
    if safe_name != image_id:
        logger.warning(f"可疑 image_id（含路径）已拒绝: {image_id!r}")
        return None
    suffix = Path(safe_name).suffix.lower()
    if suffix not in _ALLOWED_EXTS:
        logger.warning(f"不允许的图片扩展名: {suffix!r}")
        return None
    return UPLOADS_DIR / user_id / safe_name


def _file_to_data_uri(path: Path) -> str | None:
    """读取图片文件，转 base64 data URI。"""
    try:
        raw = path.read_bytes()
    except Exception as e:
        logger.warning(f"读取上传图片失败 {path}: {e}")
        return None
    mime = _MIME_MAP.get(path.suffix.lower(), "image/png")
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def build_user_content(
    text: str,
    user_id: str,
    images: list[str] | None,
) -> str | list[dict]:
    """构造 HumanMessage 的 content。

    - 无图片时返回纯文本 str（保持与原有行为完全一致，不影响纯文本路径）。
    - 有图片时返回 OpenAI Vision 格式的 list：
        [
          {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
          {"type": "text", "text": "用户输入的文本"}
        ]
      图片在前、文本在后（多数 VLM 期望此顺序）。

    Args:
        text: 用户输入的文本部分
        user_id: 当前用户 id（定位 uploads 目录）
        images: image id 列表（如 ["a1b2.png", "c3d4.jpg"]），None/空则纯文本

    Returns:
        str（无图）或 list[dict]（有图）
    """
    if not images:
        return text

    content: list[dict] = []
    for img_id in images:
        path = _resolve_upload_path(user_id, img_id)
        if path is None or not path.exists():
            logger.warning(f"图片不存在或不可访问，已跳过: user={user_id}, id={img_id}")
            continue
        data_uri = _file_to_data_uri(path)
        if data_uri:
            content.append({
                "type": "image_url",
                "image_url": {"url": data_uri},
            })

    # 所有图片都解析失败 → 退化为纯文本
    if not content:
        return text

    # 文本块放最后（即使为空也保留，VLM 需要明确的文本指令）
    content.append({"type": "text", "text": text or ""})
    return content


def extract_text(content) -> str:
    """从可能的多模态 content 中提取纯文本。

    - content 是 str → 原样返回。
    - content 是 list（多模态）→ 拼接所有 type=="text" 块的 text。
    - 其它 → str(content) 兜底。

    用于 compact_messages / save_session 等假设 content 为 str 的旧代码路径，
    防止 AttributeError。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(content) if content is not None else ""
