"""
MemoryConsolidator —— 记忆聚合器。

把原子记忆按**项目分组**后各自喂给 LLM，合并去重为更少、更高密度的条目，
原地替换。分组是隔离硬边界：项目 A 与项目 B 的记忆永不被合进同一条，
整合产物继承本组 project（不会落成对所有项目可见的全局记忆）；全局组
（project=""）单独合并，地位等同于一个普通项目组。

## 核心流程
  1. get_all 快照（聚合前状态），逐行记版本基线 v0(id)=updated_at|created_at
  2. 按 m.project 分组；组内 ≤1 条原样保留，否则该组独立 LLM 合并
     （全新 id，不与快照 id 冲突，产物带本组 project）
  3. 全部组（含原样保留组）合并为最终列表，一次 replace_all(
     merged, backup=True, baseline=v0) 单事务替换。
     任一组 LLM 失败则整体放弃替换——replace_all 是整库替换，缺了
     失败组等于把它们直接删掉。

## 并发安全（baseline 行级"活写入者优先"，T2b-② 的加固版）
聚合是 LLM 长任务（~20s），期间 per-user worker / waker 子进程可能
改写记忆。粗暴替换会丢它们。解法：把快照时刻每行的版本号作为
baseline 传给 replace_all——存储层按三条规则保留窗口内写入：
  - 窗口内新增的行（id 不在基线）→ 一律保留（原 protect_outside 行为）
  - 窗口内被 UPDATE 的行 → 当前活值胜出，不被旧快照合出的内容覆盖
    （原 protect_outside 保护不到这类写入，是它被替换的原因）
  - 窗口内被 DELETE 的行 → 不从整合结果复活
SQLiteProvider 在单事务（BEGIN IMMEDIATE）内完成，窗口为零；
FileMemoryProvider 在适配层并入，语义等价（共享 base.apply_baseline_guard）。
备份兜底照旧（replace_all 生成 backup-{ts} / profile.md.bak.{ts}）。

不引入跨进程文件锁（msvcrt/fcntl），不阻塞 chat，零平台相关代码。
"""
import json
import logging
import time

from src.llm.client import LLMClient
from src.memory.extractor import extract_json, memory_client
from src.memory.models import Memory, memory_version
from src.memory.prompts import CONSOLIDATE_PROMPT

logger = logging.getLogger("hermes.memory.consolidator")


