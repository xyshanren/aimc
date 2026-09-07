# Dashboard 使用指南

> 受众：部署好 AIMC 后用浏览器访问 dashboard 的用户
> 前提：AIMC 已配置 `AIMC_DASHBOARD_TOKEN`（见 [configure.md](configure.md)）

访问 `http://<your-host>:8080/dashboard/`，输入 token 登录（token 存 localStorage）。

Phase 3 起（2026-09-05）看板为**侧边栏 6 页哈希路由**（概览 / 成本 / 用量 / 渠道 / 情报 / 运维），
只轮询当前页；顶栏有全局时间范围 24h/7d/30d 切换（URL + localStorage 持久化）。

## 卡片速览（按侧边栏页面）

| 页 | 卡片 | 见下面 § |
|----|------|----------|
| **概览** | 今日摘要 / 实时状态 KPI（总 Token·请求数·Cache 命中率·Cache 节省）/ 标价 vs 实付趋势 / Top 模型 / 分组概览 | §2 · §4b |
| **成本** | 成本审计（按模型 / 按分组 / 按用户 tab，V6.0） | §4b |
| **成本** | Cache 节省明细（环图 + 渠道表） | §2 |
| **用量** | 用量统计（每日请求柱图 + 日表） | §4 |
| **用量** | 路由成功率（Agent 视角，原「组路由命中率」） | §5 |
| **用量** | 请求日志（状态过滤 + model 搜索 + 分页；行点开「详情」抽屉看转录，Phase 3b） | — |
| **渠道** | 渠道 + 价格（搜索过滤） | — |
| **渠道** | Model Registry + Groups（tier / capabilities / ctx） | — |
| **情报** | 优惠情报（已过滤低置信度垃圾条目） | §7 |
| **情报** | 优惠审查（低置信度人工确认） / 待确认能力变更 / 周报 | §7 |
| **运维** | 数据新鲜度 | §3 |
| **运维** | 🔴 待处理（余额不足 / Key 失效 / 智能探活状态 — 一键人工干预，V4.9） | §9 |
| **运维** | 24h 瞬时冷却记录（V6.3.1，自愈型抖动留痕） | — |
| **运维** | 待审建议（方案A 模型分组建议） | §1 |
| **运维** | 账号管理（渠道账号增删 / 过期扫描 / 限额预警并入本卡） | §7 |
| **运维** | Key 管理（V4.8 多用户） | §6 |
| **运维** | 采集源管理（V4.8.3） | §8 |
| **运维** | 自动入组观察（V6.3 埋点，纯只读） | — |

---

## §1 模型分组建议（方案 A）

**位置**：运维页「待审建议」卡

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

**位置**：运维页

**显示**：每个 apscheduler job 的 `last_success_at` 距今多久

**健康度颜色**：
- � healthy：< 1 天
- 🟡 warning：1-3 天
- 🔴 stale：> 3 天
- ⚪ unknown：从没跑过

**怎么用**：
- � = 该 job 失败了，看 `journalctl --user -u aimc -n 50` 查原因
- 🟡 = 该 job 超过 1 天没成功（数据可能过期；注意部分 job 如
  batch_crawl_siliconflow_43200 调度间隔长，偶尔 warning 是正常的）
- job 列表含 batch_crawl_<provider>_<interval>（V4.8.3 按 provider × 轮询间隔分组）
  + refresh_route_cache + check_key_rotation +
  cross_source_detection + monthly_official_audit + weekly_suggest_changes

**API**：`GET /dashboard/api/data-freshness`。

## §4 用量统计

**显示**：
- 每天请求数 / input token / output token / 平均延迟（最近 7 天）
- Chart.js 柱状图（每日请求，Phase 3 起）+ 日表

**怎么读**：
- 请求数突增 → 可能上游有优惠活动 / 爬虫异常
- 平均延迟 > 2s → 上游响应慢或本地网络问题

## §4b 成本审计（V6.0 Phase 2）

**显示**：
- KPI：实付（按渠道实际计费）/ 官方标价（按 registry 标价估算）/ 节省（标价 − 实付）/ 标价覆盖率
- 标价 vs 实付趋势双线图在概览页（Phase 3 指标重组时收敛，成本页聚焦 KPI + 明细）
- 按模型 / 按分组 / 按用户三个 tab 下钻（请求 / token / 实付 / 标价 / 节省 / 单行覆盖率）

