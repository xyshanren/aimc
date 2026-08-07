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
# 注意: Ubuntu 文件路径是 /etc/os-release (短横线), 不是 /etc/os_release (下划线)
if [[ ! -f /etc/os-release ]] || ! grep -q "Ubuntu" /etc/os-release; then
    err "仅支持 Ubuntu. 检测到: $(cat /etc/os-release 2>/dev/null | head -3 || echo unknown)"
fi
. /etc/os-release
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
# 区分: hard-fail (安装必需) vs soft-warn (可选, 备份脚本等场景才用)
for cmd in git curl; do
    if ! command -v $cmd >/dev/null 2>&1; then
        err "缺失命令: $cmd (运行: sudo apt install -y $cmd)"
    fi
done
# sqlite3 CLI 仅 backup.sh 旧版用; v4.3.3+ backup.sh 改用 Python sqlite3 模块
# 保留 soft-warn, 给极简镜像部署提供提示
if ! command -v sqlite3 >/dev/null 2>&1; then
    warn "sqlite3 CLI 未装 — v4.3.3+ backup.sh 已切到 Python, 可忽略"
fi
info "基础命令: git / curl OK"

# 4. systemd user 实例检查
# v4.3.5: LXC 容器 / 极简镜像常无 dbus, 设置 SYSTEMD_USER_OK 标志让后续步骤优雅降级
SYSTEMD_USER_OK=true
if ! systemctl --user status >/dev/null 2>&1; then
    warn "systemctl --user 不可用 — LXC 容器 / 极简镜像常见 (无 dbus socket)"
    warn "  LXC 开发模式: cd $AIMC_HOME && .venv/bin/python main.py"
    warn "  真机部署后: 重跑 install.sh 启用 systemd"
    SYSTEMD_USER_OK=false
fi

# === 部署路径检测 (v4.3.6) ===
# 优先级: 当前目录 (有 .git) > AIMC_HOME env > $HOME/aimc (有 .git) > clone
AIMC_REPO="${AIMC_REPO_URL:-}"

if [[ -d ".git" ]]; then
    # 在仓库目录内跑 install.sh, 就地安装 (推荐)
    AIMC_HOME="$(pwd)"
    info "检测到仓库目录: $AIMC_HOME (就地安装)"
elif [[ -n "${AIMC_HOME:-}" ]] && [[ -d "$AIMC_HOME/.git" ]]; then
    info "使用 AIMC_HOME=$AIMC_HOME"
elif [[ -d "$HOME/aimc/.git" ]]; then
    AIMC_HOME="$HOME/aimc"
    info "使用默认路径: $AIMC_HOME"
else
    # 没找到仓库, 走 clone 路径
    AIMC_HOME="${AIMC_HOME:-$HOME/aimc}"
    if [[ -z "$AIMC_REPO" ]]; then
        err "没检测到仓库目录, 也没设 AIMC_REPO_URL"
        echo "  提示:"
        echo "  1. 在仓库目录跑: cd /path/to/aimc && bash scripts/deploy/install.sh"
        echo "  2. 或设置仓库: export AIMC_REPO_URL=git@github.com:you/aimc.git"
        echo "  3. 或手动 clone: git clone <repo> $AIMC_HOME"
    fi
    info "Clone 仓库 -> $AIMC_HOME"
    git clone "$AIMC_REPO" "$AIMC_HOME" || err "git clone 失败: $AIMC_REPO"
    cd "$AIMC_HOME"
    AIMC_HOME="$(pwd)"
fi
info "AIMC_HOME=$AIMC_HOME"

# === 2. venv 创建 ===
if [[ ! -d "$AIMC_HOME/.venv" ]]; then
    info "创建 venv: $AIMC_HOME/.venv"
    python3 -m venv "$AIMC_HOME/.venv" || err "venv 创建失败"
fi

# === 3. 装依赖 ===
# v4.3.4: 去掉 -q, 让用户看到实时进度条
# (v4.3.3 用 -q 抑制, 但 LXC minimal image 装依赖慢, 用户看不出在动)
info "装 Python 依赖 (可能 1-10 min, 看终端进度条)..."
echo "[INFO] 先升 pip (这一步快, 5-10s)..."
"$AIMC_HOME/.venv/bin/pip" install --upgrade pip -q 2>&1 | tail -3
echo "[INFO] 装 requirements.txt (进度条实时显示)..."
# 不加 -q, pip 默认 progress bar on; 如果太卡可以手 ctrl-c 后用 -v 重跑看详情
"$AIMC_HOME/.venv/bin/pip" install -r "$AIMC_HOME/requirements.txt" \
    || err "pip install 失败"

