"""
============================================
Hermes Rich CLI 交互包
============================================
基于 Rich 的命令行界面（单用户，身份恒 LOCAL_USER），支持：
- 对话（V3 引擎，工具调用 + HITL 审批 + 工作区双模式）
- 内置命令（/mode, /resume, /rename, /skill, /mcp, /memory, /clear,
  /tools, /compact, /reset, /save, /help, /exit）
- 会话持久化（JSON 快照 + 事件流双轨，真源为事件日志）

工作流程：
    显示 Logo → boot_context 组合根 → 启动自检 → 对话循环 → 退出

拆包说明（P2：原单文件 src/cli.py → 本包，公共接口零变化）：
- render.py     展示层：console 单例 / show_* / 面板构建器 / 历史渲染 / 会话选择器
- commands.py   斜杠命令族：_cmd_* / compact_session
- chat_loop.py  对话核心：chat()（Live 流式 + HITL 审批 + 自动分段）
- entry.py      入口：main()（REPL + 斜杠分发表）+ _shutdown_mcp_quietly

兼容约定（零行为变化）：
1. 本 __init__ re-export 全部公共名——`from src.cli import main` / `import
   src.cli` 等外部调用形状与拆包前逐名等价（含到 src.session_store 的
   re-import：SESSIONS_DIR / SUMMARIES_DIR 绑定副本仍从本包可 patch）；
2. 拆包前 console / Live / Prompt / chat 等是单模块全局，测试与外部代码
   可整体替换 `src.cli.X`；拆包后子模块在使用处**调用期**经
   `from src.cli import X` 再绑定（见各子模块 docstring），该 patch 语义
   保持不变。
"""

# ============================================
# namespace 等价层：原 src/cli.py 顶部的 import 原样保留为包属性
# ============================================
import sys  # noqa: F401
import os  # noqa: F401
import uuid  # noqa: F401
import logging  # noqa: F401
import threading  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Optional  # noqa: F401

from rich.console import Console, Group  # noqa: F401
from rich.live import Live  # noqa: F401
from rich.panel import Panel  # noqa: F401
from rich.prompt import Prompt  # noqa: F401
from rich.table import Table  # noqa: F401
from rich.text import Text  # noqa: F401
from rich.markdown import Markdown  # noqa: F401
from rich import box  # noqa: F401
from rich.markup import escape  # noqa: F401

from config import get_settings, PROJECT_ROOT  # noqa: F401
from src.constants import LOCAL_USER  # noqa: F401
from src.memory import MemoryManager  # noqa: F401
from src.agent import HermesAgentV3  # noqa: F401
from src.health import run_health_check  # noqa: F401
from src.tools.virtual_fs import get_virtual_fs  # noqa: F401
from src.agent.context import ContextManager  # noqa: F401
from src.agent.multimodal import extract_text  # noqa: F401

logger = logging.getLogger("hermes.cli")

# ============================================
# 会话持久化 —— T3 已抽取到 src/session_store.py（行为零变化）
# 此处 re-import 保持 `from src.cli import save_session` 等调用形状不变
# ============================================
from src.session_store import (  # noqa: F401
    SESSIONS_DIR,
    SUMMARIES_DIR,
    lc_to_dict,
    load_message,
    _ensure_sessions_dir,
    _migrate_old_sessions,
    _get_session_file,
    ensure_session_stub,
    save_session,
    load_session,
    list_sessions,
)

# ============================================
# 子模块 re-export：公共接口（与拆包前 src/cli.py 逐名等价）
# ============================================
from .render import (  # noqa: F401
    console,
    _show_and_pick_session,
    show_logo,
    show_help,
    show_skills,
    show_memory,
    clear_memory,
    describe_workspace,
    show_tools,
    show_session_history,
    _format_tool_args,
    _estimate_lines,
    _build_memory_search_panel,
    _build_tool_panel,
)
from .commands import (  # noqa: F401
    _project_stores,
    _cmd_project,
    _project_activate,
    _cmd_events,
    _cmd_fork,
    _cmd_waker,
    _cmd_flow,
    _model_profiles,
    _cmd_model,
    compact_session,
)
from .chat_loop import chat  # noqa: F401
from .entry import _shutdown_mcp_quietly, main  # noqa: F401
