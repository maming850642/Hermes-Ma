"""IPC op 键位契约测试：web_fastapi（发送方）↔ worker（接收方）双向 AST 扫描。

协议背景见 web_fastapi/ipc_ops.py（键位的唯一登记处）。本测试用 ast 解析
双方源码，把"线上实际流转的键"对齐到 OP_PAYLOAD_KEYS[op]，双向断言：

1. 接收方读取 ⊆ 声明
   - 接收方源码 = web_fastapi/worker_process.py + worker_ops.py +
     worker_state.py（P2 三分：入口/分发 + op 实现 + 状态持久化；扫描时
     合并三者的模块级函数与字符串常量，跨文件 helper 链照常跟随）。
   - OP_HANDLERS 注册表的 op → 解析对应 handler 函数体（含经 cmd 直传的
     模块级 helper，如 _apply_llm_params_cmd）；
   - handle_command 各 `elif op == "X"` 分支 → 扫分支体内的直接读取 +
     cmd 直传的 _op_* helper。
   读取形态覆盖 ``cmd.get("k")`` / ``cmd["k"]``，以及唯一一处动态键
   （``for k in _LLM_PARAMS_KEYS`` 上的 ``cmd[k]`` / ``cmd.get(k)``，经
   模块级常量元组解析；常量单处定义在 worker_process.py，worker_ops 里
   经 ``wp._LLM_PARAMS_KEYS`` 延迟引用，两种形态都解析）。出现解析不了
   的动态键 → 明确失败而非静默放过（宁少勿假：确需豁免时在
   ALLOWED_DYNAMIC_READS 登记 + 注明原因）。

2. 发送方构造 ⊆ 声明
   扫描 web_fastapi/routers/*.py + worker_manager.py（+ src/waker/
   scheduler.py 的 waker_run 旧 IPC 路径）里 op 为字面量的调用点：
   ``WorkerProcess.send / send_stream / send_fire_and_forget``、
   ``make_request``，以及 op 转发器（_start_relay / relay_stream /
   _queue_pending_spawning）与隐式 op 包装（broadcast_llm_params）。
   键来源 = 字面量关键字名 + ``**名字`` 就近解析到同函数内的 dict 字面量
   （含 IfExp / 再嵌套 **，深度受限）；send/send_stream 自身消耗的
   timeout / lock_wait 是方法参数、不下发，剔除。

3. 注册表完整性：worker 侧 op 全集（handle_command 分支 ∪ OP_HANDLERS）
   == OP_PAYLOAD_KEYS —— 新增 op 不登记契约测试直接红（维护约定见
   ipc_ops.py docstring）。

失败信息统一带「哪一侧 + op + 漂移键 + 文件:行」，可直接定位改哪边。
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from web_fastapi.ipc_ops import OP_PAYLOAD_KEYS

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# 扫描范围与调用形态登记（改传输层/转发层时同步这里）
# ---------------------------------------------------------------------------

# P2 三分后的 worker 侧源码全集：入口/分发（worker_process.py）+ op 实现
# （worker_ops.py）+ 状态与持久化（worker_state.py）。接收方扫描合并三者
# 的顶层函数与字符串常量；新增 worker 子模块必须加进这里（下面的
# test_worker_split_modules_all_scanned 会盯住文件集不缺不漏）。
WORKER_SOURCES = [
    REPO_ROOT / "web_fastapi" / "worker_process.py",
    REPO_ROOT / "web_fastapi" / "worker_ops.py",
    REPO_ROOT / "web_fastapi" / "worker_state.py",
]

# 发送方：web_fastapi 全部路由 + worker_manager（传输层 + 广播实现）+
# src/waker/scheduler.py（waker_run 旧 IPC 路径的唯一发送方）
SENDER_SOURCES = sorted((REPO_ROOT / "web_fastapi" / "routers").glob("*.py")) + [
    REPO_ROOT / "web_fastapi" / "worker_manager.py",
    REPO_ROOT / "src" / "waker" / "scheduler.py",
]

# op 为显式字面量参数的调用形态：名字 → (op 在位置参数里的下标,
# 该调用自身消耗、不下发给 worker 的关键字)。send* 的 timeout/lock_wait
# 是 WorkerProcess 方法参数（make_request 前被吃掉），必须剔除。
_OP_ARG_CALLABLES: dict[str, tuple[int, set[str]]] = {
    "send": (0, {"timeout", "lock_wait"}),
    "send_stream": (0, {"timeout"}),
    "send_fire_and_forget": (0, set()),
    "make_request": (1, set()),
    # op 转发器：op 字面量在调用点，payload 经 **kwargs 转发
    "_start_relay": (2, set()),            # (request, worker, op, sid, **kw)
    "relay_stream": (1, set()),            # (worker, op, **kw)
    "_queue_pending_spawning": (0, set()),  # (op, payload_dict) 第 2 参即载荷
}

# op 隐含在 worker_manager 实现里的包装方法：名字 → op。
# broadcast_llm_params 会把 clear_model_overrides 并进下发载荷
# （ff_kwargs = {**payload, "clear_model_overrides": ...}），故调用点的
# 关键字名就是线上键。其余广播（remember_prefs / remember_permission_mode /
# broadcast_settings_updates）的载荷关键字在 manager 的 send 调用点已是
# 字面量，无需在此登记。
_IMPLICIT_OP_CALLABLES: dict[str, str] = {
    "broadcast_llm_params": "llm_params_set",
}

# op 非字面量的转发点（传输层定义处，op 由上层字面量调用点传入——那些
# 调用点已被扫描）。新增动态转发必须在此登记并说明，否则测试失败。
_KNOWN_DYNAMIC_OP_SITES: set[tuple[str, str]] = {
    ("chat.py", "_pump_worker_stream"),   # op 由 _start_relay 调用点传入
    ("chat.py", "relay_stream"),          # 兼容保留的同步转发器（调用点在测试）
    ("worker_manager.py", "send"),        # 传输层：make_request(req_id, op, **kw)
    ("worker_manager.py", "send_stream"),
    ("worker_manager.py", "send_fire_and_forget"),
    ("worker_manager.py", "_replay_pending_broadcasts"),  # 补发挂队广播
}

# 接收方解析不了、且确认豁免的动态读取：(op, 变量名, 说明)。宁少勿假：
# 当前为空——worker 侧唯一的动态键（_LLM_PARAMS_KEYS）已被常量解析覆盖。
_ALLOWED_DYNAMIC_READS: dict[str, str] = {}

# 发送方 **名字 解析不到同函数 dict 字面量时的豁免：(文件名, op, 变量名)。
# 宁少勿假：当前为空——出现即说明有人用扫描跟不动的方式构造载荷，
# 要么改字面量，要么在此登记并注明原因。
_ALLOWED_UNRESOLVED_SPLATS: set[tuple[str, str, str]] = set()

ENVELOPE_KEYS = {"id", "op"}


# ---------------------------------------------------------------------------
# 公共：解析缓存与小工具
# ---------------------------------------------------------------------------
_CACHE: dict[str, Any] = {}


def _parse(path: Path) -> ast.Module:
    key = f"ast:{path}"
    if key not in _CACHE:
        _CACHE[key] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _CACHE[key]


def _module_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {
        n.name: n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _module_str_seq_constants(tree: ast.Module) -> dict[str, tuple[str, ...]]:
    """模块级「字符串元组/列表」常量（如 _LLM_PARAMS_KEYS），供动态键解析。"""
    out: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, (ast.Tuple, ast.List)):
            continue
        elts = node.value.elts
        values = [e.value for e in elts
                  if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if values and len(values) == len(elts):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = tuple(values)
    return out


# ---------------------------------------------------------------------------
# 接收方扫描：worker_process.py + worker_ops.py + worker_state.py（合并命名空间）
# ---------------------------------------------------------------------------

# worker 子模块里对 worker_process 模块对象的惯用别名（函数内延迟
# import 仓库惯例）：``wp.<常量>`` 属性形式的模块常量引用照常解析。
_WP_MODULE_ALIASES = {"wp", "worker_process", "wp_mod"}


class _CmdKeyReader(ast.NodeVisitor):
    """收集一段 AST 里对 ``cmd`` 的键读取。keys: 键 → 首次出现行号。"""

    def __init__(self, seq_vars: dict[str, set[str]]):
        self.keys: dict[str, int] = {}
        self.dynamic: list[tuple[str, int]] = []  # 未解析的动态键 (描述, 行号)
        self._seq_vars = seq_vars

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if (isinstance(f, ast.Attribute) and f.attr == "get"
                and isinstance(f.value, ast.Name) and f.value.id == "cmd"
                and node.args):
            self._record(node.args[0], node.lineno)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if (isinstance(node.value, ast.Name) and node.value.id == "cmd"
                and isinstance(node.ctx, ast.Load)):
            self._record(node.slice, node.lineno)
        self.generic_visit(node)

    def _record(self, key_expr: ast.expr, lineno: int) -> None:
        if isinstance(key_expr, ast.Constant) and isinstance(key_expr.value, str):
            self.keys.setdefault(key_expr.value, lineno)
        elif isinstance(key_expr, ast.Name) and key_expr.id in self._seq_vars:
            for k in sorted(self._seq_vars[key_expr.id]):
                self.keys.setdefault(k, lineno)
        else:
            desc = (key_expr.id if isinstance(key_expr, ast.Name)
                    else type(key_expr).__name__)
            self.dynamic.append((desc, lineno))


def _worker_trees() -> list[tuple[Path, ast.Module]]:
    """解析全部 worker 侧源文件（P2 三分后 ≥3 个，见 WORKER_SOURCES）。"""
    out = []
    for path in WORKER_SOURCES:
        out.append((path, _parse(path)))
    return out


def _worker_functions() -> dict[str, ast.FunctionDef]:
    """三个 worker 文件的顶层函数合并命名空间（键 → 定义）。

    跨文件重名会静默遮蔽、让 helper 链跟错文件——直接红（合并扫描的
    前提护栏）。
    """
    funcs: dict[str, ast.FunctionDef] = {}
    for path, tree in _worker_trees():
        for name, fn in _module_functions(tree).items():
            if name in funcs:
                raise AssertionError(
                    f"worker 模块函数跨文件重名: {name}（{path.name} 与先前的定义）"
                    "——合并扫描无法消歧，请改名")
            funcs[name] = fn
    return funcs


def _worker_seq_constants() -> dict[str, tuple[str, ...]]:
    """三个 worker 文件的模块级字符串元组常量合并表（如 _LLM_PARAMS_KEYS）。"""
    out: dict[str, tuple[str, ...]] = {}
    for path, tree in _worker_trees():
        for name, values in _module_str_seq_constants(tree).items():
            if name in out and out[name] != values:
                raise AssertionError(
                    f"worker 模块字符串常量跨文件重复且不一致: {name} "
                    f"（{out[name]!r} vs {path.name} {values!r}）")
            out[name] = values
    return out


def _const_iter_keys(iter_expr: ast.expr,
                     module_seq: dict[str, tuple[str, ...]]) -> set[str] | None:
    """循环迭代对象 → 它可能是的模块级字符串常量键集；认不出返回 None。

    支持两种形态：裸名字 ``for k in _LLM_PARAMS_KEYS``（常量同模块）与
    别名属性 ``for k in wp._LLM_PARAMS_KEYS``（常量定义在 worker_process、
    子模块延迟引用——见 _WP_MODULE_ALIASES）。
    """
    if isinstance(iter_expr, ast.Name) and iter_expr.id in module_seq:
        return set(module_seq[iter_expr.id])
    if (isinstance(iter_expr, ast.Attribute) and isinstance(iter_expr.value, ast.Name)
            and iter_expr.value.id in _WP_MODULE_ALIASES
            and iter_expr.attr in module_seq):
        return set(module_seq[iter_expr.attr])
    return None


def _seq_vars_of(fn: ast.FunctionDef,
                 module_seq: dict[str, tuple[str, ...]]) -> dict[str, set[str]]:
    """函数内「for k in <模块字符串常量>」型循环变量 → 常量值集（近似全函数
    作用域；对 ⊆ 断言只会并入该常量本身，安全方向）。"""
    out: dict[str, set[str]] = {}
    for node in ast.walk(fn):
        pairs: list[tuple[ast.expr, ast.expr]] = []
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp,
                             ast.DictComp)):
            pairs = [(g.target, g.iter) for g in node.generators]
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            pairs = [(node.target, node.iter)]
        for target, it in pairs:
            if isinstance(target, ast.Name):
                keys = _const_iter_keys(it, module_seq)
                if keys is not None:
                    out.setdefault(target.id, set()).update(keys)
    return out


def _merge_reads(dst: dict[str, int], src: dict[str, int]) -> None:
    for k, ln in src.items():
        dst.setdefault(k, ln)


def _scan_cmd_reads(stmts: list[ast.stmt], owner: ast.FunctionDef,
                    funcs: dict[str, ast.FunctionDef],
                    module_seq: dict[str, tuple[str, ...]],
                    _seen: frozenset[str] = frozenset()) -> tuple[dict[str, int],
                                                                  list[tuple[str, int]]]:
    """扫一组语句（分支体 / 函数体）里对 cmd 的读取；cmd 直传的模块级
    函数调用跟着进去（helper 链，带 visited 防环）。"""
    reader = _CmdKeyReader(_seq_vars_of(owner, module_seq))
    for st in stmts:
        reader.visit(st)
    keys, dynamic = reader.keys, list(reader.dynamic)
    for node in _walk_stmts(stmts):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = node.func.id
        if (name in funcs and name not in _seen
                and any(isinstance(a, ast.Name) and a.id == "cmd"
                        for a in node.args)):
            k2, d2 = _scan_cmd_reads(funcs[name].body, funcs[name], funcs,
                                     module_seq, _seen | {name})
            _merge_reads(keys, k2)
            dynamic.extend(d2)
    return keys, dynamic


def _walk_stmts(stmts: list[ast.stmt]):
    for st in stmts:
        yield from ast.walk(st)


def _op_handlers_map(trees: list[ast.Module]) -> dict[str, str]:
    """解析 ``OP_HANDLERS: dict[str, Handler] = {"op": _op_xxx, ...}``。

    P2 三分后字面量定义在 worker_ops.py；跨全部 worker 文件恰好一份
    （多份 = 双实现入口漂移，直接红）。
    """
    found: dict[str, str] | None = None
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(target, ast.Name) and target.id == "OP_HANDLERS":
                value = node.value
                if isinstance(value, ast.Dict):
                    if found is not None:
                        raise AssertionError(
                            "OP_HANDLERS 注册表字面量在 worker 模块里定义了多份"
                            "（双实现入口会漂移）")
                    found = {
                        k.value: v.id
                        for k, v in zip(value.keys, value.values)
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                        and isinstance(v, ast.Name)
                    }
    if found is None:
        raise AssertionError(
            "worker 模块（worker_process.py / worker_ops.py / worker_state.py）"
            "里找不到 OP_HANDLERS 注册表字面量")
    return found


def _handle_command_branches(fn: ast.FunctionDef) -> dict[str, list[ast.stmt]]:
    """提取 handle_command 的 ``op == "X"`` 分支体（elif 链经 ast.walk 全覆盖）。"""
    branches: dict[str, list[ast.stmt]] = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                and test.left.id == "op" and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq) and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and isinstance(test.comparators[0].value, str)):
            branches.setdefault(test.comparators[0].value, node.body)
    return branches


def _worker_receiver_scan() -> dict[str, dict[str, Any]]:
    """op → {"reads": {键: 行号}, "dynamic": [(描述, 行号)]}（worker 侧）。"""
    if "receiver" in _CACHE:
        return _CACHE["receiver"]
    trees = [tree for _, tree in _worker_trees()]
    funcs = _worker_functions()
    module_seq = _worker_seq_constants()
    handle_command = funcs["handle_command"]
    seen = frozenset({"handle_command"})

    reads: dict[str, dict[str, int]] = {}
    dynamics: dict[str, list[tuple[str, int]]] = {}
    # 信封键：handle_command 顶部对每条命令读 id/op（body 前两条赋值）
    env_keys, _ = _scan_cmd_reads(handle_command.body[:2], handle_command,
                                  funcs, module_seq, seen)
    for op in OP_PAYLOAD_KEYS:
        reads[op] = dict(env_keys)
        dynamics[op] = []
    # 各 op == "X" 分支：分支体直读 + cmd 直传 helper
    for op, body in _handle_command_branches(handle_command).items():
        if op not in reads:  # 分支里的 op 未在契约登记 → 由完整性测试报错
            reads[op], dynamics[op] = {}, []
        k, d = _scan_cmd_reads(body, handle_command, funcs, module_seq, seen)
        _merge_reads(reads[op], k)
        dynamics[op].extend(d)
    # OP_HANDLERS 注册表 op：handler 函数体（helper 链同上）
    for op, handler_name in _op_handlers_map(trees).items():
        if op not in reads:
            reads[op], dynamics[op] = {}, []
        handler = funcs.get(handler_name)
        assert handler is not None, f"OP_HANDLERS 指向不存在的函数 {handler_name}"
        k, d = _scan_cmd_reads(handler.body, handler, funcs, module_seq,
                               frozenset({handler_name}))
        _merge_reads(reads[op], k)
        dynamics[op].extend(d)
    result = {op: {"reads": reads[op], "dynamic": dynamics[op]}
              for op in reads}
    _CACHE["receiver"] = result
    return result


# ---------------------------------------------------------------------------
# 发送方扫描：web_fastapi/routers/*.py + worker_manager.py + waker/scheduler.py
# ---------------------------------------------------------------------------
def _splat_keys(expr: ast.expr, var_map: dict[str, list[ast.expr]],
                lineno: int, depth: int = 0) -> tuple[set[str],
                                                      list[tuple[str, int]]]:
    """解析 ``**名字`` / 名字引用 → (字面量键集, 解析不到的 (名字, 行号))。

    就近解析：只认同函数内对该名字的 dict 字面量赋值（含 IfExp 两支与
    再嵌套 **名字，深度受限）。解析不到 ≠ 没有键 → 报给豁免清单断言，
    绝不静默放过。
    """
    keys: set[str] = set()
    unresolved: list[tuple[str, int]] = []
    if depth > 3:
        return keys, [(f"<depth-limit:{type(expr).__name__}>", lineno)]
    if isinstance(expr, ast.Dict):
        for k, v in zip(expr.keys, expr.values):
            if k is None:  # **名字
                if isinstance(v, ast.Name):
                    if v.id in var_map:
                        for rhs in var_map[v.id]:
                            k2, u2 = _splat_keys(rhs, var_map, v.lineno, depth + 1)
                            keys |= k2
                            unresolved.extend(u2)
                    else:
                        unresolved.append((v.id, v.lineno))
                else:
                    unresolved.append(("<complex-splat>", v.lineno))
            elif isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
    elif isinstance(expr, ast.IfExp):
        for branch in (expr.body, expr.orelse):
            k2, u2 = _splat_keys(branch, var_map, lineno, depth + 1)
            keys |= k2
            unresolved.extend(u2)
    elif isinstance(expr, ast.Name):
        if expr.id in var_map:
            for rhs in var_map[expr.id]:
                k2, u2 = _splat_keys(rhs, var_map, lineno, depth + 1)
                keys |= k2
                unresolved.extend(u2)
        else:
            unresolved.append((expr.id, lineno))
    return keys, unresolved


def _sender_scan() -> dict[str, Any]:
    """扫描发送方源码。

    返回 {"keys": op → {键: (文件, 行号)}, "dynamic_sites": {(文件, 函数)},
          "unresolved": [(文件, op, 名字, 行号)], "unknown_ops": [...]}
    """
    if "sender" in _CACHE:
        return _CACHE["sender"]
    all_keys: dict[str, dict[str, tuple[str, int]]] = {}
    dynamic_sites: set[tuple[str, str]] = set()
    unresolved: list[tuple[str, str, str, int]] = []
    unknown_ops: list[tuple[str, str, int]] = []

    def _record(op: str, key: str, where: tuple[str, int]) -> None:
        all_keys.setdefault(op, {})
        if key not in all_keys[op]:
            all_keys[op][key] = where

    for path in SENDER_SOURCES:
        tree = _parse(path)
        fname_short = path.name

        def scan_callable(owner: ast.FunctionDef | ast.Module, owner_name: str):
            var_map: dict[str, list[ast.expr]] = {}
            for node in ast.walk(owner):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = (node.targets if isinstance(node, ast.Assign)
                               else [node.target])
                    for t in targets:
                        if isinstance(t, ast.Name):
                            var_map.setdefault(t.id, []).append(node.value)
            for node in ast.walk(owner):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (func.attr if isinstance(func, ast.Attribute)
                        else func.id if isinstance(func, ast.Name) else None)
                if name is None:
                    continue

                if name in _OP_ARG_CALLABLES:
                    idx, own_kwargs = _OP_ARG_CALLABLES[name]
                    if len(node.args) <= idx:
                        continue
                    op_expr = node.args[idx]
                    if not (isinstance(op_expr, ast.Constant)
                            and isinstance(op_expr.value, str)):
                        # op 非字面量：转发点（op 由上层字面量调用点传入）
                        dynamic_sites.add((fname_short, owner_name))
                        continue
                    op = op_expr.value
                    where = (fname_short, node.lineno)
                    if op not in OP_PAYLOAD_KEYS:
                        unknown_ops.append((fname_short, op, node.lineno))
                        continue
                    for kw in node.keywords:
                        if kw.arg is None:
                            k2, u2 = _splat_keys(kw.value, var_map, node.lineno)
                            for key in k2:
                                _record(op, key, where)
                            for var, ln in u2:
                                unresolved.append((fname_short, op, var, ln))
                        elif kw.arg not in own_kwargs:
                            _record(op, kw.arg, where)
                    # _queue_pending_spawning(op, payload)：第 2 参即载荷
                    if name == "_queue_pending_spawning" and len(node.args) > 1:
                        k2, u2 = _splat_keys(node.args[1], var_map, node.lineno)
                        for key in k2:
                            _record(op, key, where)
                        for var, ln in u2:
                            unresolved.append((fname_short, op, var, ln))

                elif name in _IMPLICIT_OP_CALLABLES:
                    op = _IMPLICIT_OP_CALLABLES[name]
                    where = (fname_short, node.lineno)
                    if op not in OP_PAYLOAD_KEYS:
                        unknown_ops.append((fname_short, op, node.lineno))
                        continue
                    for kw in node.keywords:
                        if kw.arg is None:
                            k2, u2 = _splat_keys(kw.value, var_map, node.lineno)
                            for key in k2:
                                _record(op, key, where)
                            for var, ln in u2:
                                unresolved.append((fname_short, op, var, ln))
                        else:
                            # 包装方法的字面量关键字会被并入下发载荷
                            # （如 broadcast_llm_params 的 clear_model_overrides）
                            _record(op, kw.arg, where)

        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scan_callable(fn, fn.name)

    result = {"keys": all_keys, "dynamic_sites": dynamic_sites,
              "unresolved": unresolved, "unknown_ops": unknown_ops}
    _CACHE["sender"] = result
    return result


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

def test_worker_split_modules_all_scanned():
    """P2 三分护栏：worker 侧源文件全集必须被纳入接收方扫描。

    新增 worker 子模块（op/状态代码）不进 WORKER_SOURCES = 扫描盲区 →
    这里红，提示先登记再改代码。
    """
    paths = [path for path, _tree in _worker_trees()]
    assert {p.name for p in paths} == {
        "worker_process.py", "worker_ops.py", "worker_state.py",
    }, f"WORKER_SOURCES 与 worker 模块文件集不一致: {[str(p) for p in paths]}"
    for p in paths:
        assert p.exists(), f"worker 源文件缺失: {p}"
    # 合并命名空间护栏（跨文件重名/常量冲突）随扫一遍
    _worker_functions()
    _worker_seq_constants()
    # OP_HANDLERS 字面量在合并文件集里恰好一份且每个 handler 都可解析
    trees = [tree for _, tree in _worker_trees()]
    handlers = _op_handlers_map(trees)
    funcs = _worker_functions()
    missing = {op: name for op, name in handlers.items() if name not in funcs}
    assert not missing, f"OP_HANDLERS 指向不存在的函数: {missing}"


def test_worker_ops_all_registered():
    """注册表完整性：worker 侧 op 全集（分支 ∪ OP_HANDLERS）== 登记处。

    新增 op 不先登记 ipc_ops.py → 这条直接红（维护约定）。
    """
    trees = [tree for _, tree in _worker_trees()]
    branches = set(_handle_command_branches(_worker_functions()["handle_command"]))
    handlers = set(_op_handlers_map(trees))
    worker_ops = branches | handlers
    assert handlers <= branches | handlers  # 自洽（防上面提取逻辑回归）
    registered = set(OP_PAYLOAD_KEYS)
    assert worker_ops == registered, (
        "worker op 全集与 ipc_ops 登记处不一致。\n"
        f"  worker 多出（未登记，请先在 web_fastapi/ipc_ops.py 登记）: "
        f"{sorted(worker_ops - registered)}\n"
        f"  登记处多出（worker 已不存在，属过期条目请删除）: "
        f"{sorted(registered - worker_ops)}"
    )


def test_envelope_keys_declared_for_every_op():
    """信封键 id/op 必须出现在每个 op 的合法键集里（TypedDict 基类漏继承的护栏）。"""
    missing = {op: ENVELOPE_KEYS - keys for op, keys in OP_PAYLOAD_KEYS.items()
               if not ENVELOPE_KEYS <= keys}
    assert not missing, f"以下 op 的 TypedDict 未继承信封基类（缺 id/op）: {missing}"


@pytest.mark.parametrize("op", sorted(OP_PAYLOAD_KEYS))
def test_worker_reads_subset_of_declared(op):
    """接收方 ⊆ 声明：worker 模块对各 op 实际读取的键不得超出登记。"""
    scan = _worker_receiver_scan()[op]
    reads, declared = scan["reads"], OP_PAYLOAD_KEYS[op]
    drift = {k: ln for k, ln in reads.items() if k not in declared}
    assert not drift, (
        f"【接收方键位漂移】worker 模块（worker_process/worker_ops/"
        f"worker_state.py）读取了 ipc_ops.py 未为 "
        f"op={op!r} 登记的键: "
        + ", ".join(f"{k!r}（worker 模块 :{ln}）" for k, ln in sorted(drift.items()))
        + f"\n  已声明合法键: {sorted(declared)}"
        + "\n  → worker 新增读取先在 ipc_ops.py 对应 TypedDict 登记；若是误读请修 worker。"
    )
    unresolved = {desc: ln for desc, ln in scan["dynamic"]
                  if desc not in _ALLOWED_DYNAMIC_READS}
    assert not unresolved, (
        f"【接收方动态键无法静态解析】op={op!r}: "
        + ", ".join(f"{d!r}（worker_process.py:{ln}）" for d, ln in unresolved.items())
        + "\n  → 请让键可静态解析（字面量 / 模块级字符串常量元组），"
          "或在测试的 _ALLOWED_DYNAMIC_READS 登记 + 注明原因（宁少勿假）。"
    )


@pytest.mark.parametrize("op", sorted(OP_PAYLOAD_KEYS))
def test_sender_keys_subset_of_declared(op):
    """发送方 ⊆ 声明：routers / worker_manager 实际下发的字面量键不得超出登记。"""
    scan = _sender_scan()
    sent = scan["keys"].get(op, {})
    declared = OP_PAYLOAD_KEYS[op]
    drift = {k: where for k, where in sent.items() if k not in declared}
    assert not drift, (
        f"【发送方键位漂移】web_fastapi 侧向 op={op!r} 下发了 ipc_ops.py "
        f"未登记的键: "
        + ", ".join(f"{k!r}（{f}:{ln}）" for k, (f, ln) in sorted(drift.items()))
        + f"\n  已声明合法键: {sorted(declared)}"
        + "\n  → router 新增字段先在 ipc_ops.py 对应 TypedDict 登记；若 worker 需要读取"
          "请同步补 worker 侧（test_worker_reads_subset_of_declared 会盯住它）。"
    )


def test_sender_has_no_unregistered_ops():
    """发送方使用了登记处不存在的 op（拼错 / 新 op 忘登记）→ 立即暴露。"""
    scan = _sender_scan()
    assert not scan["unknown_ops"], (
        "【发送方使用了未登记的 op】"
        + ", ".join(f"{f}:{ln} op={op!r}" for f, op, ln in scan["unknown_ops"])
        + "\n  → 先在 web_fastapi/ipc_ops.py 登记（OP_PAYLOAD_TYPES + TypedDict），"
          "再确认 worker 侧有对应分支/注册表项。"
    )


def test_sender_scan_unresolved_splats_whitelisted():
    """**名字 解析不到同函数 dict 字面量的发送点必须在豁免清单里（带原因）。"""
    scan = _sender_scan()
    bad = [u for u in scan["unresolved"]
           if (u[0], u[1], u[2]) not in _ALLOWED_UNRESOLVED_SPLATS]
    assert not bad, (
        "【发送方载荷存在解析不了的 **展开】"
        + ", ".join(f"{f}:{ln} op={op!r} **{var}" for f, op, var, ln in bad)
        + "\n  → 改成字面量键 / 同函数 dict 字面量赋值，"
          "或在 _ALLOWED_UNRESOLVED_SPLATS 登记 (文件, op, 变量) 并注明原因。"
    )


def test_sender_dynamic_op_sites_whitelisted():
    """op 非字面量的转发点必须在已知清单里（防新增绕过扫描的转发路径）。"""
    scan = _sender_scan()
    unknown = scan["dynamic_sites"] - _KNOWN_DYNAMIC_OP_SITES
    assert not unknown, (
        "【发现未登记的动态 op 转发点】"
        + ", ".join(f"{f}:{fn}" for f, fn in sorted(unknown))
        + "\n  → 转发器必须由携带 op 字面量的调用点（已被扫描）触达；"
          "新增动态转发请在 _KNOWN_DYNAMIC_OP_SITES 登记 + 注明，并保证最终调用点可扫。"
    )
