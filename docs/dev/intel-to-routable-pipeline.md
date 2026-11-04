# intel 发现 → 可路由：链路断点与设计（2026-11-04）

> 触发：生产「情报-待确认能力变更」出现 6 条 metadata 行（Seedream 5.0 flash/pro、
> Seedance 2.0/2.5、Seed-Evolving、DeepSeek-V4.1-Flash），全部 provider=volcengine，
> 全部 `field_changed="metadata"`、`inferred_tier=null`。排查结论：**批准 = 结案归档，
> 零功能变更**；且其中 5 个模型本站根本没有渠道，1 个是已有模型的展示名形态。
> 本文把「intel 发现一个模型」到「它能被路由」的链路通盘拆开，给出三处设计。

> **2026-11-04 O1 已确认并落地**（用户拍板）：
> - 供给闸门（§3.3 方案 C）已实现 —— `process_model_item` 无供应证据 → `unsupplied` 不入队；
> - 确定性 resolver（§3.1 方案 A v1）已实现 —— `intelligence/model_resolver.py` 六步解析、
>   高置信直接当现网模型（冲突只留应用日志，不落队列）、启发式解析挂 proposal 待人工确认、
>   批准过的解析结果借 `model_capabilities_log` 历史行做别名记忆（**零 schema 变更**，A2 拍板不上表）；
> - 价行小修已实现（§3.2 B1）—— `pricing_writer` 写前 resolver + `raw_model_id`，顺带修了
>   旧实现"缓存精准刷新从不触发"的 dead path；
> - 周级采集 job 重启重锚已修 —— `_batch_job_add_kwargs` 保留原 `next_run_time`（未来保留 / 过期补跑）。
> 相应回归：`tests/test_intel_model_resolver.py`（17 例）+ `tests/test_intel_batch_job_next_run.py`（6 例）。

## 1. 现状链路（代码事实）

```
采集源 (intelligence/sources/*)
  └─ fetch → parser (ModelIntelItem) → promotions.raw_data(_intelligence_kind=model_metadata)
       └─ model_sync.process_promotion → process_model_item
            ├─ conf≥0.8 (T0) / ≥0.9 (非 T0) + tier 明确 → upsert model_registry + 自动入 tier: 组
            │     · 无价行 → status=inactive (V4.8.6)，价行出现后复扫翻 active
            └─ 否则 → model_capabilities_log(field=metadata, approved=0)  ← 本次 6 条在这里
                 └─ dashboard 批准 → approve_capability_change
                      · tier         → 写 registry.tier + 组迁移
                      · create       → 仅检查渠道翻 active
                      · status       → deprecated 出组
                      · 其余 (metadata / display_name / context_length / modality) → 只置 approved=1
```

价行是独立链路：
- `price_snapshots`：`model` = **规范名（路由键）**，`raw_model_id` = 上游线名（严格 raw id
  渠道必需，如 siliconflow）；`channel_id` 指向渠道。
- 写入来源：①`scripts/manual_import.py` ②看板价格表行内编辑 ③`intelligence/pricing_writer.py`
  （intel pricing 类解析结果，直接写库，不经审批）。
- 路由：`gateway/router.py` 先按规范名精确匹配价行，未命中走 fuzzy（前缀），再未命中走
  聚合透传闸门（有价行模型不透传）。

## 2. 三处断点

### B1 命名断点（结构性，影响最大）
intel 的 `item.model_id` 是**上游展示名/线名**（`DeepSeek-V4.1-Flash`、
`Doubao Seedream 5.0 flash`），而 registry/价行的键是**规范名 slug**
（`deepseek-flash`）。二者没有任何映射层：

| 后果 | 证据 |
|---|---|
| pending 无法关联现网模型 | log 58 的 `DeepSeek-V4.1-Flash` 在 registry 不存在；真身 `deepseek-flash`（T0/active/multimodal，4 渠道规范名价行） |
| 同一模型可能建出第二套 registry 行 | auto-join 分支按 `item.model_id` 原样 upsert |
| `pricing_writer` 可能写出非规范名价行 | 它用 `item.model_id` 直接写 `price_snapshots.model` |

注意：**这不影响路由正确性**（网关侧靠价行 `raw_model_id` 已能正确回写上游名），
只影响 intel 的可用性 —— 它既关联不上老模型，也说不清新模型是谁。

### B2 供给断点
"文档页出现了某个模型名" ≠ "本站能供应它"。Seedream 是生图、Seedance 是视频生成，
本来就不该进 LLM 网关；volcengine 也没有休眠渠道/key。但 `process_model_item` 的
pending 分支**不校验可供应性**，于是这些名字全部变成待确认噪声（6/6）。

### B3 价行断点
即使渠道真供应，模型要进路由还差一条**规范名 + raw_model_id 的价行**。目前三路写入里：
手工两路靠人；intel 那路（pricing_writer）既没有 resolver 也没有 raw_model_id，
对新模型/严格 raw id 渠道都可能写歪。

## 3. 设计

### 3.1 方案 A：canonical 解析层（建议「只建议，不自动合并」）

新增 `intelligence/model_resolver.py`，纯函数 + 一次 DB 查，按序解析：

1. `registry.model_id` 精确命中
2. `normalize_model_id`（小写/去空白/去 BOM）后命中
3. `price_snapshots.raw_model_id` 精确命中（大小写不敏感）→ 取 `price_snapshots.model`
4. 剥 vendor 前缀（`deepseek-ai/`）+ 剥确定性后缀（`-1017` / `-0813` 日期版、`-preview` /
   `-instruct` / `-chat` / `-latest`）后命中 registry
5. `registry.display_name` 归一后精确相等
6. 全失败 → `resolved=None`（真·新模型）

