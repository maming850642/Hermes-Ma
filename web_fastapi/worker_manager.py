"""
WorkerManager — worker 子进程管理器（M5/ADR-0005：按会话亲和的多槽位）。

槽位模型：
- 默认槽 "main"：承载非 chat 的杂项 op 与无 session_id 的请求（单实例）。
- 会话槽 slot=session_id：每个"正在/最近生成"的会话一个专属 worker 子进程，
  互不抢锁——多标签页并行对话的实现机制。
- 上限 web_max_parallel_sessions（默认 3）只挡住「正在流式生成」的会话槽；
  空闲槽在新会话需要名额时 LRU 回收（会话文件仍在，点回去冷启动水合）。
  三路都在生成才抛 SlotsFullError。

用户级状态镜像：prefs 七键与 permission_mode 缓存在 Manager 内存 +
data/web_state.json（原子写）。新槽 spawn 后立即重放（含 main 槽——
重启后权限模式/偏好不丢）；GET 类路由直接读镜像（零 IPC，chat 忙时
也可读）。

通信：stdin/stdout NDJSON（见 ipc.py）。
"""
import json
import os
import sys
import time
import subprocess
import threading
import logging
import queue as _q
from typing import Generator

from src.constants import LOCAL_USER
from src.storage import paths
from web_fastapi.ipc import (
    encode_message, decode_message,
    make_request, new_request_id,
)

logger = logging.getLogger("hermes.web.worker_manager")

# 默认槽名（杂项 op / 无会话上下文的请求）
DEFAULT_SLOT = "main"

# 单条命令默认超时（秒）——LLM 调用可能慢，给足时间
DEFAULT_TIMEOUT = 300.0

# 用户级状态镜像落盘文件（重启后权限模式/偏好保持）
_STATE_FILE = paths.data_dir("web_state.json")


class SlotsFullError(RuntimeError):
    """会话槽位已满（M5：超限立即友好提示，不排队）。"""

    def __init__(self, cap: int):
        self.cap = cap
        super().__init__(
            f"已有 {cap} 路对话正在生成。等当前回复结束再发，或先停止其中一路。"
        )


# stdout 泵的 EOF 哨兵（与"读超时返回 None"区分开）
_EOF = object()

# 泵队列上界（P2-6，docs/architecture.md:81 观察项收口）：worker 日志型
# 输出不允许拖死读取线程——失控刷屏时队列满则挤掉最旧行腾位并计数，泵线程
# 绝不阻塞；内存占用与读取延迟从此有上界。
#
# P2-26 演进为「丢最旧」（对齐 chat_bus.put_drop_oldest 语义）：P2-6 原版
# 满时丢**新行**——消费侧 send_stream 被事件循环饿住、worker 高速刷 token
# 行时，队列积压满后继续丢新行，而当前请求的 result/done 终态帧恰是新行，
# 丢了就 SSE 假死最长 DEFAULT_TIMEOUT（300s）。终态帧/新行承载请求的结局，
# 比旧日志行更有价值；旧日志行只剩观赏价值，丢它代价最小。EOF 哨兵必达性
# 不变：EOF 是该 worker 的最后一帧，入队后不再有后续 put，不会被挤掉。
STDOUT_QUEUE_MAXSIZE = 2000
#: 每累计丢弃这么多行打一条 warning 汇总（不逐行刷日志）
STDOUT_DROP_WARN_EVERY = 500


