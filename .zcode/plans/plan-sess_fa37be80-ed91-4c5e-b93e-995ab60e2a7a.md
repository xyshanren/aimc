# SenseNova 模型接入计划

## 背景

用户接受推荐：
1. 将 `sensenova-6.8-flash-lite` 加入 model_registry + tier:balanced 组 (modality=text)
2. 将 `glm-5.2` 加入 sensenova_official 的 price_snapshots（让 glm-5.2 获得免费第二渠道）
3. 图片生成模型 (sensenova-u1-fast, sensenova-u1.5-lite) 记为远期候选，本次不实现
4. sensenova-6.7-flash-lite 不加入

## 关键发现（影响设计）

1. **raw_ids 未实现**：`ModelRegistry.raw_ids` 列存在但 runtime 代码从不读取。proxy 实际通过 `_channel_model`（来自 router 的 price_snapshot 精确/模糊匹配）决定上游 model name。
2. **glm-5.2 当前通过模糊匹配工作**：siliconflow 的 price_snapshot model 字段是 `zai-org/GLM-5.2`，router 的 `_get_fuzzy_price` 把 `glm-5.2` 模糊匹配到它，proxy 再 rewrite body.model。
3. **glm-5.2 加 sensenova price_snapshot**：sensenova 上游直接接受 `glm-5.2` 作为 model name（官方文档确认），所以 price_snapshot model 字段直接写 `glm-5.2`，router 精确匹配命中，无需模糊匹配。但 sensenova 渠道 billing_mode=`pay_per_token`（非 free），price=0.0，在 cheapest 策略下与 siliconflow 的 price=8.0 竞争时，sensenova 会赢（0.0 < 8.0）。这是期望行为——免费的 sensenova 优先于付费的 siliconflow。
4. **sensenova-6.8-flash-lite modality=text**：用户确认。它能接受纯文本（API 确认），设 text 避免 P0-3 跳过。加入 tier:balanced (T2)，不加入 tier:vision (require_vision 拒绝 text modality)。
5. **seed_model_groups.py 软校验**：如果 model_id 不在 model_registry 中，seed 脚本会 warning + skip。所以 sensenova-6.8-flash-lite 必须先在 registry 中才能加入 group。seed 脚本做 delete-then-insert，sensenova-6.8-flash-lite 在 seed_model_registry.py 中加入后会自动被 seed_model_groups.py 软校验通过。
6. **billing_mode**: sensenova_official 渠道当前 billing_mode=`pay_per_token`（默认值，backfill 脚本未特殊处理）。price=0.0 在 cheapest 策略下排在最前（0.0 < 任何正价）。这无需改动。

## 改动清单（4 个文件）

### 1. `scripts/seed_model_registry.py` — 加 sensenova family

在 Llama 段之前（line 262 后），新增 SenseNova 段：

```python
    # ========== SenseNova (1 个, 商汤日日新公测) ==========
    # sensenova-6.8-flash-lite: 256K ctx, 支持 tools/json_mode/vision input
    # API 接受纯文本, modality=text 避免 P0-3 纯文本跳过
    # price=0.0 (公测免费), sensenova_official 渠道常驻
    (
        "sensenova-6.8-flash-lite", "sensenova", "SenseNova 6.8 Flash Lite", "T2",
        ["reasoning", "general"], 262144, "text", 0, "2026-08",
        "商汤日日新公测, 256K ctx, 支持 tools/vision, price=0.0 (公测免费)",
    ),
```

tier=T2（轻量多模态 agent 模型，非旗舰），context=262144（官方文档值），modality=text（用户确认，避免 P0-3 跳过），capabilities=["reasoning","general"]（支持 tools 但不是 code 专用）。

不加入 sensenova-6.7-flash-lite（8/31 后失效）、sensenova-u1-fast（图片生成，非 chat API）、sensenova-u1.5-lite（同上）。

### 2. `scripts/seed_model_groups.py` — 加 tier:balanced 成员

在 tier:balanced 组成员列表（line 104-105 后）加一条：

```python
    # ========== tier:balanced (T2 性价比) ==========
    # deepseek-v3 (T2, 硅基流动常驻) + minimax-m2.5 (T2)
    # + sensenova-6.8-flash-lite (T2, 公测免费, 256K ctx)
    ("tier:balanced", "deepseek-v3",  100, 1.0, None, "manual"),
    ("tier:balanced", "minimax-m2.5", 110, 1.0, None, "manual"),
    ("tier:balanced", "sensenova-6.8-flash-lite", 120, 1.0, None, "manual"),
```

