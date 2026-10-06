#!/usr/bin/env bash
# ============================================================================
# Hermes-Ma 一键环境搭建 + 启动脚本
#
# 用法:
#   bash setup.sh              # 交互式：缺环境建环境、缺配置配模型、问启动方式
#   bash setup.sh web          # 跳过询问直接启动 Web (http://127.0.0.1:8000)
#   bash setup.sh cli          # 跳过询问直接启动 CLI
#   bash setup.sh --reconfig   # 强制重新配置 config.yaml（模型三要素）
#
# 行为（全部幂等，可反复运行）:
#   1. 探测 Python 环境: conda 的 hermes_ma/hermes-ma（命名变体，精确名
#      优先）→ ./venv|.venv → 已激活的当前环境
#   2. 都没有 → 交互选择 conda / venv 创建（默认 venv，无外部依赖）
#   3. 依赖缺失才安装（探测 fastapi/openai/rich，避免每次重复 pip）
#   4. config.yaml 缺失（或 --reconfig）→ 交互式配置 LLM 三要素 + 连通实测
#   5. 启动 Web 或 CLI
# ============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

ENV_NAME="hermes_ma"
MODE="${1:-ask}"

# ---------- 输出助手 ----------
info()  { printf '\033[36m[setup]\033[0m %s\n' "$*"; }
ok()    { printf '\033[32m[ ok ]\033[0m %s\n' "$*"; }
warn()  { printf '\033[33m[warn]\033[0m %s\n' "$*"; }
fail()  { printf '\033[31m[fail]\033[0m %s\n' "$*"; }

# python 可执行文件探测（跨平台：Linux/macOS 在 bin/，Windows Git Bash 在 Scripts/）
py_of_env() {  # $1=环境根目录
  local p
  for p in "$1/bin/python" "$1/bin/python3" "$1/Scripts/python.exe" "$1/python.exe"; do
    [ -x "$p" ] && { printf '%s' "$p"; return 0; }
  done
  return 1
}

find_conda() {
  local c
  for c in "$(command -v conda 2>/dev/null)" \
           "$HOME/miniconda3/Scripts/conda.exe" "$HOME/miniconda3/bin/conda" \
           "$HOME/anaconda3/Scripts/conda.exe" "$HOME/anaconda3/bin/conda" \
           "/c/ProgramData/miniconda3/Scripts/conda.exe" "/c/ProgramData/anaconda3/Scripts/conda.exe"; do
    [ -n "$c" ] && [ -x "$c" ] && { printf '%s' "$c"; return 0; }
  done
  return 1
}

# ---------- 1. 探测既有环境 ----------
PY=""

# a) conda 环境表：匹配本项目相关环境名（hermes_ma / hermes-ma 命名变体；
#    精确 hermes_ma 优先，命中即取 env 路径直取 python，不依赖 activate）
if CONDA="$(find_conda)"; then
  ENV_NAME_FOUND=""
  for CAND in "$ENV_NAME" "hermes-ma"; do
    ENV_PATH="$("$CONDA" env list 2>/dev/null | awk -v n="$CAND" '$1==n {print $NF; exit}')"
    if [ -n "${ENV_PATH:-}" ] && PY="$(py_of_env "$ENV_PATH")"; then
      ENV_NAME_FOUND="$CAND"
      break
    fi
  done
  if [ -n "$ENV_NAME_FOUND" ]; then
    ok "发现 conda 环境 $ENV_NAME_FOUND: $ENV_PATH"
  fi
fi

# b) 项目内 venv
if [ -z "$PY" ]; then
  for v in "$ROOT/venv" "$ROOT/.venv"; do
    if PY="$(py_of_env "$v")"; then ok "发现 venv 环境: $v"; break; fi
  done
fi

# c) 已激活的当前环境（conda activate / venv source 后运行本脚本；
#    conda 环境名同样认 hermes_ma / hermes-ma 变体）
if [ -z "$PY" ]; then
  CUR="$(command -v python 2>/dev/null || true)"
  if [ -n "$CUR" ]; then
    CUR_ENV="${CONDA_DEFAULT_ENV:-}"
    if [ "$CUR_ENV" = "$ENV_NAME" ] || [ "$CUR_ENV" = "hermes-ma" ] || [ -n "${VIRTUAL_ENV:-}" ]; then
      PY="$CUR"
      ok "使用已激活的当前环境: $CUR${CUR_ENV:+ ($CUR_ENV)}"
    fi
  fi
