"""
============================================
Shell 执行器 —— 安全分类逻辑（纯函数，无副作用）
============================================
对命令做"白名单自动放行 / 其余需审批"的分类，是 run_shell 工具的
纵深防御第一层。本模块刻意保持纯函数 + 无 I/O 依赖，便于单测覆盖。

分类规则（按优先级，先匹配先返回）：
1. 黑名单（破坏性 / 危险重定向模式）命中 → review（即使主程序在白名单也拦）
2. 所有子命令首 token 的 basename 都在白名单 → auto
   （形态受限程序除外：git / env / printenv 需通过参数形态检查，否则 review）
3. 否则 → review

注意（必须写明，避免误以为这是完备沙箱）：
- 这是"纵深防御"而非"完备沙箱"。shell=True 本身带注入风险，
  白名单程序仍可能被组合出危险操作（典型：python 在白名单时
  `python -c "import os; os.system('任意命令')"` 可绕过——因此默认
  白名单不含解释器，或由调用方决定是否纳入）。
- 真正的隔离边界应是容器 / VM，本工具不提供。
"""

from __future__ import annotations

import re
import shlex
from pathlib import PurePath


# ---- 默认策略（config 可覆盖）----
# 默认白名单：只读 / 查询类命令，避免写入与解释器（python/node 等可执行任意代码）。
DEFAULT_ALLOWED_COMMANDS = frozenset(
    {
        "ls", "ll", "dir",
        "cat", "head", "tail", "less", "more",
        "echo", "printf",
        "grep", "egrep", "fgrep", "rg", "find",
        "pwd", "whoami", "hostname", "date", "cal",
        "wc", "sort", "uniq", "cut", "tr", "awk", "sed",
        "cd",  # 只改 cwd 无读写；复合命令带 cd 不再无谓弹审批（路径无硬边界，与 cat 绝对路径同级）
        "diff", "comm", "cmp",
        "git",  # 仅只读子命令自动放行（GIT_READONLY_SUBCOMMANDS 枚举 + 形态检查）
        "tree",
        "env", "printenv",  # 仅只读形态放行（无参 / -0 / -u NAME），见 _env_shape_allows
        "stat", "file",
        "du", "df",
    }
)

# 默认黑名单：破坏性操作 / 危险重定向 / fork 炸弹等。子串匹配（已小写）。
DEFAULT_BLOCKED_PATTERNS = (
    r"\brm\s+(-\w*)?rf?\s+/(?:\s|$)",   # rm -rf /
    r"\brm\s+(-\w*)?rf?\s+[A-Za-z]:[\\/]",  # rm -rf C:\
    r"\bmkfs\b",                          # 格式化文件系统
    r"\bdd\b.*\bof\s*=\s*/dev/",          # dd 写裸设备
    r":\(\)\s*\{\s*:\|:\&\s*\}\s*;\s*:",  # fork 炸弹 :(){:|:&};:
    r">\s*/dev/sd",                       # 重定向写裸磁盘
    r"\bshutdown\b", r"\breboot\b", r"\bpoweroff\b", r"\bhalt\b",
    r"\bformat\b\s+[A-Za-z]:",            # Windows format
    r">\s*/etc/", r">\s*/proc/", r">\s*/sys/",  # 写系统目录
    # Windows 用户/系统目录的重定向目标（S4：正/反斜杠两种写法都拦；
    # 命中 → force_deny 硬底线，任何权限模式拒绝）
    r">+\s*/c/users\b",                   # Git Bash 风格 > /c/Users/...
    r">+\s*c:/users\b",                   # > c:/Users/...
    r">+\s*c:\\users\b",                  # > C:\Users\...
    r">+\s*%appdata%",                    # cmd 展开 > %APPDATA%\...
    r">+\s*%userprofile%",                # > %USERPROFILE%\...
    r">+\s*/c/windows\b",                 # > /c/Windows/...
    r">+\s*c:/windows\b",                 # > c:/Windows/...
    r">+\s*c:\\windows\b",                # > C:\Windows\...
    r">\s*/dev/null\s*<\s*/dev/sd",       # 从裸设备读
    r"\bnslookup\b|\bnmap\b",             # 网络扫描（偏向异常探测）
    # 管道/重定向执行远程脚本
    r"curl\b.*\|\s*(sh|bash|zsh|python)\b",
    r"wget\b.*\|\s*(sh|bash|zsh|python)\b",
    r"curl\b.*\|\s*python",
    r"wget\b.*\|\s*python",
    r"chmod\s+-R\s+777\s+/",             # 全盘放开权限
    # 白名单程序的危险参数形态（这些程序在白名单内但参数可执行任意代码/写文件）
    r"\bfind\b.*\s+-(?:execdir|exec|okdir|ok)\b",  # find -exec/-execdir/-ok 任意命令执行
    r"\bawk\b.*\bsystem\s*\(",            # awk system() 任意命令执行
    r"\bawk\b.*\"\s*\|\s*getline",        # awk 管道 getline（"cmd" | getline 执行任意命令）
    r"\bawk\b.*\)\s*\|\s*getline",        # awk ("cmd") | getline（括号形态管道执行）
    r"\bawk\b.*\|\s*&",                   # awk |& 协进程（print |& "cmd" 双向管道执行）
    r"\bawk\b.*\|\s*[\"']",               # awk print | "cmd"（管道右值为命令串，执行任意命令）
    # sed -i 不在此列（2026-09 文件工具退役，sed -i 成为改文件主通道）：
    # 纯 `sed -i 's/a/b/' f` 与重定向写同级风险 → 写信号（审批），见
    # WRITE_SIGNAL_PATTERNS。RCE 形态（e 命令 / s///e）仍由下两条硬拒，
    # 与 -i 无关；-f / w/W/r/R / 块{} 经 sed 形态检查落 review（审批）。
    r"\bsed\b.*['\";/]e\s",               # sed e 命令（把模式空间当 shell 命令执行）
    r"\bsed\b.*\bs/[^/\n]*/[^/\n]*/[a-zA-Z]*e\b",  # sed s///e 标志（替换结果当命令执行）
    r"\bredis\b|\bmysql\b|\bpsql\b|\bmongo\b",  # 数据库客户端（数据面风险）
)

