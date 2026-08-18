# hermes-agent-cn 集成联调报告

> 日期：2026-07-29
> 关联：hermes-agent-cn 集成 PR (草案见 [hermes-agent-cn-integration-pr.md](hermes-agent-cn-integration-pr.md), 已提给 hermes-agent-cn 仓库)
> AIMC 侧 commit：`c4b6f1e` (3.E 响应头增强) + `6358378` (联调 E2E)


## 📦 历史归档说明

> **本文档已归档**（2026-08-15 文档重组时迁移到 docs/archive/）。
> 新内容见 [docs/index.md](../index.md) 或相关 [docs/dev/](../dev/) / [docs/user/](../user/) 文档。

## 一、联调范围

AIMC 侧为配合 hermes-agent-cn 集成 PR，准备了 3 个能力：

| 能力 | commit | 测试 |
|------|--------|------|
| `/v1/models` 元信息增强 (Phase 3.B) | `d72f65b` | E2E 场景 1 |
| 响应头增强 `X-AIMC-Actual-Tier` / `-Capabilities` (Phase 3.E) | `c4b6f1e` | E2E 场景 2、3 |
| Profile 校验依据 (`data_groups`) | `d72f65b` (含) | E2E 场景 4 |

## 二、4 个联调场景（全部通过）

### 场景 1 — hermes-agent-cn 启动拉取 `/v1/models`

**目的**：验证 hermes-agent-cn 启动时 GET `/v1/models` 能拿到完整的元信息 + group 定义。

**实际响应（mock 数据，真实数据量更大）**：

```json
{
  "object": "list",
  "data": [
    {
      "id": "Qwen/Qwen2.5-72B-Instruct",
      "object": "model",
      "owned_by": "qwen",
      "aimc_metadata": {
        "family": "qwen",
        "tier": "T3",
        "capabilities": ["reasoning", "code", "math"],
        "context_length": 32768,
        "modality": "text",
        "is_open_source": true,
        "providers": ["siliconflow"],
        "groups": ["tier:flagship"],
        ...
      }
    },
    ...5 个 model
  ],
  "data_groups": [
    {
      "id": "tier:flagship",
      "type": "model_group",
      "display_name": "旗舰组",
      "tier": "T3",
      "strategy": "cheapest",
      "fallback_group": "tier:strong",
      "members": ["Qwen/Qwen2.5-72B-Instruct"],
      "aimc_metadata": {
        "fallback_chain": ["tier:flagship", "tier:strong", "tier:balanced", "tier:light"],
        "member_tiers": ["T3"],
        "min_context": 0,
        "require_vision": false,
        "enabled": true
      }
    },
    ...4 个 group
  ]
}
```

**验证项**：
- ✅ OpenAI 向后兼容（`id` / `object="model"` / `owned_by`）
- ✅ `aimc_metadata` 必填字段齐全（family / tier / capabilities / context_length / modality / providers / groups / ...）
- ✅ 4 个 hermes-agent-cn profile 配置 group 都在：`tier:flagship` / `tier:strong` / `tier:balanced` / `tier:light`
- ✅ `fallback_chain` 自动展开（最多 5 跳防环）
- ✅ `capabilities` 是 list 不是字符串（hermes-agent-cn 不必二次解析）

### 场景 2 — 组路由 `tier:balanced` → 5 个响应头

**目的**：验证 hermes-agent-cn 发 `model="tier:balanced"` 时，AIMC 响应头能让 agent 知道实际命中。

**实际响应头**：

```http
X-AIMC-Actual-Model: deepseek-v2.5
X-AIMC-Actual-Channel: siliconflow_aggregator
X-AIMC-Group-Id: tier:balanced
X-AIMC-Actual-Tier: T2
X-AIMC-Actual-Capabilities: ["code"]
```

**验证项**：
- ✅ 5 个 header 全部注入
- ✅ `X-AIMC-Actual-Model` 命中组内最便宜 model（`deepseek-v2.5` 输入 1.0 元/百万）
- ✅ `X-AIMC-Actual-Tier=T2` 来自 model_registry
- ✅ `X-AIMC-Actual-Capabilities` 是合法 JSON（解析后 list）

### 场景 3 — 精确路由 `qwen-plus` → 4 个响应头

**目的**：验证 hermes-agent-cn 发精确 model 名时，AIMC 响应头仍有 tier/capabilities（不需二次查 `/v1/models`）。

**实际响应头**：

