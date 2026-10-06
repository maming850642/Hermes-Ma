"""
============================================
MCP 配置管理 - 文件夹模式(每个 server 一个 json 文件)
============================================
配置目录:mcp_servers/(项目根下)
每个 server 一个文件:mcp_<name>.json

新建 server = 写一个 json 文件;删除 = 删文件;reload = 重扫文件夹。
天然支持热加载,无需重启服务。

兼容:目录不存在时自动创建空目录。
"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from config import PROJECT_ROOT, settings

logger = logging.getLogger("hermes.mcp.config")


def get_mcp_config_path() -> Path:
    """返回 MCP 配置文件夹路径:mcp_servers/(项目根下)。

    向后兼容:旧代码可能传单个 yaml 路径,这里统一返回文件夹。
    """
    raw = (settings.get("mcp_servers_file") or "").strip() if settings else ""
    if raw:
        p = Path(raw)
        p = p if p.is_absolute() else (PROJECT_ROOT / p)
        if p.is_dir():
            return p
    return PROJECT_ROOT / "mcp_servers"


# 允许的信任级别（tool_factory._get_server_trust 按它决定 side_effects 标注：
# full=仅 network_access；approval/deny=保守全标 destructive+network_access）
_TRUST_LEVELS = ("full", "approval", "deny")


@dataclass
class McpServerConfig:
    """单个 MCP Server 的配置。"""
    name: str
    enabled: bool = True
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    # 用户声明的信任级别（P1-9）：full=信任该 server 的全部工具；
    # approval=保守审批（默认）；deny=不信任。未配置时默认 approval（最安全）。
    trust: str = "approval"

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "McpServerConfig":
        # 兼容 type 字段(很多 MCP 配置用 type 而非 transport)
        transport = data.get("transport") or data.get("type") or "stdio"
        # type=http → streamable_http(fastmcp 的传输名)
        if transport == "http":
            transport = "streamable_http"
        return cls(
            name=data.get("name", name) or name,
            enabled=data.get("enabled", True),
            transport=transport,
            command=data.get("command", ""),
            args=data.get("args", []),
            env=data.get("env", {}),
            url=data.get("url", ""),
            headers=data.get("headers", {}),
            trust=str(data.get("trust") or "approval").strip().lower(),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "transport": self.transport,
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "url": self.url,
            "headers": self.headers,
            "trust": self.trust,
        }

    def validate(self) -> str | None:
        if self.transport == "stdio":
            if not self.command:
                return "stdio 模式需要 command 字段"
        elif self.transport in ("sse", "streamable_http"):
            if not self.url:
                return f"{self.transport} 模式需要 url 字段"
        else:
            return f"不支持的传输类型: {self.transport}"
        if self.trust not in _TRUST_LEVELS:
            return f"非法 trust: {self.trust!r}（须为 {'/'.join(_TRUST_LEVELS)}）"
        return None


class McpConfigManager:
    """MCP 配置管理器:扫描 mcp_servers/ 文件夹,每个 server 一个 json 文件。"""

    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or get_mcp_config_path()

    def _server_file(self, name: str) -> Path:
        """单个 server 的 json 文件路径:mcp_<name>.json"""
        safe_name = "".join(c for c in name if c.isalnum() or c in ("-", "_"))
        return self.config_path / f"mcp_{safe_name}.json"

    def load(self) -> dict[str, McpServerConfig]:
        """扫描文件夹,加载所有 mcp_*.json。文件夹不存在返回 {}。"""
        if not self.config_path.exists():
            logger.debug(f"MCP 配置目录不存在: {self.config_path}")
            return {}

        result: dict[str, McpServerConfig] = {}
        for f in sorted(self.config_path.glob("mcp_*.json")):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    logger.warning(f"MCP 配置 {f.name} 不是 JSON 对象,跳过")
                    continue
                name = data.get("name") or f.stem[4:]
                cfg = McpServerConfig.from_dict(name, data)
                err = cfg.validate()
                if err:
                    logger.warning(f"MCP server '{name}' 配置无效: {err}")
                    continue
                result[name] = cfg
            except Exception as e:
                logger.warning(f"解析 MCP 配置 {f.name} 失败: {e}")

        logger.info(f"已加载 {len(result)} 个 MCP server 配置(从 {self.config_path})")
        return result

    def save_single(self, config: McpServerConfig) -> Path:
        """保存单个 server 到 mcp_<name>.json(原子写:临时文件+rename)。"""
        self.config_path.mkdir(parents=True, exist_ok=True)
        fpath = self._server_file(config.name)
        tmp = fpath.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, fpath)
        logger.info(f"MCP server '{config.name}' 已保存: {fpath}")
        return fpath

    def delete_single(self, name: str) -> bool:
        """删除单个 server 的 json 文件。返回是否删除成功。"""
        fpath = self._server_file(name)
        if fpath.exists():
            fpath.unlink()
            logger.info(f"MCP server '{name}' 配置已删除: {fpath}")
            return True
        return False

    def ensure_default_file(self) -> None:
        """确保配置目录存在(创建空目录)。"""
        self.config_path.mkdir(parents=True, exist_ok=True)