class _StdoutPump:
    """每个 worker 一个常驻读线程，把 stdout 行泵进队列（C2 重做 / P2-6 有界）。

    旧 _readline_with_timeout 每读一行起一个线程，超时后线程泄漏且仍阻塞在
    同一个缓冲流上——下次调用双线程竞争 readline，行会丢失/撕裂，超时一次后
    该 worker 的后续命令就不可靠了。常驻单读者 + queue.get(timeout) 根治：
    零线程churn、超时只是本次 get 放弃（行仍在队列里）。

    P2-6 有界化：队列满（maxsize，默认 2000 行）时挤掉最旧一行腾位并累计
    计数（每 STDOUT_DROP_WARN_EVERY 条打一条 warning 汇总），读取循环绝不
    阻塞。EOF 哨兵必达——满时挤掉最旧一行腾位（计入丢弃），否则「worker
    已退出」会被误报成响应超时。

    P2-26 丢新行 → 丢最旧（对齐 chat_bus.put_drop_oldest）：原版满时丢
    **新行**，消费侧被饿 + worker 高速刷屏时终态帧（result/done 必是新行）
    被丢，SSE 假死最长 DEFAULT_TIMEOUT。新行/终态帧比旧行更有价值，旧日志
    行价值低——满时改挤最旧（演进理由详见模块头 P2-26 注释块）。
    """

    def __init__(self, proc_stdout, maxsize: int = STDOUT_QUEUE_MAXSIZE):
        self._stdout = proc_stdout
        self._q: _q.Queue = _q.Queue(maxsize=max(1, int(maxsize)))
        # 丢弃计数（pump 线程独占写；主线程/测试只读）：dropped=累计，
        # _dropped_since_warn=距上次 warning 的新增
        self.dropped = 0
        self._dropped_since_warn = 0
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="worker-stdout-pump")
        self._thread.start()

    def _note_drop(self) -> None:
        """计一次丢弃；每凑满 STDOUT_DROP_WARN_EVERY 条打一条 warning 汇总。"""
        self.dropped += 1
        self._dropped_since_warn += 1
        if self._dropped_since_warn >= STDOUT_DROP_WARN_EVERY:
            logger.warning(
                f"worker stdout 队列满（maxsize={self._q.maxsize}），"
                f"已累计丢弃最旧 {self.dropped} 行输出（worker 刷屏/消费过慢？）")
            self._dropped_since_warn = 0

    def _put(self, item) -> None:
        """入队且绝不阻塞：满则挤掉最旧一行腾位重试（P2-26 对齐
        chat_bus.put_drop_oldest 语义），丢弃计数；EOF 同走挤最旧腾位——
        EOF 是最后一帧，入队后不再有后续 put，必达且不会被挤掉。"""
        while True:
            try:
                self._q.put_nowait(item)
                return
            except _q.Full:
                # 挤掉最旧一行腾位（消费线程并发取走时 get 会抛 Empty——
                # 下一轮 put 自会成功，循环必然终止：仅本线程在写）
                try:
                    self._q.get_nowait()
                    self._note_drop()
                except _q.Empty:
                    pass

    def _run(self) -> None:
        try:
            while True:
                line = self._stdout.readline()
                if line == "":          # EOF（worker 退出/管道关闭）
                    self._put(_EOF)
                    return
                self._put(line)
        except Exception:
            self._put(_EOF)

    def get(self, timeout: float):
        """读一行；超时返回 None，EOF 返回 _EOF 哨兵。"""
        try:
            return self._q.get(timeout=timeout)
        except _q.Empty:
            return None


