#!/usr/bin/env bash
# ============================================================
#  Fengcode 一键启动（Linux / macOS）
#  用法：
#    ./install.sh          # 安装依赖
#    ./install.sh run      # 装完并启动服务
#    ./install.sh cli      # 装完进入命令行对话
#    ./install.sh doctor   # 环境自检
#    ./install.sh mcp      # 以 MCP 服务方式运行（stdio）
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

C_RESET='\033[0m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'
C_RED='\033[0;31m'; C_CYAN='\033[0;36m'; C_GRAY='\033[0;90m'

echo ""
echo -e "  ${C_CYAN}========================================================${C_RESET}"
echo -e "    ${C_CYAN}Fengcode  ·  中文 AI Agent${C_RESET}"
echo -e "  ${C_CYAN}========================================================${C_RESET}"
echo ""

die()  { echo -e "  ${C_RED}✗ $*${C_RESET}" >&2; exit 1; }
ok()   { echo -e "  ${C_GREEN}✓${C_RESET} $*"; }
warn() { echo -e "  ${C_YELLOW}!${C_RESET} $*"; }

# ---------- 1. 找 Python ----------
PY=""
for cand in python3 python3.12 python3.11 python3.10 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            PY="$cand"
            break
        fi
    fi
done
[ -n "$PY" ] || die "没有找到 Python 3.10 或更高版本。请先安装。
    Debian/Ubuntu:  sudo apt install python3 python3-venv python3-pip
    macOS:          brew install python@3.12
    Fedora:         sudo dnf install python3 python3-pip"
echo -e "  [1/4] Python: $(command -v "$PY")  ($("$PY" --version 2>&1))"

# ---------- 2. 依赖（优先用虚拟环境，避免污染系统 Python） ----------
echo "  [2/4] 检查依赖..."
USE_VENV="${FENGCODE_NO_VENV:-0}"
VENV_DIR=".venv"

if [ "$USE_VENV" != "1" ]; then
    if "$PY" -c 'import fengcode' >/dev/null 2>&1; then
        ok "依赖已就绪（系统 Python）"
    else
        # 尝试直接装到用户目录（不需要 root）
        if "$PY" -m pip install --user --disable-pip-version-check -q -e . 2>/dev/null \
           && "$PY" -c 'import fengcode' >/dev/null 2>&1; then
            ok "依赖已安装（用户目录）"
        else
            warn "无法装到系统 Python，改用虚拟环境 ${VENV_DIR}"
            USE_VENV=1
        fi
    fi
fi

if [ "$USE_VENV" = "1" ]; then
    if [ ! -d "$VENV_DIR" ]; then
        echo "        创建虚拟环境..."
        "$PY" -m venv "$VENV_DIR" || die "创建虚拟环境失败（Debian 需装 python3-venv）"
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    PY="python"
    if ! python -c 'import fengcode' >/dev/null 2>&1; then
        echo "        安装依赖到虚拟环境..."
        python -m pip install --disable-pip-version-check -q --upgrade pip
        python -m pip install --disable-pip-version-check -q -e . \
            || python -m pip install --disable-pip-version-check -q -e . \
               -i https://pypi.tuna.tsinghua.edu.cn/simple \
            || die "依赖安装失败。请手动执行：  python -m pip install -e ."
    fi
    ok "依赖已就绪（虚拟环境 $VENV_DIR）"
fi

# ---------- 3. 模型配置提示 ----------
echo "  [3/4] 检查模型配置..."
if "$PY" -c 'import sys;from fengcode.config import get_manager as g;m=g();sys.exit(0 if any(m.resolve_api_key(p) for p in m.config.providers) else 1)' >/dev/null 2>&1; then
    ok "已配置可用模型"
else
    warn "还没有配置模型"
    echo ""
    echo -e "  ${C_YELLOW}--------------------------------------------------------${C_RESET}"
    echo -e "    ${C_YELLOW}首次使用提示${C_RESET}"
    echo -e "  ${C_YELLOW}--------------------------------------------------------${C_RESET}"
    echo -e "    ${C_GRAY}启动后打开界面 → 设置 → 模型供应商 → 添加${C_RESET}"
    echo -e "    ${C_GRAY}或在设置中手动填写模型供应商${C_RESET}"
    echo -e "  ${C_YELLOW}--------------------------------------------------------${C_RESET}"
    echo ""
fi

# ---------- 4. 执行动作 ----------
MODE="${1:-}"

case "$MODE" in
    doctor)
        echo "  [4/4] 环境自检"
        echo ""
        exec "$PY" -m fengcode.cli.main doctor
        ;;
    cli)
        echo "  [4/4] 进入命令行对话（Ctrl+Q 退出 / F1 帮助）"
        echo ""
        exec "$PY" -m fengcode.cli.main chat
        ;;
    mcp)
        echo "  [4/4] 以 MCP 服务方式运行（stdio）"
        echo ""
        exec "$PY" -m fengcode.cli.main mcp serve --transport stdio
        ;;
    run|"")
        echo "  [4/4] 启动服务"
        echo ""
        HOST_ARG="${FENGCODE_HOST:-}"
        PORT_ARG="${FENGCODE_PORT:-}"
        ARGS=()
        [ -n "$HOST_ARG" ] && ARGS+=(--host "$HOST_ARG")
        [ -n "$PORT_ARG" ] && ARGS+=(--port "$PORT_ARG")
        exec "$PY" -m fengcode.cli.main serve "${ARGS[@]}"
        ;;
    install)
        ok "安装完成"
        echo -e "  ${C_GRAY}启动： ./install.sh run${C_RESET}"
        ;;
    *)
        # 其余参数透传给 serve
        exec "$PY" -m fengcode.cli.main serve "$@"
        ;;
esac
