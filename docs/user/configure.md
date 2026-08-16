# 配置 AIMC (账号 + Dashboard token + 限额)

> 受众：装好 AIMC 后想接入多账号、配置 Dashboard 鉴权、调限额的运维
> 前提：已完成 [install.md](install.md)
> 耗时：10-20 分钟

## 1. 完整 .env 配置

```ini
# === 网关 ===
HOST=0.0.0.0
PORT=8080
LOG_LEVEL=INFO

# === 鉴权 ===
# 网关 API Key (客户端用 OpenAI SDK 时填), 留空 = 关闭鉴权
AIMC_API_KEY=your-strong-random-key-here

# Dashboard Token (浏览器访问 dashboard 用), 留空 = LAN 内裸开 (legacy)
# 推荐: openssl rand -hex 32
AIMC_DASHBOARD_TOKEN=

# === 厂商 API Keys (至少 1 个) ===
SILICONFLOW_API_KEY=sk-xxx         # 硅基流动 (推荐, 1 key 通吃 91 model)
DEEPSEEK_API_KEY=sk-xxx           # DeepSeek 官方
MINIMAX_API_KEY=sk-xxx
GLM_API_KEY=xxx
QWEN_API_KEY=sk-xxx
SENSENOVA_API_KEY=xxx
STEPFUN_API_KEY=xxx

# === 厂商 base_url 覆盖 (默认不需要) ===
# SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
# DEEPSEEK_BASE_URL=https://api.deepseek.com/v1

# === 路由 / 缓存 / 限额 ===
ROUTE_REFRESH_INTERVAL=300          # 路由缓存刷新 (秒)
AIMC_ROUTER_CACHE_TTL=60            # 路由缓存 TTL (秒, P0-4)
AIMC_CRAWL_ENABLED=true              # 总采集开关 (试用阶段可关)
PRICE_ALERT_THRESHOLD=30.0          # 价格波动告警阈值 (%)

# === AI 解析 (优惠情报用) ===
OPENAI_API_KEY=sk-xxx               # 优惠情报解析用
OPENAI_BASE_URL=https://api.siliconflow.cn/v1   # 可指向 SF 兼容 endpoint 省 token
PARSE_MODEL=MiniMax-M2.7

# === 告警 ===
BARK_KEY=
FEISHU_WEBHOOK_URL=
SERVERCHAN_KEY=
```

完整字段见 `.env.example`（[dev/setup.md](../dev/setup.md) 有字段详解）。

## 2. 录入账号

### 方式 A: Dashboard 方式（推荐）

```
1. 浏览器访问 http://localhost:8080/dashboard/
2. 输入 AIMC_DASHBOARD_TOKEN
3. 进 "账号管理" → "新增账号"
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

访问 `http://localhost:8080/dashboard/`，粘贴 token 登录。Cookie 保留 7 天。

**留空 = legacy 行为**（LAN 内裸开），适合开发环境。

## 4. 配置多维限额

AIMC 默认不限（除非你手动加）。限额配置在 db 里：

```bash
# 方式 A: 通过脚本配置 (推荐)
python scripts/seed_account_quotas.py

# 方式 B: 通过 Dashboard "限额管理" 页面 (后续 P1-14)
```

**支持的 7 个维度**：
- `rpm` / `tpm`：每分钟请求 / token
- `daily_tokens` / `weekly_tokens` / `5h_tokens`：窗口累计
- `concurrent`：并发请求数
- `balance_cny`：余额（元，永久）

详见 [ops/monitor.md](../ops/monitor.md)。

## 5. 配置 Score 权重（路由策略调优）

```bash
# Dashboard → "路由策略" → PUT /api/settings/router-score-weights
# 5 维加权: W_PRICE / W_QUOTA / W_SUCCESS / W_RECENCY / W_PRIORITY
# 默认: 0.40 / 0.30 / 0.20 / 0.05 / 0.05
# 例: 月底想省钱 -> 调高 W_PRICE 到 0.6
```

详见 [dev/api.md §路由策略](../dev/api.md)。

## 6. 验证配置

```bash
# 1. 网关鉴权工作
curl -H "Authorization: Bearer $AIMC_API_KEY" http://localhost:8080/v1/models | head

# 2. Dashboard token 工作
curl http://localhost:8080/dashboard/api/channels -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN" | head

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
