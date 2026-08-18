# 日常使用 AIMC (调用 API + 模型组)

> 受众：想用 OpenAI SDK 或 curl 调 AIMC 的用户
> 前提：AIMC 已部署并接入账号（见 [install.md](install.md) + [configure.md](configure.md)）

## 1. OpenAI SDK（推荐）

### Python

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",  # AIMC 网关地址
    api_key="your-aimc-key",              # 跟 .env 里 AIMC_API_KEY 一致
)

# 流式
stream = client.chat.completions.create(
    model="tier:flagship",  # 或具体 model 名 "deepseek-v4-pro"
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")

# 非流式
resp = client.chat.completions.create(
    model="tier:flagship",
    messages=[{"role": "user", "content": "Hello!"}],
    max_tokens=100,
)
print(resp.choices[0].message.content)
```

### Node.js

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://localhost:8080/v1",
  apiKey: "your-aimc-key",
});

const stream = await client.chat.completions.create({
  model: "tier:flagship",
  messages: [{ role: "user", content: "Hello!" }],
  stream: true,
});
for await (const chunk of stream) {
  process.stdout.write(chunk.choices[0]?.delta?.content || "");
}
```

## 2. curl（不需要 SDK）

```bash
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-aimc-key" \
  -d '{
    "model": "tier:flagship",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 100,
    "stream": true
  }'
```

## 3. 模型组路由 (`tier:xxx` / `scene:xxx`)

**AIMC 核心能力**：用 `tier:flagship` 这种语义标签，AIMC 在组内跨 channel 选最便宜的可用 model。

| 组 | 包含的 model（举例） | 适合 |
|----|---------------------|------|
| `tier:flagship` | GLM-5.2, deepseek-v4-pro | 复杂推理 |
| `tier:strong` | deepseek-v4-flash, minimax-m3, glm-4-plus | 主力通用 |
| `tier:balanced` | deepseek-v3, minimax-m2.5 | 性价比 |
| `tier:light` | minimax-m2.7-highspeed, qwen3-8b, glm-4-flash | 轻量免费 |
| `tier:vision` | minimax-m3, qwen3-vl 系列, glm-4.5v | 多模态视觉 |
| `tier:code` | minimax-m2.7, deepseek-v4-flash, deepseek-v3.2 | 代码 |

**示例**：

```python
# 自动选最便宜的 flagship
client.chat.completions.create(model="tier:flagship", messages=...)

# 纯文本请求会跳过 multimodal 候选 (P0-3)
# minimax 上游 429 时自动 failover 到其他 channel (P1-15)
```

## 4. 精确路由 vs 组路由

```python
# 精确路由: AIMC 只会用 siliconflow_aggregator channel (有该 model)
client.chat.completions.create(model="deepseek-v4-pro", messages=...)

# 组路由: AIMC 在 tier:flagship 内选最便宜
client.chat.completions.create(model="tier:flagship", messages=...)
```

如果精确路由失败（channel 不可用 / 限额耗尽），AIMC **自动 fallback** 到同 model 的其他 channel（如果有）。

## 5. 多模态（Vision）

```python
import base64

# 图片编码
img_b64 = base64.b64encode(open("cat.png", "rb").read()).decode()

resp = client.chat.completions.create(
    model="tier:vision",  # 自动选 GLM-4.5V / Qwen3-VL / MiniMax-M3
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
        ],
    }],
    max_tokens=200,
)
```

## 6. 响应头（可观测性）

AIMC 在每个响应加 5 个 `X-AIMC-*` header：

```bash
curl -i -X POST http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -d '{"model":"tier:flagship","messages":[{"role":"user","content":"hi"}]}' | head

# 响应头:
# X-AIMC-Actual-Model: deepseek-v4-pro
# X-AIMC-Actual-Channel: deepseek_official
# X-AIMC-Actual-Group-Id: tier:flagship
# X-AIMC-Actual-Tier: T0
# X-AIMC-Actual-Capabilities: ["reasoning","code",...]
```

## 7. Prompt Cache（自动）

AIMC **自动**注入 cache headers：
- **Anthropic 风格 channel**（`api_style=anthropic`）：前 2 条 message 加 `cache_control: {type: ephemeral}`
- **OpenAI 风格 channel**（默认）：body 加 `prompt_cache_key`（system content hash）

效果：cache 命中后节省约 90% input 价格。看 cache 节省金额：访问 [dashboard.md §Cache 节省](dashboard.md#cache-节省)。

## 故障排查（调用阶段）

| 症状 | 检查 | 修复 |
|------|------|------|
| 401 Unauthorized | API key 错 | 确认 `AIMC_API_KEY` 跟 SDK 一致 |
| 503 No available channel | 所有 channel 不可用 | [troubleshooting.md](troubleshooting.md) |
| 429 Quota exceeded | 限额耗尽 | 等 cooldown (60s) 或加账号 |
| 上游 429 透传 | TokenPlan 耗尽 (如 minimax) | P1-15 自动 failover，但单 channel model（如 m3）会透传 |
| 流式断流 | 网关 timeout | 调 `REQUEST_TIMEOUT` env |
