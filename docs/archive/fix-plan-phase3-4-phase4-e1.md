# AIMC release/phase3-4-phase4-e1 修复实施计划

> 配套：[review-phase3-4-phase4-e1.md](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/docs/review-phase3-4-phase4-e1.md)（首轮评审报告 + 修订与回应）
> 制定日期：2026-08-07
> 工作分支：`release/phase3-4-phase4-e1`（就地修复，不开新分支）
> 目标 commit 数：6-7 个（按 6 个主题拆批，避免巨型 commit）

---


## 📦 历史归档说明

> **本文档已归档**（2026-08-15 文档重组时迁移到 docs/archive/）。
> 新内容见 [docs/index.md](../index.md) 或相关 [docs/dev/](../dev/) / [docs/user/](../user/) 文档。

## 一、目标与边界

**目标**：修复首轮评审发现的关键缺陷，使 Phase 3.3 "情报联动模型组" 真正工作，Dashboard 可用，E1/E3 决策真正落地。

**边界**：
- ✅ 本计划只覆盖 **9 项 must-fix + 顺手修的 [B] 类 P2**
- ❌ 不修 I5/I6/I8 (N+1 查询、缓存粒度) — 下个迭代
- ❌ 不修 P2-7/P2-19~P2-24 (a11y / 响应式 / polish) — 下个迭代
- ❌ 不动 architecture.md 文档一致性（除了 P0-2 注释同步外）

---

## 二、修复批次（6 个 commit）

按"先解锁 Dashboard、再统一语义、再补安全阀、再补决策落地"分批：

### Commit 1: 🔴 fix(dashboard): SRI 哈希 + vendor 自托管

| 修复项 | P0-6 |
|--------|------|
| 影响面 | dashboard/static/ |
| 工作量 | 0.5h |

**改动**：
- 把 `htmx.org@1.9.12` 和 `chart.js@4.4.0` 下载到 `dashboard/static/vendor/`
- index.html:8-9 改用本地路径 + 移除 `integrity` 属性（自托管后可去掉 SRI）
- 测试：浏览器打开 `http://localhost:8080/dashboard/`，6 个 section 不再卡在"加载中"

**验证**：
- 手动：浏览器打开 dashboard，所有 hx-get section 正常渲染
- 自动化：暂无（前端手工验证）

**回归风险**：无（纯前端静态资源）

---

### Commit 2: 🔴 fix(intel): 统一 Tier 语义 (T0=旗舰)

| 修复项 | P0-2 + P2-2（连带） |
|--------|---------------------|
| 影响面 | intelligence/parser.py, intelligence/cross_source_validator.py, tests/ |
| 工作量 | 2h |

**改动**：
- `parser.py:73` SYSTEM_PROMPT 改 "T0 旗舰前沿 → T4 微型/嵌入"
- `parser.py:99-112` `infer_tier` size→tier 映射反转：
  - `size >= 200` → T0 (旗舰)
  - `size >= 70` → T1
  - `size >= 20` → T2
  - `size >= 7` → T3
  - else → T4 (微型)
- `cross_source_validator.py:31-32` `_TIER_ORDER` 注释改成 "T0 最高，T4 最低"
- `tests/test_phase3_2_parser.py` mock 数据同步（tier:flagship 期望 T0）
- `tests/test_phase3_3_model_sync.py` mock tier 期望同步

**验证**：
- 单元测试：`tests/test_phase3_2_parser.py` 全过
- 单元测试：`tests/test_phase3_3_model_sync.py` 全过
- 手工：跑一次 healer，看 promotions.raw_data 里的 tier 是 T0/T1/T2/T3 符合预期

**回归风险**：高（tier 语义变 → 所有 AI 推断结果变）
- 缓解：seed_model_registry.py 已经按 T0=旗舰 标注，与新 parser 一致
- 缓解：model_sync 永远写 log approved=0，需要 dashboard 人工确认才生效

---

### Commit 3: 🔴 fix(intel): 接通 process_promotion + 出组安全阀 + 审计完整

