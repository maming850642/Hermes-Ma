"""
============================================
子进程 IPC 公共设施（T8b）
============================================
收敛两类子进程入口（web_fastapi/worker_process.py 与 src/wakerflow/worker_node.py）
重复的 stdio 样板：

- configure_subprocess_stdio()：Windows UTF-8 重配 + SSL_CERT_FILE 脏值清洗
- StdoutWriter：stdout NDJSON 写设施（守护写线程 + 无界队列 / 锁守同步直写）
- read_stdin_lines()：常驻 stdin 读取守护线程（逐行 NDJSON 解码 → 队列）

注意：这里只收敛"管道读写"这一层；两套消息信封保留各自定义——
worker_process 的 id 关联请求/响应信封在 web_fastapi/ipc.py，
worker_node 的 id-less 流式信封直接内联在各 emit 调用点。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from typing import Any, Callable, IO

logger = logging.getLogger("hermes.ipc")


# ============================================
# 子进程 stdio 环境初始化
# ============================================
def configure_subprocess_stdio() -> None:
    """子进程 stdio 统一初始化（幂等，模块头部调用一次）。

    1. Windows 下强制 stdin/stdout/stderr 用 UTF-8——否则 GBK 编码无法输出
       emoji（LLM 返回的 😊 等字符会让 sys.stdout.write 崩溃）。无
       reconfigure 方法的流（替换过的替身对象）跳过，不报错。
    2. 清除无效 SSL_CERT_FILE——httpx 创建 SSL context 时若它指向不存在
       的文件会 FileNotFoundError（与 main.py 一致）。
    """
    if sys.platform == "win32":
        for name in ("stdin", "stdout", "stderr"):
            stream = getattr(sys, name, None)
            if stream is None or not hasattr(stream, "reconfigure"):
                continue
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                # reconfigure 存在但失败（如已 detach 的流）：跳过，不阻断启动
                pass
    if os.environ.get("SSL_CERT_FILE") and not os.path.exists(os.environ["SSL_CERT_FILE"]):
        del os.environ["SSL_CERT_FILE"]


# ============================================
# NDJSON 行编解码（与 web_fastapi/ipc.py 语义一致；src 层不反向依赖 web 层）
# ============================================
def encode_line(msg: dict, *, default: Callable[[Any], Any] | None = None) -> str:
    """编码为 NDJSON 行（单行 JSON + \\n，ensure_ascii=False 保中文原文）。"""
    return json.dumps(msg, ensure_ascii=False, default=default) + "\n"


def decode_line(line: str) -> dict | None:
    """从 NDJSON 行解码。空行/无效 JSON 返回 None。"""
    line = line.strip()
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


# ============================================
# StdoutWriter：stdout 写设施
# ============================================
class StdoutWriter:
    """stdout NDJSON 写设施（线程安全）。

    两种写模式：
    - send(msg)：编码后 put 到无界队列立即返回，**永不阻塞调用方**——
      管道写满/关闭的阻塞风险由常驻 daemon 写线程承担。用于运行期事件。

      背景：父进程断开（切会话/关标签页）后子进程无感知，继续往 stdout
      写事件，管道 buffer（~64KB）写满后 sys.stdout.write 永久阻塞。
      走队列后阻塞的只是后台写线程，主循环继续读命令继续工作。

    - send_sync(msg)：锁守直写（write + flush）。用于启动期同步握手信号
      （ready / init error）——它们是父子进程的同步点，必须立即送达，
      走异步队列时 daemon 写线程可能被 OS 延迟调度，实测会让父进程等满
      一个轮询周期甚至超时。也用于无需后台线程的锁守流式输出（worker_node
      的 _emit 语义：每条直写、线程间互斥）。

    管道写失败一次后置 pipe_dead，后续队列事件直接丢弃（父进程已断开）。
    send_sync 的异常照原样抛出（调用方决定吞或冒）。

    Args:
        stream: 输出流。None 时每次写取当前 sys.stdout（模块级替换后仍指向新流）。
        default: json.dumps 的 default 参数（不可序列化对象的兜底转换器，
            如 worker_node 的 str）。None 即 json 默认行为（不可序列化抛 TypeError）。
        thread_name: 守护写线程名。
    """

    def __init__(
        self,
        stream: IO[str] | None = None,
        *,
        default: Callable[[Any], Any] | None = None,
        thread_name: str = "stdout-writer",
    ):
        self._stream = stream
        self._default = default
        self._thread_name = thread_name
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._lock = threading.Lock()
        self._pipe_dead = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- 异步队列模式 ----------

    def start(self) -> None:
        """启动常驻写线程（幂等；main() 里调一次）。"""
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._writer_loop, daemon=True, name=self._thread_name,
            )
            self._thread.start()

    def send(self, msg: dict) -> None:
        """编码后入队，立即返回（永不阻塞调用方）。"""
        self._queue.put(encode_line(msg, default=self._default))

    def _writer_loop(self) -> None:
        """常驻写线程：从队列取消息写 stdout。管道满/关闭时不阻塞调用方。"""
        while True:
            msg = self._queue.get()
            if msg is None:  # 关闭信号
                return
            if self._pipe_dead.is_set():
                continue  # 管道已死，丢弃后续事件
            try:
                with self._lock:
                    stream = self._write_stream()
                    stream.write(msg)
                    stream.flush()
            except Exception:
                self._pipe_dead.set()
                logger.error("子进程 stdout 写入失败，后续事件将被丢弃", exc_info=True)

    # ---------- 同步直写模式 ----------

    def send_sync(self, msg: dict) -> None:
        """锁守直写一条 NDJSON（write + flush）。异常照原样抛出。"""
        encoded = encode_line(msg, default=self._default)
        with self._lock:
            stream = self._write_stream()
            stream.write(encoded)
            stream.flush()

    # ---------- 生命周期 ----------

    def flush(self, timeout: float = 5.0) -> bool:
        """等队列排空（写线程把已有消息写完）。超时返回 False。"""
        deadline = time.monotonic() + timeout
        while not self._queue.empty():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def close(self, timeout: float = 5.0) -> None:
        """发 None 终止信号并等写线程把残留消息写完（进程退出前调用）。"""
        self._queue.put(None)
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)

    # ---------- 内部 ----------

    def _write_stream(self) -> IO[str]:
        """解析当前输出流（stream=None 时动态取 sys.stdout）。"""
        return self._stream if self._stream is not None else sys.stdout

    @property
    def pipe_dead(self) -> bool:
        """管道是否已判定死亡（写失败过一次）。"""
        return self._pipe_dead.is_set()


# ============================================
# stdin 读取线程
# ============================================
def read_stdin_lines(
    out_queue: "queue.Queue[dict | None]",
    *,
    stream: IO[str] | None = None,
    name: str = "stdin-reader",
    on_cmd=None,
) -> threading.Thread:
    """启动常驻 stdin 读取守护线程：逐行读 NDJSON → 解码 → out_queue。

    - 无效行（空/坏 JSON/非对象）丢弃
    - stdin 关闭（EOF 或读异常）时 put None 终止信号，主循环据此退出

    独立线程的意义：chat 等长命令执行期间也能收到轻量命令（权限切换等），
    旧架构 `for line in sys.stdin` 阻塞会让并发命令拿不到响应。

    Args:
        out_queue: 解码后的命令队列（dict）+ 终止信号（None）
        stream: 输入流。None 时读当前 sys.stdin（每次 readline 动态取）。
        name: 线程名。
        on_cmd: 可选直通回调 `fn(cmd) -> bool`。返回 True 表示该命令已被
            回调完全处理、不再入队。用于 chat_stop 这类必须零延迟生效的
            命令（工具执行期 agent 零事件，排队等事件间隙处理会迟到）。
            回调内部只做线程安全操作（threading.Event.set / 经写线程写 stdout）。

    Returns:
        已启动的 daemon 线程对象。
    """
    def _body() -> None:
        while True:
            src = stream if stream is not None else sys.stdin
            try:
                line = src.readline()
            except Exception:
                break
            if not line:
                # stdin 关闭（父进程断开）
                out_queue.put(None)
                return
            cmd = decode_line(line)
            if cmd is None:
                continue
            if on_cmd is not None:
                try:
                    if on_cmd(cmd):
                        continue
                except Exception:
                    pass  # 直通回调失败 → 照常入队兜底
            out_queue.put(cmd)

    t = threading.Thread(target=_body, daemon=True, name=name)
    t.start()
    return t
