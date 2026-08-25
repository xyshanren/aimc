# hermes-agent-cn × AIMC 协同方案

> 状态：草案 v0.2（2026-07-29, 架构术语修订）
> 关联文档：[architecture.md §四 多维模型组设计](architecture.md) / [development-plan.md §Phase 3 情报联动](development-plan.md)
> 配套实现：Phase 3 后续 / Phase 3.5 hermes-agent-cn 协同层


## 📦 历史归档说明

> **本文档已归档**（2026-08-15 文档重组时迁移到 docs/archive/）。
> 新内容见 [docs/index.md](../index.md) 或相关 [docs/dev/](../dev/) / [docs/user/](../user/) 文档。

## 零、上下文与三层架构

> 架构澄清（2026-07-29）：本项目实际为三层架构, 本文档以前含糊使用 'Agent' 指代执行者, 现统一明确:

```
┌────────────────────────────────────────┐
│ hermes-tray (客户端 GUI/CLI)            │  ← 用户交互层, 配置管理
└───────────────┬────────────────────────┘
                │ 标准 API 调用
┌───────────────▼────────────────────────┐
│ hermes-agent-cn (Agent 中枢)             │  ← 任务理解/工具调用/路由决策
│   - profile 选择 (background/think/...) │
│   - /v1/models 启动拉取 + 每日刷新       │
│   - 响应头 X-AIMC-Actual-* 被动观测      │
└───────────────┬────────────────────────┘
                │ OpenAI 协议 (model='tier:balanced')
┌───────────────▼────────────────────────┐
│ AIMC (模型聚合网关)                      │  ← 渠道选择/成本优化/fallback
└───────────────┬────────────────────────┘
                │
       ┌────────┼────────┬────────┐
       ▼        ▼        ▼        ▼
     硅基流动  DeepSeek  Qwen   本地 Ollama
```

**术语约定**:
- **hermes-tray**: 客户端 (GUI/CLI), **不直接调 AIMC**, 通过 hermes-agent-cn
- **hermes-agent-cn**: Agent 中枢, 跟 AIMC 通过 OpenAI 协议交互
- **AIMC**: 模型聚合网关, 跟具体 LLM 渠道交互
- 本文档中除非引用调研对象 (CCR/OpenRouter), 否则 'Agent' = **hermes-agent-cn**

## 一、问题陈述

AIMC 是**网关**——管"用哪个渠道最便宜"，不管"该用什么模型"。
hermes-agent-cn 是**决策者**——管"这个任务该用哪个模型"，不管"这个模型在哪家更便宜"。

两者协同的核心矛盾:

```
hermes-agent-cn 想做智能路由（按任务类型/复杂度分模型）
   ↓
但 hermes-agent-cn 不知道当前有哪些 model 可用
   ↓
而且 model 列表是动态的（情报层会自动入组/出组）
```

**关键判断**：AIMC 不应该做 hermes-agent-cn 的智能路由（这违反"网关只管渠道"的设计边界）。但 AIMC 必须解决"hermes-agent-cn 怎么知道有哪些 model + 它们的特性"这个问题。

## 二、职责边界（核心原则）

| 维度 | hermes-agent-cn 负责 | AIMC 负责 |
|------|-----------|-----------|
| **任务类型识别** | "这个任务是代码/推理/视觉" | — |
| **能力档选择** | "代码任务 → scene:code" | — |
| **渠道选择** | — | "scene:code 组里 4 个 model，选最便宜的" |
| **多账号轮询** | — | "硅基流动 QPM 满 → 切 DeepSeek 官方" |
| **故障转移** | — | "deepseek 5xx → tier:strong 兜底" |
| **能力标签维护** | — | "tier/capabilities/modality 元信息" |
| **价格感知** | — | "组内 cheapest 排序" |

**铁律**：AIMC 不替 hermes-agent-cn 做"该用什么模型"的决策；hermes-agent-cn 不替 AIMC 做"该走哪个渠道"的决策。

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

### 方案 A：能力契约式（hermes-agent-cn 启动时拉 + AIMC 响应头反馈）

**核心机制**：
1. hermes-agent-cn 启动时 `GET /v1/models`，缓存全量 model 元信息
2. hermes-agent-cn 用 `model="scene:code"` 等 group 名发请求
3. AIMC 响应头 `X-AIMC-Actual-Model` 告诉 hermes-agent-cn 实际命中
4. hermes-agent-cn 记录历史命中，每日定时刷新

**hermes-agent-cn 侧改动**：
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
- hermes-agent-cn 主导决策，符合职责边界
- 简单可靠

**缺点**：
- hermes-agent-cn 写代码时假设"scene:code=deepseek"，跑出来可能是 qwen-coder，不一致