# ---- 写文件信号（S4）----
# 即使所有子命令首词都在白名单内，命中任一信号即 destructive（审批/按模式拒绝）。
# 1) 任何重定向（> / >>，含 2> 、&> 、2>&1 变体）——统一从严，不豁免 2>/dev/null
#    （理由：重定向目标无法静态判定是否越出工作区，交人工审批把关）。
# 2) 白名单程序的文件写形态：sed -i（原地改写）/ sed 的 w 写标志 /
#    sort -o / tee / git config --global。
WRITE_SIGNAL_PATTERNS = (
    re.compile(r"\bsed\b.*\s+(?:-[a-zA-Z]*i|--in-place)\b"),  # sed -i / --in-place[=bak]（含 -ni 等粘连）
    re.compile(r"\bsed\b.*s/[^/]*/[^/]*/[a-zA-Z]*w\b"),  # sed 's/a/b/w out'
    re.compile(r"\bsed\b.*?/w[\s']"),                    # sed '/pat/w out'（w 命令）
    re.compile(r"\bsed\b.*--file-output"),               # sed 写文件输出长参
    re.compile(r"\bsed\b.*['\";/]r\s"),                  # sed r 命令（读外部文件并入输出，从严按写信号审批）
    re.compile(r"\bfind\b.*\s+-(?:fprintf|fprint0|fprint|fls)\b"),  # find 结果直写文件（绕过 > 重定向检测）
    re.compile(r"\bsort\b.*?(?:\s-o(?:\s|$)|--output)"),  # sort -o out in
    re.compile(r"(?:^|[\s;|&|(])tee\b"),                 # tee（管道写文件）
    re.compile(r"\bgit\b\s+config\s+(?:--global\b|--system\b)"),  # 改全局 git 配置
)


def has_write_signal(command: str) -> bool:
    """命令含重定向或白名单程序的写文件形态 → True（destructive 信号）。"""
    if ">" in command:
        return True
    for pat in WRITE_SIGNAL_PATTERNS:
        if pat.search(command):
            return True
    return False


# ---- 参数位命令替换 / 进程替换 / here-string（二轮审查 P0 级绕过）----
# `echo $(任意命令)`、反引号、`cat <(cmd)`、`cat <<< "$(...)"` 的首词都是
# 白名单程序（echo / cat），首词白名单对这种形态整体失效。按"不可达语义"
# 原则：出现即 review，不尝试解析替换体语义。
# 引号语义（与 bash 一致，排除字面量误杀）：
# - 单引号内一切是字面量：$( / ` / <( / <<< 均不生效 → 不标记；
# - 双引号内 $( 与 ` 仍会展开 → 标记；<( 与 <<< 在双引号内是字面量 → 不标记；
# - 反斜杠转义（引号外任意字符、双引号内 \$ \` \" \\）使后一个字符字面化。

