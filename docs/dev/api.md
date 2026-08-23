# API 端点参考

> 受众：想调 AIMC API（OpenAI SDK 调用 + Dashboard HTTP 调用）
> 前提：本地或远程 AIMC 已部署

## 网关 API（OpenAI 兼容）

**基础 URL**：`http://<host>:8080`

**鉴权**：如果配置了 `AIMC_API_KEY`，所有 `/v1/*` 请求需带 `Authorization: Bearer <key>`。

V4.8 起支持多用户 key：`aimc-` 前缀的 key 先查 `api_keys` 表（SHA256 匹配，命中记
`caller_id` 并检查个人日/月限额），否则回退 legacy `AIMC_API_KEY`（无 caller 记账）。
见 [user/configure.md §4](../user/configure.md)。

### POST /v1/chat/completions

OpenAI Chat Completions 协议，AIMC 网关透传。

```bash
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AIMC_API_KEY" \
  -d '{
    "model": "tier:flagship",  # 或具体 model 名 "deepseek-v4-pro"
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 100,
    "stream": false,
    "temperature": 0.7
  }'
```

**Model 名**：
- 具体 model：`deepseek-v4-pro` / `qwen3-32b` / `glm-5.2` 等
- 模型组：`tier:flagship` / `tier:balanced` / `tier:vision` / `scene:code` 等
- 本地 model：见 [user/setup.md](../user/setup.md) 的本地模型接入

**响应头**（AIMC 加）：
- `X-AIMC-Actual-Model`: 实际命中的 model
- `X-AIMC-Actual-Channel`: 实际命中的 channel
- `X-AIMC-Actual-Group-Id`: tier:/scene: 路由时的组 ID
- `X-AIMC-Actual-Tier`: model 的 tier 等级（T0/T1/T2/T3/T4）
- `X-AIMC-Actual-Capabilities`: model 的能力列表（JSON）

### GET /v1/models

```bash
curl http://localhost:8080/v1/models -H "Authorization: Bearer $KEY"
```

返回 AIMC 支持的 model 列表（OpenAI 兼容格式）。

### GET /health

健康检查（无需鉴权）：

```bash
curl http://localhost:8080/health
# {"status":"ok"}
```

## Dashboard API（HTML 浏览器 + JSON 端点）

**基础 URL**：`http://<host>:8080/dashboard/`

**鉴权**：如果配置了 `AIMC_DASHBOARD_TOKEN`，所有 `/dashboard/*` 请求需带 token（3 种方式）：
- `Authorization: Bearer <token>`
- Cookie: `aimc_dashboard_token=<token>`
- Query: `?token=<token>`

### GET /dashboard/

主页面（HTML，浏览器访问）。

### GET /dashboard/api/channels

```bash
curl http://localhost:8080/dashboard/api/channels \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"
```

返回所有渠道列表（含 `id, name, provider, api_base, api_style, supports_prompt_cache, status`）。

### PUT /dashboard/api/channels/{channel_id}

更新渠道字段（`api_base` / `api_style` / `supports_prompt_cache` / `status` / `priority`）。

```bash
curl -X PUT http://localhost:8080/dashboard/api/channels/1 \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN" \
  -d '{"api_base":"https://api.siliconflow.cn/v1","supports_prompt_cache":1}'
```

### GET /dashboard/api/cache/stats?days=7

```bash
curl 'http://localhost:8080/dashboard/api/cache/stats?days=7' \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"
```

返回：
- `period_days / total_input_tokens / total_cached_tokens / hit_rate_pct / saved_cost_cny`
- `by_channel`: 每渠道明细
- `by_model`: TOP 10 model 明细

### GET /dashboard/api/data-freshness

```bash
curl http://localhost:8080/dashboard/api/data-freshness \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"
```

返回 ~36 个 APScheduler job 的健康状态（`last_success_at` / `health`: healthy/warning/stale/unknown）。

### GET /dashboard/api/suggestions?status=pending

```bash
curl 'http://localhost:8080/dashboard/api/suggestions?status=pending' \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"
```

返回 `model_change_suggestions` + `group_change_suggestions` 列表（按 confidence DESC 排序）。

### POST /dashboard/api/suggestions/{type}/{id}/approve

批准建议（改 status: pending → approved）：

```bash
curl -X POST http://localhost:8080/dashboard/api/suggestions/model/1/approve \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN" \
  -d '{"reviewed_by":"dashboard_user"}'
```

### POST /dashboard/api/suggestions/{type}/{id}/reject

类似 approve，但 status → rejected。