### 方案 B：声明式需求（OpenRouter v2 风格）

**核心机制**：hermes-agent-cn 请求时声明能力需求，AIMC 选最便宜满足条件的

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

**优点**：hermes-agent-cn 完全不用关心 model，声明式

**缺点**：侵入 OpenAI 协议（虽然已经在 `extra_body.aimc_preferences` 上加过类似字段）

### 方案 C：Profile 配置式（CCR 风格）

**核心机制**：hermes-agent-cn 启动时读 profile 配置，按任务类型选不同 model

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

**缺点**：hermes-agent-cn 写死在配置里，不能跟着 AIMC 自动优化

## 五、推荐组合方案：A + C

**A 主 C 辅**——用方案 A 的"运行时拉取 + 响应头反馈"做自适应，用方案 C 的"profile 配置"做硬性规则。

### 5.1 推荐的 hermes-agent-cn 配置（方案 C）

**最小化配置（1 行）**：

```yaml
models:
  default: "tier:balanced"      # 跨模型比价的唯一变动点
```

**完整配置**（`~/.hermes/config.yaml`，CCR / Hermes Agent 风格）：

```yaml
# ========== AIMC 网关配置 ==========
aimc:
  base_url: http://localhost:8080/v1      # AIMC 网关地址
  api_key: ${AIMC_API_KEY}                # 从环境变量读, 不明文写在 YAML
  timeout: 120                            # 秒 (大模型响应慢)
  max_retries: 2                          # AIMC 内部已有 fallback, hermes-agent-cn 这层不重复

# ========== 模型 profile (任务分级) ==========
# profile 名 → AIMC group 名 (tier:xxx / scene:xxx)
# 改这里不重启 AIMC (Phase 2 已支持热刷缓存)
models:
  background: "scene:free"       # 后台任务 → 本地/免费档 (0 元)
  think: "tier:strong"           # 推理任务 → 强档
  longContext: "tier:flagship"   # 长上下文 → 旗舰
  default: "tier:balanced"       # 默认 → 均衡
  code: "scene:code"             # （可选）代码专用

# ========== hermes-agent-cn 偏好 (Phase 2.6, 通过 extra_body 传给 AIMC) ==========
aimc_preferences:
  prefer_family: "deepseek"               # 同价时优先 deepseek
  exclude_providers: []                   # 排除渠道 (e.g. ["local"])
  max_input_price: 10.0                   # 元/百万 tokens, 超过的不选

# ========== 启动校验 ==========
validate_on_startup: true                 # Agent 启动时 GET /v1/models 验证 group 合法
```

**为什么用 group（`tier:xxx`/`scene:xxx`）而不是具体 model**：
- AIMC 已在 `tier:flagship` 组里准备好 4 个候选 model，路由 cheapest
- 跨模型比价是 AIMC 的核心能力，Agent 不该重复实现
- 模型注册表变动时，Agent 配置**零改动**

**`api_key: ${AIMC_API_KEY}` 语法说明**：
Hermes Agent / Claude Code Router 都支持 `${ENV_VAR}` 占位符，从进程环境变量读值。这样 YAML 文件可以安全提交到 git (不含明文 key)。Agent 启动前需要 export `AIMC_API_KEY=xxx`。

### 5.2 推荐的运行时机制（方案 A）

hermes-agent-cn 启动时拉取 /v1/models，**不是为了写死 model 名**，而是为了：
1. **验证 group 名有效**：如果配了 `tier:light` 但 AIMC 没有这个组，启动时报错
2. **记录 fallback 提示**：dashboard 显示"当前 tier:balanced 实际跑的是哪个 model"
3. **辅助能力发现**：如果 hermes-agent-cn 需要能力雷达（"现在我有哪些 vision model 可用"），用这个缓存