def _has_expansion_or_procsub(command: str) -> bool:
    """扫描原始命令（保留引号），判断是否存在引号外的命令替换 / 进程替换 /
    here-string 形态。返回 True → review。"""
    state = ""  # "" = 引号外；"'" = 单引号内；'"' = 双引号内
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if state == "'":
            # 单引号内无任何转义，仅 ' 结束
            if c == "'":
                state = ""
            i += 1
            continue
        if state == '"':
            if c == "\\":
                i += 2  # \" \$ \` \\ 等转义，后一个字符字面化
                continue
            if c == '"':
                state = ""
            elif c == "$" and i + 1 < n and command[i + 1] == "(":
                return True  # 双引号内 $( 仍展开
            elif c == "`":
                return True  # 双引号内反引号仍展开
            i += 1
            continue
        # 引号外
        if c == "\\":
            i += 2
            continue
        if c == "'" or c == '"':
            state = c
            i += 1
            continue
        if c == "$" and i + 1 < n and command[i + 1] == "(":
            return True
        if c == "`":
            return True
        if c == "<" and (command.startswith("<(", i) or command.startswith("<<<", i)):
            return True
        i += 1
    return False


# ---- 配置加载（S4：与 run_shell 的 _load_* 提取共用，classify 也读 settings）----

def load_allowed_commands(settings=None) -> set[str]:
    """从 config 读白名单命令（逗号分隔字符串 → 集合）。缺失回落默认。"""
    if settings is None:
        from config import get_settings
        settings = get_settings()
    raw = getattr(settings, "shell_allowed_commands", "")
    if not raw:
        return set(DEFAULT_ALLOWED_COMMANDS)
    return {c.strip().lower() for c in str(raw).split(",") if c.strip()}


def load_blocked_patterns(settings=None) -> list[str]:
    """从 config 读黑名单模式（`;;` 分隔）。缺失回落默认表。

    P1-2：配置在默认表之上"追加"而非整体替换——默认黑名单内含逗号（如
    :(){:|:&};:）不能用逗号切分；且整体替换会让出厂/用户配置静默丢失默认
    保护（fork 炸弹、find -exec、Windows 目录重定向等 fail-open）。
    """
    if settings is None:
        from config import get_settings
        settings = get_settings()
    raw = getattr(settings, "shell_blocked_patterns", "")
    extra = [p.strip() for p in str(raw).split(";;") if p.strip()]
    return list(DEFAULT_BLOCKED_PATTERNS) + extra


def _split_subcommands(command: str) -> list[str]:
    """
    按命令分隔符拆分子命令。

    支持 `&&` `||` `;` `|` 以及单个 `&`（后台执行）与 bash 的 `|&`。
    使用 shlex(punctuation_chars=True) 解析 token 流，以正确处理引号内的
    分隔符（如 echo "a && b" 不应被拆开），并确保 `;` `&` `|` 无论是否与
    单词粘连（`log;` vs `log ;`）都被识别为独立分隔符 token。

    换行 / 回车（P0-1）：shell 把换行当命令分隔符，而 shlex 只当普通空白
    （`cat f\\nwhoami` 会被吞成一条子命令）。解析前统一归一为 `;`；引号内的
    换行归一后成为引号内的 `;`，shlex 的引号语义保证它留在同一 token，
    不会被误拆（echo "a\\nb" 仍是一个子命令）。

    注意：shlex 不理解 shell 控制结构（if/for/while），复杂脚本会被
    粗略拆分——这可接受，因为复杂脚本几乎必然落到 review 分支。
    """
    # 预处理：换行 / 回车归一为 `;`（P0-1）
    normalized = command.replace("\r\n", ";").replace("\r", ";").replace("\n", ";")
    if not normalized or not normalized.strip():
        return []
    try:
        # punctuation_chars=';&|' 让 shlex 把这些字符当独立标点 token，
        # 修复 `git log; whoami`（无空格）被当作单 token `log;` 的绕过问题。
        lex = shlex.shlex(normalized, posix=True, punctuation_chars=";&|")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        # 引号不匹配等：视为不可解析，返回整体交由上层 review
        return [normalized.strip()]

    subcmds: list[str] = []
    current: list[str] = []
    # `&&` 与单 `&` 都是分隔符（`echo hi & cmd` 两段都会执行）；
    # `|&` 是 bash 的 stdout+stderr 管道。shlex 会把连续标点合并为单 token，
    # 故 `&&` 不会误撞 `&`。
    separators = {"&&", "||", ";", "|", "&", "|&"}
    for tok in tokens:
        if tok in separators:
            if current:
                subcmds.append(" ".join(current))
                current = []
        else:
            current.append(tok)
    if current:
        subcmds.append(" ".join(current))
    return subcmds


