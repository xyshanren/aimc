# API 端点参考

> 受众：想调 AIMC API（OpenAI SDK 调用 + Dashboard HTTP 调用）
> 前提：本地或远程 AIMC 已部署

## 网关 API（OpenAI 兼容）

**基础 URL**：`http://<host>:8080`

**鉴权**：如果配置了 `AIMC_API_KEY`，所有 `/v1/*` 请求需带 `Authorization: Bearer <key>`。

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

返回 38 个 APScheduler job 的健康状态（`last_success_at` / `health`: healthy/warning/stale/unknown）。

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
       cached_input_tokens, group_id, created_at
FROM usage_logs ORDER BY id DESC LIMIT 5
```

| 字段 | 用途 |
|------|------|
| `channel_id` / `account_id` | 走哪个渠道/账号 |
| `model` | 实际命中的 model（精确路由 = requested_model；组路由 = 实际命中）|
| `group_id` | tier:/scene: 路由时的组 ID（精确路由 NULL）|
| `input_tokens` / `output_tokens` | 真实 token 用量 |
| `cached_input_tokens` | cache 命中 token（P1-8 起填）|
| `latency_ms` | 网关总延迟 |

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