| 修复项 | P0-1 + P0-4 + P0-5 + P1-4 |
|--------|---------------------------|
| 影响面 | intelligence/healer.py, intelligence/model_sync.py |
| 工作量 | 2h |

**改动**：

**P0-1**：`healer.py:_save_promotion` commit 后，若 `raw_data._intelligence_kind == "model_metadata"`，调用 `await model_sync.process_promotion(promo_id, raw_data)`。注意：
- 用新 session（避免与现有事务嵌套）
- 调用失败只 logger.warning，不影响 _save_promotion 主流程

**P0-4**：在 model_sync.py 出组分支末尾无条件 `return SyncResult(...)` 避免 fallthrough

**P0-5**：`model_sync.py:175-178` 不再直接 mutate existing.context_length / existing.modality，全部走 `log_capability_change` 等待人工批准

**P1-4**：重复入组时，仅当 `existing.tier != item.inferred_tier` 时才写 tier log

**验证**：
- 单元测试：`tests/test_phase3_3_model_sync.py` 全过（已有 deprecated + log 路径覆盖，需补出组 early return 用例）
- 手动 e2e：跑 healer → 检查 model_capabilities_log 行数（应不再有重复 tier log）

**回归风险**：中
- P0-1 接入会触发历史堆积的 promotions 一次性消费，可能日志爆炸 — 缓解：先 dry-run，看有多少 pending promotion 准备好清理

---

### Commit 4: 🟡 fix(intel): 占位 tier="T2" 修复 + E3 UNIQUE 约束

| 修复项 | P0-3 + P0-7 |
|--------|--------------|
| 影响面 | intelligence/model_sync.py, db/models.py, db/crud.py |
| 工作量 | 1h |

**改动**：

**P0-3**：`model_sync.py:143` 注释明确化：
- 当前 tier="T2" 是占位，仅在 tier:balanced (group.tier=T2) 组中能命中路由
- 改方案：自动入组只对 **tier:flagship** (tier=NULL 兜底) 和 scene: 组生效
- tier:strong/balanced/light 三组的实际命中 → 等 dashboard approve tier log 后才走
- 修改 `_find_target_group`：如果 inferred_tier 不是 T0，不进 tier 组；只入 flagship (group_id 写死 `"tier:flagship"`) 或 scene: 组

**P0-7**：`db/models.py:347` 把 `Index("idx_cross_source_status", ...)` 升级为 `UniqueConstraint("model_id", "field_changed", "status", name="uq_cross_source_pending")`（partial unique on status=pending 在 SQLite 需 workaround — 用 status 单独走 CHECK 或允许多 resolved/ignored）

实际上 SQLite 的 UNIQUE 约束是全字段唯一，不支持 partial。**推荐方案**：
- 把 status 拆为 status_pending (0/1) + resolved_status ("pending"/"resolved"/"ignored")，pending 唯一
- 或：用 `INSERT...ON CONFLICT(model_id, field_changed) WHERE status='pending' DO UPDATE`（SQLite 3.24+ 支持）
- 或：业务层加 SELECT FOR UPDATE + INSERT

我倾向**业务层 upsert 已实现的 check-then-update，叠加 SELECT 后立刻 INSERT 失败就吞掉**（cross_source_validator 已有 try/except），UNIQUE 约束加 status 列即可，schema 简化。

**改动**：
- `models.py:347` 加 `UniqueConstraint("model_id", "field_changed", "status", name="uq_cross_source_key")`
- `cross_source_validator.py:194-198` except 改成 `except IntegrityError` 并 fallback 到 UPDATE

**验证**：
- 单元测试：`tests/test_phase4_e3_cross_source.py` 全过
- 手动：手动跑 `run-detection` 一次 + scheduler 自动跑一次，无重复 pending

**回归风险**：中
- P0-3 改了自动入组逻辑，phase 3.3 测试要补旗舰组入组 + 其它 tier 走 log 不入组的断言

---

### Commit 5: 🟡 fix(dashboard): E1/E3 决策落地 (刷缓存 + 迁移组 + resolve 回流)

