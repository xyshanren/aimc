# AIMC 竞品调研与多维模型组设计

> 调研时间：2026-07  
> 目的：为 AIMC 模型组路由方案提供竞品参考与设计依据

---

## 一、竞品调研

### 1.1 OpenRouter

**定位**：商业化模型聚合路由平台（SaaS），一个 API Key 访问 500+ 模型、60+ Provider。

**核心架构**：
```
客户端 → OpenRouter Edge (Cloudflare Workers)
              ├── 鉴权 & 余额检查
              ├── 模型解析（精确模型名 or openrouter/auto）
              ├── Provider 选择（价格/延迟/吞吐量/健康度）
              ├── Fallback（Provider 不可用时自动切换）
              └── 统一格式响应
```

**路由层次**（36kr 2026-07 分析）：
| 层次 | 说明 | 决策信号 |
|------|------|----------|
| Model Selection | 根据任务选模型 | 质量/成本最优组合 |
| Provider Routing | 同模型多 Provider 选优 | 价格/速度/uptime |
| Fallback & Retry | 失败后重试/降级 | 错误码/超时 |
| Governance | 权限/审计/合规 | 组织策略 |

**关键特性**：
- `openrouter/auto`：由 Not Diamond 元模型驱动，分析 Prompt 复杂度自动选模型
- Auto Exacto（2026-03）：结合 throughput、tool-call 遥测、benchmark score，每 5 分钟重估 Provider
- 模型命名：`provider/model-name`（如 `anthropic/claude-sonnet-4`）
- Prompt Caching：缓存重复 Prompt，最高省 90% Token
- 排行榜：基于真实使用数据生成模型排名

**对 AIMC 的借鉴**：
- ✅ Provider Routing（同模型多渠道选优）— AIMC 已实现
- ✅ 模型命名规范 `provider/model` — 可借鉴用于区分渠道
- ⚠️ Auto Router（跨模型自动选）— AIMC 的"模型组"可参考，但不做 Prompt 分析
- ❌ 商业化统一计费 — 不适用（AIMC 是自用工具）

---

### 1.2 9Router

**定位**：面向 AI Coding 工具的**本地**路由网关（开源 MIT），专注"永不断线编码"。

**核心架构**：
```
编程工具 (Claude Code/Cursor/Cline)
    → http://localhost:20128/v1
        → 9Router 智能路由
            ├── RTK Token 压缩（裁剪 git diff/grep 输出，省 20-40%）
            ├── 格式翻译（OpenAI ↔ Claude ↔ Gemini ↔ Cursor）
            ├── 配额追踪 & Token 自动刷新
            └── 三层降级路由
                ├── [第一层: 订阅] Claude Code / Codex / Copilot
                ├── [第二层: 廉价] GLM ($0.6/1M) / MiniMax ($0.2/1M)
                └── [第三层: 免费] Kiro / OpenCode Free / Vertex AI
```

**关键特性**：
- **三层瀑布流（Combo）**：按优先级配置模型列表，配额耗尽自动降级
- **多账号轮询**：同一 Provider 多账号负载均衡
- **RTK Token Saver**：Rust 编写，无损压缩工具输出
- **Caveman Mode**：注入精简提示词，省 65% 输出 Token
- **实时配额仪表盘**：可视化各 Provider 消耗/重置倒计时

**对 AIMC 的借鉴**：
- ✅ **三层降级思路**（订阅→廉价→免费）— AIMC 模型组可引入"层级"概念
- ✅ **多账号轮询** — AIMC 已实现
- ✅ **配额追踪可视化** — Dashboard 可参考
- ⚠️ **Combo 配置模式** — 用户按优先级排列模型，AIMC 可简化为模型组内权重
- ❌ Token 压缩/格式翻译 — 不是 AIMC 的关注点（AIMC 做透传）

---

### 1.3 LiteLLM

**定位**：开源 LLM 网关/代理（Python），统一 100+ Provider API 为 OpenAI 格式。