fi

# ---------- 2. 没有环境 → 创建 ----------
if [ -z "$PY" ]; then
  info "未找到相关环境（hermes_ma / hermes-ma 的 conda 环境、项目 venv 均未发现）"
  CHOICE="v"
  if [ -n "${CONDA:-}" ]; then
    read -r -p "用哪种方式创建？(c)onda / (v)env [默认 v]: " CHOICE
    CHOICE="${CHOICE:-v}"
  else
    info "未检测到 conda，使用 venv（python -m venv）"
  fi
  if [ "$CHOICE" = "c" ] || [ "$CHOICE" = "conda" ]; then
    info "创建 conda 环境 $ENV_NAME（python 3.11）..."
    "$CONDA" create -y -n "$ENV_NAME" python=3.11 || { fail "conda create 失败"; exit 1; }
    ENV_PATH="$("$CONDA" env list 2>/dev/null | awk -v n="$ENV_NAME" '$1==n {print $NF; exit}')"
    PY="$(py_of_env "$ENV_PATH")" || { fail "conda 环境创建后仍未找到 python"; exit 1; }
  else
    BASE_PY="$(command -v python python3 2>/dev/null | head -1 || true)"
    [ -z "$BASE_PY" ] && { fail "系统无 python，请先安装 Python ≥3.10 或使用 conda"; exit 1; }
    info "创建 venv → ./venv ..."
    "$BASE_PY" -m venv "$ROOT/venv" || { fail "venv 创建失败"; exit 1; }
    PY="$(py_of_env "$ROOT/venv")"
  fi
  ok "环境就绪: $PY"
fi

PY_VER="$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
info "Python $PY_VER ($PY)"

# ---------- 3. 依赖（幂等：探测关键包，缺才安装） ----------
if "$PY" -c "import fastapi, openai, rich, pydantic, httpx, yaml, duckduckgo_mcp_server" >/dev/null 2>&1; then
  ok "依赖已就绪（跳过安装）"
else
  info "安装依赖（requirements.txt，含 fastembed/sqlite-vec 首次较大，请耐心）..."
  "$PY" -m ensurepip --upgrade >/dev/null 2>&1 || true
  "$PY" -m pip install --upgrade pip >/dev/null 2>&1 || true
  "$PY" -m pip install -r requirements.txt || { fail "依赖安装失败，请检查网络/镜像源"; exit 1; }
  ok "依赖安装完成"
fi

# ---------- 4. config.yaml（交互式配置 + 连通实测） ----------
write_llm_config() {  # $1=key $2=base_url $3=model —— 用 python+yaml 写，sed 对复杂值不可靠
  LLM_KEY="$1" LLM_URL="$2" LLM_MODEL="$3" "$PY" - <<'PYEOF'
import os
import yaml

path = "config.yaml"
cfg = {}
try:
    cfg = yaml.safe_load(open(path, encoding="utf-8")) or {}
except FileNotFoundError:
    base = yaml.safe_load(open("config.example.yaml", encoding="utf-8")) or {}
    cfg = dict(base)
cfg["openai_api_key"] = os.environ["LLM_KEY"]
cfg["openai_base_url"] = os.environ["LLM_URL"]
cfg["llm_model_name"] = os.environ["LLM_MODEL"]
with open(path, "w", encoding="utf-8") as f:
    yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
print("written")
PYEOF
}

write_web_proxy() {  # $1=代理地址（空串=直连）—— 写 config.yaml 的 web_proxy，
                     # MCP 子进程（含 ddg-search）运行时自动继承为 HTTP(S)_PROXY
  WEB_PROXY="$1" "$PY" - <<'PYEOF'
import os
import yaml

path = "config.yaml"
try:
    cfg = yaml.safe_load(open(path, encoding="utf-8")) or {}
except FileNotFoundError:
    base = yaml.safe_load(open("config.example.yaml", encoding="utf-8")) or {}
    cfg = dict(base)
cfg["web_proxy"] = os.environ["WEB_PROXY"].strip()
with open(path, "w", encoding="utf-8") as f:
    yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
print("written")
PYEOF
}

