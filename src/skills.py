"""
============================================
SkillRegistry - 技能注册与加载模块
============================================
通用技能加载器，兼容市面主流 skill/rules 格式（Claude Code Skills、
Cline Rules、Cursor Rules 等）。

目录约定（v2 - 2026-06-18 重构）：
    <skills_root>/
    └── <技能名>/              ← 每个子文件夹 = 一个技能
        ├── SKILL.md           ← 入口文件（优先级最高）
        ├── script/            ← 附属脚本（自动识别为 resource）
        │   └── extract.py
        └── template.json      ← 附属资源（自动识别为 resource）

入口 md 查找优先级：
    1. SKILL.md / skill.md（Claude Code 约定）
    2. 文件夹内唯一的 .md
    3. 与文件夹同名的 .md

附属资源（resources）：技能文件夹内除入口 md 外的所有文件（递归），
排除 __pycache__、.pyc、隐藏文件。resources 清单会在 use_skill
返回时告知 LLM，LLM 可用 bash cat 按需读取。

设计原则：
- skill = 一个文件夹（入口 md + 可选附属资源）
- 无新依赖（手写 frontmatter 解析器）
- 多目录扫描：项目内 ./skills/、用户 ~/.hermes/skills/、外部 EXTRA_SKILLS_DIRS
- 全局单例，懒加载

2026-06-18: v2 重构，改为强制文件夹结构，支持附属资源
2026-06-18: 初始实现
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from config import get_settings, PROJECT_ROOT

logger = logging.getLogger("hermes.skills")


# ============================================
# 数据结构
# ============================================


@dataclass
class Skill:
    """
    单个技能的内存表示。

    Attributes:
        name: 技能唯一标识（用于 /skill <name> 和 use_skill 工具）
            优先取 frontmatter 的 name，无则用文件夹名
        description: 一句话描述（注入技能目录供 LLM 判断）
        content: 技能正文 Markdown（入口 md 去除 frontmatter 后的完整指令）
        source: 来源标记，用于 /skill 命令展示（"project" / "user" / "extra"）
        file_path: 入口 md 文件路径（调试用）
        dir_path: 技能文件夹根目录（bash cat 读取附属资源时的相对路径基准）
        resources: 附属资源相对路径列表（相对于 dir_path，例如 "script/extract.py"）
    """

    name: str
    description: str
    content: str
    source: str = "project"
    file_path: str = ""
    dir_path: str = ""
    resources: list[str] = field(default_factory=list)

    @property
    def source_icon(self) -> str:
        """来源图标（用于 CLI 展示）"""
        return {"project": "📁", "user": "🏠", "extra": "🔗"}.get(self.source, "📄")


# ============================================
# frontmatter 解析（手写，无 PyYAML 依赖）
# ============================================


def _parse_frontmatter(raw_text: str) -> tuple[dict, str]:
    """
    解析 Markdown 文件开头的 YAML frontmatter。

    仅支持最简单的格式（覆盖市面 99% 的 skill 文件）：
        ---
        name: code-review
        description: 专业代码审查
        ---

    不支持的复杂 YAML（嵌套、多行字符串）会被安全跳过，返回空 dict，
    content 回退为原文。

    Args:
        raw_text: 文件原始文本

    Returns:
        tuple[dict, str]: (frontmatter 字典, 正文内容)
    """
    stripped = raw_text.lstrip()
    if not stripped.startswith("---"):
        return {}, raw_text

    # 找到结束的 ---
    lines = stripped.split("\n")
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return {}, raw_text

    fm_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1 :]).lstrip("\n")

    meta: dict = {}
    for line in fm_lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip().lower()
        val = val.strip()
        # 去除引号包裹
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        meta[key] = val

    return meta, body


def _extract_skill_from_text(raw_text: str, fallback_name: str) -> Skill:
    """
    从入口 md 文本提取 Skill。

    Args:
        raw_text: 入口 md 原始内容
        fallback_name: 兜底技能名（文件夹名），frontmatter 无 name 时使用

    Returns:
        Skill: 提取出的技能对象（source/file_path/dir_path/resources 由调用方填充）
    """
    meta, body = _parse_frontmatter(raw_text)

    name = meta.get("name", "").strip()
    description = meta.get("description", "").strip()

    if not name:
        # 无 frontmatter 或 frontmatter 无 name → 从正文首行 # 标题推断
        body_stripped = body.lstrip()
        if body_stripped.startswith("# "):
            first_line = body_stripped.split("\n", 1)[0]
            name = first_line[2:].strip()
        if not name:
            name = fallback_name

    if not description:
        # 兜底：取正文第一段非空非标题行的前 80 字符
        for line in body.split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                description = line[:80]
                break
        if not description:
            description = f"技能 {name}"

    return Skill(
        name=name,
        description=description,
        content=body.strip(),
    )


# ============================================
# SkillRegistry
# ============================================


# 资源收集时排除的目录和扩展名
_EXCLUDE_DIRS = {"__pycache__", ".git", "node_modules", ".idea", ".vscode"}
_EXCLUDE_EXT = {".pyc", ".pyo", ".class"}


class SkillRegistry:
    """
    技能注册表：扫描多个目录，加载所有技能文件夹。

    目录扫描顺序（后者覆盖前者同名技能）：
        1. 项目内 ./skills/         （source="project"）
        2. 用户 ~/.hermes/skills/   （source="user"）
        3. EXTRA_SKILLS_DIRS 配置    （source="extra"，分号分隔）

    每个目录下，每个子文件夹 = 一个技能。
    """

    def __init__(self):
        self._skills: dict[str, Skill] = {}
        self._loaded = False

    def _scan_dir(self, root_path: Path, source: str) -> None:
        """
        扫描技能根目录，每个子文件夹作为一个技能加载。

        Args:
            root_path: 技能根目录（如 ./skills/）
            source: 来源标记
        """
        if not root_path.exists() or not root_path.is_dir():
            return

        for entry in sorted(root_path.iterdir()):
            if not entry.is_dir():
                # 顶层散落的文件忽略（v2 强制文件夹结构）
                logger.debug(f"跳过非文件夹项: {entry}（v2 仅识别技能文件夹）")
                continue
            self._load_skill_dir(entry, source)

    def _load_skill_dir(self, skill_dir: Path, source: str) -> None:
        """
        加载单个技能文件夹。

        Args:
            skill_dir: 技能文件夹路径（如 ./skills/readpdf/）
            source: 来源标记
        """
        # 1. 查找入口 md
        entry_md = self._find_entry_md(skill_dir)
        if entry_md is None:
            logger.warning(
                f"技能文件夹无入口 md，跳过: {skill_dir} "
                f"（入口应为 SKILL.md / skill.md / 唯一.md / <文件夹名>.md）"
            )
            return

        # 2. 读取并解析入口 md
        try:
            raw = entry_md.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"读取技能入口失败 {entry_md}: {e}")
            return

        skill = _extract_skill_from_text(raw, skill_dir.name)
        skill.source = source
        skill.file_path = str(entry_md)
        skill.dir_path = str(skill_dir)
        skill.resources = self._collect_resources(skill_dir, entry_md)

        self._skills[skill.name] = skill
        logger.debug(
            f"已加载技能: {skill.name} ({source}) <- {skill_dir.name} "
            f"(resources: {len(skill.resources)})"
        )

    @staticmethod
    def _find_entry_md(skill_dir: Path) -> Path | None:
        """
        在技能文件夹中查找入口 md。

        优先级：
            1. SKILL.md / skill.md（Claude Code 约定）
            2. 文件夹内唯一的 .md
            3. 与文件夹同名的 .md

        Args:
            skill_dir: 技能文件夹路径

        Returns:
            Path | None: 入口 md 路径，未找到返回 None
        """
        # 优先级 1: SKILL.md / skill.md
        for name in ("SKILL.md", "skill.md"):
            candidate = skill_dir / name
            if candidate.exists() and candidate.is_file():
                return candidate

        # 优先级 2: 文件夹内唯一的 .md（仅顶层，不递归）
        top_md_files = sorted(p for p in skill_dir.glob("*.md") if p.is_file())
        if len(top_md_files) == 1:
            return top_md_files[0]

        # 优先级 3: 与文件夹同名的 .md
        same_name = skill_dir / f"{skill_dir.name}.md"
        if same_name.exists() and same_name.is_file():
            return same_name

        return None

    @staticmethod
    def _collect_resources(skill_dir: Path, entry_md: Path) -> list[str]:
        """
        收集技能文件夹内除入口 md 外的所有文件（递归）。

        排除规则：
            - 入口 md 本身
            - __pycache__ / .git / node_modules 等目录下的文件
            - .pyc / .pyo 等编译产物
            - 隐藏文件和隐藏目录下的文件（. 开头）

        Args:
            skill_dir: 技能文件夹路径
            entry_md: 入口 md 路径（排除）

        Returns:
            list[str]: 附属资源相对路径列表（正斜杠分隔，如 "script/extract.py"）
        """
        resources: list[str] = []
        for f in sorted(skill_dir.rglob("*")):
            if not f.is_file():
                continue
            if f == entry_md:
                continue

            rel = f.relative_to(skill_dir)
            parts = rel.parts

            # 排除指定目录
            if any(part in _EXCLUDE_DIRS for part in parts):
                continue
            # 排除编译产物
            if f.suffix.lower() in _EXCLUDE_EXT:
                continue
            # 排除隐藏文件/目录
            if any(part.startswith(".") for part in parts):
                continue

            # 统一用正斜杠（跨平台一致，也方便 LLM 理解）
            resources.append(str(rel).replace("\\", "/"))

        return resources

    def load(self, force: bool = False) -> None:
        """
        扫描所有技能目录并加载（懒加载，仅首次调用实际执行）。

        Args:
            force: 强制重新加载
        """
        if self._loaded and not force:
            return

        self._skills.clear()

        # 1. 项目内 skills 目录
        project_skills = PROJECT_ROOT / "skills"
        self._scan_dir(project_skills, "project")

        # 2. 用户私有目录 ~/.hermes/skills
        user_skills = Path.home() / ".hermes" / "skills"
        self._scan_dir(user_skills, "user")

        # 3. 外部目录（配置项 extra_skills_dirs，分号分隔）
        settings = get_settings()
        extra_dirs = getattr(settings, "extra_skills_dirs", "") or ""
        for raw_dir in extra_dirs.split(";"):
            raw_dir = raw_dir.strip()
            if not raw_dir:
                continue
            self._scan_dir(Path(raw_dir).expanduser(), "extra")

        self._loaded = True
        logger.info(f"SkillRegistry 加载完成: 共 {len(self._skills)} 个技能")

    def get(self, name: str) -> Skill | None:
        """按名称获取技能（大小写敏感）。"""
        self.load()
        return self._skills.get(name)

    def list_all(self) -> list[Skill]:
        """列出所有已加载的技能（按名称排序）。"""
        self.load()
        return sorted(self._skills.values(), key=lambda s: s.name)

    def list_names(self) -> list[str]:
        """列出所有技能名称。"""
        self.load()
        return sorted(self._skills.keys())

    def exists(self, name: str) -> bool:
        """检查技能是否存在。"""
        self.load()
        return name in self._skills

    def build_catalog(self) -> str:
        """
        构建技能目录文本（注入 system prompt，供 LLM 判断是否调用 use_skill）。

        始终返回非空字符串；无技能时返回提示语。
        """
        self.load()
        skills = self.list_all()

        if not skills:
            return "## 可用技能\n（当前无可用技能）"

        lines = ["## 可用技能", "你可以通过 `use_skill` 工具加载某个技能的详细操作指令。当任务与某个技能匹配时，先调用它再执行：", ""]
        for s in skills:
            lines.append(f"- **{s.name}**: {s.description}")
        return "\n".join(lines)

# ============================================
# 全局单例
# ============================================

_registry: SkillRegistry | None = None


def get_registry() -> SkillRegistry:
    """获取全局 SkillRegistry 单例。"""
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
    return _registry