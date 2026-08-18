# Dashboard 使用指南

> 受众：部署好 AIMC 后用浏览器访问 dashboard 的用户
> 前提：AIMC 已配置 `AIMC_DASHBOARD_TOKEN`（见 [configure.md](configure.md)）

访问 `http://<your-host>:8080/dashboard/`，输入 token 登录（cookie 7 天有效）。

## 卡片速览（按显示顺序）

| 卡片 | 用途 | 见下面 § |
|------|------|----------|
| **模型分组建议**（方案A） | 待审/已批/已应用的 model + group 调整建议 | §1 |
| **Prompt Cache 节省** | cache 命中率 + 节省金额 | §2 |
| **数据新鲜度** | 38 个 apscheduler job 的健康状态 | §3 |
| **用量统计** | 每天请求数 / token / 平均延迟 | §4 |
| **组路由命中率** | tier:xxx 调用频次 + 跳到 fallback 的次数 | §5 |
| **价格波动 / 跨源差异 / 待确认** | 优惠情报层 | §6 |

---

## §1 模型分组建议（方案 A）

**位置**：dashboard 最顶部（最显眼）

**显示**：
- 待审 4 条 / 已批 0 条 / 已应用 0 条
- 表格列出每个建议：model / 变更类型（tier_change / add_to_group）/ 置信度 / 理由

**操作**：
1. 看理由 → 点 "批准" 或 "拒绝"
2. 已批的建议 → 点 "一键应用已批准"

**调参**：见 [operations.md §建议系统](operations.md#建议系统)。

## §2 Prompt Cache 节省

**显示**：
- 节省金额 ¥X.XXXX（最近 7 天）
- Cache token 数 + 命中率 %（按 channel + TOP 10 model）

**怎么读**：
- `siliconflow_aggregator` 命中率 4.6% → 节省 ¥0.003
- `Qwen/Qwen3-32B` 是 cache 最积极的 model

**调优**：高命中率说明你的 system prompt / 多轮对话有重复 prefix（适合 cache）。

**API**：`GET /dashboard/api/cache/stats?days=7`（同 dashboard 显示的数据）。

## §3 数据新鲜度

**位置**：在 cache 节省之后

**显示**：每个 apscheduler job 的 `last_success_at` 距今多久

**健康度颜色**：
- � healthy：< 1 天
- 🟡 warning：1-3 天
- 🔴 stale：> 3 天
- ⚪ unknown：从没跑过

**怎么用**：
- � = 该 job 失败了，看 `journalctl --user -u aimc -n 50` 查原因
- 🟡 = 该 job 超过 1 天没成功（数据可能过期）
- 38 个 job 列表含 batch_crawl_<provider> + refresh_route_cache + check_key_rotation + cross_source_detection + monthly_official_audit + weekly_suggest_changes

**API**：`GET /dashboard/api/data-freshness`。

## §4 用量统计

**显示**：
- 每天请求数 / input token / output token / 平均延迟（最近 7 天）
- Chart.js 折线图

**怎么读**：
- 请求数突增 → 可能上游有优惠活动 / 爬虫异常
- 平均延迟 > 2s → 上游响应慢或本地网络问题

## §5 组路由命中率

**显示**：tier:xxx / scene:xxx 调用频次 + 跳到 fallback_chain 下一候选的次数

**怎么用**：
- "tier:flagship 命中率 100%" → 没有 fallback 触发
- "tier:balanced fallback 5 次" → 该组某些候选不可用（限额 / cooldown）

**调优**：如果某 tier fallback 频繁，调路由 score 权重或加账号。

## §6 价格波动 / 跨源差异 / 待确认

这些是情报层：
- **价格波动**：input_price / output_price 变化 > 30% 告警（`PRICE_ALERT_THRESHOLD` env 控制）
- **跨源差异**：同 model 在多个 channel 有不同定价 → AIMC 自动选最便宜的
- **待确认**：AI 解析不确定的优惠信息 → 人工 review

---

## 调参速查表

| 想做什么 | 在哪改 | 生效时间 |
|---------|--------|----------|
| 调整 score 5 维权重 | `PUT /api/settings/router-score-weights` | 60s 内 refresh cache |
| 调整某 channel 是否启用 | Dashboard "账号管理" 或 `UPDATE channels SET status='disabled'` | 立即生效（refresh cache） |
| 调整某 account 是否启用 | 同上 | 同上 |
| 修改限额 | Dashboard "限额管理" 或 `UPDATE account_quotas` | 立即生效 |
| 启用/关闭采集 | `.env` `AIMC_CRAWL_ENABLED` | 重启 |
| 修改告警阈值 | `.env` `PRICE_ALERT_THRESHOLD` | 重启 |

## API 速查

所有端点见 [dev/api.md §Dashboard API](../dev/api.md)。

常用：
- `GET /dashboard/api/channels` — 渠道列表
- `PUT /api/settings/router-score-weights` — 改 score 权重
- `POST /api/cache/refresh` — 立即刷新路由缓存（不等 TTL）
- `GET /dashboard/api/cache/stats?days=7` — cache 节省
- `GET /dashboard/api/data-freshness` — job 健康度
- `GET /dashboard/api/suggestions?status=pending` — 待审建议
