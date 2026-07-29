# AIMC 架构设计文档

> 版本：v2.1（2026-07-29）
> 修订：v2.0 基础上合并 Phase 2.5 运维加固（已完成的 4 个改进），清理 v1.0 残留内容

## 一、系统定位

面向个人开发者的轻量级 LLM 网关与优惠情报聚合工具。

**核心价值**：情报发现（哪里最便宜）+ 智能路由（怎么调用最划算）。

**差异化**：市面上 OpenRouter/9Router/LiteLLM 等方案均不做情报采集，价格靠人工维护。AIMC 的核心壁垒是**情报驱动的动态价格感知路由**。

## 二、设计原则

| 原则 | 说明 |
|------|------|
| 自用优先 | 允许半自动，不追求 100% 无人值守 |
| 情报层 > 网关层 | 情报发现是核心差异化价值 |
| 先跑通再优化 | Phase 1 快速验证核心价值 |
| 保持简单 | 单进程部署，零外部依赖（无 Redis/MQ） |
| 精细化运营 | 峰谷定价、频限追踪、多账号兜底 |
| 职责分离 | AIMC 管"同能力档内选最便宜"，Agent 管"该用什么能力档" |
| 运维先于新功能 | 跑得稳 > 跑得花，让日常运维（重启/改 key/查日志）不痛 |

## 三、系统架构

```
+---------------------------------------------------------------+
|                    用户 / Agent / 客户端                         |
|         (OpenAI SDK, model="deepseek-chat" 或 "tier:balanced") |
+-----------------------------+---------------------------------+
                              |
                              v
+---------------------------------------------------------------+
|                 自研网关层 (Python FastAPI)                     |
|  1. 统一 OpenAI 兼容接口（/v1/chat/completions）               |
|  2. 模型组解析器（tier:/scene: 前缀 → 组内选优）              |
|  3. 路由决策引擎（时段/配额/价格/频限 多维权重）               |
|  4. 内存滑动窗口频限（QPM/TPM/并发/日限额）                   |
|  5. 流式/非流式原样透传（aiter_raw，不解析 SSE）               |
|  6. 多账号轮询与自动切换                                       |
+------------------------------+---------------------------------+
                              |
                              v
+---------------------------------------------------------------+
|                  SQLite 数据库 (WAL 模式)                       |
|  渠道/账号 │ 价格快照 │ 优惠活动 │ 用量日志 │ 频限配额 │ 规则  |
|  模型注册表 │ 模型组 │ 组成员 │ 能力变更日志                    |
+------------------------------^---------------------------------+
                              |
+---------------------------------------------------------------+
|                 情报采集层 (同进程异步任务)                      |
|  APScheduler → Playwright抓取 → Diff检测 → AI解析 → 入库/告警  |
|                                                                |
|  反馈模型组：                                                  |
|    新模型发现 → 自动注册 + 推断标签 → 入组                     |
|    价格变动 → 触发组内重排序                                   |
|    模型下线 → 自动移出组                                       |
+---------------------------------------------------------------+
                              |
                              v
+---------------------------------------------------------------+
|              极简 Dashboard (FastAPI 静态文件 + htmx)            |
+---------------------------------------------------------------+
```

## 四、多维模型组设计

### 4.1 问题背景

国内模型生态的现状：
- **同族多版本**：DeepSeek 有 V4-Pro / V4-Flash / V3；Kimi 有 K2.5 / K2.7-Code / 8K
- **同模型多渠道**：DeepSeek-V3 在官方、硅基流动、阿里云、火山引擎都有提供，价格各异
- **同档多替代**：DeepSeek-V4-Flash、Qwen-Plus、GLM-4-Air 能力相近但价格不同
- **第三方聚合**：硅基流动提供市面大部分开源模型；火山引擎/腾讯云/阿里云也代理第三方模型

如果 Agent 只能写死 `model="deepseek-chat"`，则永远用不到更便宜的同级替代品。

### 4.2 三维模型组织