# === 4. 复制 .env 模板 ===
if [[ ! -f "$AIMC_HOME/.env" ]]; then
    info "复制 env.template -> .env"
    cp "$AIMC_HOME/scripts/deploy/env.template" "$AIMC_HOME/.env"
    chmod 600 "$AIMC_HOME/.env"
    info "设 .env 权限为 600 (仅 owner 可读写, 保护 API key)"
    warn "请现在编辑 $AIMC_HOME/.env 填入 9 个厂商 API key + AIMC_MASTER_KEY"
    warn "  生成 master_key: python3 $AIMC_HOME/scripts/gen_master_key.py"
else
    info ".env 已存在 — 跳过 (如需重置请手动 rm)"
    # 即使已存在, 也确保权限 (防止 user 误操作改宽)
    chmod 600 "$AIMC_HOME/.env" 2>/dev/null && info "已加固 .env 权限为 600" || true
fi

# === 5. 准备数据目录 ===
mkdir -p "$AIMC_HOME/data"
info "数据目录: $AIMC_HOME/data"

# === 6. 安装 systemd unit (user 级) ===
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"
# v4.3.6: sed 替换 %h/aimc 为实际 $AIMC_HOME (不用 %h, 避免 path 假设)
sed "s|%h/aimc|$AIMC_HOME|g" "$AIMC_HOME/scripts/deploy/aimc.service" > "$SYSTEMD_USER_DIR/aimc.service"
info "systemd unit 已生成: $SYSTEMD_USER_DIR/aimc.service (WorkingDirectory=$AIMC_HOME)"

# 重新加载 + 启用 (不立即 start, 因为 .env 还没填)
# v4.3.5: LXC 容器 systemctl --user 不可用, daemon-reload 会报 "Failed to connect to bus"
if [[ "$SYSTEMD_USER_OK" == "true" ]]; then
    systemctl --user daemon-reload
    info "systemd unit 已安装: ~/.config/systemd/user/aimc.service"
else
    warn "unit 文件已复制到 ~/.config/systemd/user/aimc.service 但未 daemon-reload"
    warn "  (LXC 无 dbus, 真机部署后重跑 install.sh 启用)"
fi

# === 7. enable-linger (服务在登出后仍跑) ===
# v4.3.5: 同上, LXC 容器下 loginctl 也常常不可用, graceful skip
if [[ "$SYSTEMD_USER_OK" != "true" ]]; then
    warn "loginctl 不可用 (LXC 容器) — 真机部署后手动: sudo loginctl enable-linger $USER"
elif command -v loginctl >/dev/null 2>&1; then
    if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
        info "启用 linger (用户登出后服务继续跑) — 需要 sudo"
        sudo loginctl enable-linger "$USER" \
            && info "linger 已启用 OK" \
            || warn "linger 启用失败 — 服务在 SSH 登出后会停"
    else
        info "linger 已启用 OK"
    fi
else
    warn "loginctl 命令不存在 — 跳过 linger 检查"
fi

# === 8. 启动 (可选 — 默认不启, 让用户填完 .env 后再启) ===
# v4.3.5: LXC 容器下 systemctl --user 不可用, 改提示手动跑 python
if [[ "${AIMC_AUTOSTART:-0}" == "1" ]]; then
    if [[ "$SYSTEMD_USER_OK" == "true" ]]; then
        info "启动服务 (AIMC_AUTOSTART=1)"
        systemctl --user enable --now aimc.service
        sleep 2
        systemctl --user status aimc.service --no-pager || true
    else
        warn "AIMC_AUTOSTART=1 但 systemd --user 不可用 — LXC 手动跑:"
        warn "  cd $AIMC_HOME && .venv/bin/python main.py"
    fi
else
    info "未自动启动 (AIMC_AUTOSTART=0, 默认)"
    if [[ "$SYSTEMD_USER_OK" == "true" ]]; then
        info "  启动: systemctl --user enable --now aimc.service"
        info "  查日志: journalctl --user -u aimc -f"
    else
        info "  LXC 开发模式: cd $AIMC_HOME && .venv/bin/python main.py"
        info "  真机部署后: systemctl --user enable --now aimc.service"
    fi
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