def _first_token(subcommand: str) -> str:
    """
    取子命令的首个 token（即程序名），归一化为 basename + 小写。

    处理：
    - 环境变量前缀（CC=gcc gcc ...）：跳过赋值段
    - 路径前缀（/usr/bin/git, ./bin/run）：取 basename
    - 命令 substitution 前缀（$(...)、`...`）：无法静态判定，保守返回原串
      （几乎必然不在白名单 → review，安全）
    """
    s = subcommand.strip()
    if not s:
        return ""
    # 跳过环境变量赋值前缀：VAR=val VAR2=val2 cmd ...
    assign_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+")
    while assign_re.match(s):
        s = assign_re.sub("", s, count=1)
    # 取首个空白前的 token
    first = s.split(None, 1)[0] if s else ""
    if not first:
        return ""
    # 命令替换 / 子 shell 开头：保守不可判定
    if first.startswith("$") or first.startswith("`") or first.startswith("("):
        return first  # 不在白名单 → review
    # 取 basename（/usr/bin/git → git，./run → run，C:\\tool\\x → x）
    try:
        base = PurePath(first).name
    except (ValueError, TypeError):
        base = first
    # Windows: strip .exe/.bat/.cmd/.ps1
    base = re.sub(r"\.(exe|bat|cmd|ps1|com)$", "", base, flags=re.IGNORECASE)
    return base.lower()


# ---- 白名单内"形态受限"程序（P0-2 / P1-1）----
# 这些程序的首词虽在白名单，但特定参数形态等价于"执行任意程序 / 写任意路径"，
# 放行前需做形态检查；形态不合规按非白名单处理（review）。

# git 只读子命令枚举：仅这些子命令自动放行（P1-1——此前 git 整体放行，
# reset --hard / clean -fdx / apply 等写形态全部 auto 零审批）。
GIT_READONLY_SUBCOMMANDS = frozenset({
    "log", "show", "diff", "status", "blame", "shortlog", "describe",
    "rev-parse", "reflog", "cat-file", "ls-files", "grep",
    "for-each-ref", "merge-base", "name-rev", "count-objects",
    "diff-tree", "diff-index", "diff-files", "version",
})
# 这些子命令仅在"纯列举"（无位置参数）形态下只读：branch / remote / tag
# 带位置参数即创建 / 删除 / 改名（git branch -D x、remote add o url、tag v1）。
GIT_LIST_ONLY_SUBCOMMANDS = frozenset({"branch", "remote", "tag"})

# 带独立参数值的全局选项（找子命令时连同其值一起跳过）
_GIT_GLOBAL_OPTS_WITH_VALUE = frozenset({"-C", "--git-dir", "--work-tree", "--namespace"})


def _git_shape_allows(subcommand: str) -> bool:
    """git 子命令的只读形态检查。返回 False → 按非白名单处理（review）。

    拦截形态：
    - 全局 `-c name=value`：可注入 alias.*='!...'、core.pager、fsmonitor 等
      执行钩子（子命令区的 `-c` 是 diff/log 的合并显示选项，只读，不受限）；
    - `--output=`/`--output <file>`/`-o`/`-O`/`--open-files-in-pager`：
      把输出写到任意路径或走 pager 执行；
    - 非只读子命令（reset/clean/apply/rebase/checkout/restore/push/commit/
      config…）与未知子命令（新增子命令默认从严）。
    """
    parts = subcommand.split()
    # ① 全局选项区：定位子命令；-c 出现即拒绝
    sub = ""
    i = 1  # parts[0] = git（可能带路径前缀，已由 _first_token 归一）
    while i < len(parts):
        tok = parts[i]
        if tok == "-c":
            return False
        if tok in _GIT_GLOBAL_OPTS_WITH_VALUE:
            i += 2  # 连同其参数值一起跳过
            continue
        if tok.startswith("-"):
            i += 1
            continue
        sub = tok
        i += 1
        break
    # ② 子命令选项区：写文件 / pager 执行形态一律拒绝
    for tok in parts[i:]:
        if ("--output=" in tok or tok == "--output"
                or "--open-files-in-pager" in tok
                or tok.startswith("-o") or tok.startswith("-O")):
            return False
    if not sub:
        # 只有全局选项（如 git --version）：无子命令可写
        return True
    if sub in GIT_READONLY_SUBCOMMANDS:
        return True
    if sub in GIT_LIST_ONLY_SUBCOMMANDS:
        # 纯列举形态：其余 token 必须全是选项（出现位置参数 → 创建/删除/改名）
        return all(t.startswith("-") for t in parts[i:])
    return False


