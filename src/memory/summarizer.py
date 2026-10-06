"""
Summarizer - 会话结束总结路径。

两步分开（用户明确要求主次分明，见设计 Review 问题 2）：
  Step 1（主产品）：整段对话 → 结构化总结 → 存为 source="session_summary"
  Step 2（副产品）：从对话提取长期事实 → 复用 MemoryManager.ingest_conversation

两步独立：即使总结失败，事实提取仍会尝试；反之亦然。

I4 修复（2026-06-22）：Step 2 不再手写复制 extract_from_session 的逻辑，
而是复用 manager.ingest_conversation，避免代码漂移 + 统一 extract+decide 路径。

2026-06-24 新增：Step 1 成功后可选落盘为 Markdown 文件（save_summary_markdown），
方便用户用任意编辑器/资源管理器查看总结。

2026-06-27 (M0)：总结写入路径从 Qdrant 换成 FileMemoryStore（manager.store），
本文件无需改动——store.upsert 接口对齐。仅更新注释。
"""
import logging
from datetime import datetime
from pathlib import Path

from src.llm.client import LLMClient

from src.memory.prompts import SUMMARIZE_PROMPT
from src.memory.models import Memory
from src.memory.extractor import memory_client

logger = logging.getLogger("hermes.memory.summarizer")

# SUMMARIZE_PROMPT 的边界情况约定：全寒暄会话 LLM 输出这句话（或以其开头），
# 表示没有值得沉淀的内容——此时不写记忆、不做事实提取（空转只会产生垃圾行）
_EMPTY_SUMMARY_MARKER = "本次会话无可沉淀的有效内容"


def save_summary_markdown(
    user_id: str,
    session_id: str,
    summary_text: str,
    summaries_dir: Path,
    facts_count: int = 0,
) -> Path | None:
    """将会话总结落盘为 Markdown 文件。

    与 data/sessions 同构：summaries_dir/{user_id}/{session_id}.md。
    落盘失败仅记日志返回 None，不影响主流程（文件已写入则不回滚）。

    Args:
        user_id: 用户 ID（目录隔离）
        session_id: 会话 ID（文件名）
        summary_text: 总结正文（不含前缀）
        summaries_dir: 总结根目录（如 PROJECT_ROOT/data/summaries）
        facts_count: 同步提取的事实条数，写入元信息

    Returns:
        成功返回文件 Path，失败返回 None
    """
    try:
        user_dir = Path(summaries_dir) / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        file_path = user_dir / f"{session_id}.md"

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        content = (
            f"# 会话总结 {session_id}\n\n"
            f"- **用户**: {user_id}\n"
            f"- **生成时间**: {now}\n"
            f"- **提取事实**: {facts_count} 条\n"
            f"\n---\n\n"
            f"{summary_text}\n"
        )
        file_path.write_text(content, encoding="utf-8")
        logger.info(f"会话总结已落盘: {file_path}")
        return file_path
    except Exception as e:
        logger.warning(f"会话总结落盘失败（不影响退出）: {e}")
        return None