**按用户 tab**（V6.1+ 并入原右栏「按人用量」卡）：
- 每行 = 一个调用方（caller）：Input / Output / 请求 / Token / 实付 / 标价 / 节省 / 覆盖率
- 走全局 `AIMC_API_KEY` 的请求显示为 `(未鉴权/legacy)`
- **点用户行展开该用户各 key（label）的明细**：请求 / Input / Output / Token / 实付

**怎么读**：
- 免费模型（glm-4.7-flash 等）标价为 0 → 节省 0 是真实结论，不是数据缺失
- 标价未知的模型（如 kimi-k3）节省列显示「标价未知」，不计入节省总额；总覆盖率 < 50% 时 KPI 变黄提醒补数据
- 节省大 = 免费/订阅渠道承接了本应按标价计费的流量（口径不含缓存/时段折扣，是上界估计）

**补标价**：`model_registry.list_input_price / list_output_price` 直接 UPDATE 即可
（迁移脚本 `db/migrations/2026_09_03_v60_phase2_cost_audit.py` 的 LIST_PRICES 表可作参考；
迁移只写 NULL 行，不会覆盖手工值）。

## §5 组路由命中率（现名「路由成功率」）

**显示**：tier:xxx / scene:xxx 调用频次 + 跳到 fallback_chain 下一候选的次数（指标统一 Phase 1 起卡片更名「路由成功率」，Agent 视角口径）

**怎么用**：
- "tier:flagship 命中率 100%" → 没有 fallback 触发
- "tier:balanced fallback 5 次" → 该组某些候选不可用（限额 / cooldown）

**调优**：如果某 tier fallback 频繁，调路由 score 权重或加账号。

## §6 Key 管理（V4.8 多用户）

> V6.1+：原右栏「按人用量」卡已并入中栏成本审计的「按用户」tab（含 key 下钻），见 §4b。

**Key 管理**：用户列表 + 每人的 key（前缀脱敏）/ admin 标记 / 日月限额 / 最后使用时间。
- 吊销：点对应 key 的「吊销」按钮，立即失效不可恢复

