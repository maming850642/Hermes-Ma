"""
============================================
WakerConfig —— 数字员工配置数据模型
============================================
一个 waker（数字员工）的全部配置 + 运行时状态，用单个 dataclass 表达。
持久化成 waker.yaml，config 与 state 分两个顶层 key（便于人工查看、
也便于 save_state 只改 state 节不动 config 节）。

设计要点：
- 配置字段（用户/管理接口可改）与状态字段（runner/scheduler 自动更新）
  在同一 dataclass，但 to_yaml_dict / from_yaml_dict 分节持久化。
- name 是目录名，必须目录安全字符；validate_name() 供 create/update 入口校验。
- 不做任何 IO，纯数据结构；IO 在 store.py。
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field, asdict


# 三个 persona md 的 (文件名, 小节标题) 映射，顺序即组装顺序。
# 单一来源：store.py 取文件名，persona.py 取完整元组，避免两处重复定义。
PERSONA_FILES: list[tuple[str, str]] = [
    ("IDENTITY.md", "核心职责（IDENTITY）"),
    ("PERSONA.md", "工作风格（PERSONA）"),
    ("BIBLE.md", "工作准则（BIBLE）"),
]

# 目录名安全字符：字母数字下划线短横，1-64 字符
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# 允许的调度类型
_SCHEDULE_TYPES = {"interval", "daily", "none"}

# 允许的权限模式（复用现有权限系统）
_PERMISSION_MODES = {"full_access", "before_changes", "plan"}


class WakerConfigError(ValueError):
    """WakerConfig 校验失败。"""


def validate_name(name: str) -> str:
    """校验 waker name，通过返回原值，否则抛 WakerConfigError。"""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise WakerConfigError(
            f"非法 waker name: {name!r}（须匹配 { _NAME_RE.pattern }）"
        )
    return name


# --------------------------------------------
# 配置字段名（持久化在 config: 节）
# --------------------------------------------
_CONFIG_FIELDS = (
    "name", "description", "enabled", "working_dir", "tools",
    "permission_mode", "task_prompt", "schedule_type",
    "interval_minutes", "daily_at", "api_enabled", "api_token",
    "max_runs", "expire_at",
)

# 状态字段名（持久化在 state: 节）
_STATE_FIELDS = ("run_count", "last_run_at", "last_status", "next_run_at")


@dataclass
class WakerConfig:
    """数字员工配置。

    Attributes:
        name: waker 唯一标识（目录名，须匹配 ^[a-zA-Z0-9_-]{1,64}$）
        description: 人类可读描述
        enabled: 是否启用（禁用时调度器跳过）
        working_dir: 绑定的项目工作目录（相对 workspace 根的路径，空=workspace 根）
        tools: 工具白名单（空=全部允许）
        permission_mode: 权限模式（full_access / before_changes / plan）
        task_prompt: 自动任务描述（检查范围、处理步骤、输出位置、成功标准）
        schedule_type: 调度类型（interval / daily / none）
        interval_minutes: interval 模式的间隔分钟数
        daily_at: daily 模式的触发时刻 "HH:MM"（本地时区）
        api_enabled: 是否允许 API 触发
        api_token: API 触发 token（创建时自动生成）
        max_runs: 最大运行次数（0=不限）
        expire_at: ISO 时间，过期后不再调度（空=无截止）

        运行状态（runner/scheduler 更新）：
        run_count: 已运行次数
        last_run_at: 上次运行的 ISO 时间
        last_status: 上次运行状态（"ok"/"error"/""）
        next_run_at: 下次计划运行的 ISO 时间
    """
    name: str = ""
    description: str = ""
    enabled: bool = False
    working_dir: str = ""
    tools: list[str] = field(default_factory=list)
    permission_mode: str = "before_changes"
    task_prompt: str = ""
    schedule_type: str = "interval"
    interval_minutes: int = 60
    daily_at: str = "09:00"
    api_enabled: bool = False
    api_token: str = ""
    max_runs: int = 0
    expire_at: str = ""

    # 状态字段
    run_count: int = 0
    last_run_at: str = ""
    last_status: str = ""
    next_run_at: str = ""

    def __post_init__(self) -> None:
        # name 在构造后由调用方显式校验；这里只做宽松保护，允许空（用于 from_dict）
        if self.name and not _NAME_RE.match(self.name):
            raise WakerConfigError(
                f"非法 waker name: {self.name!r}（须匹配 {_NAME_RE.pattern}）"
            )

    # ---------- 校验 ----------
    def validate(self) -> None:
        """全字段校验。创建/更新时调用。"""
        validate_name(self.name)
        if self.schedule_type not in _SCHEDULE_TYPES:
            raise WakerConfigError(
                f"非法 schedule_type: {self.schedule_type!r}（须为 {sorted(_SCHEDULE_TYPES)}）"
            )
        if self.permission_mode not in _PERMISSION_MODES:
            raise WakerConfigError(
                f"非法 permission_mode: {self.permission_mode!r}"
                f"（须为 {sorted(_PERMISSION_MODES)}）"
            )
        if self.schedule_type == "interval" and self.interval_minutes <= 0:
            raise WakerConfigError(
                f"interval 模式 interval_minutes 须为正整数，得到 {self.interval_minutes}"
            )
        if self.schedule_type == "daily":
            _parse_hhmm(self.daily_at)

    def new_api_token(self) -> str:
        """生成并保存一个新的 api_token，返回它。"""
        self.api_token = secrets.token_urlsafe(24)
        return self.api_token

    # ---------- 序列化 ----------
    def to_dict(self) -> dict:
        """扁平 dict（配置 + 状态混在一起）。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "WakerConfig":
        """从扁平 dict 重建（忽略未知键）。"""
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in d.items() if k in known}
        return cls(**kwargs)

    def to_yaml_dict(self) -> dict:
        """分节 dict：{config: {...}, state: {...}}，用于写 waker.yaml。"""
        cfg = {}
        for f in _CONFIG_FIELDS:
            cfg[f] = getattr(self, f)
        state = {}
        for f in _STATE_FIELDS:
            state[f] = getattr(self, f)
        return {"config": cfg, "state": state}

    @classmethod
    def from_yaml_dict(cls, d: dict) -> "WakerConfig":
        """从分节 dict 重建（to_yaml_dict 的逆）。

        缺失 config/state 节时按空字典处理（向后兼容）。
        """
        cfg = d.get("config", {}) or {}
        state = d.get("state", {}) or {}
        merged = {**cfg, **state}
        return cls.from_dict(merged)


def _parse_hhmm(s: str) -> tuple[int, int]:
    """解析 "HH:MM"，返回 (hour, minute)。非法抛 WakerConfigError。"""
    if not isinstance(s, str):
        raise WakerConfigError(f"daily_at 须为 'HH:MM' 字符串，得到 {s!r}")
    parts = s.split(":")
    if len(parts) != 2:
        raise WakerConfigError(f"daily_at 格式错（须 'HH:MM'），得到 {s!r}")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        raise WakerConfigError(f"daily_at 时分须为整数，得到 {s!r}")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise WakerConfigError(f"daily_at 越界，得到 {s!r}")
    return h, m