def _sed_script_token_safe(script_token: str) -> bool:
    """对单个脚本文本 token 做放行判定。

    在 _sed_script_is_plain_substitute 之上追加一条保守规则：token 以顶层
    `;` 结尾 → review。原因：classify 拿到的子命令是 shlex 去引号后按空白
    rejoin 的串，脚本文本内部含空格时 token 边界已丢失——
    `sed -e '3s/a/b/; 1e id'` 会被拆成 `3s/a/b/;` + `1e` + `id`，若只验
    `3s/a/b/;` 会漏掉后续段。无法可靠重组脚本就以 review 从严。
    """
    t = script_token.rstrip()
    if t.endswith(";"):
        return False
    return _sed_script_is_plain_substitute(t)


def _sed_script_is_plain_substitute(script: str) -> bool:
    """sed 脚本文本是否为"纯 s/// 替换"：允许行地址前缀（数字 / $ / /pat/），
    flags 仅限 g / p / i / 数字。这是白名单放行的唯一脚本形态；
    其余一切（e/E/w/W/r/R 等命令、s/// 的 e/r/w 标志、其他命令、无法解析的
    脚本）返回 False → review（从严——不解析完整 sed 语义，按不可达语义处理）。

    解析按游标逐段进行：s 命令内部的 pattern/replacement 以其自身分隔符
    定界，其中的 `;` 是普通字符；段间分隔只认 flags 之后的顶层 `;`。
    （不能用 naive split(";")——会把 `s/,/;/g` 的 replacement `;` 误切。）
    """
    s = script.strip()
    if not s:
        return False
    n = len(s)
    i = 0
    while True:
        # 跳过段间空白与顶层分号
        while i < n and (s[i].isspace() or s[i] == ";"):
            i += 1
        if i >= n:
            return True  # 尾随分号/空白：所有段已通过
        # 剥离地址前缀：行号 / $ / /pat/（从严不解析转义，找不到配对即 False）
        c = s[i]
        if c.isdigit():
            while i < n and s[i].isdigit():
                i += 1
        elif c == "$":
            i += 1
        elif c == "/":
            close = s.find("/", i + 1)
            if close == -1:
                return False
            i = close + 1
        elif c == "{":
            return False  # 命令块 {} 从严
        if i >= n or s[i] != "s":
            return False  # 非 s 命令（或纯地址）→ 从严
        i += 1
        if i >= n:
            return False
        delim = s[i]
        if delim.isalnum() or delim in "\\ \t;":
            return False  # 非法分隔符形态 → 无法可靠解析 → 从严
        i += 1
        # 找第 2、3 个未转义分隔符（第 1 个是 delim 本身）
        ends = 0
        while i < n and ends < 2:
            ch = s[i]
            if ch == "\\":
                i += 2
                continue
            if ch == delim:
                ends += 1
            i += 1
        if ends < 2:
            return False
        # flags：直到顶层 `;` 或结尾，仅允许 g / p / i / 数字
        while i < n and s[i] != ";":
            if not (s[i].isdigit() or s[i] in "gpi"):
                return False
            i += 1