class Summarizer:
    """会话总结器。"""

    def __init__(self, manager, api_key: str, base_url: str, model: str, llm=None):
        """
        Args:
            manager: MemoryManager 实例（用于 store.upsert 写总结、ingest_conversation 存事实）
            api_key/base_url/model: 兜底路径的 LLM 配置
            llm: 共享客户端（LLMClient 或 SharedLLMProvider）。传入则复用
                chat 侧同一 client；None 时兜底自建普通 client。
        """
        self.manager = manager
        self.llm = llm if llm is not None else LLMClient(
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=0.3,  # 总结用稍高温度，文本更自然
        )

    def summarize_and_store(
        self,
        user_id: str,
        conversation_text: str,
        session_id: str,
        summaries_dir=None,
        project: str | None = None,
    ) -> dict:
        """
        对一段会话做总结 + 提取事实并存储。

        Args:
            user_id: 用户 ID
            conversation_text: 整段会话文本（"用户: ...\n助手: ..." 形式）
            session_id: 会话 ID（用于标识来源，写入总结前缀）
            summaries_dir: 总结根目录 Path（如 PROJECT_ROOT/data/summaries）。
                传入则同时落盘一份 Markdown；None 时仅写记忆（profile.md）不落盘（向后兼容）。
            project: 会话绑定项目。None 时从会话快照 meta 解析，再不行回落
                激活指针（与 ingest_conversation 同序）——总结记忆跟会话归属走。

        Returns:
            {summary_stored: bool, facts_count: int, markdown_path: Path | None}
            markdown_path 为落盘文件路径，未落盘时为 None。
        """
        # 空对话直接跳过
        if not conversation_text or not conversation_text.strip():
            return {"summary_stored": False, "facts_count": 0, "markdown_path": None, "summary_text": ""}

        # 总结归属：显式参数 → 会话快照 meta → 激活指针（与 ingest_conversation
        # 同序）。Step 1 的总结记忆必须跟会话归属走，否则项目 A 的会话总结
        # 会以全局身份对所有项目可见。
        proj = project
        if not proj and session_id:
            try:
                from src.constants import LOCAL_USER
                from src.session_store import read_session_meta
                proj = (read_session_meta(LOCAL_USER, session_id) or {}).get("project") or None
            except Exception:
                proj = None
        if not proj:
            from src.memory.manager import _stamp_project
            proj = _stamp_project()

        summary_stored = False
        facts_count = 0
        markdown_path = None
        # 暂存 Step 1 生成的正文，待 Step 2 算出 facts_count 后一并落盘
        summary_text = ""

        # Step 1: 生成总结（主产品）
        try:
            prompt = SUMMARIZE_PROMPT.format(conversation_text=conversation_text)
            summary_text = memory_client(self.llm).invoke_simple(
                prompt, temperature=0.3, extra_body={}).strip()
            if not summary_text:
                logger.warning("会话总结为空，未存储")
            elif summary_text.startswith(_EMPTY_SUMMARY_MARKER):
                # 提示词明确约定"不要硬凑"，这句话等于零信息；照单入库只会
                # 污染检索与页面（曾出现同一句重复落库多条）
                logger.info(f"会话无有效内容，跳过总结与事实提取: session={session_id}")
                return {
                    "summary_stored": False,
                    "facts_count": 0,
                    "markdown_path": None,
                    "summary_text": summary_text,
                }
            else:
                mem = Memory(
                    # 幂等键：同一会话固定一个 id，重复总结覆盖而非新增一行
                    # （此前随机 uuid 导致快速多次切换会话时同句总结堆积）
                    id=f"sess-summary-{session_id}",
                    user_id=user_id,
                    content=f"[会话总结 {session_id}]\n{summary_text}",
                    source="session_summary",
                    project=proj,
                )
                self.manager.store.upsert(mem)
                summary_stored = True
                logger.info(f"会话总结已存储: user={user_id}, session={session_id}")
        except Exception as e:
            logger.error(f"会话总结失败: {e}", exc_info=True)

        # Step 2: 提取长期事实（副产品，独立于 Step 1 的成败）
        # I4 修复：复用 manager.ingest_conversation（extract + decide + upsert），
        # 不再手写复制 extract_from_session 逻辑。传入 dict 格式的 messages。
        try:
            # ingest_conversation 期望 [{"role","content"}] 格式，
            # 把整段会话文本作为单条 user 消息传入（extract_from_session 会从中提取事实）
            messages = [{"role": "user", "content": conversation_text}]
            result = self.manager.ingest_conversation(
                user_id, messages, session_id, project=proj)
            # item_count = 非 NOOP 的事件数，作为事实提取计数
            events = result.get("events", [])
            facts_count = len([e for e in events if e != "NOOP"])
        except Exception as e:
            logger.error(f"会话事实提取失败: {e}", exc_info=True)

        # 落盘 Markdown（仅在总结成功 + 调用方传入目录时）
        if summary_stored and summary_text and summaries_dir is not None:
            markdown_path = save_summary_markdown(
                user_id, session_id, summary_text, summaries_dir, facts_count
            )

        return {
            "summary_stored": summary_stored,
            "facts_count": facts_count,
            "markdown_path": markdown_path,
            "summary_text": summary_text,
        }

