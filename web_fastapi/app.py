"""FastAPI 应用工厂。"""
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from config import get_settings
from src.health import run_health_check
from src.version import __version__
from web_fastapi.worker_manager import WorkerManager

logger = logging.getLogger("hermes.web")

BASE_DIR = Path(__file__).resolve().parent

# P1-5（防 DNS rebinding / CSRF 简单请求）：免认证 + 回环绑定是 ADR-0005
# 的设计而非缺陷——本机单用户零登录可用保持不变。下面两个中间件只封
# 浏览器侧攻击面：
#   1) Host 白名单：恶意网页诱导受害者浏览器把自有域名解析到 127.0.0.1，
#      即可以"同源"身份读写全 API（读会话、改 openai_base_url 外带 key）；
#   2) 非 GET/HEAD/OPTIONS 且带 Origin 头时必须与 Host 同源：拦 CSRF
#      简单请求（Form 表单直达 mount_local / consolidate 等无 preflight
#      端点）。无 Origin 的请求（curl / TestClient / 同源表单）照常放行。
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# testserver：FastAPI TestClient 的默认 Host（回归测试兼容）
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "testserver"})


def _host_of(authority: str) -> str:
    """从 Host 头 / URL netloc 取小写 hostname（剥端口与 IPv6 方括号）。"""
    try:
        return (urlsplit(f"//{authority or ''}").hostname or "").lower()
    except ValueError:
        return ""


def _port_of(authority: str, default_port: int = 80) -> int:
    """从 Host 头取端口；无端口按 default_port（调用方按请求 scheme 传
    该 scheme 的缺省端口）。解析失败返回 -1（必不匹配）。"""
    try:
        return urlsplit(f"//{authority or ''}").port or default_port
    except ValueError:
        return -1


def _configured_web_host() -> str:
    """读部署配置的监听地址（env WEB_HOST > settings.web_host > 127.0.0.1）。"""
    try:
        from web_fastapi.main import resolve_web_host
        return (resolve_web_host() or "").strip()
    except Exception:
        return ""


def _hb_value(v) -> str:
    """心跳数值渲染：不可得（None）渲染为 "-"，保证一行可 grep。"""
    return "-" if v is None else str(v)


def _log_startup_ops_heartbeat(app: FastAPI, storage=None) -> None:
    """P2-6：启动 ops 心跳——装配完成后打一条可 grep 的运行快照。

    内容：worker 槽位数 / hermes.db 的 WAL 文件字节数 / events 行数 /
    memories 行数（表名与 src/storage/sqlite_provider.py 的 _DDL 对齐）。
    全项只读 + 静默降级：查询失败（全新环境无库/无表等）只打一条 debug，
    绝不让启动失败。
    """
    # 版本可见性：版本与代码目录主进程本地可得，先于 ops-heartbeat 打出
    logger.info("[ops-startup] version=%s code=%s",
                __version__, Path(__file__).resolve().parents[1])
    try:
        from src.storage.housekeeping import collect_ops_snapshot
        snap = collect_ops_snapshot(storage)
        logger.info(
            "[ops-heartbeat] worker_slots=%s wal_bytes=%s events_rows=%s memories_rows=%s",
            len(app.state.worker_manager.all_slots()),
            _hb_value(snap.get("wal_bytes")),
            _hb_value(snap.get("events_rows")),
            _hb_value(snap.get("memories_rows")),
        )
    except Exception:
        logger.debug("[ops-heartbeat] 采集失败（全新环境/存储不可用，静默降级）",
                     exc_info=True)


