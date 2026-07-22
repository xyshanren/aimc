# AIMC API 接口文档

## 概述

AIMC 网关提供 OpenAI 兼容接口，客户端只需将 `base_url` 指向本网关即可无缝切换。

**基础地址**：`http://localhost:8080`

**鉴权**：如配置了 `AIMC_API_KEY`，所有 `/v1/*` 请求需携带 `Authorization: Bearer <key>` 头。

---

## 网关接口

### GET /health

健康检查探针。

**响应示例**：
```json
{"status": "ok", "service": "aimc"}
```

---

### GET /v1/models

列出所有可用模型（聚合全部活跃渠道）。

**响应示例**：
```json
{
  "object": "list",
  "data": [
    {"id": "deepseek-chat", "object": "model", "owned_by": "aimc"},
    {"id": "deepseek-reasoner", "object": "model", "owned_by": "aimc"},
    {"id": "qwen-max", "object": "model", "owned_by": "aimc"},
    {"id": "qwen-plus", "object": "model", "owned_by": "aimc"}
  ]
}
```

---

### POST /v1/chat/completions

OpenAI 兼容的对话补全接口。网关自动路由到最优渠道。

**请求体**（与 OpenAI 格式完全一致）：
```json
{
  "model": "deepseek-chat",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "你好"}
  ],
  "stream": false,
  "temperature": 0.7,
  "max_tokens": 1024
}
```

**非流式响应**：
```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "model": "deepseek-chat",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "你好！有什么可以帮助你的？"},
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 15,
    "total_tokens": 25
  }
}
```

**流式响应**（`stream: true`）：

返回 `text/event-stream`，原样透传上游 SSE 数据：
```
data: {"id":"chatcmpl-xxx","choices":[{"delta":{"content":"你"},"index":0}]}

data: {"id":"chatcmpl-xxx","choices":[{"delta":{"content":"好"},"index":0}]}

data: [DONE]
```

**错误响应**：
```json
{
  "error": {
    "message": "No available channel for model: xxx",
    "type": "service_unavailable"
  }
}
```

---

## 路由决策逻辑

请求到达后，网关按以下流程选择最优渠道：

1. **模型匹配** — 筛选支持该 model 的渠道（支持前缀模糊匹配）
2. **时段匹配** — 根据当前时间匹配峰/谷价格
3. **频限过滤** — 排除 QPM/TPM/并发已达阈值的账号
4. **配额过滤** — 排除总配额耗尽的账号
5. **多维排序** — 价格 ASC → 剩余配额 DESC → 优先级 DESC
6. **故障转移** — 首选失败时自动切换下一候选（最多重试 2 次）

---

## Dashboard API

### GET /dashboard/api/channels

渠道状态列表。

```json
[
  {
    "id": 1,
    "name": "deepseek_official",
    "provider": "deepseek",
    "status": "active",
    "priority": 10,
    "accounts": 1,
    "active_accounts": 1
  }
]
```

### GET /dashboard/api/promotions?status=active

优惠活动列表。

```json
[
  {
    "id": 1,
    "provider": "deepseek",
    "title": "DeepSeek 深夜半价",
    "discount_desc": "5折",
    "end_date": "2026-08-01",
    "status": "active",
    "confidence": 0.95,
    "url": "https://..."
  }
]
```

### GET /dashboard/api/prices

当前价格列表（按输入价格升序）。

```json
[
  {
    "id": 1,
    "channel_name": "deepseek_official",
    "provider": "deepseek",
    "model": "deepseek-chat",
    "price_type": "offpeak",
    "time_range": "00:00-08:00",
    "input_price": 0.5,
    "output_price": 1.0
  }
]
```

### GET /dashboard/api/usage/summary?days=7

用量统计摘要（按天聚合）。

```json
[
  {
    "date": "2026-07-22",
    "request_count": 42,
    "total_input_tokens": 15000,
    "total_output_tokens": 8000,
    "avg_latency_ms": 1200.5
  }
]
```

---

## 错误码说明

| HTTP 状态码 | 含义 |
|-------------|------|
| 400 | 请求体格式错误 / 缺少 model 字段 |
| 401 | API Key 无效（如配置了鉴权） |
| 502 | 上游渠道连接失败 |
| 503 | 无可用渠道（全部频限/配额耗尽） |
| 504 | 上游响应超时（120s） |
