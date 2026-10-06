"""
============================================
ShellExecutor —— 通用 shell 命令执行器
============================================
服务 bash 工具（文件工具 2026-09 退役后是唯一文件通道）。

YAML 的 runtime 段配置字段映射：
    type: shell
    commandField: command      # 哪个参数是命令字符串
    timeoutField: timeout      # 哪个参数是超时秒数
    cwd: workDir               # 工作目录（固定值 "workDir" → resolve_workspace_root）

shell_enabled 门控由 ToolSpec.config_guard 声明，Registry 在 bind_tools 前过滤。
HITL 审批（非白名单命令）由 permissions Layer 3 的 classify_command evaluator 处理。
  本 executor 被调用时，权限已全通过，直接 subprocess.run。

"""

from __future__ import annotations

import locale
import logging
import os
import signal
import subprocess
import sys
import threading
from typing import TYPE_CHECKING, Any

from src.tools.executor_base import ToolExecutor, resolve_workspace_root
from src.types import ToolResult

if TYPE_CHECKING:
    from src.tools.context import ToolContext

logger = logging.getLogger("hermes.tools.executors.shell")


def _decode_output(data: bytes) -> str:
    """解码子进程输出 bytes → str。

    中文 Windows 的 cmd.exe 错误信息是 GBK（cp936），但 Python/现代工具
    可能输出 UTF-8。按优先级尝试：UTF-8（现代工具）→ GBK/cp936（Windows
    系统命令）→ locale 默认 → replace 兜底。
    """
    if not data:
        return ""
    # 1. UTF-8（现代 Python 工具、跨平台标准）
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # 2. GBK / cp936（中文 Windows cmd.exe 错误信息）
    try:
        return data.decode("gbk")
    except (UnicodeDecodeError, LookupError):
        pass
    # 3. locale 默认编码，兜底
    enc = locale.getencoding()
    return data.decode(enc, errors="replace")


# Windows shell 探测结果缓存（进程级，首次 execute() 时探测一次）
_win_shell_cache: tuple[str, list[str]] | None | bool = False
# False = 尚未探测；None = 探测过但没找到；tuple = 探测结果


def _find_git_bash() -> str | None:
    """在 Windows 上探测 Git Bash 的 bash.exe 路径。

    不只靠 PATH（用户可能从 CMD/PowerShell 启动 Web，PATH 里没有 Git）。
    依次探测：
      1. shutil.which("bash") —— PATH 里有就直接用
      2. 常见安装路径 —— Program Files / 用户目录
      3. git.exe 所在目录推导 —— git 安装目录的 ../bin/bash.exe
    """
    import shutil as _shutil

    # 1. PATH 中的 bash
    bash = _shutil.which("bash")
    if bash and os.path.isfile(bash):
        return bash

    # 2. 常见 Git Bash 安装路径
    candidate_dirs = [
        r"C:\Program Files\Git\bin",
        r"C:\Program Files\Git\usr\bin",
        r"C:\Program Files (x86)\Git\bin",
        r"C:\Program Files (x86)\Git\usr\bin",
        os.path.expanduser(r"~\AppData\Local\Programs\Git\bin"),
        os.path.expanduser(r"~\AppData\Local\Programs\Git\usr\bin"),
        os.path.expanduser(r"~\scoop\apps\git\current\bin"),
    ]
    for d in candidate_dirs:
        p = os.path.join(d, "bash.exe")
        if os.path.isfile(p):
            return p

    # 3. 从 git.exe 反推（git 在 PATH 但 bash 不在）
    git_exe = _shutil.which("git")
    if git_exe:
        git_dir = os.path.dirname(git_exe)
        # git.exe 通常在 <GitRoot>/cmd 或 <GitRoot>/bin
        # bash.exe 在 <GitRoot>/bin/bash.exe
        for parent_depth in range(2):
            base = git_dir
            for _ in range(parent_depth):
                base = os.path.dirname(base)
            for subdir in ("bin", "usr/bin"):
                p = os.path.join(base, subdir, "bash.exe")
                if os.path.isfile(p):
                    return p

    return None


