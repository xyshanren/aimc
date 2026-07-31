# Agent × AIMC 协同方案

> 状态：草案 v0.1（2026-07-29）
> 关联文档：[architecture.md §四 多维模型组设计](architecture.md) / [development-plan.md §Phase 3 情报联动](development-plan.md)
> 配套实现：Phase 3 后续 / Phase 3.5 Agent 协同层

## 一、问题陈述

AIMC 是**网关**——管"用哪个渠道最便宜"，不管"该用什么模型"。
Agent 是**决策者**——管"这个任务该用哪个模型"，不管"这个模型在哪家更便宜"。

两者协同的核心矛盾：

```
Agent 想做智能路由（按任务类型/复杂度分模型）
   ↓
但 Agent 不知道当前有哪些 model 可用
   ↓
而且 model 列表是动态的（情报层会自动入组/出组）
```

**关键判断**：AIMC 不应该做 Agent 的智能路由（这违反"网关只管渠道"的设计边界）。但 AIMC 必须解决"Agent 怎么知道有哪些 model + 它们的特性"这个问题。

## 二、职责边界（核心原则）

| 维度 | Agent 负责 | AIMC 负责 |
|------|-----------|-----------|
| **任务类型识别** | "这个任务是代码/推理/视觉" | — |
| **能力档选择** | "代码任务 → scene:code" | — |
| **渠道选择** | — | "scene:code 组里 4 个 model，选最便宜的" |
| **多账号轮询** | — | "硅基流动 QPM 满 → 切 DeepSeek 官方" |
| **故障转移** | — | "deepseek 5xx → tier:strong 兜底" |
| **能力标签维护** | — | "tier/capabilities/modality 元信息" |
| **价格感知** | — | "组内 cheapest 排序" |

**铁律**：AIMC 不替 Agent 做"该用什么模型"的决策；Agent 不替 AIMC 做"该走哪个渠道"的决策。

## 三、主流方案调研

调研了 5 个有代表性的方案：

### 1. OpenRouter v2 Smart Routing（2026）

- **核心机制**：用户请求时只声明需求（"需要 reasoning + vision + 长上下文"），路由器选最便宜满足条件的模型
- **能力标签体系**：reasoning / coding / vision / tool_use / structured_output / long_context
- **Agent 怎么用**：`model="openrouter/auto"` + 附加 requirements
- **节省**：30-50% 成本下降
- **缺点**：牺牲可预测延迟（最便宜≠最快）

### 2. OpenCode Zen

- **核心机制**：抽象"capability profiles"（`fast edit` / `deep reasoning` / `code review`），路由器内部选满足 profile 的最便宜 model
- **Agent 怎么用**：配置 profile 字符串，路由器自动展开
- **扩展**：SLIM plugin 系统允许自定义路由规则

### 3. Claude Code Router (CCR) — 重点参考

- **核心机制**：配置文件里硬编码 5 类任务路由
  ```json
  {
    "background": "本地小模型",
    "think": "推理旗舰",
    "longContext": "长上下文模型",
    "longContextThreshold": 60000,
    "image": "视觉模型",
    "webSearch": "支持联网搜索的模型"
  }
  ```
- **Agent 怎么用**：CCR 作为本地网关（默认 `:3456`），Agent 接 CCR 而不是直接接厂商
- **支持 Agent**：Claude Code / Codex / Grok CLI / Kimi CLI / OpenCode / Pi / ZCode 等 9 个编程 Agent
- **高级能力**：Fusion 视觉、ToolHub、AgentClaw（微信/钉钉接力）
- **借鉴价值**：profile 命名规范、custom_router.js 自定义路由脚本

### 4. Hermes Agent / Nous Portal

- **核心机制**：Agent 内部决策
  - 复杂推理 → 贵模型
  - 文件操作 → 本地便宜模型
- **配套**：OpenRouter Pareto Code Router（`openrouter/pareto-code` + `mincoding_score` 阈值）
- **Agent 怎么用**：配置 `provider_routing`（sort=price/throughput/latency）+ `fallback_providers` 链

### 5. OpenRouter Pareto Code Router

- **核心机制**：单 API 端点 `openrouter/pareto-code` + `mincoding_score=0.65` → 路由器选 ≥0.65 分的最便宜 coder
- **Agent 怎么用**：纯声明式，不用知道具体 model
- **动态性**：Pareto frontier 每天重算（模型加入/退出自动适应）

## 四、3 个候选方案

基于调研，针对 AIMC 设计 3 个候选方案：

### 方案 A：能力契约式（Agent 启动时拉 + AIMC 响应头反馈）