NEED_CONFIG=0
[ ! -f config.yaml ] && NEED_CONFIG=1
[ "${1:-}" = "--reconfig" ] && NEED_CONFIG=1

if [ "$NEED_CONFIG" = "1" ]; then
  if [ ! -f config.yaml ]; then
    cp config.example.yaml config.yaml
    info "已从模板创建 config.yaml"
  fi
  info "配置 LLM（任何 OpenAI 兼容端点：OpenAI / DeepSeek / Ollama / vLLM / one-api…）"
  for ATTEMPT in 1 2 3; do
    read -r -p "  API Key (sk-...): " IN_KEY
    read -r -p "  Base URL [https://api.openai.com/v1]: " IN_URL; IN_URL="${IN_URL:-https://api.openai.com/v1}"
    read -r -p "  模型名 [gpt-4o-mini]: " IN_MODEL; IN_MODEL="${IN_MODEL:-gpt-4o-mini}"
    write_llm_config "$IN_KEY" "$IN_URL" "$IN_MODEL" >/dev/null || { fail "写入失败"; exit 1; }
    info "连通性实测（GET /models）..."
    if "$PY" run_health.py >/dev/null 2>&1; then
      ok "LLM 连通 ✓ ($IN_URL · $IN_MODEL)"
      break
    else
      warn "第 $ATTEMPT 次配置未通过连通测试（Key/URL/网络？）"
      [ "$ATTEMPT" = "3" ] && { warn "已保留该配置；可稍后 bash setup.sh --reconfig 重配"; }
      [ "$ATTEMPT" = "3" ] && break
      read -r -p "  重试配置？(Y/n): " RETRY; RETRY="${RETRY:-Y}"
      [ "${RETRY,,}" = "n" ] && break
    fi
  done

  # 2026-09-09: 代理配置 —— DDG 搜索等境外访问需要；留空=直连。
  # 回车保留现值；HERMES_WEB_PROXY 环境变量可预置（非交互/自动化场景）。
  CUR_PROXY="$("$PY" - <<'PYEOF'
import yaml
try:
    print((yaml.safe_load(open("config.yaml", encoding="utf-8")) or {}).get("web_proxy") or "")
except FileNotFoundError:
    print("")
PYEOF
)"
  info "网络代理（DDG 搜索等境外访问需要；留空=直连，形如 http://127.0.0.1:7890）"
  if [ -n "${HERMES_WEB_PROXY:-}" ]; then
    IN_PROXY="$HERMES_WEB_PROXY"
    info "  已从 HERMES_WEB_PROXY 读取代理"
  else
    read -r -p "  HTTP 代理 [$CUR_PROXY]: " IN_PROXY
    IN_PROXY="${IN_PROXY:-$CUR_PROXY}"
  fi
  write_web_proxy "$IN_PROXY" >/dev/null || { fail "代理写入失败"; exit 1; }
  [ -n "$IN_PROXY" ] && ok "代理已写入 web_proxy: $IN_PROXY" || ok "代理留空（直连）"
else
  ok "config.yaml 已存在（重新配置请: bash setup.sh --reconfig）"
fi

# ---------- 5. 启动 ----------
if [ "$MODE" != "web" ] && [ "$MODE" != "cli" ]; then
  read -r -p "启动 (w)eb 浏览器工作台 / (c)li 终端 [默认 w]: " PICK; PICK="${PICK:-w}"
  MODE="$([ "${PICK,,}" = "c" ] || [ "$PICK" = "cli" ] && echo cli || echo web)"
fi

if [ "$MODE" = "web" ]; then
  ok "启动 Web → http://127.0.0.1:8000 （Ctrl+C 停止）"
  exec "$PY" -m web_fastapi.main
else
  ok "启动 CLI（/help 查看命令，/exit 退出）"
  exec "$PY" main.py
fi
