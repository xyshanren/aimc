# 故障排查（Troubleshooting）

> 受众：AIMC 出问题时找原因
> 前提：先看 [operations.md](operations.md) 日常运维

## 1. 网关连不上 / 启动失败

### 症状：`curl http://localhost:8080/health` 连不上

```bash
# 1. service 状态
systemctl --user status aimc
# 或: ps aux | grep "python main.py"

# 2. 端口监听
ss -tlnp | grep 8080

# 3. 启动日志
journalctl --user -u aimc -n 50
# 或: python main.py 直接前台跑看错误
```

### 症状：`Address already in use`

```bash
# 找占用 8080 的进程
lsof -i :8080
# 或: ss -tlnp | grep 8080

# 杀掉
kill <PID>
```

## 2. 401 / 403 错误

### 症状：API 调用返 401 Unauthorized

```bash
# 1. 确认 .env 里 AIMC_API_KEY 设置了
grep AIMC_API_KEY .env

# 2. 确认 SDK 用的 key 跟 .env 一致
# Python: OpenAI(api_key="your-key")
# 注意: SDK 默认带 Bearer 前缀, gateway 期望 Bearer 前缀

# 3. 确认 .env 改了后重启了
systemctl --user restart aimc
```

### 症状：Dashboard 返 401

```bash
# 1. 确认 .env 里 AIMC_DASHBOARD_TOKEN 设置了
grep AIMC_DASHBOARD_TOKEN .env

# 2. 重新生成 + 重启
echo "AIMC_DASHBOARD_TOKEN=$(openssl rand -hex 32)" >> .env
systemctl --user restart aimc
```

### 症状：key 明明对，客户端侧却 401 —— 先端到端验证

`scripts/tmp_verify_hermes_key.sh` 从客户端视角验证 key + 组路由（读 .env 进变量再引用，**命令面不出现明文 key**）：

```bash
# Windows Git Bash → WSL 执行（hermes 等 WSL 侧客户端的场景）
MSYS_NO_PATHCONV=1 wsl bash /mnt/d/work/workspace/Qoder/aimc/scripts/tmp_verify_hermes_key.sh
# 传参: 第 1 个 = .env 路径, 第 2 个 = 网关 base_url（默认 http://127.0.0.1:8080/v1）
```

三条路径全 200 即 key/鉴权/组路由全通：`GET /v1/models`（组列表）+ `POST tier:light` + `POST tier:balanced`（真实路由链）。

⚠️ **别把明文 key 直接写进 `-H 'Authorization: Bearer ...'` 命令行测鉴权**——本机 Agent 安全钩子会把命令里的密钥替换成 `<SECRET_xxx>` 占位符原样执行，产生假 401（2026-09-21 实测踩坑：key 对库 sha256 逐字节匹配却全部 401，verbose 输出里出现占位符才定位）。一律变量间接引用。

WSL 内探 Windows 主机服务（Ollama 等 NAT 隔离场景）用 `scripts/tmp_gw_probe.sh`：打印 WSL 默认网关并探测 11434 可达性；网关 IP 可达后可填 `HERMES_OLLAMA_HOST=http://<gw-ip>:11434` 让 hermes quickstart 识别 Windows 侧 Ollama（网关 IP 随 WSL 重启可能漂移）。

## 3. 429 错误

### 症状：429 "Quota exceeded"（P0-6 限额）

```bash
# 1. 看具体哪个维度 + 哪个 account 超
journalctl --user -u aimc -n 20 | grep -i "quota"

# 2. 查 db 限额配置
sqlite3 data/aimc.db "SELECT * FROM account_quotas_usage_view LIMIT 5"
# 或:
sqlite3 data/aimc.db "
  SELECT a.account_name, q.dimension, u.used_value, q.limit_value
  FROM account_quota_usage u
  JOIN account_quotas q ON u.account_id=q.account_id AND u.dimension=q.dimension
  JOIN accounts a ON a.id=u.account_id
  WHERE u.used_value > 0.9 * q.limit_value
"

# 3. 等 cooldown (60s) 或加账号 / 调限额
python scripts/seed_account_quotas.py  # 重设
# 或: dashboard 运维页「账号管理」卡（限额预警展示）
```

### 症状：429 "已达到 Token Plan 用量上限"（上游）

```bash
# 1. 看是哪个上游
journalctl --user -u aimc -n 20 | grep -iE "minimax|429|Token Plan"

# 2. P1-15 自动 failover (组路由)
#    tier:flagship 会自动跳到非 minimax 渠道
#    但 minimax-m3 (单一渠道) 会 429 透传

# 3. 解决:
#    - 升级 Token Plan (推荐)
#    - 加 minimax 第二账号 (P1-12 multi-account_import)
#    - 等 cooldown 60s 后重试
```

