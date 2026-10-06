"""
记忆数据模型。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import time
import uuid


def memory_version(m: Memory) -> float:
    """并发守卫的行版本号：updated_at 优先，从未更新过则用 created_at。

    replace_all(baseline=...) 的统一比较口径；调用方在快照时刻逐行计算。
    """
    return m.updated_at if m.updated_at is not None else m.created_at


def apply_baseline_guard(
    mems: list,
    current: list,
    baseline: dict | None,
):
    """replace_all 的 baseline 参数实现：行级"活写入者优先"。

    针对"快照 → 替换"长窗口（如聚合的 LLM 调用 ~20s）内的并发写：

      - id 不在 baseline 里 ⇒ 窗口内新增，一律保留；
      - id 在 baseline 里且现存行版本号 ≠ 基线值 ⇒ 窗口内被 UPDATE，
        当前活值胜出，mems 中指向该 id 的条目被压制；
      - id 在 baseline 里但现全量中已消失 ⇒ 窗口内被 DELETE，
        mems 中该 id 不复活。

    放在本模块而非 storage.base：consolidator 与两个存储实现都要用，
    而 storage ←→ memory 存在包初始化顺序耦合，叶子层定义可避免循环导入
    （models 是全仓最底层，不 import 任何业务包）。

    Args:
        mems: 拟写入的新全集（如聚合产物）。
        current: 替换执行时刻存储里的现全量。
        baseline: 快照时刻 {id: memory_version(行)}；None 直接透传。

    Returns:
        (过滤后应写入的 mems 子集, 需原样保留的幸存行)。
    """
    if baseline is None:
        return list(mems), []
    cur_by_id = {m.id: m for m in current}
    overwritten: set = set()
    survivors = []
    for cm in current:
        if cm.id not in baseline:
            survivors.append(cm)
        elif memory_version(cm) != baseline[cm.id]:
            overwritten.add(cm.id)
            survivors.append(cm)
    kept = []
    for m in mems:
        if m.id in overwritten:
            continue  # 规则A 另一面：过期合并版让位给活值
        if m.id in baseline and m.id not in cur_by_id:
            continue  # 规则B：基线行已被并发删除，不复活
        kept.append(m)
    return kept, survivors


@dataclass
class Memory:
    """
    一条原子事实记忆的内存表示。

    Attributes:
        id: 唯一标识（uuid4 十六进制）
        user_id: 用户隔离键
        content: 记忆文本（如"用户叫小明"）
        source: 来源标记
            - "tool:remember": LLM 主动调用 remember 工具存入
            - "session_summary": 会话结束总结路径存入
            - "legacy": 从 Mem0 旧数据迁移而来
        created_at: 创建时间戳（Unix 秒）
        updated_at: 最近更新时间戳，未更新过为 None
        project: 项目 slug（""=全局/未绑定；inbox 或具体项目）
    """
    user_id: str
    content: str
    source: str = "tool:remember"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    updated_at: float | None = None
    project: str = ""


def memory_visible_in(mem_project: str, active: str) -> bool:
    """检索可见性：全局（空 project）到处可见；否则仅当前项目。"""
    mp = (mem_project or "").strip()
    if not mp:
        return True
    return mp == (active or "").strip()


@dataclass
class Hit:
    """检索命中结果。"""
    memory: Memory
    score: float
    # 通道分量（sqlite 混合检索：{vec, fts, recency}；弱召回 {weak: True}）。
    # 仅诊断面板展示用；file 后端不填（None）。
    detail: dict | None = None

    def to_event_dict(self) -> dict:
        """转为 memory_search 事件需要的字段（memory/score[/detail]）。"""
        d = {"memory": self.memory.content, "score": self.score}
        if self.detail is not None:
            d["detail"] = self.detail
        return d


@dataclass
class Decision:
    """
    Decider 对单条新事实的决策。

    Attributes:
        action: "ADD" | "UPDATE" | "DELETE" | "NOOP" | "FAIL"
            FAIL = LLM/解析失败（不是「已记过」）
        content: 决策后的最终文本（ADD 时=新事实；UPDATE 时=合并后文本；其他可空）
        target_id: UPDATE/DELETE 时指向旧记忆的 id；ADD 时为 None
    """
    action: str
    content: str = ""
    target_id: str | None = None
