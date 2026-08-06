#!/usr/bin/env bash
# ============================================================
# AIMC 数据库备份脚本 (Phase 4 v4.3)
# ============================================================
# 用 SQLite .backup 命令做热备份 (WAL 模式安全)
# 不依赖 sqlite3 二进制 (用 sqlite3 模块, 跟 Python 同步)
# 用法:
#   bash backup.sh                        # 默认 ~/aimc/data/aimc.db → ~/aimc/backups/
#   bash backup.sh /path/to/aimc.db       # 指定源 db
# ============================================================

set -euo pipefail

# === 配置 ===
DB_PATH="${1:-$HOME/aimc/data/aimc.db}"
BACKUP_DIR="$HOME/aimc/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/aimc_${TIMESTAMP}.db"
KEEP_DAYS=14  # 保留 14 天

# === 检查 ===
if [[ ! -f "$DB_PATH" ]]; then
    echo "[FAIL] 数据库文件不存在: $DB_PATH" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"

# === 热备份 (SQLite 推荐方式) ===
# 用 sqlite3 .backup 命令安全备份 (WAL 检查点 + atomic rename)
echo "[INFO] 备份 $DB_PATH → $BACKUP_FILE"
sqlite3 "$DB_PATH" ".backup '$BACKUP_FILE'"

# 验证
BACKUP_SIZE=$(stat -c %s "$BACKUP_FILE" 2>/dev/null || stat -f %z "$BACKUP_FILE")
SOURCE_SIZE=$(stat -c %s "$DB_PATH" 2>/dev/null || stat -f %z "$DB_PATH")

if [[ "$BACKUP_SIZE" -lt 1000 ]]; then
    echo "[FAIL] 备份文件过小 ($BACKUP_SIZE bytes), 异常" >&2
    exit 1
fi

echo "[OK] 备份完成: $BACKUP_FILE ($BACKUP_SIZE bytes, 源 $SOURCE_SIZE bytes)"

# === 清理老备份 ===
DELETED=$(find "$BACKUP_DIR" -name "aimc_*.db" -mtime +$KEEP_DAYS -delete -print | wc -l)
if [[ "$DELETED" -gt 0 ]]; then
    echo "[INFO] 清理 $KEEP_DAYS 天前的备份: $DELETED 个"
fi

# === 列表 ===
echo
echo "=== 当前备份列表 (最近 5 个) ==="
ls -lt "$BACKUP_DIR"/aimc_*.db 2>/dev/null | head -5 || echo "(空)"

# === 提示 ===
echo
echo "=== cron 接入示例 (每天 03:00 跑) ==="
echo "  crontab -e"
echo "  0 3 * * * bash $HOME/aimc/scripts/deploy/backup.sh >> $HOME/aimc/backups/backup.log 2>&1"