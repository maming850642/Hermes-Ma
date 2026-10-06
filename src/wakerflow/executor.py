"""
============================================
WakerFlowExecutor —— WakerFlow DAG 调度执行器（核心）
============================================
在主进程 FlowRunner 线程池内实例化，**同步串行**跑完整个 flow
（askUser 节点 M2.3 才实现阻塞等审批文件，本任务返回 skipped 占位）。

## 节点类型分发（_run_step）
- worker 节点：fork src.wakerflow.worker_node 子进程跑一个 waker 任务，
  读 stdout NDJSON 取最终 result.content（_run_worker）
- parallel 节点：ThreadPoolExecutor 并发跑各子 step，sub_results 汇总
- pipeline 节点：串行跑各子 step，上游 result 自动喂给下游 context
- ask_user 节点：M2.3 占位（返回 skipped + 清晰 message）
- action 节点：HTTP 调用（urllib，不引 requests）

## context 结构（供模板插值）
    context = {
        "inputs": {完整输入 dict（validate_inputs 后）},
        "steps":  {step_id: NodeResult（to_dict 后便于模板访问）},
    }
模板里可写 {{ inputs.x }} / {{ steps.<id>.result }} 等。

## 事件回调（on_event）
全程通过 on_event(event_dict) 回调输出节点事件（node_start / node_end /
node_result / flow_start / flow_end）。调用方（worker_process）用它写 jsonl
或转发给前端。回调抛异常被吞掉（不影响 flow 执行）。

## 依赖
parser/template/models 由并行任务实现，本模块 import 时用 try/except 兜底：
- template.render：若 import 失败，用内置简单 {{ }} 插值 lambda 占位
- FlowSpec/StepNode：纯 dataclass，parser 写好后直接用
"""
from __future__ import annotations

import ast
import json
import logging
import operator as _op
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("hermes.wakerflow.executor")

# worker_node 子进程默认超时（秒）。与 src.waker.scheduler.WAKER_RUN_TIMEOUT 对齐。
WAKER_NODE_TIMEOUT = 600.0


def waker_node_timeout() -> float:
    """worker_node 子进程总超时（秒）：config.waker_node_timeout 可覆盖
    （与 src.waker.async_runner._node_timeout 同键同回退），缺省/非法回退 600。"""
    from config import get_settings
    try:
        v = float(getattr(get_settings(), "waker_node_timeout", 0) or 0)
    except Exception:
        v = 0.0
    return v if v > 0 else WAKER_NODE_TIMEOUT


# ════════════════════════════════════════════════════════════════
# 模板渲染：防御性 import（parser/template 可能尚未实现）
# ════════════════════════════════════════════════════════════════
def _default_render(template: str, context: dict) -> str:
    """简单 {{ }} 插值兜底（template 模块未实现时用）。

    支持 {{ a.b.c }} 形式的点号取值；未定义键替换为空串（保守，不抛）。
    非 str 模板（None/数字等）原样返回字符串形式。
    """
    if template is None:
        return ""
    if not isinstance(template, str):
        # 非 str（dict/list/数字等）：不渲染，原样返回 str 形式
        return str(template)

    def _resolve(path: str, ctx: dict):
        cur: Any = ctx
        for part in path.strip().split("."):
            if cur is None:
                return ""
            p = part.strip()
            if not p:
                return ""
            if isinstance(cur, dict):
                cur = cur.get(p, "")
            elif isinstance(cur, list):
                # 支持 {{ x.0 }} 索引取值
                try:
                    cur = cur[int(p)]
                except (ValueError, IndexError):
                    return ""
            else:
                cur = getattr(cur, p, "")
        return cur if cur is not None else ""

    out = []
    i = 0
    while i < len(template):
        j = template.find("{{", i)
        if j < 0:
            out.append(template[i:])
            break
        out.append(template[i:j])
        k = template.find("}}", j + 2)
        if k < 0:
            out.append(template[j:])
            break
        expr = template[j + 2:k].strip()
        out.append(str(_resolve(expr, context)))
        i = k + 2
    return "".join(out)


try:
    from src.wakerflow.template import render as _template_render  # type: ignore
    from src.wakerflow.template import TemplateError as _TemplateError  # type: ignore
except Exception:  # pragma: no cover - 联调期 fallback
    _template_render = None
    _TemplateError = None


def render(template: str, context: dict) -> str:
    """渲染模板：优先用 template 模块，模块不可用时用内置兜底。

    设计取舍：
    - 若 template 模块存在（联调完成后常态），直接用它的 render，**让
      TemplateError 正常向上抛**（缺失键是用户错误，应被 _run_step 捕获报错，
      而非静默替换为空串——避免 typo 占位符悄悄产生空 prompt）。
    - 若 template 模块尚未实现（联调期），用内置 _default_render 容错
      （缺失键替换空串，便于无 parser 时也能跑 smoke）。
    """
    if _template_render is not None:
        # 不吞 TemplateError——让上层 _run_step 把它转成节点 error
        return _template_render(template, context)
    return _default_render(template, context)


