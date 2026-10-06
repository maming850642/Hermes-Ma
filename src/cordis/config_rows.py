"""
============================================
插件行校验（config rows）
============================================
boot/boot_file 的输入是「插件行」列表，本模块负责把外部
（yaml / 调用方）给的行规范化为内核可消费的 dict：

- id      必填 str，全局唯一（也是拓扑排序的节点标识）
- plugin  必填，格式 "模块路径:属性名"，如 "src.cordis.xxx:apply"
- enabled 默认 True，bool
- config  默认 {}，dict
- inject  默认 []，list[str]，声明依赖的服务键

任何错误抛 ValueError，消息带行号与行 id（可用时），
让配置错误能在启动第一时间定位。
"""

from __future__ import annotations

import re

#: plugin 字段格式：模块路径:属性名（模块路径允许点分，属性名为合法标识符）
_PLUGIN_RE = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")


def validate_rows(rows: list) -> list[dict]:
    """校验插件行列表并填默认值。

    Args:
        rows: 原始行列表（通常来自 yaml 的 plugins: 段）

    Returns:
        规范化后的行列表，每行含 id/plugin/enabled/config/inject 五键

    Raises:
        ValueError: 任一行结构非法，消息含行号/行 id
    """
    validated: list[dict] = []
    seen_ids: set[str] = set()

    for lineno, row in enumerate(rows, start=1):
        where = f"第 {lineno} 行"
        if not isinstance(row, dict):
            raise ValueError(f"插件行必须是 dict（{where}），实际为 {type(row).__name__}")

        row_id = row.get("id")
        if isinstance(row_id, str) and row_id:
            where = f"{where}[id={row_id}]"

        if not isinstance(row_id, str) or not row_id:
            raise ValueError(f"插件行缺少必填字段 id 或 id 非非空字符串（{where}）")
        if row_id in seen_ids:
            raise ValueError(f"插件 id 重复: {row_id}（{where}）")
        seen_ids.add(row_id)

        plugin = row.get("plugin")
        if not isinstance(plugin, str) or not _PLUGIN_RE.match(plugin):
            raise ValueError(
                f"plugin 必须是 '模块路径:属性名' 格式（{where}），实际为 {plugin!r}"
            )

        enabled = row.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"enabled 必须是 bool（{where}），实际为 {enabled!r}")

        config = row.get("config", {})
        if not isinstance(config, dict):
            raise ValueError(
                f"config 必须是 dict（{where}），实际为 {type(config).__name__}"
            )

        inject = row.get("inject", [])
        if not isinstance(inject, list) or any(not isinstance(key, str) for key in inject):
            raise ValueError(f"inject 必须是 list[str]（{where}），实际为 {inject!r}")

        validated.append(
            {
                "id": row_id,
                "plugin": plugin,
                "enabled": enabled,
                "config": dict(config),
                "inject": list(inject),
            }
        )

    return validated
