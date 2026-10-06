"""
============================================
Hermes Rich CLI —— 斜杠命令族（_cmd_* + compact_session）
============================================
P2 拆包自 src/cli.py（函数体原样搬出，行为零变化）：
- 项目空间：_project_stores / _cmd_project / _project_activate
- 事件轨迹与分支：_cmd_events / _cmd_fork
- 数字员工 / 工作流：_cmd_waker / _cmd_flow
- 模型热切换：_model_profiles / _cmd_model
- 手动压缩：compact_session

拆包兼容层（零行为变化）：console / ContextManager 拆包前是 cli 单模块
全局，测试可整体替换 `src.cli.console` / `src.cli.ContextManager`；命令体
在使用处一律**调用期** `from src.cli import ...` 再绑定，保持该语义不变。
"""

import logging
from pathlib import Path

from rich.markup import escape
from rich.table import Table

from config import get_settings
from src.session_store import ensure_session_stub

logger = logging.getLogger("hermes.cli")


# ============================================
# 项目空间 / 事件轨迹 / waker / flow（与 Web 能力面对齐）
# ============================================

def _project_stores(boot_ctx):
    """从组合根取 ProjectStore 与 WorkspaceService（与 Web 同一 SQLite kv）。"""
    from src.storage.projects_store import ProjectStore
    from src.workspace.service import WorkspaceService

    provider = boot_ctx.get("storage")
    return ProjectStore(provider), WorkspaceService(provider)


def _cmd_project(boot_ctx, arg: str) -> None:
    """`/project [list|switch <slug>|new <名称>|off|info]`——项目空间管理。

    激活流水线与 Web /api/projects/activate 完全一致（choose_chat_only /
    mount_local → touch → set_active），激活状态存共享 kv：CLI 切换后
    Web 端刷新即见同一激活项。
    """
    from src.cli import console  # 调用期再绑定（拆包兼容层，见模块 docstring）
    store, ws = _project_stores(boot_ctx)
    parts = arg.split(maxsplit=1)
    sub = parts[0].lower() if parts else "list"
    operand = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("list", "info", ""):
        active = store.active_slug()
        table = Table(title="📁 项目空间", show_header=True, border_style="dim")
        table.add_column("激活", width=4)
        table.add_column("slug", style="cyan")
        table.add_column("类型", width=8)
        table.add_column("路径", style="dim", overflow="fold")
        for p in store.list():
            mark = "[green]●[/green]" if p["slug"] == active else ""
            table.add_row(mark, p["slug"], p["type"], p.get("path") or "—")
        console.print(table)
        return

    if sub == "switch":
        if not operand:
            console.print("  [red]用法: /project switch <slug>[/red]")
            return
        record = store.get(operand)
        if record is None:
            console.print(f"  [red]项目不存在: {operand}[/red]（/project 查看列表）")
            return
        _project_activate(store, ws, record)
        return

    if sub == "new":
        if not operand:
            console.print("  [red]用法: /project new <名称>[/red]")
            return
        record = store.create(operand, "hosted")
        Path(record["path"]).mkdir(parents=True, exist_ok=True)
        _project_activate(store, ws, record)
        return

    if sub == "off":
        record = {"slug": "inbox", "type": "inbox", "name": "收件箱", "path": ""}
        _project_activate(store, ws, record)
        return

    console.print(f"  [red]未知子命令: {sub}[/red]（list / switch <slug> / new <名称> / off）")


def _project_activate(store, ws, record: dict) -> None:
    """激活流水线：与 Web activate_project 端点同构（挂载 → touch → set_active）。"""
    from src.cli import console  # 调用期再绑定（拆包兼容层，见模块 docstring）
    from src.workspace.service import WorkspaceError

    try:
        ptype = record["type"]
        if ptype == "inbox":
            ws.choose_chat_only()
        else:
            root = Path(record["path"])
            if ptype == "hosted":
                root.mkdir(parents=True, exist_ok=True)
            elif not record.get("path") or not root.exists():
                console.print(f"  [red]项目目录不可用: {record.get('path', '')}[/red]")
                return
            ws.mount_local(str(root), display_name=record.get("name") or record["slug"])
    except WorkspaceError as e:
        console.print(f"  [red]挂载失败: {escape(str(e))}[/red]")
        return
    except Exception as e:
        console.print(f"  [red]挂载服务异常: {escape(str(e))}[/red]")
        return
    store.touch(record["slug"])
    store.set_active(record["slug"])
    console.print(f"  ✅ 已切换项目空间: [cyan]{record['slug']}[/cyan]（Web 端共享同一激活状态）")