# ════════════════════════════════════════════════════════════════
# NodeResult 数据结构
# ════════════════════════════════════════════════════════════════
@dataclass
class NodeResult:
    """单节点执行结果。

    Attributes:
        node_id: 节点 id（StepNode.id）
        status: ok / error / skipped
        result: worker / action 节点的最终文本输出
        answer: ask_user 节点的回答（M2.3 用）
        sub_results: parallel / pipeline 的子节点结果 {sub_id: NodeResult}
        error: error/skipped 时的错误/原因说明
        ts: ISO 时间戳
    """
    node_id: str
    status: str = "ok"
    result: str = ""
    answer: Any = None
    sub_results: dict = field(default_factory=dict)
    error: str = ""
    ts: str = ""

    def to_dict(self) -> dict:
        """转 dict（供模板访问 + on_event 序列化）。

        sub_results 的每个子节点直接平铺到顶层（按子 id 作为 key），
        让模板能简洁引用 {{steps.<父id>.<子id>.result}}（而非 .sub_results.<子id>）。
        同时保留 sub_results 字段供完整序列化。
        """
        d = {
            "node_id": self.node_id,
            "status": self.status,
            "result": self.result,
            "answer": self.answer,
            "sub_results": {
                k: (v.to_dict() if isinstance(v, NodeResult) else v)
                for k, v in self.sub_results.items()
            },
            "error": self.error,
            "ts": self.ts,
        }
        # 平铺子结果到顶层（子 id 作 key），方便模板引用
        for k, v in self.sub_results.items():
            d[k] = v.to_dict() if isinstance(v, NodeResult) else v
        return d


# ════════════════════════════════════════════════════════════════
# ask_user 挂起等待（P2-22）：独立等待结构 + 回调唤醒
# ════════════════════════════════════════════════════════════════
@dataclass
class SuspendedAsk:
    """一次因 ask_user 等待而挂起的断点（含续跑所需的全部状态）。

    executor 实例与 context 驻留内存（FlowRunner 持有），审批就绪后由
    FlowRunner 在池线程调 executor.resume(susp) 从断点继续。
    """
    executor: "WakerFlowExecutor"
    run_id: str
    user_id: str
    flow_name: str
    node_id: str
    step_index: int              # 续跑起点：ask step 之后的顶层 step 下标
    context: dict
    approval_path: Path
    timeout: int
    default: Any
    watch: "_ApprovalWatch | None" = None   # FlowRunner 挂起时创建并回填


class FlowSuspended(BaseException):
    """顶层 ask_user 等待挂起：run() 中途让出 FlowRunner 池线程。

    继承 BaseException 而非 Exception：绕过 _run_step / _run_parallel 的
    `except Exception` 异常隔离层。只允许从 run() 的顶层 step 循环抛出
    （resume 断点只支持顶层 ask_user；嵌套在 parallel/pipeline 里的
    ask_user 仍走阻塞轮询，占的是节点内部线程）。
    """

    def __init__(self, suspension: SuspendedAsk):
        super().__init__(f"ask_user 挂起: {suspension.node_id}")
        self.suspension = suspension


class _ApprovalWatch:
    """审批文件看护线程（P2-22 的独立等待结构）。

    后台 daemon 线程轮询审批 json：status 变 answered / cancelled 或到超时，
    记录终态并回调 on_done（FlowRunner 借此把续跑提交回线程池）。等待期
    不占用任何执行线程池——两个没人理的审批不再冻结全部后续 flow。
    """

    def __init__(
        self, path: Path, timeout: int, on_done: Callable[[], None],
        poll_interval: float = 2.0,
    ):
        self.path = Path(path)
        self.timeout = max(0, int(timeout))
        self._on_done = on_done
        self._poll_interval = poll_interval
        # 终态：answered / cancelled / timeout（cancel() 放弃时不置终态）
        self.status = "timeout"
        self.answer: Any = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._poll, daemon=True, name="wakerflow-approval-watch",
        )
        self._thread.start()

    def _poll(self) -> None:
        deadline = time.monotonic() + self.timeout
        while not self._stop.is_set():
            try:
                if self.path.exists():
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    st = data.get("status")
                    if st == "answered":
                        self.status = "answered"
                        self.answer = data.get("answer")
                        break
                    if st == "cancelled":
                        # 审批文件被写 cancelled：提前终止等待（P2-22）
                        self.status = "cancelled"
                        break
            except Exception:
                logger.debug("审批看护读文件异常（继续轮询）", exc_info=True)
            if time.monotonic() >= deadline:
                self.status = "timeout"
                break
            self._stop.wait(self._poll_interval)
        if not self._stop.is_set():
            try:
                self._on_done()
            except Exception:
                logger.exception("审批看护 on_done 回调异常")

    def cancel(self) -> None:
        """放弃等待（runner 关闭等场景）：停轮询，不置终态、不回调。"""
        self._stop.set()


# ════════════════════════════════════════════════════════════════
# 安全表达式求值（if 条件）
# ════════════════════════════════════════════════════════════════
# 允许的 ast 节点白名单 + 二元运算符映射
_AST_BINOPS = {
    ast.Add: _op.add, ast.Sub: _op.sub, ast.Mult: _op.mul,
    ast.Div: _op.truediv, ast.Mod: _op.mod, ast.FloorDiv: _op.floordiv,
}
_AST_CMPOPS = {
    ast.Eq: _op.eq, ast.NotEq: _op.ne, ast.Lt: _op.lt, ast.LtE: _op.le,
    ast.Gt: _op.gt, ast.GtE: _op.ge, ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}
_AST_UNARYOPS = {
    ast.Not: _op.not_, ast.USub: _op.neg, ast.UAdd: _op.pos,
}


