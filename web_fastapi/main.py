"""Web 启动入口（对标 main.py）。"""
import os
import sys

# 清除无效 SSL_CERT_FILE（与 main.py 一致）
if os.environ.get("SSL_CERT_FILE") and not os.path.exists(os.environ["SSL_CERT_FILE"]):
    del os.environ["SSL_CERT_FILE"]

import config  # noqa: F401
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*LangChainPendingDeprecationWarning.*")

from src.logging_config import setup_logging

_settings = config.get_settings()
setup_logging(
    debug="--debug" in sys.argv,
    log_dir=_settings.get("log_dir", "logs"),
    log_to_file=_settings.get("log_to_file", True),
)

# S2 安全基线：默认仅本机监听。应用为免认证形态（ADR-0005）：无任何访问
# 控制，绑 0.0.0.0 意味着局域网内任何人都能以本地用户身份使用完整 Agent 能力。
DEFAULT_WEB_HOST = "127.0.0.1"


def resolve_web_host(settings=None) -> str:
    """解析 Web 监听地址。优先级：env WEB_HOST > settings.web_host > 127.0.0.1。"""
    if settings is None:
        settings = config.get_settings()
    return os.environ.get("WEB_HOST", settings.get("web_host", DEFAULT_WEB_HOST))


def warn_if_exposed(host: str, port: int) -> bool:
    """host 绑定到非回环地址时打印显著安全警告。返回是否发生了告警。

    风险组合：免认证（任何能访问端口的人都是 LOCAL_USER）+ 非回环暴露。
    """
    if host in ("127.0.0.1", "localhost", "::1"):
        return False
    print(
        f"\n"
        f"  ┌──────────────────────── 安全警告 ────────────────────────┐\n"
        f"  │ Web 服务监听 {host} —— 局域网内其他设备可以访问。        │\n"
        f"  │ 当前服务没有任何访问控制：任何能访问 {host}:{port} 的设备│\n"
        f"  │ 都能以本地用户身份使用完整 Agent 能力（文件/Shell/工具）。│\n"
        f"  │ 建议：仅绑定回环地址，或自行为端口加反向代理鉴权，      │\n"
        f"  │ 并确认只暴露给完全可信的网络。                          │\n"
        f"  └──────────────────────────────────────────────────────────┘\n",
        flush=True,
    )
    return True


if __name__ == "__main__":
    import uvicorn
    # 默认 127.0.0.1 仅本机监听（S2 安全基线）。
    # 局域网使用：设 WEB_HOST=0.0.0.0 或 config.yaml web_host（会打印暴露警告）
    host = resolve_web_host()
    port = int(os.environ.get("WEB_PORT", _settings.get("web_port", 8000)))
    warn_if_exposed(host, port)

    # 打印访问地址（Windows 上浏览器不能用 0.0.0.0 访问，要用 localhost/局域网IP）
    import socket
    try:
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _s.connect(("8.8.8.8", 80))
        lan_ip = _s.getsockname()[0]
        _s.close()
    except Exception:
        lan_ip = "127.0.0.1"
    print(f"\n  ⚡ Hermes-Ma Web 已启动")
    print(f"     本机访问:   http://127.0.0.1:{port}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"     局域网访问: http://{lan_ip}:{port}\n")

    uvicorn.run(
        "web_fastapi.app:create_app",
        factory=True, host=host, port=port,
        reload="--debug" in sys.argv,
    )
