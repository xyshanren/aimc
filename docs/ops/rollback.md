# 回滚 / 撤销指南

> 受众：迁移失败 / 改错 / 上游故障需要回滚的人
> 前提：操作前必做 [backup.md](backup.md) 备份（虽然本文档暂未拆出来, 见 [monitor.md §备份](monitor.md#备份)）

## 1. db 备份 / 还原

```bash
# 备份
sqlite3 data/aimc.db ".backup /backup/aimc-$(date +%Y%m%d-%H%M%S).db"

# 还原
cp /backup/aimc-YYYYMMDD-HHMMSS.db data/aimc.db
systemctl --user restart aimc
```

**注意**：db 还原后 **代码版本必须匹配**（旧 db schema 可能跟新代码不兼容）。

## 2. 代码回滚

```bash
# 查看近期 commit
git log --oneline -10

# 回滚到上一个稳定 commit
git reset --hard <commit-hash>
systemctl --user restart aimc
python -m pytest tests/ -v
```

## 3. tier 调整回滚（方案A 误操作）

如果 dashboard "一键应用" 误改了 model tier，**立即回滚**：

### 方式 A: 通过 db 直接还原（推荐）

```bash
# 1. 找建议历史 (applied 的有 applied_at)
sqlite3 data/aimc.db "
  SELECT model_id, change_type, current_value AS old_tier,
         suggested_value AS new_tier, applied_at, reviewed_by
  FROM model_change_suggestions
  WHERE status = 'applied'
  ORDER BY applied_at DESC LIMIT 10
"

# 2. 手动还原 (把 new_tier 改回 old_tier)
# 例: 把 deepseek-v4-pro 从 T1 改回 T2
sqlite3 data/aimc.db "
  UPDATE model_registry SET tier = 'T2'
  WHERE model_id = 'deepseek-v4-pro'
"

# 3. 触发 cache refresh
curl -X POST http://localhost:8080/dashboard/api/cache/refresh
```

### 方式 B: 写回滚脚本（批量回滚多个）

```bash
cat > /tmp/rollback_tiers.py << 'PYEOF'
"""回滚最近 N 个 applied tier 调整"""
import sqlite3
from datetime import datetime

conn = sqlite3.connect("~/aimc/data/aimc.db")
c = conn.cursor()
# 找最近 24h 内 applied 的 tier_change
c.execute("""
    UPDATE model_registry SET tier = (
        SELECT current_value FROM model_change_suggestions s
        WHERE s.model_id = model_registry.model_id
          AND s.status = 'applied'
          AND s.change_type = 'tier_change'
        ORDER BY s.applied_at DESC LIMIT 1
    )
    WHERE model_id IN (
        SELECT model_id FROM model_change_suggestions
        WHERE status = 'applied' AND change_type = 'tier_change'
          AND applied_at > datetime('now', '-1 day')
    )
""")
# 同步更新 suggestions 表 (rollback 状态)
c.execute("""
    UPDATE model_change_suggestions
    SET status = 'rolled_back'
    WHERE status = 'applied' AND change_type = 'tier_change'
      AND applied_at > datetime('now', '-1 day')
""")
conn.commit()
print(f"回滚 {c.rowcount} 条 tier 调整")
conn.close()
PYEOF
python3 /tmp/rollback_tiers.py
curl -X POST http://localhost:8080/dashboard/api/cache/refresh
```

> 注：P2-19 真自动应用时会先建 `model_change_audit` 表（备份 tier 历史），rollback 会更简单。

## 4. settings_runtime 改坏

```bash
# score weights 改坏 -> 还原默认
sqlite3 data/aimc.db "
  DELETE FROM settings_runtime WHERE key = 'router_score_weights'
"
curl -X POST http://localhost:8080/dashboard/api/cache/refresh
# router 用默认值 (0.4/0.3/0.2/0.05/0.05)
```

## 5. accounts 表改坏（ciphertext）

```bash
# 找 master key 备份 (如果用户备份过 .env)
cp ~/.backup-aimc-env-XXX .env

# 如果没有备份, 只能重加账号:
# Dashboard → "账号管理" → 重新填 API Key
```

## 6. scheduler 坏了（job 全跑不起来）

```bash
# 看具体错误
journalctl --user -u aimc -n 50 | tail -30

# 手动触发某个 job
python3 -c "
from intelligence.scheduler import _run_provider_batch
import asyncio
asyncio.run(_run_provider_batch('deepseek', []))
" 2>&1 | tail -10

# 如果是 import 错误: 升级代码
git pull
pip install -r requirements.txt
systemctl --user restart aimc
```

## 7. db 损坏（不能启动）

```bash
# 症状: 启动报 "database disk image is malformed"
sqlite3 data/aimc.db ".timeout 5000" "PRAGMA integrity_check"
# 期望: "ok"; 否则报 corruption 详情

# 还原 (有备份)
cp /backup/aimc-pre-corruption.db data/aimc.db

# 如果无备份: 试修复 (SQLite 提供)
sqlite3 data/aimc.db ".recover" | sqlite3 data/aimc.db
# 但 corruption 严重时这也会失败, 只能 reset_db + 重新配置
```

**reset_db 重新配置**（最差情况）：

```bash
# 1. 备份当前损坏 db (即使损坏也留个证据)
cp data/aimc.db /backup/aimc-corrupted-$(date +%Y%m%d).db

# 2. 删 db
rm data/aimc.db

# 3. 重新建 (按 [migration.md](migration.md) 完整 7 步)
python db/init_db.py
# 跑所有 migration
# 重新跑 scripts/manual_import.py 加账号

# 4. 跑测试确认
python -m pytest tests/ -v
```

## 8. 故障排查：哪些操作可以回滚？

| 操作 | 可回滚？ | 回滚方法 |
|------|---------|----------|
| 改 score weights | ✅ | DELETE settings_runtime.router_score_weights |
| 改 tier (方案A apply) | ✅ | UPDATE model_registry SET tier=... |
| 加账号 | ❌ (加密, 无备份难删) | 重新加 (不冲突) |
| 删账号 | ⚠️ 部分 (model_group_members 引用) | 先 update group_id 再删 |
| 跑 migration | ✅ (用 db 备份) | `cp backup.db aimc.db` |
| 升级代码 (git pull) | ✅ | `git reset --hard <commit>` |
| 改 .env | ✅ | `cp backup-env .env` |
| 改 cron | ✅ | `crontab -e` 编辑 |

## 9. 预防：迁移前必做

```bash
# 标准迁移前 checklist
1. sqlite3 data/aimc.db ".backup /backup/aimc-pre-$(date +%Y%m%d-%H%M%S).db"
2. cp .env ~/.backup-aimc-env-$(date +%Y%m%d)
3. git log --oneline -5  # 记录当前 commit hash
4. git status  # 确认 working tree clean
5. python -m pytest tests/ -v  # 跑通基线
6. 现在可以跑 migration 了
```
