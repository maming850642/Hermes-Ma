"""
============================================
ModelRegistry —— 模型档案注册表（单 JSON 文档存储）
============================================
管理可运行时新增/切换的多个模型档案（LLM endpoint 配置）。每个档案：

    id              稳定标识（^[a-z0-9][a-z0-9_-]{0,31}$，可从 display 推导）
    display         显示名（必填非空，自由文本）
    model           模型名（必填，如 gpt-4o / deepseek-chat）
    base_url        API 根地址（必填，如 https://api.example.com/v1）
    api_key         鉴权 key（可空；只落盘，任何展示出口不回显——
                    REST 层只给 has_key 布尔，内部消费走 resolve_profile）
    context_window  上下文窗口（可空 = 跟随全局配置）

存储：data/home/model_profiles.json（路径取 paths.agent_home，不硬编码），
结构 {"profiles": [{...}, ...]}。写纪律仿 session_store._atomic_write_json：
模块级锁 + tmp 文件（带进程号+纳秒戳）+ os.replace 原子替换。

## 与并行模块的分工
- web_fastapi/routers/models.py：REST 门面（/api/models），api_key 一律掩码
- agent_v3 / worker 侧：切换模型用 resolve_profile(id) 取完整三元组
  （model / base_url / api_key）
- 删除正在使用的档案由调用方负责回退，本模块只如实删除并返回被删档案
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from src.storage import paths

logger = logging.getLogger("hermes.model_registry")

# id 白名单：小写字母/数字开头，后续允许 _ -；总长 ≤32
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
#: id 去重后缀上限（base, base-2 ... base-99）
_MAX_DEDUPE_SUFFIX = 99
#: update_profile(changes) 允许的字段（api_key 走专用参数，不进 changes）
_ALLOWED_CHANGE_KEYS = frozenset({"display", "model", "base_url", "context_window"})

#: update_profile 的 api_key 缺省哨兵：传它（或 None/空串）= 保持原值
API_KEY_UNCHANGED = object()

# 读改写互斥（单 JSON 文档，进程内多线程共享一份盘上文件）
_LOCK = threading.Lock()


class ModelProfileError(ValueError):
    """档案字段校验失败。消息中文、可直接展示给 UI（ValueError→400）。"""


# ============================================
# 内部：路径 / 原子写 / 读写
# ============================================
def _profiles_file() -> Path:
    """档案文件路径（data/home/model_profiles.json）。每次现算——
    测试用 paths.set_data_root 重定向数据根。"""
    return paths.agent_home("model_profiles.json")


def _now() -> str:
    """ISO 时间戳（秒级，与 waker store 同风格）。"""
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write_json(file_path: Path, data: dict) -> None:
    """tmp 同目录写入 + os.replace 原子替换（仿 session_store._atomic_write_json）。

    tmp 名带进程号+纳秒时间戳，多进程互不踩 tmp；写失败（含 rename 阶段）
    时旧文件原样保留，finally 清掉残留 tmp。
    """
    tmp_path = file_path.with_name(
        f"{file_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, file_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _load() -> dict:
    """读整档。文件缺失/损坏返回空结构（不建目录不落盘）。"""
    p = _profiles_file()
    if not p.exists():
        return {"profiles": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"model_profiles.json 解析失败，按空档案处理: {e}")
        return {"profiles": []}
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
        return {"profiles": []}
    return data


def _save(data: dict) -> None:
    """整档原子写回（父目录自动创建）。"""
    p = _profiles_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(p, data)


# ============================================
# 校验
# ============================================
def validate_profile_id(profile_id: str) -> str:
    """校验并返回规范 id；非法抛 ModelProfileError。"""
    s = (profile_id or "").strip()
    if not _ID_RE.match(s):
        raise ModelProfileError(
            f"非法档案标识: {profile_id!r}（仅允许小写字母/数字开头，"
            f"字符集 a-z 0-9 _ -，长度 ≤32）")
    return s


def _validate_fields(display, model, base_url, context_window) -> tuple:
    """创建/更新共用的字段校验。返回规范化后的四元组。

    display/model/base_url 必填非空；context_window 空（None/""）= 跟随
    全局，否则必须是正整数。
    """
    display = (display or "").strip()
    if not display:
        raise ModelProfileError("显示名不能为空")
    model = (model or "").strip()
    if not model:
        raise ModelProfileError("模型名不能为空")
    base_url = (base_url or "").strip()
    if not base_url:
        raise ModelProfileError("base_url 不能为空")
    if context_window in ("", None):
        context_window = None
    else:
        try:
            context_window = int(context_window)
        except (TypeError, ValueError):
            raise ModelProfileError(f"context_window 必须是整数: {context_window!r}")
        if context_window <= 0:
            raise ModelProfileError(f"context_window 必须为正整数: {context_window}")
    return display, model, base_url, context_window


def slug_from_display(display: str) -> str:
    """由显示名推导候选 id（projects_store.default_slug_for 同思路）。

    ASCII 可映射 → 折叠为白名单字符（截到 32 位）；产出为空/含非 ASCII
    （中文名不做丢语义转写）→ mp-<6hex> 随机段。
    """
    raw = (display or "").strip().lower()
    if raw and all(ord(ch) < 128 for ch in raw):
        folded = re.sub(r"[^a-z0-9_-]+", "-", raw)[:32].strip("-_")
        if _ID_RE.match(folded):
            return folded
    return f"mp-{uuid.uuid4().hex[:6]}"


def _unique_id(base: str, existing: set) -> str:
    """base 不冲突即用；否则追加 -2..-99（projects_store.unique_slug 同思路）。"""
    for suffix in range(1, _MAX_DEDUPE_SUFFIX):
        test = base if suffix == 1 else f"{base}-{suffix}"
        if test not in existing:
            return test
    raise ModelProfileError(f"档案标识空间耗尽: {base}")


# ============================================
# 公开 API
# ============================================
def list_profiles() -> list[dict]:
    """全部档案（按创建顺序）。返回副本，调用方改动不影响落盘数据。"""
    with _LOCK:
        return [dict(p) for p in _load()["profiles"]]


def get_profile(profile_id: str) -> dict | None:
    """按 id 取档案（含 api_key，供内部读）。不存在/非法 id 返回 None。"""
    try:
        validate_profile_id(profile_id)
    except ModelProfileError:
        return None
    with _LOCK:
        for p in _load()["profiles"]:
            if p.get("id") == profile_id:
                return dict(p)
    return None


def add_profile(display: str, model: str, base_url: str,
                api_key: str = "", context_window=None,
                profile_id: str = "") -> dict:
    """新增档案。id 缺省从 display 推导并去重；字段非法/id 冲突抛
    ModelProfileError。返回新档案 dict（含 api_key——只进内存/落盘，
    调用方不得原样外发）。"""
    display, model, base_url, context_window = _validate_fields(
        display, model, base_url, context_window)
    with _LOCK:
        data = _load()
        existing = {p.get("id") for p in data["profiles"]}
        if (profile_id or "").strip():
            clean_id = validate_profile_id(profile_id)
            if clean_id in existing:
                raise ModelProfileError(f"档案标识已存在: {clean_id}")
        else:
            clean_id = _unique_id(slug_from_display(display), existing)
        now = _now()
        profile = {
            "id": clean_id,
            "display": display,
            "model": model,
            "base_url": base_url,
            "api_key": (api_key or "").strip(),
            "context_window": context_window,
            "created_at": now,
            "updated_at": now,
        }
        data["profiles"].append(profile)
        _save(data)
    logger.info(f"模型档案已创建: {clean_id} (model={model})")
    return dict(profile)


def update_profile(profile_id: str, changes: dict,
                   api_key=API_KEY_UNCHANGED) -> dict | None:
    """更新档案（只改传入字段）。不存在返回 None；非法 id 抛
    ModelProfileError。

    Args:
        changes: 允许键 display / model / base_url / context_window；
            未出现的字段保持原值。携带其他键（含 api_key）抛
            ModelProfileError——key 更新必须走专用参数。
        api_key: 新 key；缺省哨兵 / None / 空串 = 保持原值。
    """
    validate_profile_id(profile_id)
    bad = set(changes or {}) - _ALLOWED_CHANGE_KEYS
    if bad:
        raise ModelProfileError(f"不允许更新的字段: {', '.join(sorted(bad))}")
    with _LOCK:
        data = _load()
        target = None
        for p in data["profiles"]:
            if p.get("id") == profile_id:
                target = p
                break
        if target is None:
            return None
        # 新旧合成完整四元组再过校验（老值按不变式必合法，故等价于
        # 只校验改动字段，同时拦截 display→"" 这类清空写法）。
        # target.get 双兜底：手写/旧版 JSON 可能缺字段，[...] 直接 KeyError
        # 会把一次可修复的更新变成 500——缺失按"未配置"（None）交给校验
        # 给出可读错误。
        display, model, base_url, context_window = _validate_fields(
            changes.get("display", target.get("display")),
            changes.get("model", target.get("model")),
            changes.get("base_url", target.get("base_url")),
            changes.get("context_window", target.get("context_window")),
        )
        target.update({
            "display": display,
            "model": model,
            "base_url": base_url,
            "context_window": context_window,
            "updated_at": _now(),
        })
        if api_key is not API_KEY_UNCHANGED and api_key:
            target["api_key"] = str(api_key).strip()
        _save(data)
        logger.info(f"模型档案已更新: {profile_id}")
        return dict(target)


def delete_profile(profile_id: str) -> dict | None:
    """删除档案，返回被删档案（正在使用中的回退由调用方处理）。
    不存在返回 None；非法 id 抛 ModelProfileError。"""
    validate_profile_id(profile_id)
    with _LOCK:
        data = _load()
        for i, p in enumerate(data["profiles"]):
            if p.get("id") == profile_id:
                removed = data["profiles"].pop(i)
                _save(data)
                logger.info(f"模型档案已删除: {profile_id}")
                return dict(removed)
    return None


def resolve_profile(profile_id: str) -> dict | None:
    """解析档案为完整配置（含 api_key 明文），供内部消费
    （agent_v3 / worker 切换模型取 model / base_url / api_key 三元组）。

    与 get_profile 同形，但语义出口不同：本函数是「内部消费」入口，
    允许拿明文 key；REST 展示一律走 get_profile + 路由层掩码。
    不存在/非法 id 返回 None（调用方按未配置档案降级）。
    """
    return get_profile(profile_id)