| 修复项 | P1-1 + P1-10 + P1-7 |
|--------|---------------------|
| 影响面 | dashboard/routes.py, db/crud.py |
| 工作量 | 2h |

**改动**：

**P1-1 (E1 approve 落地)**：
- `crud.approve_capability_change`：当 `field_changed=="tier"` 时，额外做：
  - 从所有 `model_group_members` 中 `DELETE WHERE model_id = log.model_id`
  - 找到新 tier 对应的 group (`group.tier == log.new_value` 或 `tier=NULL`)
  - INSERT into model_group_members (新 group)
- `dashboard/routes.py:approve_pending_review` 在 commit 后调 `await route_engine.refresh_cache()`

**P1-10 (E3 resolve 回流)**：
- `dashboard/routes.py:resolve_cross_source_discrepancy` 在 commit 后：
  - 调 `await route_engine.refresh_cache()`
  - 若 `discrepancy.field_changed in ("tier", "capabilities", "context_length", "modality")` 且 `resolved_value` 非空：
    - 写一条 `model_capabilities_log` (field_changed=field_changed, old_value=current, new_value=resolved_value, source="human_resolve", confidence=1.0, approved=1)
    - 更新 `model_registry` 对应字段（tier 永久立即生效，其它也立即生效，因人工已拍板）

**P1-7 (log 输出顺序)**：
- `crud.py:483-485` 先 `old_tier = registry.tier`，再 `registry.tier = log.new_value`，再 logger
- 同样套路应用到 E3 resolve

**验证**：
- 单元测试：`tests/test_phase4_e1_pending_review.py` 补迁移组断言
- 单元测试：`tests/test_phase4_e3_cross_source.py` 补 resolve 回流断言
- 手动 e2e：dashboard approve tier log → 立即发 `/v1/chat/completions` 请求 → 响应头 X-AIMC-Actual-Tier 已是新值

**回归风险**：中
- P1-1 approve 调用 refresh_cache 是同步操作，dashboard 响应变慢
- 缓解：refresh_cache 本身是异步加载数据，UI 上加 spinner

---

### Commit 6: 🟡 fix(intel): E2/E3 鲁棒性 + 4 个小修

| 修复项 | P1-8 + P1-9 + P1-11 + P1-12 + P2-15 + P2-16 + P2-13 + P2-17 |
|--------|-----------------------------------------------------------------|
| 影响面 | intelligence/price_alert.py, intelligence/cross_source_validator.py, dashboard/routes.py |
| 工作量 | 1.5h |

**改动**：

**P1-8**：`price_alert.py:8` docstring + 代码一致用 `price_{provider}_{model}`
- `pricing_sync.py` `PriceChange` 加 `provider` 字段（dataclass 扩展）
- `price_alert.py:125` throttle_key 改为 `f"price_{change.provider}_{change.model}"`
- `price_alert.py:121` 消息加 `渠道: {change.provider}`

**P1-11**：`cross_source_validator.py:194-198` except 内加 `await session.rollback()`

**P1-12**：`dashboard/routes.py:673` 改 `if resolved_value is None`；ignored=True 时允许空

**P2-15**：`price_alert.py:59` 改成 `alerter.get_runtime_override("price_alert_threshold")`（新增 public accessor）

**P2-16**：`price_alert.py:63` float() 失败时 logger.warning + 走默认值

**P2-13**：`cross_source_validator.py:_group_models_by_id` 去掉 async 装饰

**P2-17**：`db/crud.py:551` 去掉 ORM `updated_at = func.now()`（已有 onupdate）

**验证**：
- 单元测试：`tests/test_phase4_e2_price_alert.py` 加 provider 字段用例
- 单元测试：`tests/test_phase4_e3_cross_source.py` 加 rollback 用例 + resolve API 边界用例

**回归风险**：低
- P1-8 加 provider 字段，PriceChange 是 datacaclass，初始化要更新所有测试 mock

---

## 三、测试盲区补救（无独立 commit，融入上述各 commit）

