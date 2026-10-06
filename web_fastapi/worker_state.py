"""
Worker 运行时状态与持久化（P2 自 worker_process.py 三分抽出）。

- SessionBucket：单个会话的运行时桶（messages/todos/vfs/waker/project）。
- WorkerState：worker 进程级状态——会话分桶、agent/组合根生命周期
  （init_agent / shutdown_context）、磁盘持久化（_save_bucket /
  save_all_buckets）。

纯状态模块：不 import worker_process / worker_ops（无环）。worker_process.py
经 `from web_fastapi.worker_state import SessionBucket, WorkerState`
re-export 保持既有导入面——tests 大量 `wp.WorkerState` / `wp.SessionBucket`
引用与 `wp.WorkerState._save_bucket` 类属性 monkeypatch 依赖同一类对象。
"""
import logging
import threading
import uuid

from src.agent.hitl import InterruptStore

logger = logging.getLogger("hermes.web.worker")


class SessionBucket:
    """单个会话的运行时状态（分桶隔离）。

    同一用户可以有多个并发会话（多标签页），每个会话的消息/待办/虚拟文件
    系统互不干扰。session_id 作为唯一 key，前端发消息时显式携带。
    """

    __slots__ = ("session_id", "messages", "todos", "vfs", "waker", "project")

    def __init__(self, session_id: str, project: str | None = None):
        self.session_id = session_id
        self.messages: list = []
        self.todos: list = []
        self.vfs: dict = {}
        self.waker: str = ""  # 会话绑定的 waker 名（空=默认助手）
        # 创建时刻的激活项目（ADR-0005 D2）：归属在"会话诞生"时点确定，
        # 而不是首次落盘时——否则首条消息生成期间切项目会绑错。
        self.project = project