def _cmd_events(boot_ctx, session_id: str, arg: str) -> None:
    """`/events [条数]`——当前会话的事件溯源轨迹。"""
    from src.cli import console  # 调用期再绑定（拆包兼容层，见模块 docstring）
    log = boot_ctx.get("sessions") if boot_ctx is not None else None
    if log is None:
        console.print("  [red]事件存储不可用（组合根未启动）[/red]")
        return
    try:
        n = max(1, int(arg)) if arg.strip() else 20
    except ValueError:
        console.print("  [red]用法: /events [条数][/red]")
        return
    # 冷归档合并视图（P3）：冷区 + 热表按 id 合并，与归档前 events() 等价
    evs = log.load_events_with_archive(session_id)
    if not evs:
        console.print("  [dim]当前会话还没有事件（发一条消息后产生）[/dim]")
        return
    table = Table(title=f"🧾 事件流（最近 {min(n, len(evs))} / 共 {len(evs)} 条）",
                  show_header=True, border_style="dim")
    table.add_column("id", style="dim", width=6, justify="right")
    table.add_column("类型", style="cyan", width=20)
    table.add_column("摘要", overflow="fold")
    for e in evs[-n:]:
        p = e.get("payload") or {}
        summary = p.get("input") or p.get("content") or p.get("tool") or p.get("action") or ""
        summary = escape(str(summary).replace("\n", " ")[:70])
        if e.get("payload", {}).get("cancelled"):
            summary += " [red](cancelled)[/red]"
        table.add_row(str(e.get("id", "")), e.get("type", ""), summary)
    console.print(table)


def _cmd_fork(boot_ctx, user_id: str, session_id: str, session_messages: list,
              arg: str) -> str | None:
    """`/fork [事件ID]`——复制当前会话事件流为新分支并切换。

    返回新 session_id（调用方负责替换主循环状态）；返回 None 表示未 fork。
    """
    from src.cli import console  # 调用期再绑定（拆包兼容层，见模块 docstring）
    log = boot_ctx.get("sessions") if boot_ctx is not None else None
    if log is None:
        console.print("  [red]事件存储不可用（组合根未启动）[/red]")
        return None
    up_to = None
    if arg.strip():
        try:
            up_to = int(arg)
        except ValueError:
            console.print("  [red]用法: /fork [事件ID]（/events 查看 id；不给 id = 全量复制）[/red]")
            return None

    # 冷归档合并视图（P3）：截断点落冷区也能切（与 Web fork 端点同语义）
    src_events = log.load_events_with_archive(session_id)
    if up_to is not None:
        src_events = [e for e in src_events if e.get("id", 0) <= up_to]

    import uuid as _uuid
    new_sid = None
    for _ in range(5):
        candidate = str(_uuid.uuid4())[:8]
        try:
            occupied = bool(log.events(candidate))
        except Exception:
            occupied = False
        if not occupied:
            new_sid = candidate
            break
    if new_sid is None:
        console.print("  [red]新会话 ID 连续冲突，请重试[/red]")
        return None

    for ev in src_events:
        log.append(new_sid, ev.get("type", ""), ev.get("payload") or {})
    actual_up_to = max((e.get("id", 0) for e in src_events), default=0)
    # JSON 快照 stub：与 Web fork 端点同构——内容用事件投影（derive），
    # project 继承源会话（ADR-0005 D2；丢失会让分支在项目过滤下不可见）
    from src.session_store import read_session_meta
    src_project = (read_session_meta(user_id, session_id) or {}).get("project", "")
    ensure_session_stub(user_id, log.derive_messages(new_sid), new_sid,
                        name=f"fork:{session_id[:8]}", project=src_project)
    console.print(
        f"  ✅ 已创建分支 [cyan]{new_sid}[/cyan]"
        f"（复制 {len(src_events)} 条事件"
        + (f"，截至事件 {actual_up_to}" if up_to is not None else "，全量")
        + "），已切换过去"
    )
    return new_sid


