"""请求级标识符校验，防御路径穿越与注入。

`user_id` / `session_id` 等会被直接拼入文件系统路径
（如 `SESSIONS_DIR / user_id / f"{session_id}.json"`）或子进程命令行，
因此必须在进入业务逻辑前拒绝任何含路径分隔符、`..`、空字节等危险片段的值。

允许的字符集：字母、数字、下划线、连字符、点、中文等非 ASCII 字符
（兼容用户用中文作为 user_id）。仅拒绝"路径语义上危险"的形态。
"""
import re
from fastapi import HTTPException

# 危险片段：路径分隔符（正反斜杠）、空字节、显式 `..` 段、冒号。
# 注意：按整体字符串检查，而非仅检查开头——`a/../b` 同样要拒。
# 冒号（R3-16）：chat 会话 ID 是 uuid 短串，从不含 ":"；`waker:`/`wakerflow:`
# 前缀的 ID 属于无人值守任务的私有事件流，必须在 Web 层拒绝（防 chat API
# 读写 waker 会话）。Windows 上 `:` 还是 NTFS ADS 保留字符（a:b.txt 会
# 解析成备用数据流），一并封禁。
_DANGEROUS = re.compile(r"[\\/]\u0000|\.\.|[\\/]|:|^\.|\u0000")


def _is_safe(value: str) -> bool:
    if not value or not value.strip():
        return False
    if _DANGEROUS.search(value):
        return False
    return True


def validate_user_id(value: str) -> str:
    """校验 user_id，非法时抛 HTTP 400。合法时返回 strip 后的值。"""
    value = (value or "").strip()
    if not _is_safe(value):
        raise HTTPException(status_code=400, detail="用户 ID 含非法字符")
    return value


def validate_id(value: str, name: str = "标识符") -> str:
    """校验通用标识符（session_id 等），非法时抛 HTTP 400。"""
    value = (value or "").strip()
    if not _is_safe(value):
        raise HTTPException(status_code=400, detail=f"{name} 含非法字符")
    return value
