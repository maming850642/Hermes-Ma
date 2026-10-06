"""
============================================
IPC op 协议契约（TypedDict 化的 op payload schema）
============================================

用途
----
web_fastapi（父进程）与 worker_process（子进程）之间是 NDJSON 信封协议
（信封编解码见 web_fastapi/ipc.py = 父侧构造器 / src/ipc.py = 子侧 stdio
读写设施）。信封里 ``{"id", "op", **payload}`` 的 payload 此前是裸 dict、
无 schema，router 与 worker 靠字符串字面量对齐——改一侧漏一侧只有运行期
（甚至肉眼 review）才能发现。

本模块是 **op payload 键位的唯一登记处**：

- 每个 op 一个 TypedDict：必选键用 ``total=True`` 基类（信封键 id/op），
  可选 payload 键用 ``total=False`` 子类——"total=False 风格区分必选/可选"。
- ``OP_PAYLOAD_KEYS``：op → 全部合法键（含信封键 id/op），供契约测试
  tests/test_web_fastapi/test_ipc_op_contract.py 消费。该测试用 AST 双向
  扫描锁定两件事：
    1) 接收方（worker_process.py 的 handle_command 分支 + OP_HANDLERS
       各 handler）读到的键 ⊆ 声明；
    2) 发送方（web_fastapi/routers/ 与 worker_manager.py 构造各 op
       payload 的字面量键）⊆ 声明。
  任何一侧漂移，测试失败信息会直接指出是哪一侧哪个键。

维护约定
--------
1. **新增 op 必须先在本模块登记**（TypedDict + OP_PAYLOAD_TYPES 各一条），
   再写 worker 分支 / router 调用点——否则契约测试直接红。
2. **改键同理**：worker 想多读一个键、router 想多发一个键，先在对应
   TypedDict 加字段；删键时两侧同步删。
3. 声明的是"线上实际流转的全部合法键"= worker 读取键 ∪ 发送方发送键。
   某键只有一侧在用也要登记（并注明另一侧语义），例如 llm_params_set 的
   session_id：主进程亲和路由携带，worker 端当前不读。
4. 已无发送方的 op（如 memory_get / sessions_list，主进程已改直连存储）
   仍保留登记——worker 分支还在，属于协议面的一部分，删 op 时一起清。

键位速查（payload 键，信封键 id/op 省略；●=发送方在发，○=仅 worker 读取）
    chat               message● images● thinking● session_id● waker● prelogged●
    chat_approve       thread_id● decision● reason●
    chat_stop          （无 payload）
    memory_get         （无 payload；无活跃发送方）
    memory_clear       （无 payload；无活跃发送方）
    sessions_list      project○（无活跃发送方，主进程直读磁盘）
    current_session    session_id●
    session_load       session_id●
    session_save       session_id●
    session_reset      new_sid●
    session_rename     session_id● name●
    session_delete     session_id●
    session_summary    session_id●
    tools_list         （无 payload）
    skills_list        （无 payload）
    compact            session_id●
    health             （无 payload）
    mcp_list           （无 payload）
    mcp_set_enabled    name● enabled●
    mcp_remove         name●
    mcp_add            config●（McpServerConfig.from_dict 的字典）
    mcp_reload         （无 payload）
    prefs_get          （无 payload）
    prefs_set          prefs●（七键偏好字典）
    permission_mode_set mode●
    permission_mode_get（无 payload）
    waker_set          name● session_id●
    waker_get          session_id●
    llm_params_set     model● base_url● api_key● context_window●
                       clear_model_overrides● session_id●（仅路由用，worker 不读）
    settings_update    updates●（settings 键值字典）
    waker_run          name● run_id● api_prompt●（src/waker/scheduler.py 旧 IPC 路径）
    workspace_changed  （无 payload）
    exit               （无 payload）
"""
from __future__ import annotations

from typing import Any, TypedDict


# ---------------------------------------------------------------------------
# 信封必选键（所有 op 共用，total=True）
# ---------------------------------------------------------------------------
class _CmdEnvelope(TypedDict):
    """NDJSON 请求信封必选键：make_request(req_id, op, **kwargs) 的前两项。"""

    id: str    # 请求关联 id（响应事件按它配对）
    op: str    # 操作名