### POST /dashboard/api/suggestions/apply-all

一键应用所有 status=approved 的建议（直接 UPDATE db，写 applied_at）：

```bash
curl -X POST http://localhost:8080/dashboard/api/suggestions/apply-all \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN" \
  -d '{"min_confidence":0.5,"reviewed_by":"dashboard_user"}'
```

### GET /api/settings/router-score-weights

读取当前 5 维权重：

```bash
curl http://localhost:8080/dashboard/api/settings/router-score-weights \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"
```

返回 `{weights, defaults, override, sum, description}`。

### PUT /api/settings/router-score-weights

修改权重（60s 内 router cache refresh 后生效）：

```bash
curl -X PUT http://localhost:8080/dashboard/api/settings/router-score-weights \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN" \
  -d '{"weights":{"W_PRICE":0.6,"W_QUOTA":0.15,"W_SUCCESS":0.15,"W_RECENCY":0.05,"W_PRIORITY":0.05}}'
```

校验：必须含 5 个 W_* 字段 + [0,1] 范围。

### POST /api/cache/refresh

立即刷新 router cache（不等 TTL）：

```bash
curl -X POST http://localhost:8080/dashboard/api/cache/refresh \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"
# {"status":"ok","channels":6,"models":108}
```

### GET /dashboard/api/settings/alert

读取告警 runtime override（`alert_channel` / `bark_key` / `feishu_webhook_url` 等）。

### PUT /dashboard/api/settings/alert

修改告警 override（白名单字段，`null` 删 override）。

### GET /dashboard/api/stats/per-user?days=7（V4.8）

按 caller 聚合用量（近 N 天）：

```bash
curl 'http://localhost:8080/dashboard/api/stats/per-user?days=7' \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"
```

返回 `{count, days, items: [{caller_id, username, request_count, input_tokens, output_tokens, total_tokens, total_cost, keys: [{key_id, label, request_count, ...}]}]}`。
`caller_id=NULL` 的行显示为 `(未鉴权/legacy)`（走全局 key 的请求）。
V4.8.1 起每行内嵌 `keys` 子数组（按 key label 下钻，key_id 为 NULL 的请求不入子数组）。

### GET /dashboard/api/keys（V4.8）

用户 + key 列表（脱敏，只含 `key_prefix`，永不返回 hash）：

```bash
curl http://localhost:8080/dashboard/api/keys \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"
```

### POST /dashboard/api/keys/{key_id}/revoke（V4.8）

吊销 key（立即失效，不可恢复）：

```bash
curl -X POST http://localhost:8080/dashboard/api/keys/3/revoke \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"
```

### GET /dashboard/api/stats/group_routing

组路由命中分布（近 7 天，按 `usage_logs.group_id` 聚合）：

```bash
curl http://localhost:8080/dashboard/api/stats/group_routing \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"
```

返回 `{summary, groups: [{group_id, request_count, model_distribution, channels_used, total_cost}], unmatched}`。
注：`tier:vision/glm-4.5v` 写法的 group_id 已归一化为 `tier:vision`（2026-08-21 起）。

### GET /dashboard/api/promotions

优惠情报列表（默认过滤 `flagged=1` 低置信度条目）：

```bash
# 只看可信条目
curl http://localhost:8080/dashboard/api/promotions \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"

# 调试：查全部（含 flagged）
curl 'http://localhost:8080/dashboard/api/promotions?include_flagged=true' \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"
```

返回字段含 `url`（采集源页面链接），前端渲染为"查看原文"链接。
按 `(provider, title)` GROUP BY 去重，每个活动只显示最新一条（2026-08-21 起）。

### GET /dashboard/api/pending-review

待确认能力变更列表（`model_capabilities_log` WHERE `approved=0 AND source='ai_infer'`）：

```bash
curl http://localhost:8080/dashboard/api/pending-review \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"
```

返回字段含 `display_name`（来自 `model_registry`，方便用户判断）和 `field_changed`（前端映射为中文标签：create→新模型入库、tier→档位调整、metadata→元信息变更等）。

### GET /dashboard/api/sources（V4.8.3）

采集源列表（合并内存定义 + DB 状态）：

```bash
curl http://localhost:8080/dashboard/api/sources \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"
```

返回 `{count, sources: [{provider, source_type, url, poll_interval, status, last_check_at, error_count, consecutive_no_change, enabled, api_endpoint_configured}]}`。

### POST /dashboard/api/sources/{provider}/toggle（V4.8.3）

