"""
============================================
WakerAsyncRunner —— 主进程后台运行 waker（脱离 worker IPC）
============================================
在 FastAPI 主进程内用线程池 fork worker_node 子进程跑 waker，**完全不碰
per-user worker 的 IPC 锁**，因此不会和 chat 抢锁。

## 为什么需要它？
之前 waker scheduler 调 wp.send("waker_run")，走 per-user worker 的 IPC。
worker 是一把串行锁——chat 进行中时 waker_run 拿不到锁（5s 超时），
反之 waker 跑时 chat 也得等。flow 已经脱离 worker（FlowRunner fork worker_node），
waker 单体用同样的模型即可。

## 与 FlowRunner 的关系
- FlowRunner：跑 WakerFlowExecutor（多节点 DAG），executor 内部 fork worker_node
- WakerAsyncRunner：直接 fork 一个 worker_node 跑单个 waker（相当于"只有一个
  worker 节点的 flow"）

两者都用 worker_node 子进程，但 WakerAsyncRunner 更轻——不需要 executor 的
DAG 调度，直接 fork → 读 stdout → 写 jsonl + latest_result。

## worker_node 的 stdout NDJSON 协议
子进程输出：
  {"type":"ready", ...}                 启动就绪
  {"type":"node_event", "event":{...}}  agent 事件（token/tool_*/complete）
  {"type":"result", "status", "content", "run_id", "waker"}  最终结果
本 runner 读这些，把 node_event 里的 event 写进 waker 的 jsonl（与原
src/waker/runner.py 的格式一致），result.content 写 latest_result.md。

## task 组装
worker_node 的任务文本经 stdin 传入（--task-stdin，P2-20：argv 会撞
Windows 32k 命令行上限），覆盖 waker 的 task_prompt。本 runner 读
cfg.task_prompt + 可选 api_prompt 拼成 task（与原 runner.py 的逻辑一致）。
"""
from __future__ import annotations

import json
import logging
import os
import queue as _q
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.storage.run_registry import RunRegistry
from src.waker.store import WakerStore

logger = logging.getLogger("hermes.waker.async_runner")

# worker_node 子进程默认超时（与 WAKER_RUN_TIMEOUT / FlowRunner 对齐）
WAKER_NODE_TIMEOUT = 600.0


def _node_timeout() -> float:
    """子进程总超时（秒）：config.waker_node_timeout 可覆盖，缺省/非法回退 600。

    600s 对重任务不够——9B 模型逐篇抓取+解读 10 分钟可归档 9-15 篇后撞墙
    （任务在正常推进，只是被上限截断成 error），调大而非放任截断。
    """
    from config import get_settings
    try:
        v = float(getattr(get_settings(), "waker_node_timeout", 0) or 0)
    except Exception:
        v = 0.0
    return v if v > 0 else WAKER_NODE_TIMEOUT


@dataclass
class WakerRunRecord:
    """单次 waker 运行的记录字段形状（与 FlowRunner.RunRecord 对齐）。

    T8b 起登记表落 kv（RunRegistry，重启可见）；此类保留定义对外字段
    契约，运行态实际以 dict 形式存储。
    """
    run_id: str
    waker_name: str
    user_id: str
    status: str = "running"      # running / ok / error / interrupted
    started_at: str = ""
    finished_at: str = ""