**核心概念 — Model Group**：
```yaml
model_list:
  - model_name: "gpt-4"          # ← 这就是"模型组"名
    litellm_params:
      model: "azure/chatgpt-v-2"  # 实际部署 1
  - model_name: "gpt-4"          # 同名 = 同组
    litellm_params:
      model: "gpt-4"             # 实际部署 2
  - model_name: "gpt-4"
    litellm_params:
      model: "openrouter/gpt-4"  # 实际部署 3

router_settings:
  routing_strategy: "latency-based-routing"  # 或 cost-based / simple-shuffle
  fallbacks: [{"gpt-4": ["gpt-3.5-turbo"]}]  # 组间降级
```

**路由策略**：
| 策略 | 说明 |
|------|------|
| simple-shuffle | 随机负载均衡 |
| latency-based-routing | 选延迟最低的部署 |
| cost-based-routing | 选最便宜的部署 |
| usage-based-routing | 按 TPM/RPM 使用率分配 |

**对 AIMC 的借鉴**：
- ✅ **Model Group 概念**：同名多部署 = 一个逻辑组 — AIMC 直接借鉴
- ✅ **组间 Fallback**：`gpt-4` 组失败 → 降级到 `gpt-3.5-turbo` 组
- ✅ **routing_strategy 可配置** — AIMC 路由引擎可扩展
- ⚠️ LiteLLM 的 Model Group 是"同模型多部署"，不含"跨模型替代"

---

### 1.4 RouteLLM（学术方案）

**定位**：UC Berkeley LMSYS 团队，ICLR 2025 论文，基于分类器的成本-质量路由。

**核心思路**：
```
query → 轻量分类器(BERT) → P(强模型赢) → 与阈值 α 比较
    P > α → 用强模型（GPT-4）
    P ≤ α → 用弱模型（Mixtral-8x7B）
```

**效果**：85% 成本降低，保持 GPT-4 95% 质量。路由器推理 < 10ms。

**对 AIMC 的借鉴**：
- ⚠️ 思路好但 AIMC 不做 Prompt 分析（定位是透传网关）
- ✅ "能力档位"概念可借鉴 — 模型按能力分 tier，Agent 侧指定 tier 而非具体模型
- ❌ 需要训练数据和分类器 — 超出 AIMC 自用工具的复杂度

---

### 1.5 其他方案速览

| 方案 | 核心做法 | AIMC 可借鉴 |
|------|----------|-------------|
| **Not Diamond** | 元模型预测最优模型（OpenRouter Auto 底层） | 理念参考，不实现 |
| **Martian** | 不运行模型即预测输出质量 | 理念参考 |
| **Router-R1** (NeurIPS 2025) | LLM 自身做 CoT 路由决策 | 太重，不适合 |
| **Amazon Bedrock** | 同族两模型 Prompt Routing | 限制太大 |
| **微软 Model Router** | 成本/质量/平衡三模式 | 策略分类可参考 |
| **vLLM Semantic Router** | 判断是否需要推理（reasoning） | 未来可考虑 |

---

### 1.6 国内模型分发平台

#### 硅基流动（SiliconFlow）

- **定位**：开源模型一站式云服务平台，100+ 模型，行业最低价
- **特点**：
  - 完全 OpenAI 兼容，改 base_url 即可
  - 15 个免费模型（DeepSeek-R1 蒸馏版、Qwen3-8B、GLM-4-9B 等）
  - 付费模型定价极低（Qwen2.5-72B: ¥41.3/M tokens）
  - 注册送 14 元余额
  - 同一模型可能有不同量化版本
- **对 AIMC 的意义**：重要的第三方渠道来源，同模型价格可能比官方便宜

#### 火山引擎（方舟）

- **定位**：字节跳动 AI 服务平台
- **特点**：
  - 自研豆包/Seed 系列 + 第三方模型（DeepSeek、Kimi、GLM）
  - "打包订阅 + 模型抵扣系数"（调豆包扣 1 次，调 GLM-5.1 扣 5 次）
  - Auto 模式自动选模型
  - 首月特惠 ¥8.9
- **对 AIMC 的意义**：订阅制计价模式需要特殊处理（非按量计费）

#### 腾讯云（TokenHub）