**核心机制**：
1. Agent 启动时 `GET /v1/models`，缓存全量 model 元信息
2. Agent 用 `model="scene:code"` 等 group 名发请求
3. AIMC 响应头 `X-AIMC-Actual-Model` 告诉 Agent 实际命中
4. Agent 记录历史命中，定期刷新

**Agent 侧改动**：
```python
# 启动时
models = httpx.get("http://aimc:8080/v1/models").json()
capability_cache = {
    "scene:code": "deepseek-v4-flash",  # 历史命中
    "scene:longctx": "kimi-k3",
    "tier:flagship": "kimi-k3",
}

# 发请求
resp = client.chat.completions.create(model="scene:code", messages=...)
actual = resp.headers.get("X-AIMC-Actual-Model")  # 学习真实命中
```

**优点**：
- AIMC 零协议改动（已有 X-AIMC-* 头）
- Agent 主导决策，符合职责边界
- 简单可靠

**缺点**：
- Agent 写代码时假设"scene:code=deepseek"，跑出来可能是 qwen-coder，不一致

### 方案 B：声明式需求（OpenRouter v2 风格）

**核心机制**：Agent 请求时声明能力需求，AIMC 选最便宜满足条件的

```http
POST /v1/chat/completions
{
  "model": "aimc/auto",
  "messages": [...],
  "aimc_requirements": {  // AIMC 扩展字段
    "required_capabilities": ["code", "reasoning"],
    "min_context": 32000,
    "max_input_price": 2.0,
    "exclude_providers": ["local"]
  }
}
```

**优点**：Agent 完全不用关心 model，声明式

**缺点**：侵入 OpenAI 协议（虽然已经在 `extra_body.aimc_preferences` 上加过类似字段）

### 方案 C：Profile 配置式（CCR 风格）

**核心机制**：Agent 启动时读 profile 配置，按任务类型选不同 model

```yaml
# ~/.hermes/config.yaml
models:
  background: "scene:free"
  think: "tier:strong"
  longContext: "tier:flagship"
  default: "tier:balanced"
```

**优点**：
- 跟 CCR 一致，个人开发者最熟悉的模式
- 配置文件即可，零代码改动
- profile 名字跟 AIMC 的 group 名 (`tier:xxx`/`scene:xxx`) 对齐

**缺点**：Agent 写死在配置里，不能跟着 AIMC 自动优化

## 五、推荐组合方案：A + C

**A 主 C 辅**——用方案 A 的"运行时拉取 + 响应头反馈"做自适应，用方案 C 的"profile 配置"做硬性规则。

### 5.1 推荐的 Agent 配置（方案 C）

```yaml
# ~/.hermes/config.yaml
models:
  background: "scene:free"      # 后台任务 → 本地或免费档
  think: "tier:strong"          # 推理任务 → 强档
  longContext: "tier:flagship"  # 长上下文 → 旗舰
  default: "tier:balanced"      # 默认 → 均衡
```

**为什么用 group（`tier:xxx`/`scene:xxx`）而不是具体 model**：
- AIMC 已在 `tier:flagship` 组里准备好 4 个候选 model，路由 cheapest
- 跨模型比价是 AIMC 的核心能力，Agent 不该重复实现
- 模型注册表变动时，Agent 配置**零改动**

### 5.2 推荐的运行时机制（方案 A）

Agent 启动时拉取 /v1/models，**不是为了写死 model 名**，而是为了：
1. **验证 group 名有效**：如果配了 `tier:light` 但 AIMC 没有这个组，启动时报错
2. **记录 fallback 提示**：dashboard 显示"当前 tier:balanced 实际跑的是哪个 model"
3. **辅助能力发现**：如果 Agent 需要能力雷达（"现在我有哪些 vision model 可用"），用这个缓存

```python
# Agent 启动伪代码
def validate_profiles(aimc_url: str, config: dict):
    resp = httpx.get(f"{aimc_url}/v1/models").json()
    valid_groups = {g["id"] for g in resp.get("data_groups", [])}
    for profile, model_id in config["models"].items():
        if model_id.startswith(("tier:", "scene:")):
            if model_id not in valid_groups:
                raise ConfigError(f"profile {profile} 引用了不存在的 group: {model_id}")
        # 精确 model 名不验证 — AIMC fallback 会兜底
```

### 5.3 推荐的动态适应机制（3 步纯观测）

Agent 不需要"实时"知道 AIMC 模型变动。通过 3 个观测机制自然适应：