class WorkerState:
    """worker 进程的运行时状态（内存里，贴 CLI 语义）。

    slot：本实例的槽名（main / 会话 sid），仅用于日志与 ready 握手标识。

    会话状态（messages/todos/vfs）按 session_id 分桶，支持同一用户多会话
    并发。user 级偏好（permission_mode/prefs）不分桶——它们是用户全局设定。
    """

    def __init__(self, user_id: str, slot: str = "main"):
        self.user_id = user_id
        self.slot = slot
        self.agent = None
        self.memory_manager = None
        # T4 组合根上下文（init_agent 里 boot_context() 构造，进程退出拆卸）
        self.ctx = None
        # 组合根 storage 服务缓存（init_agent 时接线；项目激活归属读取用）
        self._shared_storage = None
        # ── 会话分桶 ──
        self._buckets: dict[str, SessionBucket] = {}
        # 幽灵治理（ADR-0004-D4①）：当前会话只保留 sid 句柄，不预建内存桶。
        # get_bucket 懒创建——首个真实输入才物化；从没说过话就退出的 sid
        # 不再经 save_all_buckets 落成磁盘空快照（审计实测曾繁殖 177/184）。
        self._current_sid: str = str(uuid.uuid4())[:8]
        # V3 权限模式：full_access / before_changes / plan
        self.permission_mode: str = "before_changes"
        # per-user 偏好（热更新）
        self.prefs = {
            "workspace_root": "",
            "web_proxy": "",
            "temperature": 0.7,
            # prefs 是 per-user 覆盖，种子值必须跟随全局 config.yaml（唯一
            # 真相源）。此前硬编码 2000 且恒存在，agent 侧 settings.max_tokens
            # 的 fallback 永远轮不到——2026-09-19 用量看板「输出恒 2000 截断」
            # 的根因（详见用量排查记录）。
            "max_tokens": self._seed_max_tokens(),
            "memory_min_score": 0.4,
            "max_memory_results": 5,
            "compact_threshold_pct": 80,
        }

    @staticmethod
    def _seed_max_tokens() -> int | None:
        """prefs.max_tokens 种子值：跟随 config.yaml 的 settings.max_tokens。

        None = 用户未单独设定上限，worker 每轮 set_llm_params(max_tokens=None)
        → agent 回退 settings.max_tokens（也为空则请求不带该字段，由服务端
        默认决定）。读不到配置绝不硬编码数值兜底——硬编码正是双真相源的祸根。
        """
        try:
            from config import get_settings
            v = int(getattr(get_settings(), "max_tokens", 0) or 0)
        except Exception:
            return None
        return v if v > 0 else None

    # ── 会话分桶操作 ──

    def get_bucket(self, session_id: str | None = None) -> SessionBucket:
        """取/建会话桶。session_id 为空时用 _current_sid。

        如果桶不存在（前端首次发新 session_id），自动创建空桶。
        """
        sid = session_id or self._current_sid
        bucket = self._buckets.get(sid)
        if bucket is None:
            bucket = SessionBucket(sid, project=self._get_active_project())
            self._buckets[sid] = bucket
            logger.info(f"新建会话桶: sid={sid}, project={bucket.project!r}, 总桶数={len(self._buckets)}")
        return bucket

    def set_current(self, session_id: str) -> SessionBucket:
        """切换当前会话（load/reset 用）。"""
        self._current_sid = session_id
        return self.get_bucket(session_id)

    @property
    def current_sid(self) -> str:
        return self._current_sid

    @property
    def current_bucket(self) -> SessionBucket:
        return self.get_bucket(self._current_sid)

    # ── 向后兼容属性（过渡期，让 session_messages/session_id/todos/vfs
    #    仍可访问，全部代理到 current_bucket）──

    @property
    def session_messages(self) -> list:
        return self.current_bucket.messages

    @session_messages.setter
    def session_messages(self, value: list):
        self.current_bucket.messages = value

    @property
    def session_id(self) -> str:
        return self._current_sid

    @session_id.setter
    def session_id(self, value: str):
        self._current_sid = value

    @property
    def todos(self) -> list:
        return self.current_bucket.todos

    @todos.setter
    def todos(self, value: list):
        self.current_bucket.todos = value

    @property
    def vfs(self) -> dict:
        return self.current_bucket.vfs

    @vfs.setter
    def vfs(self, value: dict):
        self.current_bucket.vfs = value

    def init_agent(self):
        """初始化组合根 Context + HermesAgentV3（权限模式架构）。

        T4：核心服务（config/storage/sessions/memory/llm/tools/skills/mcp）
        经 boot_context()（仓库根 cordis.yaml）插件化挂载。
        - memory_manager 统一取 ctx.memory（store=ctx.storage，与旧
          MemoryManager() 默认后端同为 data/hermes.db）
        - agent 注入 ctx.tools.registry（权限段 tools/pre-execute 事件化）
        - T6：agent 注入 kernel_ctx=ctx（循环事件化的作用域；sessions 服务
          经它自动接线成 durable 事件日志——T3 的 worker shim 已移进 agent
          循环本体）
        - T3 行为保持：sessions 可用时构造 InterruptStore(session_log=ctx.sessions)
          + recover_into；失败降级为纯内存（agent 内自建）
        """
        from src.plugins import boot_context
        from src.agent import HermesAgentV3

        ctx = boot_context()
        self.ctx = ctx
        self.memory_manager = ctx.get("memory")
        try:
            self._shared_storage = ctx.try_get("storage")
        except Exception:
            self._shared_storage = None

        agent_kwargs = {"registry": ctx.get("tools").registry, "kernel_ctx": ctx}
        sessions = ctx.try_get("sessions")
        if sessions is not None:
            try:
                interrupt_store = InterruptStore(session_log=sessions)
                recovered = sessions.recover_into(interrupt_store)
                agent_kwargs["interrupt_store"] = interrupt_store
                logger.info(f"SessionLog 就绪，恢复 pending 中断 {recovered} 个: user={self.user_id}")
            except Exception:
                logger.warning("SessionLog 初始化失败，事件溯源降级关闭", exc_info=True)

        self.agent = HermesAgentV3(self.memory_manager, tool_callback=None, **agent_kwargs)
        # 默认权限模式：变更前访问（destructive 工具需审批）
        self.agent.set_permission_mode("before_changes")
        self.permission_mode = "before_changes"
        # 记忆链路复用 chat 侧同一 LLM client（agent 构造时序在 memory 之后，
        # 只能事后绑定；summarizer 由 session_lifecycle 读 llm_shared 跟随）
        try:
            self.memory_manager.set_llm_provider(self.agent.get_llm_client)
        except Exception:
            logger.warning("memory_manager 绑定共享 LLM client 失败（走兜底 client）", exc_info=True)
        # 本地向量模型预热（daemon）：memory_plugin 挂 embedder 后，首轮
        # 对话的记忆检索会同步触发模型加载（秒级）——预热挪到启动期后台，
        # 首轮对话不再付这笔延迟。失败只降级纯关键词（与检索路径同语义）
        threading.Thread(target=self._warm_embedder, daemon=True,
                         name="worker-embed-warmup").start()
        # MCP 后台连接（2026-09-07）：此前 worker 启动链没有任何 MCP 连接
        # 步骤，connect_enabled_all 只在设置页手动 reload 时执行——每个新
        # 会话槽的 worker 里 MCP 永远断连，直到用户手动 reload。agent 每
        # turn resolve_tools 会动态拉取已连接 server 的工具，故后台连接
        # 成功后下一 turn 即可见；不阻塞 ready 握手（spawn 窗口不受影响）。
        threading.Thread(target=self._connect_mcp_servers, daemon=True,
                         name="worker-mcp-connect").start()
        logger.info(f"worker agent 初始化完成: user={self.user_id}")

    def _connect_mcp_servers(self) -> None:
        """后台连接所有 enabled 的 MCP server（对齐设置页 reload 的结果）。

        连接失败的降级：告警日志（client.connect 内部已带重试与子进程
        清理）；成功的 server 经 rebind_tools 立即可被 agent resolve。
        """
        try:
            from src.mcp.client import get_client_manager
            results = get_client_manager().connect_enabled_all()
        except Exception:
            logger.warning("worker 启动期 MCP 后台连接失败（降级为无 MCP 工具）",
                           exc_info=True)
            return
        connected = [name for name, (ok, _) in results.items() if ok]
        for name, (ok, msg) in results.items():
            (logger.info if ok else logger.warning)(
                f"MCP 后台连接 {name}: {msg}")
        if connected:
            try:
                self.agent.rebind_tools()
                logger.info(f"MCP 工具已 rebind: {', '.join(connected)}")
            except Exception:
                logger.warning("MCP 工具 rebind 失败（下一 turn resolve 重试）",
                               exc_info=True)

    @staticmethod
    def _warm_embedder() -> None:
        try:
            from src.memory.embeddings import get_default_embedder
            get_default_embedder().embed(["预热"])
        except Exception:
            logger.info("向量模型预热失败，记忆检索降级纯关键词（不阻断 worker）")

    def shutdown_context(self):
        """T4：拆卸组合根上下文（幂等；storage 连接等随之释放）。"""
        if self.ctx is not None:
            try:
                self.ctx.teardown()
            except Exception:
                logger.warning("组合根上下文 teardown 失败", exc_info=True)
            self.ctx = None

    def _get_active_project(self) -> str:
        """当前激活项目 slug（会话首次落盘时随快照 stamp，ADR-0005 D2）。

        ctx 未就绪时走默认库兜底；读取失败一律回落 inbox（"" 语义），
        绝不阻断保存主路径。
        """
        try:
            from src.storage.projects_store import get_active_project
            return get_active_project(self._shared_storage)
        except Exception:
            return ""

    def _save_bucket(self, bucket: SessionBucket):
        """保存指定会话桶到磁盘。"""
        from src.session_store import save_session
        # 幽灵治理（ADR-0004-D4②）：从未发言的桶不落盘。消息为空即无任何
        # 可恢复状态——todos/vfs/waker 只有随历史一起才能被加载回来，单独
        # 存在的空会话只会成为侧栏里点击必败的幽灵条目（审计 §5.1）。
        if not bucket.messages:
            return
        save_session(self.user_id, bucket.messages, bucket.session_id,
                     todos=bucket.todos, virtual_fs=bucket.vfs, waker=bucket.waker,
                     project=bucket.project or self._get_active_project())

    def drop_bucket(self, session_id: str) -> None:
        """从内存移除指定会话桶（session_delete 用）。

        不弹的话退出时 save_all_buckets 会把已删除会话重写回盘——删档复活。
        """
        self._buckets.pop(session_id, None)

    def _save_current(self):
        """保存当前会话到磁盘（复用 cli.py save_session）。"""
        self._save_bucket(self.current_bucket)

    def save_all_buckets(self):
        """进程退出时遍历所有桶落盘。"""
        for bucket in list(self._buckets.values()):
            try:
                self._save_bucket(bucket)
            except Exception:
                logger.warning(f"保存会话桶失败: sid={bucket.session_id}", exc_info=True)