def _resolve_shell_program() -> tuple[str, list[str]] | None:
    """解析 shell 程序路径 + 固定参数（进程级缓存）。

    Windows 上 subprocess.Popen(shell=True) 默认用 cmd.exe（COMSPEC），
    而 cmd.exe 不支持 mkdir -p / ls / cat / grep 等 POSIX 命令——LLM 生成的
    命令几乎都是 POSIX 语法。这里在 Windows 上自动探测 Git Bash。

    优先级：
      1. config.yaml 的 shell_program（如果设了）
      2. PATH 中的 bash（Git Bash / WSL bash）
      3. 常见安装路径硬探测
      4. None → 回退 shell=True（cmd.exe，POSIX 命令会失败）

    Returns:
        (executable_path, prefix_args) 如 ("C:/.../bash.exe", ["-c"])
        或 None（回退 shell=True）
    """
    global _win_shell_cache

    if sys.platform != "win32":
        return None

    # 已探测过 → 直接返回缓存
    if _win_shell_cache is not False:
        return _win_shell_cache if _win_shell_cache is not None else None

    # 首次探测
    from config import get_settings

    # 1. 显式配置
    configured = getattr(get_settings(), "shell_program", "")
    if configured and os.path.isfile(configured):
        logger.info(f"shell 使用配置的 shell_program: {configured}")
        _win_shell_cache = (configured, ["-c"])
        return _win_shell_cache

    # 2+3. 自动探测 Git Bash
    bash = _find_git_bash()
    if bash:
        logger.info(f"shell 自动探测到 Git Bash: {bash}")
        _win_shell_cache = (bash, ["-c"])
        return _win_shell_cache

    # 4. 没找到 → 回退 cmd.exe（POSIX 命令会失败）
    logger.error(
        "⚠️ Windows 上未找到 Git Bash！shell 命令将回退到 cmd.exe，"
        "POSIX 命令（mkdir -p / ls / cat / grep）将无法执行。"
        "解决方法：在 config.yaml 设置 shell_program: <bash.exe 绝对路径>，"
        "或确保启动 Web 的环境 PATH 含 Git Bash。"
    )
    _win_shell_cache = None
    return None


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """跨平台杀进程树。

    subprocess.run(shell=True, timeout=...) 在 Windows 上只杀 shell（cmd.exe），
    子进程成为孤儿继续持有 stdout/stderr 管道 → communicate() 阻塞直到孤儿
    自然退出（可能很久）。这里用 Popen + 手动 kill 整棵树来彻底解决。
    """
    if sys.platform == "win32":
        # Windows：用 taskkill /T /F 杀整个进程树（/T=含子进程，/F=强杀）
        # 比 CREATE_NEW_PROCESS_GROUP + TerminateProcess 更可靠地处理多层子进程
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
        except Exception:
            # taskkill 失败时尝试直接 kill 主进程
            try:
                proc.kill()
            except Exception:
                pass
    else:
        # POSIX：杀整个进程组（start_new_session=True 创建的 session）
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


