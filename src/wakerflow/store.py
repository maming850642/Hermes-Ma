"""
============================================
FlowStore —— WakerFlow 定义 + run 历史存储（单用户，T2b-② 拍平）
============================================
布局（每个 flow = <root>/wakerflows/<name>/ 下的一组文件）：

    flow.yaml               flow 定义（用户编写的 YAML，parser 解析为 FlowSpec）
    runs/<run_id>.jsonl     每次运行的事件日志（executor 通过 on_event 写）
    _approvals/<run_id>.json  pending 审批（M2.3 用，本任务只建目录/提供路径辅助）

根解析（root = 显式 workspace_root 参数（agent home 语义）or
paths.agent_home()）、原子写一律复用 src.waker.store 的同名工具函数
（_resolve_workspace / _atomic_write_text），保持与 WakerStore 行为一致。
user_id 参数保留（兼容调用面）但路径不再使用。

## 与 executor / parser 的分工
- parser：把 flow.yaml 文本解析成 FlowSpec（M2.2a，本任务不碰）
- store：纯文件 CRUD + 路径辅助，不做解析（get 返回 yaml 文本，
  调用方自行用 parser 解析）
- executor：用 FlowStore 拿 run 目录路径、写 jsonl（通过传入的 on_event 回调）

## 与 WakerStore 的差异
- WakerStore.get 返回 WakerConfig 对象（解析后）；FlowStore.get 返回 yaml
  文本（未解析）——因为 FlowSpec 解析逻辑在 parser 子系统，store 不依赖 parser，
  避免循环 import。调用方按需 `FlowSpec.from_yaml(store.get(name))`。
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from pathlib import Path

from src.constants import LOCAL_USER

# 复用 src.waker.store 的根解析 + 原子写工具（单一来源，不重写）
from src.waker.store import (
    _resolve_workspace,
    _atomic_write_text,
)

logger = logging.getLogger("hermes.wakerflow.store")

# Flow name 字符集（与 src.wakerflow.parser._NAME_RE 同款，parser.py:38）。
# 本地定义对齐而非 import：store 按模块契约不依赖 parser（见文件头），
# 两处须同步修改。save 是 name 进 store 的唯一入口，在此统一强制，
# yaml / blocks / 重命名等创建路径全部收敛——blocks 路径 canvas.blocks_to_flow
# 纯 dataclass 构造绕过 parser，yaml 路径 router 存的顶层 body.name 与
# parse_flow 校验的 yaml 内层 name 可不同，前端卡片渲染依赖名称安全。
_FLOW_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class FlowStore:
    """WakerFlow 文件存储（单用户，agent home 下 wakerflows/）。

    Args:
        user_id: 已废弃（单用户，保参兼容调用面）
        workspace_root: agent home 根。空则自动解析（同 WakerStore）
    """

    def __init__(self, user_id: str, workspace_root: str = ""):
        self.user_id = user_id
        self._ws = _resolve_workspace(workspace_root)
        self._flows_root = self._ws / "wakerflows"

    # ---------- 路径辅助 ----------
    @property
    def flows_root(self) -> Path:
        return self._flows_root

    @staticmethod
    def _safe_component(value: str, what: str = "name") -> str:
        """路径组件防穿越校验（getter 纵深防御：URL 参数直达 store 的入口）。"""
        value = value or ""
        if (not value or value.startswith("_") or "/" in value or "\\" in value
                or value in (".", "..") or ":" in value):
            raise ValueError(f"非法 {what}: {value!r}")
        return value

    def flow_dir(self, name: str) -> Path:
        return self._flows_root / self._safe_component(name, "flow name")

    def _yaml_path(self, name: str) -> Path:
        return self.flow_dir(name) / "flow.yaml"

    def run_dir(self, name: str) -> Path:
        """某 flow 的 run 日志目录（runs/）。"""
        return self.flow_dir(name) / "runs"

    def run_jsonl_path(self, name: str, run_id: str) -> Path:
        """某次 run 的 jsonl 事件日志路径。"""
        return self.run_dir(name) / f"{self._safe_component(run_id, 'run_id')}.jsonl"

    def approvals_dir(self) -> Path:
        """pending 审批目录（M2.3 用，本任务只提供路径辅助）。

        放在 wakerflows/_approvals/（下划线前缀避免与 flow 名冲突）。
        """
        return self._flows_root / "_approvals"

    def approval_path(self, run_id: str) -> Path:
        """某次 run 的 pending 审批文件路径（M2.3 用）。

        run_id 过 _safe_component（P3-4，对齐 run_jsonl_path）：URL 可达
        此路径构造，Windows 下 %5C 反斜杠可穿越出 approvals 目录。
        """
        return self.approvals_dir() / f"{self._safe_component(run_id, 'run_id')}.json"

    def new_run_id(self) -> str:
        """生成 run_id：时间戳 + 短随机（与 WakerStore.new_run_id 同风格）。"""
        return f"{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"

    def state_path(self, name: str) -> Path:
        """某 flow 的运行时状态文件路径（state.json）。"""
        return self.flow_dir(name) / "state.json"

    def load_state(self, name: str):
        """读取 flow 的运行时状态。不存在返回空 FlowState。"""
        from src.wakerflow.models import FlowState
        sp = self.state_path(name)
        if not sp.exists():
            return FlowState()
        try:
            import json as _json
            data = _json.loads(sp.read_text(encoding="utf-8"))
            return FlowState(
                run_count=int(data.get("run_count", 0)),
                last_run_at=str(data.get("last_run_at", "")),
                last_status=str(data.get("last_status", "")),
                next_run_at=str(data.get("next_run_at", "")),
            )
        except Exception as e:
            logger.warning(f"读 flow state 失败 {sp}: {e}")
            return FlowState()

    def save_state(self, name: str, state) -> None:
        """保存 flow 的运行时状态（原子写）。"""
        import json as _json
        sp = self.state_path(name)
        sp.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "run_count": state.run_count,
            "last_run_at": state.last_run_at,
            "last_status": state.last_status,
            "next_run_at": state.next_run_at,
        }
        _atomic_write_text(sp, _json.dumps(data, ensure_ascii=False, indent=2))

    @staticmethod
    def new_api_token() -> str:
        """生成 API token（与 WakerConfig.new_api_token 同风格）。"""
        return secrets.token_urlsafe(24)

    # ---------- 读 ----------
    def list(self) -> list[tuple[str, str]]:
        """列出全部 flow。

        Returns:
            list of (name, yaml_text)。name 按 ASCII 排序。
            解析失败的 flow 仍列入（返回其原始文本，调用方自行处理），
            但读 yaml 文本本身失败的（编码/IO 错误）跳过并记日志。
        """
        out: list[tuple[str, str]] = []
        if not self._flows_root.exists():
            return out
        for entry in sorted(self._flows_root.iterdir()):
            if not entry.is_dir():
                continue
            # 跳过审批目录（下划线前缀的辅助目录，非 flow）
            if entry.name.startswith("_"):
                continue
            yp = entry / "flow.yaml"
            if not yp.exists():
                continue
            try:
                text = yp.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning(f"读 flow.yaml 失败 {yp}: {e}")
                continue
            out.append((entry.name, text))
        return out

    def get(self, name: str) -> str | None:
        """按 name 取单个 flow 的 yaml 文本。不存在返回 None。

        注意：返回的是**原始 yaml 文本**（未解析）。调用方按需用 parser：
            spec = FlowSpec.from_yaml(store.get(name))
        """
        yp = self._yaml_path(name)
        if not yp.exists():
            return None
        try:
            return yp.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"读 flow.yaml 失败 {yp}: {e}")
            return None

    # ---------- 写 ----------
    def save(self, name: str, yaml_text: str) -> None:
        """保存（创建或覆盖）一个 flow 定义。

        自动建目录 + runs/ 目录。原子写（tmp + os.replace）。
        name 在此统一强制 parser 同款字符集校验（_FLOW_NAME_RE，对齐
        parser._NAME_RE）：仅字母/数字/下划线/短横，1-64 字符，且禁止
        _ 开头（保留给辅助目录如 _approvals）。此前只防 _ 前缀与路径
        穿越字符，引号/尖括号/& 等可入库——blocks 模式绕过 parser 的
        name 校验，前端卡片按钮 onclick 拼接因此可达注入。
        """
        if not name or name.startswith("_"):
            raise ValueError(f"非法 flow name: {name!r}（不能为空或下划线开头）")
        if not _FLOW_NAME_RE.match(name):
            # 覆盖旧有的 / \\ . .. 穿越/保留字符检查（字符集外全部拒绝）
            raise ValueError(
                f"非法 flow name: {name!r}"
                f"（须匹配 {_FLOW_NAME_RE.pattern}：仅字母/数字/下划线/短横，1-64 字符）"
            )

        fdir = self.flow_dir(name)
        fdir.mkdir(parents=True, exist_ok=True)
        self.run_dir(name).mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self._yaml_path(name), yaml_text)
        logger.info(f"flow 已保存: user={self.user_id} name={name}")

    def delete(self, name: str) -> bool:
        """删除整个 flow 目录（含 runs/）。不存在返回 False。"""
        if not name or name.startswith("_"):
            raise ValueError(f"非法 flow name: {name!r}")
        fdir = self.flow_dir(name)
        if not fdir.exists():
            return False
        import shutil
        shutil.rmtree(fdir)
        logger.info(f"flow 已删除: user={self.user_id} name={name}")
        return True


# ============================================
# 模块级：发现全部 flow（供主进程调度/索引）
# ============================================
def iter_all_flows(workspace_root: str = ""):
    """遍历根下全部 wakerflow 定义（单用户）。

    Yields:
        tuple[LOCAL_USER, name, yaml_text]（user 恒为 LOCAL_USER，保元组契约兼容）
        路径约定：<root>/wakerflows/<name>/flow.yaml

    与 src.waker.store.iter_all_wakers 同构（但不解析 yaml——解析在调用方）。
    """
    ws = _resolve_workspace(workspace_root)
    flows_root = ws / "wakerflows"
    if not flows_root.exists():
        return
    for flow_entry in sorted(flows_root.iterdir()):
        if not flow_entry.is_dir():
            continue
        if flow_entry.name.startswith("_"):
            continue  # 跳过 _approvals 等辅助目录
        yp = flow_entry / "flow.yaml"
        if not yp.exists():
            continue
        try:
            text = yp.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"读 flow.yaml 失败 {yp}: {e}")
            continue
        yield LOCAL_USER, flow_entry.name, text