1. **启动时拉取**：Agent 启动时 GET /v1/models，验证 profile 合法（仅启动时，1 次）
2. **响应头反馈**：每次请求后读 `X-AIMC-Actual-Model`，Agent 可记录“最近 100 次 tier:balanced 跑出 deepseek-v4-flash”——这是**软信号**，**不改变路由决策**，仅供人看
3. **定期刷新**：Agent 启动时 + 每天 1 次（或手动触发）拉取 /v1/models，刷新 group 元信息

**关键不变量**：
- Agent **不写**任何 AIMC 配置
- Agent **不反向调整**自己 profile（除非人手动改）
- AIMC 路由决策**只**听 DB + 情报层，**不听** Agent

**为什么不做“实时推送”**：AIMC 单进程 + 个人用，引入 WebSocket / SSE 推送复杂度太高。Agent 启动 + 定期拉是**足够好**的方案。

## 六、AIMC 侧的最小增强

为了让方案 A+C 落地，AIMC 需要做以下小改动：

### 6.1 /v1/models 元信息增强（必须，~4 小时）

当前 `data` 字段只返回 `{id, object, owned_by}`，需要加上 `aimc_metadata`：

```json
{
  "object": "list",
  "data": [
    {
      "id": "deepseek-v4-pro",
      "object": "model",
      "owned_by": "aimc",
      "aimc_metadata": {
        "family": "deepseek",
        "tier": "T1",
        "capabilities": ["reasoning", "code", "math"],
        "modality": "text",
        "context_length": 128000,
        "is_open_source": true,
        "providers": ["deepseek_official", "siliconflow_aggregator"]
      }
    }
  ],
  "data_groups": [
    {
      "id": "tier:balanced",
      "type": "model_group",
      "display_name": "均衡性价比组",
      "tier": "T2",
      "strategy": "cheapest",
      "fallback_group": "tier:light",
      "members": ["deepseek-v4-flash", "qwen-plus", "glm-4-flash", ...],
      "member_count": 7,
      "aimc_metadata": {
        "fallback_chain": ["tier:light", null],  // 降级链
        "member_tiers": ["T2", "T2", "T2"]       // 每个 member 的 tier
      }
    }
  ]
}
```

**实现**：修改 `gateway/routes.py` 的 `list_models()`，从 `route_engine._registry_cache` / `_groups_cache` 读元信息
**兼容性**：OpenAI 客户端忽略 `aimc_metadata`，向后兼容

### 6.2 响应头增强（已有，可选再增强）

当前已有 3 个响应头（Phase 2.5）：
- `X-AIMC-Actual-Model`：实际命中的 model
- `X-AIMC-Actual-Channel`：实际命中的渠道
- `X-AIMC-Group-Id`：命中的 group（仅组路由）

**可选增强**：加 `X-AIMC-Actual-Tier` 和 `X-AIMC-Actual-Capabilities`（JSON），让 Agent 不必再去查 model registry。

### 6.3 文档与示例（必须，~2 小时）

写 `docs/agent-integration.md`（本文档）+ 给 hermes-tray 提 PR 集成示例。

## 七、Agent 侧适配清单

### 7.1 最小适配（不推荐，但能用）

Agent 不改任何代码，直接：
- 把 `model="deepseek-chat"` 换成 `model="tier:balanced"`
- AIMC 自动在组内选最便宜的

**适用场景**：只想用 AIMC 省钱，不关心任务分级
**缺点**：复杂任务也走 tier:balanced，质量下降

### 7.2 推荐适配（CCR profile 风格）— **本项目推荐**

Agent 配置文件加 profile：
```yaml
# ~/.hermes/config.yaml
models:
  background: "scene:free"
  think: "tier:strong"
  longContext: "tier:flagship"
  default: "tier:balanced"
```

启动时拉一次 `/v1/models` 验证 profile 合法。

**适用场景**：个人编程 Agent，有明确的任务分级需求
**优点**：简单可控，配置即用

### 7.3 高级适配（被动观测，不动路由决策）

**重要**：这里的“学习”是**被动观测**——Agent 记录 AIMC 实际命中，**不**主动调整 AIMC 配置或路由决策。

Agent 加 3 个观测增强：
1. **响应头记录**：每次请求读 `X-AIMC-Actual-Model`，累积"group → 实际 model"命中率（仅写入内存/日志，不写回任何配置）
2. **统计报告生成**：周期或手动生成“最近 100 次 tier:balanced 命中分布”报告，**给人看**（不给 Agent 自己看）
3. **fallback 事件记录**：当 `X-AIMC-Group-Id` 变了（如 tier:flagship 降级到 tier:strong），记录“降级事件”供 dashboard 展示

**具体代码示例**（~20 行）：