def _sed_shape_allows(subcommand: str) -> bool:
    """sed 的参数形态检查（参照 _git_shape_allows 模式）。返回 False → review。

    背景（二轮审查）：黑名单正则只覆盖 e/w/r 前是引号/分号/斜杠的形态，
    `sed -e '3e cmd'`（数字地址+e）、`-e '1w file'`、`-e 'W file'`、
    `-e '1r file'`、`s/a/id/eg`（逆序双标志）全部漏拦——e 命令把模式空间
    当 shell 命令执行，w/W 写任意文件。改为形态白名单：脚本文本必须是
    纯 s/// 替换（见 _sed_script_is_plain_substitute），其余一律 review；
    -f/--file（脚本来自文件，内容不可静态判定）直接 review。
    """
    parts = subcommand.split()
    script_checked = False
    i = 1  # parts[0] = sed
    while i < len(parts):
        tok = parts[i]
        if tok.startswith("-") and len(tok) > 1 and not script_checked:
            if tok in ("-e", "--expression"):
                # -e 的下一个 token 是脚本文本（shlex 已去掉引号）
                if i + 1 >= len(parts):
                    return False
                if not _sed_script_token_safe(parts[i + 1]):
                    return False
                script_checked = True
                i += 2
                continue
            if tok in ("-f", "--file") or tok.startswith("-f"):
                return False  # 脚本来自文件，内容不可静态判定（含 -ffile 粘连）
            if tok.startswith("--expression="):
                if not _sed_script_token_safe(tok.split("=", 1)[1]):
                    return False
                script_checked = True
                i += 1
                continue
            if tok.startswith("--file="):
                return False
            if tok.startswith("-e"):
                # 粘连形态 -es/a/b/
                if not _sed_script_token_safe(tok[2:]):
                    return False
                script_checked = True
                i += 1
                continue
            if tok in ("-l", "--line-length"):
                i += 2  # 带值选项，连同值跳过
                continue
            # 其余选项（-n/-r/-E/-s/-z/-u/--posix 等）不改变脚本语义，跳过
            i += 1
            continue
        if not script_checked:
            # 首个位置参数 = 脚本文本
            if not _sed_script_token_safe(tok):
                return False
            script_checked = True
        else:
            # 脚本之后再出现 -e/-f/--expression/--file → 混入未验脚本，从严
            if tok.startswith(("-e", "-f", "--expression", "--file")):
                return False
            # 其余为输入文件名/尾部选项，放行
        i += 1
    # 无脚本文本（形态异常）→ 从严
    return script_checked


def _env_shape_allows(subcommand: str) -> bool:
    """env / printenv 的只读形态检查（P0-2）。

    `env <程序> ...` 的首 token 恒为 env，白名单命中即放行 = 无条件任意程序
    执行（白名单机制对这条路径整体失效）。因此仅放行"查询环境"形态：
    无参数 / -0（NUL 分隔输出）/ -u NAME（剔除变量后打印）；其余（含任何
    位置参数 = 要执行的程序）→ review。
    """
    parts = subcommand.split()
    i = 1  # parts[0] = env / printenv
    while i < len(parts):
        tok = parts[i]
        if tok == "-0":
            i += 1
        elif tok == "-u":
            if i + 1 >= len(parts):
                return False  # -u 缺变量名，形态不完整
            i += 2
        else:
            return False  # 位置参数 = 借 env 执行的程序 → review
    return True


_SHAPE_CHECKERS = {
    "git": _git_shape_allows,
    "env": _env_shape_allows,
    "printenv": _env_shape_allows,
    "sed": _sed_shape_allows,
}


def _matches_blocked(command_lowered: str, blocked_patterns) -> str | None:
    """
    检查完整命令是否命中黑名单模式。返回命中的模式串（用于原因说明），未命中返回 None。

    Args:
        command_lowered: 已小写的完整命令
        blocked_patterns: 编译好的 regex 列表（或字符串，内部 compile）
    """
    for pat in blocked_patterns:
        if isinstance(pat, str):
            try:
                pat_c = re.compile(pat)
            except re.error:
                continue
        else:
            pat_c = pat
        if pat_c.search(command_lowered):
            return pat_c.pattern if hasattr(pat_c, "pattern") else str(pat)
    return None


