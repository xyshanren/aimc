# AIMC Agent API 契约 (V6.1)

> 受众：需要程序化查询/操作 AIMC 的 agent（编码助手、运维 bot、自动化脚本）
> 原则（V6.1 架构友好化，[next-major-v6-plan.md §3](next-major-v6-plan.md)）：
> **查询 = REST GET；变更 = REST POST/PUT；CLI 与 REST 等价；看板只做核心数据 + 人工干预入口。**

## 0. 快速开始

```bash
BASE=http://<your-host>:8080

# 鉴权（三种等价方式，任选其一）
# 1. dashboard token:   -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"
# 2. admin api_key:     -H "Authorization: Bearer aimc-xxxx"   (is_admin=1 的多用户 key, V6.1 起)
# 3. query param:       "?token=$AIMC_DASHBOARD_TOKEN"

# 机器可读契约（网关 + dashboard 全量端点，免鉴权）
curl $BASE/openapi.json
# 人读交互式文档
open $BASE/docs
```

CLI 等价入口（薄 REST 客户端，零依赖）：

```bash
export AIMC_BASE_URL=$BASE
export AIMC_DASHBOARD_TOKEN=xxx        # 或 AIMC_API_KEY=<admin aimc- key>
python scripts/aimc.py status          # 全部子命令 --help 可查
```

## 1. 操作分类（谁该走哪条路）

| 类别 | 例子 | 入口 |
|------|------|------|
| **纯自动**（agent 直接调） | 查用量/成本/账号/冷却、刷路由缓存、探活恢复确认、加账号、发 key | REST / CLI |
| **半自动**（agent 提交，人工审批） | model/group 变更建议、跨源分歧裁决、优惠入库审批 | REST 提交 + Dashboard/CLI 审批 |
| **纯人工**（只看板判断） | tier 调整最终拍板（architecture.md §十）、优惠期定价确认 | Dashboard |

## 2. 查询端点（GET）

| 端点 | 用途 |
|------|------|
| `GET /health` | 健康检查（免鉴权） |
| `GET /v1/models` | 模型组列表（`?include_models=true` 展开成员，免鉴权，OpenAI 兼容） |
| `GET /dashboard/api/channels` | 渠道 + 价格 + 配额 |
| `GET /dashboard/api/accounts` | 渠道账号状态 |
| `GET /dashboard/api/cooldown` / `.../needs-human` | 冷却中 / 需人工干预账号 |
| `GET /dashboard/api/usage/summary?days=N` | 每日请求/token/延迟 |
| `GET /dashboard/api/stats/per-user?days=N` | 按调用方用量（含 key 下钻） |
| `GET /dashboard/api/stats/group_routing` | 组路由命中/fallback 分布 |
| `GET /dashboard/api/usage/cost-by-model?days=N` | 成本审计：按模型（V6.0） |
| `GET /dashboard/api/usage/cost-by-group?days=N` | 成本审计：按分组（V6.0） |
| `GET /dashboard/api/usage/cost-by-user?days=N` | 成本审计：按调用方（V6.0） |
| `GET /dashboard/api/usage/cost-trend?days=N` | 成本审计：按天趋势（V6.0） |
| `GET /dashboard/api/cache/stats?days=N` | prompt cache 命中/节省 |
| `GET /dashboard/api/suggestions?status=pending` | 待审路由建议 |
| `GET /dashboard/api/pending-summary` | 🔴 待处理聚合（余额/Key/探活） |
| `GET /dashboard/api/auth/status` | 鉴权状态自检 |
| `GET /openapi.json`、`GET /docs` | 机器可读契约 / 交互式文档（免鉴权） |

## 3. 变更端点（POST / PUT）

| 端点 | 用途 | 半自动? |
|------|------|--------|
| `POST /dashboard/api/accounts` | 加渠道账号 | 否 |
| `POST /dashboard/api/accounts/{id}/enable` / `disable` | 启停账号 | 否 |
| `POST /dashboard/api/accounts/{id}/rotate` | 轮换账号 key | 否 |
| `POST /dashboard/api/accounts/expire-check` | 扫描过期账号 | 否 |
| `POST /dashboard/api/keys` | 创建/复用 user + 发 key（明文只回一次，V6.1 新增） | 否 |
| `POST /dashboard/api/keys/{id}/revoke` | 吊销 key（立即失效） | 否 |
| `POST /dashboard/api/cache/refresh` | 立即刷新路由缓存 | 否 |
| `POST /dashboard/api/cooldown/{id}/ack-recover` | 人工确认账号恢复 | 否 |
| `POST /dashboard/api/sources/{provider}/toggle` / `run-now` | 采集源开关/立即采集 | 否 |
| `PUT /dashboard/api/settings/router-score-weights` | 调路由 5 维权重 | 否 |
| `PUT /dashboard/api/settings/alert` | 调告警阈值 | 否 |
| `POST /dashboard/api/suggestions/{type}/{id}/approve` / `reject` | 审批路由建议 | ✔ 半自动 |
| `POST /dashboard/api/suggestions/apply-all` | 应用全部已批准建议 | ✔ 半自动 |
| `POST /dashboard/api/pending-review/{id}/approve` / `reject` | 能力变更人工确认 | ✔ 半自动 |
| `POST /dashboard/api/promotion-reviews/{id}/approve` / `reject` | 优惠入库审批 | ✔ 半自动 |

> 变更后生效：多数配置写 DB 后路由缓存 ≤60s 自动刷新；要立即生效调
> `POST /dashboard/api/cache/refresh`。

## 4. CLI 等价命令（`python scripts/aimc.py`）

| CLI | 等价 REST |
|-----|-----------|
| `status` / `models [--include-models]` / `auth` | GET /health · /v1/models · auth/status |
| `accounts list` / `add --provider P --name N --api-key K` / `enable\|disable ID` / `rotate ID --new-key K` / `expire-check` | 对应 accounts 端点 |
| `keys list` / `create --username U [--label L] [--admin] [--daily-limit N]` / `revoke ID` | 对应 keys 端点 |
| `usage summary\|per-user\|cost-by-model\|cost-by-group\|cost-by-user\|cost-trend [--days N]` | 对应 usage/stats 端点 |
| `suggestions pending` / `approve\|reject TYPE ID` / `apply-all` | 对应 suggestions 端点 |
| `cooldown pending` / `ack ID` | 对应 cooldown 端点 |
| `cache stats [--days N]` / `refresh` | 对应 cache 端点 |

全局：`--base-url` 覆盖 `AIMC_BASE_URL`；输出统一 JSON；exit 0 成功 / 1 HTTP 或连接错误 / 2 参数错误。
SSRF 防护：CLI 只允许指向回环/私网 AIMC 实例。

## 5. 尚未契约化（后续版本）

- **MCP server**：把上表封装为 MCP tools — 规划中（V6.1 后续 / V6.2），当前 agent 走 REST/CLI 即可。
- **写操作幂等性承诺**：`keys/{id}/revoke` 幂等；其余写操作重复调用行为见各自 docstring（`dev/api.md` 有摘要）。