class WakerAsyncRunner:
    """主进程 waker 运行器（线程池 + fork worker_node 子进程）。

    Args:
        workspace_root: workspace 根
        max_concurrent: 同时运行的 waker 数上限
        storage: KVProtocol（运行登记表持久化用）。None 时用默认库的
            SQLiteProvider（data/hermes.db）
    """

    def __init__(self, workspace_root: str = "", max_concurrent: int = 2, storage=None):
        self._workspace_root = workspace_root
        self._max_concurrent = max(1, int(max_concurrent))
        self._executor: ThreadPoolExecutor | None = None
        self._pending_futures: set = set()
        # 运行状态表：run_id → 记录 dict（供路由层查活跃 run）。
        # T8b 起经 RunRegistry 写穿 kv（scope="runs", key="waker_async"），
        # 重启后可见；残留 running 标 interrupted。
        self._runs = RunRegistry("waker_async", storage)
        self._runs_lock = threading.Lock()

    def start(self) -> None:
        if self._executor is not None:
            return
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_concurrent,
            thread_name_prefix="waker-async-run",
        )
        logger.info(f"WakerAsyncRunner 已启动: max_concurrent={self._max_concurrent}")

    def shutdown(self) -> None:
        ex = self._executor
        if ex is None:
            return
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except Exception:
            ex.shutdown(wait=False)
        self._executor = None
        logger.info("WakerAsyncRunner 已关闭")

    # ════════════════════════════════════════════════════════════
    # 提交运行
    # ════════════════════════════════════════════════════════════
    def submit(
        self, user_id: str, name: str, run_id: str,
        api_prompt: str | None = None,
    ) -> str:
        """提交一次 waker 运行（异步，立即返回 run_id）。

        在线程池里 fork worker_node 子进程跑，不阻塞调用方。

        Raises:
            ValueError: waker 不存在 / task_prompt 为空 / 线程池未启动
        """
        if self._executor is None:
            raise RuntimeError("WakerAsyncRunner 未启动")

        # 读配置 + 组装 task（同步，让错误立即抛给调用方）
        store = WakerStore(user_id, workspace_root=self._workspace_root)
        cfg = store.get(name)
        if cfg is None:
            raise ValueError(f"waker 不存在: {user_id}/{name}")
        task_input = cfg.task_prompt or ""
        if api_prompt:
            task_input = (task_input + "\n\n[API 触发附加指令]\n" + api_prompt).strip()
        if not task_input:
            raise ValueError(f"waker {name} task_prompt 为空，无任务可执行")

        # 记录运行状态（供路由层查活跃 run；RunRegistry 写穿 kv）
        with self._runs_lock:
            self._runs.upsert({
                "run_id": run_id, "waker_name": name, "user_id": user_id,
                "status": "running",
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": "",
            })

        future = self._executor.submit(
            self._run_safe, user_id, name, run_id, task_input,
        )
        self._pending_futures.add(future)
        future.add_done_callback(lambda f: self._pending_futures.discard(f))
        logger.info(f"waker 已提交: {user_id}/{name} run_id={run_id}")
        return run_id

    def _run_safe(
        self, user_id: str, name: str, run_id: str, task: str,
    ) -> tuple[str, str]:
        """线程池任务：异常隔离。返回 (status, final_text)。"""
        try:
            status, text = self._run(user_id, name, run_id, task)
        except Exception:
            logger.exception(f"waker 异步运行异常: {user_id}/{name}/{run_id}")
            status, text = ("error", "内部异常（见服务日志）")
        # 更新内存状态表
        self._set_record_status(run_id, status)
        return (status, text)

    def _set_record_status(self, run_id: str, status: str) -> None:
        with self._runs_lock:
            fields: dict = {"status": status}
            if status in ("ok", "error"):
                fields["finished_at"] = datetime.now().isoformat(timespec="seconds")
            self._runs.update_status(run_id, **fields)

    def get_status(self, run_id: str) -> dict | None:
        """查某次 run 的状态。不存在返回 None。"""
        with self._runs_lock:
            rec = self._runs.get(run_id)
            if rec is None:
                return None
            return {
                "run_id": rec.get("run_id", ""), "waker_name": rec.get("waker_name", ""),
                "status": rec.get("status", ""), "started_at": rec.get("started_at", ""),
                "finished_at": rec.get("finished_at", ""),
            }

    def list_active(self, user_id: str | None = None) -> list[dict]:
        """列出正在运行的 waker（供路由层标记卡片 running 态）。"""
        with self._runs_lock:
            out = []
            for rec in self._runs.list():
                if rec.get("status") == "running":
                    if user_id is None or rec.get("user_id") == user_id:
                        out.append({
                            "run_id": rec.get("run_id", ""),
                            "waker_name": rec.get("waker_name", ""),
                            "user_id": rec.get("user_id", ""),
                            "status": rec.get("status", ""),
                            "started_at": rec.get("started_at", ""),
                        })
            return out

    def run_sync(
        self, user_id: str, name: str, run_id: str,
        api_prompt: str | None = None,
    ) -> str:
        """同步运行 waker（阻塞到子进程结束）。供 WakerScheduler 在自己的线程池里调。

        与 submit 的区别：submit 异步立即返回；run_sync 等子进程跑完。
        scheduler 本来就在 ThreadPoolExecutor 线程里，同步等结果是合适的。

        Returns:
            "ok" | "error"
        Raises:
            ValueError: waker 不存在 / task_prompt 为空
        """
        store = WakerStore(user_id, workspace_root=self._workspace_root)
        cfg = store.get(name)
        if cfg is None:
            raise ValueError(f"waker 不存在: {user_id}/{name}")
        task_input = cfg.task_prompt or ""
        if api_prompt:
            task_input = (task_input + "\n\n[API 触发附加指令]\n" + api_prompt).strip()
        if not task_input:
            raise ValueError(f"waker {name} task_prompt 为空")

        status, _text = self._run(user_id, name, run_id, task_input)
        return status

    def _run(
        self, user_id: str, name: str, run_id: str, task: str,
    ) -> tuple[str, str]:
        """fork worker_node 子进程，读 stdout NDJSON，写 jsonl + latest_result。

        Returns:
            (status, final_text)
        """
        node_run_id = f"{run_id}"  # waker 单体的 node_run_id = run_id
        cmd = [
            sys.executable, "-m", "src.wakerflow.worker_node",
            "--user-id", user_id,
            "--waker-name", name,
            # P2-20 同源：任务文本经 stdin 传入（--task-stdin），不走 argv
            #——api_prompt 拼接后可达数万字符，Windows 的 32k 命令行上限
            # 会让 spawn 直接 WinError 206。与 executor._run_worker 对齐。
            "--task-stdin",
            "--node-run-id", node_run_id,
            "--workspace-root", self._workspace_root,
        ]
        creationflags = (
            subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )

        # 准备 jsonl（与 src/waker/runner.py 的格式一致）
        store = WakerStore(user_id, workspace_root=self._workspace_root)
        run_dir = store.run_dir(name)
        run_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = run_dir / f"{run_id}.jsonl"

        def _append_event(event: dict) -> None:
            line = json.dumps(event, ensure_ascii=False, default=str)
            with open(jsonl_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()

        _append_event({
            "type": "run_start",
            "run_id": run_id, "name": name,
            "ts": datetime.now().isoformat(timespec="seconds"),
        })

        proc = None
        final_status = "error"
        final_text = ""
        _node_budget = _node_timeout()
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # stderr 不能 PIPE 不读——子进程日志写满缓冲区会死锁
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            # 任务文本写入 stdin 后立即关闭——子进程启动即整读 stdin
            # （--task-stdin），单次 write 即便超过管道缓冲区也会被及时
            # 消费，无死锁（与 executor._run_worker 一致）。
            assert proc.stdin is not None
            try:
                proc.stdin.write(task)
                proc.stdin.close()
            except Exception as e:
                if proc.poll() is None:
                    proc.kill()
                raise RuntimeError(
                    f"任务文本写入 stdin 失败: {e}（task {len(task)} 字符）"
                ) from e
            assert proc.stdout is not None
            # P2-3：泵线程 + 队列 + 总截止——旧 for line in stdout 无界阻塞，
            # 卡死的子进程永久占住 waker 池线程（超时分支不可达）
            line_q: "_q.Queue[str | None]" = _q.Queue()

            def _pump() -> None:
                try:
                    for raw in proc.stdout:
                        line_q.put(raw)
                except Exception:
                    pass
                finally:
                    line_q.put(None)

            threading.Thread(target=_pump, daemon=True, name="waker-pump").start()
            _deadline = time.monotonic() + _node_budget
            while True:
                _remain = _deadline - time.monotonic()
                if _remain <= 0:
                    raise subprocess.TimeoutExpired(cmd=f"{name}/{run_id}", timeout=_node_budget)
                try:
                    raw = line_q.get(timeout=min(_remain, 30.0))
                except _q.Empty:
                    continue
                if raw is None:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type")
                if etype == "ready":
                    logger.info(f"waker 子进程就绪: {name}/{run_id}")
                elif etype == "node_event":
                    # 透传 agent 事件到 jsonl（与 runner.py 格式一致）
                    inner = ev.get("event", {})
                    if isinstance(inner, dict):
                        _append_event(inner)
                elif etype == "result":
                    final_status = ev.get("status", "error")
                    final_text = ev.get("content", "") or ""
                    if final_status == "error" and not final_text:
                        final_text = ev.get("message", "") or ""

            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass

            if final_status != "ok" and not final_text:
                final_text = "worker_node 未返回 result（子进程崩溃或超时）"

        except subprocess.TimeoutExpired:
            if proc is not None and proc.poll() is None:
                proc.kill()
            final_status = "error"
            final_text = f"waker 超时（>{_node_budget}s）"
        except Exception as e:
            logger.exception(f"waker 异步运行失败: {name}/{run_id}")
            final_status = "error"
            final_text = f"运行失败: {e}"

        # 写 latest_result.md（与 runner.py 格式一致）
        try:
            result_path = store.latest_result_path(name)
            result_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = result_path.with_suffix(result_path.suffix + ".tmp")
            tmp.write_text(
                f"# waker: {name}\n\n**run_id**: {run_id}\n**status**: {final_status}\n"
                f"**ts**: {datetime.now().isoformat(timespec='seconds')}\n\n---\n\n{final_text}\n",
                encoding="utf-8",
            )
            os.replace(tmp, result_path)
        except Exception:
            logger.exception(f"waker {name}/{run_id} 写 latest_result 失败")

        _append_event({
            "type": "run_end",
            "run_id": run_id, "name": name,
            "status": final_status,
            "ts": datetime.now().isoformat(timespec="seconds"),
        })
        return (final_status, final_text)
