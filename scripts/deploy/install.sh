#!/usr/bin/env bash
# ============================================================
# AIMC Ubuntu VM 一键安装脚本 (Phase 4 v4.3, 2026-08-06)
# ============================================================
# 目标: ~/aimc + user 级 systemd
# 设计: 不依赖 sudo (除 enable-linger), 全部在用户 home 下
# 用法:
#   bash install.sh                       # 默认 (从 git clone 仓库)
#   AIMC_REPO_URL=git@github.com:you/aimc.git bash install.sh  # 指定仓库
# ============================================================

set -euo pipefail

# === 颜色输出 ===
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${GREEN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err() { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }

# === 前置检查 ===
info "=== Phase 4 v4.3 AIMC 安装 ==="

# 1. OS 检查
if [[ ! -f /etc/os_release ]] || ! grep -q "Ubuntu" /etc/os_release; then
    err "仅支持 Ubuntu. 检测到: $(cat /etc/os-release 2>/dev/null | head -3 || echo unknown)"
fi
. /etc/os_release
info "OS: $PRETTY_NAME"

# 2. Python 版本
PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
if [[ "$PYTHON_MAJOR" -lt 3 ]] || [[ "$PYTHON_MAJOR" -eq 3 && "$PYTHON_MINOR" -lt 10 ]]; then
    err "Python 3.10+ 必需. 检测到: $PYTHON_VERSION"
fi
info "Python: $PYTHON_VERSION OK"

# 3. 依赖检查
for cmd in git curl sqlite3; do
    if ! command -v $cmd >/dev/null 2>&1; then
        err "缺失命令: $cmd (运行: sudo apt install -y $cmd)"
    fi
done
info "基础命令: git / curl / sqlite3 OK"

# 4. systemd user 实例检查
if ! systemctl --user status >/dev/null 2>&1; then
    warn "systemd --user 不可用 — 检查是否需要 loginctl enable-linger"
    warn "  尝试: sudo loginctl enable-linger $USER"
    warn "  或重新登录 (确保 XDG_RUNTIME_DIR 设置)"
fi

# === 部署路径 ===
AIMC_HOME="$HOME/aimc"
AIMC_REPO="${AIMC_REPO_URL:-https://github.com/<your-repo>/aimc.git}"

# === 1. Clone / 更新仓库 ===
if [[ -d "$AIMC_HOME/.git" ]]; then
    info "仓库已存在: $AIMC_HOME — 拉取最新"
    cd "$AIMC_HOME"
    git pull --rebase --autostash || warn "git pull 失败, 继续 (可能是未提交本地修改)"
else
    info "Clone 仓库 -> $AIMC_HOME"
    git clone "$AIMC_REPO" "$AIMC_HOME" || err "git clone 失败: $AIMC_REPO"
    cd "$AIMC_HOME"
fi

# === 2. venv 创建 ===
if [[ ! -d "$AIMC_HOME/.venv" ]]; then
    info "创建 venv: $AIMC_HOME/.venv"
    python3 -m venv "$AIMC_HOME/.venv" || err "venv 创建失败"
fi

# === 3. 装依赖 ===
info "装 Python 依赖 (可能 1-3 min)..."
"$AIMC_HOME/.venv/bin/pip" install --upgrade pip -q
"$AIMC_HOME/.venv/bin/pip" install -r "$AIMC_HOME/requirements.txt" -q \
    || err "pip install 失败"

# === 4. 复制 .env 模板 ===
if [[ ! -f "$AIMC_HOME/.env" ]]; then
    info "复制 env.template -> .env"
    cp "$AIMC_HOME/scripts/deploy/env.template" "$AIMC_HOME/.env"
    warn "请现在编辑 $AIMC_HOME/.env 填入 9 个厂商 API key + AIMC_MASTER_KEY"
    warn "  生成 master_key: python3 $AIMC_HOME/scripts/gen_master_key.py"
else
    info ".env 已存在 — 跳过 (如需重置请手动 rm)"
fi

# === 5. 准备数据目录 ===
mkdir -p "$AIMC_HOME/data"
info "数据目录: $AIMC_HOME/data"

# === 6. 安装 systemd unit (user 级) ===
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"
cp "$AIMC_HOME/scripts/deploy/aimc.service" "$SYSTEMD_USER_DIR/aimc.service"

# 重新加载 + 启用 (不立即 start, 因为 .env 还没填)
systemctl --user daemon-reload
info "systemd unit 已安装: ~/.config/systemd/user/aimc.service"

# === 7. enable-linger (服务在登出后仍跑) ===
if command -v loginctl >/dev/null 2>&1; then
    if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
        info "启用 linger (用户登出后服务继续跑) — 需要 sudo"
        sudo loginctl enable-linger "$USER" \
            && info "linger 已启用 OK" \
            || warn "linger 启用失败 — 服务在 SSH 登出后会停"
    else
        info "linger 已启用 OK"
    fi
fi

# === 8. 启动 (可选 — 默认不启, 让用户填完 .env 后再启) ===
if [[ "${AIMC_AUTOSTART:-0}" == "1" ]]; then
    info "启动服务 (AIMC_AUTOSTART=1)"
    systemctl --user enable --now aimc.service
    sleep 2
    systemctl --user status aimc.service --no-pager || true
else
    info "未自动启动 (AIMC_AUTOSTART=0, 默认)"
    info "  启动: systemctl --user enable --now aimc.service"
    info "  查日志: journalctl --user -u aimc -f"
fi

# === 9. 健康检查 ===
if curl -sf http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
    info "OK healthz OK"
else
    warn "healthz 未响应 — 可能服务未启 / .env 没填完"
fi

echo
info "=== 安装完成 ==="
echo "  代码:   $AIMC_HOME"
echo "  venv:   $AIMC_HOME/.venv"
echo "  配置:   $AIMC_HOME/.env (请现在编辑!)"
echo "  数据:   $AIMC_HOME/data"
echo "  启动:   systemctl --user enable --now aimc.service"
echo "  状态:   systemctl --user status aimc"
echo "  日志:   journalctl --user -u aimc -f"
echo