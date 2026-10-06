"""
============================================
worker_node —— WakerFlow worker 节点的子进程入口（M2.1）
============================================
WakerFlow 的 worker 节点 fork 本模块为独立子进程，跑一个**完整的
HermesAgentV3**（带记忆/会话/HITL/stream/工具白名单/permission），
实现"真并发 + 完整功能 + 用 waker 人格"。

## 通信协议（stdout NDJSON）
子进程通过 stdout 输出 NDJSON 事件，executor 逐行读取：
  {"type":"ready"}                              启动就绪
  {"type":"node_event", "event": {...}}         agent 事件透传（token/tool_*/complete）
  {"type":"result", "status":"ok"|"error", "content":"...", "run_id":"..."}  最终结果
  {"type":"log", "message":"..."}               日志（executor 可记录）

stderr 留给 Python logging（不干扰 NDJSON 协议）。

## CLI 参数
  --user-id      用户 ID（per-user 隔离，必填）
  --waker-name   引用的 waker 名（加载其人格 + 配置，必填）
  --task         节点任务（覆盖 waker 的 task_prompt）
  --task-stdin   从 stdin 读任务文本（P2-20：渲染后的任务可达数万字符，
                 走 argv 会撞 Windows 32k 命令行上限，spawn 直接
                 WinError 206）。与 --task 二选一，stdin 优先。
  --node-run-id  节点运行 ID（写 jsonl 文件名 + result 回传）
  --workspace-root  workspace 根（默认从 config 解析）
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading

# T8b：子进程 stdio 样板（Windows UTF-8 重配 + SSL_CERT_FILE 清洗）收敛到 src/ipc.py
from src.ipc import StdoutWriter, configure_subprocess_stdio

configure_subprocess_stdio()

logger = logging.getLogger("hermes.wakerflow.worker_node")


# ════════════════════════════════════════════════════════════════
# stdout NDJSON 输出（线程安全：executor 单线程读，但 agent stream
# 可能在 callback 线程里输出——StdoutWriter.send_sync 锁守直写互斥；
# default=str 兜底不可序列化对象）
# ════════════════════════════════════════════════════════════════
_writer = StdoutWriter(default=str)

# MCP 连接预算（秒）：真实服务里两个 server 各 1-2s 连上；挂死的 server
# 不得拖住无人值守的 waker 启动
MCP_CONNECT_BUDGET_S = 10.0


def _emit(obj: dict) -> None:
    """向 stdout 输出一行 NDJSON（线程安全，锁守直写）。"""
    _writer.send_sync(obj)


# ════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════
def main():
    args = _parse_args()
    # 日志走 stderr（不污染 stdout NDJSON 协议）
    logging.basicConfig(
        level=logging.INFO,
        format=f"[worker_node:{args.waker_name}] %(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    # 预热 import（触发 src.tools 的 loader 初始化，保持与主进程一致的工具目录）
    import src.agent.agent_v3  # noqa: F401
    logger.info(f"启动: waker={args.waker_name}, task={args.task[:60]}")

    _emit({"type": "ready", "waker": args.waker_name, "node_run_id": args.node_run_id})

    try:
        result = _run_node(args)
        _emit(result)
        logger.info(f"完成: status={result.get('status')}")
        sys.exit(0 if result.get("status") == "ok" else 1)
    except Exception as e:
        logger.exception(f"worker_node 未捕获异常: {e}")
        _emit({
            "type": "result",
            "status": "error",
            "content": f"worker_node 异常: {e}",
            "run_id": args.node_run_id,
            "waker": args.waker_name,
        })
        sys.exit(1)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="WakerFlow worker 节点子进程")
    p.add_argument("--user-id", required=True, help="用户 ID（per-user 隔离）")
    p.add_argument("--waker-name", required=True, help="引用的 waker 名（加载其人格+配置）")
    p.add_argument("--task", default="", help="节点任务（覆盖 task_prompt）；与 --task-stdin 二选一")
    p.add_argument(
        "--task-stdin", action="store_true",
        help="从 stdin 读任务文本。P2-20：渲染后的任务可达数万字符，走 argv "
             "会撞 Windows 32k 命令行上限（WinError 206）。",
    )
    p.add_argument("--node-run-id", required=True, help="节点运行 ID")
    p.add_argument("--workspace-root", default="", help="workspace 根（默认从 config 解析）")
    args = p.parse_args()
    if args.task_stdin:
        # stdin 已由 configure_subprocess_stdio（模块导入时）统一为 UTF-8。
        # 整读 stdin 是启动后的第一个动作，父进程写完即关闭，无管道死锁。
        args.task = sys.stdin.read()
    if not args.task:
        p.error("--task 与 --task-stdin 须提供其一")
    return args


def _connect_mcp_tools(agent, budget_s: float | None = None) -> None:
    """连接所有 enabled 的 MCP server，有成功则 rebind 工具（失败降级）。

    与 chat worker 的启动连接（web_fastapi/worker_process.py
    _connect_mcp_servers）同源语义。此前本子进程从不连接 MCP——任务提示词
    指定的 mcp 工具（如 ddg-search）在 waker 运行里不存在，模型为"找不到
    工具"空转到迭代上限（chat 正常、waker 打转的根因）。

    连接在后台线程跑、join 限时（默认 10s）：MCP server 挂死不得拖住
    无人值守的 waker 启动。超时则先跑任务——resolve_tools 每 turn 动态
    拉取已连接 server，连接完成后下一轮自动可见。异常一律吞掉只告警。

    HERMES_WORKER_NODE_SKIP_MCP=1 跳过（测试/诊断开关：集成测试不该
    依赖真实 MCP server）。
    """
    if os.environ.get("HERMES_WORKER_NODE_SKIP_MCP") == "1":
        logger.info("HERMES_WORKER_NODE_SKIP_MCP=1：跳过 MCP 连接")
        return
    budget = MCP_CONNECT_BUDGET_S if budget_s is None else budget_s
    box: dict = {}

    def _connect() -> None:
        try:
            from src.mcp.client import get_client_manager
            box["results"] = get_client_manager().connect_enabled_all()
        except Exception as e:
            box["exc"] = e

    t = threading.Thread(target=_connect, daemon=True, name="waker-mcp-connect")
    t.start()
    t.join(budget)
    if t.is_alive():
        logger.warning(
            f"MCP 连接超过 {budget}s 预算，先跑任务（连接完成后下一轮可见）")
        return
    if "exc" in box:
        logger.warning(f"waker 子进程 MCP 连接失败（降级为无 MCP 工具）: {box['exc']}")
        return
    results: dict = box.get("results") or {}
    connected = [name for name, (ok, _) in results.items() if ok]
    for name, (ok, msg) in results.items():
        (logger.info if ok else logger.warning)(
            f"waker 子进程 MCP 连接 {name}: {msg}")
    if connected:
        try:
            agent.rebind_tools()
            logger.info(f"waker 子进程 MCP 工具已 rebind: {', '.join(connected)}")
        except Exception:
            logger.warning("waker 子进程 MCP rebind 失败", exc_info=True)


def _run_node(args) -> dict:
    """跑一个完整 HermesAgentV3 任务，返回 result dict。

    流程：
    1. 设 user_id contextvar（让 virtual_fs / remember 按 per-user 隔离）
    2. 读 waker 配置 + 人格
    3. 构造 MemoryManager + HermesAgentV3（完整功能）
    4. 工具白名单 / permission_mode 临时覆盖
    5. stream_invoke(waker_persona=...) → 消费事件流，透传给 stdout + 取最终文本
    6. 返回 {status, content, run_id, waker}
    """
    # 1. 设 user_id contextvar
    from src.tools.remember import set_current_user_id
    set_current_user_id(args.user_id)

    # 2. 读 waker 配置 + 人格
    from src.waker.store import WakerStore
    from src.waker.persona import load_persona_prompt
    store = WakerStore(args.user_id, workspace_root=args.workspace_root)
    cfg = store.get(args.waker_name)
    if cfg is None:
        return {
            "type": "result", "status": "error",
            "content": f"waker 不存在: {args.waker_name}",
            "run_id": args.node_run_id, "waker": args.waker_name,
        }
    persona = load_persona_prompt(store, args.waker_name)

    # 3. 构造组合根 Context + HermesAgentV3（完整功能）
    # 懒 import，避免纯单测触发重依赖
    # T4：核心服务经 boot_context() 插件化（memory=ctx.memory，
    # registry=ctx.tools.registry——权限段 tools/pre-execute 事件化），
    # 进程生命周期结束时 teardown
    # T6：kernel_ctx=ctx——循环事件化作用域 + sessions 自动接线成 durable 日志
    from src.plugins import boot_context
    from src.agent.agent_v3 import HermesAgentV3

    ctx = boot_context()
    try:
        memory_manager = ctx.get("memory")
        agent = HermesAgentV3(
            memory_manager,
            registry=ctx.get("tools").registry,
            kernel_ctx=ctx,
        )

        # 测试设施：HERMES_WORKER_NODE_MOCK_LLM=1 时注入 mock LLM client，
        # 让无真实 LLM 的环境（CI/演示）也能跑通链路。生产环境不设此变量即用真实 LLM。
        if os.environ.get("HERMES_WORKER_NODE_MOCK_LLM") == "1":
            _inject_mock_llm(agent, args.task)

        # MCP 连接 + rebind（chat worker 启动即有，waker 子进程此前缺失）
        _connect_mcp_tools(agent)

        # 4. permission_mode 临时覆盖
        target_mode = cfg.permission_mode if cfg.permission_mode in ("full_access", "before_changes", "plan") else "before_changes"
        saved_mode = agent.get_permission_mode()
        if saved_mode != target_mode:
            agent.set_permission_mode(target_mode)
            logger.info(f"临时切换 permission_mode: {saved_mode} → {target_mode}")

        final_text = ""
        final_status = "ok"
        try:
            # 5. 工具白名单（经 ToolContext.allowed_tools，与 run_waker 一致逻辑）
            whitelist = set(cfg.tools) if cfg.tools else None
            final_text = _run_stream(
                agent, args.user_id, args.task, persona or None,
                args.waker_name, args.node_run_id, whitelist,
            )
        except Exception as e:
            logger.exception(f"stream_invoke 失败: {e}")
            final_status = "error"
            final_text = f"运行失败: {e}"
        finally:
            # 恢复 permission_mode
            if saved_mode != target_mode:
                try:
                    agent.set_permission_mode(saved_mode)
                except Exception:
                    logger.warning("恢复 permission_mode 失败", exc_info=True)
    finally:
        ctx.teardown()

    # 空结果检测：模型只思考没产出（reasoning 模型常见问题）
    if final_status == "ok" and not final_text.strip():
        final_status = "error"
        final_text = (
            "模型未产出有效内容（可能只输出了思考过程没行动，或被审批拦下）。"
            "检查 task_prompt 是否清晰、工具白名单是否包含所需工具。"
        )
        logger.warning(f"worker_node 空结果: {args.waker_name}/{args.node_run_id}")

    return {
        "type": "result",
        "status": final_status,
        "content": final_text,
        "run_id": args.node_run_id,
        "waker": args.waker_name,
    }


def _run_stream(agent, user_id: str, task: str, persona,
                waker_name: str, node_run_id: str, whitelist) -> str:
    """构造 stream_invoke + 消费事件流，透传事件到 stdout，返回最终文本。

    工具白名单过滤（T8a 作用域化）：whitelist 非空时经 stream_invoke 的
    allowed_tools 参数传给 ToolContext，resolve_tools 尾部按名过滤。
    与 src/waker/runner.py 的 _run_stream 同构（保持行为一致），
    取代旧的 resolve_tools monkey-patch（作用域天然覆盖整个消费期）。
    """
    thread_id = f"wakerflow:{waker_name}"

    def _consume(stream) -> str:
        final_text = ""
        for event in stream:
            # 透传事件给 executor（它可记日志或转发）
            _emit({"type": "node_event", "event": event, "node_run_id": node_run_id})
            if event.get("type") == "complete":
                final_text = event.get("content", "") or ""
            elif event.get("type") == "human_approval_request":
                # R3-16：与 in-worker 路径（src/waker/runner.py）同型——
                # 无人值守 auto-reject 时 pop 掉 pending 中断（写
                # interrupt/resolved），防残留 pending 被恢复扫描重导入。
                # 本子进程的 agent 默认持纯内存 store（未接 session_log），
                # pop 只清内存；持久侧由 pending_interrupts 的 waker 前缀
                # 排除兜底（src/agent/session_log.py）。
                store = getattr(agent, "interrupt_store", None)
                if store is not None:
                    try:
                        store.pop(thread_id, "reject", "waker_auto")
                    except Exception:
                        logger.warning("wakerflow auto-reject pop 中断失败（忽略）", exc_info=True)
        return final_text

    stream = agent.stream_invoke(
        user_id, task,
        session_id=f"wakerflow:{waker_name}:{node_run_id}",
        thread_id=thread_id,
        role=None,
        waker_persona=persona,
        allowed_tools=whitelist,
        caller_context="employee",  # 同 waker runner：管理面工具（create_waker 等）对流程步骤隐藏
    )
    return _consume(stream)


def _inject_mock_llm(agent, task: str) -> None:
    """注入 mock LLM client（测试/演示用，HERMES_WORKER_NODE_MOCK_LLM=1 触发）。

    让 agent.stream_invoke 不调真实 LLM，直接返回基于 task 的固定回复。
    """
    from unittest.mock import MagicMock
    from src.llm.messages import AIMsg, Chunk

    reply = f"[mock-LLM] 已处理任务：{task[:60]}"
    mock_client = MagicMock()
    mock_client.stream_chat.return_value = iter([
        Chunk(content_delta=reply[: len(reply)//2]),
        Chunk(content_delta=reply[len(reply)//2:]),
    ])
    mock_client.accumulate = staticmethod(lambda cs: AIMsg(content=reply))
    mock_client.chat.return_value = AIMsg(content=reply)
    agent._llm_client = mock_client
    logger.info(f"已注入 mock LLM（HERMES_WORKER_NODE_MOCK_LLM=1）")


if __name__ == "__main__":
    main()
