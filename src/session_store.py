"""
============================================
会话持久化（JSON 文件）—— 从 src/cli.py 抽出的存储层
============================================
职责：
- 会话的 save/list/load（+ rename/delete 由调用方组合 save/unlink 实现）
- 路径助手（_ensure_sessions_dir / _get_session_file，含路径穿越防御）
- 旧格式迁移（data/sessions/{user_id}.json → data/sessions/{user_id}/{session_id}.json）
- 消息 dict 规范化（lc_to_dict / load_message，供持久化与事件日志共用）

状态权威（P3 双轨收口）：todos / virtual_fs / waker 三字段的权威存储是
kv（scope="session_state"，src/storage/session_state_store.py）。本模块
save / ensure_session_stub 与 JSON 快照同步双写 kv（kv 先行，失败仅告警
不阻断）；load 读状态时 kv 优先，kv 缺失（旧会话）回退 JSON 既有值并
回填 kv（读一次即升级）。JSON 快照降级为缓存：继续写（list_sessions
列表预览等消费方不破坏），语义上可丢——删掉 JSON 文件后状态仍可从 kv
完整恢复（消息侧权威在事件流，见 load_session_events_first）。

抽取原则（T3）：代码从 cli.py 原样搬出，行为零变化；cli.py 改为
from src.session_store import ...（调用形状不变），web worker 的引用同步切换。

消息序列化格式（T7 起，schema v4，format="openai-dict"）：
    {"role": "user"|"assistant"|"system"|"tool",
     "content": ...,
     "tool_calls"?,        # assistant 专属（OpenAI 格式）
     "reasoning"?,         # assistant 专属（推理面板恢复用）
     "tool_call_id"?}      # tool 专属

旧格式（schema ≤v3，T7 前的消息类对象序列化）兼容加载：
    {"type": "HumanMessage"|"AIMessage"|"SystemMessage"|"ToolMessage", ...}
    加载时映射为 OpenAI dict（AIMessage 的 tool_calls 由 {"name","args","id"}
    转成 OpenAI function 格式；ToolMessage 的 tool_call_id 保留）。
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from config import PROJECT_ROOT, get_settings
from src.storage.projects_store import INBOX_SLUG

logger = logging.getLogger("hermes.session_store")

# 会话持久化目录
SESSIONS_DIR = PROJECT_ROOT / "data" / "sessions"
# 会话总结 Markdown 落盘目录（与 data/sessions 同级，结构同构：{user_id}/{session_id}.md）
SUMMARIES_DIR = PROJECT_ROOT / "data" / "summaries"

# T7 消息格式标识（save 写入；load 据此 + 逐条 shape 识别两种格式）
MESSAGE_FORMAT = "openai-dict"

# 旧格式 type 名 → OpenAI role（T7 前的消息类序列化兼容）
_LEGACY_TYPE_TO_ROLE = {
    "HumanMessage": "user",
    "AIMessage": "assistant",
    "SystemMessage": "system",
    "ToolMessage": "tool",
}


# ============================================
# 消息 dict 规范化（持久化/事件日志共用）
# ============================================
def lc_to_dict(msg) -> dict:
    """消息 → 可 JSON 序列化的 OpenAI dict。

    T7 dict 迁移：入参即 OpenAI dict，本函数做「直通 + 规范化」——
    只保留已知键（role/content/tool_calls/tool_call_id/reasoning），
    剔除运行期附带键（compact_id 等），返回浅拷贝（不污染调用方）。
    函数名沿用历史导出（cli.py re-export / session_log 共用）。
    """
    if not isinstance(msg, dict):
        # 兜底：非 dict 输入（理论不该出现）按 user 纯文本处理
        return {"role": "user", "content": str(msg)}

    out: dict = {
        "role": msg.get("role", "user"),
        "content": msg.get("content", ""),
    }
    if msg.get("tool_calls"):
        out["tool_calls"] = msg["tool_calls"]
    if msg.get("tool_call_id"):
        out["tool_call_id"] = msg["tool_call_id"]
    if msg.get("reasoning"):
        out["reasoning"] = msg["reasoning"]
    return out


def load_message(msg_data: dict) -> dict | None:
    """持久化 dict → OpenAI dict 消息（两种格式都认）。

    - 新格式（format="openai-dict"）：带 "role" 键 → 规范化直通。
    - 旧格式：带 "type" 键（"HumanMessage" 等类名）→ 映射为 role；
      AIMessage 的 tool_calls（{"name","args","id"}）转 OpenAI function 格式；
      ToolMessage 的 tool_call_id 保留；additional_kwargs.reasoning 保留。

    无法识别（未知 type / 无 role）返回 None（调用方跳过）。
    """
    if not isinstance(msg_data, dict):
        return None

    msg_type = msg_data.get("type", "")
    if msg_type in _LEGACY_TYPE_TO_ROLE:
        role = _LEGACY_TYPE_TO_ROLE[msg_type]
        out: dict = {"role": role, "content": msg_data.get("content", "")}
        if msg_type == "AIMessage":
            legacy_tcs = msg_data.get("tool_calls")
            if legacy_tcs:
                out["tool_calls"] = [
                    {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": _args_to_json(tc.get("args", {})),
                        },
                    }
                    for tc in legacy_tcs
                ]
            reasoning = (msg_data.get("additional_kwargs") or {}).get("reasoning")
            if reasoning:
                out["reasoning"] = reasoning
        elif msg_type == "ToolMessage":
            tool_call_id = msg_data.get("tool_call_id", "")
            if tool_call_id:
                out["tool_call_id"] = tool_call_id
        return out

    if msg_data.get("role"):
        return lc_to_dict(msg_data)

    return None


def _args_to_json(args) -> str:
    """args（dict）→ JSON 字符串（已是字符串则原样返回）。"""
    if isinstance(args, str):
        return args
    import json as _json
    return _json.dumps(args or {}, ensure_ascii=False)


# ============================================
# 会话自动命名（首句派生，2026-09 草稿会话需求）
# ============================================
# 句界符集：中英文句号/问号/叹号/分号 + 换行
_NAME_BOUNDARIES = set("。！？!?；;\n")


def derive_session_name(text: str, limit: int = 30) -> str:
    """从首条用户消息派生会话名：句界优先、limit 字硬截兑底。

    规则：
    - 清洗 markdown 常见符号与多余空白（保留 \n：换行本身是句界）；
    - 在前 limit 个字符内找**最后一个**句界符，命中（且不在首位）
      则截到句界符之前（不含句界符本身）——句号/问号等结尾标点
      不进会话名；
    - 整句无句界：不超过 limit 原样返回，否则硬截 limit 字加省略号；
    - 派生结果为空（纯图片/空白输入）返回 ""，调用方保持“未命名”。

    供 _compose_session_data 在快照无名字时调用；用户手动 rename 后
    name 非空，本函数不再被触达（“传入优先，否则保持”语义天然保证）。
    """
    import re
    if not isinstance(text, str):
        return ""
    cleaned = re.sub(r"[#>*`~\[\]()!_]", " ", text)
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
    if not cleaned:
        return ""
    cut = -1
    for i in range(min(len(cleaned), limit)):
        if cleaned[i] in _NAME_BOUNDARIES:
            cut = i
    if cut >= 1:
        name = cleaned[:cut]
    elif len(cleaned) <= limit:
        name = cleaned
    else:
        name = cleaned[:limit] + "…"
    return re.sub(r"\s+", " ", name.replace("\n", " ")).strip()


# ============================================
# 路径助手
# ============================================
def _ensure_sessions_dir(user_id: str = None):
    """确保会话目录存在"""
    if user_id:
        user_dir = SESSIONS_DIR / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
    else:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _migrate_old_sessions():
    """
    迁移旧格式会话文件（data/sessions/{user_id}.json）
    到新格式（data/sessions/{user_id}/{session_id}.json）

    仅在首次启动新版本时执行，迁移完成后重命名旧文件为 .bak
    """
    if not SESSIONS_DIR.exists():
        return

    # 找到根目录下的旧格式 .json 文件（非目录内的）
    old_files = [f for f in SESSIONS_DIR.iterdir()
                 if f.is_file() and f.suffix == ".json" and not f.name.endswith(".bak")]

    if not old_files:
        return

    logger.info(f"发现 {len(old_files)} 个旧格式会话文件，开始迁移...")

    for old_file in old_files:
        user_id = old_file.stem  # e.g., "Ming"
        try:
            with open(old_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            messages = data.get("messages", [])
            if not messages:
                old_file.rename(old_file.with_suffix(".json.bak"))
                continue

            # 生成 session_id（使用文件修改时间 + 随机后缀）
            mtime = old_file.stat().st_mtime
            from datetime import datetime as _dt
            ts = _dt.fromtimestamp(mtime).strftime("%Y%m%d_%H%M")
            new_session_id = f"{ts}_{uuid.uuid4().hex[:4]}"

            # 创建用户目录
            user_dir = SESSIONS_DIR / user_id
            user_dir.mkdir(parents=True, exist_ok=True)

            # 获取预览（旧单文件格式的消息带 type 类名，经映射表识别）
            preview = ""
            for msg in messages:
                if _LEGACY_TYPE_TO_ROLE.get(msg.get("type", "")) == "user":
                    if msg.get("content", "").strip():
                        preview = msg["content"].strip()[:60]
                        break

            # 写入新格式
            new_data = {
                "user_id": user_id,
                "session_id": new_session_id,
                "created_at": data.get("updated_at", ""),
                "updated_at": data.get("updated_at", ""),
                "message_count": len(messages),
                "preview": preview,
                "messages": messages,
            }

            new_file = user_dir / f"{new_session_id}.json"
            with open(new_file, "w", encoding="utf-8") as f:
                json.dump(new_data, f, ensure_ascii=False, indent=2)

            # 重命名旧文件为 .bak
            old_file.rename(old_file.with_suffix(".json.bak"))
            logger.info(f"迁移: {old_file.name} → {user_id}/{new_session_id}")

        except Exception as e:
            logger.error(f"迁移失败 {old_file.name}: {e}")


def _get_session_file(user_id: str, session_id: str) -> Path:
    """获取用户指定会话的文件路径。

    纵深防御：即便上游 web 层漏校验，这里也拒绝含路径分隔符/`..` 的段，
    避免拼出 SESSIONS_DIR 之外的路径（路径穿越）。

    R3-16：同时拒绝 `:`——chat 会话 ID 从不含冒号；`waker:`/`wakerflow:`
    前缀的 ID 是无人值守任务的私有事件流（不走 JSON 快照），且 Windows
    上 `:` 是 NTFS ADS 保留字符（a:b.json 会落成备用数据流）。
    """
    for label, val in (("user_id", user_id), ("session_id", session_id)):
        if not val or "/" in val or "\\" in val or ".." in val or ":" in val or "\u0000" in val:
            raise ValueError(f"非法 {label}: {val!r}")
    candidate = SESSIONS_DIR / user_id / f"{session_id}.json"
    # 双重确认：解析后必须仍在 SESSIONS_DIR 内
    try:
        candidate.resolve().relative_to(SESSIONS_DIR.resolve())
    except ValueError:
        raise ValueError(f"会话文件路径越界: {candidate}")
    return candidate


# ============================================
# save / load / list
# ============================================
def _compose_session_data(user_id: str, messages: list, session_id: str,
                          todos: list | None, virtual_fs: dict | None,
                          name: str | None, waker: str | None,
                          project: str | None = None) -> dict:
    """组装会话快照 dict（save_session / ensure_session_stub 共用）。

    含 read-modify-write 语义：读旧文件保持 created_at / name / waker /
    project，传参非 None 时覆盖。
    """
    # 获取首条用户消息作为预览 + 派生自动命名（一次遍历同时算两者）
    # 多模态 content 可能是 list（含 image_url），extract_text 提取纯文本
    # （延迟 import：src.agent 链会经 session_log 反向依赖本模块，顶层 import 成环）
    from src.agent.multimodal import extract_text
    preview = ""
    derived_name = ""
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "user":
            _preview_text = extract_text(msg.get("content", "")).strip()
            if _preview_text:
                preview = _preview_text[:60]
                derived_name = derive_session_name(_preview_text)
                break

    serializable = [lc_to_dict(msg) for msg in messages]

    # 读取已有文件的 created_at / name / waker（保持首次创建时间和已有自定义字段）
    file_path = _get_session_file(user_id, session_id)
    existing_created_at = ""
    existing_name = ""
    existing_waker = ""
    existing_project = ""
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
                existing_created_at = existing_data.get("created_at", "")
                existing_name = existing_data.get("name", "")
                existing_waker = existing_data.get("waker", "")
                existing_project = existing_data.get("project", "")
        except Exception:
            pass

    # 2026-06-16: name 优先用传入值，其次保持已有值
    final_name = name if name is not None else existing_name
    # 自动命名（2026-09 草稿会话）：显式传入为空且快照也无名字时，按首条
    # 用户消息的首句派生。用户手动 rename 后 name 非空，永不再被覆盖。
    if not final_name:
        final_name = derived_name
    # 2026-07-27: waker 同样优先用传入值，其次保持已有值
    final_waker = waker if waker is not None else existing_waker
    # 项目归属是「一次性绑定」：旧值非空则永远保持（用户切项目后继续聊
    # 旧会话不得漂移归属）；空/新会话才接受传入的当前激活 slug。
    # 修改归属属显式迁移动作，不在常规保存路径里做。
    final_project = existing_project or (project or "")

    return {
        "user_id": user_id,
        "session_id": session_id,
        "name": final_name,                            # 2026-06-16: 会话自定义名称
        "created_at": existing_created_at or datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "message_count": len(messages),
        "preview": preview,
        "messages": serializable,
        "todos": todos or [],                          # schema v2: 跨轮次持久化 todos
        "virtual_fs": virtual_fs or {},                # schema v2: 持久化虚拟文件系统
        "waker": final_waker,                          # schema v3: 会话绑定的 waker 人格
        "project": final_project,                      # schema v4 追加键: 项目 slug（""=inbox）
        "format": MESSAGE_FORMAT,                      # v4: 消息为 OpenAI dict 格式
        "schema_version": 4,                           # v4: 消息格式换 openai-dict
    }


def _atomic_write_json(file_path: Path, data: dict) -> None:
    """tmp 同目录写入 + os.replace 原子替换（ADR-0004-D1）。

    崩溃/断电窗口内最多丢"本次"更新，不会留下半截 JSON 被读取方当成
    占位记录。tmp 名带进程号+纳秒时间戳：多进程写同一会话时互不踩 tmp
    （os.replace 的原子性只保证最终 rename，tmp 写入阶段允许并发）。
    不用 uuid——测试会打桩全局 uuid 模块，请求路径上的 uuid4 调用会
    污染其序列。
    """
    tmp_path = file_path.with_name(
        f"{file_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, file_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _save_state_kv(session_id: str, todos: list, virtual_fs: dict, waker: str) -> None:
    """状态三字段写 kv 权威行（P3）。失败仅告警不阻断——JSON 缓存仍在，
    下次 load 回退 JSON 并回填（迁移自愈双向收敛）。"""
    from src.storage import session_state_store
    try:
        session_state_store.save_state(session_id, todos, virtual_fs, waker)
    except Exception:
        logger.warning(f"会话状态 kv 写入失败（JSON 缓存已兜底）: sid={session_id}",
                       exc_info=True)


def save_session(user_id: str, messages: list, session_id: str,
                 todos: list | None = None, virtual_fs: dict | None = None,
                 name: str | None = None, waker: str | None = None,
                 project: str | None = None) -> None:
    """
    将对话历史保存到 JSON 文件

    Args:
        user_id: 用户 ID
        messages: 消息列表（OpenAI dict 格式）
        session_id: 会话 ID（用于文件命名）
        todos: 待办事项列表（跨轮次持久化）
        virtual_fs: 虚拟文件系统（仅 workspace_root 为空时需要持久化）
        name: 2026-06-16 新增，会话自定义名称（None 时保持已有值）
        waker: 2026-07-27 新增，会话绑定的 waker 名（None 时保持已有值；空串=默认助手）
        project: 2026-08-27 新增，项目 slug。一次性绑定语义：仅当快照尚无
            归属时写入当前值；旧值非空则保持不变（ADR-0005 D2）。
    """
    settings = get_settings()
    if not settings.session_persist:
        return

    _ensure_sessions_dir(user_id)
    session_data = _compose_session_data(
        user_id, messages, session_id, todos, virtual_fs, name, waker, project)

    # P3 kv 权威：状态三字段与 JSON 同步双写，kv 先行（JSON 写失败时 kv
    # 已持最新状态；kv 写失败时 JSON 兜底，读侧回退自愈）
    _save_state_kv(session_id, session_data["todos"],
                   session_data["virtual_fs"], session_data["waker"])

    file_path = _get_session_file(user_id, session_id)
    _atomic_write_json(file_path, session_data)

    logger.debug(f"[DEBUG] 会话已保存: {file_path} (todos={len(todos or [])}, vfs={len(virtual_fs or {})}, waker={session_data['waker'] or '-'})")


def ensure_session_stub(user_id: str, messages: list, session_id: str,
                        todos: list | None = None, virtual_fs: dict | None = None,
                        name: str | None = None, waker: str | None = None,
                        project: str | None = None) -> bool:
    """仅当会话快照不存在时创建（fork stub 场景）。返回是否创建了文件。

    ADR-0004-D2 双写者收敛：worker 每轮末的整文件保存是权威写者；主进程
    fork 等旁路只允许「从无到有」，绝不覆盖已有文件——竞态下宁可放弃
    stub（worker 下一次保存会补上权威版本），不可用旁路内容覆盖历史。
    写前二次收口存在性检查，把覆盖窗口压窄到同 tick 内的极限时序。
    """
    settings = get_settings()
    if not settings.session_persist:
        return False

    _ensure_sessions_dir(user_id)
    file_path = _get_session_file(user_id, session_id)
    if file_path.exists():
        return False

    session_data = _compose_session_data(
        user_id, messages, session_id, todos, virtual_fs, name, waker, project)
    if file_path.exists():
        return False
    _atomic_write_json(file_path, session_data)
    # P3 kv 权威：stub 创建时状态行同步进 kv（fork 透传 / chat 开轮 stub
    # 的 todos/vfs/waker 在 JSON 被清（缓存语义）后仍可恢复）
    _save_state_kv(session_id, session_data["todos"],
                   session_data["virtual_fs"], session_data["waker"])
    return True


def read_session_meta(user_id: str, session_id: str) -> dict | None:
    """读取快照顶层字段（不解析消息细节）；文件缺失/损坏返回 None。

    fork 继承源会话 project 归属等场景使用——比 load_session 轻得多
    （同样要 json.load 全文，但语义上只承诺 meta，避免误用 messages）。
    """
    try:
        file_path = _get_session_file(user_id, session_id)
    except ValueError:
        return None
    if not file_path.exists():
        return None
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _resolve_state(user_id: str, session_id: str, data: dict | None) -> tuple[list, dict, str]:
    """状态三字段（todos/vfs/waker）读取：kv 权威优先（P3）。

    - kv 行存在 → 直接采用（JSON 此时只是缓存，可能已删除/陈旧/损坏）；
    - kv 缺失或形状非法（旧会话 / 历史写失败）且 JSON 快照在（data 非
      None）→ 回退 JSON 既有值，并回填 kv（迁移自愈：读一次旧会话即升级；
      形状非法的旧行同时被覆盖修复）。回填失败静默（下次 load 重试），
      不影响本次返回值；
    - JSON 快照缺失/损坏（data 为 None）：kv 缺失即默认值，**不回填**——
      无 JSON 值可迁移，补空行只会给不存在的会话繁殖占位数据。
    """
    from src.storage import session_state_store
    try:
        state = session_state_store.load_state(session_id)
    except Exception:
        logger.warning(f"会话状态 kv 读取失败（回退 JSON 快照）: sid={session_id}",
                       exc_info=True)
        state = None
    if state is not None:
        return state
    if data is None:
        # JSON 快照缺失/损坏：无 JSON 值可回填，kv 缺失即默认值
        return [], {}, ""
    # JSON 回退值（默认值口径不变：v2 文件无 waker → ""）
    todos = data.get("todos", [])
    virtual_fs = data.get("virtual_fs", {})
    waker = data.get("waker", "")
    try:
        session_state_store.save_state(session_id, todos, virtual_fs, waker)
        logger.info(f"会话状态 kv 回填（迁移自愈）: sid={session_id} "
                    f"(todos={len(todos)}, vfs={len(virtual_fs)}, waker={waker or '-'})")
    except Exception:
        logger.debug(f"会话状态 kv 回填失败（下次 load 重试）: sid={session_id}",
                     exc_info=True)
    return todos, virtual_fs, waker


def load_session(user_id: str, session_id: str) -> tuple[list, list, dict, str]:
    """
    从 JSON 文件加载指定会话的对话历史

    Args:
        user_id: 用户 ID
        session_id: 会话 ID

    Returns:
        tuple[list, list, dict, str]: (消息列表, todos, virtual_fs, waker)
            - 旧格式文件（schema_version 缺失/v2）自动返回空 todos/virtual_fs，waker=""
            - waker 空串表示默认助手
            - P3：状态三字段 kv 权威优先——JSON 文件缺失/损坏（缓存语义，
              可丢）时状态仍从 kv 恢复；kv 也缺失才给默认值
    """
    file_path = _get_session_file(user_id, session_id)

    if not file_path.exists():
        logger.debug(f"[DEBUG] 无此会话: {user_id}/{session_id}")
        # JSON 缓存可丢（P3）：状态仍可从 kv 恢复（消息仅在事件流里另行投影）
        todos, virtual_fs, waker = _resolve_state(user_id, session_id, None)
        return [], todos, virtual_fs, waker

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        messages = []
        for msg_data in data.get("messages", []):
            msg = load_message(msg_data)
            if msg is not None:
                messages.append(msg)

        # schema v2/v3: 状态三字段（todos/virtual_fs/waker）——P3 起 kv
        # 权威优先，kv 缺失回退 JSON 既有值并回填（见 _resolve_state）
        todos, virtual_fs, waker = _resolve_state(user_id, session_id, data)

        logger.debug(f"[DEBUG] 加载会话: user_id={user_id}, session_id={session_id}, 消息数={len(messages)}, todos={len(todos)}, vfs={len(virtual_fs)}, waker={waker or '-'}")
        return messages, todos, virtual_fs, waker

    except Exception as e:
        logger.error(f"[ERROR] 加载会话失败: {e}")
        # JSON 损坏同样是"缓存失效"：状态走 kv 恢复（不回填——损坏值不可信）
        todos, virtual_fs, waker = _resolve_state(user_id, session_id, None)
        return [], todos, virtual_fs, waker


def load_session_events_first(user_id: str, session_id: str,
                              session_log=None) -> tuple[list, list, dict, str]:
    """
    事件优先加载（T9）：消息以 SessionLog 事件流投影为准。

    回退矩阵：
    - 该 sid 在 SessionLog（scope="chat"）有事件
        → messages = derive_messages(session_id)（事件投影）；
          todos/vfs/waker 经 load_session 的 kv 权威读（旧会话 kv 缺失
          回退 JSON 快照并回填；都缺失给默认值 []/{}/""）
    - 无事件（旧会话 / SessionLog 不可用）
        → 走原 JSON 路径 load_session（向后兼容，状态同样 kv 优先）

    Args:
        session_log: SessionLog 实例。None 时按需构造（默认库
            data/hermes.db）；worker 调用方可传自己的实例
            （agent._session_log，与组合根同源）。

    Returns:
        与 load_session 同形：tuple[list, list, dict, str]
    """
    if session_log is None:
        from src.agent.session_log import SessionLog  # 延迟 import（session_log 反向依赖本模块）
        session_log = SessionLog()

    try:
        has_events = bool(session_log.events(session_id))
    except Exception:
        logger.warning(f"SessionLog 事件查询失败，回退 JSON 路径: sid={session_id}", exc_info=True)
        has_events = False

    if not has_events:
        return load_session(user_id, session_id)

    messages = session_log.derive_messages(session_id)
    # todos/vfs/waker 不在事件流里：经 load_session 走 kv 权威读（P3；
    # 旧会话 kv 缺失回退 JSON 快照并回填，快照也缺失给默认值）
    _, todos, vfs, waker = load_session(user_id, session_id)
    logger.debug(
        f"[DEBUG] 事件优先加载会话: user_id={user_id}, session_id={session_id}, "
        f"事件投影消息数={len(messages)}, todos={len(todos)}, vfs={len(vfs)}, waker={waker or '-'}"
    )
    return messages, todos, vfs, waker


def list_sessions(user_id: str, include_empty: bool = False,
                  project: str | None = None) -> list[dict]:
    """
    列出用户的历史会话（按更新时间倒序）

    Args:
        include_empty: 是否包含 messages==[] 的合法空快照。默认 False——
            幽灵治理（ADR-0004-D4③）：历史上 worker 重启/重置产生的空快照
            会繁殖进侧栏且点击必然加载失败；新代码已不落盘空桶，此过滤只为
            消化存量。解析失败的损坏文件仍走占位记录显式可见，不受影响。
        project: 项目过滤（ADR-0005 D2）。非 None 时只返回归属匹配的会话：
            非空 slug 精确匹配；"inbox" 同时容纳未绑定的旧会话
            （project 字段缺失/""）。过滤模式下空快照无意义，一律排除。

    Returns:
        list[dict]: 每个元素包含 session_id, name, updated_at, message_count, preview, waker, project
    """
    user_dir = SESSIONS_DIR / user_id
    if not user_dir.exists():
        return []

    sessions = []
    for f in user_dir.glob("*.json"):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not include_empty and not data.get("messages"):
                continue
            if project is not None:
                bound = data.get("project", "")
                if bound != project and not (project == INBOX_SLUG and not bound):
                    continue
            sessions.append({
                "session_id": data.get("session_id", f.stem),
                "name": data.get("name", ""),
                "updated_at": data.get("updated_at", ""),
                "message_count": data.get("message_count", len(data.get("messages", []))),
                "preview": data.get("preview", ""),
                "waker": data.get("waker", ""),
                "project": data.get("project", ""),
            })
        except Exception:
            sessions.append({
                "session_id": f.stem,
                "name": "",
                "updated_at": "",
                "message_count": 0,
                "preview": "",
                "waker": "",
                "project": "",
            })
    sessions.sort(key=lambda s: s["updated_at"], reverse=True)
    return sessions
