# 监控 AIMC (job 健康 + shadow log + 限额)

> 受众：日常 / 每周看 AIMC 状态的人
> 前提：[user/dashboard.md §数据新鲜度](../user/dashboard.md#3-数据新鲜度) + [user/operations.md](../user/operations.md)

## 1. 数据新鲜度（job_health）

Dashboard "数据新鲜度" 卡片显示 ~41 个 APScheduler job 的健康度。

### 健康度等级

- 🟢 **healthy**：上次成功 < 1 天前
- 🟡 **warning**：1-3 天前
- 🔴 **stale**：> 3 天前
- ⚪ **unknown**：从没成功过

### 排查 🔴 异常的 job

```bash
# 1. 看具体 job 的 last_error_msg
sqlite3 data/aimc.db "
  SELECT job_id, last_error_at, last_error_msg, last_success_at
  FROM job_health
  WHERE health = 'stale' OR last_error_msg IS NOT NULL
"

# 2. 对应 source 文件里找 fetch 逻辑
ls intelligence/sources/
# e.g. intelligence/sources/glm.py

# 3. 手动跑那个 source
python3 -c "
from intelligence.sources.glm import GLMSource
import asyncio
async def go():
    s = GLMSource()
    items = await s.fetch()
    print(items[:3])
asyncio.run(go())
"

# 4. 修复后重启
systemctl --user restart aimc
```

## 2. Shadow Log（建议系统决策数据）

方案A 的 `suggest_changes.py --apply` 每次跑时同步写 `intelligence/auto_apply_shadow.log`。

```bash
# 看最近 100 条
tail -100 intelligence/auto_apply_shadow.log

# 格式: "model X: tier_change T1 -> T2 (conf=0.85) 理由..."
# 是 "如果自动应用会改哪些" 的记录

# 累积够 30 天后, 对比人工批准率
wc -l intelligence/auto_apply_shadow.log
```

启动真自动应用（P2-19）条件：
- shadow log 累积 ≥ 30 天
- 人工批准率 ≥ 80%（你 review 周报建议时点了 "批准" ≥ 80%）

详见 [user/operations.md §Shadow Log → 自动应用](../user/operations.md#shadow-log-自动应用准备)。

## 3. 限额监控

```bash
# 看哪些 account 接近限额
sqlite3 data/aimc.db "
  SELECT a.account_name, q.dimension, u.used_value, q.limit_value,
         ROUND(100.0 * u.used_value / q.limit_value, 1) AS pct
  FROM account_quota_usage u
  JOIN account_quotas q ON u.account_id=q.account_id AND u.dimension=q.dimension
  JOIN accounts a ON a.id=u.account_id
  WHERE u.used_value > 0.7 * q.limit_value
  ORDER BY pct DESC
"

# 看 cooldown 中 account (P1-15 自动 mark)
sqlite3 data/aimc.db "
  SELECT a.account_name, COUNT(*) AS cd_count
  FROM cooldown_state s
  JOIN accounts a ON a.id=s.account_id
  WHERE s.cooldown_until > datetime('now')
  GROUP BY a.account_name
  ORDER BY cd_count DESC
"
```

## 4. 关键指标

| 指标 | 来源 | 阈值 |
|------|------|------|
| Cache 命中率 | `/dashboard/api/cache/stats` | < 5% 检查 system prompt 是否太动态 |
| 上游 429 次数 | `journalctl \| grep 429` | > 5/小时 加账号或升级 Plan |
| 数据新鲜度 🔴 比例 | dashboard 卡片 | > 10% 看具体 job 失败原因 |
| 限额接近 100% | 上面的 SQL | 看是否要加账号或调配额 |
| Shadow log 增长 | `wc -l ...shadow.log` | 期望 ≈ 每周 4 条建议 |

## 5. 报警阈值（.env）

```bash
AIMC_PRICE_ALERT_THRESHOLD_PCT=30.0   # 价格波动 > 30% 告警 (%)
AIMC_BARK_KEY=...                    # Bark iOS push
AIMC_FEISHU_WEBHOOK_URL=...          # 飞书机器人
AIMC_SERVERCHAN_KEY=...             # Server酱
```

详见 [.env.example §告警](../../.env.example)。

## 6. 监控 cron（推荐）

```bash
# 每周日凌晨 3 点统计 db 健康度, 发到 Bark
0 3 * * 0 echo "{\"text\":\"AIMC weekly stats: $(sqlite3 /home/lxc/aimc/data/aimc.db 'SELECT COUNT(*) FROM accounts, channels;')...\"}" | curl -X POST "https://api.day.app/$AIMC_BARK_KEY" -H 'Content-Type: application/json' -d @-
```

## 故障排查（监控阶段）

| 症状 | 检查 |
|------|------|
| 某个 source 一直 🔴 | 看 `intelligence/sources/<source>.py` + 手动跑 |
| 限额接近 100% | 加账号 或 在 Dashboard 运维页「账号管理」卡（限额预警）查看后调 `account_quotas` |
| 上游 429 频繁 | `journalctl \| grep 429 \| tail -50` 看是哪个渠道 |
| 数据新鲜度全 🟡 | 看 `intelligence/scheduler.py` 是否启动 + db 可写 |
| 周报没收到 Bark | `.env AIMC_BARK_KEY` 对吗 / journal 看 `alerter` 模块 |