- **定位**：混元自研 + 第三方模型聚合
- **特点**：
  - 集成 GLM-5.1、Kimi-K2.5、MiniMax-M2.7
  - Token Plan 套餐制
  - 兼容多种 AI 编程工具
- **对 AIMC 的意义**：第三方渠道，套餐内调用可能零边际成本

#### 阿里云百炼（PAI Token 服务）

- **定位**：通义千问系列 + 第三方模型
- **特点**：
  - 提供 DeepSeek-V4-Pro (¥14.4/M)、DeepSeek-V4-Flash (¥1.2/M)
  - Kimi-K2.7-Code (¥7.8/M 输入)
  - GLM-5.2 (¥9.6/M 输入)
  - 坐席套餐制 ¥198/人/月
- **对 AIMC 的意义**：第三方定价参考，同模型不同渠道价差明显

#### 关键发现

> **同一模型在不同渠道的价格差异巨大**，这正是 AIMC 的核心价值所在：

| 模型 | 官方价 | 硅基流动 | 阿里云 | 火山引擎 |
|------|--------|----------|--------|----------|
| DeepSeek-V3 | ¥2/M(输入) | ¥1/M | ¥2/M | 套餐内含 |
| GLM-4-9B | 免费(官方) | 免费 | - | 套餐内含 |
| Qwen2.5-72B | ¥4/M | ¥41.3/M(微调) | ¥4/M | 套餐内含 |
| Kimi-K2.5 | ¥4/M(输入) | - | - | 套餐抵扣 |

---

## 二、竞品对比总结

| 维度 | OpenRouter | 9Router | LiteLLM | AIMC（目标） |
|------|-----------|---------|---------|-------------|
| 定位 | 商业 SaaS | 本地编码网关 | 通用 LLM 代理 | 自用省钱网关 |
| 路由粒度 | 同模型多 Provider | 三层降级 | Model Group | **模型族 × 能力档 × 渠道** |
| 跨模型替代 | Auto Router | Combo 列表 | Fallback 组 | **模型组内选优** |
| 情报能力 | 无（静态价格） | 无 | 无 | **✅ 核心差异化** |
| 价格更新 | 手动/API | 手动配置 | 手动配置 | **自动爬取+AI解析** |
| 部署形态 | 云端 | 本地 Node.js | 本地/Docker | 本地 Python |
| 成本 | 加价转售 | 免费 | 免费 | 免费 |

**AIMC 的差异化定位**：
- OpenRouter/9Router/LiteLLM 都**不做情报**（价格靠人工维护）
- AIMC 的核心价值 = **情报驱动的价格感知路由**
- 不追求"智能判断任务复杂度"（那是 Agent 的事），专注"同能力档内选最便宜"

---

## 三、多维模型组设计

### 3.1 设计目标

解决的核心问题：
```
Agent 请求: model="deepseek-chat"
AIMC 内部:
  - DeepSeek 官方有 V4-Pro、V4-Flash、V3
  - 硅基流动也提供 DeepSeek-V3（更便宜）
  - 阿里云也提供 DeepSeek-V4-Flash
  - 同级别的 Qwen-Plus、GLM-4-Air 可能更便宜
问题: 如何组织这些模型，让路由既精确又灵活？
```

### 3.2 核心概念：三维模型组织