```
维度一: 模型族 (Model Family)
─────────────────────────────
deepseek / qwen / glm / kimi / minimax / llama / local
作用: 标识模型血统，用于偏好排序和情报采集分组

维度二: 能力档 (Capability Tier)
─────────────────────────────────
T0 (旗舰)  — 最强推理/编码，如 DeepSeek-V4-Pro、GLM-5.1、Qwen3-235B
T1 (强力)  — 接近旗舰但稍弱或更便宜，如 DeepSeek-V3、Qwen-Max
T2 (均衡)  — 性价比主力，如 DeepSeek-V4-Flash、Qwen-Plus、GLM-4-Air
T3 (轻量)  — 小参数/蒸馏版，如 Qwen2.5-7B、GLM-4-9B
T4 (微型)  — 极小模型/嵌入模型，如 Qwen2.5-1.5B
作用: Agent 按任务复杂度指定档位，AIMC 在档内选最便宜

维度三: 供给渠道 (Provider/Channel)
────────────────────────────────────
official     — 模型厂商官方 API（如 api.deepseek.com）
third_party  — 第三方托管（如硅基流动、阿里云百炼、火山引擎）
aggregator   — 聚合平台（如 OpenRouter）
local        — 本地部署（如 Ollama、vLLM 自建）
作用: 同模型可能有多个渠道，价格/稳定性各异
```

### 4.3 模型组定义

**模型组 = 一组能力可互换的模型，Agent 请求组名，AIMC 在组内选最便宜的 (模型, 渠道, 账号) 三元组。**

#### 组命名规范

| 前缀 | 含义 | 示例 |
|------|------|------|
| `tier:` | 按能力档分组 | `tier:flagship`、`tier:balanced`、`tier:light` |
| `scene:` | 按使用场景分组 | `scene:code`、`scene:free`、`scene:long-context` |
| 无前缀 | 精确模型名（现有行为） | `deepseek-chat`、`qwen-plus` |

#### 组内路由策略

| 策略 | 说明 | 适用场景 |
|------|------|----------|
| `cheapest` | 按输入价格升序（默认） | 日常省钱 |
| `priority` | 按人工设定的优先级 | 有明确偏好时 |
| `weighted` | 按权重随机分配 | 需要分散用量避免单渠道频限 |
| `latency` | 按历史平均延迟排序 | 对响应速度敏感 |

#### 组间降级（Fallback Chain）

```
tier:flagship → tier:strong → tier:balanced → tier:light → tier:free
     ↓ 组内全部不可用时
自动降级到下一组，直到找到可用渠道
```

### 4.4 路由决策流程（完整版）

```
收到请求 model=X
    │
    ├─ X 含 ":" 前缀？（如 "tier:balanced"）
    │   │
    │   ├─ YES → 模型组路由
    │   │   1. 查 model_groups 表获取组定义
    │   │   2. 查 model_group_members 获取成员模型列表
    │   │   3. 对每个成员模型，查其所有活跃渠道+账号
    │   │   4. 获取各渠道当前时段价格（price_snapshots）
    │   │   5. 过滤：
    │   │      - 频限过滤（排除 QPM/TPM/并发 达阈值）
    │   │      - 配额过滤（排除耗尽的账号）
    │   │      - 时段过滤（排除非当前时段的价格）
    │   │      - 约束过滤（min_context、require_vision 等）
    │   │   6. 排序：按组策略（cheapest → input_price ASC）
    │   │   7. 选出最优 (model, channel, account) 三元组
    │   │   8. 将请求中 model 字段替换为实际模型名
    │   │   9. 转发
    │   │
    │   └─ 组内全部不可用？
    │       → 查 fallback_group → 递归执行下一组
    │       → 所有组均不可用 → 返回 503
    │
    └─ NO → 精确模型路由（现有逻辑不变）
        1. 筛选支持该 model 的渠道
        2. 时段匹配 → 频限过滤 → 配额过滤
        3. 多维排序：价格 ASC > 配额 DESC > 优先级 DESC
        4. 选择最优渠道执行
        5. 失败 → fallback 到下一候选（最多重试2次）
        6. 全部不可用 → fallback 到 priority 最低 channel（Phase 2E, 聚合平台兜底）
```

### 4.5 Agent 侧接入协议