```python
import time
from collections import defaultdict

# 纯内存累积，不需要持久化
routing_log = []          # 全量原始记录 (group, model, channel, ts)
group_distribution = defaultdict(lambda: defaultdict(int))  # group → {model: count}

def observe_routing(response, requested_group: str):
    """从 AIMC 响应头读实际命中，仅记录不干预"""
    actual_model = response.headers.get("X-AIMC-Actual-Model")
    actual_channel = response.headers.get("X-AIMC-Actual-Channel")
    actual_group = response.headers.get("X-AIMC-Group-Id", requested_group)

    if not actual_model:
        return  # 不是组路由，无观测价值

    routing_log.append({
        "group": actual_group, "model": actual_model,
        "channel": actual_channel, "ts": time.time(),
    })
    group_distribution[actual_group][actual_model] += 1

def report(group_id: str) -> str:
    """生成命中率报告（人读，不参与路由决策）"""
    dist = group_distribution.get(group_id, {})
    total = sum(dist.values())
    if total == 0:
        return f"  {group_id}: 暂无数据"

    lines = [f"  {group_id} 最近 {total} 次命中分布:"]
    for model, count in sorted(dist.items(), key=lambda x: -x[1]):
        pct = count * 100 // total
        lines.append(f"    {model:<30} {pct:>3}%")
    return "\n".join(lines)

# 调用方
response = client.chat.completions.create(model="tier:balanced", messages=...)
observe_routing(response, requested_group="tier:balanced")

# 人手动查看
print(report("tier:balanced"))
# tier:balanced 最近 247 次命中分布:
#   deepseek-v4-flash               62%
#   qwen-plus                       21%
#   glm-4-flash                     11%
#   ... (fallback events)            6%
```

**人根据报告做决策**（**不是 Agent 自动决策**）：
- “tier:balanced 总跑 deepseek-v4-flash，要不要换成 deepseek-v4-pro 提升质量？” → **人**手动改 profile
- “tier:strong 经常降级到 tier:balanced，是不是该扩 deepseek 的 quota？” → **人**手动加账号

**适用场景**：重度 Agent 用户，想做精细化成本/质量调优
**缺点**：开发成本高（记录+报告 UI）

### 7.4 为什么 Agent 不主动学习、不反向调整

如果 Agent 反向调整 AIMC 配置（比如“我看 tier:balanced 总跑 deepseek，那以后直接调 deepseek”），会存在 4 个问题：

1. **破坏职责边界**：Agent 又当路由决策者了（违反“网关管渠道”原则）
2. **失去跨模型比价**：AIMC 的核心价值没了，Agent 写死变成单渠道代理
3. **价格变动失效**：AIMC 路由 cheapest，硟基流动降费时自动切；Agent 写死就丢了
4. **复杂度爆炸**：需要双向通信、冲突解决、版本同步

**铁律**：AIMC 只听 .env / DB 配置 + 情报层自动入组，**不听** Agent 反向推送。

## 八、Phase 3 协同任务

Phase 3 原本是"情报联动模型组"（自动入组/出组）。结合本文档，建议 Phase 3 增加以下并行小任务：

| 任务 | 优先级 | 工作量 | 依赖 |
|------|--------|--------|------|
| /v1/models 元信息增强（§6.1） | P0 | 4h | Phase 2 已完成 |
| docs/agent-integration.md（本文档） | P0 | 2h | 无 |
| hermes-tray 集成 PR（§7.2 推荐适配） | P1 | 1d | hermes-tray 配合 |
| /v1/models SSE 推送模型变动 | P3 | 4h | 不推荐，先不做 |
| 响应头增强（§6.2） | P2 | 1h | Phase 2 已完成 |

## 九、对比总结

| 方案 | 协议侵入 | Agent 复杂度 | AIMC 复杂度 | 适用场景 |
|------|---------|------------|------------|---------|
| **A 启动拉 + 响应头反馈** | 0 | 中 | 低 | 想做软学习的 Agent |
| **B 声明式需求** | 中（加 aimc_requirements） | 低 | 中 | 想完全声明式的 Agent |
| **C Profile 配置** | 0 | 低 | 0（已有 group） | 大多数 Agent 推荐 |
| **A+C 推荐组合** | 0 | 低-中 | 低 | 个人编程 Agent |

## 十、决策记录

- **2026-07-29**：本文档草案 v0.1，方案 A+C 推荐组合
- **下一步**：等 Phase 3 启动后，把本文档落地为代码（§6.1 + §7.2）
- **后续迭代**：根据 hermes-tray 集成反馈决定是否引入 §7.3 高级适配