class WorkerProcess:
    """单个 worker 子进程的封装。"""

    def __init__(self, user_id: str, proc: subprocess.Popen,
                 slot: str = DEFAULT_SLOT):
        self.user_id = user_id
        self.slot = slot
        self.proc = proc
        self._lock = threading.Lock()
        self._pump = _StdoutPump(proc.stdout)
        # 流式在途标志（send_stream 持续期间为 True）：/api/chat/active
        # 探测"该会话正在后台生成"用；与 _lock 的区别是短命令不置位。
        self.streaming = False
        self.last_active = time.time()

    @property
    def key(self) -> tuple[str, str]:
        return (self.user_id, self.slot)

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def send(self, op: str, timeout: float = DEFAULT_TIMEOUT, lock_wait: float = 5.0, **kwargs) -> list[dict]:
        """发送命令，收集所有响应事件直到 done/result/error。返回事件列表。

        lock_wait: 获取 worker 锁的等待秒数。chat 流式期间 send_stream 会长时间
        持锁（最长 DEFAULT_TIMEOUT/轮）；并发的小命令（切会话/memory）若无限等锁
        会卡死前端。拿不到锁立即抛 TimeoutError，让调用方友好降级而非永久挂起。
        """
        req_id = new_request_id()
        msg = make_request(req_id, op, **kwargs)
        events = []

        # 锁获取加超时（问题2 卡死点 A）：chat 进行中时小命令不无限排队
        if not self._lock.acquire(timeout=lock_wait):
            raise TimeoutError(
                f"worker {self.user_id} 忙（chat 进行中？），op={op} 超过 {lock_wait}s 未拿到锁"
            )
        try:
            if not self.is_alive():
                raise RuntimeError(f"worker {self.user_id} 已退出")
            self.proc.stdin.write(encode_message(msg))
            self.proc.stdin.flush()

            while True:
                line = self._pump.get(timeout)
                if line is None:
                    raise TimeoutError(f"worker {self.user_id} 响应超时（{timeout}s）op={op}")
                if line is _EOF or not line:
                    raise RuntimeError(f"worker {self.user_id} stdout 关闭")
                data = decode_message(line)
                if data is None:
                    continue
                if data.get("id") != req_id:
                    continue
                mtype = data.get("type")
                if mtype == "done":
                    break
                elif mtype == "error":
                    raise RuntimeError(data.get("message", "worker 错误"))
                elif mtype == "result":
                    events.append(data)
                    break
                elif mtype == "event":
                    events.append(data)
        finally:
            self._lock.release()

        return events

    def send_stream(self, op: str, timeout: float = DEFAULT_TIMEOUT, **kwargs) -> Generator[dict, None, None]:
        """发送命令，yield 每条事件（供 SSE 流式转发）。"""
        req_id = new_request_id()
        msg = make_request(req_id, op, **kwargs)

        # C4: 锁获取加超时——chat 流式期间另一标签页并发发起会无限阻塞一个
        # 线程池线程（且客户端断连也无法取消阻塞中的 acquire）。超时改走 SSE error。
        # busy 标记：本请求从未上车，任何情况下都不得触发 request_cancel
        # （否则会把正在跑的另一条 chat 误杀——曾导致切权限模式后消息"没反应"）。
        if not self._lock.acquire(timeout=5.0):
            yield {"type": "error", "busy": True,
                   "message": "AI 正在思考（worker 忙），请稍后重试"}
            return
        self.streaming = True
        self.last_active = time.time()
        try:
            if not self.is_alive():
                raise RuntimeError(f"worker {self.user_id} 已退出")
            self.proc.stdin.write(encode_message(msg))
            self.proc.stdin.flush()

            while True:
                line = self._pump.get(timeout)
                if line is None:
                    # timeout 标记：worker 疑似卡死，上层 relay 据此发安全网取消
                    yield {"type": "error", "timeout": True,
                           "message": f"worker 响应超时（{timeout}s）"}
                    break
                if line is _EOF or not line:
                    yield {"type": "error", "message": "worker stdout 关闭"}
                    break
                data = decode_message(line)
                if data is None:
                    continue
                if data.get("id") != req_id:
                    continue
                mtype = data.get("type")
                if mtype == "done":
                    break
                elif mtype == "error":
                    yield data
                    break
                elif mtype == "result":
                    yield data
                    break
                elif mtype == "event":
                    yield data
        finally:
            self.streaming = False
            self._lock.release()

    def shutdown(self):
        """关闭 worker 进程。"""
        try:
            if self.is_alive():
                self.proc.stdin.write(encode_message(make_request("exit-req", "exit")))
                self.proc.stdin.flush()
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    self.proc.kill()
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        finally:
            try:
                self.proc.stdin.close()
                self.proc.stdout.close()
            except Exception:
                pass

    def request_cancel(self):
        """紧急发送 chat_stop，不走 _lock（推理期间锁被 send_stream 占用）。

        直接写 stdin 插队。worker 主循环是串行的——这条 cancel 命令会在
        当前 chat 完成或被中断后处理；但 worker 侧的 _cancel_event 轮询
        会在 chat 执行期间就生效，不需要等命令被读取。

        线程安全：TextIOWrapper.write+flush 在 CPython 下由 GIL 保护，
        各写一行 NDJSON 不会字节级交错；worker 按行 for line in sys.stdin
        解析，行间不混淆。
        """
        if not self.is_alive():
            return
        try:
            req_id = new_request_id()
            msg = make_request(req_id, "chat_stop")
            self.proc.stdin.write(encode_message(msg))
            self.proc.stdin.flush()
        except Exception:
            pass  # 取消是 best-effort，不能因取消失败影响主流程

    def send_fire_and_forget(self, op: str, **kwargs) -> bool:
        """发送命令但不等待响应（不走 _lock，chat 期间也能即时送达）。

        用于轻量状态变更命令（permission_mode_set）。
        worker 的 stdin 读取线程 + _drain_stream_events 内联处理机制
        保证命令在 chat 执行期间即时生效。

        Returns:
            True = 写入成功（不代表 worker 已处理），False = worker 已死/写入失败
        """
        if not self.is_alive():
            return False
        try:
            req_id = new_request_id()
            msg = make_request(req_id, op, **kwargs)
            self.proc.stdin.write(encode_message(msg))
            self.proc.stdin.flush()
            return True
        except Exception:
            logger.warning(f"send_fire_and_forget({op}) 写入失败")
            return False


