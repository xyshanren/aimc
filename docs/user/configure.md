# 配置 AIMC (账号 + Dashboard token + 多用户 Key + 限额)

> 受众：装好 AIMC 后想接入多账号、配置 Dashboard 鉴权、发多用户 Key、调限额的运维
> 前提：已完成 [install.md](install.md)
> 耗时：10-20 分钟

## ⚠️ .env 行内注释陷阱（systemd 用户必读）

AIMC 的 systemd unit 用 `EnvironmentFile=` 加载 `.env`。**systemd 不剥离行内注释** —
`KEY=value  # 注释` 会把 `# 注释`（含前面空格）整段带进值里，而 pydantic 直接读
`.env` 文件时会剥离，两条路径行为不一致（曾导致 `AIMC_ALERT_CHANNEL` 值污染、周报推送失败）。

```ini
# ❌ 错误 (systemd 下值会变成 "feishu          # 飞书机器人")
AIMC_ALERT_CHANNEL=feishu          # 飞书机器人

# ✅ 正确 (注释单独一行)
# 飞书机器人
AIMC_ALERT_CHANNEL=feishu
```

规则：**`.env` 里任何生效行的值后不要跟 `#` 注释**，注释一律独立成行。

## 1. 完整 .env 配置

```ini
# === 网关 ===
AIMC_HOST=0.0.0.0
AIMC_PORT=18080
AIMC_LOG_LEVEL=INFO

# === 鉴权 ===
# 网关 API Key (客户端用 OpenAI SDK 时填), 留空 = 关闭鉴权
AIMC_API_KEY=your-strong-random-key-here

# Dashboard Token (浏览器访问 dashboard 用), 留空 = LAN 内裸开 (legacy)
# 推荐: openssl rand -hex 32
AIMC_DASHBOARD_TOKEN=

# === 厂商 API Keys (至少 1 个) ===
# 硅基流动 (推荐, 1 key 通吃 91 model)
SILICONFLOW_API_KEY=sk-xxx
# DeepSeek 官方
DEEPSEEK_API_KEY=sk-xxx
MINIMAX_API_KEY=sk-xxx
GLM_API_KEY=xxx
QWEN_API_KEY=sk-xxx
SENSENOVA_API_KEY=xxx
STEPFUN_API_KEY=xxx

# === 厂商 base_url 覆盖 (默认不需要) ===
# SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
# DEEPSEEK_BASE_URL=https://api.deepseek.com/v1

# === 路由 / 缓存 / 限额 ===
# 路由缓存刷新 (秒)
AIMC_ROUTE_REFRESH_INTERVAL=300
# 路由缓存 TTL (秒, P0-4) — router.py 直读, 非 pydantic settings
AIMC_ROUTER_CACHE_TTL=60
# 总采集开关 (试用阶段可关)
AIMC_CRAWL_ENABLED=true
# 价格波动告警阈值 (%)
AIMC_PRICE_ALERT_THRESHOLD_PCT=30.0

# === AI 解析 (情报采集层, 2026-10-13 起两档) ===
# A. 默认 (推荐): 全留空 = 自调用本网关, 解析走组路由复用已配渠道
#    可用 AIMC_PARSE_MODEL 换组或指定模型 (tier:flagship / tier:light / 具体名)
# B. 外部模型: 填 OPENAI_API_KEY (+ 可选 OPENAI_BASE_URL) 自动切换
# OPENAI_API_KEY=
# OPENAI_BASE_URL=
# AIMC_PARSE_MODEL=tier:balanced

# === 告警 ===
AIMC_BARK_KEY=
AIMC_FEISHU_WEBHOOK_URL=
AIMC_SERVERCHAN_KEY=

# === V4.9 智能探活 (channel 健康状态机) ===
# 总开关 — false 则 channel_health_probe job 跳过所有探活
AIMC_HEALTH_PROBE_ENABLED=true
# 调度间隔 (秒) — 实际只探 cooldown_manager.accounts_due_for_probe() 的 account
AIMC_HEALTH_PROBE_INTERVAL=60
# 并发数 — 防打爆上游 (Semaphore 限流)
AIMC_HEALTH_PROBE_CONCURRENCY=8
# 单 channel 超时 (秒)
AIMC_HEALTH_PROBE_TIMEOUT=5.0
# 指数退避上限 (秒) — 连续探活失败间隔不会超过这个值
AIMC_HEALTH_PROBE_MAX_BACKOFF=3600

# === V4.9 needs_human 阈值 (按 ErrorCategory) ===
# 累计失败次数 >= 此阈值时, 标 needs_human=true (等人介入, 不再主动探活)
# quota 失败 2 次 → 需人工 (避免单次偶发误判)
AIMC_NEEDS_HUMAN_QUOTA_THRESHOLD=2
# auth 失败 1 次即需人工 (key 失效不该自愈)
AIMC_NEEDS_HUMAN_AUTH_THRESHOLD=1
# model 失败 2 次 → 需人工 (避免误判)
AIMC_NEEDS_HUMAN_MODEL_THRESHOLD=2

# === V4.9.1 探活防护 (防雪崩 / 防资源泄漏) ===
# 单 job 最多探活数 — 超出下个 job 再探, 防止「网络全挂时积压上千个待探活」
AIMC_HEALTH_PROBE_MAX_BATCH=50
# job 总超时 (秒) — 超时回填 NETWORK_TRANSIENT, 不阻塞下次调度
AIMC_HEALTH_PROBE_TOTAL_TIMEOUT=60.0
# account_health_log 保留天数 — 凌晨 3 点的清理 job 会删 cutoff 之前记录
# 7 天够 dashboard 查探活历史 + 周报分析, 调大可保留更久
AIMC_ACCOUNT_HEALTH_LOG_RETENTION_DAYS=7
```

