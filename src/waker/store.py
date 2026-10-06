"""
============================================
WakerStore —— 数字员工文件存储（单用户，T2b-② 拍平）
============================================
每个 waker = <root>/wakers/<name>/ 下的一组文件：

    IDENTITY.md       核心职责
    PERSONA.md        工作风格
    BIBLE.md          工作准则
    waker.yaml        配置 + 状态（config: / state: 两节）
    runs/<run_id>.jsonl   每次运行的事件日志（列表/详情按文件扫摘要）
    latest_result.md      最近一次运行的结果摘要

root 解析：显式 workspace_root 参数（语义为 agent home 根，测试指向 tmp 用）
or paths.agent_home()（= data/home）。旧 users/<uid>/ 层级已废弃；
user_id 参数保留（兼容调用面）但路径不再使用。

写文件一律原子：tmp + os.replace（同 file_store._write_all 的风格）。

## 与调度器/runner 的分工
- 主进程调度器：用 iter_all_wakers(workspace_root) 发现全部 waker
- per-user worker：用 WakerStore(uid, workspace_root) 增删改查、读人格、写运行状态
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path

import yaml

from src.constants import LOCAL_USER
from src.storage import paths
from src.waker.models import PERSONA_FILES, WakerConfig, WakerConfigError, validate_name
from src.waker.schedule_parse import validate_schedule

logger = logging.getLogger("hermes.waker.store")


# persona md 的文件名（取自单一来源 models.PERSONA_FILES）
_PERSONA_FILES = tuple(fname for fname, _ in PERSONA_FILES)


def _resolve_workspace(workspace_root: str = "") -> Path:
    """解析根目录（语义为 agent home 根）：显式参数 or paths.agent_home()。"""
    if workspace_root:
        return Path(workspace_root)
    return paths.agent_home()


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写文本（tmp + os.replace）。父目录自动创建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_yaml(path: Path, data: dict) -> None:
    """原子写 yaml。"""
    text = yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    _atomic_write_text(path, text)


def _load_yaml(path: Path) -> dict:
    """读 yaml，文件不存在/解析失败返回 {}。"""
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        logger.warning(f"waker.yaml 解析失败 {path}: {e}")
        return {}


class WakerStore:
    """waker 文件存储（单用户，agent home 下 wakers/）。"""

    def __init__(self, user_id: str, workspace_root: str = ""):
        """
        Args:
            user_id: 已废弃（单用户，保参兼容调用面）
            workspace_root: agent home 根。空则 paths.agent_home()
        """
        self.user_id = user_id
        self._ws = _resolve_workspace(workspace_root)
        self._wakers_root = self._ws / "wakers"

    # ---------- 路径辅助 ----------
    @property
    def wakers_root(self) -> Path:
        return self._wakers_root

    @staticmethod
    def _safe_component(value: str, what: str = "waker name") -> str:
        """路径组件防穿越校验（getter 纵深防御：URL 参数直达 store 的入口）。"""
        value = value or ""
        if (not value or value.startswith("_") or "/" in value or "\\" in value
                or value in (".", "..") or ":" in value):
            raise ValueError(f"非法 {what}: {value!r}")
        return value

    def waker_dir(self, name: str) -> Path:
        return self._wakers_root / self._safe_component(name)

    def run_dir(self, name: str) -> Path:
        return self.waker_dir(name) / "runs"

    def run_jsonl_path(self, name: str, run_id: str) -> Path:
        """某次 run 的 jsonl 路径。run_id 过 _safe_component，防路径穿越。"""
        return self.run_dir(name) / f"{self._safe_component(run_id, 'run_id')}.jsonl"

    def latest_result_path(self, name: str) -> Path:
        return self.waker_dir(name) / "latest_result.md"

    def _yaml_path(self, name: str) -> Path:
        return self.waker_dir(name) / "waker.yaml"

    def new_run_id(self) -> str:
        """生成 run_id：时间戳 + 短随机。"""
        return f"{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"

    # ---------- 读 ----------
    def list(self) -> list[WakerConfig]:
        """列出该用户所有 waker（已 enabled/未 enabled 都含）。"""
        out: list[WakerConfig] = []
        if not self._wakers_root.exists():
            return out
        for entry in sorted(self._wakers_root.iterdir()):
            if not entry.is_dir():
                continue
            yp = entry / "waker.yaml"
            if not yp.exists():
                continue
            try:
                d = _load_yaml(yp)
                out.append(WakerConfig.from_yaml_dict(d))
            except Exception as e:
                logger.warning(f"加载 waker 失败 {yp}: {e}")
                continue
        return out

    def get(self, name: str) -> WakerConfig | None:
        """按 name 取单个 waker。不存在返回 None。"""
        yp = self._yaml_path(name)
        if not yp.exists():
            return None
        d = _load_yaml(yp)
        if not d:
            return None
        return WakerConfig.from_yaml_dict(d)

    # ---------- 写 ----------
    def create(
        self,
        cfg: WakerConfig,
        identity: str = "",
        persona: str = "",
        bible: str = "",
    ) -> WakerConfig:
        """创建一个新 waker（生成目录 + 三个 md + waker.yaml）。

        name 冲突报 ValueError。cfg 会做全字段校验 + 调度字段校验
        （P1-7：validate_schedule 接线——带时区的 expire_at 等在创建即拒绝，
        不再流入调度器后让其后的任务停摆）。
        若 api_token 为空，自动生成一个。
        """
        cfg.validate()
        validate_schedule(cfg)
        validate_name(cfg.name)
        wdir = self.waker_dir(cfg.name)
        if wdir.exists():
            raise ValueError(f"waker 已存在: {cfg.name}")
        if not cfg.api_token:
            cfg.new_api_token()

        wdir.mkdir(parents=True, exist_ok=False)
        try:
            self.run_dir(cfg.name).mkdir(parents=True, exist_ok=True)

            # 三个 md（即使空也写入占位，persona.py 会跳过空内容）
            # md 顺序与文件名由 PERSONA_FILES 决定；本函数按位置接受 identity/persona/bible
            for fname, content in zip(_PERSONA_FILES, (identity, persona, bible)):
                _atomic_write_text(
                    wdir / fname,
                    content.strip() + ("\n" if content.strip() else ""),
                )

            # waker.yaml
            _atomic_write_yaml(self._yaml_path(cfg.name), cfg.to_yaml_dict())
        except Exception:
            # 写 md / yaml 失败：回滚已建的目录，避免半成品残留导致重试报"已存在"
            import shutil
            shutil.rmtree(wdir, ignore_errors=True)
            raise
        logger.info(f"waker 已创建: user={self.user_id} name={cfg.name}")
        return cfg

    def update(self, cfg: WakerConfig) -> WakerConfig:
        """更新配置（只写 waker.yaml，不动 md）。

        写入口全字段校验 + 调度校验（P2-25 / P1-7：非法调度值在此拒绝，
        而不是落盘后 compute_next_run 静默返回 None 永不调度）。
        P3-11：写回保留盘上 state 节——调度器经 save_state 推进的
        run_count/next_run_at 不能被"读改写窗口里的旧 cfg"整体覆盖回退。
        """
        validate_name(cfg.name)
        yp = self._yaml_path(cfg.name)
        if not yp.exists():
            raise ValueError(f"waker 不存在: {cfg.name}")
        cfg.validate()
        validate_schedule(cfg)
        d = _load_yaml(yp)
        new = cfg.to_yaml_dict()
        if isinstance(d.get("state"), dict) and d["state"]:
            new["state"] = d["state"]
        _atomic_write_yaml(yp, new)
        return cfg

    def update_persona(
        self,
        name: str,
        identity: str | None = None,
        persona: str | None = None,
        bible: str | None = None,
    ) -> None:
        """更新人格 md（None 的不改动）。"""
        validate_name(name)
        wdir = self.waker_dir(name)
        if not wdir.exists():
            raise ValueError(f"waker 不存在: {name}")
        for fname, content in zip(_PERSONA_FILES, (identity, persona, bible)):
            if content is None:
                continue
            _atomic_write_text(wdir / fname, content.strip() + ("\n" if content.strip() else ""))

    def save_state(self, cfg: WakerConfig) -> None:
        """只更新 state 节，保留 config 节。

        读出现有 yaml → 替换 state 节 → 原子写回。
        """
        validate_name(cfg.name)
        yp = self._yaml_path(cfg.name)
        d = _load_yaml(yp)
        if not d:
            # 文件不存在：用 cfg 当前值兜底建一份
            d = cfg.to_yaml_dict()
        else:
            d.setdefault("config", {})
            d["state"] = {
                "run_count": cfg.run_count,
                "last_run_at": cfg.last_run_at,
                "last_status": cfg.last_status,
                "next_run_at": cfg.next_run_at,
            }
        _atomic_write_yaml(yp, d)

    def set_enabled(self, name: str, enabled: bool) -> None:
        """启用/禁用 waker（改 config.enabled）。"""
        cfg = self.get(name)
        if cfg is None:
            raise ValueError(f"waker 不存在: {name}")
        cfg.enabled = enabled
        self.update(cfg)

    def delete(self, name: str) -> bool:
        """删除整个 waker 目录。不存在返回 False。"""
        validate_name(name)
        wdir = self.waker_dir(name)
        if not wdir.exists():
            return False
        import shutil
        shutil.rmtree(wdir)
        logger.info(f"waker 已删除: user={self.user_id} name={name}")
        return True


# ============================================
# 模块级：发现全部 waker（供主进程调度器）
# ============================================
def iter_all_wakers(workspace_root: str = ""):
    """遍历根下全部 waker（单用户）。

    Yields:
        tuple[LOCAL_USER, WakerConfig]（user 恒为 LOCAL_USER，保元组契约兼容）
        路径约定：<root>/wakers/<name>/waker.yaml
    """
    ws = _resolve_workspace(workspace_root)
    wakers_root = ws / "wakers"
    if not wakers_root.exists():
        return
    for waker_entry in sorted(wakers_root.iterdir()):
        if not waker_entry.is_dir():
            continue
        yp = waker_entry / "waker.yaml"
        if not yp.exists():
            continue
        try:
            d = _load_yaml(yp)
            cfg = WakerConfig.from_yaml_dict(d)
        except Exception as e:
            logger.warning(f"加载 waker 失败 {yp}: {e}")
            continue
        yield LOCAL_USER, cfg
