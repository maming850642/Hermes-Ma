"""
============================================
FlowRunner —— 主进程后台运行 WakerFlow
============================================
在 FastAPI 主进程内用线程池跑 WakerFlowExecutor，**完全脱离 worker IPC**。

## 为什么不走 worker IPC？
WakerFlow 的 worker 节点 fork 的是独立的 src.wakerflow.worker_node 子进程
（跑完整 HermesAgentV3），与 web_fastapi/worker_process.py 的 per-user worker
是两套子进程。executor 本身在主进程线程里跑即可，不需要 worker_process 的 agent。

之前让 flow 走 worker.send("wakerflow_run") 是架构错误——它会**占住 per-user
worker 的 IPC 通道**，导致 flow 运行期间同一用户的 chat 全部排队阻塞。

## 线程模型
- submit(...) → 立即返回 run_id，executor 在线程池里异步跑
- 内存状态表 _runs 记录每次 run 的 status / returns / error
- jsonl 事件日志照旧写盘（executor on_event 回调 → FlowStore.run_jsonl_path）
- ask_user 审批文件照旧（executor 轮询 _approvals/<run_id>.json），
  approve 现在由主进程路由直接写文件，不走任何 IPC

## 生命周期
app.py lifespan 里创建（仿 WakerScheduler），退出时 shutdown(wait=False)。
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from src.storage.run_registry import RunRegistry
from src.wakerflow.executor import (
    FlowSuspended,
    SuspendedAsk,
    WakerFlowExecutor,
    _ApprovalWatch,
)
from src.wakerflow.parser import FlowParseError, parse_flow
from src.wakerflow.store import FlowStore

logger = logging.getLogger("hermes.wakerflow.runner")


@dataclass
class RunRecord:
    """单次 flow 运行的记录字段形状。

    T8b 起登记表落 kv（RunRegistry，重启可见）；此类保留定义对外字段
    契约，运行态实际以 dict 形式存储。jsonl 事件日志照旧独立落盘。
    """
    run_id: str
    flow_name: str
    user_id: str
    status: str = "pending"      # pending / running / completed / failed / error / interrupted
    started_at: str = ""
    finished_at: str = ""
    returns: dict = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "flow_name": self.flow_name,
            "user_id": self.user_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "returns": self.returns,
            "error": self.error,
        }


class FlowRunner:
    """主进程 WakerFlow 运行器（线程池 + 运行状态表）。

    Args:
        workspace_root: workspace 根（FlowStore / executor 用）
        max_concurrent: 同时运行的 flow 数上限
        storage: KVProtocol（运行登记表持久化用）。None 时用默认库的
            SQLiteProvider（data/hermes.db）
    """

    def __init__(self, workspace_root: str = "", max_concurrent: int = 2, storage=None):
        self._workspace_root = workspace_root
        self._max_concurrent = max(1, int(max_concurrent))
        self._executor: ThreadPoolExecutor | None = None
        # run_id → 记录 dict（含所有用户的 run，按 run_id 唯一）。
        # T8b 起经 RunRegistry 写穿 kv（scope="runs", key="flow"），重启后
        # 可见；残留 pending/running/waiting_approval 标 interrupted
        # （重启后看护线程与 executor 都不在了，挂起的 run 不可能续跑）。
        self._runs = RunRegistry(
            "flow", storage,
            active_statuses=("pending", "running", "waiting_approval"),
        )
        self._runs_lock = threading.Lock()
        self._pending_futures: set = set()
        # P2-19/P2-22：run_id → 完成回调。FlowScheduler 的防重入键靠它覆盖
        # 整个运行期（submit 返回 ≠ 运行结束）；ask_user 挂起期间回调保留，
        # 续跑完成后才触发（一次性，pop 语义）。
        self._on_done: dict[str, Callable[[], None]] = {}
        # 关停标志：shutdown() 置位，start() 复位。submit 用它与
        # _executor is None 一起判定，堵「登记 on_done 后线程池被关停」
        # 的竞态窗口（否则 on_done 泄漏 + run 永远 pending）。
        self._shutdown = False

    # ════════════════════════════════════════════════════════════
    # 生命周期
    # ════════════════════════════════════════════════════════════
    def start(self) -> None:
        if self._executor is not None:
            return
        self._shutdown = False
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_concurrent,
            thread_name_prefix="wakerflow-run",
        )
        logger.info(f"FlowRunner 已启动: max_concurrent={self._max_concurrent}")

    def shutdown(self) -> None:
        """关闭线程池。不强制等待（flow 可能卡在 ask_user 几小时）。

        cancel_futures=True 取消尚未开始的；已在跑的让它自然完成
        （进程退出时 daemon 线程会被强杀，可接受）。
        先置关停标志再关池：submit 据此拒绝新任务（防登记后线程池消失）。
        """
        self._shutdown = True
        ex = self._executor
        if ex is None:
            return
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except Exception:
            ex.shutdown(wait=False)
        self._executor = None
        logger.info("FlowRunner 已关闭")

    # ════════════════════════════════════════════════════════════
    # 提交运行
    # ════════════════════════════════════════════════════════════
    def submit(
        self, user_id: str, flow_name: str, inputs: dict | None = None,
        on_done: "Callable[[], None] | None" = None,
    ) -> str:
        """提交一次 flow 运行，立即返回 run_id。

        on_done：运行**真正结束**后的一次性回调（completed/failed/error，
        或 ask_user 挂起后续跑收尾）。FlowScheduler 用它把防重入键的释放
        覆盖到整个运行期（P2-19）——submit 返回只代表已提交，不代表跑完。

        Raises:
            ValueError: flow 不存在 / YAML 解析失败
            RuntimeError: FlowRunner 未启动 / 已关停（含提交瞬间被关停）
        """
        if self._executor is None or self._shutdown:
            raise RuntimeError("FlowRunner 未启动或已关停")

        store = FlowStore(user_id, workspace_root=self._workspace_root)
        yaml_text = store.get(flow_name)
        if yaml_text is None:
            raise ValueError(f"flow 不存在: {flow_name}")

        # 解析放在提交线程（同步），让解析错误立即抛给调用方
        flow = parse_flow(yaml_text)  # 失败抛 FlowParseError

        run_id = store.new_run_id()
        record = RunRecord(
            run_id=run_id, flow_name=flow_name, user_id=user_id,
            started_at=_now_iso(),
        )
        with self._runs_lock:
            if on_done is not None:
                self._on_done[run_id] = on_done
            self._runs.upsert(record.to_dict())

        ex = self._executor
        try:
            # 关停竞态窗口：上面检查通过后 shutdown() 才发生 → ex.submit 抛
            # RuntimeError（cancel_futures 后拒绝新任务），此时 run 已登记、
            # 线程池已不在——on_done 永不触发（防重入键泄漏）、run 永远
            # pending。兜底：清理 on_done 登记，run 落明确 error 终态，
            # 异常原样抛给调用方。
            if ex is None or self._shutdown:
                raise RuntimeError("FlowRunner 已关停，任务未执行")
            future = ex.submit(
                self._run_safe, user_id, flow_name, run_id, flow, inputs or {},
            )
        except RuntimeError:
            with self._runs_lock:
                self._on_done.pop(run_id, None)
            self._set_status(run_id, "error", error="FlowRunner 已关停，任务未执行")
            raise
        self._pending_futures.add(future)
        future.add_done_callback(self._make_done_cb(run_id))
        logger.info(f"flow 已提交: {user_id}/{flow_name} run_id={run_id}")
        return run_id

    def _make_done_cb(self, run_id: str):
        def _cb(fut) -> None:
            self._pending_futures.discard(fut)
        return _cb

    def _run_safe(
        self, user_id: str, flow_name: str, run_id: str,
        flow: Any, inputs: dict,
    ) -> None:
        """线程池任务：异常隔离包装。

        挂起（等待审批）不算完成——on_done 留给续跑完成后触发（P2-22）。
        """
        suspended = False
        try:
            suspended = self._run(user_id, flow_name, run_id, flow, inputs)
        except Exception:
            logger.exception(f"flow 运行异常: {user_id}/{flow_name}/{run_id}")
            self._set_status(run_id, "error", error="内部异常（见服务日志）")
        if not suspended:
            self._fire_on_done(run_id)

    def _run(
        self, user_id: str, flow_name: str, run_id: str,
        flow: Any, inputs: dict,
    ) -> bool:
        """实际跑 executor（在线程池线程里）。返回是否因 ask_user 挂起。"""
        self._set_status(run_id, "running")

        store = FlowStore(user_id, workspace_root=self._workspace_root)
        jsonl_path = store.run_jsonl_path(flow_name, run_id)
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)

        # on_event：写 jsonl（与原 worker_process.wakerflow_run 行为一致）
        jl_file = open(jsonl_path, "a", encoding="utf-8")
        try:
            def _on_event(event: dict) -> None:
                try:
                    jl_file.write(
                        json.dumps(event, ensure_ascii=False, default=str) + "\n"
                    )
                    jl_file.flush()
                except Exception:
                    pass

            _on_event({
                "type": "flow_start", "run_id": run_id,
                "flow": flow_name, "ts": _now_iso(),
            })

            ex = WakerFlowExecutor(
                flow, user_id=user_id, run_id=run_id, inputs=inputs,
                workspace_root=self._workspace_root, on_event=_on_event,
                suspend_ask=True,  # P2-22：顶层 ask_user 等待挂起，不占池线程
            )
            try:
                result = ex.run()
            except FlowSuspended as susp_exc:
                self._suspend_run(run_id, user_id, flow_name, susp_exc.suspension)
                return True

            # flow_end 由 executor._finish 统一 emit（带 flow_name/node_count/
            # returns 的语义完整版）——这里不再重复补发一条只有 status 的
            # flow_end（曾导致每个 run 的 jsonl 落两条 flow_end）。
            self._set_status(
                run_id, result["status"],
                returns=result.get("returns", {}),
            )
        finally:
            try:
                jl_file.close()
            except Exception:
                pass
        return False

    # ════════════════════════════════════════════════════════════
    # ask_user 挂起 / 续跑（P2-22）
    # ════════════════════════════════════════════════════════════
    def _suspend_run(
        self, run_id: str, user_id: str, flow_name: str, susp: SuspendedAsk,
    ) -> None:
        """登记 ask_user 挂起：起看护线程，池线程随即让出。

        审批文件被写 answered / cancelled（或超时）时，看护线程回调把续跑
        提交回线程池；等待期不占用 FlowRunner 的并发槽。
        """
        def _on_ready() -> None:
            self._submit_resume(susp)

        # 先置 waiting_approval 再建 watch：watch 线程构造后立即首轮轮询，
        # 极窄窗口内审批可能已落盘并唤醒续跑（终态回写先行）；若此刻才
        # _set_status("waiting_approval")，迟到的挂起态回写会覆盖终态
        # （run 永久卡 waiting_approval、防重入键不放）。状态先行，
        # 续跑侧的任何回写都必然发生在此之后。
        self._set_status(run_id, "waiting_approval")
        susp.watch = _ApprovalWatch(susp.approval_path, susp.timeout, _on_ready)
        logger.info(f"flow 挂起等待审批: {flow_name}/{susp.node_id} run_id={run_id}")

    def _submit_resume(self, susp: SuspendedAsk) -> None:
        """审批就绪后的回调：把续跑提交回线程池（runner 已关则一次性线程兜底）。"""
        ex = self._executor
        if ex is not None:
            future = ex.submit(self._resume_safe, susp)
            self._pending_futures.add(future)
            future.add_done_callback(self._make_done_cb(susp.run_id))
        else:
            threading.Thread(
                target=self._resume_safe, args=(susp,), daemon=True,
                name="wakerflow-resume",
            ).start()

    def _resume_safe(self, susp: SuspendedAsk) -> None:
        try:
            self._resume(susp)
        except FlowSuspended as susp_exc:
            # flow 有后续顶层 ask_user：resume 复用 _run_loop 且 executor 的
            # _suspend_ask 仍为 True，续跑到下一个顶层 ask_user 会再次抛
            # FlowSuspended（继承 BaseException，绕过 except Exception）。
            # 不重新登记的话：审批文件已写但 watch 未创建（永无续跑）、run
            # 卡 running、on_done 被 finally 触发放掉防重入键（可重入成僵尸
            # run）。这里走 _suspend_run 把 watch 挂到新 suspension，审批后
            # 继续续跑；run 未结束，on_done 不触发。
            new_susp = susp_exc.suspension
            logger.info(
                f"flow 续跑再次挂起: {susp.user_id}/{susp.flow_name}/"
                f"{new_susp.node_id} run_id={susp.run_id}"
            )
            self._suspend_run(
                susp.run_id, susp.user_id, susp.flow_name, new_susp
            )
            return
        except Exception:
            logger.exception(
                f"flow 续跑异常: {susp.user_id}/{susp.flow_name}/{susp.run_id}"
            )
            self._set_status(susp.run_id, "error", error="内部异常（见服务日志）")
        self._fire_on_done(susp.run_id)

    def _resume(self, susp: SuspendedAsk) -> None:
        """审批就绪后续跑挂起的 flow（在线程池线程里）。"""
        self._set_status(susp.run_id, "running")

        store = FlowStore(susp.user_id, workspace_root=self._workspace_root)
        jsonl_path = store.run_jsonl_path(susp.flow_name, susp.run_id)
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)

        jl_file = open(jsonl_path, "a", encoding="utf-8")
        try:
            def _on_event(event: dict) -> None:
                try:
                    jl_file.write(
                        json.dumps(event, ensure_ascii=False, default=str) + "\n"
                    )
                    jl_file.flush()
                except Exception:
                    pass

            result = susp.executor.resume(susp, on_event=_on_event)

            # flow_end 由 executor._finish 统一 emit（同 _run，不重复补发）。
            self._set_status(
                susp.run_id, result["status"],
                returns=result.get("returns", {}),
            )
        finally:
            try:
                jl_file.close()
            except Exception:
                pass

    def cancel(self, user_id: str, run_id: str) -> bool:
        """取消挂起中的 run：把审批文件写为 cancelled（P2-22）。

        看护线程读到 cancelled 后提前终止等待，flow 以失败收尾。
        无 pending 审批文件返回 False。
        """
        store = FlowStore(user_id, workspace_root=self._workspace_root)
        apath = store.approval_path(run_id)
        if not apath.exists():
            return False
        try:
            data = json.loads(apath.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(data, dict) or data.get("status") != "pending":
            return False
        data["status"] = "cancelled"
        tmp = apath.with_suffix(apath.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        import os as _os
        _os.replace(tmp, apath)
        logger.info(f"flow run 已取消: {run_id}")
        return True

    def _fire_on_done(self, run_id: str) -> None:
        """触发并清理 run 的完成回调（一次性：挂起 + 续跑只触发一次）。"""
        with self._runs_lock:
            cb = self._on_done.pop(run_id, None)
        if cb is not None:
            try:
                cb()
            except Exception:
                logger.exception(f"flow on_done 回调异常: {run_id}")

    # ════════════════════════════════════════════════════════════
    # 状态查询
    # ════════════════════════════════════════════════════════════
    def get_status(self, run_id: str) -> dict | None:
        """查某次 run 的状态。不存在返回 None。"""
        with self._runs_lock:
            return self._runs.get(run_id)

    def list_active(self, user_id: str | None = None) -> list[dict]:
        """列出运行中的 flow（可选过滤某用户）。供前端轮询进度。

        waiting_approval（ask_user 挂起）也属活跃——run 没结束。
        """
        with self._runs_lock:
            out = []
            for rec in self._runs.list():
                if rec.get("status") in ("pending", "running", "waiting_approval"):
                    if user_id is None or rec.get("user_id") == user_id:
                        out.append(rec)
            return out

    def list_recent(self, user_id: str | None = None, limit: int = 20) -> list[dict]:
        """列出最近的 run（含已完成）。前端 flow 卡片状态用。"""
        with self._runs_lock:
            recs = self._runs.list()
        if user_id is not None:
            recs = [r for r in recs if r.get("user_id") == user_id]
        # 按 started_at 降序
        recs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        return recs[:limit]

    # ════════════════════════════════════════════════════════════
    # 审批（主进程直接写文件，不走任何 IPC）
    # ════════════════════════════════════════════════════════════
    def approve(self, user_id: str, run_id: str, answer: Any, answered_by: str = "") -> bool:
        """写入审批响应。文件不存在返回 False。

        executor 的 _run_ask_user 在轮询读这个文件，status 变 answered 后继续。
        """
        store = FlowStore(user_id, workspace_root=self._workspace_root)
        apath = store.approval_path(run_id)
        if not apath.exists():
            return False
        try:
            data = json.loads(apath.read_text(encoding="utf-8"))
        except Exception:
            return False
        data["status"] = "answered"
        data["answer"] = answer
        data["answered_by"] = answered_by
        data["answered_ts"] = _now_iso()
        tmp = apath.with_suffix(apath.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        import os as _os
        _os.replace(tmp, apath)
        logger.info(f"审批已响应: {run_id} → {answer}")
        return True

    # ════════════════════════════════════════════════════════════
    # 内部
    # ════════════════════════════════════════════════════════════
    def _set_status(
        self, run_id: str, status: str,
        returns: dict | None = None, error: str = "",
    ) -> None:
        with self._runs_lock:
            fields: dict = {"status": status}
            if returns is not None:
                fields["returns"] = returns
            if error:
                fields["error"] = error
            if status in ("completed", "failed", "error"):
                fields["finished_at"] = _now_iso()
            self._runs.update_status(run_id, **fields)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