def _cmd_waker(arg: str, user_id: str, agent) -> None:
    """`/waker [list|run <名>]`——数字员工列表与立即执行。"""
    from src.cli import console  # 调用期再绑定（拆包兼容层，见模块 docstring）
    from src.waker.store import WakerStore

    store = WakerStore(user_id)
    names = store.list_names() if hasattr(store, "list_names") else None
    parts = arg.split(maxsplit=1)
    sub = parts[0].lower() if parts else "list"

    if sub == "run":
        name = parts[1].strip() if len(parts) > 1 else ""
        if not name:
            console.print("  [red]用法: /waker run <名称>[/red]")
            return
        import uuid as _uuid
        from types import SimpleNamespace
        from src.waker.runner import run_waker

        console.print(f"  ⏳ [dim]正在执行 waker「{name}」（完整一轮 agent 任务）…[/dim]")
        state = SimpleNamespace(user_id=user_id, agent=agent)
        result = run_waker(state, name, str(_uuid.uuid4())[:8])
        if result.get("status") == "ok":
            console.print(f"  ✅ waker「{name}」执行完成，结果已写入 latest_result.md")
        else:
            console.print(f"  [red]❌ 执行失败: {result.get('message', '未知错误')}[/red]")
        return

    # list（默认）
    cfgs = store.list()
    if not cfgs:
        console.print("  [dim]暂无 waker。在 data/home/wakers/ 下创建目录（含 waker.yaml）或用 Web 端管理。[/dim]")
        return
    table = Table(title="🧑‍💼 数字员工", show_header=True, border_style="dim")
    table.add_column("名称", style="cyan")
    table.add_column("状态", width=6)
    table.add_column("调度", width=12)
    table.add_column("描述", overflow="fold")
    for c in cfgs:
        table.add_row(
            escape(c.name),
            "启用" if c.enabled else "停用",
            str(c.schedule_type or "—"),
            escape((c.description or "—")[:60]),
        )
    console.print(table)
    console.print("[dim]💡 /waker run <名称> 立即执行一轮任务[/dim]")


def _cmd_flow(arg: str, user_id: str, agent) -> None:
    """`/flow [list|run <名>]`——WakerFlow 工作流列表与运行。

    run 走 FlowRunner.submit（与 Web invoke 端点同一执行器，线程池异步），
    CLI 侧同步轮询 get_status 直到结束（上限 5 分钟，与 Web 前端一致）。
    """
    from src.cli import console  # 调用期再绑定（拆包兼容层，见模块 docstring）
    from src.wakerflow.store import FlowStore

    store = FlowStore(user_id)
    parts = arg.split(maxsplit=1)
    sub = parts[0].lower() if parts else "list"

    if sub == "run":
        name = parts[1].strip() if len(parts) > 1 else ""
        if not name:
            console.print("  [red]用法: /flow run <名称>[/red]")
            return
        from src.wakerflow.runner import FlowRunner

        if store.get(name) is None:
            console.print(f"  [red]flow 不存在: {name}[/red]")
            return
        runner = FlowRunner()
        runner.start()
        try:
            run_id = runner.submit(user_id, name, {})
        except ValueError as e:
            console.print(f"  [red]❌ 提交失败: {escape(str(e))}[/red]")
            return
        except RuntimeError as e:
            console.print(f"  [red]❌ {escape(str(e))}[/red]")
            return
        console.print(f"  ⏳ [dim]工作流「{name}」已提交（run_id: {run_id}），运行中…[/dim]")
        import time as _time
        deadline = _time.time() + 300
        rec: dict = {}
        while _time.time() < deadline:
            rec = runner.get_status(run_id) or {}
            if rec.get("status") not in ("pending", "running", "unknown"):
                break
            _time.sleep(2)
        status = rec.get("status", "unknown")
        if status == "completed":
            console.print(f"  ✅ 工作流「{name}」运行完成")
            for k, v in (rec.get("returns") or {}).items():
                console.print(f"  [dim]↳ {k}: {escape(str(v)[:120])}[/dim]")
        else:
            console.print(f"  [red]❌ 工作流「{name}」结束于 {status}[/red]"
                          + (f"：{escape(str(rec.get('error', '')))}" if rec.get("error") else ""))
        runner.shutdown()
        return

    flows = store.list()
    if not flows:
        console.print("  [dim]暂无工作流。在 data/home/wakerflows/ 下创建目录（含 flow.yaml）或用 Web 端管理。[/dim]")
        return
    table = Table(title="🔀 WakerFlow 工作流", show_header=True, border_style="dim")
    table.add_column("名称", style="cyan")
    table.add_column("描述", overflow="fold")
    for fname, fdesc in flows:
        table.add_row(escape(fname), escape((fdesc or "—")[:70]))
    console.print(table)
    console.print("[dim]💡 /flow run <名称> 运行一次工作流[/dim]")


def _model_profiles() -> list:
    """读取模型注册表落盘文件 data/home/model_profiles.json（若存在）。

    CLI 刻意不 import 模型注册表模块（避免耦合），直接 json 读取档案
    字段 {id, display, model[, base_url, api_key, context_window]}。
    解析失败/文件不存在一律返回空列表（/model 只展示当前模型）。
    """
    import json

    from src.storage.paths import agent_home

    path = agent_home("model_profiles.json")
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"model_profiles.json 读取失败（忽略）: {e}")
        return []
    if isinstance(data, list):
        return [p for p in data if isinstance(p, dict)]
    if isinstance(data, dict) and isinstance(data.get("profiles"), list):
        return [p for p in data["profiles"] if isinstance(p, dict)]
    return []