def classify_command(
    command: str,
    allowed: set[str] | frozenset[str] | None = None,
    blocked_patterns=None,
) -> tuple[str, str]:
    """
    对命令分类，返回 (decision, reason)。

    decision:
        - "auto":   白名单内，可自动执行无需审批
        - "review": 需人工审批（含黑名单命中 / 含非白名单程序 / 解析失败）
        - "block":  命中高危黑名单的极端情况（当前并入 review，由人工最终把关）

    reason: 人类可读的分类原因（中文），用于审批面板与日志。

    Args:
        command: 完整命令字符串
        allowed: 白名单程序集合（None 用 DEFAULT_ALLOWED_COMMANDS）
        blocked_patterns: 黑名单正则（None 用 DEFAULT_BLOCKED_PATTERNS）
    """
    if not command or not command.strip():
        return ("review", "空命令")

    if allowed is None:
        allowed = DEFAULT_ALLOWED_COMMANDS
    if blocked_patterns is None:
        blocked_patterns = DEFAULT_BLOCKED_PATTERNS

    command_lowered = command.lower()

    # 1. 黑名单优先（即使主程序在白名单也拦）
    hit = _matches_blocked(command_lowered, blocked_patterns)
    if hit:
        return ("review", f"命中危险模式黑名单: {hit}")

    # 1.5 参数位命令替换 / 进程替换 / here-string：`echo $(cmd)` 首词是
    #     白名单程序，但替换体可执行任意命令——首词白名单对这种形态失效，
    #     出现即 review（单引号内字面量豁免，双引号内仍展开按存在处理）。
    if _has_expansion_or_procsub(command):
        return (
            "review",
            "检测到命令替换/进程替换（$( / 反引号 / <( / <<<），需人工审批",
        )

    # 2. 拆子命令，逐个检查首 token 是否在白名单
    subcmds = _split_subcommands(command)
    if not subcmds:
        return ("review", "无法解析命令结构")

    non_allowed: list[str] = []
    for sc in subcmds:
        ft = _first_token(sc)
        if not ft:
            continue
        if ft not in allowed:
            non_allowed.append(ft)
            continue
        # 白名单内的形态受限程序（git / env / printenv）：参数形态不合规
        # （如 env python、git reset --hard）等价于非白名单 → review
        checker = _SHAPE_CHECKERS.get(ft)
        if checker is not None and not checker(sc):
            non_allowed.append(ft)

    if non_allowed:
        return (
            "review",
            f"包含非白名单程序: {', '.join(non_allowed)}",
        )

    return ("auto", "白名单内命令")


# ════════════════════════════════════════════════════════════════
# V3 Layer 3 evaluator 包装
# ════════════════════════════════════════════════════════════════

# ---- 自进化护栏（2026-09）：写自己仓库的路径检测 ----
# 目标：agent 改自己源码/配置的常见写形态，即使在 full_access 也强制
# 人工审批（force_approval）；.git/ 与 .self-evolve/ 的破坏则硬拒。
# 触发以"命令文本含仓库根路径字串（三形态）"为准——刻意不按 cwd 锚定
# 判定：未挂载会话锚点在 data/home，相对写入够不到 src/（要够到必须写
# 绝对路径或 cd 链，前者必含路径字串被拦，后者是声明的诚实边界）；
# 按锚定拦会把 full_access 下 waker 的常规写盘全部误伤。
# 诚实边界：正则尽力而为——cd ../.. 链、变量拼接等变通可绕过；
# 无 OS 级沙箱，硬保证 = 审批卡 + self_backup 备份 + git。

# .git 内部路径段（前后须为路径边界，.gitignore 不误伤）
_GIT_INTERNALS_RE = re.compile(r"(?:[/\\]|^|[\s\"'=])\.git(?:[/\\]|$)")
# 删除/移动备份目录（rm .self-evolve / mv .self-evolve xxx）
_BACKUP_DESTROY_RE = re.compile(r"\b(?:rm|rmdir|mv)\b[^;|&]*\.self-evolve")


def _repo_path_variants() -> list[str]:
    """PROJECT_ROOT 的三种文本形态（已小写）：正斜杠 / 反斜杠 / MSYS。

    用于子串匹配命令文本：命令里出现仓库绝对路径（无论哪种写法）
    即视为"提到仓库"。
    """
    from src.storage import paths

    s = str(paths.PROJECT_ROOT).lower().replace("\\", "/")
    drive, _, rest = s.partition(":")
    variants = [s, s.replace("/", "\\")]
    if rest:  # Windows 盘符路径才有 MSYS 形态 /d/...
        variants.append("/" + drive + rest)
    return variants


def _mentions_repo_path(command_lowered: str) -> bool:
    """命令文本是否包含仓库根路径字串（三形态任一）。"""
    return any(v in command_lowered for v in _repo_path_variants())