def create_app() -> FastAPI:
    # W3：全局异常映射——IPC 桥接路由的 TimeoutError（worker 忙）统一 503、
    # worker 已退出类 RuntimeError 502、store 层 ValueError（非法名字/穿越）400，
    # 取代散落各路由的裸 500。
    from fastapi import Request
    from fastapi.responses import JSONResponse

    def _register_exception_handlers(app_obj) -> None:
        @app_obj.exception_handler(TimeoutError)
        async def _timeout_handler(request: Request, exc: TimeoutError):
            return JSONResponse(
                status_code=503,
                content={"detail": "AI 正在思考（worker 忙），请稍后重试"},
            )

        @app_obj.exception_handler(RuntimeError)
        async def _runtime_handler(request: Request, exc: RuntimeError):
            msg = str(exc)
            if "worker" in msg.lower():
                return JSONResponse(
                    status_code=502, content={"detail": f"服务暂时不可用: {msg}"}
                )
            return JSONResponse(status_code=500, content={"detail": msg})

        @app_obj.exception_handler(ValueError)
        async def _value_error_handler(request: Request, exc: ValueError):
            # store 层的名字/路径防穿越校验（FlowStore/WakerStore/projects）
            return JSONResponse(status_code=400, content={"detail": str(exc)})

    def _register_browser_guards(app_obj) -> None:
        """P1-5：Host 白名单 + Origin 同源两个全局中间件（详见模块头注释）。

        注册顺序即包装顺序（后注册在外层）→ _host_guard 最先执行。
        """
        from fastapi import Request
        from fastapi.responses import JSONResponse

        configured = _configured_web_host()
        # 通配监听（0.0.0.0/*/::）是文档化的局域网部署形态（main.py 打印
        # 暴露警告）——该模式本就信任整个所连网络（无认证），rebinding 无
        # 增量风险，跳过 Host 校验以保留它；回环模式（出厂默认）严格执行。
        wildcard = configured in ("0.0.0.0", "*", "::")
        allowed_hosts = set(_LOOPBACK_HOSTS)
        if configured and not wildcard:
            allowed_hosts.add(_host_of(configured))

        @app_obj.middleware("http")
        async def _origin_guard(request: Request, call_next):
            if request.method.upper() not in _SAFE_METHODS:
                origin = (request.headers.get("origin") or "").strip()
                if origin:
                    ok = False
                    try:
                        o = urlsplit(origin)
                        if o.scheme and o.netloc:
                            host_header = request.headers.get("host", "")
                            origin_port = o.port or (
                                443 if o.scheme == "https" else 80)
                            # Host 侧缺省端口同样按请求 scheme 取（https/反代
                            # 部署 Host 多不带端口）——旧实现一律按 80，会把
                            # https 同源请求误判成跨站（全站非 GET 403）。
                            host_port = _port_of(
                                host_header,
                                443 if (request.url.scheme or "http") == "https"
                                else 80,
                            )
                            ok = (
                                (o.hostname or "").lower()
                                == _host_of(host_header)
                                and origin_port == host_port
                            )
                    except ValueError:
                        ok = False
                    if not ok:
                        return JSONResponse(
                            status_code=403,
                            content={"detail": "跨站请求被拒绝（Origin 与 Host 不同源）"},
                        )
            return await call_next(request)

        @app_obj.middleware("http")
        async def _host_guard(request: Request, call_next):
            if not wildcard:
                if _host_of(request.headers.get("host", "")) not in allowed_hosts:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "拒绝的 Host（防 DNS rebinding）"},
                    )
            return await call_next(request)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动自检（不阻断，仅记录）
        try:
            run_health_check(silent=True)
        except Exception as e:
            logger.warning(f"启动自检失败（不阻断）: {e}")
        # 初始化（方向 B：WorkerManager 按 user_id fork 子进程）
        app.state.worker_manager = WorkerManager()

        # 可恢复 SSE 事件总线（chat_bus）：per-session 环形缓冲 + 订阅者
        # 扇出。POST /api/chat/stream 的后台泵把 worker 流全量写入总线
        # （seq 唯一编号源），GET /api/chat/stream/{sid} 据此补差续流。
        # 在 lifespan（事件循环线程）创建 → 首次使用即绑定本循环。
        from web_fastapi.chat_bus import ChatBus
        app.state.chat_bus = ChatBus()

        # T5：主进程组合根上下文（仓库根 cordis.yaml）。workspace 插件随 boot
        # 注册 WorkspaceService 并 set_service——/api/workspace 路由从这里取
        # 服务（ctx.workspace）；worker 子进程各自 boot_context，同一 SQLite
        # 库跨进程共享挂载状态。boot 失败不阻断 web（workspace API 503 降级）。
        app.state.cordis_ctx = None
        try:
            from src.plugins import boot_context
            app.state.cordis_ctx = boot_context()
            logger.info("主进程组合根上下文已启动（workspace 服务就绪）")
        except Exception:
            logger.exception("主进程组合根上下文启动失败（workspace API 将 503 降级）")

        # T8a：统一调度服务。cordis boot 成功时三者共用 ctx.schedule
        # （单 daemon 线程托管 waker/flow/memory 的周期驱动）；
        # boot 失败/缺服务时传 None，各调度器自建独立实例（行为回退一致）。
        shared_schedule = None
        if app.state.cordis_ctx is not None:
            shared_schedule = app.state.cordis_ctx.try_get("schedule")
            if shared_schedule is not None:
                logger.info("统一调度服务已就绪（waker/flow/memory 共用）")

        # T8b：运行登记表持久化（RunRegistry 写穿 kv，scope="runs"）。
        # 复用主进程组合根的 storage 服务；boot 失败时传 None，各登记表
        # 自行落到默认库 SQLiteProvider（data/hermes.db）。
        shared_storage = None
        if app.state.cordis_ctx is not None:
            shared_storage = app.state.cordis_ctx.try_get("storage")

        # waker 调度器：读 waker.enabled，开启则注册到期扫描。
        # T2b-②：workspace_root 不再透传——各 store 自解析 paths.agent_home()。
        app.state.waker_scheduler = None
        app.state.waker_async_runner = None
        try:
            settings = get_settings()
            waker_cfg = settings.get("waker") if hasattr(settings, "get") else getattr(settings, "waker", {})
            waker_cfg = waker_cfg or {}
            # waker 异步运行器：始终启用（fork worker_node 子进程，不抢 worker 锁）。
            # 即使 waker.enabled=false（调度不开），手动 invoke 也走它。
            try:
                from src.waker.async_runner import WakerAsyncRunner
                app.state.waker_async_runner = WakerAsyncRunner(
                    max_concurrent=int(waker_cfg.get("max_concurrent", 2)),
                    storage=shared_storage,
                )
                app.state.waker_async_runner.start()
                logger.info("waker 异步运行器已启用（fork 子进程，不影响 chat）")
            except Exception:
                logger.exception("启动 waker 异步运行器失败（不阻断，回退 worker IPC）")

            if waker_cfg.get("enabled", False):
                from src.waker.scheduler import WakerScheduler
                app.state.waker_scheduler = WakerScheduler(
                    app.state.worker_manager,
                    tick_seconds=int(waker_cfg.get("tick_seconds", 30)),
                    max_concurrent=int(waker_cfg.get("max_concurrent", 2)),
                    waker_runner=app.state.waker_async_runner,
                    schedule=shared_schedule,
                )
                logger.info(
                    f"waker 调度器已启用: tick={waker_cfg.get('tick_seconds')}s, "
                    f"max_concurrent={waker_cfg.get('max_concurrent')}"
                )
            else:
                logger.info("waker 调度器已禁用（waker.enabled=false）")
        except Exception:
            logger.exception("启动 waker 调度器失败（不阻断 web 服务）")

        # WakerFlow 运行器：主进程线程池跑 flow（脱离 worker IPC，不阻塞 chat）。
        # 始终启用（不像 waker_scheduler 受 waker.enabled 开关；flow 运行是按需触发）。
        # T2b-②：workspace_root 不再透传——FlowStore 自解析 paths.agent_home()。
        app.state.flow_runner = None
        try:
            from src.wakerflow.runner import FlowRunner
            app.state.flow_runner = FlowRunner(
                max_concurrent=2,
                storage=shared_storage,
            )
            app.state.flow_runner.start()
            logger.info("WakerFlow 运行器已启用（主进程后台线程，不影响 chat）")
        except Exception:
            logger.exception("启动 WakerFlow 运行器失败（不阻断 web 服务）")

        # WakerFlow 调度器：注册到期扫描 → submit 到 FlowRunner。
        # 始终启用（flow 是否调度由各 flow 自己的 enabled/schedule_type 决定）。
        app.state.flow_scheduler = None
        try:
            if app.state.flow_runner is not None:
                from src.wakerflow.scheduler import FlowScheduler
                app.state.flow_scheduler = FlowScheduler(
                    app.state.flow_runner,
                    tick_seconds=30,
                    schedule=shared_schedule,
                )
                logger.info("WakerFlow 调度器已启用")
        except Exception:
            logger.exception("启动 WakerFlow 调度器失败（不阻断 web 服务）")

        # 记忆聚合调度器：注册达标扫描，后台自动聚合记忆。
        # 始终启动；是否真跑聚合由 kv 里的聚合配置决定（默认关）。
        app.state.memory_consolidation_scheduler = None
        try:
            from src.memory.scheduler import MemoryConsolidationScheduler
            app.state.memory_consolidation_scheduler = MemoryConsolidationScheduler(
                tick_seconds=300,
                max_concurrent=2,
                schedule=shared_schedule,
                storage=shared_storage,
            )
            app.state.memory_consolidation_scheduler.start()
            logger.info("记忆聚合调度器已启用（默认 auto_consolidate=off）")
        except Exception:
            logger.exception("启动记忆聚合调度器失败（不阻断 web 服务）")

        # 存储卫生（ADR-0004-D3）：WAL checkpoint + resolved-interrupt TTL
        # + 会话事件冷归档（P3）。廉价低频操作挂统一调度服务；boot 失败时
        # 退化为默认库自建实例。
        app.state.storage_housekeeping = None
        try:
            import time as _time

            from src.constants import LOCAL_USER as _LOCAL_USER
            from src.storage.housekeeping import (
                ARCHIVE_QUIET_SECONDS,
                StorageHousekeeping,
            )

            def _session_active(sid: str) -> bool:
                """活跃会话判定（冷归档并发防护①）：该 sid 有存活 worker 槽
                且（流式在途 或 最近活跃过）→ 活跃。槽按 sid 亲和命名，
                主槽会话查不到 → False，由静默时间门槛（防护②）兜底。
                判定链路任何异常按"活跃"处理（保守跳过）。"""
                try:
                    wm = app.state.worker_manager
                    wp = wm.lookup(_LOCAL_USER, sid) if wm is not None else None
                    if wp is None:
                        return False
                    if getattr(wp, "streaming", False):
                        return True
                    return (_time.time() - float(getattr(wp, "last_active", 0.0) or 0.0)
                            ) < ARCHIVE_QUIET_SECONDS
                except Exception:
                    return True

            app.state.storage_housekeeping = StorageHousekeeping(
                tick_seconds=3600,
                schedule=shared_schedule,
                storage=shared_storage,
                is_session_active=_session_active,
            )
            app.state.storage_housekeeping.start()
            logger.info("存储卫生任务已启用（WAL checkpoint / interrupt TTL / 事件冷归档）")
        except Exception:
            logger.exception("启动存储卫生任务失败（不阻断 web 服务）")

        # 内置本地向量模型预热线程（用户无感）：懒加载单例，首次触发一次性
        # 模型下载到 data/models/；就绪后自动补嵌存量 keyword-only 记忆。
        # 下载失败/断网只降级为纯关键词检索，不阻断启动。
        def _warm_embedder() -> None:
            try:
                from src.memory.embeddings import get_default_embedder
                from src.storage.sqlite_provider import SQLiteProvider

                get_default_embedder()  # 触发懒加载（含首次模型下载）
                logger.info("本地向量模型就绪，语义检索通道启用")
                prov = SQLiteProvider(embedder=get_default_embedder())
                try:
                    n = prov.backfill_embeddings()
                    if n:
                        logger.info(f"已补嵌 {n} 条 keyword-only 记忆")
                finally:
                    prov.close()
            except Exception as e:
                logger.warning(f"本地向量模型不可用，记忆检索降级为纯关键词: {e}")

        threading.Thread(target=_warm_embedder, daemon=True, name="embedding-warmup").start()

        # P2-6：启动 ops 心跳（装配完成后一条可 grep 快照：槽位/WAL/行数；
        # 查询失败静默降级为 debug，绝不阻断启动）。
        _log_startup_ops_heartbeat(app, shared_storage)

        yield
        # 进程退出：先停 flow 调度器（不再触发新 flow），再停 flow 运行器
        # （取消未开始的 flow），再停 waker 调度器，最后关闭所有 worker 子进程。
        try:
            if getattr(app.state, "flow_scheduler", None) is not None:
                app.state.flow_scheduler.stop()
        except Exception:
            logger.exception("停止 WakerFlow 调度器失败（忽略）")
        try:
            if getattr(app.state, "flow_runner", None) is not None:
                app.state.flow_runner.shutdown()
        except Exception:
            logger.exception("停止 WakerFlow 运行器失败（忽略）")
        try:
            if getattr(app.state, "waker_scheduler", None) is not None:
                app.state.waker_scheduler.stop()
        except Exception:
            logger.exception("停止 waker 调度器失败（忽略）")
        try:
            if getattr(app.state, "waker_async_runner", None) is not None:
                app.state.waker_async_runner.shutdown()
        except Exception:
            logger.exception("停止 waker 异步运行器失败（忽略）")
        try:
            if getattr(app.state, "memory_consolidation_scheduler", None) is not None:
                app.state.memory_consolidation_scheduler.stop()
        except Exception:
            logger.exception("停止记忆聚合调度器失败（忽略）")
        try:
            if getattr(app.state, "storage_housekeeping", None) is not None:
                app.state.storage_housekeeping.stop()
        except Exception:
            logger.exception("停止存储卫生任务失败（忽略）")
        # 可恢复 SSE 总线收尾：清空全部会话通道（在途订阅者收 STREAM_END
        # 收流、TTL 定时器撤销——内存上界在 shutdown 侧的兜底）。
        try:
            if getattr(app.state, "chat_bus", None) is not None:
                app.state.chat_bus.clear()
        except Exception:
            logger.exception("清空 chat 事件总线失败（忽略）")
        app.state.worker_manager.shutdown_all()

        # T5：拆卸主进程组合根上下文（放在 worker 全关之后——workspace 服务
        # 与 worker 共用的 SQLite 连接在此释放；teardown 幂等）。
        try:
            if getattr(app.state, "cordis_ctx", None) is not None:
                app.state.cordis_ctx.teardown()
        except Exception:
            logger.exception("拆卸主进程组合根上下文失败（忽略）")

    app = FastAPI(title="Hermes-Ma Web", lifespan=lifespan)
    _register_exception_handlers(app)
    _register_browser_guards(app)

    # 静态文件 + 模板（目录可能尚未存在，用 try 保护）
    static_dir = BASE_DIR / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # 注册路由
    from web_fastapi.routers import auth, chat, sessions, memory, system, mcp, config_router, pages, upload, waker, wakerflow, workspace, projects, runs, models, git_router, usage
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
    app.include_router(sessions.router, prefix="/api/sessions", tags=["sessions"])
    app.include_router(memory.router, prefix="/api/memory", tags=["memory"])
    app.include_router(system.router, prefix="/api", tags=["system"])
    app.include_router(mcp.router, prefix="/api/mcp", tags=["mcp"])
    app.include_router(config_router.router, prefix="/api/config", tags=["config"])
    app.include_router(upload.router, prefix="/api", tags=["upload"])
    app.include_router(waker.router, prefix="/api/waker", tags=["waker"])
    app.include_router(wakerflow.router, prefix="/api/wakerflow", tags=["wakerflow"])
    app.include_router(workspace.router, prefix="/api/workspace", tags=["workspace"])
    app.include_router(projects.router, prefix="/api/projects", tags=["projects"])
    app.include_router(runs.router, prefix="/api/runs", tags=["runs"])
    app.include_router(usage.router, prefix="/api/usage", tags=["usage"])
    app.include_router(models.router, prefix="/api/models", tags=["models"])
    app.include_router(git_router.router, prefix="/api/workspace/git", tags=["git"])
    app.include_router(pages.router, tags=["pages"])

    return app