```
┌─────────────────────────────────────────────────────────┐
│                    模型组织三维空间                        │
│                                                         │
│  维度一: 模型族 (Model Family)                           │
│  ─────────────────────────                              │
│  deepseek / qwen / glm / kimi / llama / local           │
│                                                         │
│  维度二: 能力档 (Capability Tier)                        │
│  ─────────────────────────────                          │
│  T0(旗舰) / T1(强力) / T2(均衡) / T3(轻量) / T4(微型)  │
│                                                         │
│  维度三: 供给渠道 (Provider/Channel)                     │
│  ─────────────────────────────────                      │
│  official / siliconflow / aliyun / volcengine / tencent  │
│  / huawei / local                                       │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 3.3 数据模型设计

#### 表1: model_registry（模型注册表 — 新增）

记录所有已知模型的**静态元信息**，与渠道无关。

```sql
CREATE TABLE model_registry (
    id              INTEGER PRIMARY KEY,
    model_id        TEXT NOT NULL UNIQUE,     -- 规范ID: "deepseek-v4-pro"
    family          TEXT NOT NULL,            -- 模型族: "deepseek"
    display_name    TEXT,                     -- 展示名: "DeepSeek V4 Pro"
    
    -- 能力标签（多维）
    tier            TEXT NOT NULL DEFAULT 'T2', -- 能力档: T0/T1/T2/T3/T4
    capabilities    TEXT,                     -- JSON: ["reasoning","code","math","general"]
    context_length  INTEGER,                 -- 最大上下文: 128000
    modality        TEXT DEFAULT 'text',     -- text/vision/multimodal
    
    -- 能力基准分（情报层更新）
    benchmark_scores TEXT,                   -- JSON: {"mmlu": 88.5, "humaneval": 92.0, ...}
    
    -- 元信息
    is_open_source  BOOLEAN DEFAULT TRUE,    -- 是否开源权重
    release_date    TEXT,                    -- 发布日期
    status          TEXT DEFAULT 'active',   -- active/deprecated/preview
    notes           TEXT,                    -- 备注
    
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

#### 表2: model_groups（模型组 — 新增）

定义**可互换模型组**，Agent 可以请求组名。

```sql
CREATE TABLE model_groups (
    id              INTEGER PRIMARY KEY,
    group_id        TEXT NOT NULL UNIQUE,     -- 组ID: "tier:code-strong"
    display_name    TEXT,                     -- "强力代码模型组"
    
    -- 组定义维度
    tier            TEXT,                     -- 限定能力档（可选）
    capabilities    TEXT,                     -- JSON: 要求的能力标签（交集匹配）
    
    -- 路由策略
    strategy        TEXT DEFAULT 'cheapest',  -- cheapest/latency/priority/weighted
    fallback_group  TEXT,                     -- 降级组: "tier:code-medium"
    
    -- 约束
    min_context     INTEGER DEFAULT 0,       -- 最低上下文要求
    require_vision  BOOLEAN DEFAULT FALSE,   -- 是否要求视觉能力
    
    enabled         BOOLEAN DEFAULT TRUE,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

#### 表3: model_group_members（组成员 — 新增）

```sql
CREATE TABLE model_group_members (
    id              INTEGER PRIMARY KEY,
    group_id        TEXT NOT NULL REFERENCES model_groups(group_id),
    model_id        TEXT NOT NULL REFERENCES model_registry(model_id),
    
    -- 组内优先级（同价格时的排序依据）
    priority        INTEGER DEFAULT 100,     -- 越小越优先
    weight          REAL DEFAULT 1.0,        -- 加权路由时的权重
    
    -- 约束
    max_cost_ratio  REAL,                    -- 价格超过组内最低价N倍时排除
    
    UNIQUE(group_id, model_id)
);
```

#### 表4: channels 表扩展

现有 `channels` 表增加字段：

```sql
ALTER TABLE channels ADD COLUMN provider_type TEXT DEFAULT 'official';
-- provider_type: official / third_party / local / aggregator

ALTER TABLE channels ADD COLUMN billing_mode TEXT DEFAULT 'pay_per_token';
-- billing_mode: pay_per_token / subscription / free
```

#### 表5: model_capabilities_log（能力标签变更日志 — 新增）

```sql
CREATE TABLE model_capabilities_log (
    id              INTEGER PRIMARY KEY,
    model_id        TEXT NOT NULL,
    field_changed   TEXT NOT NULL,           -- "tier" / "benchmark_scores" / "capabilities"
    old_value       TEXT,
    new_value       TEXT,
    source          TEXT,                    -- "intel_crawler" / "manual" / "benchmark_sync"
    confidence      REAL,                    -- AI解析置信度
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 3.4 模型组示例配置

```yaml
# === 模型注册示例 ===
models:
  # DeepSeek 族
  - model_id: deepseek-v4-pro
    family: deepseek
    tier: T0
    capabilities: [reasoning, code, math, agent]
    context_length: 160000
    is_open_source: true
    benchmark_scores: {mmlu: 92.1, humaneval: 95.3, swe_bench: 72.0}
    
  - model_id: deepseek-v4-flash
    family: deepseek
    tier: T2
    capabilities: [general, code, reasoning]
    context_length: 160000
    is_open_source: true
    benchmark_scores: {mmlu: 86.5, humaneval: 88.1}
    
  - model_id: deepseek-v3
    family: deepseek
    tier: T2
    capabilities: [general, code]
    context_length: 128000
    is_open_source: true
    
  # Qwen 族
  - model_id: qwen3-235b
    family: qwen
    tier: T0
    capabilities: [reasoning, code, math, general]
    context_length: 128000
    
  - model_id: qwen-plus
    family: qwen
    tier: T2
    capabilities: [general, code, creative]
    context_length: 128000
    
  # GLM 族
  - model_id: glm-5.1
    family: glm
    tier: T0
    capabilities: [code, agent, reasoning]
    context_length: 198000
    
  - model_id: glm-4-air
    family: glm
    tier: T2
    capabilities: [general, code]
    context_length: 128000

# === 模型组定义 ===
groups:
  # 按能力档分组（Agent 主要使用方式）
  - group_id: "tier:flagship"
    display_name: "旗舰推理组"
    tier: T0
    strategy: cheapest
    fallback_group: "tier:strong"
    members: [deepseek-v4-pro, qwen3-235b, glm-5.1, kimi-k2.5]
    
  - group_id: "tier:strong"
    display_name: "强力通用组"
    tier: T1
    strategy: cheapest
    fallback_group: "tier:balanced"
    members: [deepseek-v3, qwen-max, glm-4-plus]
    
  - group_id: "tier:balanced"
    display_name: "均衡性价比组"
    tier: T2
    strategy: cheapest
    fallback_group: "tier:light"
    members: [deepseek-v4-flash, qwen-plus, glm-4-air, kimi-8k]
    
  - group_id: "tier:light"
    display_name: "轻量免费组"
    tier: T3
    strategy: cheapest
    members: [qwen-2.5-7b, glm-4-9b, deepseek-r1-distill-7b]

  # 按场景分组（可选）
  - group_id: "scene:code"
    display_name: "代码专用组"
    capabilities: [code]
    min_context: 64000
    strategy: cheapest
    members: [deepseek-v4-pro, glm-5.1, qwen3-235b, deepseek-v4-flash]
    
  - group_id: "scene:free"
    display_name: "零成本组"
    strategy: priority
    members: [qwen-2.5-7b-sf-free, glm-4-9b-sf-free, deepseek-r1-7b-sf-free]
```

### 3.5 路由决策流程（升级版）

```
Agent 请求
    │
    ├── model="deepseek-chat"（精确模型名）
    │       → 走现有逻辑：在提供该模型的渠道中选最优
    │
    ├── model="tier:balanced"（模型组名）
    │       → 新逻辑：
    │       1. 解析组定义 → 获取成员模型列表
    │       2. 对每个成员模型，查找其所有活跃渠道
    │       3. 获取各渠道当前价格（price_snapshots 最新）
    │       4. 过滤：频限/配额/时段
    │       5. 排序：按策略（cheapest → 价格升序）
    │       6. 选出最优 (model, channel, account) 三元组
    │       7. 转发请求，将 model 字段替换为实际模型名
    │
    └── model="scene:code"（场景组名）
            → 同上，但过滤条件增加 capabilities 匹配
```

### 3.6 情报层联动：自动打标与更新

AIMC 的核心差异化 — **情报驱动的动态标签**：

```
┌────────────────────────────────────────────────────┐
│              情报层 → 模型注册表 联动               │
│                                                    │
│  定时爬虫                                          │
│    ├── 厂商定价页 → 更新 price_snapshots           │
│    ├── 厂商模型列表页 → 发现新模型 → model_registry │
│    ├── Benchmark 榜单页 → 更新 benchmark_scores    │
│    └── 活动页 → 更新 promotions                    │
│                                                    │
│  AI 解析引擎                                       │
│    ├── 新模型发现 → 自动推断 family/tier/capabilities│
│    ├── 价格变动 → 触发模型组内重排序               │
│    └── 置信度 < 0.8 → Telegram 告警人工确认        │
│                                                    │
│  自愈循环                                          │
│    ├── 模型下线 → 自动从组成员移除                 │
│    ├── 新模型上线 → 自动加入对应组                 │
│    └── 价格异常 → 标记渠道为 suspect               │
│                                                    │
└────────────────────────────────────────────────────┘
```

**标签更新来源优先级**：
1. 用户手动配置（最高优先）
2. 厂商官方公告（情报层爬取）
3. 公开 Benchmark 榜单（定期同步）
4. AI 推断（最低优先，需人工确认）

### 3.7 Agent 侧接入方式

Agent 有三种请求方式，**向后兼容**：

```python
# 方式1: 精确模型名（现有行为不变）
response = client.chat.completions.create(
    model="deepseek-chat",  # 精确指定，AIMC 只选渠道
    messages=[...]
)

# 方式2: 模型组（新增，推荐）
response = client.chat.completions.create(
    model="tier:balanced",  # AIMC 在组内选最便宜的模型+渠道
    messages=[...]
)

# 方式3: 带约束的组（高级）
response = client.chat.completions.create(
    model="tier:balanced",
    extra_body={
        "aimc_preferences": {
            "prefer_family": "deepseek",   # 同价时优先 DeepSeek
            "min_context": 64000,          # 最低上下文
            "exclude_providers": ["local"] # 排除本地
        }
    },
    messages=[...]
)
```

### 3.8 /v1/models 响应升级

```json
{
  "object": "list",
  "data": [
    {"id": "deepseek-chat", "object": "model", "owned_by": "aimc"},
    {"id": "deepseek-v4-flash", "object": "model", "owned_by": "aimc"},
    {"id": "qwen-plus", "object": "model", "owned_by": "aimc"},
    {"id": "glm-4-air", "object": "model", "owned_by": "aimc"},
    
    // 模型组（以 "tier:" 或 "scene:" 前缀标识）
    {"id": "tier:flagship", "object": "model", "owned_by": "aimc-group"},
    {"id": "tier:balanced", "object": "model", "owned_by": "aimc-group"},
    {"id": "tier:light", "object": "model", "owned_by": "aimc-group"},
    {"id": "scene:code", "object": "model", "owned_by": "aimc-group"},
    {"id": "scene:free", "object": "model", "owned_by": "aimc-group"}
  ]
}
```

---

## 四、实施路线

### Phase 1: 模型注册 + 静态分组（MVP）

- [ ] 新增 `model_registry` 表
- [ ] 新增 `model_groups` + `model_group_members` 表
- [ ] 路由引擎支持 `tier:xxx` 前缀解析
- [ ] 手动录入模型元信息和组配置
- [ ] `/v1/models` 返回模型组

### Phase 2: 情报联动

- [ ] 爬虫增加"模型列表页"采集源
- [ ] AI 解析器支持提取模型能力信息
- [ ] Benchmark 榜单定期同步
- [ ] 新模型自动入组（置信度 > 0.8）

### Phase 3: 智能优化

- [ ] 基于历史用量数据的组内权重自动调整
- [ ] 价格预测（峰谷趋势）辅助路由
- [ ] Dashboard 模型组可视化配置
- [ ] Agent 偏好学习（记录哪些组效果好）

---

## 五、设计决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| 路由是否分析 Prompt | **否** | AIMC 定位是透传网关，任务判断交给 Agent |
| 模型组 vs 精确模型 | **两者共存** | 向后兼容 + 灵活性 |
| 能力标签来源 | **人工+情报** | 不引入 ML 分类器，保持简单 |
| 组内选优默认策略 | **cheapest** | 符合"省钱"核心定位 |
| 是否支持订阅制渠道 | **是** | 火山引擎/腾讯云等套餐内调用零边际成本 |
| 模型组降级 | **组间 fallback** | 借鉴 9Router 三层 + LiteLLM fallback |
| 本地模型处理 | **作为渠道** | local 是一种 provider_type，频限由 Agent 管 |