class MemoryConsolidator:
    """记忆聚合器：原子记忆 → 整合记忆，原地替换。"""

    def __init__(self, manager, api_key: str, base_url: str, model: str, llm=None):
        """
        Args:
            manager: MemoryManager 实例（用它的 store 读写记忆）
            api_key/base_url/model: 兜底路径的 LLM 配置
            llm: 共享客户端（LLMClient 或 SharedLLMProvider）。传入则复用
                chat 侧同一 client；None 时兜底自建普通 client。
        """
        self.manager = manager
        self.llm = llm if llm is not None else LLMClient(
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=0.3,  # 整合用稍高温度，文本更自然
        )

    def consolidate(self, user_id: str) -> dict:
        """按项目分组聚合全部记忆，原地替换。

        Returns:
            {ok, before_count, after_count, backup_name, error}
            - ok=False 时 error 说明原因（too_few / llm_failed），存储不变
            - ok=True 时 backup_name 为备份 label（可能为 None，如原库为空）
        """
        store = self.manager.store
        try:
            # 1) 快照：聚合前的全部记忆 + id 集合（并发保护锚点）
            snapshot = store.get_all()
            before_count = len(snapshot)
            if before_count <= 1:
                # 太少不值得聚合（省 LLM）
                logger.info(f"consolidate 跳过：user={user_id} 仅 {before_count} 条记忆")
                return {
                    "ok": False, "before_count": before_count, "after_count": before_count,
                    "backup_name": None, "error": "too_few",
                }

            # 行级版本基线：窗口内被 UPDATE 的行靠它保住活值、被 DELETE 的
            # 不复活；新增行无需在此记录（id 不在基线即视为新增）
            pre_baseline = {m.id: memory_version(m) for m in snapshot}

            # 2) 按项目分组（隔离硬边界：组间永不混合）
            groups: dict[str, list[Memory]] = {}
            for m in snapshot:
                groups.setdefault(getattr(m, "project", "") or "", []).append(m)

            # 3) 逐组合并：≤1 条原样保留；多条独立 LLM 合并（产物带本组 project）
            merged_all: list[Memory] = []
            merged_groups = 0
            for proj, group in groups.items():
                if len(group) <= 1:
                    merged_all.extend(group)
                    continue
                merged = self._llm_merge(user_id, group, project=proj)
                if merged is None:
                    # 整库替换语义下不能只替换部分组（缺组=删组），整体放弃
                    logger.warning(
                        f"consolidate LLM 合并失败（project={proj!r}），未替换: user={user_id}")
                    return {
                        "ok": False, "before_count": before_count,
                        "after_count": before_count,
                        "backup_name": None, "error": "llm_failed",
                    }
                merged_all.extend(merged)
                merged_groups += 1
            if merged_groups == 0:
                # 每组都 ≤1 条：无可合并，不值得整库替换
                logger.info(f"consolidate 跳过：user={user_id} 各项目组均 ≤1 条")
                return {
                    "ok": False, "before_count": before_count, "after_count": before_count,
                    "backup_name": None, "error": "too_few",
                }

            # 4) 备份 + 原子替换（baseline：LLM 期间的新增/改写/删除由
            #    存储层按行级规则保留）
            backup_name = store.replace_all(merged_all, backup=True, baseline=pre_baseline)
            # after_count 读回实际全量（守卫可能保留了并发写入）
            after_count = len(store.get_all())
            logger.info(
                f"consolidate 完成：user={user_id}, {before_count} → {after_count} 条"
                f"（{len(groups)} 个项目空间，{merged_groups} 组参与合并）"
                f"{f'，备份 {backup_name}' if backup_name else ''}"
            )
            return {
                "ok": True, "before_count": before_count, "after_count": after_count,
                "backup_name": backup_name, "error": None,
            }
        except Exception as e:
            logger.error(f"consolidate 异常（未替换）：user={user_id}: {e}", exc_info=True)
            return {
                "ok": False, "before_count": -1, "after_count": -1,
                "backup_name": None, "error": str(e),
            }

    # ---------- 内部：LLM 合并 ----------

    def _llm_merge(self, user_id: str, snapshot: list[Memory],
                   project: str = "") -> list[Memory] | None:
        """调 LLM 把 snapshot（同一项目空间内）合并为更少更密的 Memory 列表。

        返回 None 表示失败（LLM/解析），调用方降级不替换。
        新 Memory 的 created_at 取其 source_ids 中最早的一条（保留历史时间线），
        project 继承本组（整合不改变记忆可见范围）。
        """
        # 构造 LLM 输入
        mem_json = json.dumps(
            [{"id": m.id, "content": m.content} for m in snapshot],
            ensure_ascii=False,
        )
        prompt = CONSOLIDATE_PROMPT.format(memories_json=mem_json)
        raw = memory_client(self.llm).invoke_simple(
            prompt, temperature=0.3, extra_body={})
        data = extract_json(raw)
        if data is None:
            logger.warning("consolidate: LLM 输出非 JSON")
            return None
        items = data.get("items")
        if not isinstance(items, list) or not items:
            logger.warning("consolidate: LLM 输出 items 为空或格式错误")
            return None

        # id → created_at 映射，用于整合条目保留最早时间
        id_to_created = {m.id: m.created_at for m in snapshot}
        now = time.time()

        merged: list[Memory] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            content = (it.get("content") or "").strip()
            if not content:
                continue
            source_ids = it.get("source_ids") or []
            # created_at：取被合并条目里最早的；若无则用当前
            times = [id_to_created[sid] for sid in source_ids if sid in id_to_created]
            created_at = min(times) if times else now
            merged.append(Memory(
                user_id=user_id,
                content=content,
                source="consolidated",
                created_at=created_at,
                updated_at=now,
                project=project,
            ))
        if not merged:
            logger.warning("consolidate: 整合后条目为空，放弃替换")
            return None
        return merged