```http
X-AIMC-Actual-Model: qwen-plus
X-AIMC-Actual-Channel: siliconflow_aggregator
X-AIMC-Actual-Tier: T1
X-AIMC-Actual-Capabilities: ["code", "general"]
```

**验证项**：
- ✅ 精确路由不触发 `X-AIMC-Group-Id`（无 group 概念）
- ✅ `X-AIMC-Actual-Tier=T1` 和 `Capabilities=["code","general"]` 来自 model_registry
- ✅ Phase 3.E 关键改动：`select_best_channel` 也注入 `_registry_*` 字段，精确路由也能告诉 agent tier/cap

### 场景 4 — Profile 校验（启动合法性）

**目的**：验证 hermes-agent-cn 启动时 validate profile，能用 `/v1/models` 的 `data_groups` 作为唯一可信源。

**模拟 hermes-agent-cn 配置**：

```yaml
models:
  default: tier:balanced
  think: tier:strong
  longContext: tier:flagship
  background: tier:light
```

**校验逻辑**（hermes-agent-cn 侧 PR 草案的 `AIMCClient.validate_profiles()`）：

```python
known = {g["id"] for g in response["data_groups"]}
for name, group_id in profile.items():
    if group_id not in known:
        raise StartupError(f"profile.{name}={group_id} 不在 AIMC data_groups 里")
```

**结果**：
- ✅ 4 个合法 profile 全部存在
- ✅ 拼写错误（如 `tier:nonexistent`）能被 fail-fast 检测
- ✅ AIMC `data_groups` 是 hermes-agent-cn 启动校验的**唯一可信源**（不依赖任何配置文件）

## 三、关键设计要点

### 1. 向后兼容

- OpenAI 客户端只看 `data[].id`，AIMC 扩展字段全在 `aimc_metadata` 嵌套对象里
- 不破坏任何现有 OpenAI SDK 行为

### 2. 数据来源单一可信

- AIMC 是 `data_groups` / `aimc_metadata` 的唯一生产者
- hermes-agent-cn 不读、不写、不缓存 AIMC 配置
- 模型组路由决策**只**听 DB + 情报层，**不听** hermes-agent-cn

### 3. 响应头污染保护

- Phase 2.10 E2E 发现的 bug：组路由注入 `_actual_model` 到共享 channel dict，后续精确路由命中**同一个 dict** 时残留
- Phase 3.E 修复：`select_best_channel` 返回前也写 `_actual_model`，确保 `_actual_model` 永远反映当前请求
- 测试 7（test_phase3e_headers.py）专门覆盖此场景

### 4. capabilities 双重转义保护

- DB 存的是 JSON 字符串（`"[\"reasoning\",\"code\"]"`）
- `_build_aimc_headers` 先 `json.loads` 解析，再 `json.dumps(ensure_ascii=False)` 重序列化
- 避免给 hermes-agent-cn 的 header 是 `"[\"reasoning\"]"` 这种双重转义
- 中文 capability 也能正常传输（测试 6）

## 四、测试覆盖

| 测试 | 文件 | 用例数 | 覆盖 |
|------|------|--------|------|
| 单元测试 | [tests/test_phase3e_headers.py](../tests/test_phase3e_headers.py) | 7 | `_build_aimc_headers` 7 个边界 case（含污染保护） |
| E2E | [tests/test_hermes_agent_cn_e2e.py](../tests/test_hermes_agent_cn_e2e.py) | 4 | hermes-agent-cn 集成 4 个核心场景 |

**跑法**：

```bash
# 单元测试 (无需 fastapi)
python tests/test_phase3e_headers.py

# E2E (in-process, mock cache, 无需 DB/uvicorn)
python tests/test_hermes_agent_cn_e2e.py

# 生产环境 HTTP E2E (需要 uvicorn 跑起来)
# 启动: uvicorn main:app --port 8080 &
# 跑: tests/test_phase2_e2e.py (Phase 2 已覆盖 HTTP 路径)
```

## 五、后续

- ✅ Phase 3.0 hermes-agent-cn 协同层全部完成
- ⬜ Phase 3.1 采集源扩展（半天）
- ⬜ Phase 3.2 AI 解析器升级（1-2 天，Phase 3 核心智能）
- ⬜ Phase 3.3 自动入组/出组（1 周）
- ⬜ Phase 3.4 价格联动（2 天）

**推荐下一步**：根据用户实际优先级选择（详见 [development-plan.md §Phase 3](development-plan.md#phase-3-情报联动模型组)）。