完整字段见 `.env.example`（[dev/setup.md](../dev/setup.md) 有字段详解）。

## 2. 录入账号

> 💡 **全新部署推荐先走看板「初始化配置」页** — 打开看板即自动进入，按渠道卡片
> （免费渠道优先）注册并粘贴 API Key 即可，效果等同本节所有手动步骤。
> 本节是手动/批量路径。

### 方式 A: Dashboard 方式（推荐）

```
1. 浏览器访问 http://localhost:18080/dashboard/
2. 输入 AIMC_DASHBOARD_TOKEN
3. 首启自动进「初始化配置」页；之后加账号走 "运维 → 账号管理" → "新增账号"
4. 选渠道（siliconflow_aggregator / deepseek_official / ...）+ 填 API Key
5. 保存
```

详见 [dashboard.md](dashboard.md)。

### 方式 B: CLI 方式

```bash
python scripts/manual_import.py
# 按提示填 channel + API key, 多账号重复
```

### 方式 C: 多账号批量导入（V4.6.1）

```bash
python scripts/multi_account_import.py
# 适合同一 channel 多账号 (分散频限)
```

## 3. 配置 Dashboard 鉴权

```bash
# 生成强 token
DASHBOARD_TOKEN=$(openssl rand -hex 32)
echo "AIMC_DASHBOARD_TOKEN=$DASHBOARD_TOKEN" >> .env
systemctl --user restart aimc
```

访问 `http://localhost:18080/dashboard/`，粘贴 token 登录（token 存浏览器 localStorage）。

**留空 = legacy 行为**（LAN 内裸开），适合开发环境。

## 4. 多用户 Key 管理（V4.8）

V4.8 起支持**多用户体系**：每个用户有独立的 `aimc-` 前缀 API Key，请求按人记账
（`usage_logs.caller_id`），可配个人日/月 token 限额。

### 发 Key

