# 日常运维 (周报 / shadow log / 告警)

> 受众：每周 / 每月看一次 AIMC 状态的用户
> 前提：已部署 AIMC + Dashboard 可访问

## 每周必看（10 分钟）

### 1. 周一上午：Bark 推送的周报

AIMC 自动每周一 09:00 跑周报生成 + Bark 推送（含模型分组建议章节）。

**周报章节**（[scripts/generate_weekly_report.py](../../scripts/generate_weekly_report.py)）：
1. 本周统计：总请求 / token / 平均延迟
2. TOP models 按使用量
3. 趋势对比（vs 上周）
4. 告警汇总（v44_alerts）
5. **模型分组建议（方案A）** — 自动发现 tier 错配

**手动触发**：
```bash
python scripts/generate_weekly_report.py
cat reports/weekly-latest.md
```

### 2. 建议系统（方案A）

周报第 5 章节列出 4 条建议（如 deepseek-v4-pro 应升 T1、GLM-5.1 应升 T1 等）。

**操作**：
1. 看周报 → 进 Dashboard "建议中心"
2. 点 "批准" 或 "拒绝"
3. 已批的 → 点 "一键应用已批准"

**调参**：默认 confidence 阈值 0.0；想严格就调高（只应用高置信度建议）。

### 3. Shadow Log（自动应用准备）

```bash
# 看 shadow log（积累真自动应用决策数据）
tail -50 /home/lxc/aimc/intelligence/auto_apply_shadow.log

# 格式: 每次 suggest_changes --apply 跑时记录 "如果自动应用会改哪些"
# 当前是人工 review + apply, shadow 记录供未来 P2-19 真自动应用决策用
```

启动真自动应用条件（建议稳定期 1 个月后）：
- shadow log 累积 ≥ 30 天
- 人工批准率 ≥ 80%（你 review 周报时点了 "批准" ≥ 80% 的建议）

## 告警处理

AIMC 告警走 **Bark / 飞书 / Server酱**（`.env` 配置）。

**告警触发场景**：
- 价格波动 > 30%（`AIMC_PRICE_ALERT_THRESHOLD_PCT` 控制）
- NoActiveAccount（无可用 account）
- 4 类告警（v44_alerts）：80% / 100% 配额、cost rising 等
- 上游错误 5xx / 401/403（持续）

V4.4 告警（配额 / 无活跃账号 / 成本上涨）由 `v44_alerts_check` 定时任务每 6 小时
自动执行（2026-08-21 起注册到 scheduler，此前为死代码未运行）。

采集失败告警已分级静默（V4.8.1 起）：同一源首次失败告警后静默 30 分钟，连续失败
升级为静默 2 小时；源从 error 恢复时发独立的恢复告警（静默 1 小时）。

**采集源异常恢复**：
- 采集源连续失败 3 次后 `status` 置为 `"error"`，但 scheduler 不会跳过该源——
  下次调度仍会尝试，成功后自动把 `status` 恢复为 `"active"` 并清零 `error_count`
  （2026-08-21 起修复，此前 `status` 被置 error 后永不恢复）。
- 连续无变化的源会自适应降频（V4.8.3 起）：≥4 次降为 4 倍间隔，≥10 次降为 8 倍；
  检测到变化后自动恢复正常频率。
- 可在 Dashboard「采集源管理」卡片按 provider 禁用/启用源（V4.8.3 起，写 DB
  `status=disabled`，scheduler 跳过禁用源）。
- 如果某源持续失败，检查 `intelligence/sources/<source>.py` 的 fetch 逻辑或
  厂商页面是否改版。

**收到告警后**：
```bash
# 1. 看 journal
journalctl --user -u aimc --since "10 minutes ago" | grep -iE "error|warning"

# 2. 看具体错误
journalctl --user -u aimc -n 100 | tail -50

# 3. 如果是 quota 耗尽 (P1-15 自动处理)
# minimax TokenPlan 429 -> cooldown 60s 自动恢复
# 如果频繁: 升级 TokenPlan 或加账号

# 4. 如果是 channel 不可用
curl http://localhost:8080/dashboard/api/channels | python3 -m json.tool
```

## 限额监控

```bash
# 看当前所有 account 限额
sqlite3 data/aimc.db "SELECT account_id, dimension, limit_value, used_value FROM account_quotas_usage_view"
# (或 SELECT * FROM account_quota_usage)

# 看哪些 account 快用完了
sqlite3 data/aimc.db "
  SELECT a.account_name, q.dimension, u.used_value, q.limit_value,
         ROUND(100.0 * u.used_value / q.limit_value, 1) AS pct
  FROM account_quota_usage u
  JOIN account_quotas q ON u.account_id = q.account_id AND u.dimension = q.dimension
  JOIN accounts a ON a.id = u.account_id
  WHERE u.used_value > 0.5 * q.limit_value
  ORDER BY pct DESC
"
```

## 数据新鲜度

