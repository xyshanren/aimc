# Embeddings / Rerank 直通端点对接文档

> 对接方权威口径。更新：2026-10-01（qwen3-vl 系列 image_tokens 计量修复 + 多模态格式实测规范）。

## 端点

| 端点 | 格式 | 上游 |
|------|------|------|
| `POST /v1/embeddings` | OpenAI 兼容 | siliconflow（单渠道直通） |
| `POST /v1/rerank` | Jina 兼容 | siliconflow（单渠道直通） |

鉴权与 `/v1/chat/completions` 一致（`Authorization: Bearer <key>`）；用量与费用照常入 `usage_logs`。

## 可用模型（网关规范名，必须小写）

| 模型 | 用途 | 输入价 (元/M token) | 输出价 |
|------|------|------|------|
| `baai-bge-m3` | 纯文本向量（1024 维） | 0 | 0 |
| `baai-bge-large-en-v1.5` / `zh-v1.5` | 纯文本向量 | 0 | 0 |
| `pro/baai-bge-m3` | 纯文本向量（Pro 池） | 0.07 | 0.07 |
| `qwen3-embedding-0.6b` / `4b` / `8b` | 纯文本向量 | 0.07 / 0.14 / 0.28 | 0 |
| **`qwen3-vl-embedding-8b`** | **图片/多模态向量（4096 维）** | 0.7 | - |
| `qwen3-vl-reranker-8b` | 多模态重排序 | 0.7 | - |

选型：**纯文本首选 `baai-bge-m3`（免费）；图片向量化才用 `qwen3-vl-embedding-8b`**。embedding 模型不走 `/v1/chat/completions`。

## 图片输入格式（2026-10-01 生产实测）

**可用格式是简式 `{"image": "<data URI>"}`**：

```json
{"model": "qwen3-vl-embedding-8b",
 "input": [{"image": "data:image/jpeg;base64,<...>"}]}
```

实测边界：

- data URI 前缀 `data:<mime>;base64,` **必须带**，纯 base64 无前缀 → 400
- **只收 data URI**，不收 https URL → 400
- 批量：`input` 数组多个 `{"image":...}` 条目 → 200，多图正常出向量
- SF 官方文档的 `image_url` 嵌套格式（`[{"type":"image_url","image_url":{"url":...}}]`）当前上游**不支持**（400 code 20015），勿用
- 支持 MRL：`dimensions` 参数（如 1024）在图像路径同样生效

### 两个静默陷阱（200 但结果不可用）

1. **data URI 当纯字符串传**：`input: ["data:image/png;base64,..."]` → 200，但被按**文本**编码（`image_tokens=0`，prompt_tokens 膨胀）。图片必须放 `{"image": ...}` 对象里。
2. **text 与 image 同条目共存**：`{"text": "...", "image": "<data URI>"}` → 200，但**图片被静默忽略**，只编码文本（`image_tokens=0`）。

判别方法：看响应 `usage.image_tokens`，>0 才是真编码了图片。

## rerank 多模态用法

`query` 支持图片，`documents` 支持图文混合：

```json
{"model": "qwen3-vl-reranker-8b",
 "query": {"image": "data:image/jpeg;base64,<...>"},
 "documents": [{"image": "data:image/jpeg;base64,<...>"},
               {"text": "纯文本候选"}]}
```

## 计量口径

上游对多模态模型将 `image_tokens` 与 `input_tokens` **分列**返回；网关自 2026-10-01 起将两者**合并计入** `usage_logs.input_tokens`（SF pricing 页对两模型仅标单一输入价 0.7 元/M，同价合并；此前 image 部分漏计）。响应体中的 `usage` / `meta.tokens` 仍保持上游原样（分列），合并只影响落库计量。

## 行为特性（对接方需知）

- **无 failover**：单渠道直连设计，上游故障直接返回上游错误码；上游 429 会触发账号冷却（300s 基数指数退避），冷却期内该端点返回 503 `service_unavailable`。建议对接方做指数退避重试。
- **无 AIMC 配额预检**：chat 链路的 rpm/tpm 预检不覆盖本端点，约束直接来自上游限流。
- 计价按 siliconflow 渠道价行；`cached_input_price` 对 bge 系为 0（不支持 cache）。