def _cmd_model(agent, arg: str) -> None:
    """`/model [id]`——模型热切换（无参=查看当前与列表，带 id=切换）。

    切换经 agent.set_llm_params 注入（与 Web 系统配置保存同一链路），
    已缓存客户端作废，下一轮对话即按新模型重建——无需重启。
    """
    from src.cli import console  # 调用期再绑定（拆包兼容层，见模块 docstring）
    if not arg:
        overrides = getattr(agent, "_llm_overrides", None) or {}
        current = overrides.get("model") or get_settings().llm_model_name
        console.print(f"\n  当前模型: [cyan]{current}[/cyan]")
        profiles = _model_profiles()
        if not profiles:
            console.print("  [dim]暂无模型档案（data/home/model_profiles.json 不存在或为空）[/dim]\n")
            return
        table = Table(title="🧠 可用模型", show_header=True, border_style="dim")
        table.add_column("id", style="cyan")
        table.add_column("显示名")
        table.add_column("model", style="dim", overflow="fold")
        for p in profiles:
            table.add_row(escape(str(p.get("id", ""))), escape(str(p.get("display", ""))),
                          escape(str(p.get("model", ""))))
        console.print(table)
        console.print("[dim]💡 /model <id> 热切换（下一轮对话生效，无需重启）[/dim]\n")
        return

    profile = next((p for p in _model_profiles() if str(p.get("id", "")) == arg), None)
    if profile is None:
        console.print(f"  [red]模型档案不存在: {arg}[/red]（/model 查看列表）\n")
        return
    # 四键全量 + clear 模式：省略键=清除该项 override（与 Web PUT /model 同语义），
    # 避免跨档案切换时 api_key/context_window 残留上一个档案的值
    params = {
        "model": profile.get("model") or "",
        "base_url": profile.get("base_url") or "",
        "api_key": profile.get("api_key") or "",
        "context_window": profile.get("context_window"),
        "clear_model_overrides": True,
    }
    if not params["model"]:
        console.print(f"  [red]档案 {arg} 缺少 model 字段，无法切换[/red]\n")
        return
    agent.set_llm_params(**params)
    console.print(f"  ✅ [green]已切换模型: {params['model']}[/green]（下一轮对话生效，无需重启）\n")


def compact_session(boot_ctx, session_id: str, session_messages: list):
    """
    手动压缩对话历史：委托给 ContextManager.compact_messages() 统一执行。

    压缩逻辑已收归到 ContextManager 中，此处仅负责 CLI 显示。
    P3（二轮审查）：对齐 agent 的 _compact_messages——压缩落定后写
    compact/applied durable 事件（含保留区），否则 /fork 按事件流复制
    分支时投影重建出压缩前全量历史（压缩前消息复活）。
    """
    from src.cli import ContextManager, console  # 调用期再绑定（拆包兼容层，见模块 docstring）
    ctx_mgr = ContextManager()

    if not ctx_mgr.should_auto_compact(session_messages):
        console.print("  [dim]对话历史太短，无需压缩[/dim]\n")
        return

    console.print("  ⏳ [dim]正在压缩对话历史...[/dim]")

    try:
        result = ctx_mgr.compact_messages(session_messages)
    except Exception as e:
        console.print(f"  [red]❌ 压缩失败: {escape(str(e))}[/red]\n")
        return

    if result is None:
        console.print("  [dim]对话历史太短，无需压缩[/dim]\n")
        return

    console.print(f"  ✅ [green]对话已压缩：{result.compacted_count} 条早期消息 → 1 条摘要[/green]")
    console.print(f"  [dim]摘要预览: {escape(result.summary[:100])}...[/dim]\n")

    # durable：compact/applied（与 agent _compact_messages 同型 payload；
    # kept_messages = 压缩后保留区去掉头部摘要 system 消息，lc_to_dict
    # 规范化——derive_messages 据此重建投影，fork 分支不再复活压缩前历史）
    log = boot_ctx.get("sessions") if boot_ctx is not None else None
    if log is None or not session_id:
        return
    try:
        from src.agent.session_log import COMPACT_APPLIED, build_compact_applied_payload
        log.append(session_id, COMPACT_APPLIED,
                   build_compact_applied_payload(result, session_messages))
    except Exception:
        logger.warning("compact/applied 事件写入失败（忽略）", exc_info=True)