```python
# 方式1: 精确模型名（向后兼容，行为不变）
client.chat.completions.create(model="deepseek-chat", ...)

# 方式2: 能力档（推荐，AIMC 在档内跨模型选最便宜）
client.chat.completions.create(model="tier:balanced", ...)

# 方式3: 场景组（按能力标签过滤）
client.chat.completions.create(model="scene:code", ...)

# 方式4: 带偏好约束（高级，通过 extra_body 传递）
client.chat.completions.create(
    model="tier:balanced",
    extra_body={
        "aimc_preferences": {
            "prefer_family": "deepseek",      # 同价时优先 DeepSeek
            "exclude_providers": ["local"],   # 排除本地渠道
            "min_context": 64000,             # 最低上下文长度
            "max_input_price": 5.0            # 价格上限（元/百万tokens）
        }
    },
    ...,
)
```

**响应头**（让调用方知道实际用了什么）：
```
X-AIMC-Actual-Model: deepseek-v4-flash
X-AIMC-Actual-Channel: siliconflow
X-AIMC-Route-Reason: cheapest_in_group
```

### 4.6 /v1/models 响应格式

```json
{
  "object": "list",
  "data": [
    {"id": "deepseek-chat", "object": "model", "owned_by": "aimc"},
    {"id": "deepseek-v4-flash", "object": "model", "owned_by": "aimc"},
    {"id": "qwen-plus", "object": "model", "owned_by": "aimc"},
    {"id": "glm-4-air", "object": "model", "owned_by": "aimc"},

    {"id": "tier:flagship", "object": "model", "owned_by": "aimc-group"},
    {"id": "tier:strong", "object": "model", "owned_by": "aimc-group"},
    {"id": "tier:balanced", "object": "model", "owned_by": "aimc-group"},
    {"id": "tier:light", "object": "model", "owned_by": "aimc-group"},
    {"id": "scene:code", "object": "model", "owned_by": "aimc-group"},
    {"id": "scene:free", "object": "model", "owned_by": "aimc-group"}
  ]
}
```

## 五、情报层 → 模型组反馈机制

### 5.1 反馈触发点

| 情报事件 | 触发动作 | 置信度要求 |
|----------|----------|-----------|
| 发现新模型 | 写入 model_registry + 推断标签 + 加入对应组 | > 0.8 自动；≤ 0.8 告警 |
| 模型价格变动 | 更新 price_snapshots → 组内排序自动变化 | 直接入库（价格无需推断） |
| 模型下线/废弃 | model_registry.status → deprecated → 移出组 | > 0.9 自动；≤ 0.9 告警 |
| 新渠道上线 | 写入 channels + 关联已有模型 | > 0.8 自动 |
| Benchmark 更新 | 更新 benchmark_scores → 可能触发 tier 调整 | 仅记录，tier 调整需人工确认 |
| 优惠活动发现 | 写入 promotions → 可能临时调整渠道优先级 | > 0.8 自动 |

### 5.2 新模型自动入组流程

```
情报爬虫发现新模型（如 "DeepSeek-V4-Pro"）
    │
    ▼
AI 解析引擎提取结构化信息:
    {
        "model_id": "deepseek-v4-pro",
        "family": "deepseek",           ← 从命名推断
        "display_name": "DeepSeek V4 Pro",
        "context_length": 160000,        ← 从文档提取
        "capabilities": ["reasoning", "code", "math", "agent"],
        "suggested_tier": "T0",          ← AI 基于参数规模/定位推断
        "confidence": 0.92
    }
    │
    ▼
置信度判断:
    ├─ confidence > 0.8 → 自动执行:
    │   1. INSERT model_registry（status=active）
    │   2. 根据 tier 和 capabilities 匹配现有组:
    │      - tier=T0 → 加入 "tier:flagship"
    │      - capabilities 含 "code" → 加入 "scene:code"
    │   3. INSERT model_group_members
    │   4. INSERT model_capabilities_log（记录变更）
    │   5. 触发路由缓存刷新
    │
    └─ confidence ≤ 0.8 → 告警:
        1. 飞书/Bark/Server酱 推送: "发现新模型 DeepSeek-V4-Pro，推断为T0，置信度0.72，请确认"
        2. 暂不入组，等待人工在 Dashboard 确认
```

### 5.3 价格变动 → 组内重排序