### 症状：429 余额不足 / needs_human 长时间未恢复（V4.9 行为）

V4.9 起，**余额不足类 429 会被标 `needs_human=true`**，路由**不再主动探活**（避免反复撞死 key）。需要人工处理：

**判断是余额不足还是临时限流**：

| 信号 | 含义 |
|------|------|
| `X-AIMC-Last-Error-Code: 1113` | 国内厂商余额不足（DeepSeek/GLM 等） |
| `X-AIMC-Last-Error-Code: insufficient_quota` | OpenAI 风格配额耗尽 |
| `X-AIMC-Last-Error-Code: rate_limit_exceeded` 且无 quota 关键字 | 临时限流（**会自愈**） |
| 响应头 `Retry-After: 3600` | AIMC 判定为 `needs_human`，不要立即重试 |

**处理流程**：

1. **先看 Dashboard「🔴 待处理 (N)」**（运维页卡片，顶栏红 badge）
2. 找到「💰 余额不足」类，看具体 channel + 账号 + 错误消息
3. 充值后点 **「确认恢复」** → 立即触发一次探活 → 移除此条
4. 如果账号作废（key 撤销），点 **「禁用」** → 路由永久排除

如果没看到 🔴 tab：

```bash
# 1. 直接查 API
curl -s http://localhost:8080/dashboard/api/cooldown/needs-human \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN" | jq

# 2. 看 DB usage_logs (V4.9 持久化)
sqlite3 data/aimc.db "
  SELECT account_id, error_type, error_code, error_msg, needs_human, created_at
  FROM usage_logs
  WHERE error_type IS NOT NULL
  ORDER BY created_at DESC LIMIT 10
"

# 3. 看 AccountHealthLog 探活历史
sqlite3 data/aimc.db "
  SELECT account_id, triggered_by, success, error_type, probe_at
  FROM account_health_log
  ORDER BY probe_at DESC LIMIT 10
"
```

调阈值：`.env` 改 `AIMC_NEEDS_HUMAN_QUOTA_THRESHOLD`（默认 2）。

## 4. 500 / 503 错误

### 症状：503 "No available channel for model"

```bash
# 1. 看 channel 状态
sqlite3 data/aimc.db "SELECT name, status, provider_type FROM channels"
sqlite3 data/aimc.db "SELECT account_name, status FROM accounts"

# 2. 检查 account 是否 cooldown
journalctl --user -u aimc -n 20 | grep -i "cooldown"

# 3. 重置 cooldown
# Dashboard /api/cache/refresh 后会重新 load
curl -X POST http://localhost:8080/dashboard/api/cache/refresh
```

### 症状：500 Internal Server Error

```bash
# 1. 看完整 traceback
journalctl --user -u aimc -n 100 | tail -80

# 2. 看 db 是否 lock
sqlite3 data/aimc.db ".timeout 5000" "PRAGMA integrity_check"

# 3. 重启
systemctl --user restart aimc
```

## 5. cache 节省不准 / 缓存命中率低

```bash
# 1. 看 cache stats
curl http://localhost:8080/dashboard/api/cache/stats?days=7 | python3 -m json.tool

# 2. 检查 channel.supports_prompt_cache (P1-12 加的字段)
sqlite3 data/aimc.db "SELECT name, supports_prompt_cache FROM channels"

# 3. 没填 -> dashboard PUT /api/channels/{id} 设 1

# 4. cache 价不准 (估算的 10%)
#    方案: 等 SF 公开 cache 真实价 (目前 hardcode 估算)
#    或: 看 price_snapshots.cached_input_price 是否填了
sqlite3 data/aimc.db "SELECT COUNT(*) FROM price_snapshots WHERE channel_id=3 AND cached_input_price IS NOT NULL"
```

## 6. 数据不一致 / db 漂移

```bash
# 1. model_registry vs price_snapshots (P0-2 修复)
sqlite3 data/aimc.db "
  SELECT mr.model_id, mr.tier, ps.input_price, ps.output_price
  FROM model_registry mr
  LEFT JOIN price_snapshots ps ON mr.model_id = ps.raw_model_id
  WHERE mr.tier IS NOT NULL
  ORDER BY mr.tier, mr.model_id
  LIMIT 10
"

# 2. 跑一致性检查
python scripts/check_model_consistency.py --check db
```

## 7. 建议系统不工作

```bash
# 1. 看建议表
sqlite3 data/aimc.db "SELECT id, model_id, change_type, status, confidence FROM model_change_suggestions ORDER BY id DESC LIMIT 5"

# 2. 手动触发建议生成 (默认 shadow mode)
cd /home/lxc/aimc && source .venv/bin/activate
PYTHONPATH=. python intelligence/analytics/suggest_changes.py --shadow-mode

# 3. 写到 db
PYTHONPATH=. python intelligence/analytics/suggest_changes.py --apply

# 4. 看 log
journalctl --user -u aimc -n 20 | grep suggest
```

