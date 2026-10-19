# 日常使用 AIMC (调用 API + 模型组)

> 受众：想用 OpenAI SDK 或 curl 调 AIMC 的用户
> 前提：AIMC 已部署并接入账号（见 [install.md](install.md) + [configure.md](configure.md)）

## 1. OpenAI SDK（推荐）

### Python

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:18080/v1",  # AIMC 网关地址
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
  baseURL: "http://localhost:18080/v1",
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
curl -X POST http://localhost:18080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-aimc-key" \
  -d '{
    "model": "tier:flagship",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 100,
    "stream": true
  }'
```

## 3. 模型组路由 (`tier:xxx`)

**AIMC 核心能力**：用 `tier:flagship` 这种语义标签，AIMC 在组内跨 channel 选最便宜的可用 model。
组成员随渠道配置自动种子（`channels_catalog.py` 分级 + 三层控制 `specialized`/`auto_group`/`group_priorities`，与生产策展一致），下表为生产当前形态：

| 组 | 包含的 model（生产当前成员, priority） | 适合 |
|----|---------------------|------|
| `tier:flagship` | deepseek-flash(100), kimi-k3(105), glm-5.3(115) | 复杂推理 |
| `tier:strong` | minimax-m3.1-flash(100), glm-5.2(108), glm-5.3-flash(120) | 主力通用 |
| `tier:balanced` | sensenova-6.8-flash-lite(100), minimax-m3(110) | 性价比 |
| `tier:light` | qwen3-8b(100), qwen3.5-4b(110), glm-4.7-flash(130)（免费） | 轻量 |
| `tier:vision` | glm-5.3-flash(100), deepseek-flash(120) | 多模态视觉 |
| `tier:vision-light` | glm-4.6v-flash(100), sensenova-6.8-flash-lite(105), minimax-m3(110) | 轻量视觉免费兜底 |
| `tier:code` | kimi-k2.7-code(100), qwen3-coder-30b-a3b-instruct(105)（特化代码模型） | 代码 |

视觉请求 fallback 链：`tier:vision → tier:vision-light → tier:light`（V6.0 起）。
注意：vision 组成员均为多模态，纯文本请求会被 P0-3 规则跳过并沿 fallback 落到下游文本组——测 vision 组自身请带图。

**示例**：

```python
# 自动选最便宜的 flagship
client.chat.completions.create(model="tier:flagship", messages=...)

# 组内指定 model（V4.6.5）：斜杠后缀锁定组内某个成员，
# 其余成员作为 fallback（该 model 不可用时仍走组内其他成员）
client.chat.completions.create(model="tier:vision/glm-5.3-flash", messages=...)

# P0-3（V6.0 收窄）：纯文本请求只在 require_vision 组内跳过 multimodal 候选；
# 多分组模型（如 glm-5.3-flash）在 strong 等文本组的角色不受影响
# 上游 429 时自动 failover 到其他 channel (P1-15)
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
    model="tier:vision",  # glm-5.3-flash / deepseek-flash 领跑, 不可用降级 vision-light (glm-4.6v-flash 免费)
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
curl -i -X POST http://localhost:18080/v1/chat/completions \
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

AIMC **自动**注入 cache 标记（channel 开启 `supports_prompt_cache` 时，如硅基流动/DeepSeek）：
- **OpenAI 风格 channel**（默认）：body 加 `prompt_cache_key` = 稳定前缀分段 hash
  （全量 system + 首条 user 消息，各截断 4096 字符）。多轮 agent 对话跨轮同 key →
  上游同 cache shard → 前缀命中率高。调用方已传 `prompt_cache_key` 时保留不覆盖。
- **Anthropic 风格 channel**（`api_style=anthropic`）：system 消息 + 第一条 user 消息加
  `cache_control: {type: ephemeral}`（校验 role，不盲目标前两条）

效果：cache 命中后 input 价格大幅下降（硅基流动约 1 折）。看命中率和节省金额：
Dashboard「缓存节省」栏（OpenAI `cached_tokens` 与 Anthropic `cache_read` 都已纳入统计）。

## 故障排查（调用阶段）

| 症状 | 检查 | 修复 |
|------|------|------|
| 401 Unauthorized | API key 错 | 确认 `AIMC_API_KEY` 或多用户 key（`aimc-` 前缀）正确 |
| 401（user key） | key 被吊销 | 管理员在 Dashboard「Key 管理」重新发 key |
| 429 daily/monthly exceeded | 个人 token 限额超额 | 等次日/次月，或找管理员调额（见 [configure.md §4](configure.md)） |
| 503 No available channel | 所有 channel 不可用 | [troubleshooting.md](troubleshooting.md) |
| 429 Quota exceeded | 上游账号限额耗尽 | 等 cooldown (60s) 或加账号 |
| 上游 429 透传 | TokenPlan 耗尽 (如 minimax) | P1-15 自动 failover，但单 channel model（如 m3）会透传 |
| 流式断流 | 网关 timeout | 调 `AIMC_REQUEST_TIMEOUT` env |