# ---------------------------------------------------------------------------
# 各 op 的 payload TypedDict（total=False：payload 键均可选下发）
# ---------------------------------------------------------------------------
class ChatCmd(_CmdEnvelope, total=False):
    """chat：对话主命令（SSE 流式）。"""

    message: str            # 用户消息文本
    images: list[str]       # 多模态 image id 列表
    thinking: bool          # 思考模式开关
    session_id: str         # 会话分桶 key（多标签页隔离）
    waker: str              # 本次请求显式指定的人格（空=沿用会话桶记忆）
    prelogged: bool         # 主进程已预写 turn/start+user/message，worker 跳过重写


class ChatApproveCmd(_CmdEnvelope, total=False):
    """chat_approve：HITL 审批恢复轮。"""

    thread_id: str          # === session_id（审批恢复路由键）
    decision: str           # approve / reject
    reason: str             # 拒绝理由（decision=reject 时拼接进 resume_payload）


class ChatStopCmd(_CmdEnvelope, total=False):
    """chat_stop：软中断当前对话（无 payload；可内联/读取线程直通）。"""


class MemoryGetCmd(_CmdEnvelope, total=False):
    """memory_get：全量记忆（无活跃发送方；主进程已改直连存储）。"""


class MemoryClearCmd(_CmdEnvelope, total=False):
    """memory_clear：清空记忆（无活跃发送方；主进程已改直连存储）。"""


class SessionsListCmd(_CmdEnvelope, total=False):
    """sessions_list：会话列表（无活跃发送方；主进程直读磁盘快照）。"""

    project: str | None     # 按项目过滤


class CurrentSessionCmd(_CmdEnvelope, total=False):
    """current_session：恢复当前会话（标签页恢复，可带 sid 懒水合）。"""

    session_id: str


class SessionLoadCmd(_CmdEnvelope, total=False):
    """session_load：显式加载指定会话。"""

    session_id: str


class SessionSaveCmd(_CmdEnvelope, total=False):
    """session_save：保存指定会话桶（缺省当前桶）。"""

    session_id: str


class SessionResetCmd(_CmdEnvelope, total=False):
    """session_reset：落盘旧桶 + 切新桶。"""

    new_sid: str            # 主进程统一生成下发的新 sid（缺省 worker 本地生成）


class SessionRenameCmd(_CmdEnvelope, total=False):
    """session_rename：改目标会话名。"""

    session_id: str
    name: str


class SessionDeleteCmd(_CmdEnvelope, total=False):
    """session_delete：删会话文件 + 清事件流 + 弹内存桶。"""

    session_id: str


class SessionSummaryCmd(_CmdEnvelope, total=False):
    """session_summary：按需生成会话总结（Web 侧唯一总结入口）。"""

    session_id: str


class ToolsListCmd(_CmdEnvelope, total=False):
    """tools_list：工具清单（与 resolve_tools 同源）。"""


class SkillsListCmd(_CmdEnvelope, total=False):
    """skills_list：技能清单。"""


class CompactCmd(_CmdEnvelope, total=False):
    """compact：压缩指定会话历史（缺省当前桶）。"""

    session_id: str


class HealthCmd(_CmdEnvelope, total=False):
    """health：健康自检。"""


class McpListCmd(_CmdEnvelope, total=False):
    """mcp_list：MCP server 状态清单。"""


class McpSetEnabledCmd(_CmdEnvelope, total=False):
    """mcp_set_enabled：启停 MCP server。"""

    name: str
    enabled: bool


class McpRemoveCmd(_CmdEnvelope, total=False):
    """mcp_remove：移除 MCP server。"""

    name: str


class McpAddCmd(_CmdEnvelope, total=False):
    """mcp_add：新增 MCP server。"""

    config: dict[str, Any]  # McpServerConfig.from_dict 的输入字典


class McpReloadCmd(_CmdEnvelope, total=False):
    """mcp_reload：重载 MCP 配置并重连。"""


class PrefsGetCmd(_CmdEnvelope, total=False):
    """prefs_get：读用户偏好（七键）。"""


class PrefsSetCmd(_CmdEnvelope, total=False):
    """prefs_set：合并用户偏好（Manager 镜像广播）。"""

    prefs: dict[str, Any]   # 偏好键值（worker 侧只收 state.prefs 已有键）


class PermissionModeSetCmd(_CmdEnvelope, total=False):
    """permission_mode_set：V3 权限模式切换（可内联）。"""

    mode: str               # full_access / before_changes / plan


