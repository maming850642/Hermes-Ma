"""
WorkspaceService —— 挂载状态机（kv 持久化）。

状态存 kv（scope="workspace"）：
    key="mount"    当前 MountState dict；None（无键或 null）= 从未配置/已卸载
    key="history"  历史条目 list（同构 + unmounted_at），保留最近 20 条

安全边界（如实声明）：
    本系统【没有 OS 级沙箱】。对 agent 的约束 = 工具层路径守卫
    （src/tools/path_guard.resolve_under_root，fs 工具路径必须落在挂载根内）
    + HITL 审批（shell 等破坏性命令需人工批准）。shell 可 cd 逃逸，
    这是已知边界，靠审批兜底而非沙箱。
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from uuid import uuid4

from src.storage import paths
from src.workspace.models import (
    MODE_LOCAL,
    MODE_NONE,
    MODE_UPLOAD,
    MountState,
    history_entry,
)

logger = logging.getLogger("hermes.workspace.service")

SCOPE = "workspace"
KEY_MOUNT = "mount"
KEY_HISTORY = "history"

# 历史上限（保留最近 N 条）
MAX_HISTORY = 20

# settings 键缺省值
# list_wakers/create_waker/set_waker_enabled/create_wakerflow 是管理面工具
# （写 data/home，不碰工作区文件），挂载与否都应可用——"chat 里聊出数字员工"
# 恰恰发生在未挂载工作区的默认会话。task 也在白名单：收件箱可派子任务
# （子代理 inherit_tools 仍走 resolve_tools，只能拿到本集合内的工具）。
DEFAULT_CHAT_ONLY_TOOLS = (
    "write_todos,compact_conversation,task,"
    "list_wakers,create_waker,set_waker_enabled,create_wakerflow,"
    "list_mcps,create_mcp,remove_mcp,web_fetch,web_search,use_skill,mcp__*"
)
DEFAULT_UPLOAD_MAX_MB = 200


class WorkspaceError(Exception):
    """挂载校验失败。消息中文、可直接展示给 UI。"""


def chat_only_tools_from_settings(settings=None) -> set[str]:
    """从 config 读仅对话模式的工具白名单（逗号分隔）。

    settings 键 workspace_chat_only_tools 缺省（或配置为空）时回落
    DEFAULT_CHAT_ONLY_TOOLS。独立进程（无 WorkspaceService）也用它做
    resolve_tools 的安全回退。
    """
    if settings is None:
        from config import get_settings
        settings = get_settings()
    raw = getattr(settings, "workspace_chat_only_tools", "") or ""
    names = {t.strip() for t in str(raw).split(",") if t.strip()}
    if names:
        return names
    return {t.strip() for t in DEFAULT_CHAT_ONLY_TOOLS.split(",") if t.strip()}


def _upload_max_bytes(settings=None) -> int:
    """上传解压总大小上限（字节）。settings 键 workspace_upload_max_mb。"""
    if settings is None:
        from config import get_settings
        settings = get_settings()
    mb = getattr(settings, "workspace_upload_max_mb", DEFAULT_UPLOAD_MAX_MB)
    try:
        mb = float(mb)
    except (TypeError, ValueError):
        mb = DEFAULT_UPLOAD_MAX_MB
    return int(max(0, mb) * 1024 * 1024)


def _windows_forbidden_roots() -> list[Path]:
    """Windows 系统目录黑名单（%WINDIR%、Program Files、盘符根等）。

    非 Windows 环境返回空列表（相关 env 不存在，全部跳过）。
    """
    if os.name != "nt":
        return []
    roots: list[Path] = []
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if windir:
        roots.append(Path(windir))
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        v = os.environ.get(env)
        if v:
            roots.append(Path(v))
    # 常见硬编码兜底（env 被清掉的极端场景）
    for drive in ("C:", "D:"):
        for sub in ("Program Files", "Program Files (x86)"):
            p = Path(drive + "\\") / sub
            if p.is_dir():
                roots.append(p)
    return roots


def _posix_forbidden_paths() -> list[Path]:
    """POSIX 系统目录黑名单（/, /usr, /etc, /bin, /var, /home 根）。"""
    if os.name == "nt":
        return []
    return [Path(p) for p in ("/", "/usr", "/etc", "/bin", "/var", "/home")]


def _user_forbidden_roots() -> list[Path]:
    """用户数据/配置目录黑名单（R3-17，P1-5 收紧）。

    %USERPROFILE%、%APPDATA%、%LOCALAPPDATA%、%ProgramData%、
    %ALLUSERSPROFILE%。挂载这些目录等于把浏览器配置/凭据/系统级应用
    数据整棵交给 agent。封禁语义见 _is_forbidden_system_dir：根本身 +
    _SENSITIVE_USER_SUBTREES 敏感子树；其余普通子目录（桌面/文档下的
    工程等）不受影响。环境变量缺失（非 Windows / 精简环境）自动跳过。
    """
    roots: list[Path] = []
    for env in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "ProgramData", "ALLUSERSPROFILE"):
        v = os.environ.get(env)
        if v:
            roots.append(Path(v))
    return roots


# 用户数据根下的敏感子树（P1-5）：免认证形态下匿名 CSRF 可直达
# mount_local，只封根本身挡不住 ~/.ssh（密钥）、Chrome User Data 等
# 凭据存放地——挂载它们等于把凭据整棵交给 agent，必须默认拒绝。
# （Windows 的 AppData 承载浏览器 Profile 与应用凭据库；.aws/.kube/
#   .gnupg/.ssh 是各平台通行的密钥/凭据目录。）
_SENSITIVE_USER_SUBTREES = (".ssh", ".aws", ".kube", ".gnupg", "AppData")


def _temp_root() -> Path | None:
    """系统临时目录（resolve 后）。不可得返回 None（不做豁免）。

    见 _is_forbidden_system_dir：豁免仅在临时目录本身位于敏感子树内时
    生效（Windows %TEMP% 布局归属 %LOCALAPPDATA%\\Temp）。
    """
    try:
        return Path(tempfile.gettempdir()).resolve()
    except OSError:
        return None


def _is_forbidden_system_dir(resolved: Path) -> bool:
    """解析后的挂载候选是否落在系统目录黑名单内（含目录本身）。

    子树语义：黑名单目录本身及其全部子路径都拒。项目根（paths.PROJECT_ROOT，
    R3-17 新增）按子树封禁——挂载仓库（或其子目录）会让 agent 直接改写
    自身源码/配置/数据库。用户数据根（%USERPROFILE% 等，P1-5 收紧）封
    根本身 + _SENSITIVE_USER_SUBTREES 敏感子树（.ssh/.aws/.kube/.gnupg/
    AppData）；其余普通子目录不受影响。
    唯一豁免：系统临时目录本身位于某敏感子树内时（Windows %TEMP% 的
    布局归属 %LOCALAPPDATA%\\Temp），临时目录之下的路径不因该布局误杀
    ——临时工程目录（pytest tmp、解压草稿）是标准作业面而非凭据存储。
    Chrome User Data 等临时目录之外的凭据地照封不误。
    豁免：data/projects/spaces/**（ADR-0005 托管项目空间）虽在仓库/data
    内，但它是用户项目的法定作业面，不承载源码、配置与数据库。
    """
    exempt_hosted = _is_hosted_space_root(resolved)
    # 子树封禁：系统目录 + 项目根
    subtree_roots = _windows_forbidden_roots() + _posix_forbidden_paths()
    if not exempt_hosted:
        subtree_roots.append(paths.PROJECT_ROOT)
    for forbidden in subtree_roots:
        try:
            f = forbidden.resolve()
        except OSError:
            continue
        if resolved == f or f in resolved.parents:
            return True
    # 用户数据根：根本身 + 敏感子树（仅系统临时目录布局豁免）
    temp_root = _temp_root()
    for forbidden in _user_forbidden_roots():
        try:
            f = forbidden.resolve()
        except OSError:
            continue
        if resolved == f:
            return True
        for name in _SENSITIVE_USER_SUBTREES:
            s = f / name
            if resolved == s or s in resolved.parents:
                if temp_root is not None and (
                    (s == temp_root or s in temp_root.parents)
                    and (resolved == temp_root or temp_root in resolved.parents)
                ):
                    continue  # 临时目录布局在敏感子树内 → 其下路径豁免
                return True
    return False


def _is_drive_root(resolved: Path) -> bool:
    """是否为盘符根（C:\\、D:\\）或 POSIX 根（/）——parent == 自身。"""
    return resolved.parent == resolved


def _is_hosted_space_root(resolved: Path) -> bool:
    """resolved 是否落在托管项目空间根 data/projects/spaces/ 之内（含本身）。

    该子树是 ADR-0005 hosted 项目的法定工作区（agent 的正常作业面），
    对 data_root 黑名单豁免；spaces 之外的一切 data 内部路径照旧封禁。
    """
    spaces_root = paths.data_dir("projects", "spaces")
    try:
        resolved.relative_to(spaces_root)
        return True
    except ValueError:
        return False


class WorkspaceService:
    """挂载状态机。provider 需实现 KVProtocol（kv_get/kv_put）。"""

    def __init__(self, provider) -> None:
        self._provider = provider

    # ────────────────────────────────────────────────
    # 读取
    # ────────────────────────────────────────────────

    def status(self) -> MountState | None:
        """当前挂载状态。None = 从未配置（或数据损坏，按安全侧处理）。"""
        raw = self._provider.kv_get(SCOPE, KEY_MOUNT)
        if not isinstance(raw, dict):
            return None
        try:
            st = MountState.from_dict(raw)
        except (TypeError, ValueError):
            logger.warning(f"workspace mount 数据损坏，按未配置处理: {raw!r}")
            return None
        if st.mode not in (MODE_NONE, MODE_LOCAL, MODE_UPLOAD):
            logger.warning(f"workspace mount 未知模式 {st.mode!r}，按未配置处理")
            return None
        return st

    def history(self) -> list[dict]:
        """历史条目（含 unmounted_at），最新在前。"""
        raw = self._provider.kv_get(SCOPE, KEY_HISTORY)
        if not isinstance(raw, list):
            return []
        entries = [e for e in raw if isinstance(e, dict)]
        # 存储按时间追加（旧→新），返回时倒序（最新在前）
        return list(reversed(entries))

    def current_root(self) -> Path | None:
        """挂载根。local/upload → Path(path)；none/未配置 → None。"""
        st = self.status()
        if st is None or not st.is_mounted() or not st.path:
            return None
        return Path(st.path)

    def chat_only_tools(self) -> set[str]:
        """仅对话模式允许的工具名集合（settings 可覆盖）。"""
        return chat_only_tools_from_settings()

    # ────────────────────────────────────────────────
    # 写入（挂载/卸载/模式选择）
    # ────────────────────────────────────────────────

    def mount_local(self, path: str, display_name: str = "") -> MountState:
        """挂载本地文件夹。校验失败抛 WorkspaceError（消息可直显 UI）。"""
        raw = (path or "").strip().strip('"').strip("'")
        if not raw:
            raise WorkspaceError("请填写要挂载的文件夹绝对路径")
        p = Path(raw)
        if not p.is_absolute():
            raise WorkspaceError(f"路径必须是绝对路径: {raw}（例如 D:\\projects\\my-work）")

        try:
            resolved = p.resolve()
        except OSError as e:
            raise WorkspaceError(f"路径无法解析: {raw}（{e}）") from e

        # R3-17：拒绝 reparse point（junction/symlink 挂载）。realpath 会把
        # 链接解开成目标路径，abspath 只做词法规范化——两者不一致说明路径
        # 上有链接（junction 会被 realpath 解开）。挂载链接等于把校验结论
        # （黑名单/盘符根判断）建立在目标上，且链接可事后改指（换根）。
        # normcase 抹掉 Windows 盘符/大小写写法差异，避免真实目录误伤。
        abspath = Path(os.path.abspath(p))
        if os.path.normcase(str(abspath)) != os.path.normcase(str(resolved)):
            raise WorkspaceError(
                f"不允许挂载链接目录（junction/symlink），请填写真实路径: {raw}"
            )

        if not resolved.exists():
            raise WorkspaceError(f"目录不存在: {resolved}")
        if not resolved.is_dir():
            raise WorkspaceError(f"不是目录（是文件）: {resolved}")
        if not os.access(resolved, os.W_OK):
            raise WorkspaceError(f"目录不可写: {resolved}（agent 需要在该目录读写文件）")

        # 拒绝 hermes data_root 子树（含 data_root 本身）——挂载它会让 agent
        # 写进会话库/记忆库等系统内部数据。
        # 豁免：data/projects/spaces/**（ADR-0005 托管项目的法定工作区）——
        # 它在 data 下纯属布局归属，内容是用户自己的项目文件；其余
        # （sessions/hermes.db/home/uploads…）仍按内部数据封禁。
        data_root = paths.data_root()
        if not _is_hosted_space_root(resolved):
            if resolved == data_root or data_root in resolved.parents:
                raise WorkspaceError(
                    f"不允许挂载 Hermes 数据目录及其子目录: {resolved}（系统内部数据）"
                )

        # 拒绝系统目录（Windows: WINDIR/Program Files/盘符根；POSIX: / /usr 等）
        if _is_drive_root(resolved):
            raise WorkspaceError(f"不允许挂载整个盘符根: {resolved}")
        if _is_forbidden_system_dir(resolved):
            raise WorkspaceError(f"不允许挂载系统目录: {resolved}")

        st = MountState(
            mode=MODE_LOCAL,
            path=str(resolved),
            display_name=display_name.strip() or resolved.name,
            mounted_at=time.time(),
        )
        self._commit(st)
        logger.info(f"挂载本地目录: {st.path} (display={st.display_name})")
        return st

    def mount_upload(self, zip_path: Path, display_name: str = "") -> MountState:
        """上传 zip → 解压成托管工作区（data/mounts/<id12>）。

        校验：仅 .zip；zip-slip 防护（成员 resolve 后必须落在目标目录内，
        违者整体拒绝并删除残留）；总解压大小上限 workspace_upload_max_mb。
        """
        zp = Path(zip_path)
        if zp.suffix.lower() != ".zip":
            raise WorkspaceError(f"仅支持 .zip 文件: {zp.name}")
        if not zp.is_file():
            raise WorkspaceError(f"zip 文件不存在: {zp}")

        max_bytes = _upload_max_bytes()
        target = paths.data_dir("mounts", uuid4().hex[:12])
        target.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(zp) as zf:
                # 预检：声明大小超限直接拒绝（不解压）
                declared = sum(info.file_size for info in zf.infolist())
                if declared > max_bytes:
                    raise WorkspaceError(
                        f"解压后总大小超过上限（{declared // 1024 // 1024}MB > "
                        f"{max_bytes // 1024 // 1024}MB）"
                    )
                extracted = 0
                for info in zf.infolist():
                    # zip-slip：成员名 resolve 后必须落在 target 内
                    member_dest = (target / info.filename).resolve()
                    if member_dest != target and target not in member_dest.parents:
                        raise WorkspaceError(
                            f"zip 内含越界路径，已整体拒绝: {info.filename}"
                        )
                    if info.is_dir():
                        member_dest.mkdir(parents=True, exist_ok=True)
                        continue
                    member_dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(member_dest, "wb") as dst:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            extracted += len(chunk)
                            if extracted > max_bytes:
                                raise WorkspaceError(
                                    "解压后总大小超过上限"
                                    f"（>{max_bytes // 1024 // 1024}MB）"
                                )
                            dst.write(chunk)
        except WorkspaceError:
            shutil.rmtree(target, ignore_errors=True)
            raise
        except zipfile.BadZipFile as e:
            shutil.rmtree(target, ignore_errors=True)
            raise WorkspaceError(f"zip 文件损坏或不是有效压缩包: {e}") from e
        except Exception as e:
            shutil.rmtree(target, ignore_errors=True)
            raise WorkspaceError(f"解压失败: {e}") from e

        st = MountState(
            mode=MODE_UPLOAD,
            path=str(target),
            display_name=display_name.strip() or zp.stem,
            mounted_at=time.time(),
        )
        self._commit(st)
        logger.info(f"挂载上传工作区: {st.path} (display={st.display_name})")
        return st

    def unmount(self) -> None:
        """卸载当前挂载/模式 → mount 置 None（下次进门禁重新选择）。"""
        st = self.status()
        if st is None:
            raise WorkspaceError("当前没有已配置的工作区")
        self._close_current(st)
        # 写 null（json.loads → None）→ status() 返回 None = 未配置
        self._provider.kv_put(SCOPE, KEY_MOUNT, None)
        logger.info(f"卸载工作区: mode={st.mode}, path={st.path}")

    def choose_chat_only(self) -> MountState:
        """引导页第三选：仅对话（零文件权限）。"""
        st = MountState(
            mode=MODE_NONE,
            path="",
            display_name="仅对话",
            mounted_at=time.time(),
        )
        self._commit(st)
        logger.info("选择仅对话模式（chat-only）")
        return st

    # ────────────────────────────────────────────────
    # 内部
    # ────────────────────────────────────────────────

    def _commit(self, st: MountState) -> None:
        """写入新状态；若此前有挂载，先在 history 里记上一条的结束。"""
        current = self.status()
        if current is not None:
            self._close_current(current)
        self._provider.kv_put(SCOPE, KEY_MOUNT, st.to_dict())

    def _close_current(self, st: MountState) -> None:
        """把一条状态收进 history（保留最近 MAX_HISTORY 条）。"""
        entries = self._provider.kv_get(SCOPE, KEY_HISTORY)
        if not isinstance(entries, list):
            entries = []
        entries.append(history_entry(st, unmounted_at=time.time()))
        if len(entries) > MAX_HISTORY:
            entries = entries[-MAX_HISTORY:]
        self._provider.kv_put(SCOPE, KEY_HISTORY, entries)
