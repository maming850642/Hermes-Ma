"""页面路由（返回 HTML 壳模板）。

免认证形态（ADR-0005）：页面本身无鉴权，API 匿名可达。
`/` 为欢迎页/项目选择器（替代原登录页；工作区挂载引导已并入欢迎页）。
"""
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _active_project() -> str:
    """当前激活项目 slug（注入页面 meta，前端按它过滤/恢复会话）。

    get_active_project 任何失败都回落 inbox，页面渲染不因项目存储故障 500。
    """
    from src.storage.projects_store import get_active_project
    return get_active_project()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """欢迎页：项目空间选择器（最近项目 / 新建 / 打开目录 / 克隆 / 直接开聊）。"""
    return _templates.TemplateResponse(request=request, name="home.html", context={})


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    """聊天主界面。"""
    return _templates.TemplateResponse(request=request, name="chat.html",
                                       context={"active_project": _active_project()})


@router.get("/sessions")
async def sessions_page(request: Request):
    """会话页已下线（2026-09-05）：切换/改名/删除/复制分支/事件流能力全部
    迁入对话页侧栏（跨项目总览放弃），整页 302 到 /chat。
    会话 API 端点（routers/sessions.py）不受影响、全保留。"""
    return RedirectResponse(url="/chat", status_code=302)


@router.get("/memory", response_class=HTMLResponse)
async def memory_page(request: Request):
    return _templates.TemplateResponse(request=request, name="memory.html", context={})


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    return _templates.TemplateResponse(request=request, name="config.html", context={})


@router.get("/waker", response_class=HTMLResponse)
async def waker_page(request: Request):
    """数字员工（waker）管理页。"""
    return _templates.TemplateResponse(request=request, name="waker.html", context={})


@router.get("/wakerflow", response_class=HTMLResponse)
async def wakerflow_page(request: Request):
    """WakerFlow（编排）管理页。"""
    return _templates.TemplateResponse(request=request, name="wakerflow.html", context={})


@router.get("/usage", response_class=HTMLResponse)
async def usage_page(request: Request):
    """Token 用量看板（ADR-0005 D2）。"""
    return _templates.TemplateResponse(request=request, name="usage.html", context={})