```python
# hermes-agent-cn 启动伪代码
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

hermes-agent-cn 不需要"实时"知道 AIMC 模型变动。通过 3 个观测机制自然适应：

1. **启动时拉取**：hermes-agent-cn 启动时 GET /v1/models，验证 profile 合法（仅启动时，1 次）
2. **响应头反馈**：每次请求后读 `X-AIMC-Actual-Model`，hermes-agent-cn 可记录“最近 100 次 tier:balanced 跑出 deepseek-v4-flash”——这是**软信号**，**不改变路由决策**，仅供人看
3. **定期刷新**：hermes-agent-cn 启动时 + 每天 1 次（或手动触发）拉取 /v1/models，刷新 group 元信息

**关键不变量**：
- hermes-agent-cn **不写**任何 AIMC 配置
- hermes-agent-cn **不反向调整**自己 profile（除非人手动改）
- AIMC 路由决策**只**听 DB + 情报层，**不听** hermes-agent-cn

**为什么不做“实时推送”**：AIMC 单进程 + 个人用，引入 WebSocket / SSE 推送复杂度太高。hermes-agent-cn 启动 + 定期拉是**足够好**的方案。

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

**V4.9 (2026-08-25) 失败链路响应头**（仅在最终失败响应注入）：
- `X-AIMC-Failed-Channels`：JSON array, 每个对象含 `{channel, account, account_id, status, error_type, error_code, error_msg, needs_human}`
- `X-AIMC-Last-Error-Type`：最后一个失败的 ErrorCategory（`quota_exhausted` / `auth_failed` / `network_transient` / `upstream_5xx` 等）
- `X-AIMC-Last-Error-Code`：上游 error.code（如 `1113` / `insufficient_quota`）
- `Retry-After`：`3600`（仅当 `needs_human=True`，提示人工干预而非重试）

**可选增强**：加 `X-AIMC-Actual-Tier` 和 `X-AIMC-Actual-Capabilities`（JSON），让 hermes-agent-cn 不必再去查 model registry。

### 6.3 文档与示例（必须，~2 小时）

写 `docs/agent-integration.md`（本文档）+ 给 hermes-agent-cn 提 PR 集成示例（见 [hermes-agent-cn-integration-pr.md](hermes-agent-cn-integration-pr.md)）。

## 七、hermes-agent-cn 侧适配清单

### 7.1 最小适配（不推荐，但能用）

hermes-agent-cn 不改任何代码，直接：
- 把 `model="deepseek-chat"` 换成 `model="tier:balanced"`
- AIMC 自动在组内选最便宜的

**适用场景**：只想用 AIMC 省钱，不关心任务分级
**缺点**：复杂任务也走 tier:balanced，质量下降

### 7.2 推荐适配（CCR profile 风格）— **本项目推荐**

hermes-agent-cn 配置文件加 profile：
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

### 7.3 高级适配（命中率报告）—— **实际上不需要 Agent 做**

**重要发现**（2026-07-29）：命中率报告**不需要** Agent 侧记录/上报。AIMC 本身有 `usage_logs` 表（已记了实际命中的 model 字段），只要增补一个 `group_id` 列 + Dashboard 聚合视图，就能在 AIMC 侧完整展示命中率分布，**Agent 零代码改动**。

**为什么不需要 Agent 做**：
- ❌ Agent 内存累积：重启丢失、多 Agent 重复实现、历史不持久
- ❌ Agent 上报 AIMC：违反铁律（AIMC 不听 Agent 反向推送）
- ✅ **AIMC Dashboard 聚合**：数据本来就在 `usage_logs` 里，零搬运

**AIMC Dashboard 实现路径**（~半小时工作量，**推荐**）：

1. **Step 1**: `usage_logs` 表加 `group_id` 列
   ```python
   # db/models.py - UsageLog 增加一列
   group_id = Column(Text, nullable=True, comment="请求时的 group_id (NULL=精确模型路由)")
   ```

2. **Step 2**: `proxy.py` 写日志时同步记 group
   ```python
   # gateway/proxy.py - _write_usage_log 增加 group_id 参数
   async def _write_usage_log(channel_id, account_id, model, ..., group_id=None):
       log = UsageLog(channel_id=channel_id, account_id=account_id,
                      model=model, group_id=group_id, ...)
   ```

3. **Step 3**: Dashboard 聚合查询 (`GROUP BY group_id, model`)
   ```python
   # dashboard/routes.py 新增 /api/stats/group_routing
   @router.get("/api/stats/group_routing")
   async def group_routing():
       # SELECT group_id, model, COUNT(*) FROM usage_logs
       # WHERE group_id IS NOT NULL
       # GROUP BY group_id, model
       # ORDER BY group_id, count DESC
   ```

4. **Step 4**: Dashboard 页面增加“模型组路由分布”卡片（Chart.js 饼图）

**架构对比**：

| 方案 | 数据来源 | 历史持久化 | 多 Agent 共享 | Agent 代码量 | AIMC 代码量 |
|------|---------|-----------|--------------|-------------|-------------|
| ~~Agent 累积~~ | Agent 内存 | ❌ | ❌ | 20+ 行 | 0 |
| **AIMC Dashboard** | `usage_logs` 表 | ✅ SQLite 永久 | ✅ 所有 Agent 共一份 | **0** | ~50 行 |

**最终架构图**：

```
hermes-agent-cn         AIMC                       Dashboard
 │                         │                            │
 │── POST /v1/chat ────→   │                            │
 │   model="tier:balanced" │                            │
 │                         │── usage_logs 写入 ─────────→│
 │                         │   {group_id: "tier:balanced",│
 │                         │    model: "deepseek-v4-flash"}│
 │←────── response ────────│                            │
 │   (hermes-agent-cn 不做任何事)    │                  │
 │                         │                            │
 │                         │  人打开 Dashboard           │
 │                         │←──── 看 group 命中率分布 ──│
