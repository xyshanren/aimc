# Phase 2 设计文档：多维模型组

> 日期：2026-07-29
> 状态：🔨 设计中（待 review → 实施）
> 关联：[architecture.md §四 多维模型组设计](architecture.md#四多维模型组设计) / [development-plan.md Phase 2](development-plan.md#phase-2-多维模型组当前重点)

---

## 1. 目标

让 Agent 可以按 **"能力档 / 场景"** 调用模型，而不是写死模型名：

```python
# 现有 (精确模型)
client.chat.completions.create(model="deepseek-chat", ...)

# 目标 (能力档)
client.chat.completions.create(model="tier:balanced", ...)
#  → AIMC 在 tier:balanced 组内选最便宜的 (model, channel, account) 三元组
#  → 失败时自动降级到 fallback_group (tier:light → tier:free)
```

向后兼容：精确模型名请求行为不变。

---

## 2. 决策回顾

### 2.1 `model_cards` vs `model_registry` 双轨保留 (B 方案)

| 表 | 职责 | 谁写 | 谁读 |
|----|------|------|------|
| `model_cards` (43 张) | **模型评测档案** —— 公开 benchmark 分数（OpenCompass / LMSYS Arena），不含 tier/capabilities | 人工 seed（`scripts/seed_model_cards.py`） | Dashboard 展示 / 路由时 `model_id` soft join 补充元信息 |
| `model_registry` (新) | **模型路由元信息** —— tier / family / capabilities / context_length / modality | 人工配置（Phase 2）+ 情报层辅助（Phase 3） | 路由决策（组过滤 / 降级） |

**为什么双轨而非合并**：

- 评测卡（"DeepSeek-V3 在 OpenCompass 拿了 75 分"）是**相对稳定**的引用数据，几个月才动一次
- 路由元信息（"这个 model 是 T2 档 / 支持 vision"）是**配置性**的，会随业务调整
- 合并的迁移成本 = 改 ~10 个 call site + 改 seed 脚本 + 风险大
- 双轨用 `model_id` 字符串 soft join，**零强制约束**——评测卡下架不影响 registry，registry 调整不影响评测分

**`model_id` 命名约定**：`{family}-{version}[-{variant}]`，例如 `deepseek-v4-pro` / `qwen3-235b` / `glm-4-9b` / `kimi-k2`。

### 2.2 路由策略默认 `cheapest`

| 策略 | 含义 | 适用 |
|------|------|------|
| `cheapest` | 输入价格 ASC（默认） | 日常省钱 |
| `priority` | 人工优先级 DESC | 有明确偏好 |
| `weighted` | 权重随机 | 分散用量避免单渠道频限（Phase 3 再实现） |
| `latency` | 历史平均延迟 | 对响应速度敏感（Phase 4） |

**Phase 2 只实现 `cheapest` + `priority`**；`weighted` 和 `latency` 留接口位但不实现，留给 Phase 3/4。

### 2.3 `tier` 调整永远人工确认

`model_capabilities_log.approved` 字段：AI 推断的 tier 变更**永远默认 `approved=FALSE`**，必须人工在 Dashboard 点确认才生效。这是安全阀，避免 AI 误判导致路由异常（architecture.md §十 已定）。

---

## 3. 数据模型

完整 schema 在 [architecture.md §六](architecture.md#待新增表4张phase-2-模型组) 已有，这里只列关键决策点。

### 3.1 `model_registry`

```sql
CREATE TABLE model_registry (
    id              INTEGER PRIMARY KEY,
    model_id        TEXT NOT NULL UNIQUE,         -- "deepseek-v4-pro" / "qwen3-235b"
    family          TEXT NOT NULL,                -- "deepseek" / "qwen" / "glm" / "kimi"
    display_name    TEXT,
    tier            TEXT NOT NULL DEFAULT 'T2',   -- T0/T1/T2/T3/T4
    capabilities    TEXT,                         -- JSON: ["reasoning","code","math"]
    context_length  INTEGER,
    modality        TEXT DEFAULT 'text',          -- text / vision / multimodal
    benchmark_scores TEXT,                        -- JSON: 仅展示用, 不参与路由
    is_open_source  BOOLEAN DEFAULT TRUE,
    release_date    TEXT,
    status          TEXT DEFAULT 'active',        -- active / deprecated / preview
    notes           TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

**注意**：`tier` 和 `capabilities` 是路由决策用的"硬"字段；`benchmark_scores` 是 Dashboard 展示用的"软"字段。

### 3.2 `model_groups` + `model_group_members`

```sql
-- 组定义
CREATE TABLE model_groups (
    id              INTEGER PRIMARY KEY,
    group_id        TEXT NOT NULL UNIQUE,         -- "tier:balanced" / "scene:code"
    display_name    TEXT,
    tier            TEXT,                         -- 限定能力档 (NULL=不限)
    capabilities    TEXT,                         -- JSON: 要求的能力标签交集
    min_context     INTEGER DEFAULT 0,
    require_vision  BOOLEAN DEFAULT FALSE,
    strategy        TEXT DEFAULT 'cheapest',
    fallback_group  TEXT,                         -- "tier:light" (NULL=无降级)
    enabled         BOOLEAN DEFAULT TRUE,
    sort_order      INTEGER DEFAULT 0,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 组成员
CREATE TABLE model_group_members (
    id              INTEGER PRIMARY KEY,
    group_id        TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    priority        INTEGER DEFAULT 100,         -- 组内优先级
    weight          REAL DEFAULT 1.0,            -- weighted 策略用 (Phase 2 不实现)
    max_cost_ratio  REAL,                        -- 价格超过组内最低 N 倍时排除
    added_by        TEXT DEFAULT 'manual',       -- manual / intel_auto
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(group_id, model_id)
);
```

**不强制 FK**：因为 `model_id` 跟 `model_registry` 用 soft join，组成员可能包含"还没入库的新 model"（情报层发现的）。

### 3.3 `model_capabilities_log`

```sql
CREATE TABLE model_capabilities_log (
    id              INTEGER PRIMARY KEY,
    model_id        TEXT NOT NULL,
    field_changed   TEXT NOT NULL,                -- "tier" / "capabilities" / "status"
    old_value       TEXT,
    new_value       TEXT,
    source          TEXT NOT NULL,                -- "manual" / "intel_crawler" / "ai_infer"
    confidence      REAL,                         -- AI 推断时填
    approved        BOOLEAN DEFAULT FALSE,        -- 人工确认前不生效
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 3.4 `channels` 表扩展

```sql
ALTER TABLE channels ADD COLUMN provider_type TEXT DEFAULT 'official';
-- official / third_party / aggregator / local

ALTER TABLE channels ADD COLUMN billing_mode TEXT DEFAULT 'pay_per_token';
-- pay_per_token / subscription / free
```

**`billing_mode` 路由影响**：`subscription` 和 `free` 在 `cheapest` 策略下价格视为 `0`，永远优先。

---

## 4. 路由升级

### 4.1 入口解析

```python
# gateway/router.py 新增
def select_channel(self, model: str) -> Optional[dict]:
    if model.startswith("tier:") or model.startswith("scene:"):
        return self._select_by_group(model)
    return self.select_best_channel(model)  # 精确模型, 现有逻辑不变
```

### 4.2 模型组路由伪代码

```python
def _select_by_group(self, group_id: str) -> Optional[dict]:
    visited = set()  # 防 fallback chain 死循环
    current = group_id

    while current and current not in visited:
        visited.add(current)

        # 1. 查组定义
        group = self._groups_cache.get(current)
        if not group or not group.get("enabled"):
            current = group.get("fallback_group")
            continue

        # 2. 查成员 model_id 列表
        members = self._group_members_cache.get(current, [])

        # 3. 组过滤：tier / capabilities / min_context / require_vision
        candidates = []
        for model_id in members:
            registry = self._registry_cache.get(model_id)
            if not registry: continue
            if not self._match_group_filter(registry, group): continue

            # 4. 对每个 member model_id, 走精确模型路由（复用现有逻辑）
            best = self.select_best_channel(model_id)
            if best:
                # 5. 注入 X-AIMC-Actual-Model 响应头
                best["_actual_model"] = model_id
                best["_group_id"] = current
                candidates.append((best, registry))

        if not candidates:
            logger.warning(f"组 {current} 全部不可用, fallback 到 {group.get('fallback_group')}")
            current = group.get("fallback_group")
            continue

        # 6. 按组策略排序
        if group["strategy"] == "priority":
            candidates.sort(key=lambda x: x[0].get("priority", 0), reverse=True)
        else:  # cheapest (默认)
            candidates.sort(key=lambda x: x[0].get("prices", [{}])[0].get("input_price", 999))

        best = candidates[0][0]
        logger.info(f"组路由: {group_id} -> {best['_actual_model']} via {best['name']}")
        return best

    logger.error(f"组 fallback chain 死循环或全部不可用: {group_id}")
    return None
```

### 4.3 缓存设计

新增 3 个内存 cache（跟 `route_engine._channels_cache` 一样 5min 自动 + 手动 refresh）：

| cache | key | value |
|-------|-----|-------|
| `_registry_cache` | `model_id` | ModelRegistry ORM |
| `_groups_cache` | `group_id` | ModelGroup ORM |
| `_group_members_cache` | `group_id` | `list[model_id]` |

`POST /dashboard/api/cache/refresh` endpoint（Phase 2.5 #5 已实现）同时刷这 3 个 cache。

### 4.4 响应头注入

```python
# gateway/proxy.py
response.headers["X-AIMC-Actual-Model"] = channel["_actual_model"]  # 精确模型名
response.headers["X-AIMC-Actual-Channel"] = channel["name"]
response.headers["X-AIMC-Group-Id"] = channel.get("_group_id", "")  # tier:balanced
```

**为什么注入**：Agent 想知道"我的 tier:balanced 实际打到哪个 model + channel"，方便调试和审计。

### 4.5 Agent 偏好 (extra_body)

```python
# Phase 2 实现（基础 3 个）
{
  "aimc_preferences": {
    "prefer_family": "deepseek",      # 同价时优先
    "exclude_providers": ["local"],   # 排除
    "max_input_price": 5.0            # 价格上限
  }
}
```

**实现位置**：`gateway/proxy.py` 解析 `extra_body.aimc_preferences`，传给 `select_channel(prefs=...)`。

---

## 5. 实施步骤（小步走）

每步独立 commit + 验证，不混。

| 步 | 改动 | 工时 | 验证 |
|----|------|------|------|
| **2.1** | 4 张新表 ORM + 幂等迁移（`db/init_db.py` 检表不存在则 CREATE） | 1 h | `sqlite3 data/aimc.db ".tables"` 看 4 张新表 |
| **2.2** | 种子脚本 `scripts/seed_model_registry.py`：先入 8-10 个 model（`deepseek-chat` / `qwen-plus` / `glm-4-plus` / `kimi-k2` / `MiniMax-Text-01` 等已有 channel 支持的），手动填 tier/capabilities | 1 h | `SELECT * FROM model_registry` 看 8-10 行 |
| **2.3** | `channels` 加 `provider_type` / `billing_mode` + 手动 backfill 现有 9 个 channel | 30 min | `SELECT name, provider_type, billing_mode FROM channels` 看 9 行 |
| **2.4** | 路由升级 `gateway/router.py`：`select_channel(model)` + `_select_by_group()` + 3 个 cache | 2 h | 单元测试 + chat 请求 `model="tier:balanced"` 走通 |
| **2.5** | 响应头注入（`X-AIMC-Actual-Model/Channel/Group-Id`） | 30 min | curl 看 response headers |
| **2.6** | Agent 偏好 `aimc_preferences` 3 个字段 | 1 h | `prefer_family=deepseek` 验证同价时优先 deepseek 渠道 |
| **2.7** | `scripts/seed_model_groups.py`：seed 4 个 tier 组 + 2 个 scene 组（参考 architecture.md §九） | 1 h | `SELECT * FROM model_groups` 看 6 行 + `model_group_members` 看 ~20 行 |
| **2.8** | `/v1/models` endpoint 升级：返回 `model` 列表 + `group` 列表 | 1 h | `curl /v1/models` 看到 `tier:balanced` 等组名 |
| **2.9** | Dashboard 极简管理页：`/dashboard/models` 看 model_registry + `/dashboard/groups` 看 model_groups | 2 h | 浏览器能看 |
| **2.10** | 集成测试：精确模型请求 / 组路由 / fallback chain / 偏好过滤 4 个 happy-path | 1 h | 4/4 通过 |

**总工时 ~11h**，分 2-3 个 sprint 散开。

---

## 6. 验证标准

### 6.1 数据层

- [ ] 4 张新表创建成功（`init_db` 幂等）
- [ ] 8-10 个 model seed 进 `model_registry`，跟现有 channel 实际支持的 model 对得上
- [ ] 9 个 channel 全部 backfill `provider_type` + `billing_mode`
- [ ] 6 个 group seed（4 tier + 2 scene），组成员 ~20 行

### 6.2 路由层

- [ ] `model="deepseek-chat"` 行为不变（精确模型路由走原路径）
- [ ] `model="tier:balanced"` 返回 200 + `X-AIMC-Actual-Model` 响应头是 group 成员
- [ ] `model="tier:nonexistent"` 返回 503 + 日志记 fallback chain 死循环
- [ ] 组内全部 channel 频限满 → 自动降级到 fallback_group
- [ ] `max_input_price=5.0` 过滤生效
- [ ] `prefer_family=deepseek` 同价时优先 deepseek channel

### 6.3 兼容性

- [ ] 现有 91 个硅基流动 model 请求行为不变
- [ ] 现有 8-10 个 channel（deepseek/qwen/glm/kimi/MiniMax 等）请求行为不变
- [ ] 精确模型名 `model="MiniMax-Text-01"` 仍能路由到 siliconflow_aggregator fallback
- [ ] `POST /dashboard/api/cache/refresh` 同步刷 3 个新 cache

---

## 7. 风险 / 已知限制

| 风险 | 缓解 |
|------|------|
| `model_id` 命名不一致（不同 channel 用不同写法） | Phase 2.1 seed 时人工规范化现有 8-10 个 model；Phase 3 情报层发现新 model 时强制规范 |
| 组成员包含"还没入库的 model" | 路由时静默 skip（`registry_cache.get(model_id) is None`），Dashboard 标"missing model" |
| Fallback chain 死循环（A→B→A） | `_select_by_group` 用 `visited` set 防御；进入死循环返回 503 + 日志告警 |
| tier 误改导致路由全错 | `model_capabilities_log.approved=FALSE` 时 dashboard 红色提示 + AI 推断永不自动改 tier |
| 响应头注入影响 SSE 流式 | 在 `proxy.py` 第一个 chunk 前注入 header；流式 body 保持原样 |

---

## 8. 不在 Phase 2 范围

- **Dashboard 增删改 model/组**（Phase 2.9 只做"查看"，CRUD 留 Phase 3）—— 跟"dashboard 编辑 key 暂不做"哲学一致
- **Agent 偏好高级字段**（`min_context` / `require_vision`）—— Phase 2 只实现 3 个基础字段
- **`weighted` / `latency` 策略** —— 留 Phase 3/4
- **AI 自动入组 / 自动 tier 推断** —— 留 Phase 3 情报层
- **价格趋势预测** —— Phase 4

---

## 9. 待 review 项

请确认下面 3 个决策点（如果不动，Phase 2.1 开工）：

1. **B 双轨保留**：model_cards 永久存 benchmark，model_registry 存路由元信息，soft join ✓ (已拍)
2. **种子规模**：Phase 2.2 seed 8-10 个 model 是否够？建议先 `deepseek-chat` / `qwen-plus` / `qwen-max` / `glm-4-plus` / `glm-4-air` / `kimi-k2` / `MiniMax-Text-01` / `doubao-pro-32k` 8 个（覆盖已配 channel 的实际支持 model）
3. **响应头命名**：`X-AIMC-Actual-Model` / `X-AIMC-Actual-Channel` / `X-AIMC-Group-Id` —— 跟 hermes-agent-cn v0.3.0 计划消费的字段对齐（hermes-tray 通过 hermes-agent-cn 间接受益）

如果都没问题，回复"开干"，我从 Phase 2.1（4 张新表 ORM + 幂等迁移）开始。
