"""
============================================
remember 工具 — LLM 自主记忆
============================================
让 LLM 在判断"这条信息值得长期记住"时主动调用。
是 agent memory 的核心：记忆从"系统强塞"变成"agent 能力"。

user_id 隔离方案（C1 修复）：
用 contextvars.ContextVar 存当前 user_id，每个请求/线程/协程自然隔离，
彻底消除模块级 dict 的多用户竞态。由 agent 循环 stream_invoke 在
每轮开头 set 上下文。

MemoryManager 仍是模块级单例（线程安全由 FileMemoryStore 的模块级 _WRITE_LOCK 保证）。
"""
import logging
import contextvars

logger = logging.getLogger("hermes.tools.remember")

# C1 修复：用 contextvars 替代模块级 dict 存 user_id
# 每个请求/线程/协程有独立的上下文副本，Web 多用户并发时不会串数据
_current_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "hermes_remember_user_id", default=None
)

# 会话绑定项目（与 user_id 同模式）：worker 开轮时从会话桶注入，
# remember 写入与 prestep 记忆检索都以它为准，取代"运行时刻的全局激活
# 指针"——多项目并发/生成中切顶栏不再串项目。None/空串 = 未设置
# （CLI、waker 等路径），由 MemoryManager._stamp_project 回落激活指针。
_current_project: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "hermes_remember_project", default=None
)

# manager 仍是单例（线程安全：FileMemoryStore 的模块级 _WRITE_LOCK 保证写入串行化）
_manager_holder: dict = {}


def set_memory_manager(manager) -> None:
    """由 agent 构造时调用，注入 MemoryManager 实例（单例，无竞态）。"""
    _manager_holder["manager"] = manager


def set_current_user_id(user_id: str) -> contextvars.Token:
    """每轮对话开始时由 agent 注入当前 user_id 到上下文变量。

    返回 Token 供调用方在退出时 reset（虽然 stream_invoke 一次性执行，
    通常不需要显式 reset，但 Web 异步场景可用 contextvars.copy_context() 隔离）。

    C1 修复：用 ContextVar.set 替代 dict 赋值，保证并发隔离。
    """
    return _current_user_id.set(user_id)


def set_current_project(project: str | None) -> contextvars.Token:
    """每轮对话开始时由调用方（worker 开轮/审批恢复）注入会话绑定项目。

    与 set_current_user_id 同模式：contextvars 天然按请求/协程隔离。
    """
    return _current_project.set(project)


def _get_manager():
    return _manager_holder.get("manager")


def get_current_user_id() -> str | None:
    """公开访问器：返回当前 user_id（contextvar）。

    供 virtual_fs / run_shell 等 workspace 按用户分层的模块使用（M1-1），
    亦供本模块 remember 工具内部调用。
    """
    return _current_user_id.get()


def get_current_project() -> str | None:
    """当前会话绑定项目（contextvar）。None = 未设置，回落激活指针。"""
    return _current_project.get()


def remember(content: str) -> str:
    """
    把一条值得长期记住的事实写入用户记忆。

    何时调用：
    - 用户透露了身份信息（姓名、职业、所在地）
    - 用户明确表达了偏好或厌恶
    - 出现了重要的决策、目标、计划
    - 用户纠正了之前的错误信息

    不要调用：
    - 闲聊、寒暄、情绪
    - 一次性的任务执行过程
    - 你自己（助手）的回复内容

    Args:
        content: 一条原子事实，用陈述句表达（如"用户主要用 Python 做数据分析"）。
                 不要塞多条事实，分开多次调用。

    Returns:
        存储结果说明，供你判断是否成功。
    """
    manager = _get_manager()
    if manager is None:
        return "记忆系统未就绪，无法存储。"

    user_id = get_current_user_id()
    if not user_id:
        return "无法确定当前用户，记忆未存储。"

    try:
        result = manager.remember_fact(
            user_id, content, source="tool:remember",
            project=get_current_project(),
        )
        if not result.get("success"):
            return "记忆存储失败，请稍后再试。"
        events = result.get("events", [])
        if "ADD" in events:
            return f"已记住：{content}"
        elif "UPDATE" in events:
            return f"已更新记忆：{content}"
        elif "DELETE" in events:
            return f"已删除过时记忆：{content}"
        elif "NOOP" in events:
            return "这条已经记过了，无需重复。"
        else:
            return f"记忆处理完成：{events}"
    except Exception as e:
        logger.error(f"remember 工具失败: {e}", exc_info=True)
        return f"记忆存储异常：{e}"


# ════════════════════════════════════════════════════════════════
# V3 PythonExecutor 入口
# ════════════════════════════════════════════════════════════════

def _execute_remember(content: str, *, ctx=None) -> str:
    """V3 PythonExecutor 入口。"""
    return remember(content)
