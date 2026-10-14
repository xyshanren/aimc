# AIMC 路由策略设计文档 (V5.0 设计稿)

> 状态: **设计稿，暂不执行**
> 日期: 2026-08-26
> 背景: V4.9.5 运行后对路由体系的全面梳理与改进设计

---

## 目录

1. [现状分析](#1-现状分析)
2. [问题诊断](#2-问题诊断)
3. [tier:vision-light 新组设计](#3-tiervision-light-新组设计)
4. [命名问题分析与方案](#4-命名问题分析与方案)
5. [路由策略全面设计](#5-路由策略全面设计)
6. [成员优先级激活方案](#6-成员优先级激活方案)
7. [执行清单（暂不执行）](#7-执行清单暂不执行)

---

## 1. 现状分析

### 1.1 组结构 (6 个组)

| 组 ID | display_name | tier 限定 | require_vision | strategy | fallback_group | sort_order |
|---|---|---|---|---|---|---|
| tier:flagship | 旗舰推理组 | NULL | 0 | cheapest | tier:strong | 10 |
| tier:strong | 主力通用组 | T1 | 0 | cheapest | tier:balanced | 20 |
| tier:balanced | 均衡性价比组 | T2 | 0 | cheapest | tier:light | 30 |
| tier:light | 轻量免费组 | T3 | 0 | cheapest | NULL | 40 |
| tier:vision | 多模态视觉组 | T1 | 1 | cheapest | tier:strong | 25 |
| tier:code | 代码专用组 | T2 | 0 | cheapest | tier:balanced | 50 |

**来源**: `scripts/seed_model_groups.py` GROUPS 列表 (第 55-84 行)

### 1.2 tier:vision 当前成员

| model_id | family | display_name | 注册表 tier | 注册表 modality | 上游价格 (in/out, 元/百万) | priority |
|---|---|---|---|---|---|---|
| minimax-m3 | minimax | MiniMax M3 | T1 | multimodal | 订阅制 | 100 |
| deepseek-v4-flash-vision-exp | deepseek | DeepSeek V4 Flash Vision Exp | T1 | vision | 实验免费 | 105 |
| qwen-vl-32b-instruct | qwen | Qwen3 VL 32B Instruct | T1 | vision | 1 / 4 | 110 |
| qwen-vl-32b-thinking | qwen | Qwen3 VL 32B Thinking | T1 | vision | 1 / 10 | 120 |
| qwen-vl-30b-a3b-thinking | qwen | Qwen3 VL 30B A3B Thinking | T1 | vision | 0.7 / 2.8 | 130 |
| qwen-vl-8b-instruct | qwen | Qwen3 VL 8B Instruct | **T3** | vision | 0.5 / 2 | 140 |
| glm-4.5v | glm | GLM-4.5V | T1 | vision | 1.5 / 4 | 150 |

**关键问题**: tier:vision 组 tier 限定为 T1，但 qwen-vl-8b-instruct 注册表 tier 为 T3。router.py:1300 的 tier 硬过滤会将其跳过——它永远无法被路由到，是"死成员"。

### 1.3 tier:light 当前成员

| model_id | family | 注册表 tier | priority |
|---|---|---|---|
| minimax-m2.7-highspeed | minimax | T3 | 100 |
| qwen-qwen3-8b | qwen | T3 | 110 |
| qwen-qwen3.5-4b | qwen | T3 | 120 |
| minimax-m2.5-highspeed | minimax | T3 | 130 |
| glm-4-flash | glm | T3 | 140 |

---

## 2. 问题诊断

### 2.1 tier 硬过滤杀死成员

`router.py:1299-1306`:

```python
if group.tier and registry.tier != group.tier:
    skipped += 1
    continue
```

**影响**: tier:vision (T1 限定) 中的 qwen-vl-8b-instruct (T3) 永远被跳过。它作为组成员存在但无法被路由。

### 2.2 成员优先级 (priority) 是死代码

`_member_priority_cache` 在 `router.py:87` 声明，但:

- `_load_groups_cache` (412-453) 加载成员时**不读取** `m.priority`
- 排序逻辑 (1412-1430) 使用的是 `channel.priority`，而非成员优先级
- `seed_model_groups.py:88` 注释说 "priority 数字越小越优先 (组内同价时 tie-break)"，但此功能从未实现

### 2.3 命名不一致

**问题 A (tier:vision)**: qwen-vl-* 模型缺少版本号 "3"。用户期望 `qwen3-vl-32b-instruct`，实际为 `qwen-vl-32b-instruct`。

根因: `db/normalizers.py:84` 的 `validate_model_id` 要求 `model_id.startswith(f"{family}-")`。family="qwen"，所以 model_id 必须以 `qwen-` 开头。`qwen3-vl-32b-instruct` 以 `qwen3-` 开头（不是 `qwen-`），验证不通过。

**问题 B (tier:light)**: qwen-qwen3-* 模型多出 family 前缀。用户期望 `qwen3-8b`，实际为 `qwen-qwen3-8b`。

根因: `sync_siliconflow_to_registry.py:137` 的 `convert_sf_model_id` 将上游 `Qwen/Qwen3-8B` 转为 `qwen-qwen3-8b`（先预置 family 前缀 `qwen-`，再去重 `qwen-qwen-` 但 `qwen-qwen3-` 不会被去重，因为去重逻辑只匹配 `qwen-qwen-`）。

### 2.4 max_cost_ratio 字段未启用

ModelGroupMember 表中有 `max_cost_ratio` 字段，但:

- 全部成员的 `max_cost_ratio` 均为 None
- router.py 排序逻辑中不读取此字段

### 2.5 qwen-vl-32b 系列能力定位

用户反馈: qwen-vl-32b 系列模型与 minimax-m3 和 deepseek-v4-flash-vision-exp 比起来明显差一档。当前它们都在 tier:vision (T1) 中，但实际能力不足以与 T1 旗舰视觉模型并列。

---

## 3. tier:vision-light 新组设计

### 3.1 设计目标

将明显弱一档的视觉模型从 tier:vision 中拆出，组成新的 tier:vision-light 组，使 tier:vision 保留真正的旗舰视觉模型。

### 3.2 成员调整

#### tier:vision (保留 3 个旗舰视觉模型)

| model_id | 注册表 tier | 上游价格 (in/out) | 说明 |
|---|---|---|---|
| minimax-m3 | T1 | 订阅制 | 多模态旗舰 |
| deepseek-v4-flash-vision-exp | T1 | 实验免费 | DeepSeek 视觉实验版 |
| glm-4.5v | T1 | 1.5 / 4 | 智谱视觉旗舰，保持原状 |

#### tier:vision-light (新增 4 个轻量视觉模型)

| model_id | 当前注册表 tier | 调整后注册表 tier | 上游价格 (in/out) | 说明 |
|---|---|---|---|---|
| qwen-vl-32b-instruct | T1 -> **T2** | T2 | 1 / 4 | 降档：能力不足 T1 |
| qwen-vl-32b-thinking | T1 -> **T2** | T2 | 1 / 10 | 降档：能力不足 T1 |
| qwen-vl-30b-a3b-thinking | T1 -> **T2** | T2 | 0.7 / 2.8 | 降档：能力不足 T1 |
| qwen-vl-8b-instruct | T3 (不变) | T3 | 0.5 / 2 | 本就是 T3 |

### 3.3 tier:vision-light 组配置

```
group_id:        tier:vision-light
display_name:    轻量视觉组
tier:            NULL          <- 关键: 不限 tier，兼容 T2 和 T3 成员
require_vision:  1
strategy:        cheapest
fallback_group:  tier:light    <- 视觉轻量组用完 -> 轻量文本组兜底
sort_order:      26            <- 紧跟 tier:vision (25)
```

**tier 设为 NULL 的理由**:

- 组内有 T2 (降档的 qwen-vl-32b 系列) 和 T3 (qwen-vl-8b-instruct) 两种 tier
- 如果设 tier=T2，qwen-vl-8b-instruct (T3) 又会变成死成员
- 如果设 tier=T3，降档的 qwen-vl-32b (T2) 又会被跳过
- NULL 不做 tier 过滤，所有成员都能参与路由，由 cheapest 策略自然排序

**fallback_group 设为 tier:light 的理由**:

- 视觉轻量组全部不可用时，降级到轻量文本组（虽然有损视觉能力，但至少有响应）
- 不设 fallback 为 tier:vision（避免循环回旗舰组）

### 3.4 tier:vision 的 fallback 调整

当前 tier:vision -> fallback -> tier:strong (文本组)。

设计调整为: tier:vision -> fallback -> **tier:vision-light** -> fallback -> tier:strong

这样视觉请求的降级链为:

```
tier:vision (旗舰视觉) -> tier:vision-light (轻量视觉) -> tier:strong (主力文本) -> ...
```

### 3.5 调整后完整组结构

| 组 ID | tier 限定 | require_vision | strategy | fallback_group | sort_order |
|---|---|---|---|---|---|
| tier:flagship | NULL | 0 | cheapest | tier:strong | 10 |
| tier:strong | T1 | 0 | cheapest | tier:balanced | 20 |
| tier:vision | T1 | 1 | cheapest | tier:vision-light | 25 |
| tier:vision-light | NULL | 1 | cheapest | tier:light | 26 |
| tier:balanced | T2 | 0 | cheapest | tier:light | 30 |
| tier:light | T3 | 0 | cheapest | NULL | 40 |
| tier:code | T2 | 0 | cheapest | tier:balanced | 50 |

---

## 4. 命名问题分析与方案

### 4.1 问题根源: validate_model_id

`db/normalizers.py:61-87`:

```python
def validate_model_id(model_id: str, family: str) -> bool:
    if not model_id.startswith(f"{family}-"):
        return False
    return bool(_MODEL_ID_RE.match(model_id))
```

- family="qwen" 时，model_id 必须以 `qwen-` 开头
- `qwen3-vl-32b-instruct` 以 `qwen3-` 开头 -> 不通过
- 权宜之计: 去掉版本号 "3"，用 `qwen-vl-32b-instruct`

### 4.2 问题根源: convert_sf_model_id

`sync_siliconflow_to_registry.py:124-143`:

- 上游 `Qwen/Qwen3-8B` -> family="qwen", model_part="qwen3-8b" -> new_id="qwen-qwen3-8b"
- 去重逻辑只匹配 `qwen-qwen-` (prefix=`qwen-qwen-`)，不匹配 `qwen-qwen3-`
- 所以 `qwen-qwen3-8b` 不会被去重，保留双重前缀

### 4.3 四种方案

#### 方案 A: 放宽 validate_model_id（推荐）

修改验证器，允许 model_id 以 `{family}` 开头后跟数字再跟横杠:

```python
import re

def validate_model_id(model_id: str, family: str) -> bool:
    if not model_id or not family:
        return False
    pattern = rf"^{re.escape(family)}(?:\d+(?:\.\d+)?)?-"
    if not re.match(pattern, model_id):
        return False
    return bool(_MODEL_ID_RE.match(model_id))
```

这样 `qwen3-vl-32b-instruct` (family="qwen") 通过，`qwen-vl-32b-instruct` 也通过，`deepseek-v4-pro` (family="deepseek") 也通过。

**优点**: 彻底解决命名约束，model_id 可以包含版本号
**缺点**: 需要回归测试所有 validate_model_id 调用路径；需要迁移已 seed 的 model_id

#### 方案 B: 统一为自动同步命名

放弃手工 seed 的 `qwen-vl-*` 前缀，统一使用 `convert_sf_model_id` 生成的命名。同时修复 `convert_sf_model_id` 的去重逻辑:

```python
# 修复: 去重 "{family}-" 前缀时，也匹配 "{family}" 后跟数字的情况
prefix = f"{family}-"
if new_id.startswith(prefix):
    rest = new_id[len(prefix):]
    if rest.startswith(family):
        new_id = rest
```

这样 `qwen-qwen3-vl-32b-instruct` -> `qwen3-vl-32b-instruct`。

但这与 validate_model_id 冲突（`qwen3-vl-*` 不以 `qwen-` 开头），所以仍需方案 A 的验证器放宽。

**结论**: 方案 B 依赖方案 A。

#### 方案 C: 保持现状，仅在 display_name 中保留版本信息

当前做法。model_id 用 `qwen-vl-32b-instruct`，display_name 用 `Qwen3 VL 32B Instruct`。

**优点**: 零改动，无风险
**缺点**: model_id 丢失版本信息，与上游名称不一致，模糊匹配依赖 _get_fuzzy_price

#### 方案 D: 更改 family

将 Qwen3 VL 系列的 family 从 "qwen" 改为 "qwen3"。这样 `qwen3-vl-32b-instruct` 以 `qwen3-` 开头，通过验证。

**优点**: 简单直接
**缺点**: 破坏 family 聚合语义（同厂商不同代际模型应共享 family）；影响 prefer_family 逻辑

### 4.4 推荐方案

**短期 (V5.0)**: 方案 C（保持现状）。命名问题不影响路由功能，_get_fuzzy_price 能正确匹配价格。

**中期 (V5.1+)**: 方案 A（放宽验证器）+ 方案 B（修复 convert_sf_model_id），一次性迁移所有 model_id 到带版本号的规范命名。

---

## 5. 路由策略全面设计

### 5.1 策略选项

| 策略 | 排序键 | 适用场景 |
|---|---|---|
| cheapest (默认) | (price, family_bonus, -channel.priority) | 成本优先，同价时 prefer_family 优先 |
| priority | channel.priority DESC | 通道优先级优先，忽略价格 |

### 5.2 cheapest 策略排序详解

```python
def sort_key(item):
    ch, _model_id, price, family_bonus, _ch_model = item
    billing = ch.get("billing_mode", "pay_per_token")
    if billing in ("subscription", "free"):
        return (0.0, family_bonus, -ch.get("priority", 0))
    return (price, family_bonus, -ch.get("priority", 0))
```

排序键三元组: `(input_price, family_bonus, -channel_priority)`

1. **input_price**: 订阅/免费模型视为 0，永远最便宜
2. **family_bonus**: prefer_family 匹配时为 0（优先），否则为 1（兜底）
3. **-channel_priority**: channel.priority 越大越优先（取负号后越小越优先）

### 5.3 设计改进: 引入成员优先级作为第四排序键

当前排序三元组的最后一项是 `-channel.priority`，这是通道优先级，不是成员优先级。设计增加第四键:

```python
def sort_key(item):
    ch, _model_id, price, family_bonus, member_priority, _ch_model = item
    billing = ch.get("billing_mode", "pay_per_token")
    if billing in ("subscription", "free"):
        return (0.0, family_bonus, -ch.get("priority", 0), member_priority)
    return (price, family_bonus, -ch.get("priority", 0), member_priority)
```

这样在价格、family 偏好、通道优先级都相同时，成员 priority 数字越小越优先（与 seed_model_groups.py 注释一致）。

### 5.4 max_cost_ratio 设计（未来）

ModelGroupMember.max_cost_ratio 字段可用于限制成员的最高成本比例:

```python
# 伪代码: 如果组内最便宜成员价格为 X，max_cost_ratio=2.0 的成员价格不超过 2X
if max_cost_ratio and cheapest_price and price > cheapest_price * max_cost_ratio:
    skipped += 1
    continue
```

V5.0 暂不启用，保留字段供未来使用。

### 5.5 完整 fallback 链

```
tier:flagship -> tier:strong -> tier:balanced -> tier:light -> NULL
tier:vision -> tier:vision-light -> tier:light -> NULL
tier:code -> tier:balanced -> tier:light -> NULL
```

### 5.6 P0-3 规则 (纯文本跳过 multimodal)

当请求不含 image/audio/file 时，跳过 modality=multimodal 的成员:

```python
has_mm = _has_multimodal_content(messages)
if not has_mm and registry.modality == "multimodal":
    skipped += 1
    continue
```

**影响**: minimax-m3 (modality=multimodal) 在纯文本请求时会被跳过。在 tier:vision 和 tier:strong 中都受此规则约束。

### 5.7 require_vision 过滤

require_vision=1 的组只接受 modality 为 vision 或 multimodal 的成员:

```python
if group.require_vision and registry.modality not in ("vision", "multimodal"):
    skipped += 1
    continue
```

---

## 6. 成员优先级激活方案

### 6.1 当前问题

`_load_groups_cache` 只读取 `m.group_id` 和 `m.model_id`，不读取 `m.priority`:

```python
# 当前 (router.py:447-449)
self._group_members_cache = {}
for m in mem_result.scalars().all():
    self._group_members_cache.setdefault(m.group_id, []).append(m.model_id)
```

### 6.2 改进设计

```python
# 改进: 同时读取 priority，存入 _member_priority_cache
self._group_members_cache = {}
self._member_priority_cache = {}  # {(group_id, model_id): priority}
for m in mem_result.scalars().all():
    self._group_members_cache.setdefault(m.group_id, []).append(m.model_id)
    self._member_priority_cache[(m.group_id, m.model_id)] = m.priority or 100
```

在候选收集阶段读取:

```python
member_priority = self._member_priority_cache.get((current, model_id), 100)
candidates.append((best, model_id, input_price, family_bonus, member_priority, best.get("_channel_model")))
```

在排序阶段使用 (见 5.3):

```python
candidates.sort(key=lambda x: (..., x[4]))  # member_priority 作为末位 tie-break
```

### 6.3 priority 策略也纳入成员优先级

```python
if strategy == "priority":
    candidates.sort(
        key=lambda x: (-x[0].get("priority", 0), x[4])  # channel.priority DESC, member.priority ASC
    )
```

---

## 7. 执行清单（暂不执行）

> **以下所有变更均为设计稿，暂不执行。**

### 7.1 seed_model_groups.py 改动

1. GROUPS 列表新增 tier:vision-light 组
2. tier:vision 的 fallback_group 改为 tier:vision-light
3. GROUP_MEMBERS 中 qwen-vl-32b 系列 3 个模型从 tier:vision 移到 tier:vision-light
4. GROUP_MEMBERS 中 qwen-vl-8b-instruct 从 tier:vision 移到 tier:vision-light

### 7.2 seed_model_registry.py 改动

1. qwen-vl-32b-instruct: tier T1 -> T2
2. qwen-vl-32b-thinking: tier T1 -> T2
3. qwen-vl-30b-a3b-thinking: tier T1 -> T2
4. qwen-vl-8b-instruct: tier T3 (不变)

### 7.3 router.py 改动（可选，V5.1+）

1. `_load_groups_cache` 填充 `_member_priority_cache`
2. 候选收集阶段读取成员优先级
3. 排序逻辑增加成员优先级作为 tie-break
4. (可选) 启用 max_cost_ratio 过滤

### 7.4 normalizers.py 改动（可选，V5.1+）

1. 放宽 validate_model_id 允许 `{family}<数字>-` 前缀
2. 修复 convert_sf_model_id 去重逻辑

### 7.5 执行顺序

```
Phase 1 (V5.0): seed_model_groups.py + seed_model_registry.py -> reseed
Phase 2 (V5.1): router.py 成员优先级激活
Phase 3 (V5.2): normalizers.py 命名规范化 + model_id 迁移
```

---

## 附录 A: 完整成员表（V5.0 设计后）

### tier:vision (3 个旗舰视觉模型)

| model_id | 注册表 tier | priority | 上游价格 (in/out) |
|---|---|---|---|
| minimax-m3 | T1 | 100 | 订阅制 |
| deepseek-v4-flash-vision-exp | T1 | 105 | 实验免费 |
| glm-4.5v | T1 | 150 | 1.5 / 4 |

### tier:vision-light (4 个轻量视觉模型)

| model_id | 注册表 tier | priority | 上游价格 (in/out) |
|---|---|---|---|
| qwen-vl-32b-instruct | T2 | 110 | 1 / 4 |
| qwen-vl-32b-thinking | T2 | 120 | 1 / 10 |
| qwen-vl-30b-a3b-thinking | T2 | 130 | 0.7 / 2.8 |
| qwen-vl-8b-instruct | T3 | 140 | 0.5 / 2 |

### tier:flagship (2 个)

| model_id | 注册表 tier | priority |
|---|---|---|
| deepseek-v4-pro | T0 | 100 |
| glm-5.2 | T1 | 110 |

### tier:strong (3 个)

| model_id | 注册表 tier | priority |
|---|---|---|
| deepseek-v4-flash | T1 | 100 |
| minimax-m3 | T1 | 110 |
| glm-4-plus | T1 | 120 |

### tier:balanced (3 个)

| model_id | 注册表 tier | priority |
|---|---|---|
| deepseek-v3 | T2 | 100 |
| minimax-m2.5 | T2 | 110 |
| sensenova-6.8-flash-lite | T2 | 120 |

### tier:light (5 个)

| model_id | 注册表 tier | priority |
|---|---|---|
| minimax-m2.7-highspeed | T3 | 100 |
| qwen-qwen3-8b | T3 | 110 |
| qwen-qwen3.5-4b | T3 | 120 |
| minimax-m2.5-highspeed | T3 | 130 |
| glm-4-flash | T3 | 140 |

### tier:code (3 个)

| model_id | 注册表 tier | priority |
|---|---|---|
| minimax-m2.7 | T1 | 100 |
| deepseek-v4-flash | T1 | 110 |
| deepseek-v3.2 | T2 | 120 |

---

## 附录 B: Fallback 链图

```
tier:flagship (T0/T1 旗舰推理)
  -> tier:strong (T1 主力通用)
       -> tier:balanced (T2 性价比)
            -> tier:light (T3 轻量免费)
                 -> NULL

tier:vision (T1 旗舰视觉)
  -> tier:vision-light (T2/T3 轻量视觉)
       -> tier:light (T3 轻量免费)
            -> NULL

tier:code (T2 代码专用)
  -> tier:balanced (T2 性价比)
       -> tier:light (T3 轻量免费)
            -> NULL
```

---

## 附录 C: 命名问题对照表

| 上游名称 (siliconflow) | 当前 model_id | 期望 model_id | 命名问题 | 根因 |
|---|---|---|---|---|
| Qwen/Qwen3-VL-32B-Instruct | qwen-vl-32b-instruct | qwen3-vl-32b-instruct | 缺少版本号 3 | validate_model_id 要求 qwen- 前缀 |
| Qwen/Qwen3-VL-32B-Thinking | qwen-vl-32b-thinking | qwen3-vl-32b-thinking | 同上 | 同上 |
| Qwen/Qwen3-VL-30B-A3B-Thinking | qwen-vl-30b-a3b-thinking | qwen3-vl-30b-a3b-thinking | 同上 | 同上 |
| Qwen/Qwen3-VL-8B-Instruct | qwen-vl-8b-instruct | qwen3-vl-8b-instruct | 同上 | 同上 |
| Qwen/Qwen3-8B | qwen-qwen3-8b | qwen3-8b | 多出 family 前缀 | convert_sf_model_id 去重不完整 |
| Qwen/Qwen3.5-4B | qwen-qwen3.5-4b | qwen3.5-4b | 同上 | 同上 |
| zai-org/GLM-4.5V | glm-4.5v | glm-4.5v | 无问题 | 去重正确 (glm-glm- -> glm-) |

---

*文档结束*
