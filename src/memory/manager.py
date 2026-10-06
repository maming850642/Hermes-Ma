"""
MemoryManager - 自研记忆管理门面。

组合存储后端（SQLiteProvider 默认，data/hermes.db）+
MemoryExtractor + MemoryDecider。

## 存储注入（T2b-②）
  __init__(workspace_root="", store=None)：
  - store 给定 → 直接用（MemoryStoreProtocol，无 user 维度）
  - 都不给 → 默认 SQLiteProvider()（data/hermes.db）
  - workspace_root 已废弃（P3-5 文件后端退役）：保参兼容旧调用面，
    被忽略——不再有文件后端兜底，不会静默落到 <root>/profile.md
内部所有 store 调用为无 user 形状；公开方法签名保留 user_id 参数
（调用方零改动，值被忽略——单用户架构）。

## 存储入口（agent memory 设计）

  - remember_fact(): agent 已提炼好事实 → 只 decide（去重）+ upsert
                    跳过 extract，避免重复 LLM 调用（问题 1 的解）
  - ingest_conversation(): 会话总结专用 → extract + decide + upsert
                    唯一需要 extract 的入口

## 存储触发（agent memory 核心）

  - 主路径：LLM 调用 remember 工具 → remember_fact
  - 兜底路径：会话结束 → Summarizer → ingest_conversation
  （graph.py 的每轮 store_memory 节点已改 no-op）
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from config import get_settings
from src.memory.consolidator import MemoryConsolidator
from src.memory.decider import MemoryDecider
from src.memory.extractor import MemoryExtractor, SharedLLMProvider
from src.memory.models import Decision, Memory, memory_visible_in

if TYPE_CHECKING:
    # 仅类型标注用（运行期导入会成环：storage.base → memory.models → 本模块）
    from src.storage.base import MemoryStoreProtocol

logger = logging.getLogger("hermes.memory")


def _stamp_project(project: str | None = None) -> str:
    """写入用项目 slug：显式传入优先，否则当前激活项目（失败回 inbox）。

    兜底一律 inbox 而非 ""（全局）——异常路径写入的记忆宁可圈在 inbox，
    也不能变成对所有项目可见的全局记忆（隔离最弱取值）。
    """
    if project is not None and str(project).strip():
        return str(project).strip()
    try:
        from src.storage.projects_store import get_active_project
        return get_active_project() or "inbox"
    except Exception:
        return "inbox"


class MemoryManager:
    """
    记忆管理门面。组合 Store/Extractor/Decider。

    对外方法：
      - search_with_detail(user_id, query, limit, min_score, session_id) -> dict
      - get_all(user_id) -> list[dict]（含 source/时间戳，供 web 页逐条展示）
      - delete_all(user_id) -> bool
      - delete_memory(user_id, memory_id) -> bool（单条删除）
      - edit_memory(user_id, memory_id, content) -> bool（单条人工修正）

    agent memory 路径：
      - remember_fact(user_id, content, source) -> dict
      - ingest_conversation(user_id, messages, session_id) -> dict
    """

    def __init__(self, workspace_root: str = "",
                 store: MemoryStoreProtocol | None = None,
                 llm_provider=None):
        # workspace_root 已废弃（文件后端退役，P3-5）：保参兼容旧调用面，
        # 被忽略；传入也不会再静默落到文件后端
        settings = get_settings()
        # 共享 LLM 提供方：worker 启动后经 set_llm_provider 绑定 chat 侧
        # 同一 client（构造时序上 agent 在本组件之后，故用事后绑定；
        # 未绑定的独立场景用兜底 client，同样不带模型专属参数）
        self.llm_shared = SharedLLMProvider(llm_provider) if llm_provider else None
        if store is not None:
            # 显式注入：协议调用，调用方负责生命周期
            self.store = store
        else:
            # 默认：SQLite（data/hermes.db），内置本地向量客户端
            # （懒加载单例；fastembed 缺失/加载失败时检索自动纯关键词降级）
            from src.memory.embeddings import get_default_embedder
            from src.storage.sqlite_provider import SQLiteProvider
            self.store = SQLiteProvider(embedder=get_default_embedder())
        self.extractor = MemoryExtractor(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.llm_model_name,
            llm=self.llm_shared,
        )
        self.decider = MemoryDecider(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.llm_model_name,
            llm=self.llm_shared,
        )
        self.consolidator = MemoryConsolidator(
            self,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.llm_model_name,
            llm=self.llm_shared,
        )
        self._settings = settings
        backend = type(self.store).__name__
        logger.info(f"MemoryManager 初始化完成（store={backend}）")

    def set_llm_provider(self, provider_fn) -> None:
        """绑定共享 LLM 提供方（零参 callable，返回 chat 侧当前客户端）。

        记忆链路（extract/decide/consolidate）复用同一 client，payload
        差异由各组件调用级参数表达（temperature / extra_body={}）；
        Summarizer 由 session_lifecycle 在构造时读 llm_shared 属性。
        """
        self.llm_shared = SharedLLMProvider(provider_fn)
        self.extractor.llm = self.llm_shared
        self.decider.llm = self.llm_shared
        self.consolidator.llm = self.llm_shared

    # ---------- 内部：执行一条 Decision ----------

    def _apply_decision(self, user_id: str, source: str, decision: Decision,
                        project: str = "") -> str:
        """执行单条决策，返回事件名（ADD/UPDATE/DELETE/NOOP）。"""
        if decision.action == "ADD":
            mem = Memory(user_id=user_id, content=decision.content, source=source,
                         project=project)
            self.store.upsert(mem)
            return "ADD"
        elif decision.action == "UPDATE":
            if not decision.target_id:
                # 缺 target_id，降级为 ADD（保持信息不丢）
                mem = Memory(user_id=user_id, content=decision.content, source=source,
                             project=project)
                self.store.upsert(mem)
                return "ADD"
            # I1 修复：直取目标记忆（替代全量 get_all 线性查找）
            prev = self.store.get_by_id(decision.target_id)
            updated = Memory(
                id=decision.target_id,
                user_id=user_id,
                content=decision.content,
                source=source,
                created_at=prev.created_at if prev else time.time(),
                updated_at=time.time(),
                project=(prev.project if prev and getattr(prev, "project", None) else project),
            )
            self.store.upsert(updated)
            return "UPDATE"
        elif decision.action == "DELETE":
            if decision.target_id:
                if self.store.delete(decision.target_id):
                    return "DELETE"
                # 删除失败：降级为 NOOP（不向用户谎报已删除，I9 修复）
                logger.warning(f"删除记忆失败: target_id={decision.target_id}")
                return "NOOP"
            return "NOOP"
        elif decision.action == "FAIL":
            return "FAIL"
        else:
            return "NOOP"

    # ---------- 新路径 A：remember_fact（agent 已提炼好事实）----------

    def remember_fact(self, user_id: str, content: str, source: str = "tool:remember",
                      project: str | None = None) -> dict:
        """
        agent 已想好要记的内容 → 跳过 extract，只 decide（去重）+ upsert。

        通常只 1 次 LLM（decide）；若无候选旧记忆可短路为 0 次。
        这是 agent memory 的主路径——LLM 通过 remember 工具主动调用。

        Returns:
            {success: bool, events: list[str], item_count: int}
            events 元素是 "ADD"/"UPDATE"/"DELETE"/"NOOP"（供 cli.py 面板统计）
        """
        if not user_id or not user_id.strip():
            raise ValueError("user_id 不能为空")
        if not content or not content.strip():
            return {"success": False, "events": [], "item_count": 0}
        try:
            proj = _stamp_project(project)
            candidates = self.store.search_candidates(content, limit=5, project=proj)
            decisions = self.decider.decide(content, candidates)
            events = [self._apply_decision(user_id, source, d, project=proj) for d in decisions]
            if "FAIL" in events:
                logger.warning(f"remember_fact 决策失败: user={user_id}, events={events}")
                return {"success": False, "events": events, "item_count": 0}
            item_count = len([e for e in events if e not in ("NOOP", "FAIL")])
            logger.info(f"remember_fact: user={user_id}, events={events}")
            return {"success": True, "events": events, "item_count": item_count}
        except Exception as e:
            logger.error(f"remember_fact 失败: {e}", exc_info=True)
            return {"success": False, "events": [], "item_count": 0}

    # ---------- 新路径 B：ingest_conversation（会话总结专用）----------

    def ingest_conversation(self, user_id: str, messages: list[dict],
                            session_id: str | None = None,
                            project: str | None = None) -> dict:
        """
        会话总结路径：extract + decide + upsert。

        messages 是 [{"role": "user"/"assistant", "content": "..."}] 形式。
        由 Summarizer 在会话结束时调用。
        """
        if not user_id or not user_id.strip():
            raise ValueError("user_id 不能为空")
        try:
            # 拼对话文本供 extract_from_session
            conv_text = "\n".join(
                f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages
            )
            facts = self.extractor.extract_from_session(conv_text)
            # 会话总结常吐出整段对话；长期记忆只收短事实
            facts = [(f[:400] if len(f) > 400 else f) for f in (facts or []) if (f or "").strip()]
            if not facts:
                return {"success": True, "events": [], "item_count": 0}

            proj = project
            if not proj and session_id:
                try:
                    from src.constants import LOCAL_USER
                    from src.session_store import read_session_meta
                    meta = read_session_meta(LOCAL_USER, session_id) or {}
                    proj = meta.get("project") or None
                except Exception:
                    proj = None
            proj = _stamp_project(proj)

            events: list[str] = []
            # TODO(性能优化): 当前对 N 条事实逐条 decide（N 次 LLM）。会话总结提取事实多时较慢。
            # 可优化为批量 decide——一次把多条事实+各自候选喂给 Decider。当前有 session_lifecycle
            # 的超时兜底，影响可控。
            for fact in facts:
                candidates = self.store.search_candidates(fact, limit=5, project=proj)
                decisions = self.decider.decide(fact, candidates)
                for d in decisions:
                    events.append(self._apply_decision(
                        user_id, "session_summary", d, project=proj))

            item_count = len([e for e in events if e != "NOOP"])
            logger.info(f"ingest_conversation: user={user_id}, events={events}")
            return {"success": True, "events": events, "item_count": item_count}
        except Exception as e:
            logger.error(f"ingest_conversation 失败: {e}", exc_info=True)
            return {"success": False, "events": [], "item_count": 0}

    # ---------- 检索 ----------

    def search_with_detail(
        self,
        user_id: str,
        query: str,
        limit: int | None = None,
        min_score: float | None = None,
        session_id: str | None = None,
        project: str | None = None,
    ) -> dict:
        """语义检索，返回带 raw/filtered 详情。

        返回结构（cli.py/web.py 依赖）：
            {raw_results, filtered_results, raw_count, hit_count}
            filtered_results 元素含 memory/score
        """
        if not user_id or not user_id.strip():
            raise ValueError("user_id 不能为空")
        if limit is None:
            limit = self._settings.max_memory_results
        if min_score is None:
            # 融合相关度语义下默认不过滤（旧 memory_min_score 覆盖率阈值已退役）
            min_score = 0.0

        try:
            proj = project if project is not None else _stamp_project()
            hits = self.store.search(query, limit=limit, min_score=0.0, project=proj)
            # raw_results：原始召回（未过滤 min_score）
            raw_results = [h.to_event_dict() for h in hits]
            # filtered_results：过滤 min_score 后（detail=通道分量，诊断面板用）
            filtered_results = [
                {"memory": h.memory.content, "score": h.score, "detail": h.detail}
                for h in hits if h.score >= min_score
            ]
            logger.info(f"检索: user={user_id}, raw={len(raw_results)}, hit={len(filtered_results)}")
            return {
                "raw_results": raw_results,
                "filtered_results": filtered_results,
                "raw_count": len(raw_results),
                "hit_count": len(filtered_results),
            }
        except Exception as e:
            logger.error(f"检索失败: {e}", exc_info=True)
            return {"raw_results": [], "filtered_results": [], "raw_count": 0, "hit_count": 0}

    # ---------- 全量读取 / 清空（保持旧签名）----------

    def get_all(self, user_id: str, project: str | None = None) -> list[dict]:
        """返回当前项目空间可见记忆（本项目 ∪ 全局），与检索可见性一致。

        元素键：memory/id/source/created_at/updated_at/project。
        project=None 跟顶栏激活项目；跨项目记忆不返回。
        """
        if not user_id or not user_id.strip():
            raise ValueError("user_id 不能为空")
        try:
            active = _stamp_project(project)
            mems = self.store.get_all()
            return [
                {
                    "memory": m.content,
                    "id": m.id,
                    "source": m.source,
                    "created_at": m.created_at,
                    "updated_at": m.updated_at,
                    "project": getattr(m, "project", "") or "",
                }
                for m in mems
                if memory_visible_in(getattr(m, "project", "") or "", active)
            ]
        except Exception as e:
            logger.error(f"get_all 失败: {e}")
            return []

    def delete_all(self, user_id: str, project: str | None = None) -> bool:
        """只清当前项目绑定的记忆（project=本项目），全局记忆不动。

        全局记忆（project=""，所有项目共享）不随项目级清空连带删除——
        否则从任一项目页点清空会把其他项目共用的记忆一并抹掉。
        """
        if not user_id or not user_id.strip():
            raise ValueError("user_id 不能为空")
        try:
            active = _stamp_project(project)
            if not active:
                # 连激活项目都解析不出（异常态）：宁可不删，不冒删全局的风险
                logger.warning("delete_all 跳过：项目解析为空，拒绝按全局语义清空")
                return False
            n = 0
            for m in self.store.get_all():
                if (getattr(m, "project", "") or "") == active:
                    if self.store.delete(m.id):
                        n += 1
            logger.info(f"清除记忆: user={user_id}, project={active}, n={n}")
            return True
        except Exception as e:
            logger.error(f"delete_all 失败: {e}")
            return False

    # ---------- 单条管理（web 记忆页的逐条纠错入口）----------

    def delete_memory(self, user_id: str, memory_id: str) -> bool:
        """删除单条记忆。未知 id / 存储异常返回 False。"""
        if not user_id or not user_id.strip():
            raise ValueError("user_id 不能为空")
        try:
            ok = self.store.delete(memory_id)
            logger.info(f"删除记忆: user={user_id}, id={memory_id}, ok={ok}")
            return ok
        except Exception as e:
            logger.error(f"delete_memory 失败: {e}")
            return False

    def edit_memory(self, user_id: str, memory_id: str, content: str) -> bool:
        """人工修正单条记忆文本。

        保留原 id/source/created_at，updated_at 刷新为当前时间
        （该版本号同时是 replace_all baseline 守卫的比较口径，人工修正
        视同一次活写入，聚合窗口内不会被整合结果覆盖）。
        未知 id / 空文本返回 False。
        """
        if not user_id or not user_id.strip():
            raise ValueError("user_id 不能为空")
        text = (content or "").strip()
        if not text:
            return False
        try:
            cur = self.store.get_by_id(memory_id)
            if cur is None:
                return False
            self.store.upsert(Memory(
                id=cur.id,
                user_id=cur.user_id,
                content=text,
                source=cur.source,
                created_at=cur.created_at,
                updated_at=time.time(),
                project=getattr(cur, "project", "") or "",
            ))
            logger.info(f"编辑记忆: user={user_id}, id={memory_id}")
            return True
        except Exception as e:
            logger.error(f"edit_memory 失败: {e}")
            return False

    def add_memory(self, user_id: str, content: str, project: str | None = None,
                   source: str = "manual") -> str | None:
        """用户在记忆页手动新增一条事实（不走 LLM decide）。"""
        if not user_id or not user_id.strip():
            raise ValueError("user_id 不能为空")
        text = (content or "").strip()
        if not text:
            return None
        if len(text) > 400:
            text = text[:400]
        mem = Memory(
            user_id=user_id, content=text, source=source,
            project=_stamp_project(project),
        )
        try:
            self.store.upsert(mem)
            logger.info(f"手动新增记忆: user={user_id}, id={mem.id}")
            return mem.id
        except Exception as e:
            logger.error(f"add_memory 失败: {e}")
            return None

    # ---------- 聚合：整合记忆 + 备份/恢复（转发 consolidator/store）----------

    def consolidate(self, user_id: str) -> dict:
        """聚合全部原子记忆为更少更密的条目，原地替换。

        返回结构见 MemoryConsolidator.consolidate：
            {ok, before_count, after_count, backup_name, error}
        """
        if not user_id or not user_id.strip():
            raise ValueError("user_id 不能为空")
        return self.consolidator.consolidate(user_id)

    def list_backups(self, user_id: str) -> list:
        """列出全部聚合备份（label 列表，时间倒序）。"""
        if not user_id or not user_id.strip():
            raise ValueError("user_id 不能为空")
        try:
            return self.store.list_backups()
        except Exception as e:
            logger.error(f"list_backups 失败: {e}")
            return []

    def restore_backup(self, user_id: str, backup_name: str) -> bool:
        """把指定备份恢复为当前全量（恢复前当前内容也会备份）。"""
        if not user_id or not user_id.strip():
            raise ValueError("user_id 不能为空")
        try:
            ok = self.store.restore_backup(backup_name)
            logger.info(f"恢复备份: user={user_id}, name={backup_name}, ok={ok}")
            return ok
        except Exception as e:
            logger.error(f"restore_backup 失败: {e}")
            return False