def _safe_eval_node(node: ast.AST, context: dict) -> Any:
    """递归求值 ast 节点（白名单限制）。非法节点抛 ValueError。"""
    if isinstance(node, ast.Expression):
        return _safe_eval_node(node.body, context)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        # 名字先从 context 顶层取（inputs/steps 等）；缺失则把标识符本身
        # 当字符串字面量（GitHub Actions if: 语义——用户写 mode == skip
        # 期望 skip 是字面量 "skip"，不是变量引用）。
        # 注意：Python 的 True/False/None 是 ast.Constant 不是 Name，
        # 不受此规则影响。
        if node.id in context:
            return context[node.id]
        return node.id
    if isinstance(node, ast.Attribute):
        base = _safe_eval_node(node.value, context)
        if base is None:
            return None
        if isinstance(base, dict):
            return base.get(node.attr)
        return getattr(base, node.attr, None)
    if isinstance(node, ast.Subscript):
        base = _safe_eval_node(node.value, context)
        idx = _safe_eval_node(node.slice, context)
        try:
            return base[idx]
        except Exception:
            return None
    if isinstance(node, ast.List):
        return [_safe_eval_node(e, context) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_safe_eval_node(e, context) for e in node.elts)
    if isinstance(node, ast.BinOp):
        op = _AST_BINOPS.get(type(node.op))
        if op is None:
            raise ValueError(f"不支持的二元运算: {type(node.op).__name__}")
        return op(
            _safe_eval_node(node.left, context),
            _safe_eval_node(node.right, context),
        )
    if isinstance(node, ast.Compare):
        left = _safe_eval_node(node.left, context)
        for op_node, right_node in zip(node.ops, node.comparators):
            op = _AST_CMPOPS.get(type(op_node))
            if op is None:
                raise ValueError(f"不支持的比较: {type(op_node).__name__}")
            right = _safe_eval_node(right_node, context)
            if not op(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result = True
            for v in node.values:
                if not _safe_eval_node(v, context):
                    return False
            return True
        if isinstance(node.op, ast.Or):
            for v in node.values:
                if _safe_eval_node(v, context):
                    return True
            return False
    if isinstance(node, ast.UnaryOp):
        op = _AST_UNARYOPS.get(type(node.op))
        if op is None:
            raise ValueError(f"不支持的一元运算: {type(node.op).__name__}")
        return op(_safe_eval_node(node.operand, context))
    raise ValueError(f"不支持的 ast 节点: {type(node).__name__}")


# ════════════════════════════════════════════════════════════════
# WakerFlowExecutor
# ════════════════════════════════════════════════════════════════
class WakerFlowExecutor:
    """WakerFlow DAG 调度器。

    Args:
        flow: FlowSpec 实例（已解析）
        user_id: 用户 ID（per-user 隔离）
        run_id: 本次运行的 run_id（写 jsonl 文件名 + 子进程 node-run-id）
        inputs: 用户提供的输入（run 前 validate_inputs 校验/补全）
        workspace_root: workspace 根（传给 worker_node 子进程）
        on_event: 可选回调 (event_dict) -> None，用于写 jsonl / 转发前端。
            回调异常被吞掉（不影响 flow 执行）。
    """

    def __init__(
        self,
        flow: Any,  # FlowSpec（parser 提供，此处用 Any 避免硬依赖）
        user_id: str,
        run_id: str,
        inputs: dict,
        workspace_root: str = "",
        on_event: Callable[[dict], None] | None = None,
        suspend_ask: bool = False,
    ):
        self.flow = flow
        self.user_id = user_id
        self.run_id = run_id
        self.inputs = inputs or {}
        self.workspace_root = workspace_root
        self._on_event = on_event
        # P2-22：True 时顶层 ask_user 等待走挂起（抛 FlowSuspended，由
        # FlowRunner 登记看护并在审批后 resume），不再阻塞轮询占死池线程；
        # False（默认，直接构造/嵌套场景）保持旧的阻塞轮询语义。
        self._suspend_ask = suspend_ask

    # ---------- 事件回调 ----------
    def _emit(self, event: dict) -> None:
        """安全触发 on_event 回调（异常吞掉）。"""
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:
            logger.debug("on_event 回调异常（已忽略）", exc_info=True)

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    # ════════════════════════════════════════════════════════════
    # 主入口
    # ════════════════════════════════════════════════════════════
    def run(self) -> dict:
        """跑完整 flow。

        Returns:
            {"status": "completed"|"failed", "returns": {...}, "node_count": N,
             "run_id": ..., "flow_name": ...}

        流程：
        1. validate_inputs → 完整 inputs dict
        2. context = {"inputs": inputs, "steps": {}}
        3. 顺序遍历 flow.steps，逐个 _run_step，存 context["steps"][id]
        4. 任一 step status=error（非 skipped）→ flow failed
        5. 渲染 returns（用最终 context 模板插值）

        P2-22：suspend_ask=True 且顶层 ask_user 需要等待时，写好审批文件、
        发出事件后抛 FlowSuspended（不阻塞）——由 FlowRunner 登记挂起并在
        审批文件 answered/cancelled（或超时）后调 resume() 续跑。
        """
        self._emit({
            "type": "flow_start",
            "run_id": self.run_id,
            "flow_name": getattr(self.flow, "name", ""),
            "ts": self._now(),
        })

        # 1. 校验 + 补全 inputs
        try:
            full_inputs = self.flow.validate_inputs(self.inputs)
        except Exception as e:
            logger.exception(f"validate_inputs 失败: {e}")
            self._emit({
                "type": "flow_end", "run_id": self.run_id,
                "status": "failed", "error": f"validate_inputs: {e}",
                "ts": self._now(),
            })
            return {
                "status": "failed",
                "returns": {},
                "node_count": 0,
                "run_id": self.run_id,
                "flow_name": getattr(self.flow, "name", ""),
                "error": f"validate_inputs: {e}",
            }

        # 2. context
        context: dict = {"inputs": full_inputs, "steps": {}}

        # 3. 顺序跑各 step（挂起/续跑共用同一循环体）
        return self._run_loop(0, context)

    def _run_loop(self, start: int, context: dict) -> dict:
        """从顶层 steps[start] 起顺序执行到 flow 结束（resume 复用同一入口）。"""
        steps = list(getattr(self.flow, "steps", []) or [])
        flow_status = "completed"
        i = start
        while i < len(steps):
            step = steps[i]
            # P2-22：顶层 ask_user → 挂起（等待移出本线程），不占池线程。
            # if_cond 为 false 的 ask_user 不挂起，交给 _run_step 按 skipped 处理。
            if (
                self._suspend_ask
                and getattr(step, "ask_user", None) is not None
                and (
                    not getattr(step, "if_cond", None)
                    or self._eval_if(step.if_cond, self._render_context(context))
                )
            ):
                raise FlowSuspended(self._suspend_on_ask(step, i, context))
            result = self._run_step(step, context)
            # 存 NodeResult 对象（模板访问时 to_dict）+ dict 快照
            context["steps"][step.id] = result
            if result.status == "error":
                flow_status = "failed"
                # 任一 step 失败 → 整个 flow failed（本任务不支持容错标记）
                logger.error(
                    f"flow step 失败，中止: step={step.id} err={result.error}"
                )
                break
            # skipped 不算失败（条件跳过 / askUser 占位都允许继续）
            i += 1
        return self._finish(context, flow_status, len(steps))

    def resume(self, susp: SuspendedAsk, on_event: Callable[[dict], None] | None = None) -> dict:
        """审批就绪（answered / cancelled / 超时）后从挂起点继续（FlowRunner 调）。

        先把 ask step 的结果按看护终态落进 context，再从其后 step 继续到
        flow 结束（returns 渲染 + flow_end 由 _finish 统一处理）。
        """
        if on_event is not None:
            self._on_event = on_event  # 挂起期间旧句柄已关，续跑换新句柄
        node_result = self._ask_result_from_watch(susp)
        self._emit({
            "type": "node_end",
            "run_id": self.run_id, "node_id": susp.node_id,
            "status": node_result.status, "ts": self._now(),
        })
        self._emit({
            "type": "node_result", "run_id": self.run_id,
            "node_id": susp.node_id, "status": node_result.status,
            "result": node_result.to_dict(), "ts": self._now(),
        })
        susp.context["steps"][susp.node_id] = node_result
        if node_result.status == "error":
            # ask 失败（取消/超时无 default）→ 与 _run_loop 的 error 语义一致：
            # 剩余 step 不跑，flow failed
            logger.error(
                f"flow step 失败，中止: step={susp.node_id} err={node_result.error}"
            )
            steps = list(getattr(self.flow, "steps", []) or [])
            return self._finish(susp.context, "failed", len(steps))
        return self._run_loop(susp.step_index, susp.context)

    def _finish(self, context: dict, flow_status: str, node_count: int) -> dict:
        """渲染 returns + 发 flow_end + 构造 run 结果（run/resume 共用收尾）。"""
        returns_raw = getattr(self.flow, "returns", {}) or {}
        returns: dict = {}
        if isinstance(returns_raw, dict):
            for k, v in returns_raw.items():
                try:
                    returns[k] = render(v, self._render_context(context))
                except Exception:
                    returns[k] = ""
        else:
            returns = {"value": render(str(returns_raw), self._render_context(context))}

        self._emit({
            "type": "flow_end",
            "run_id": self.run_id,
            "flow_name": getattr(self.flow, "name", ""),
            "status": flow_status,
            "node_count": node_count,
            "returns": returns,  # 附最终 returns，让前端/日志能看到产出
            "ts": self._now(),
        })

        return {
            "status": flow_status,
            "returns": returns,
            "node_count": node_count,
            "run_id": self.run_id,
            "flow_name": getattr(self.flow, "name", ""),
        }

    def _render_context(self, context: dict) -> dict:
        """构造模板可访问的 context 快照（steps 转 dict）。"""
        steps_snapshot = {}
        for sid, res in context.get("steps", {}).items():
            steps_snapshot[sid] = (
                res.to_dict() if isinstance(res, NodeResult) else res
            )
        return {"inputs": context.get("inputs", {}), "steps": steps_snapshot}

    # ════════════════════════════════════════════════════════════
    # 节点分发
    # ════════════════════════════════════════════════════════════
    def _run_step(self, step: Any, context: dict) -> NodeResult:
        """按节点类型分发。支持 if_cond 跳过。"""
        # if 条件求值（false → skipped，不报错）
        if getattr(step, "if_cond", None):
            if not self._eval_if(step.if_cond, self._render_context(context)):
                res = NodeResult(
                    node_id=step.id, status="skipped",
                    error=f"if_cond 为 false: {step.if_cond}",
                    ts=self._now(),
                )
                self._emit({
                    "type": "node_result", "run_id": self.run_id,
                    "node_id": step.id, "status": "skipped",
                    "ts": self._now(),
                })
                return res

        self._emit({
            "type": "node_start",
            "run_id": self.run_id, "node_id": step.id,
            "node_type": self._node_type(step),
            "ts": self._now(),
        })

        try:
            if getattr(step, "worker", None):
                result = self._run_worker(step, context)
            elif getattr(step, "parallel", None) is not None:
                result = self._run_parallel(step, context)
            elif getattr(step, "pipeline", None) is not None:
                result = self._run_pipeline(step, context)
            elif getattr(step, "ask_user", None):
                result = self._run_ask_user(step, context)
            elif getattr(step, "action", None):
                result = self._run_action(step, context)
            else:
                result = NodeResult(
                    node_id=step.id, status="error",
                    error=f"未知节点类型（无 worker/parallel/pipeline/ask_user/action）",
                    ts=self._now(),
                )
        except Exception as e:
            logger.exception(f"_run_step 异常: step={step.id}")
            result = NodeResult(
                node_id=step.id, status="error", error=str(e), ts=self._now(),
            )

        self._emit({
            "type": "node_end",
            "run_id": self.run_id, "node_id": step.id,
            "status": result.status, "ts": self._now(),
        })
        self._emit({
            "type": "node_result", "run_id": self.run_id,
            "node_id": step.id, "status": result.status,
            "result": result.to_dict(), "ts": self._now(),
        })
        return result

    @staticmethod
    def _node_type(step: Any) -> str:
        if getattr(step, "worker", None):
            return "worker"
        if getattr(step, "parallel", None) is not None:
            return "parallel"
        if getattr(step, "pipeline", None) is not None:
            return "pipeline"
        if getattr(step, "ask_user", None):
            return "ask_user"
        if getattr(step, "action", None):
            return "action"
        return "unknown"

    # ════════════════════════════════════════════════════════════
    # worker 节点：fork worker_node 子进程
    # ════════════════════════════════════════════════════════════
    def _run_worker(self, step: Any, context: dict) -> NodeResult:
        """fork worker_node 子进程跑一个 waker 任务。

        命令：sys.executable -m src.wakerflow.worker_node
              --user-id <uid> --waker-name <step.worker>
              --task-stdin（任务文本 render(step.task, context) 经 stdin 传入）
              --node-run-id <run_id>/<step.id>
              --workspace-root <ws>

        读 stdout NDJSON：
          - ready：忽略（仅表示子进程启动）
          - node_event：透传给 on_event
          - result：取 status + content 作为最终结果
          - log：忽略
        超时：WAKER_NODE_TIMEOUT（默认 600s）
        Windows 加 CREATE_NO_WINDOW
        """
        rendered_task = render(step.task or "", self._render_context(context))
        node_run_id = f"{self.run_id}/{step.id}"

        cmd = [
            sys.executable, "-m", "src.wakerflow.worker_node",
            "--user-id", self.user_id,
            "--waker-name", step.worker,
            # P2-20：任务文本经 stdin 传给子进程（--task-stdin），不再走
            # argv——{{steps.x.result}} 渲染链可达数万字符，Windows 的
            # 32k 命令行上限会让 spawn 直接 WinError 206。
            "--task-stdin",
            "--node-run-id", node_run_id,
            "--workspace-root", self.workspace_root,
        ]

        creationflags = (
            subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )

        self._emit({
            "type": "worker_spawn",
            "run_id": self.run_id, "node_id": step.id,
            "waker": step.worker, "node_run_id": node_run_id,
            "ts": self._now(),
        })

        proc = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # stderr 不能用 PIPE 不读——子进程日志写满 PIPE 缓冲区（~64KB）
                # 会死锁整个子进程（三方库 warning + logging 输出很多）。
                # worker_node 的关键信息已通过 stdout NDJSON 传达，stderr 丢弃即可。
                # 调试时若需看 stderr，改用 DEVNULL → 临时文件，或开线程消费 PIPE。
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            # 保留任务规模等可读信息（即便 spawn 因超长等原因失败也能定位）
            return NodeResult(
                node_id=step.id, status="error",
                error=f"spawn worker_node 失败: {e}（task {len(rendered_task)} 字符）",
                ts=self._now(),
            )

        # 写任务文本后立即关闭 stdin。子进程启动即整读 stdin，之后才输出
        # NDJSON——单次 write 即使超过管道缓冲区也会被子进程及时消费，无死锁。
        try:
            proc.stdin.write(rendered_task)
            proc.stdin.close()
        except Exception as e:
            if proc.poll() is None:
                proc.kill()
            return NodeResult(
                node_id=step.id, status="error",
                error=f"任务文本写入 stdin 失败: {e}（task {len(rendered_task)} 字符）",
                ts=self._now(),
            )

        final_status = "error"
        final_content = ""
        final_error = ""
        try:
            # 逐行读 stdout NDJSON。P2-3：读行放后台泵线程 + 队列，主循环带
            # WAKER_NODE_TIMEOUT 总截止——旧写法 for line in stdout 无界阻塞，
            # 卡死的子进程会永久占住 FlowRunner 池线程（超时分支不可达）。
            import queue as _q
            assert proc.stdout is not None
            line_q: "_q.Queue[str | None]" = _q.Queue()

            def _pump() -> None:
                try:
                    for raw in proc.stdout:
                        line_q.put(raw)
                except Exception:
                    pass
                finally:
                    line_q.put(None)  # EOF/读挂哨兵

            threading.Thread(target=_pump, daemon=True, name="wnode-pump").start()
            _node_budget = waker_node_timeout()
            _deadline = time.monotonic() + _node_budget
            while True:
                _remain = _deadline - time.monotonic()
                if _remain <= 0:
                    raise subprocess.TimeoutExpired(cmd=str(step.id), timeout=_node_budget)
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
                    self._emit({
                        "type": "worker_ready", "run_id": self.run_id,
                        "node_id": step.id, "waker": ev.get("waker"),
                        "ts": self._now(),
                    })
                elif etype == "node_event":
                    # 透传 agent 事件给 on_event（前端可实时显示）
                    self._emit({
                        "type": "worker_event", "run_id": self.run_id,
                        "node_id": step.id, "event": ev.get("event", {}),
                        "ts": self._now(),
                    })
                elif etype == "result":
                    final_status = ev.get("status", "error")
                    final_content = ev.get("content", "") or ""
                    if final_status == "error" and not final_content:
                        final_content = ev.get("message", "") or ""
                elif etype == "log":
                    logger.debug(
                        f"worker_node log [{step.worker}]: {ev.get('message')}"
                    )
                # 未知事件忽略

            # 等 stdout 读完后收尾（子进程应在发完 result 后 exit）
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass

            if final_status != "ok" and not final_content:
                # stdout 没收到 result 事件：stderr 已 DEVNULL 丢弃，无法回读。
                # worker_node 的 result 事件本应包含 error message；若连 result 都没有，
                # 通常是子进程崩溃或被 timeout 杀。给通用错误信息。
                final_error = "worker_node 未返回 result 事件（子进程崩溃或超时）"
                final_content = final_error

        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            return NodeResult(
                node_id=step.id, status="error",
                error=f"worker_node 超时（>{_node_budget}s）",
                ts=self._now(),
            )
        except Exception as e:
            logger.exception(f"_run_worker 异常: step={step.id}")
            if proc is not None and proc.poll() is None:
                proc.kill()
            return NodeResult(
                node_id=step.id, status="error", error=str(e), ts=self._now(),
            )

        nr = NodeResult(
            node_id=step.id, status=final_status,
            result=final_content, error=final_error, ts=self._now(),
        )
        if final_status == "error" and not nr.error:
            nr.error = final_content
        return nr

    # ════════════════════════════════════════════════════════════
    # parallel 节点：ThreadPoolExecutor 并发
    # ════════════════════════════════════════════════════════════
    def _run_parallel(self, step: Any, context: dict) -> NodeResult:
        """并发跑各子 step，sub_results 汇总。

        各子 step 共享同一 context 快照（不互相依赖；上游 result 不会喂下游——
        这是 parallel 与 pipeline 的关键区别）。
        任一子 step error → parallel 整体 error（其余仍跑完不中断）。
        """
        sub_steps = list(step.parallel or [])
        if not sub_steps:
            return NodeResult(
                node_id=step.id, status="skipped",
                error="parallel 节点无子 step", ts=self._now(),
            )

        # 各子 step 共享同一 context 快照（避免并发写同一 dict）
        ctx_snapshot = self._render_context(context)
        sub_context = {
            "inputs": ctx_snapshot["inputs"],
            "steps": dict(ctx_snapshot["steps"]),
        }

        sub_results: dict = {}
        # 线程安全的 node_result emit 用锁串行化（避免事件交错）
        emit_lock = threading.Lock()
        original_emit = self._emit

        def _thread_safe_emit(ev: dict) -> None:
            with emit_lock:
                original_emit(ev)

        self._emit = _thread_safe_emit  # type: ignore
        try:
            with ThreadPoolExecutor(
                max_workers=min(len(sub_steps), 8),
                thread_name_prefix=f"wf-parallel-{step.id}",
            ) as ex:
                future_to_step = {
                    ex.submit(self._run_step, ss, sub_context): ss
                    for ss in sub_steps
                }
                for fut in as_completed(future_to_step):
                    ss = future_to_step[fut]
                    try:
                        sub_results[ss.id] = fut.result()
                    except Exception as e:
                        sub_results[ss.id] = NodeResult(
                            node_id=ss.id, status="error",
                            error=f"子 step 异常: {e}", ts=self._now(),
                        )
        finally:
            self._emit = original_emit  # type: ignore

        # 汇总状态：任一 error → error；全 skipped → skipped；否则 ok
        statuses = [r.status for r in sub_results.values()]
        if any(s == "error" for s in statuses):
            agg_status = "error"
            agg_error = "; ".join(
                f"{sid}: {r.error}" for sid, r in sub_results.items()
                if r.status == "error"
            )
        elif statuses and all(s == "skipped" for s in statuses):
            agg_status = "skipped"
            agg_error = "所有子 step 被跳过"
        else:
            agg_status = "ok"
            agg_error = ""

        # 子节点 id 全局唯一（parser 保证），平铺到主 context["steps"]，
        # 让后续节点能直接用 {{steps.<子id>.result}} 引用（而非 {{steps.<父id>.<子id>.result}}）。
        # 与 pipeline 行为一致（pipeline 在 _run_pipeline 里也做了同样的平铺）。
        for sid, r in sub_results.items():
            context["steps"][sid] = r

        return NodeResult(
            node_id=step.id, status=agg_status,
            sub_results=sub_results, error=agg_error, ts=self._now(),
        )

    # ════════════════════════════════════════════════════════════
    # pipeline 节点：串行，上游 result 喂下游
    # ════════════════════════════════════════════════════════════
    def _run_pipeline(self, step: Any, context: dict) -> NodeResult:
        """串行跑各子 step，上游 result 自动作为下游 context。

        实现：复用主 context["steps"] dict（pipeline 内子 step 的 id 也会写进去，
        下游子 step 模板可引用 {{ steps.<上游id>.result }}）。
        任一子 step error → 中止后续，pipeline 整体 error。
        """
        sub_steps = list(step.pipeline or [])
        if not sub_steps:
            return NodeResult(
                node_id=step.id, status="skipped",
                error="pipeline 节点无子 step", ts=self._now(),
            )

        # pipeline 子 step 写入主 context["steps"]（id 全局唯一，parser 保证）。
        # 同时写嵌套层 steps.<父id>.<子id>，让外部模板可引用 {{steps.<父id>.<子id>.result}}
        sub_results: dict = {}
        pipeline_status = "ok"
        pipeline_error = ""
        for ss in sub_steps:
            r = self._run_step(ss, context)
            sub_results[ss.id] = r
            context["steps"][ss.id] = r  # 平铺：喂给下游子 step（{{steps.research.result}}）
            if r.status == "error":
                pipeline_status = "error"
                pipeline_error = f"子 step {ss.id} 失败: {r.error}"
                break
            # skipped 允许继续（条件跳过）

        return NodeResult(
            node_id=step.id, status=pipeline_status,
            sub_results=sub_results, error=pipeline_error, ts=self._now(),
        )

    # ════════════════════════════════════════════════════════════
    # ask_user 节点：人工审批（落盘 + 轮询等待）
    # ════════════════════════════════════════════════════════════
    def _run_ask_user(self, step: Any, context: dict) -> NodeResult:
        """ask_user 节点：写 pending 审批文件 → 轮询等响应 → 取 answer。

        审批文件：<approvals_dir>/<run_id>.json
        - executor 写入：{run_id, node_id, question, options, status:"pending", ts}
        - approve op（WebUI 提交）改写为：{..., status:"answered", answer:<value>, answered_by, answered_ts}
        - executor 轮询读该文件，status=="answered" → 取 answer；超时 → 用 default

        超时后无 default → status=error（flow 失败）；有 default → answer=default 继续。
        """
        cfg = step.ask_user
        if cfg is None:
            return NodeResult(
                node_id=step.id, status="error",
                error="ask_user 节点无 ask_user 配置", ts=self._now(),
            )

        # 渲染问题（支持 {{}} 插值）
        question = render(getattr(cfg, "question", "") or "", self._render_context(context))
        options = getattr(cfg, "options", []) or []
        # 注意：timeout=0 是合法值（立即超时），不能用 `or` 兜底（0 or 86400 → 86400）
        _raw_timeout = getattr(cfg, "timeout", None)
        timeout = int(_raw_timeout) if _raw_timeout is not None else 86400
        default = getattr(cfg, "default", None)

        from src.wakerflow.store import FlowStore
        store = FlowStore(self.user_id, workspace_root=self.workspace_root)
        approval_path = store.approval_path(self.run_id)

        # 1. 写 pending 审批文件（原子写）
        try:
            self._write_pending_approval(approval_path, step.id, question, options)
        except Exception as e:
            return NodeResult(
                node_id=step.id, status="error",
                error=f"写审批文件失败: {e}", ts=self._now(),
            )

        # 2. 发审批事件（WebUI 可据此刷新审批区）
        self._emit({
            "type": "approval_required",
            "run_id": self.run_id,
            "node_id": step.id,
            "question": question,
            "options": options,
            "ts": self._now(),
        })
        logger.info(f"askUser 等待审批: {self.run_id}/{step.id}（超时 {timeout}s）")

        # 3. 轮询等响应（每 2s 读一次文件，直到 answered/cancelled 或超时）
        deadline = time.time() + max(0, timeout)
        poll_interval = 2.0
        while True:
            # 先检查响应
            try:
                if approval_path.exists():
                    data = json.loads(approval_path.read_text(encoding="utf-8"))
                    if data.get("status") == "answered":
                        answer = data.get("answer")
                        logger.info(f"审批已响应: {self.run_id}/{step.id} → {answer}")
                        # 响应已消费，删除 pending 文件
                        self._unlink_quiet(approval_path)
                        return NodeResult(
                            node_id=step.id, status="ok",
                            answer=answer, result=str(answer),
                            ts=self._now(),
                        )
                    if data.get("status") == "cancelled":
                        # P2-22：审批被取消 → 提前终止等待，flow 以失败收尾
                        logger.info(f"审批已取消: {self.run_id}/{step.id}")
                        self._unlink_quiet(approval_path)
                        return NodeResult(
                            node_id=step.id, status="error",
                            error="审批已取消（cancelled）", ts=self._now(),
                        )
            except Exception:
                logger.debug("轮询审批文件异常（继续）", exc_info=True)
            # 再判超时（timeout<=0 时这里直接 break，不会 sleep）
            if time.time() >= deadline:
                break
            time.sleep(poll_interval)

        # 4. 超时
        if default is not None:
            logger.info(f"审批超时，用 default: {self.run_id}/{step.id} → {default}")
            self._unlink_quiet(approval_path)
            return NodeResult(
                node_id=step.id, status="ok",
                answer=default, result=str(default),
                error=f"审批超时({timeout}s)，用 default", ts=self._now(),
            )

        # 无 default → 失败
        return NodeResult(
            node_id=step.id, status="error",
            error=f"审批超时({timeout}s)且无 default", ts=self._now(),
        )

    def _write_pending_approval(
        self, approval_path: Path, node_id: str, question: str, options: list,
    ) -> None:
        """原子写 pending 审批文件（阻塞/挂起两条 ask_user 路径共用）。"""
        import os as _os
        approval_data = {
            "run_id": self.run_id,
            "flow_name": getattr(self.flow, "name", ""),
            "node_id": node_id,
            "question": question,
            "options": options,
            "status": "pending",
            "created_ts": self._now(),
        }
        approval_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = approval_path.with_suffix(approval_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(approval_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _os.replace(tmp, approval_path)

    def _suspend_on_ask(self, step: Any, index: int, context: dict) -> SuspendedAsk:
        """顶层 ask_user 挂起准备（P2-22）：写审批文件 + 发事件后返回断点，
        本线程立即让出——不轮询、不占用 FlowRunner 池线程。"""
        cfg = step.ask_user
        question = render(getattr(cfg, "question", "") or "", self._render_context(context))
        options = getattr(cfg, "options", []) or []
        _raw_timeout = getattr(cfg, "timeout", None)
        timeout = int(_raw_timeout) if _raw_timeout is not None else 86400
        default = getattr(cfg, "default", None)

        from src.wakerflow.store import FlowStore
        store = FlowStore(self.user_id, workspace_root=self.workspace_root)
        approval_path = store.approval_path(self.run_id)

        self._emit({
            "type": "node_start",
            "run_id": self.run_id, "node_id": step.id,
            "node_type": "ask_user",
            "ts": self._now(),
        })
        self._write_pending_approval(approval_path, step.id, question, options)
        self._emit({
            "type": "approval_required",
            "run_id": self.run_id,
            "node_id": step.id,
            "question": question,
            "options": options,
            "ts": self._now(),
        })
        logger.info(f"askUser 挂起等待审批: {self.run_id}/{step.id}（超时 {timeout}s）")
        return SuspendedAsk(
            executor=self,
            run_id=self.run_id,
            user_id=self.user_id,
            flow_name=getattr(self.flow, "name", ""),
            node_id=step.id,
            step_index=index + 1,
            context=context,
            approval_path=approval_path,
            timeout=timeout,
            default=default,
        )

    def _ask_result_from_watch(self, susp: SuspendedAsk) -> NodeResult:
        """按看护终态构造 ask step 的 NodeResult（并消费/清理审批文件）。"""
        watch = susp.watch
        status = watch.status if watch is not None else "timeout"
        if status == "answered":
            answer = watch.answer
            logger.info(f"审批已响应: {self.run_id}/{susp.node_id} → {answer}")
            self._unlink_quiet(susp.approval_path)
            return NodeResult(
                node_id=susp.node_id, status="ok",
                answer=answer, result=str(answer), ts=self._now(),
            )
        if status == "cancelled":
            logger.info(f"审批已取消: {self.run_id}/{susp.node_id}")
            self._unlink_quiet(susp.approval_path)
            return NodeResult(
                node_id=susp.node_id, status="error",
                error="审批已取消（cancelled）", ts=self._now(),
            )
        # 超时：有 default 用 default（与阻塞路径语义一致），否则失败
        if susp.default is not None:
            logger.info(
                f"审批超时，用 default: {self.run_id}/{susp.node_id} → {susp.default}"
            )
            self._unlink_quiet(susp.approval_path)
            return NodeResult(
                node_id=susp.node_id, status="ok",
                answer=susp.default, result=str(susp.default),
                error=f"审批超时({susp.timeout}s)，用 default", ts=self._now(),
            )
        return NodeResult(
            node_id=susp.node_id, status="error",
            error=f"审批超时({susp.timeout}s)且无 default", ts=self._now(),
        )

    @staticmethod
    def _unlink_quiet(path: Path) -> None:
        """删审批文件，失败忽略（响应已消费，残留不影响语义）。"""
        try:
            path.unlink()
        except Exception:
            pass

    # ════════════════════════════════════════════════════════════
    # action 节点：HTTP 调用（urllib）
    # ════════════════════════════════════════════════════════════
    def _run_action(self, step: Any, context: dict) -> NodeResult:
        """HTTP 调用。渲染 url/body/headers，用 urllib.request（不引 requests）。

        返回 NodeResult(result=f"{status_code} {response_body[:500]}")。
        异常 / 非 2xx → error status。
        """
        cfg = step.action
        if cfg is None:
            return NodeResult(
                node_id=step.id, status="error",
                error="action 节点无 action 配置", ts=self._now(),
            )

        method = (cfg.method or "POST").upper()
        url = render(cfg.url or "", self._render_context(context))
        if not url:
            return NodeResult(
                node_id=step.id, status="error",
                error="action 节点 url 为空", ts=self._now(),
            )

        # headers
        headers: dict = {}
        raw_headers = cfg.headers or {}
        if isinstance(raw_headers, dict):
            for k, v in raw_headers.items():
                headers[k] = render(str(v), self._render_context(context))

        # body：dict/list 走 JSON 序列化，str 渲染，None 无 body
        body = cfg.body
        data_bytes: bytes | None = None
        if body is not None:
            if isinstance(body, str):
                rendered_body = render(body, self._render_context(context))
                data_bytes = rendered_body.encode("utf-8")
                headers.setdefault("Content-Type", "text/plain")
            else:
                # dict/list：JSON 序列化（先渲染每个 str 值）
                rendered_body = self._render_body_obj(body, context)
                data_bytes = json.dumps(
                    rendered_body, ensure_ascii=False, default=str
                ).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")

        # GET/HEAD 默认无 body
        if method in ("GET", "HEAD") and data_bytes is not None and not url.strip().endswith(("?", "&")):
            # 不强制清 body（某些 API 允许 GET+body），但通常不该有；保留兼容
            pass

        req = urllib.request.Request(
            url, data=data_bytes, method=method, headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status_code = resp.getcode()
                resp_body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            # HTTPError 也带响应体，作为 result 一部分（便于调试）
            try:
                resp_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                resp_body = ""
            return NodeResult(
                node_id=step.id, status="error",
                result=f"{e.code} {resp_body[:500]}",
                error=f"HTTP {e.code}: {e.reason}", ts=self._now(),
            )
        except Exception as e:
            return NodeResult(
                node_id=step.id, status="error",
                error=f"action 调用异常: {e}", ts=self._now(),
            )

        if 200 <= status_code < 300:
            return NodeResult(
                node_id=step.id, status="ok",
                result=f"{status_code} {resp_body[:500]}", ts=self._now(),
            )
        return NodeResult(
            node_id=step.id, status="error",
            result=f"{status_code} {resp_body[:500]}",
            error=f"HTTP 非 2xx: {status_code}", ts=self._now(),
        )

    def _render_body_obj(self, obj: Any, context: dict) -> Any:
        """递归渲染 body 中的 str 值（dict/list 不渲染结构）。"""
        if isinstance(obj, str):
            return render(obj, self._render_context(context))
        if isinstance(obj, dict):
            return {k: self._render_body_obj(v, context) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._render_body_obj(v, context) for v in obj]
        return obj

    # ════════════════════════════════════════════════════════════
    # if 条件求值
    # ════════════════════════════════════════════════════════════
    def _eval_if(self, if_cond: str, context: dict) -> bool:
        """求值 if 条件。

        格式：先 render(if_cond, context) 把 {{}} 插值，再 ast 安全求值。
        支持 "X == Y" / "X != Y" / "X in [a,b]" / 逻辑组合 / 比较链。
        求值失败默认 True（保守执行——条件坏时倾向执行而非跳过）。
        """
        if not if_cond:
            return True
        rendered = render(if_cond, context)
        rendered = rendered.strip()
        if not rendered:
            return True
        # 短路：渲染后可能直接是 "True"/"False"/"true"/"false"
        low = rendered.lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no", "", "none", "null"):
            return False

        try:
            tree = ast.parse(rendered, mode="eval")
            val = _safe_eval_node(tree, context)
            return bool(val)
        except Exception as e:
            logger.debug(
                f"_eval_if 求值失败，默认 True: cond={if_cond!r} "
                f"rendered={rendered!r} err={e}"
            )
            return True