class WorkerManager:
    """worker 子进程管理器（M5：键 = (LOCAL_USER, slot)，槽位上限保护）。"""

    def __init__(self, max_parallel: int | None = None, state_path=None):
        self._workers: dict[tuple[str, str], WorkerProcess] = {}
        self._lock = threading.Lock()
        # P2-12：在途 spawn 占位（key → 事件）。get_or_create 的全局锁内只做
        # 查表 + 占位去重；spawn + ready 等待在全局锁外进行，同 key 并发请求
        # 在 per-spawn 事件上等完成后 double-check 复用——lookup /
        # acquire_chat_slot / cancel_for 不再被 spawn 卡最长 60s。
        self._spawning: dict[tuple[str, str], threading.Event] = {}
        # spawn 窗口期的广播暂存（key → [(op, kwargs), ...]）：broadcast_* 只
        # 覆盖 _workers 时，"正在 spawn 的槽"会漏收本次更新（spawn 完成后读
        # 到的是 spawn 前进程镜像的旧配置）。广播时同时挂队到对应占位 key，
        # get_or_create 注册完成后统一补发。spawn 失败则丢弃（下次成功 spawn
        # 从 config.yaml / 镜像读到最新值，补发非正确性依赖）。
        self._pending_broadcasts: dict[tuple[str, str], list[tuple[str, dict]]] = {}
        # 测试注入用固定上限；None = 每次从 settings 读
        self._max_parallel = max_parallel
        # 用户级状态镜像（最近一次设置值；新槽 spawn 后重放，多实例语义一致；
        # 落盘 data/web_state.json，重启后保持；测试经 state_path 指向 tmp）
        self._state_file = state_path if state_path is not None else _STATE_FILE
        self._mirror_prefs: dict | None = None
        self._mirror_perm: str | None = None
        # 模型切换内存挂账（broadcast_llm_params 最新一次值；spawn 经
        # _replay_user_state 重放）。刻意不进 _load_state/_persist_state——
        # api_key 不得落 web_state.json（与广播路径同一纪律）。
        self._pending_llm_params: dict | None = None
        self._load_state()

    # ---- 镜像持久化（尽力而为：读写失败不阻塞主流程） ----

    def _load_state(self) -> None:
        try:
            with open(self._state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            perm = data.get("permission_mode")
            if perm in ("full_access", "before_changes", "plan"):
                self._mirror_perm = perm
            if isinstance(data.get("prefs"), dict):
                self._mirror_prefs = dict(data["prefs"])
        except (FileNotFoundError, ValueError, TypeError):
            pass  # 不存在/非 JSON → 从空开始
        except Exception:
            logger.warning("web_state.json 读取失败，镜像从空开始", exc_info=True)

    def _persist_state(self) -> None:
        try:
            data = {
                "permission_mode": self._mirror_perm,
                "prefs": self._mirror_prefs,
            }
            tmp = f"{self._state_file}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._state_file)
        except Exception:
            logger.warning("web_state.json 写入失败（镜像仍在内存生效）", exc_info=True)

    def _python_executable(self) -> str:
        return sys.executable

    def _cap(self) -> int:
        """会话槽并发上限。"""
        if self._max_parallel is not None:
            return max(1, int(self._max_parallel))
        try:
            from config import get_settings
            return max(1, int(getattr(get_settings(),
                                      "web_max_parallel_sessions", 3) or 3))
        except Exception:
            return 3

    def lookup(self, user_id: str, slot: str) -> WorkerProcess | None:
        """只读查找存活槽实例（存在且存活才返回，绝不创建）。

        管理类路由用：会话已有专属槽（chat 创建过）→ 亲和复用；
        没有 → 调用方回落 main。绝不在此 spawn 会话槽——否则每浏览一个
        会话就永久占一个并发名额（429 根因）。
        """
        with self._lock:
            wp = self._workers.get((LOCAL_USER, slot))
        return wp if (wp is not None and wp.is_alive()) else None

    def acquire_chat_slot(self, session_id: str) -> str:
        """为一次 chat 请求确定会话槽名；三路都在生成才抛 SlotsFullError。

        - 无 session_id → 默认槽（不占会话槽名额）；
        - 该会话已有存活实例 → 直接复用；
        - 新会话且存活会话槽 < 上限 → 分配新槽；
        - 满且有空闲槽（非 streaming）→ LRU 回收最久未活动的空闲槽让位；
        - 三路都在流式 → SlotsFullError。

        P2-12：在途 spawn 也计入名额，不超卖并发生成上限。
        """
        slot = (session_id or "").strip() or DEFAULT_SLOT
        if slot == DEFAULT_SLOT:
            return slot
        victim = None
        live = 0
        cap = self._cap()
        with self._lock:
            key = (LOCAL_USER, slot)
            wp = self._workers.get(key)
            if wp and wp.is_alive():
                wp.last_active = time.time()
                return slot
            live = sum(
                1 for (u, s), w in self._workers.items()
                if s != DEFAULT_SLOT and w.is_alive()
            )
            live += sum(1 for (u, s) in self._spawning if s != DEFAULT_SLOT)
            if live >= cap:
                victim = self._pick_idle_victim_unlocked(exclude=slot)
        if live >= cap:
            if victim:
                logger.info(f"空闲会话槽回收: {victim} → 让位给 {slot}")
                self.remove(LOCAL_USER, slot=victim)
            else:
                raise SlotsFullError(cap)
        return slot

    def _pick_idle_victim_unlocked(self, exclude: str) -> str | None:
        """持锁下挑一个非流式、非 main 的最久未活动槽。没有则 None。"""
        idle: list[tuple[float, str]] = []
        for (_u, s), w in self._workers.items():
            if s == DEFAULT_SLOT or s == exclude:
                continue
            if not w.is_alive():
                continue
            if getattr(w, "streaming", False):
                continue
            idle.append((float(getattr(w, "last_active", 0.0) or 0.0), s))
        if not idle:
            return None
        idle.sort()
        return idle[0][1]

    def cancel_for(self, session_id: str) -> bool:
        """向指定会话槽发取消；槽不存在/未启动时静默跳过（不为此 spawn）。"""
        slot = (session_id or "").strip() or DEFAULT_SLOT
        with self._lock:
            wp = self._workers.get((LOCAL_USER, slot))
        if wp is not None and wp.is_alive():
            wp.request_cancel()
            return True
        return False

    def remember_prefs(self, prefs: dict) -> None:
        """记录并广播用户 prefs 到全部存活实例（含默认槽）。

        合并而非替换：PUT /prefs 只提交变化键（None 过滤后），历史键
        保留在镜像里——替换语义会让"先设 A 再设 B"丢掉 A（新槽 spawn
        重放时 A 消失，多实例语义漂移）。
        """
        base = self._mirror_prefs if isinstance(self._mirror_prefs, dict) else {}
        self._mirror_prefs = {**base, **(prefs or {})}
        self._persist_state()
        with self._lock:
            targets = [w for w in self._workers.values() if w.is_alive()]
        for w in targets:
            w.send_fire_and_forget("prefs_set", prefs=self._mirror_prefs)

    def remember_permission_mode(self, mode: str) -> None:
        """记录并广播权限模式到全部存活实例。"""
        self._mirror_perm = mode
        self._persist_state()
        with self._lock:
            targets = [w for w in self._workers.values() if w.is_alive()]
        for w in targets:
            w.send_fire_and_forget("permission_mode_set", mode=mode)

    def broadcast_llm_params(self, clear_model_overrides: bool = False, **params) -> int:
        """广播模型热切换参数（llm_params_set）到全部存活实例。

        与 prefs/permission_mode 不同：不进镜像落盘——模型参数的持久真相
        源是 config.yaml（保存路径已先写盘 + reload_settings），新槽 spawn
        时自会读到新值；存量实例靠本命令注入，避免双源不一致。api_key 只
        在父子进程内存中流转，不落 web_state.json。

        clear_model_overrides=True（模型切换/回默认专用）：None/空串值不被
        过滤、原样下发——worker 侧语义为"显式清除该项 override"（防止
        上一档案的 api_key/context_window 残留）。默认 False：None 值过滤
        （保持现值，prefs 注入形状）。

        Returns:
            成功送达的实例数。单实例写入失败只告警不影响其余；
            worker 未启动时不为此 spawn（返回 0）。零存活实例时本方法
            仍记录内存挂账（_pending_llm_params），由下一个 spawn 重放——
            草稿会话（首条消息前）切换模型、此刻还没有任何 worker 的
            场景由此接住。
        """
        payload = (dict(params) if clear_model_overrides
                   else {k: v for k, v in params.items() if v is not None})
        ff_kwargs = {**payload, "clear_model_overrides": clear_model_overrides}
        with self._lock:
            targets = [w for w in self._workers.values() if w.is_alive()]
            self._queue_pending_spawning("llm_params_set", ff_kwargs)
            # 内存挂账（最新一次切换胜出）：不落盘，api_key 只在父子进程
            # 内存中流转。
            self._pending_llm_params = dict(ff_kwargs)
        delivered = 0
        for w in targets:
            try:
                if w.send_fire_and_forget("llm_params_set", **ff_kwargs):
                    delivered += 1
            except Exception:
                logger.warning("llm_params_set 广播失败（跳过该实例）", exc_info=True)
        return delivered

    def broadcast_settings_updates(self, updates: dict) -> int:
        """广播系统配置热更新（settings_update）到全部存活实例。

        与 broadcast_llm_params 同语义：不进镜像落盘——持久真相源是
        config.yaml（保存路径已先写盘 + 主进程 reload_settings），新槽
        spawn 时自会读到新值；存量实例靠本命令把变更原地合并进内存
        settings（worker 侧原地生效，无需重启）。重启键（workspace_root /
        web_proxy）由调用方过滤，不进本通道。

        Returns:
            成功送达的实例数。单实例写入失败只告警不影响其余；
            worker 未启动时不为此 spawn（返回 0）。
        """
        payload = {k: v for k, v in (updates or {}).items() if v is not None}
        if not payload:
            return 0
        with self._lock:
            targets = [w for w in self._workers.values() if w.is_alive()]
            self._queue_pending_spawning("settings_update", {"updates": payload})
        delivered = 0
        for w in targets:
            try:
                if w.send_fire_and_forget("settings_update", updates=payload):
                    delivered += 1
            except Exception:
                logger.warning("settings_update 广播失败（跳过该实例）", exc_info=True)
        return delivered

    def get_permission_mode(self) -> str:
        """读镜像权限模式（零 IPC；worker 忙/未启动时也可即时读取）。"""
        return self._mirror_perm or "before_changes"

    # ---- spawn 窗口期的广播补发（Fix3：_workers + _spawning 全覆盖）----

    def _queue_pending_spawning(self, op: str, kwargs: dict) -> None:
        """把一次广播（op + 与 send_fire_and_forget 同形的 kwargs）挂队到
        全部在途 spawn 占位（须持 self._lock 调用）。

        广播快照只取 _workers 会漏掉"正在 spawn"的槽——该槽 ready 前收不
        到本次更新。挂队后由 owner 线程在 get_or_create 注册完成、镜像重放
        之后统一补发（顺序在镜像之后，保证广播值不被 spawn 重放覆盖）。
        """
        for key in self._spawning:
            self._pending_broadcasts.setdefault(key, []).append((op, dict(kwargs)))

    def _replay_pending_broadcasts(self, key: tuple[str, str], wp: WorkerProcess) -> None:
        """spawn 落地后补发挂队广播（非 owner 线程不会走到这里）。"""
        with self._lock:
            pending = self._pending_broadcasts.pop(key, [])
        for op_name, kwargs in pending:
            try:
                wp.send_fire_and_forget(op_name, **kwargs)
            except Exception:
                logger.warning(f"pending 广播补发失败: slot={key[1]}, op={op_name}",
                               exc_info=True)

    def _replay_user_state(self, key: tuple[str, str], wp: WorkerProcess) -> None:
        """spawn 后重放用户级状态到新 worker（所有槽含 main）。

        顺序：prefs/权限镜像 → 挂队广播 → 模型挂账（最后，保证最新一次
        模型切换不被更早的挂队值覆盖）。
        - 镜像（prefs/权限）落盘 web_state.json：worker 崩溃/重启后不丢；
          main 槽不再是"设置来源"——镜像落盘后才是唯一事实源。
        - 模型挂账（_pending_llm_params）只在内存：api_key 不得进
          web_state.json（与 broadcast_llm_params 的不落盘纪律一致）。
          草稿会话切换模型时若无任何存活 worker，靠本重放让下一个 spawn
          （通常正是首条消息的会话 worker）拿到同一 override。
        """
        if self._mirror_prefs:
            wp.send_fire_and_forget("prefs_set", prefs=self._mirror_prefs)
        if self._mirror_perm:
            wp.send_fire_and_forget("permission_mode_set", mode=self._mirror_perm)
        # spawn 窗口期挂队的广播（settings_update / llm_params_set）在镜像
        # 重放之后补发——顺序保证广播值不被 spawn 重放覆盖
        self._replay_pending_broadcasts(key, wp)
        if self._pending_llm_params:
            wp.send_fire_and_forget("llm_params_set", **self._pending_llm_params)

    def get_or_create(self, user_id: str = LOCAL_USER,
                      slot: str = DEFAULT_SLOT) -> WorkerProcess:
        """获取或创建指定槽位的 worker 进程（user_id 恒为 LOCAL_USER）。

        P2-12：spawn + ready 等待（Popen + 最长 60s 轮询）移出全局锁——
        全局锁内只做「查表 + 占位去重」：
        - 快路径：槽存活 → 直接返回（持锁，O(1)）；
        - 同 key 已有在途 spawn → 在 per-spawn 事件上等待，落地后
          double-check 复用（或接手重试）；
        - 成为 owner → 释放全局锁后再 Popen + 等 ready，成功才写回表。
        期间 lookup / acquire_chat_slot / cancel_for 等只读路径零阻塞。
        """
        user_id = LOCAL_USER
        slot = slot or DEFAULT_SLOT
        key = (user_id, slot)
        while True:
            with self._lock:
                wp = self._workers.get(key)
                if wp and wp.is_alive():
                    return wp
                if wp and not wp.is_alive():
                    del self._workers[key]
                ev = self._spawning.get(key)
                owner = ev is None
                if owner:
                    ev = threading.Event()
                    self._spawning[key] = ev
            if owner:
                break
            # 他人在 spawn 本槽：等其尘埃落定后回到顶部 double-check
            ev.wait()

        try:
            # 全局锁外 spawn（Windows Popen + 重 import + ready 握手以十秒计）
            proc = subprocess.Popen(
                [self._python_executable(), "-m", "web_fastapi.worker_process",
                 user_id, slot],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=sys.stderr,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
            wp = WorkerProcess(user_id, proc, slot=slot)

            # 等待 ready 信号（60s 超时）——走 WorkerProcess 自带的常驻泵
            deadline = time.time() + 60
            ready = False
            while time.time() < deadline:
                remaining = deadline - time.time()
                line = wp._pump.get(min(remaining, 10))
                if line is None:
                    continue
                if line is _EOF or not line:
                    proc.kill()
                    raise RuntimeError(f"worker[{slot}] 启动失败（stdout 关闭）")
                data = decode_message(line)
                if data and data.get("type") == "ready":
                    ready = True
                    break
                if data and data.get("type") == "error":
                    proc.kill()
                    raise RuntimeError(
                        f"worker[{slot}] 初始化失败: {data.get('message')}")

            if not ready:
                proc.kill()
                raise RuntimeError(f"worker[{slot}] 启动超时（60s 未收到 ready）")

            with self._lock:
                self._workers[key] = wp
            logger.info(f"worker 启动: user={user_id}, slot={slot}, pid={proc.pid}")
        finally:
            with self._lock:
                self._spawning.pop(key, None)
                # spawn 失败：挂队广播一并丢弃（成功路径已在下方补发时弹出，
                # 此处 pop 对其是 no-op）——避免失败残留被下一次 spawn 误收
                # （补发非正确性依赖：新进程从 config.yaml/镜像读最新值）。
                self._pending_broadcasts.pop(key, None)
            ev.set()

        # 锁外重放用户级状态（所有槽含 main：镜像 + 挂队广播 + 模型挂账）
        self._replay_user_state(key, wp)
        return wp

    def remove(self, user_id: str = LOCAL_USER,
               slot: str = DEFAULT_SLOT) -> None:
        """关闭并移除指定槽位的 worker。

        P2-12：spawn 移出全局锁后，删除可能赶上一个在途 spawn——等其
        落地再回收（保持旧全局锁串行下"删除必删干净"的语义）。
        """
        user_id = LOCAL_USER
        key = (user_id, slot or DEFAULT_SLOT)
        while True:
            with self._lock:
                wp = self._workers.pop(key, None)
                ev = self._spawning.get(key)
            if wp:
                wp.shutdown()
                logger.info(f"worker 关闭: user={user_id}, slot={slot}")
                return
            if ev is None:
                return
            ev.wait()

    def all_slots(self) -> list[tuple[str, str]]:
        """当前全部存活槽键（保留 all_users 别名兼容旧调用方）。"""
        with self._lock:
            return list(self._workers.keys())

    def all_users(self) -> list[tuple[str, str]]:
        return self.all_slots()

    def shutdown_all(self):
        """关闭所有 worker（进程退出时调）。

        P2-12：在途 spawn 落地后可能补写 _workers——迭代排空直到无存量
        也无在途（等价旧实现"全局锁使 shutdown 与 spawn 互斥"的语义）。
        """
        while True:
            with self._lock:
                workers = list(self._workers.values())
                self._workers.clear()
                events = list(self._spawning.values())
            for wp in workers:
                wp.shutdown()
            if not events:
                break
            for ev in events:
                ev.wait()
        logger.info(f"已关闭全部 worker ({len(workers)} 个)")
