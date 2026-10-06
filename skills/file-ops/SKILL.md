---
name: file-ops
description: 用 bash 读写改文件——读/写/改/复制移动删除/查找的命令形态、审批预期、Git Bash 路径规则与常见坑
---

# 文件操作（bash 统一通道）

文件工具已退役。工作区内一切读、写、改、复制、移动、删除、查找都走 `bash`。
首次做文件任务先加载本技能，再按下面的形态调用。

初始 cwd **已经是工作区根**——不要先 `cd`，直接用相对路径。

## 读

| 意图 | 命令 | 说明 |
|---|---|---|
| 整文件 | `cat path` | 短文件 |
| 头/尾 | `head -n 50 path` / `tail -n 50 path` | 先看结构 |
| 分页 | `sed -n '1,80p' path` | 长文件**必须分页**。bash 输出超约 8000 字符会被截断，一次 `cat` 大文件等于白读 |
| 行号 | `nl -ba path` 或 `sed -n '20,40p' path` | 改文件前先定位 |

一次没有就是没有，不要换路径名反复试。不确定目录结构先 `ls`（相对路径，不要 `ls /`）。

## 写

覆盖写用 heredoc（单引号 EOF，避免 shell 展开）：

```bash
cat > notes.md <<'EOF'
第一行
第二行
EOF
```

追加：`cat >> notes.md <<'EOF' ... EOF`，或 `echo '一行' >> notes.md`。

新建目录：`mkdir -p dir/sub`。

## 改（sed -i）

纯替换（推荐形态，会弹审批，`full_access` 直通）：

```bash
sed -i 's/旧文本/新文本/' file.txt
```

多处替换、多条命令**一律用 `&&` 串联，不用 `;`**（`;` 会掩盖中间失败的退出码，看起来成功其实没改完）：

```bash
sed -i 's/foo/bar/' a.txt && sed -i 's/baz/qux/' b.txt
```

标志限 `g` / `p` / `i` / 数字。`s///e`、`e` 命令、`-f` 脚本文件、`w/W/r/R`、块 `{}` 会被硬拒或强制审批——别用。

复杂多行、缩进敏感、或 sed 搞不定时改用：

```bash
python -c "from pathlib import Path; p=Path('f.txt'); t=p.read_text(encoding='utf-8'); p.write_text(t.replace('old','new'), encoding='utf-8')"
```

或 `perl -i -pe 's/old/new/g' file.txt`。

### 已知坑

- **CRLF**：Windows 检出的文件行尾是 `\r\n`。sed 的 `$` 锚点会失配。先 `file path` 或 `od -c path | head` 确认；需要时 `sed -i 's/\r$//' path` 再改，或用 python 读改写。
- **GBK 旧文件**：非 UTF-8 时 sed/python 默认编码会改不生效或乱码。用 `file path` 判断；python 显式 `encoding='gbk'` 或先转 UTF-8。
- **文件锁 / rename 失败**：Windows 上被编辑器、杀毒、本进程占用时，`sed -i` 的临时文件 rename 会失败。关掉占用方重试；不要立刻加大 timeout 死磕。

## 复制 / 移动 / 删除

```bash
cp src dst
cp -r src_dir dst_dir
mv src dst
rm -f file
rm -r dir
mkdir -p dir
```

不可逆操作（`rm -r`、覆盖重要文件）预期会弹审批。覆盖前先 `ls` 确认目标。

## 查找

```bash
ls -la dir
find . -name '*.py'          # 从 cwd 起，不要 find /
grep -n 'pattern' file
grep -r 'pattern' dir
rg 'pattern' dir             # 若可用
```

缩小范围，不要从根扫。超时（默认 30s）被杀后先收窄路径/通配，不要加大 timeout。

## 审批预期

| 形态 | before_changes | plan | full_access |
|---|---|---|---|
| 纯读（ls/cat/head/tail/grep/find 无写标志） | 放行 | 放行 | 放行 |
| 写信号（`>` `>>`、`sed -i`、`tee`、`sort -o`） | **弹审批** | 拒绝 | 直通 |
| 危险形态（`rm -rf /`、`sed s///e`、命令替换 `$(…)`） | 硬拒或审批 | 拒绝 | 硬拒仍拦 |

不要为了躲审批去改用别的绕过写法——会被拦得更死。

## 路径规则（Git Bash / MSYS，重要）

宿主机是 Windows，shell 是 Git Bash：

- 工作区内：相对路径（`src/foo.py`、`notes.md`）。cwd 已是工作区根。
- 工作区外：`D:/some/dir` 或 `/d/some/dir`。
- **严禁** `cd /`、`ls /`、`find /`、`grep -r x /`——`/` 是 MSYS 虚拟根（Git 安装目录），不是磁盘根，慢且扫不到用户文件。
- 不要用文件工具时代的 `/profile.md` 这种「必须以 / 开头」的虚拟路径；那套已经没了。

软边界（不是沙箱）：白名单只读命令零审批且**无路径检查**，`cat D:/任意` 可静默读。不要主动去读工作区外的隐私/系统目录。