启用/禁用该 provider 的所有源（DB `status` 在 `disabled` / `active` 间切换，
scheduler 对 `disabled` 源跳过采集）：

```bash
curl -X POST http://localhost:8080/dashboard/api/sources/deepseek/toggle \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"
```

返回 `{"status":"ok","provider","new_status","toggled"}`（`toggled` = 本次切换的源数，
取决于 DB 中已有行数）。未知 provider 返回 404。

### POST /dashboard/api/sources/{provider}/interval（V4.8.3）

调整该 provider 所有源的轮询间隔（写入 DB `intel_sources.poll_interval`，
**重启后生效**——实际调度间隔在启动时从内存 source 定义读取）：

```bash
curl -X POST 'http://localhost:8080/dashboard/api/sources/deepseek/interval?interval=3600' \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"
```

`interval` 取值 60~604800 秒。

### POST /dashboard/api/sources/run-now

立即触发指定 provider 的采集（后台 fire-and-forget，接口立即返回）：

```bash
curl -X POST 'http://localhost:8080/dashboard/api/sources/run-now?provider=deepseek' \
  -H "Authorization: Bearer $AIMC_DASHBOARD_TOKEN"
# {"status":"started","provider":"deepseek","count":4}
```

未知 provider 返回 400。

## 错误响应格式

AIMC 网关错误（4xx/5xx）用 OpenAI 兼容格式：

```json
{
  "error": {
    "message": "No available channel for model: deepseek-v4-pro",
    "type": "service_unavailable",
    "code": null
  }
}
```

具体错误码：
- `401`: Invalid API key
- `429`: Quota exceeded (有 `retry-after` header) / upstream Token Plan 耗尽
- `500`: 上游错误 / AIMC 内部
- `503`: No available channel for model

## 内部数据结构

### `usage_logs` 行示例

```sql
SELECT model, input_tokens, output_tokens, status_code,
       cached_input_tokens, group_id, caller_id, created_at
FROM usage_logs ORDER BY id DESC LIMIT 5
```

| 字段 | 用途 |
|------|------|
| `channel_id` / `account_id` | 走哪个渠道/账号 |
| `caller_id` | 调用方 user id（V4.8；走全局 key 时 NULL）|
| `key_id` | 命中的 api_keys.id（V4.8.1；按 key label 下钻用，走全局 key 时 NULL）|
| `model` | 实际命中的 model（精确路由 = requested_model；组路由 = 实际命中）|
| `group_id` | tier:/scene: 路由时的组 ID（精确路由 NULL；`tier:xxx/model` 写法已归一化存 `tier:xxx`）|
| `input_tokens` / `output_tokens` | 真实 token 用量 |
| `cached_input_tokens` | cache 命中 token（OpenAI 风格，P1-8 起填）|
| `cache_read_input_tokens` / `cache_creation_input_tokens` | Anthropic 风格 cache 读/写 token |
| `latency_ms` | 网关总延迟 |
| `created_at` | 本地时间（2026-08-21 起写入本地时钟；之前 UTC，存量已迁移）|

### `price_snapshots` 行示例

```sql
SELECT model, input_price, output_price, cached_input_price, currency
FROM price_snapshots WHERE channel_id=3 LIMIT 10
```

| 字段 | 来源 |
|------|------|
| `input_price` / `output_price` | 阶段B 解析 SF pricing 网页的 53 行真实价 |
| `cached_input_price` | P0-3 follow-up hardcode 9 行（DeepSeek V3 等 cache 0）|
| `currency` | CNY（默认） |

### `model_change_suggestions` 行示例

```sql
SELECT id, model_id, change_type, suggested_value, confidence, status
FROM model_change_suggestions ORDER BY confidence DESC LIMIT 5
```

| 字段 | 含义 |
|------|------|
| `change_type` | tier_change / add_to_group / remove_from_group |
| `confidence` | 0-1（4 数据源加权） |
| `status` | pending / approved / rejected / applied |

## SDK 兼容性

AIMC 严格兼容 OpenAI Chat Completions 协议，所以以下 SDK 都直接能用：

```python
# Python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8080/v1", api_key="...")

# Node.js
import OpenAI from "openai";
const client = new OpenAI({baseURL: "...", apiKey: "..."});

# Go
# github.com/sashabaranov/go-openai
# cfg := openai.DefaultConfig(...)  // baseURL 改

# 任意 OpenAI 协议 SDK
```

## 流式响应

```python
stream = client.chat.completions.create(
    model="tier:flagship",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")
```

AIMC 直接透传上游 SSE 流（不解析）。
