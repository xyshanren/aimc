# Dashboard 使用指南

> 受众：部署好 AIMC 后用浏览器访问 dashboard 的用户
> 前提：AIMC 已配置 `AIMC_DASHBOARD_TOKEN`（见 [configure.md](configure.md)）

访问 `http://<your-host>:8080/dashboard/`，输入 token 登录（cookie 7 天有效）。

## 卡片速览（按显示顺序）

| 卡片 | 用途 | 见下面 § |
|------|------|----------|
| **模型分组建议**（方案A） | 待审/已批/已应用的 model + group 调整建议 | §1 |
| **Prompt Cache 节省** | cache 命中率 + 节省金额 | §2 |
| **数据新鲜度** | apscheduler job（按 provider 数浮动，约 21 个）的健康状态 | §3 |
| **用量统计** | 每天请求数 / token / 平均延迟 | §4 |
| **组路由命中率** | tier:xxx 调用频次 + 跳到 fallback 的次数 | §5 |
| **按人用量**（V4.8） | 近 7 天各调用方的请求数/token/费用 | §6 |
| **Key 管理**（V4.8） | 多用户 key 列表 / 吊销 | §6 |
| **账号管理** | 渠道账号增删 / 过期扫描 | §7 |
| **待确认能力变更** | AI 推断的模型能力变更，人工批准/拒绝 | §7 |
| **优惠情报** | 各厂商活动（已过滤低置信度垃圾条目） | §7 |

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

统计口径：OpenAI 风格 `cached_tokens` 与 Anthropic 风格 `cache_read` 都算命中
（2026-08-21 起纳入；`cache_creation` 是写缓存成本不算节省）。

**怎么读**：
- `siliconflow_aggregator` 命中率 4.6% → 节省 ¥0.003
- `Qwen/Qwen3-32B` 是 cache 最积极的 model

**调优**：高命中率说明你的 system prompt / 多轮对话有重复 prefix（适合 cache）。
AIMC 自动注入 cache 标记（见 [usage.md §7](usage.md)），无需客户端配置。

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
- 🟡 = 该 job 超过 1 天没成功（数据可能过期；注意部分 job 如
  batch_crawl_siliconflow 调度间隔长，偶尔 warning 是正常的）
- job 列表含 batch_crawl_<provider> + refresh_route_cache + check_key_rotation +
  cross_source_detection + monthly_official_audit + weekly_suggest_changes

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

## §6 按人用量 + Key 管理（V4.8 多用户）

**按人用量**：近 7 天各调用方（caller）的请求数 / input / output / 总 token / 估算费用。
- 带 `aimc-` 前缀多用户 key 的请求按用户名分组
- 走全局 `AIMC_API_KEY` 的请求显示为 `(未鉴权/legacy)`

**Key 管理**：用户列表 + 每人的 key（前缀脱敏）/ admin 标记 / 日月限额 / 最后使用时间。
- 吊销：点对应 key 的「吊销」按钮，立即失效不可恢复

发 key / 设限额用 CLI（见 [configure.md §4](configure.md#4-多用户-key-管理v48)）。

**API**：`GET /dashboard/api/stats/per-user?days=7`、`GET /dashboard/api/keys`、
`POST /dashboard/api/keys/<id>/revoke`。

## §7 账号管理 / 待确认能力变更 / 优惠情报

**账号管理**：渠道账号增删、key 轮换、过期扫描（折叠卡片）。

**待确认能力变更**：AI 从厂商页面推断的模型能力变化（tier / capabilities / context 等），
置信度分高/中/低（绿/黄/红），人工「批准」后写入 registry 并同步模型组。
字段值是 JSON 时按 key=value 格式化展示（2026-08-21 起）。
- 变更类型用中文标签显示（新模型入库 / 元信息变更 / 档位调整 / 状态变更 / 显示名变更 /
  上下文长度 / 多模态变更），鼠标悬停可见原始值（2026-08-21 起）。
- 条目旁附模型显示名（如 `kimi-k2.7 (Kimi K2.7 Code)`），便于判断是否该批准。

**优惠情报**：各厂商活动列表。**默认只显示可信条目**（AI 解析置信度低的 flagged
条目已过滤；`GET /api/promotions?include_flagged=true` 可查全部）。无标题的解析
结果不再入库（源头治理，2026-08-21）。
- 同一厂商同名活动自动去重：写入时按 (provider, title) 查 7 天窗口去重，读取时
  按 (provider, title) 取最新一条，避免重扫重复刷屏（2026-08-21 起）。
- 条目附「查看原文 ↗」链接，点击跳转采集源页面（2026-08-21 起）。

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
- `GET /dashboard/api/stats/per-user?days=7` — 按人用量
- `GET /dashboard/api/keys` — 用户 key 列表（脱敏）
- `GET /dashboard/api/stats/group_routing` — 组路由命中分布
- `GET /dashboard/api/promotions` — 优惠情报（默认过滤低置信度）