priority=120（最低优先级，但 cheapest 策略下 price=0.0 会让它排第一——这是期望行为：免费的 sensenova 优先于付费的 deepseek-v3/minimax-m2.5）。member priority 在 cheapest 策略中不参与排序（只有 price/family_bonus/channel_priority 参与），所以 priority=120 只影响同价时的 tie-break（实际上不会发生，因为其他模型都有正价）。

### 3. `scripts/manual_import.py` — sensenova prices 加 glm-5.2

在 sensenova 渠道的 prices 列表（line 257-262）末尾加一条：

```python
        "prices": [
            ("sensenova-6.8-flash-lite", "normal", "全天", 0.0, 0.0),  # 主力
            ("sensenova-u1-fast", "normal", "全天", 0.0, 0.0),  # 加速版
            ("deepseek-v4-flash", "normal", "全天", 0.0, 0.0),  # 公测免费 (跨渠道)
            ("sensenova-6.7-flash-lite", "normal", "全天", 0.0, 0.0),  # 8/31 后失效, 保留兼容
            ("glm-5.2", "normal", "全天", 0.0, 0.0),  # 公测免费 (跨渠道, 免费第二渠道)
        ],
```

同时更新注释块（line 244-249）加一行 glm-5.2 说明。

### 4. `scripts/fix_sensenova_models.py` — MODELS_TO_ADD 加 glm-5.2

在 MODELS_TO_ADD 列表（line 64-71）加一条：

```python
MODELS_TO_ADD = [
    # (model, price_type, time_range, input_price, output_price)
    ("sensenova-6.8-flash-lite", "normal", "全天", 0.0, 0.0),
    ("sensenova-u1-fast",        "normal", "全天", 0.0, 0.0),
    ("deepseek-v4-flash",        "normal", "全天", 0.0, 0.0),
    ("glm-5.2",                  "normal", "全天", 0.0, 0.0),  # 公测免费 (跨渠道)
    # 6.7 保留 (8/31 前仍可用)
    # ("sensenova-6.7-flash-lite", "normal", "全天", 0.0, 0.0),
]
```

### 5. 新建 `db/migrations/2026_08_25_add_sensenova_models.py` — VM 一次性迁移

VM 上不能直接跑 seed 脚本（会 delete-then-insert 丢失 siliconflow sync 的 91 个模型）。用迁移脚本精确操作：

1. **INSERT sensenova-6.8-flash-lite 到 model_registry**（如果不存在）
2. **INSERT sensenova-6.8-flash-lite 到 model_groups_members**（tier:balanced 组，priority=120）
3. **INSERT glm-5.2 price_snapshot 到 sensenova_official 渠道**（如果不存在）
4. **INSERT sensenova-6.8-flash-lite price_snapshot 到 sensenova_official 渠道**（如果不存在——fix_sensenova_models.py 可能已经加过了，做幂等检查）

遵循已有迁移模式（dry-run / --apply / sqlite3 直连 / BEGIN-COMMIT 事务）。

## 执行顺序

1. 改 4 个源文件（seed_model_registry.py, seed_model_groups.py, manual_import.py, fix_sensenova_models.py）
2. 新建迁移脚本 `db/migrations/2026_08_25_add_sensenova_models.py`
3. 本地跑测试确认无回归
4. 提交 + 推送
5. 在 VM 上更新代码
6. 在 VM 上跑迁移脚本 `--apply`
7. 重启服务
8. 验证：
   - `tier:balanced` 请求 → sensenova-6.8-flash-lite (price=0.0 最便宜)
   - `tier:flagship` 请求 → glm-5.2 经 sensenova_official 渠道 (price=0.0 < siliconflow 8.0)
   - exclude sensenova → tier:balanced 走 deepseek-v3/minimax-m2.5; tier:flagship 走 siliconflow glm-5.2

## 验证命令

```bash
# 1. 确认 registry
sqlite3 aimc.db "SELECT model_id, family, tier, modality FROM model_registry WHERE model_id='sensenova-6.8-flash-lite';"

# 2. 确认 group member
sqlite3 aimc.db "SELECT * FROM model_group_members WHERE model_id='sensenova-6.8-flash-lite';"

# 3. 确认 glm-5.2 price_snapshot on sensenova
sqlite3 aimc.db "SELECT channel_id, model, input_price FROM price_snapshots WHERE model='glm-5.2';"

# 4. 发 tier:balanced 请求, 检查 usage_logs
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $AIMC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"tier:balanced","messages":[{"role":"user","content":"ping"}]}'
# 期望: sensenova-6.8-flash-lite via sensenova_official

# 5. 发 tier:flagship 请求
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $AIMC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"tier:flagship","messages":[{"role":"user","content":"ping"}]}'
# 期望: glm-5.2 via sensenova_official (price=0.0 胜过 siliconflow 8.0)
```