```
情报爬虫检测到硅基流动 DeepSeek-V3 降价: ¥2/M → ¥1/M
    │
    ▼
更新 price_snapshots:
    INSERT 新快照（input_price=1.0, source="crawler"）
    │
    ▼
路由缓存定时刷新（每 300s）:
    RouteEngine.refresh_cache() 重新加载所有价格
    │
    ▼
下次 "tier:balanced" 请求:
    组内排序自动变化:
      硅基流动/DeepSeek-V3  ¥1/M  ← 升为第一
      官方/DeepSeek-V4-Flash ¥2/M
      官方/Qwen-Plus        ¥4/M
```

**注意**：价格变动不需要额外触发重排序——路由引擎每次请求都从缓存读取最新价格，缓存定时刷新即可。

### 5.4 模型下线 → 自动移出

```
情报爬虫检测到某模型 404 / 官方公告下线
    │
    ▼
AI 解析确认（confidence > 0.9）:
    UPDATE model_registry SET status='deprecated' WHERE model_id=X
    │
    ▼
自动移出:
    DELETE FROM model_group_members WHERE model_id=X
    INSERT model_capabilities_log（field_changed='status', new_value='deprecated'）
    │
    ▼
路由缓存刷新后，该模型不再参与任何组的选优
```

### 5.5 标签更新来源优先级

当多个来源对同一模型的标签有不同结论时：

```
优先级（高→低）:
1. 用户手动修改（Dashboard / 直接改库）
2. 厂商官方公告（情报层爬取自官方页面）
3. 公开 Benchmark 榜单（定期同步）
4. AI 推断（解析非官方来源）

规则:
- 高优先级来源的标签不会被低优先级覆盖
- 低优先级来源若与现有标签冲突，记录到 model_capabilities_log 并告警
- tier 调整永远需要人工确认（即使 AI 置信度 > 0.8）
```

### 5.6 情报采集源分类

| 采集源类型 | 目标 | 频率 | 产出 |
|-----------|------|------|------|
| 定价页 | 各厂商 pricing 页面 | 每 15 分钟 | price_snapshots |
| 模型列表页 | 各厂商 models 页面 | 每 1 小时 | model_registry（新模型发现） |
| 活动/公告页 | 促销、降价公告 | 每 30 分钟 | promotions |
| Benchmark 榜单 | LMSYS Arena、SuperCLUE 等 | 每 24 小时 | benchmark_scores |
| 第三方平台 | 硅基流动、火山引擎模型列表 | 每 1 小时 | channels + price_snapshots |

## 六、数据模型

### 现有表（9 张，Phase 1 + 2D-4 已实现）

| 表名 | 用途 | 引入阶段 |
|------|------|----------|
| `channels` | 渠道（厂商接入点） | Phase 1 |
| `accounts` | 账号（多账号兜底） | Phase 1 |
| `price_snapshots` | 价格快照（支持峰谷时段） | Phase 1 |
| `promotions` | 优惠活动（情报核心） | Phase 1 |
| `channel_quotas` | 频限配额配置 | Phase 1 |
| `usage_logs` | 用量日志（归档查账） | Phase 1 |
| `parse_rules` | 解析规则（自愈循环） | Phase 1 |
| `intel_sources` | 情报采集源配置 | Phase 1 |
| `model_cards` | 模型评测卡（OpenCompass 引用分） | Phase 2D-4 |

### 待新增表（4 张，Phase 2 模型组）

#### model_registry（模型注册表）