class PermissionModeGetCmd(_CmdEnvelope, total=False):
    """permission_mode_get：读当前权限模式（无 payload）。"""


class WakerSetCmd(_CmdEnvelope, total=False):
    """waker_set：切换会话绑定的 waker 人格（可内联；空=默认助手）。"""

    name: str
    session_id: str         # 缺省当前桶


class WakerGetCmd(_CmdEnvelope, total=False):
    """waker_get：读会话绑定的 waker 名。"""

    session_id: str


class LlmParamsSetCmd(_CmdEnvelope, total=False):
    """llm_params_set：模型热切换（可内联；模型键经 _LLM_PARAMS_KEYS 读取）。"""

    model: str | None
    base_url: str | None
    api_key: str | None     # 仅父子进程内存流转，不落盘
    context_window: int | None
    clear_model_overrides: bool  # True=None/空串=显式清除该项 override
    # 主进程会话分支亲和路由时携带（config_router.put_model）；worker 端
    # 当前不读该键（路由在父进程完成）——登记以锁住线上的真实键位。
    session_id: str


class SettingsUpdateCmd(_CmdEnvelope, total=False):
    """settings_update：系统配置全键热更新（可内联；原地合并 settings 单例）。"""

    updates: dict[str, Any]  # settings 键值（布尔键经 normalize_setting_value 归一）


class WakerRunCmd(_CmdEnvelope, total=False):
    """waker_run：worker 进程内跑一轮数字员工任务（src/waker/scheduler.py
    旧 IPC 路径；新路径走 wakerflow worker_node，不经本协议）。"""

    name: str
    run_id: str
    api_prompt: str | None


class WorkspaceChangedCmd(_CmdEnvelope, total=False):
    """workspace_changed：挂载状态变化通知（fire-and-forget，worker 仅 ack）。"""


class ExitCmd(_CmdEnvelope, total=False):
    """exit：请求 worker 退出（置标志走统一收尾，仅 ack）。"""


# ---------------------------------------------------------------------------
# op → TypedDict 注册表与键位表（契约测试的单一事实源）
# ---------------------------------------------------------------------------
OP_PAYLOAD_TYPES: dict[str, type] = {
    "chat": ChatCmd,
    "chat_approve": ChatApproveCmd,
    "chat_stop": ChatStopCmd,
    "memory_get": MemoryGetCmd,
    "memory_clear": MemoryClearCmd,
    "sessions_list": SessionsListCmd,
    "current_session": CurrentSessionCmd,
    "session_load": SessionLoadCmd,
    "session_save": SessionSaveCmd,
    "session_reset": SessionResetCmd,
    "session_rename": SessionRenameCmd,
    "session_delete": SessionDeleteCmd,
    "session_summary": SessionSummaryCmd,
    "tools_list": ToolsListCmd,
    "skills_list": SkillsListCmd,
    "compact": CompactCmd,
    "health": HealthCmd,
    "mcp_list": McpListCmd,
    "mcp_set_enabled": McpSetEnabledCmd,
    "mcp_remove": McpRemoveCmd,
    "mcp_add": McpAddCmd,
    "mcp_reload": McpReloadCmd,
    "prefs_get": PrefsGetCmd,
    "prefs_set": PrefsSetCmd,
    "permission_mode_set": PermissionModeSetCmd,
    "permission_mode_get": PermissionModeGetCmd,
    "waker_set": WakerSetCmd,
    "waker_get": WakerGetCmd,
    "llm_params_set": LlmParamsSetCmd,
    "settings_update": SettingsUpdateCmd,
    "waker_run": WakerRunCmd,
    "workspace_changed": WorkspaceChangedCmd,
    "exit": ExitCmd,
}


def _all_keys(cls: type) -> frozenset[str]:
    """TypedDict 的全部键 = 必选（含基类信封键）∪ 可选。"""
    return frozenset(cls.__required_keys__ | cls.__optional_keys__)


# op → 全部合法键（含信封键 id/op）。契约测试（tests/test_web_fastapi/
# test_ipc_op_contract.py）以本表为基准双向断言：worker 读取 ⊆ 声明、
# 发送方构造 ⊆ 声明。新增/修改 op 键位先改 OP_PAYLOAD_TYPES 再看这里。
OP_PAYLOAD_KEYS: dict[str, set[str]] = {
    op: set(_all_keys(cls)) for op, cls in OP_PAYLOAD_TYPES.items()
}