[Dashboard §数据新鲜度](dashboard.md#3-数据新鲜度) 卡片显示约 41 个 apscheduler job 的健康状态。

**怎么看**：
- 🟢 = job 正常
- 🟡 = 1-3 天没成功（数据可能过期）
- 🔴 = > 3 天没成功（job 死了）
- ⚪ = 从没跑过

**异常处理**：
```bash
# 手动跑那个 job 看错误
python -c "from intelligence.scheduler import _run_provider_batch; import asyncio; asyncio.run(_run_provider_batch('deepseek', [...]))"

# 或直接看对应源文件
# intelligence/sources/<source>.py 看 fetch 逻辑
```

## Shadow Log → 自动应用（稳定期）

稳定期 1 个月后启动 P2-19：

```bash
# 1. 看 shadow log 累积的数据
wc -l intelligence/auto_apply_shadow.log

# 2. 评估 "人工批准率"
#    数周报建议 → 你点了几个"批准"?
#    如果 ≥ 80% 一致 -> 可以启用

# 3. 启用 (待 P2-19 commit)
python intelligence/analytics/suggest_changes.py --auto-apply
# --auto-apply 自动 apply tier_change 且 confidence ≥ 0.9 的
# group_change / add_to_group 仍人工 review
```

详见 `intelligence/analytics/suggest_changes.py --help`（P2-19 实现后）。

## 月度操作

### 备份

```bash
# 1. SQLite 备份 (WAL 模式需要 .backup 命令)
sqlite3 data/aimc.db ".backup /backup/aimc-$(date +%Y%m%d).db"

# 2. .env 备份 (含 key, 别提交 git)
cp .env ~/.backup-aimc-env-$(date +%Y%m%d)

# 3. 配合 cron (每月 1 号)
cat > ~/.config/cron/aimc-backup.cron << 'EOF'
0 3 1 * * sqlite3 /home/lxc/aimc/data/aimc.db ".backup /backup/aimc-$(date +\%Y\%m\%d).db"
EOF
```

### 升级 AIMC

```bash
cd ~/aimc
git pull
source .venv/bin/activate
pip install -r requirements.txt
# V4.9 后: schema 变更走 ensure_migrations 自动跑, 不需要手动 migration
# 只有 db/migrations/ 下的独立脚本 (2026_08_* / 2026_09_* ...) 才需要手动跑
for m in db/migrations/*.py; do python $m --apply; done 2>/dev/null || true
systemctl --user restart aimc
# 跑测试确认
python -m pytest tests/ -v

# V4.9.1 部署验证 (必跑! 防止 APScheduler 静默挂掉)
sleep 5
# 1. 所有 job 都注册成功 (V4.9.1 新增 3 个)
grep 'Added job' /tmp/aimc.log | grep -E 'Channel 智能探活|Cooldown 过期清理|探活日志清理'
# 期望 3 条
# 2. scheduler 没因 pickle 失败 (lambda 回调) 静默挂掉
grep -i 'ValueError\|cannot be serialized' /tmp/aimc.log
# 期望空
```

### 升级账号加密（API-Sec-1）

如果之前是 plaintext 加密 key 升级到 Fernet（P0-5 已自动校验 master_key 格式）：

```bash
python scripts/migrate_encrypt_keys.py --apply
systemctl --user restart aimc
```

## V4.9.1 探活防护监控

`channel_health_probe` 是新加的 APScheduler job，**有 3 层防护**防止资源泄漏（吸取 Playwright 采集进程 OOM 教训）：

| 防护 | 监控方式 |
|------|----------|
| 共享 httpx.AsyncClient 单例 | `grep 'close_probe_client' /tmp/aimc.log`（应仅 shutdown 出现 1 次） |
| job hard timeout 60s | `grep 'channel_health_probe.*超时' /tmp/aimc.log`（超时时应出现） |
| account_health_log retention 7 天 | `sqlite3 data/aimc.db "SELECT COUNT(*) FROM account_health_log"`（应 < 数十万） |

**每 60s 探活触发条件**（不能太频繁）：

- cooldown_manager._state 里的 account 有 `next_probe_at <= now`
- `needs_human=False` 且 `manual_disabled_at` 为空
- `cooldown_manager.accounts_due_for_probe()` 返回列表

如果发现探活一直没跑：

```bash
# 1. job 注册了?
grep 'Added job' /tmp/aimc.log | grep -E 'Channel 智能探活'

# 2. 总开关是否开了?
grep AIMC_HEALTH_PROBE_ENABLED /home/lxc/aimc/.env

# 3. cooldown_manager 有没有待探活 account?
# (无代码检查入口, 直接重启服务看)
```

## 故障排查（运维阶段）

| 症状 | 排查命令 |
|------|----------|
| 收不到周报 Bark | `journalctl --user -u aimc \| grep -i bark` |
| 告警发不出 | 检查 `.env` `AIMC_BARK_KEY` / `AIMC_FEISHU_WEBHOOK_URL` |
| 限额监控不准 | 跑 `python scripts/sync_balance_quota.py`（如果存在）|
| 数据新鲜度全 🟡 | 看 shadow log + 看 intelligence/scheduler.py |