class ShellExecutor(ToolExecutor):
    """通用 shell 执行器。

    执行器实例是配置对象（YAML 加载时构造一次）。
    command_field / timeout_field 来自 YAML 的 runtime 段。
    """

    def __init__(
        self,
        command_field: str = "command",
        timeout_field: str | None = "timeout",
        cwd: str = "workDir",
    ):
        self.command_field = command_field
        self.timeout_field = timeout_field
        self.cwd = cwd
        # P2-4：registry 串行外层超时的 cancel 钩子登记表——execute 期间
        # 登记运行中的 Popen，cancel() 杀整棵进程树（复用 _kill_process_tree），
        # 工作线程下的 bash 不再被遗弃成孤儿。
        # P2-4b：执行器实例是共享配置对象且经 loader 进程级缓存——并发批量
        # / 多个 task 子代理可能同时用同一实例跑 bash。登记表按 execute 调用
        # 粒度（key=发起调用的 ToolContext 的 id，value=该 ctx 名下运行中的
        # Popen 集合）登记，cancel(key) 只杀该次调用方 ctx 的进程，不再全杀
        # 牵连兄弟执行（同一 ctx 的串行残留僵尸进程同属一个 key，仍会被
        # 覆盖清理，杀树逻辑不变）。登记表加锁。
        self._proc_lock = threading.Lock()
        self._active_procs: "dict[int, set[subprocess.Popen]]" = {}

    def cancel(self, call_token: int | None = None) -> None:
        """外部超时钩子（registry._execute_with_timeout 超时路径调用）：
        终止运行中的命令进程树。无运行中进程时为无害 no-op。

        call_token（= registry 传入的本次调用 ToolContext id）给定时只终止
        该次调用的进程（共享实例上其他会话/子代理正在跑的命令不受牵连）；
        None = 兼容旧语义，终止全部登记的进程。
        """
        with self._proc_lock:
            if call_token is None:
                procs = [
                    proc for proc_set in self._active_procs.values()
                    for proc in proc_set
                ]
            else:
                procs = list(self._active_procs.get(call_token, ()))
        for proc in procs:
            try:
                if proc.poll() is None:
                    _kill_process_tree(proc)
            except Exception:
                logger.warning(
                    f"cancel 杀进程树失败: pid={getattr(proc, 'pid', '?')}",
                    exc_info=True,
                )

    def execute(self, args: dict[str, Any], ctx: "ToolContext") -> ToolResult:
        command = args.get(self.command_field, "")
        if not command or not command.strip():
            return ToolResult(content="错误：命令为空")

        # 工作目录锁定（挂载目录优先，未挂载回退 agent home）。
        # 注意：仅锚定 cwd，无 OS 级沙箱，cd 可逃逸——破坏性命令由
        # 权限层（白名单+HITL 审批）兜底。
        workspace = resolve_workspace_root()
        if workspace is None:
            return ToolResult(
                content="错误：workspace_root 未配置，shell 执行器拒绝在无锚点时运行（防误伤宿主机）。"
            )
        if not workspace.is_dir():
            return ToolResult(content=f"错误：workspace_root 指向的目录不存在：{workspace}")

        # 超时
        timeout = self._resolve_timeout(args)

        # 输出截断限制
        from config import get_settings
        max_output = int(getattr(get_settings(), "shell_max_output_chars", 8000))

        # 用 Popen + 手动 kill 进程树取代 subprocess.run(timeout=...)。
        # subprocess.run 在 Windows shell=True 下超时只杀 cmd.exe，子进程成孤儿
        # 继续持管道 → communicate() 永久阻塞。Popen 让我们手动杀整棵树。
        #
        # Shell 程序选择（关键）：
        # Windows 上 shell=True 默认用 cmd.exe，不支持 mkdir -p / ls / cat 等
        # POSIX 命令。_resolve_shell_program() 在 Windows 上探测 Git Bash，
        # 用 [bash, '-c', command] + executable=bash 取代 shell=True。
        # POSIX 系统原生 shell 即可，回退 shell=True。
        #
        # 编码：捕获 raw bytes，用 _decode_output 先 UTF-8 再 GBK 回退解码。
        # 不用 text=True（中文 Windows 默认 GBK，会把 UTF-8 输出变乱码）。
        shell_info = _resolve_shell_program()
        popen_kwargs: dict[str, Any] = dict(
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,   # 不让 bash 继承 worker 的 stdin PIPE
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if shell_info is not None:
            # Windows + Git Bash：显式 executable + args
            shell_exe, shell_prefix = shell_info
            popen_args = [shell_exe] + shell_prefix + [command]
            popen_kwargs["executable"] = shell_exe
            shell_display = f"bash({shell_exe})"
        else:
            # POSIX：shell=True + start_new_session（让 os.killpg 能杀整组）
            popen_kwargs["shell"] = True
            popen_args = command
            if sys.platform != "win32":
                popen_kwargs["start_new_session"] = True
            shell_display = "shell=True(cmd.exe/POSIX)"

        logger.info(f"run_shell 执行 [{shell_display}]: {command[:120]}")

        try:
            proc = subprocess.Popen(popen_args, **popen_kwargs)
        except Exception as e:
            logger.error(f"run_shell Popen 失败: {e}", exc_info=True)
            return ToolResult(content=f"错误：命令启动失败 - {e}")

        # P2-4b：本次调用的取消粒度 key（发起调用的 ToolContext 身份——
        # 同一 agent 流/子代理共享一个 ToolContext，跨会话互不相同）
        call_token = id(ctx)
        try:
            # P2-4：登记进 cancel 钩子登记表（registry 外层超时可杀树）；
            # timeout<=0 = 不限（bash.yaml 声明 minimum: 0 且描述"0 表示
            # 不限"——coerce_args 的 clamp 会把越界值钳回 [0, 600]，哨兵 0
            # 原样穿透，这里显式映射为 communicate 不限时）
            with self._proc_lock:
                self._active_procs.setdefault(call_token, set()).add(proc)
            stdout_b, stderr_b = proc.communicate(
                timeout=timeout if timeout > 0 else None
            )
        except subprocess.TimeoutExpired:
            # 超时 → 杀整棵进程树（含子进程），再 communicate 放干管道
            logger.warning(f"run_shell 超时（{timeout}s）：{command[:120]}")
            # 诊断：超时前 dump 进程状态（pid / poll / 是否还在跑）
            logger.warning(
                f"run_shell 超时诊断: pid={proc.pid}, poll={proc.poll()}, "
                f"shell={shell_display}, cwd={workspace}"
            )
            _kill_process_tree(proc)
            try:
                stdout_b, stderr_b = proc.communicate(timeout=5)
                # 诊断：杀掉后看 bash 有没有输出
                if stdout_b:
                    logger.warning(
                        f"run_shell 超时后 stdout ({len(stdout_b)} bytes): "
                        f"{_decode_output(stdout_b)[:200]!r}"
                    )
                if stderr_b:
                    logger.warning(
                        f"run_shell 超时后 stderr ({len(stderr_b)} bytes): "
                        f"{_decode_output(stderr_b)[:200]!r}"
                    )
            except Exception:
                pass
            return ToolResult(
                content=(
                    f"错误：命令执行超过 {timeout} 秒已被终止（进程树已杀掉）。\n"
                    "提示：宿主机是 Windows + Git Bash，`/` 是 Git Bash 的 MSYS 虚拟根"
                    "（指向 Git 安装目录），不是文件系统根——不要从 `/` 或盘符根全盘 find/ls。"
                    "当前目录已是工作区根，请在其内用相对路径查找；"
                    "工作区外的路径用盘符形式（如 D:/some/dir）。"
                    "重试前先缩小查找范围，而不是加大 timeout。"
                )
            )
        finally:
            # P2-4：执行结束（正常完成 / 内层超时杀树 / cancel 杀树）一律注销，
            # 后续 cancel() 对已结束进程不再重复操作。仅在自己仍登记在册时
            # 移除（同 ctx 的后续调用可能已复用该 key）
            with self._proc_lock:
                proc_set = self._active_procs.get(call_token)
                if proc_set is not None:
                    proc_set.discard(proc)
                    if not proc_set:
                        self._active_procs.pop(call_token, None)

        stdout = _decode_output(stdout_b)
        stderr = _decode_output(stderr_b)
        returncode = proc.returncode

        def _truncate(text: str, label: str) -> str:
            if len(text) > max_output:
                return text[:max_output] + f"\n[...{label} 已截断，原始长度 {len(text)} 字符...]"
            return text

        parts = [f"退出码 {returncode}"]
        if stdout:
            parts.append("--- stdout ---")
            parts.append(_truncate(stdout, "stdout"))
        if stderr:
            parts.append("--- stderr ---")
            parts.append(_truncate(stderr, "stderr"))
        return ToolResult(content="\n".join(parts))

    def _resolve_timeout(self, args: dict[str, Any]) -> int:
        """从参数读取超时，防御性强转。"""
        from config import get_settings
        default_timeout = int(getattr(get_settings(), "shell_timeout", 30))
        if self.timeout_field is None:
            return default_timeout
        timeout = args.get(self.timeout_field)
        if timeout is None:
            return default_timeout
        try:
            return int(timeout)
        except (TypeError, ValueError):
            return default_timeout