写入方式：
- pending 的 `new_value` 增补 `resolved_model_id` / `resolved_by`，**不改写 `model_id`**；
- auto-join 分支前置：`resolved` 存在 → 不新建 registry，降级为
  display_name / context_length / modality 参考 log（附 resolved 值）；
- 前端卡片显示「≈ 已匹配 deepseek-flash」。

拍板项：
- **A1 不做 fuzzy（编辑距离/相似度）**：`deepseek-v4-flash` 与 `deepseek-flash` 是不同代际，
  fuzzy 误并的代价（错价/错档/错组）远高于多留一条 pending。推荐：确定性规则止步于第 5 步。
- **A2 是否引入 `model_alias` 表**（schema 变更）：把人工确认过的别名持久化，后续扫描零成本命中。
  推荐 v2 再上；v1 先用「每次解析 + 人工 approve 留痕」。

### 3.2 方案 B：新模型 → 可路由 的三段闸门

| 段 | 判定 | 现状 |
|---|---|---|
| 段1 入库 | registry 行存在；无价行 → `status=inactive` | 已有（V4.8.6） |
| 段2 **可供应** | 渠道 `/v1/models` 真实列出该模型（`model_list_snapshots`）**或**已有价行 | **缺** |
| 段3 **可路由** | registry.active + ≥1 条 active 渠道价行（规范名 + raw_model_id） | 靠手工 |

价行生成建议收敛为三路，且**默认不直写路由**：
1. 渠道 API 列表 → 只生成「待报价」条目（无价）；
2. 官方定价页 intel → 经 resolver 映射 → 写规范名价行**并带 raw_model_id**；
3. 看板行内编辑（人工兜底，已有）。

拍板项：
- **B1 `pricing_writer` 是否改为「写建议价行」而非直写 `price_snapshots`**：现状错价会立刻进路由。
  推荐：先做小修（写前 resolver + raw_model_id），价行审批队列另立项。
- **B2 段2 的数据源**：用 `model_list_snapshots`（已有表，/v1/models 快照）为主，价行为辅。

### 3.3 方案 C：准入闸门下沉（治噪声的最小改动，建议先做）

在 `process_model_item` 的 pending 分支前加一道 `is_suppliable(item.model_id)`（段2 判定）：
不可供应的模型**不写 `model_capabilities_log`**，只在 promotions 里留观察记录。
这条做完，本次 6 条 volcengine 噪声从源头就不会产生；配合 A 的 resolver，
`DeepSeek-V4.1-Flash` 会被解析到 `deepseek-flash` 从而自然消解。

### 3.4 落地顺序

| 优先级 | 内容 | 状态 |
|---|---|---|
| P0 | 批准弹窗文案按 field 如实说明；volcengine 源代码级停用 | ✅ 已完成 |
| P1 | C 闸门 + A 的确定性 resolver（只建议）、pending 附 resolved 字段 | ✅ 已完成 (O1) |
| P2 | 价行链路接入 resolver + raw_model_id（pricing_writer 小修，不动审批流） | ✅ 已完成 (O1)；`model_alias` 表仍不建 |
| P3 | 价行「建议 → 审批」队列化（`pricing_writer` 不再直写） | ⏳ 另立项 |
| P4 | 看板「发现但不可路由」清单（段2/段3 健康链可视化） | ⏳ 未拍板 |

## 4. 采集源治理盘点（2026-11-04 生产实测）

### 4.1 为什么「采集管理」里看不到 volcengine
它在，且是最后一行的原因：`/api/sources` 按 `(provider, source_type)` 字母序排，
`volcengine` 排最后；整表是 40 行扁平表格塞在 300px 高的滚动容器里（`scroll-y-300`），
不滚到底就看不见。

### 4.2 三类「没有管理入口」的源

| 类别 | 实例 | 为什么管不到 |
|---|---|---|
| DB 孤儿行（内存无对应源） | qwen 35/36/37、deepseek 38/40、hunyuan 39、sensenova 41（共 7 行，均 disabled） | 状态键是 `(provider, url)`，源 URL 改过后旧行留在库里；UI 只渲染内存源 → 不可见、不可切、不可删 |
| 从未成功执行 | `artificial_analysis`、`opencompass`（benchmark，无 DB 行） | 无 DB 行 ⇒ 未成功采集过；而 11-04 新增的结构性死源审计只扫 `status=active` 的行，抓不到这类 |
| 想单独关一个源 | sensenova 4 源、qwen 5 源、deepseek 5 源… | toggle 是 **provider 级**（`/api/sources/{provider}/toggle` 切该 provider 全部源），单源不可选 |

### 4.3 周级 benchmark job 被重启无限推迟（新发现）
`apscheduler_jobs` 里 4 个 `batch_crawl_*_604800` 的 `next_run_time` 全部 = **今日 11:15
重启时刻 + 7 天**；lmsys/livebench/superclue 的 `last_check_at` 停在 2026-10-12。
即：每次服务重启 `add_job(..., replace_existing=True)` 都会重算 `next_run_time`，
**只要重启间隔 < 7 天，周级源就永远不会执行**。

建议（待拍板）：启动时对已存在的周级 job 保留原 `next_run_time`（存在即跳过 re-add），
或给 benchmark 类源改用日级 tick + 内部周闸门。

### 4.4 已落地
- `enabled=False` 从"装饰性字段"变成真闸门：调度注册、手动触发、`_run_source_task`
  三处短路；dashboard 显示「已下线(代码)」并灰掉开关/采集按钮；toggle/run-now 返回 409。
- 生产库 `intel_sources#32 (volcengine)` 已置 `disabled`（即时止血，无需等发版）。
- `get_all_sources()` 仍保留 volcengine 实例（摘注册会退化成 4.1 的孤儿行问题）。