```

**价值**：
- hermes-agent-cn 零改动（不需响应头解析、不需本地累积、不需上报）
- AIMC Dashboard 侧闭环（数据本来就在 `usage_logs` 里）
- 历史持久化（SQLite 永久保存）
- 多 hermes-agent-cn 共享（所有走 AIMC 的 agent 都被统计）

**附（hermes-agent-cn 侧可选增强，仍然不需要记录实际命中）**：
如果 hermes-agent-cn 想看到**请求上下文**（哪个 prompt 走哪个 group），仍然不需记录命中，只需要在请求代码加一行：
```python
# hermes-agent-cn 发起请求时
logger.info(f"request: group={group}, prompt_chars={len(messages)}")
```
但这只是调试需要，不是命中率报告的一部分。

### 7.4 为什么 hermes-agent-cn 不主动学习、不反向调整

如果 hermes-agent-cn 反向调整 AIMC 配置（比如“我看 tier:balanced 总跑 deepseek，那以后直接调 deepseek”），会存在 4 个问题：

1. **破坏职责边界**：hermes-agent-cn 又当路由决策者了（违反“网关管渠道”原则）
2. **失去跨模型比价**：AIMC 的核心价值没了，hermes-agent-cn 写死变成单渠道代理
3. **价格变动失效**：AIMC 路由 cheapest，硟基流动降费时自动切；hermes-agent-cn 写死就丢了
4. **复杂度爆炸**：需要双向通信、冲突解决、版本同步

**铁律**：AIMC 只听 .env / DB 配置 + 情报层自动入组，**不听** hermes-agent-cn 反向推送。

## 八、Phase 3 协同任务

Phase 3 原本是"情报联动模型组"（自动入组/出组）。结合本文档，建议 Phase 3 增加以下并行小任务：

| 任务 | 优先级 | 工作量 | 依赖 |
|------|--------|--------|------|
| /v1/models 元信息增强（§6.1） | P0 | 4h | Phase 2 已完成 |
| docs/agent-integration.md（本文档） | P0 | 2h | 无 |
| **Dashboard 命中率聚合（§7.3 新发现）** | P1 | 0.5h | Phase 2 已完成 |
| hermes-agent-cn 集成 PR（§7.2 推荐适配） | P1 | 1d | hermes-agent-cn 配合（集成 PR 草案见 [hermes-agent-cn-integration-pr.md](hermes-agent-cn-integration-pr.md)） |
| 响应头增强（§6.2） | P2 | 1h | Phase 2 已完成 |
| /v1/models SSE 推送模型变动 | P3 | 4h | 不推荐，先不做 |

**Dashboard 命中率聚合任务明细**：
1. `db/models.py` UsageLog 加 `group_id` 列
2. `scripts/migrate_add_group_id.py` 一次性迁移脚本（老数据 group_id 全 NULL）
3. `gateway/proxy.py` `_write_usage_log` 增加 `group_id` 参数，`_schedule_usage_log` 从 `requested_model` 提取
4. `dashboard/routes.py` 新增 `GET /api/stats/group_routing`
5. `dashboard/static/index.html` 增加“模型组路由分布”卡片（Chart.js 饼图）

## 九、对比总结

| 方案 | 协议侵入 | Agent 复杂度 | AIMC 复杂度 | 适用场景 |
|------|---------|------------|------------|---------|
| **A 启动拉 + 响应头反馈** | 0 | 中 | 低 | 想做软学习的 Agent |
| **B 声明式需求** | 中（加 aimc_requirements） | 低 | 中 | 想完全声明式的 Agent |
| **C Profile 配置** | 0 | 低 | 0（已有 group） | 大多数 Agent 推荐 |
| **A+C 推荐组合** | 0 | 低-中 | 低 | 个人编程 Agent |

## 十、决策记录

- **2026-07-29 (v0.2)**: 架构术语修订, 明确三层架构 (hermes-tray → hermes-agent-cn → AIMC), 'Agent' 统一为 hermes-agent-cn
- **下一步**：等 Phase 3 启动后，把本文档落地为代码（§6.1 + §7.2）
- **后续迭代**：根据 hermes-agent-cn 集成反馈决定是否引入 §7.3 高级适配