每个 commit 在改业务代码的同时，补一个 e2e 集成测试（mock healer + 真 DB + 真 route_engine）：

```
tests/test_e2e_intel_to_route.py
- Test 1: AI 推断新 model tier=T0 → process_promotion → route 命中 (旗舰组)
- Test 2: AI 推断 model status=deprecated conf=0.95 → 不入组，registry 标记 deprecated
- Test 3: dashboard approve tier log → 立即 route 命中新 tier 组
- Test 4: AI 推断 tier 未变 → 不写重复 tier log
- Test 5: 跨源 tier 分歧 → E3 detect → resolve → registry 更新 + 路由刷新
```

这个测试用 in-memory SQLite + 临时 route_engine，不依赖 playwright。

---

## 四、commit 节奏

| # | 标题 | 预计工作量 | 验证 | push |
|---|------|----------|------|------|
| 1 | fix(dashboard): vendor 自托管 htmx/chart.js + 移除假 SRI 哈希 | 0.5h | 浏览器打开 dashboard | ✅ push |
| 2 | fix(intel): Tier 语义统一 (T0=旗舰) — parser + cross_source_validator + tests | 2h | test_phase3_2 + test_phase3_3 全过 | ✅ push |
| 3 | fix(intel): 接通 process_promotion + 出组安全阀 + 审计完整 | 2h | test_phase3_3 全过 + healer e2e | ✅ push |
| 4 | fix(intel): 占位 tier 修复 + CrossSourceDiscrepancy UNIQUE 约束 | 1h | test_phase3_3 + test_phase4_e3 全过 | ✅ push |
| 5 | fix(dashboard): E1/E3 决策落地 (approve/resolve 刷缓存 + 迁移组 + 回流) | 2h | test_phase4_e1 + test_phase4_e3 全过 + 手动 e2e | ✅ push |
| 6 | fix(intel): E2/E3 鲁棒性 (provider 字段 + rollback + 校验) | 1.5h | test_phase4_e2 + test_phase4_e3 全过 | ✅ push |

**总工作量**: 9h（≈ 1 个工作日）
**commit 间隔**: 每完成一个 commit 就 push 到 `release/phase3-4-phase4-e1`，便于 reviewer 增量审阅

---

## 五、风险与回退

| 风险 | 缓解 | 回退方案 |
|------|------|---------|
| Commit 2 改 tier 语义后历史 promotions 消费时 tier 全错 | tier 永远走 log approved=0，等 dashboard 批准 | git revert commit 2 |
| Commit 3 接通 process_promotion 后 promotions 堆积数据一次性爆炸 | 加 dry-run flag，第一次只统计不写 | 临时关 scheduler，手动 DELETE pending promotions |
| Commit 5 approve 同步 refresh_cache 阻塞响应 | dashboard 加 spinner；refresh_cache 失败只 logger 不抛 500 | git revert commit 5 |
| Commit 6 PriceChange 加 provider 字段影响所有 pricing_sync 测试 | 同步改 dataclass + 测试 mock | n/a (低风险) |

---

## 六、不在本计划内（下个迭代处理）

- I5 本地 channel priority fallback 行为验证
- I6 cache 粒度粗（pricing_sync.py:137-152 全量刷新）
- I8 N+1 查询（pricing_sync.py:71-76）
- P1-2 自动出组不删 members（设计选择，更新 architecture.md §5.4）
- P1-3 process_promotion 单事务（每 model 独立事务）
- P1-4 ✅ 已合并到 commit 3
- P1-5 / P1-6 / P1-13~P1-18 (前端 polish)
- P2-7 asyncio deprecated / P2-19~P2-24 a11y / polish

---

## 八、执行前置检查

每次 commit 前必须跑：

```bash
git check-ignore -v .env  # 确认 .env 未被追踪
git status --short         # 确认无意外文件
```

参考 [user_behavior: 提交前必须检查.env未被git追踪]。

---

**计划完成时间**：2026-08-08（一天）
**计划 review**：完成后由架构师/全栈/前端三方重审