```bash
# 新建用户并发一把 key（明文只显示一次，立即保存）
python scripts/add_user_key.py --username zhangsan --display-name 张三

# 给已有用户加一把 key（如工作机/笔记本各一把）
python scripts/add_user_key.py --username zhangsan --label 笔记本

# admin key（可看 dashboard 全局数据）
python scripts/add_user_key.py --username admin --admin

# 设个人限额（日 10 万 / 月 100 万 token；NULL=不限，0=禁用）
python scripts/add_user_key.py --username zhangsan \
    --daily-limit 100000 --monthly-limit 1000000
```

### 用法（跟普通 OpenAI 客户端一样）

```bash
curl http://localhost:18080/v1/chat/completions \
  -H "Authorization: Bearer aimc-xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"model": "tier:balanced", "messages": [...]}'
```

### 鉴权优先级

middleware 按顺序匹配：**api_keys 表**（`aimc-` 前缀，SHA256 查表）→ **legacy
AIMC_API_KEY**（全局 key，无 caller 记账）。两种 key 可并存，渐进迁移。

### 限额语义

个人限额在请求前检查，超额返回 **429**（`daily_token_limit_exceeded` /
`monthly_token_limit_exceeded`）。限额按本地日历日/月计算，超限时已用 + 本次预估
> 限额即拦截。

### 管理

```bash
# 列出所有用户/key（脱敏，只显示前缀）
python scripts/list_user_keys.py

# 吊销 key（dashboard "Key 管理" 栏点吊销，或 API）
curl -X POST http://localhost:18080/dashboard/api/keys/<id>/revoke \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"
```

用量按人统计见 Dashboard「按人用量」栏（近 7 天）。

## 5. 配置多维限额

AIMC 默认不限（除非你手动加）。限额配置在 db 里：

```bash
# 方式 A: 通过脚本配置 (推荐)
python scripts/seed_account_quotas.py

# 方式 B: 通过 Dashboard 运维页「账号管理」卡（限额预警已并入该卡, 仅展示; 配置仍走方式 A 或 SQL）
```

**支持的 7 个维度**：
- `rpm` / `tpm`：每分钟请求 / token
- `daily_tokens` / `weekly_tokens` / `5h_tokens`：窗口累计
- `concurrent`：并发请求数
- `balance_cny`：余额（元，永久）

详见 [ops/monitor.md](../ops/monitor.md)。

## 6. 配置 Score 权重（路由策略调优）

```bash
# Dashboard → "路由策略" → PUT /api/settings/router-score-weights
# 5 维加权: W_PRICE / W_QUOTA / W_SUCCESS / W_RECENCY / W_PRIORITY
# 默认: 0.40 / 0.30 / 0.20 / 0.05 / 0.05
# 例: 月底想省钱 -> 调高 W_PRICE 到 0.6
```

详见 [dev/api.md §路由策略](../dev/api.md)。

## 7. 验证配置

```bash
# 1. 网关鉴权工作
curl -H "Authorization: Bearer $AIMC_API_KEY" http://localhost:18080/v1/models | head

# 2. Dashboard token 工作
curl http://localhost:18080/dashboard/api/channels -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN" | head

# 3. 限额配置生效
sqlite3 data/aimc.db "SELECT * FROM account_quotas LIMIT 5"

# 4. Score 权重生效
sqlite3 data/aimc.db "SELECT value FROM settings_runtime WHERE key='router_score_weights'"
```

## 故障排查（配置阶段）

| 症状 | 检查 | 修复 |
|------|------|------|
| Dashboard 401 | token 错 | 重启后用 `curl -H "Authorization: Bearer $T" /dashboard/api/auth/status` |
| Dashboard 503 | 网关没启 | `systemctl --user status aimc` |
| `manual_import` 报 `Invalid API key` | 厂商 key 错 | 去厂商官网重新生成 |
| 限额不生效 | 没跑 seed 脚本 | `python scripts/seed_account_quotas.py` |
| Score 权重不生效 | 没 trigger cache refresh | `curl -X POST /dashboard/api/cache/refresh` |
| 上游 429 (如 minimax TokenPlan) | 限额耗尽 | P1-15 自动 failover 到其他渠道, 见 [troubleshooting.md](troubleshooting.md) |