```sql
CREATE TABLE model_registry (
    id              INTEGER PRIMARY KEY,
    model_id        TEXT NOT NULL UNIQUE,       -- 规范ID: "deepseek-v4-pro"
    family          TEXT NOT NULL,              -- 模型族: "deepseek"
    display_name    TEXT,                       -- 展示名: "DeepSeek V4 Pro"

    -- 能力标签
    tier            TEXT NOT NULL DEFAULT 'T2', -- 能力档: T0/T1/T2/T3/T4
    capabilities    TEXT,                       -- JSON数组: ["reasoning","code","math"]
    context_length  INTEGER,                   -- 最大上下文窗口
    modality        TEXT DEFAULT 'text',       -- text / vision / multimodal / audio

    -- 基准分（情报层定期同步）
    benchmark_scores TEXT,                      -- JSON: {"mmlu":88.5,"humaneval":92.0,"swe_bench":72}

    -- 元信息
    is_open_source  BOOLEAN DEFAULT TRUE,      -- 是否开源权重
    release_date    TEXT,                      -- 发布日期
    status          TEXT DEFAULT 'active',     -- active / deprecated / preview
    notes           TEXT,

    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

**字段说明**：
- `model_id`：全局唯一标识，格式为 `{family}-{version}[-{variant}]`，如 `deepseek-v4-pro`、`qwen3-235b`、`glm-4-9b`
- `tier`：由人工配置或 AI 推断（需人工确认），**不会**被情报层自动修改
- `capabilities`：能力标签枚举值为 `reasoning` / `code` / `math` / `general` / `creative` / `agent` / `vision` / `long_context`
- `benchmark_scores`：仅用于 Dashboard 展示和人工参考，**不参与**路由决策

**注意**：Phase 2D-4 已实现的 `model_cards` 表是过渡设计，Phase 2 模型组落地时应**合并/迁移**到 `model_registry`，避免双轨。

#### model_groups（模型组定义表）

```sql
CREATE TABLE model_groups (
    id              INTEGER PRIMARY KEY,
    group_id        TEXT NOT NULL UNIQUE,       -- 组ID: "tier:balanced" / "scene:code"
    display_name    TEXT,                       -- 展示名: "均衡性价比组"

    -- 组过滤条件（成员必须满足）
    tier            TEXT,                       -- 限定能力档（NULL=不限）
    capabilities    TEXT,                       -- JSON数组: 要求的能力标签交集
    min_context     INTEGER DEFAULT 0,         -- 最低上下文要求
    require_vision  BOOLEAN DEFAULT FALSE,     -- 是否要求视觉能力

    -- 路由策略
    strategy        TEXT DEFAULT 'cheapest',   -- cheapest / priority / weighted / latency
    fallback_group  TEXT,                       -- 降级目标组ID（NULL=无降级）

    enabled         BOOLEAN DEFAULT TRUE,
    sort_order      INTEGER DEFAULT 0,         -- Dashboard 展示排序
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

#### model_group_members（组成员关联表）

```sql
CREATE TABLE model_group_members (
    id              INTEGER PRIMARY KEY,
    group_id        TEXT NOT NULL REFERENCES model_groups(group_id),
    model_id        TEXT NOT NULL REFERENCES model_registry(model_id),

    priority        INTEGER DEFAULT 100,       -- 组内优先级（越小越优先，同价时生效）
    weight          REAL DEFAULT 1.0,          -- weighted 策略时的权重
    max_cost_ratio  REAL,                      -- 价格超过组内最低价 N 倍时排除（NULL=不限）

    added_by        TEXT DEFAULT 'manual',     -- manual / intel_auto
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(group_id, model_id)
);
```

#### model_capabilities_log（标签变更审计日志）

```sql
CREATE TABLE model_capabilities_log (
    id              INTEGER PRIMARY KEY,
    model_id        TEXT NOT NULL,
    field_changed   TEXT NOT NULL,             -- "tier" / "capabilities" / "status" / "benchmark_scores"
    old_value       TEXT,
    new_value       TEXT,
    source          TEXT NOT NULL,             -- "manual" / "intel_crawler" / "benchmark_sync" / "ai_infer"
    confidence      REAL,                      -- AI 推断时的置信度（manual 时为 NULL）
    approved        BOOLEAN DEFAULT FALSE,     -- 是否已人工确认
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 现有表扩展（加密字段）

#### `accounts.api_key_ciphertext`（API-Sec-1 已实现）

```sql
ALTER TABLE accounts ADD COLUMN api_key_ciphertext BLOB;
-- api_key TEXT 字段保留, 标记 deprecated (双读兜底, 迁移期使用)
```

**为什么加密**：
- DB 文件备份 / 误上传云盘 / 同步到其他设备 → 拿到的是 ciphertext，**看不到明文 key**
- `.env` 跟 `accounts.api_key` **解耦**：`.env` 漏了不会带出所有 channel key
- 详细的现状分析 + 设计对比见 [api-key-security.md](api-key-security.md)

**加密方案**（轻量化自实现）：
- 算法：**XOR 循环 + Base64**（`security/crypto.py` ~30 行，**零外部依赖**）
- 密钥派生：用 `secrets.token_hex(32)` 生成 64 字符 hex master key（32 bytes 原始）
- 主密钥：`.env` 里的 `AIMC_MASTER_KEY`（64 字符 hex = 32 bytes，与其他 .env 字段模式一致）
- 不加密 .env 本身（避免"开机输入 master key 解密 .env 才能解 DB"的鸡生蛋问题）
- **每次部署重新生成 master key**（无跨环境同步需求，丢了 = 重新部署）
- **诚实评估**：XOR 不是密码学标准加密（已知明文能解），但在本项目场景下安全等级 ≈ Fernet（master key 跟 .env 在一起时 Fernet 也救不了）

**双读策略**（过渡期）：
- 读：优先 `api_key_ciphertext`（解密），fallback 到 `api_key`（明文，仅迁移期）
- 写：永远写 ciphertext，**不再写明文**到 `api_key` 字段

**主密钥丢了 = 永久性数据丢失**（ciphertext 全部不可解）：
- 缓解：`scripts/gen_master_key.py` 生成时强制让 user 存到密码管理器
- 缓解：master key 跟 ciphertext 备份分开保存（用户责任）

### 现有表扩展

#### channels 表新增字段

```sql
ALTER TABLE channels ADD COLUMN provider_type TEXT DEFAULT 'official';
-- official: 模型厂商官方
-- third_party: 第三方托管（硅基流动/阿里云/火山引擎等）
-- aggregator: 聚合平台
-- local: 本地部署

ALTER TABLE channels ADD COLUMN billing_mode TEXT DEFAULT 'pay_per_token';
-- pay_per_token: 按量计费（默认）
-- subscription: 订阅制（套餐内零边际成本）
-- free: 完全免费
```

**billing_mode 对路由的影响**：
- `free` 渠道：价格视为 0，在 cheapest 策略中永远排第一
- `subscription` 渠道：价格视为 0（套餐内），但受套餐配额约束
- `pay_per_token` 渠道：使用 price_snapshots 中的实际价格

## 七、核心模块设计

### 7.1 路由决策引擎 (`gateway/router.py`)

现有功能（保持不变）：
- 精确模型名匹配 → 渠道选优
- 时段价格匹配
- 频限/配额过滤
- 多维排序 + fallback

新增功能：
- `resolve_model_group(group_id)` — 解析组定义，返回成员模型列表
- `select_best_in_group(group_id, preferences)` — 组内跨模型全局选优
- `apply_preferences(candidates, prefs)` — 应用 Agent 偏好约束
- 递归 fallback：组内无可用 → 查 fallback_group → 递归
- 精确模型路由 fallback 到底层：channel 全不匹配 → 透传到 priority 最低 channel（Phase 2E, 聚合平台兜底）

### 7.2 频限器 (`gateway/limiter.py`)

纯内存实现（不变）：
- **QPM**：60s 滑动窗口 deque
- **TPM**：60s 滑动窗口（按 token 累加）
- **并发数**：asyncio.Semaphore
- **日限额**：每日零点重置计数器

### 7.3 代理透传 (`gateway/proxy.py`)

核心原则：**绝不解析 SSE 流，原样透传字节**（不变）。

现有能力：
- 流式：`httpx` stream + `aiter_raw()` → `StreamingResponse`
- 非流式：等待完整响应 → `JSONResponse`
- 超时：120s（大模型响应慢）
- 失败告警：5xx + 401/403 触发 `alerter.send_alert` (Phase 2E)
- 异步记录用量（`usage_logs`）

新增（Phase 2）：
- 响应头注入实际路由信息（X-AIMC-Actual-Model 等）

### 7.4 情报采集层 (`intelligence/`)

插件式采集源设计（不变）+ 新增模型组反馈：
- `base.py` 定义 `IntelSourceBase` 协议
- 每个厂商一个文件，注册到调度器
- 流程：定时抓取 → Diff 检测 → AI 解析 → 入库
- 自愈：置信度 > 0.8 自动入库；< 0.8 告警人工
- **新增**：解析结果中包含模型元信息时，触发 model_registry 写入和入组逻辑

### 7.5 模型组管理器 (`gateway/group_manager.py` — 新增)

职责：
- 加载模型组配置到内存缓存
- 提供 `resolve(group_id) → list[model_id]` 查询
- 处理情报层的新模型入组/下线移出事件
- 维护 model_capabilities_log 审计日志

### 7.6 告警器 (`intelligence/alerter.py`)

支持 channel（通过 `ALERT_CHANNEL` env 选）：
- `log` 只 log（默认 dry-run）
- `bark` iOS Bark push（国内首选）
- `serverchan` Server 酱（推微信）
- `feishu` 飞书自定义机器人（国内办公首选）

Phase 2E throttle（**已实现**）：同 `(channel_name, status_code)` key 60s 内只发 1 次，
防 webhook 刷屏。锁内 check+mark 原子完成，防并发 race。

## 八、运维加固（Phase 2.5 已完成）

2026-07-25 在 Phase 2 实施前补的 4 个真实痛点改进，让"日常运维不痛"先于"模型组新能力"。

### 8.1 硅基流动 91 个 model 入库（`#1`）

**问题**：`/v1/models` 端点直接 `select price_snapshots.model` 字段，手填只覆盖 2 个，掩盖了"硅基流动 1 key 通吃 100+" 的核心能力。

**解决**：
- 新增 `scripts/refresh_siliconflow_models.py`，调硅基流动 `/v1/models` 拿 91 个 model
- 给 `siliconflow_aggregator` channel 全部入 `price_snapshots`（`input_price/output_price` 留空，聚合平台无自有价格）
- 已存在的 model 跳过，**idempotent** 可重跑

**效果**：`GET /v1/models` 从 2 个 → 91 个。`zai-org/GLM-4.5V` chat 测试 200 OK。

**commit**: `636c334`

### 8.2 告警 throttle（`#3`）

**问题**：Phase 2E proxy 失败告警触发条件 5xx + 401/403，硅基流动挂掉时飞书会刷屏。

**解决**：
- `alerter.send_alert(message, key=None)` 新增 throttle key 参数
- 同 key 在 60s 窗口内只发 1 次
- throttle key 形如 `"{channel_name}:{status_code}"`，不同 status 独立
- 锁内 check+mark 原子完成，防并发 race
- 失败也占用 60s 槽位（防 webhook 暂时 503 反复重试刷屏）

**验证**：
- 单元测试 6/6（同 key / 不同 key / 失败占槽 / log 跳过 / key=None / 并发安全）
- E2E：DB 改假 key → 重启 uvicorn → 连续 3 次 chat → 飞书**只收 1 条**

**commit**: `244f279`

### 8.3 pre-commit hook 防 `.env` 误 commit（`#4`）

**问题**：真实 API key 在 `.env` 里，误 commit 进 git 历史 → 信任丢失，不可逆。

**解决**（双层防护）：
- `.gitignore` 排除 `.env` / `.env.*`（已有）
- `.githooks/pre-commit` 做**第二层防护**：检查 staged files，匹配 `.env` / `.env.local` 等模式则拒绝 commit
- 启用：`git config core.hooksPath .githooks`（**local config，不 commit**）

**验证**：
```
$ git add -f .env && git commit -m "test"
[pre-commit] FAIL: 检测到敏感文件, 拒绝 commit:
  - .env  (匹配 pattern: .env)
```

正常文件走 hook 不受影响，`.env.example` 走白名单。

**文件**: `.githooks/pre-commit`（在 commit `7d28552` 已存在，hook 启用是 2026-07-25 补的 local config）

### 8.4 手动 cache refresh endpoint（`#5`）

**问题**：router cache 启动时从 DB 加载一次，虽然 scheduler 每 300s 自动刷新，但改完 channel key 后立即测会被"5 分钟还没到"卡住。

**解决**：
- 新增 `POST /dashboard/api/cache/refresh` endpoint
- 立即调 `route_engine.refresh_cache()`
- 返回 `{ "status": "ok", "channels": N, "models": M }`，让用户看到当前 cache 状态
- 失败 500

**验证**（7 步 E2E）：改 key → POST refresh → 401 → 改回真 key → POST refresh → 200。**不需要重启 uvicorn**。

**commit**: `e50bb7a`

### 8.5 运维能力边界

**改完即生效（不用重启）**：
- channel key、priority、status
- 新增/删除 model（跑 `refresh_siliconflow_models.py` → POST refresh）
- 新增/删除 account
- 告警 channel 切换（重启后生效，因 settings 是 Pydantic 启动加载）

**仍需重启**：
- 改了 `config.py` 里 Pydantic Settings 字段
- 改了 `gateway/router.py` / `proxy.py` / `intelligence/alerter.py` 等代码

## 九、预置模型组配置

```yaml
# 按能力档（Agent 主要使用方式）
groups:
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
    fallback_group: null
    members: [qwen2.5-7b, glm-4-9b, deepseek-r1-distill-7b]

  # 按场景（可选）
  - group_id: "scene:code"
    display_name: "代码专用组"
    capabilities: ["code"]
    min_context: 64000
    strategy: cheapest
    fallback_group: "tier:balanced"
    members: [deepseek-v4-pro, glm-5.1, qwen3-235b, deepseek-v4-flash]

  - group_id: "scene:free"
    display_name: "零成本组"
    strategy: priority
    fallback_group: null
    members: [qwen2.5-7b-sf, glm-4-9b-sf, deepseek-r1-7b-sf]
```

## 十、关键设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 自研网关 vs OneAPI | 自研 FastAPI | 更轻量、完全可控、与情报层同进程 |
| SQLite vs PostgreSQL | SQLite WAL | 零运维、单文件、个人工具并发足够 |
| 同进程 vs 独立爬虫 | 同进程异步 | 简化部署 |
| 内存频限 vs Redis | 内存 | 单进程无需共享状态 |
| Dashboard | htmx + 静态文件 | 零构建，维护成本极低 |
| 路由是否分析 Prompt | **否** | AIMC 是透传网关，任务判断交给 Agent |
| 模型组 vs 精确模型 | **两者共存** | 向后兼容 + 灵活性 |
| 能力标签来源 | **人工配置 + 情报辅助** | 不引入 ML 分类器，保持简单 |
| tier 调整权限 | **仅人工确认** | 避免 AI 误判导致路由异常 |
| 组内默认策略 | **cheapest** | 符合"省钱"核心定位 |
| 订阅制渠道 | **价格视为 0** | 套餐内调用零边际成本 |
| 本地模型 | **作为渠道（provider_type=local）** | 频限由 Agent 侧管理 |
| 模型组降级 | **组间 fallback chain** | 借鉴 9Router 三层 + LiteLLM fallback |
| 精确模型无匹配时 | **fallback 到 priority 最低 channel** | 聚合平台 1 key 通吃兜底 (Phase 2E) |
| 告警 throttle | **锁内 check+mark 60s 窗口** | 防刷屏优先，宁可 60s 不重试 |
| `.env` 保护 | **gitignore + pre-commit 双层** | 单层 gitignore 不够，commit -f 可绕过 |
| Router cache 刷新 | **5min 自动 + endpoint 手动** | 平时够用，改完立即刷不用等 |
| `accounts.api_key` 加密 | **XOR+Base64 轻量化 (零依赖)** | DB 文件泄露 ≠ channel key 泄露；不用 cryptography 库；不上 Infisical（破坏单进程原则） |
| 加密 key 丢了 | **永久性数据丢失** | 每次部署重新生成 master key；丢了 = 重新部署（个人用工具可接受） |
| 加密 vs .env 关系 | **不加密 .env 本身** | 避免"开机输入 master key 解密 .env 才能解 DB"的鸡生蛋问题 |

## 十一、部署方案

```bash
# 单命令启动（网关+调度器+Dashboard 同进程）
uvicorn main:app --host 0.0.0.0 --port 8080

# 进程守护（Linux）
supervisorctl start aimc

# 数据备份（cron 每日）
python scripts/backup.py
```

环境要求：Python 3.11+，无需 Docker。

## 十二、相关文档

- [开发计划](development-plan.md) — Phase 1/2/3/4 实施路线与验证标准
- [竞品调研](competitive-research.md) — OpenRouter/9Router/LiteLLM/RouteLLM 对比
- [API 接口文档](api.md) — 网关接口说明
- [快速开始指南](quickstart.md) — 安装、配置、部署