发 key / 设限额用 CLI（见 [configure.md §4](configure.md#4-多用户-key-管理v48)）或
`POST /dashboard/api/keys`（V6.1，见 [agent-api.md](../agent-api.md)）。

**API**：`GET /dashboard/api/keys`、`POST /dashboard/api/keys/<id>/revoke`。

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

## §8 采集源管理（V4.8.3）

**位置**：运维页

**显示**：全部 37 个采集源的 provider / 类型（API / HTML）/ 轮询间隔 / 状态 /
最后检查时间 / 错误次数 / 连续无变化次数。

**操作**：
- 「采集」— 立即触发该 provider 的所有源（不等定时器，后台 fire-and-forget）
- 「禁用 / 启用」— 按 provider 切换（写 DB `status=disabled`，scheduler 跳过禁用源）

**注意**：
- 连续无变化 ≥ 4 次的源会自适应降频（4 倍间隔；≥ 10 次降为 8 倍），前端黄色
  badge 提示，无需手动处理
- 调整轮询间隔暂无 UI 按钮，走 API（`POST /dashboard/api/sources/{provider}/interval`，
  重启后生效，见 [dev/api.md](../dev/api.md)）

**API**：`GET /dashboard/api/sources`。

---

## 调参速查表

| 想做什么 | 在哪改 | 生效时间 |
|---------|--------|----------|
| 调整 score 5 维权重 | `PUT /api/settings/router-score-weights` | 60s 内 refresh cache |
| 调整某 channel 是否启用 | Dashboard "账号管理" 或 `UPDATE channels SET status='disabled'` | 立即生效（refresh cache） |
| 调整某 account 是否启用 | 同上 | 同上 |
| 修改限额 | Dashboard 运维页「账号管理」卡（限额预警展示）或 `UPDATE account_quotas` | 立即生效 |
| 启用/关闭采集 | `.env` `AIMC_CRAWL_ENABLED` | 重启 |
| 修改告警阈值 | `.env` `AIMC_PRICE_ALERT_THRESHOLD_PCT` | 重启 |

## API 速查

所有端点见 [dev/api.md §Dashboard API](../dev/api.md)。

常用：
- `GET /dashboard/api/channels` — 渠道列表
- `PUT /api/settings/router-score-weights` — 改 score 权重
- `POST /api/cache/refresh` — 立即刷新路由缓存（不等 TTL）
- `GET /dashboard/api/cache/stats?days=7` — cache 节省
- `GET /dashboard/api/data-freshness` — job 健康度
- `GET /dashboard/api/suggestions?status=pending` — 待审建议
- `GET /dashboard/api/stats/per-user?days=7` — 按调用方用量 + key 明细（成本审计按用户 tab 数据源之一）
- `GET /dashboard/api/usage/cost-by-model?days=7` — 按模型成本审计（V6.0）
- `GET /dashboard/api/usage/cost-by-group?days=7` — 按分组成本审计（V6.0）
- `GET /dashboard/api/usage/cost-by-user?days=7` — 按用户成本审计（V6.0）
- `GET /dashboard/api/usage/cost-trend?days=7` — 按天成本趋势（V6.0）
- `GET /dashboard/api/keys` — 用户 key 列表（脱敏）
- `GET /dashboard/api/stats/group_routing` — 组路由命中分布
- `GET /dashboard/api/promotions` — 优惠情报（默认过滤低置信度）
- `GET /dashboard/api/sources` — 采集源列表（V4.8.3）
- `GET /dashboard/api/pending-summary` — 🔴 待处理 tab 总览（V4.9）
- `GET /dashboard/api/cooldown/needs-human` — needs_human 详情列表（V4.9）
- `POST /dashboard/api/cooldown/{aid}/ack-recover` — 人工确认恢复（V4.9）
- `POST /dashboard/api/accounts/{aid}/manual-disable` — 人工禁用账号（V4.9）
- `POST /dashboard/api/accounts/{aid}/manual-enable` — 解除人工禁用（V4.9）

---

## §9 🔴 待处理（V4.9 P5 新增）

**位置**：运维页，卡片标题有红色 badge 显示总待处理数（如 `🔴 待处理 3`）；顶栏另有持久待处理 badge（V6.3.1 起排除自愈型瞬时冷却）。

**作用**：聚合 5 类需要关注的项，让你一眼看到哪些账号因什么原因需要人工干预（**不用翻日志/重启服务**）：

| 类别 | 含义 | severity |
|------|------|----------|
| 💰 余额不足 | 累计失败 ≥ 阈值（如 quota 2 次）→ `needs_human=true` | danger |
| 🔑 Key 失效 | auth 失败（如 invalid_api_key / 401） | danger |
| 🧩 模型不支持 | model_not_found / 已下架 | warning |
| ⚠️ 自动冷却中 | 自愈中的冷却（如 5xx / 网络） | warning |
| 🚫 人工禁用 | dashboard 手动禁用的账号 | info |

**每条 needs_human 显示 2 个按钮**：
- **确认恢复**：调 `POST /api/cooldown/{aid}/ack-recover` → `mark_success` + 立即探活（异步）
- **禁用**：调 `POST /api/accounts/{aid}/manual-disable` → 路由永久排除

**30s 自动刷新**，无需手动点。

### 工作原理

- 错误分类器（`gateway/error_classifier.py`）8 种 ErrorCategory 把上游失败分类
- CooldownManager 5 状态机：healthy / failing / degraded / needs_human / manual_disabled
- 智能探活 job `channel_health_probe`（每 60s，仅探 `accounts_due_for_probe()` 的 account，needs_human 不主动探）写 `account_health_log` 表

### 操作流程示例

余额不足场景：
1. 用户请求触发 `quota_exhausted` → 1 次 mark_failure, 冷却 1h
2. 同一账号第 2 次仍 429 → 累计 2 次 ≥ `AIMC_NEEDS_HUMAN_QUOTA_THRESHOLD=2` → `needs_human=true`
3. Bark/Server酱/飞书推一条 1h 静默告警（throttle key = `needs_human:{channel}:{error_type}`）
4. Dashboard 「🔴 待处理」badge 变红，显示「💰 余额不足 N」+ 账号/channel/错误消息
5. 充值后点「确认恢复」 → 立即探活一次确认余额 OK → 移除此条

### 相关 API

- `GET /dashboard/api/pending-summary` — 聚合总览（本卡片数据源）
- `GET /dashboard/api/cooldown/needs-human` — 详情列表（弹窗用）
- `POST /dashboard/api/cooldown/{aid}/ack-recover` — 确认恢复
- `POST /dashboard/api/accounts/{aid}/manual-disable` — 人工禁用
- `POST /dashboard/api/accounts/{aid}/manual-enable` — 解除禁用
