"""Pydantic 请求/响应模型。"""
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    # 单用户坍缩：user_id 不再承载身份（恒 LOCAL_USER），字段保留仅为
    # 兼容仍传 user_id 的旧客户端；非空时仍做路径安全校验
    # （web_fastapi.security.validate_user_id，login 路由把关）。
    user_id: str = Field(default="", max_length=64)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    thinking: bool = False
    images: list[str] = Field(default_factory=list)  # image id 列表（多模态输入）
    session_id: str = ""  # 会话分桶 key（多标签页隔离）
    waker: str = ""  # 会话绑定的 waker 名（空=默认助手；非空时注入 waker 人格）


class ApprovalRequest(BaseModel):
    thread_id: str
    decision: str = Field(..., pattern="^(approve|reject)$")
    reason: str = ""


class StopRequest(BaseModel):
    """停止生成（M5：带会话标识实现精确取消，只停目标槽）。"""
    session_id: str = ""


class RenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)


class PrefsUpdate(BaseModel):
    workspace_root: str | None = None
    web_proxy: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    memory_min_score: float | None = None
    max_memory_results: int | None = None
    compact_threshold_pct: int | None = None


class SystemConfigUpdate(BaseModel):
    """系统配置更新：key→value，仅允许 system 类键。"""
    updates: dict[str, str]


class PermissionModeUpdate(BaseModel):
    """V3 权限模式切换：full_access / before_changes / plan。"""
    mode: str = Field(..., pattern="^(full_access|before_changes|plan)$")


class WakerSelectBody(BaseModel):
    """会话 waker 人格切换：name 空串=默认助手。

    session_id 可选（P2-14）：提供时按会话亲和路由到该会话的 worker 槽，
    waker 落到该会话的桶；缺省维持 main 槽旧行为。
    """
    name: str = ""
    session_id: str = ""


class ModelSwitchBody(BaseModel):
    """会话模型热切换：profile_id 空串=回退 config.yaml 默认模型。

    session_id 可选：提供时按会话亲和路由到该会话的 worker 槽（该会话
    下一轮生效）；缺省广播全部存活 worker（全局切换）。
    """
    profile_id: str = ""
    session_id: str = ""


class EnabledBody(BaseModel):
    enabled: bool


# ---- Waker（数字员工）管理 ----
class WakerCreateBody(BaseModel):
    """新建 waker 的请求体。

    name 必填；其余可选，缺省由 WakerConfig 默认值兜底。
    enabled 缺省为 False（创建后由用户显式启用）。
    api_token 由后端自动生成（不接受前端传入）。
    """
    name: str = Field(..., min_length=1, max_length=64)
    description: str | None = None
    identity: str | None = None   # 写 IDENTITY.md
    persona: str | None = None    # 写 PERSONA.md
    bible: str | None = None       # 写 BIBLE.md
    working_dir: str | None = None
    tools: list[str] | None = None
    permission_mode: str | None = None
    task_prompt: str | None = None
    schedule_type: str | None = None
    interval_minutes: int | None = None
    daily_at: str | None = None
    api_enabled: bool | None = None
    max_runs: int | None = None
    expire_at: str | None = None


class WakerUpdateBody(BaseModel):
    """更新 waker 的请求体。全字段可选；不含 name（路径参数给）。"""
    description: str | None = None
    identity: str | None = None
    persona: str | None = None
    bible: str | None = None
    working_dir: str | None = None
    tools: list[str] | None = None
    permission_mode: str | None = None
    task_prompt: str | None = None
    schedule_type: str | None = None
    interval_minutes: int | None = None
    daily_at: str | None = None
    api_enabled: bool | None = None
    max_runs: int | None = None
    expire_at: str | None = None


class WakerInvokeBody(BaseModel):
    """手动触发 waker 的请求体。prompt 可选。"""
    prompt: str | None = None


# ---- WakerFlow（编排）管理 ----
class FlowCreateBody(BaseModel):
    """新建 WakerFlow 的请求体。

    两种创建方式二选一：
    - yaml：直接给 YAML 文本（parser 校验）
    - blocks：给积木块 JSON（canvas 转换层 → flow_to_yaml）
    schedule 字段可选，仅 blocks 模式生效（yaml 模式由 YAML 文本自带）。
    """
    name: str = Field(..., min_length=1, max_length=64)
    yaml: str | None = None
    blocks: list[dict] | None = None
    description: str = ""
    inputs: list[dict] | None = None
    returns: dict | None = None
    # 调度配置（可选）
    enabled: bool | None = None
    schedule_type: str | None = None
    interval_minutes: int | None = None
    daily_at: str | None = None
    api_enabled: bool | None = None
    max_runs: int | None = None
    expire_at: str | None = None


class FlowUpdateBody(BaseModel):
    """更新 WakerFlow 的请求体（name 由路径参数给）。

    yaml 与 blocks 二选一：yaml 直接覆盖文本；blocks 走 canvas 转换层。
    schedule 字段可选，仅 blocks 模式生效。
    """
    yaml: str | None = None
    blocks: list[dict] | None = None
    description: str | None = None
    inputs: list[dict] | None = None
    returns: dict | None = None
    # 调度配置（可选）
    enabled: bool | None = None
    schedule_type: str | None = None
    interval_minutes: int | None = None
    daily_at: str | None = None
    api_enabled: bool | None = None
    max_runs: int | None = None
    expire_at: str | None = None


class FlowBlocksBody(BaseModel):
    """积木块编辑器保存的请求体（新建用，name 必填）。

    blocks = 积木块 JSON 列表（DOM 顺序 = steps 顺序）。
    后端走 canvas.blocks_to_flow → flow_to_yaml → FlowStore.save。
    """
    name: str = Field(..., min_length=1, max_length=64)
    blocks: list[dict] = Field(default_factory=list)
    description: str = ""
    inputs: list[dict] | None = None
    returns: dict | None = None


class FlowInvokeBody(BaseModel):
    """手动触发 flow 运行的请求体。inputs 可选。"""
    inputs: dict = Field(default_factory=dict)


class FlowApproveBody(BaseModel):
    """提交审批响应的请求体。answer 为用户选中的 option value。"""
    answer: str


# ---- Workspace（T5 挂载 + 双模式）----
class MountLocalBody(BaseModel):
    """挂载本地文件夹的请求体。display_name 可选（缺省用目录名）。"""
    path: str = Field(..., min_length=1)
    display_name: str = ""