def classify(args: dict, ctx) -> "SideEffectsOverride":
    """V3 Layer 3 evaluator：按本次命令内容动态修正 destructive 标签。

    签名遵循 SideEffectsEvaluator 协议：(args, ctx) -> SideEffectsOverride。

    决策逻辑（复用 classify_command + 单独判黑名单；S4 起读 settings）：
        黑名单命中（fork bomb 等）      → force_deny=True   （硬底线，任何模式拒绝）
        写 .git/ 内部 / 破坏 .self-evolve → force_deny=True （自进化护栏硬底线）
        命令提到仓库路径且非纯读         → force_approval   （full_access 也审批）
        写文件信号（重定向等）          → destructive=True （白名单首词也不放行）
        白名单内（ls/git/grep）         → destructive=False（降级，plan 放行）
        非白名单（rm/mkdir/pip）        → destructive=True （维持，before_changes 审批）

    自进化护栏的强制审批触发条件（满足其一）：
        a) 写文件信号 + 命令文本含仓库根路径字串（三形态：正斜杠/反斜杠/MSYS）；
        b) 命令文本含仓库路径字串 且 非只读白名单形态（堵 python -c 带仓库
           绝对路径的绕过；纯读 cat/grep 仓库文件不受影响）。
        刻意不按 cwd 锚定判定（见模块注释：误伤 full_access waker 常规写盘，
        且对代码面保护增益趋近于零）。

    S4：白名单/黑名单从 settings 读取（shell_allowed_commands /
    shell_blocked_patterns，缺省回落模块默认值）——此前 classify 用硬编码
    两表，用户配置被无视。load_allowed_commands / load_blocked_patterns
    与 run_shell 的 _load_* 共用同一实现。

    为何单独判黑名单：现有 classify_command 把黑名单命中并入 "review"，
    无法区分"非白名单"和"黑名单命中"。这里用 _matches_blocked() 补判一次，
    让 force_deny 能精确覆盖 fork bomb / mkfs / dd 写裸设备 / Windows
    用户目录重定向 等极端危险操作。
    """
    # 延迟 import 避免循环依赖（shell_safety 被 schema 模块引用）
    from src.tools.schema import SideEffectsOverride

    command = args.get("command", "") or ""
    if not command.strip():
        return SideEffectsOverride(destructive=True)  # 空命令保守视为破坏性

    command_lowered = command.lower()

    # settings 的白名单/黑名单生效（S4 / M1）
    settings = _settings_or_none()
    allowed = load_allowed_commands(settings)
    blocked = load_blocked_patterns(settings)

    # ① 黑名单优先：force_deny 硬底线
    hit = _matches_blocked(command_lowered, blocked)
    if hit:
        return SideEffectsOverride(force_deny=True)

    # ② 写文件信号：重定向 / sed w / sort -o / tee / git config --global
    #    （白名单首词也拦——重定向目标无法静态判定是否越出工作区）
    write_signal = has_write_signal(command)

    # ③ classify_command 三态 → 基础 destructive 标签
    decision, _reason = classify_command(
        command,
        allowed=allowed,
        blocked_patterns=blocked,
    )
    destructive = write_signal or decision != "auto"

    # ④ 自进化护栏·硬底线（任何模式拒绝，含 full_access）：
    #    写/动 .git/ 内部（毁提交历史——纯读 cat .git/config 仍放行）；
    #    写/删 .self-evolve/（毁备份与验证状态——防绕过 respawn 防跳步；
    #    从备份 cp 回滚不受影响）。
    if _GIT_INTERNALS_RE.search(command_lowered) and (write_signal or decision != "auto"):
        return SideEffectsOverride(force_deny=True)
    if ".self-evolve" in command_lowered and (
        write_signal or _BACKUP_DESTROY_RE.search(command_lowered)
    ):
        return SideEffectsOverride(force_deny=True)

    # ⑤ 自进化护栏·强制审批（full_access 的放行也被顶掉）：
    #    命令提到仓库路径 + 写信号或非纯读形态
    mentions_repo = _mentions_repo_path(command_lowered)
    if mentions_repo and (write_signal or decision != "auto"):
        return SideEffectsOverride(destructive=True, force_approval=True)

    return SideEffectsOverride(destructive=destructive)


def _settings_or_none():
    """取全局 settings（classify 无 ctx settings 通道；失败返回 None 走默认表）。"""
    try:
        from config import get_settings
        return get_settings()
    except Exception:
        return None