## 8. 常见报错速查

| 报错 | 含义 | 修复 |
|------|------|------|
| `database is locked` | 多进程启动 / 没用 WAL | `pkill -f main.py; systemctl --user restart aimc` |
| `Master Key 格式错` | AIMC_MASTER_KEY 不是 64 字符 hex | 重生成 `openssl rand -hex 32` |
| `ciphertext 解密失败` | AIMC_MASTER_KEY 改了 / 旧 plaintext key | `python scripts/migrate_encrypt_keys.py` 或重新加账号 |
| `JobNotFound` | scheduler job 没注册 | 看 `intelligence/scheduler.py` start_scheduler() 是否跑 |
| `greenlet_spawn` | sync session 用 async engine | P0-8 已修（独立 sync_engine），如再现重启 |
| `KeyError: 'cache-hit-tokens'` | SF HTML 结构变了 | SF pricing 解析需更新（阶段B 重抓） |

## 9. V4.9.1 探活 / scheduler 启动异常

### 症状：APScheduler 静默挂掉（job 没跑但没报错）

**背景**：V4.9.1 加了 `channel_health_probe` / `cooldown_cleanup` / `account_health_log_cleanup` 3 个 job。如果它们注册时 pickle 失败（比如回调是 lambda），**整个 scheduler.start() 会抛 ValueError，所有 job 都不会跑**，但服务进程仍在（gateway 健康检查 200）。

**判断**：

```bash
# 1. 看 job 是否都注册了
grep 'Added job' /tmp/aimc.log | grep -E 'Channel 智能探活|Cooldown 过期清理|探活日志清理'
# 期望 3 条, 实际 0 条 → scheduler 没起来

# 2. 看有没有 ValueError (lambda 不能 pickle)
grep -i 'ValueError\|cannot be serialized' /tmp/aimc.log | tail -5
```

**修复**：检查新增 job 是否用了 `lambda: ...` 回调（**APScheduler 用 SQLAlchemyJobStore 持久化，lambda 不能被 pickle**）。改成模块级函数：

```python
# 错误 (lambda 不能被 pickle)
scheduler.add_job(lambda: my_func(arg), ...)

# 正确 (模块级函数)
async def my_func_with_arg():
    return await my_func(arg)
scheduler.add_job(my_func_with_arg, ...)
```

### 症状：channel_health_probe 跑得太频繁 / 积压几千个待探活

**原因**：网络全挂时 cooldown_manager._state 累积大量 next_probe_at=过去 的 entry。

**检查**：

```bash
grep 'channel_health_probe:' /tmp/aimc.log | tail -10
# 看日志是否出现 "due_ids=N > 50" 警告 (max_batch 触发)
```

**缓解**：调大 `AIMC_HEALTH_PROBE_MAX_BATCH`（默认 50），或调大 `AIMC_HEALTH_PROBE_TOTAL_TIMEOUT`（默认 60s）。

### 症状：account_health_log 表无限增长

**原因**：V4.9.1 前没有 retention，1 channel 每天 1440 条。

**检查**：

```bash
sqlite3 data/aimc.db "SELECT COUNT(*) FROM account_health_log"
# 数十万 → 之前漏跑 retention
```

**手动清理**：

```bash
sqlite3 data/aimc.db "
  DELETE FROM account_health_log
  WHERE probe_at < datetime('now', '-7 days')
"

# 确认 retention job 跑了
grep 'account_health_log_cleanup' /tmp/aimc.log | tail -5
```

## 10. 收集诊断信息

如果以上不能解决，收集以下信息提交 issue：

```bash
# 1. 系统信息
uname -a
python --version
pip list 2>&1 | grep -iE "fastapi|sqlalchemy|apscheduler|httpx"

# 2. AIMC 状态
systemctl --user status aimc
journalctl --user -u aimc -n 200 > /tmp/aimc.log

# 3. db 状态
sqlite3 data/aimc.db ".tables" > /tmp/aimc-tables.txt
sqlite3 data/aimc.db "SELECT COUNT(*) FROM channels, accounts, price_snapshots, model_change_suggestions"

# 4. 复现步骤
echo "request: $(date)" >> /tmp/aimc.log
curl -v http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AIMC_API_KEY" \
  -d '{"model":"tier:flagship","messages":[{"role":"user","content":"hi"}]}' >> /tmp/aimc.log 2>&1

# 然后发到 Gitee issue: https://gitee.com/XiaoYRecluse/aimc/issues
```
