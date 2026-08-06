# AIMC release/phase3-4-phase4-e1 分支代码评审报告

> 评审日期：2026-08-06（首轮评审）
> 评审 HEAD：`b2611c6` (feat(phase4.E3): 跨源交叉验证)
> 评审方式：架构师 + 全栈开发 + 前端代码评审 三方并行
> 评审范围：Phase 3.1-3.4 / Phase 3.A-E / Phase 4.E1-E3 全部新增代码
> 评审性质：**首轮只读评审，未修改任何业务代码**
> 修订日期：2026-08-07（评审反馈整合与设计选择澄清，详见第十一节）

---

## 一、评审概述

本报告为首轮评审成果，覆盖 Phase 3.1-3.4 / Phase 3.A-E / Phase 4.E1-E3 全部新增代码。所有 P0/P1 问题均为**首次暴露**，不存在"上轮未修复"的情况——本分支是项目的首个完整 feature 分支，从未经过 review→fix→re-review 循环。

### 1.1 评审范围提交历史

```
b2611c6 feat(phase4.E3): 跨源交叉验证 — 同 model 多源 tier/capabilities 不一致时告警
8a2f0c6 feat(phase4.E2): 价格大幅波动告警 (≥30% 自动 Bark/Feishu)
c747964 feat(phase4.E1): Dashboard 待确认队列 — 人工确认 tier / 出组变更
ccb231d feat(intel): Phase 3.3 自动入组/出组 — 模型元信息消费服务
01792a5 feat(phase3.4): 价格联动与路由缓存刷新
4666231 feat(phase3.2): 升级 AI 解析器支持元信息与 tier 推断
f1c3487 feat(phase3.1): 扩展情报采集源与分级调度
```

### 1.2 评审代码量

- **Phase 3 + Phase 4.E1**：39 文件、6037 行新增
- **Phase 4.E2**：5 文件、473 行新增
- **Phase 4.E3**：8 文件、1157 行新增
- **总计**：52 文件、约 7700 行新增

---

## 二、总体评价

| 评审维度 | 等级 | 核心判断 |
|---------|------|---------|
| 架构设计 | **B** | 目录分层清晰、安全设计扎实。P0 问题集中在"情报联动未接通 + Tier 语义不一致"，修复后可达 A- |
| 后端代码 | **B** | 功能完整、设计有亮点（如 E2 异常隔离、E3 与 model_sync 解耦），需修复 P0 + 4 项 P1 后达 B+ |
| 前端代码 | **C** | XSS 防护意识到位、API 契约 100% 对齐，但 **P0-6 SRI 伪造 → Dashboard 根本打不开**是最严重抹点；修复后可達 B- |

> **修订记录 (2026-08-07)**：原报告给"架构 中等偏下 / 后端 B- / 前端 D"。重新核对后调整：
> - 架构 D → B：原报告以"B1+B2+B3 三重缺陷互相独立"为依据，但 B3 的推理链不成立（flagship tier=NULL 放行，只有 strong/balanced/light 三组受影响）；P0-2 tier 语义冲突是单一根本问题，统一修复即可
> - 后端 B- → B：原报告算上 P1-1/P1-10 等"决策不生效"问题。修复 4 项 P1 后业务逻辑贯通
> - 前端 D → C：原报告以 SRI 伪造直接判定 D，但实际上后端 API 契约 100% 对齐，XSS 防护到位，只是 htmx 使用模式需调，整体不算最差

### 2.1 核心结论

1. **Phase 3.3 "情报联动模型组"核心问题是 Tier 语义不一致 (B2)** —— B2 修复后 B3 的严重度明显下降（只有 tier:strong/balanced/light 三组受影响），B1（未接通）是独立问题需单独修。B2 是“最根上一个问题”。
2. **Phase 4.E2 价格波动告警是健康的小而美增量**，仅需后续修 P1-8/P1-9（throttle key + 消息加 provider）。

3. **Phase 4.E3 检测侧完整，但拍板侧"悬空"** —— resolve 只更新自己表，不写 `model_capabilities_log`、不刷路由缓存、不迁移组，与上轮 I1 同类问题。

4. **前端 SRI 哈希是伪造的**（含明显顺序字符模式），现代浏览器会严格拒绝加载 htmx 和 Chart.js，Dashboard 当前根本打不开。

5. **测试盲区**：所有 Phase 3 测试都用 mock DB 且不实例化 RouteEngine，无法发现路由层冲突。

---

## 三、首轮评审 P0/P1 基线清单

**说明**：本节为首轮评审的 P0/P1 问题清单（非"上轮未修复"表）。所有项目均为首次暴露，开发分支从零开始一路推至 `b2611c6`，未经过中间 review 节点。**2026-08-07 评审反馈整合后**对每项标注：

- ✅ **确认成立**：技术论断在最新代码上核对通过，必须在本 release 修复
- ⚠️ **部分成立**：核心问题真实，但描述的推理链有瑕疵，需调整修复方向
- 🔵 **设计选择**：非 bug，是当前设计的合理性，需更新文档/注释使其自洽
- ⏸️ **待验证**：需要进一步看代码才能下结论
- ⏭️ **下个迭代**：本 release 不修，下个迭代统一处理

| 编号 | 严重度 | 问题 | 状态 | 证据 |
|------|--------|------|------|------|
| **B1** | P0 | `process_promotion` 从未被生产代码调用 | ✅ 确认成立 | [healer.py:_save_promotion](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/healer.py#L300-L348) 仍未调 `process_promotion`；E3 反而绕过该入口直接读表 |
| **B2** | P0 | Tier 语义冲突（T0=旗舰 vs T0=基础） | ✅ 确认成立（最严重） | [parser.py:73](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/parser.py#L73) 仍把 T0 当"基础"；E3 新增 [cross_source_validator.py:31-32](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/cross_source_validator.py#L31-L32) 注释"T0 最低，T4 最高"，与 seed/architecture.md 完全相反 |
| **B3** | P0 | 占位 `tier="T2"` 与路由过滤冲突 | ⚠️ 部分成立 | [model_sync.py:143](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L143) 占位 T2 真实；但 reviewer 描述的"AI 推断 T0→flagship→被过滤"链不成立（flagship tier=NULL，路由放行）；真实问题是 tier:strong/balanced/light 三组的 tier 过滤与 T2 占位冲突 |
| **B4** | P0 | 出组分支 fallthrough 导致 deprecated 模型重新入组 | ✅ 确认成立 | [model_sync.py:87-110](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L87-L110) 无 early return；should_auto_leave(conf≥0.9) 必然满足 should_auto_join(conf≥0.8) |
| **B5** | P0 | `context_length`/`modality` 直接 mutate 未写审计 log | ✅ 确认成立 | [model_sync.py:175-178](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L175-L178) 与 docstring 矛盾 |
| **B6 (前端)** | P0 | htmx/Chart.js SRI 哈希伪造 | ✅ 确认成立（最阻塞） | [index.html:8-9](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/index.html#L8-L9) 仍含 `K2g3H4j5L6m7N8o9P0q1R2s3T4u5V6w7X8y9Z0`，Dashboard 当前打不开 |
| **I1** | P1 | approve tier 后不刷缓存、不迁移组 | ✅ 确认成立 | [crud.py:478-485](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/db/crud.py#L478-L485)；E3 拍板侧新增同类问题 |
| **I2** | P1 | 自动出组不删 `model_group_members` | 🔵 设计选择 | 我们设计就是保留 members（路由层 status 过滤兜底，audit 完整），需更新 architecture.md §5.4 文档使其自洽 |
| **I3** | P1 | `process_promotion` 单事务包裹整批 | ✅ 确认成立 | [model_sync.py:238-247](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L238-L247) |
| **I4** | P1 | 重复入组每次重写 tier log | ✅ 确认成立 | [model_sync.py:180-190](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L180-L190) |
| **I5** | P1 | 本地 channel `priority=0` 抢占聚合平台 fallback | ⏸️ 待验证 | [seed_local_channel.py:155](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/scripts/seed_local_channel.py#L155) + router 行为需进一步看 |
| **I6** | P1 | 缓存刷新粒度过粗（N model = N 次全量刷） | ⏭️ 下个迭代 | [pricing_sync.py:137-152](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/pricing_sync.py#L137-L152) |
| **I7** | P1 | approve_capability_change 日志输出在赋值之后 | ✅ 确认成立 | [crud.py:483-485](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/db/crud.py#L483-L485) 永远打印 "T3 -> T3" |
| **I8** | P1 | `sync_price_change` 在循环里 N+1 查询 channels | ⏭️ 下个迭代 | [pricing_sync.py:71-76](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/pricing_sync.py#L71-L76) |
| **I9 (前端)** | P1 | htmx + JSON + innerHTML 渲染模式数据完整性隐患 | ✅ 确认成立 | E3 新增 [app.js:393](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L393) `JSON.parse(container.textContent)` |
| **I10 (前端)** | P1 | approve/reject 与 htmx 30s/60s 轮询竞态，卡片"复活" | ✅ 确认成立 | E3 新增 [app.js:332-368](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L332-L368) |
| **I11 (前端)** | P1 | `esc()` 不转义引号 + `onclick` 内联拼接 → XSS 隐患 | ✅ 确认成立 | E3 新增 [app.js:427-428](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L427-L428) |
| **I12 (前端)** | P1 | approve/reject 按钮无 loading/禁用态 | ✅ 确认成立 | E3 `resolveCrossSource` 同样无禁用 |

---

## 四、完整问题清单（按严重程度分级）

### 4.1 P0 阻塞级（合并前必须修复）

#### P0-1: Phase 3.3 自动入组/出组从未被生产代码调用（死代码）

**评审方**：架构师
**定位**：[healer.py:_save_promotion](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/healer.py#L300-L348)
**关联**：[model_sync.py:215](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L215) `process_promotion` 定义

**证据**：全局搜索 `process_promotion` 调用点，仅出现在 [tests/test_phase3_3_model_sync.py](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/tests/test_phase3_3_model_sync.py)，生产代码零调用。

- [healer.py:handle_parse_result](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/healer.py#L96-L189) 在 `_save_promotion` 后直接 `return`，从不调用 `model_sync.process_promotion`
- [scheduler.py:start_scheduler](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/scheduler.py#L51-L113) 只注册 3 类 job（crawl / refresh_route_cache / check_key_rotation），没有 model_sync 消费 job
- [healer.py:318-321](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/healer.py#L318-L321) 把 `models[]` + `_intelligence_kind` 塞进 `promotions.raw_data`，但没有任何消费者读这个字段触发入组

**后果**：Phase 3.3 的核心承诺"情报爬虫发现新模型 → 自动入组"在生产中完全不发生。promotions 表里堆积着带 `models[]` 的行，`model_registry` / `model_group_members` 永远不会被自动维护。

**修复方向**：在 [healer.py:_save_promotion](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/healer.py#L300-L347) commit 后，若 `raw_data._intelligence_kind == "model_metadata"` 则调用 `await model_sync.process_promotion(promo_id, raw_data)`；或在 scheduler 注册一个定期扫描未消费 promotions 的 job。

---

#### P0-2: Tier 语义在 AI parser 与 DB seed/architecture.md 之间完全相反

**评审方**：架构师
**定位**：
- [parser.py:73](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/parser.py#L73) SYSTEM_PROMPT
- [parser.py:99-112](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/parser.py#L99-L112) infer_tier
- [cross_source_validator.py:31-32](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/cross_source_validator.py#L31-L32) `_TIER_ORDER` 注释（E3 新增矛盾点）
- 对比 [docs/architecture.md:90-94](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/docs/architecture.md#L90-L94) + [scripts/seed_model_groups.py:55-66](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/scripts/seed_model_groups.py#L55-L66) + [scripts/seed_model_registry.py:46-48](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/scripts/seed_model_registry.py#L46-L48)

**证据**：四处定义相互冲突：

| 来源 | T0 含义 | T4 含义 |
|------|---------|---------|
| architecture.md §4.2（规范） | 旗舰 | 微型 |
| seed_model_registry.py / seed_model_groups.py | deepseek-reasoner（旗舰） | — |
| parser.py SYSTEM_PROMPT | **基础/嵌入** | **旗舰前沿** |
| parser.py infer_tier | <7B → T0 | 200B+ → T4 |
| cross_source_validator.py 注释 | "T0 最低" | "T4 最高" |
| test_phase3_3 mock | — | tier:flagship=T4 |

**后果链**：
1. AI 按 parser.py 指令推断"旗舰大模型" → 返回 `inferred_tier="T4"`
2. [model_sync.py:135](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L135) `_find_target_group(session, "T4")` 找 `group.tier=="T4"` 的组 —— 但 seed_model_groups.py 里旗舰组 `tier=None`、strong=T1、balanced=T2、light=T3，没有 T4 组
3. `target_group=None` → 模型不入组
4. tier log `new_value="T4"` 写入；人工批准后 [crud.py:483](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/db/crud.py#L478-L484) 设 `registry.tier="T4"`，但没有任何 tier: 组的 tier=T4，模型永远无法参与 tier 组路由

**修复方向**：统一以 architecture.md §4.2 为准（T0=旗舰）。修改 [parser.py:73](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/parser.py#L73) SYSTEM_PROMPT 和 [parser.py:99-112](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/parser.py#L99-L112) infer_tier 的 size→tier 映射（200B+→T0, <7B→T4），同步修 [cross_source_validator.py:31-32](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/cross_source_validator.py#L31-L32) 注释和测试 mock。

---

#### P0-3: 自动入组后路由 tier 过滤冲突（placeholder tier bug）

**评审方**：架构师
**定位**：[model_sync.py:139-151](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L139-L151) + [router.py:646](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/gateway/router.py#L646)

**证据**：即使 P0-1/P0-2 修复，自动入组的模型仍会被路由跳过。

- [model_sync.py:139-151](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L139-L151)：新模型 `upsert_model_registry(tier="T2")`（占位），注释明说"tier 字段保留 T2 占位, 实际 tier 走 log + 人工确认"
- [model_sync.py:135](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L135) + [model_sync.py:194](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L193-L197)：按 AI `inferred_tier`（如 T0）选 target_group（如 tier:flagship），加入该组
- [router.py:646-652](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/gateway/router.py#L645-L652)：组路由过滤 `if group.tier and registry.tier != group.tier: skip`

**冲突链**：AI 推断 T0（旗舰） → 加入 tier:flagship（group.tier 设为 T0） → registry.tier 仍是 T2 占位 → 路由 `group.tier(T0) != registry.tier(T2)` → 跳过。只有 AI 恰好推断 T2 时，占位值碰巧匹配 tier:balanced，模型才能被路由。

**修复方向**（三选一）：
- (a) 最干净：自动入组只对 `scene:` 组（不按 tier 过滤）生效；`tier:` 组入组推迟到人工批准 tier log 时触发
- (b) 路由层：[router.py:646](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/gateway/router.py#L645-L652) 过滤时，若 registry.tier 是占位值且该 model 有 pending tier log，用 log.new_value 参与 tier 比对（复杂，不推荐）
- (c) 占位值改为 `None` 而非 "T2"，并让路由对 `registry.tier is None` 放行（但 ModelRegistry.tier 是 NOT NULL，需改 schema）

---

#### P0-4: 出组分支 fallthrough 导致 deprecated 模型重新入组（安全阀失效）

**评审方**：全栈
**定位**：[model_sync.py:86-110](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L86-L110)

`should_auto_leave(item) == True` 时仅当 `existing and existing.status != "deprecated"` 才 `return`。当 registry 不存在该 model 或已被标记 deprecated 时，代码会 fall through 到入组判定，而 `should_auto_leave` 要求 `confidence >= 0.9`，必然也满足 `should_auto_join` 的 `confidence >= 0.8` 条件 —— 一个被 AI 判定为 deprecated 的模型会被重新入组。

**修复方向**：在出组判定分支末尾无条件 `return`，避免 fallthrough 到入组流程。

---

#### P0-5: `context_length` / `modality` 直接 mutate ModelRegistry 未写审计 log

**评审方**：全栈
**定位**：[model_sync.py:163-178](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L163-L178)

文件头 docstring [model_sync.py:9-12](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L9-L12) 明确写："capabilities / context_length / modality 变更也走 log，不直接改 ModelRegistry"，但实际代码对 `display_name` 写 log，对 `context_length` / `modality` 直接 `existing.xxx = item.xxx` 跳过审计。AI 误判 modality=text→vision 时，路由组 `require_vision` 过滤会被绕过，dashboard 也无法审计这次变更。

**修复方向**：与 display_name 保持一致，全部走 `log_capability_change`，context_length/modality 不直接改 registry，等 dashboard 批准。

---

#### P0-6 (前端): htmx + Chart.js 的 SRI `integrity` 哈希是伪造占位符

**评审方**：前端
**定位**：[index.html:8-9](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/index.html#L8-L9)

```html
<script src="https://unpkg.com/htmx.org@1.9.12"
        integrity="sha384-ujb1lZTOjDkG0eMq1vq9iF1K2g3H4j5L6m7N8o9P0q1R2s3T4u5V6w7X8y9Z0"
        crossorigin="anonymous"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"
        integrity="sha384-k2G1V1M0mN4j5L6n7O8p9Q0r1S2t3U4v5W6x7Y8z9A0b1C2d3E4f5G6h7I8j9K0"
        crossorigin="anonymous"></script>
```

`sha384-` 哈希内容含明显顺序字符模式（`K2g3H4j5L6m7N8o9P0q1R2s3T4u5V6w7X8y9Z0`），非真实 SHA-384 摘要。当 `integrity` 属性存在且哈希不匹配时，现代浏览器会严格拒绝执行该脚本。

**后果**：
- htmx 不加载 → 所有 `hx-get` 失效 → 7 个 section 永远停留在"加载中..."
- Chart.js 不加载 → `new Chart(...)` 抛 `ReferenceError` → 用量图表空白
- E3 新增的 [app.js:380](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L380) `htmx.trigger('#cross-source-list', 'load')` 会抛 `ReferenceError: htmx is not defined`

**修复方向**：方案 A（推荐，自托管）—— 把 htmx 和 chart.js 下载到 `dashboard/static/vendor/`；方案 B —— 用 `curl | openssl dgst -sha384` 重新计算真实哈希。

---

#### P0-7 (E3 新增): upsert 非原子 + 无 UNIQUE 约束 → 并发产生重复 pending 行

**评审方**：全栈
**定位**：[models.py:347](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/db/models.py#L347)（非唯一索引）+ [crud.py:540-563](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/db/crud.py#L540-L563)（check-then-insert）

`upsert_cross_source_discrepancy` 是 check-then-insert，无行级锁。索引 `idx_cross_source_status` 是非唯一组合索引。当 24h scheduler 任务与 dashboard `/api/cross-source/run-detection` 手动触发并发时，两路都查不到 pending 行 → 各自 INSERT → 同一 (model_id, field_changed) 出现 2 条 pending。后续 upsert 只覆盖第一条，第二条成孤儿。

**修复方向**：对 (model_id, field_changed, status) 加 UNIQUE 约束，或 INSERT...ON CONFLICT。

---

### 4.2 P1 重要级（影响正确性/可用性/安全性，应尽快修复）

#### P1-1: approve tier 后不刷缓存、不迁移组（"生效"是名义上的）

**评审方**：架构师 + 全栈
**定位**：[crud.py:478-484](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/db/crud.py#L478-L484) + [dashboard/routes.py:560-588](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/routes.py#L560-L588)

`approve_capability_change` 设 `registry.tier = log.new_value` 后，不调用 `route_engine.refresh_cache()`。approve endpoint 也不刷缓存，且不更新 `model_group_members`：模型还在旧 tier 组里，新 tier 组没有它。用户在 Dashboard 批准"model X 从 T2→T0"后，最长 5 分钟路由缓存仍是旧 tier；即便缓存刷新，`_group_members_cache` 里 model X 仍在 tier:balanced 组、不在 tier:flagship 组。

**修复方向**：approve endpoint 在 commit 后调 `route_engine.refresh_cache()`；并在 `approve_capability_change` 里，当 `field_changed=="tier"` 时，把 model 从旧 tier 组移除、加入新 tier 组。

---

#### P1-2: 自动出组不删除 model_group_members（违反架构 §5.4）

**评审方**：架构师
**定位**：[model_sync.py:87-109](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L87-L109)

`should_auto_leave` 分支只设 `existing.status = "deprecated"`，不 DELETE `model_group_members`。[architecture.md:304-310](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/docs/architecture.md#L304-L310) §5.4 明确要求：`DELETE FROM model_group_members WHERE model_id=X`。

路由间接排除（`_registry_cache` 只加载 status==active），但 `model_group_members` 表累积脏行，[dashboard/routes.py:get_registry_groups](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/routes.py#L244-L280) 仍把废弃模型列在组成员里，数据一致性破坏。

**修复方向**：在设 status=deprecated 后，加 `DELETE FROM model_group_members WHERE model_id=X`。

---

#### P1-3: `process_promotion` 单事务包裹整个 batch，单个失败导致整批回滚

**评审方**：全栈
**定位**：[model_sync.py:238-247](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L238-L247)

`for raw in models:` 内只 try/except 了 `ModelIntelItem(**raw)` 构造，`process_model_item` 内部任意 DB 写入抛异常会传播到 `async with async_session_factory() as session:` 块外，触发整批回滚。一个 batch 里 10 个 model 中第 7 个失败，前 6 个的入组/log 也会丢失。

**修复方向**：每个 model 独立事务（commit per item）或用 savepoint。

---

#### P1-4: 重复入组时每次都重写 tier log，dashboard 待确认队列被同条目刷屏

**评审方**：全栈
**定位**：[model_sync.py:180-190](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L180-L190)

对已存在 registry 的 model，每次 `process_model_item` 都会无条件 append 一条 `field_changed="tier"` 的 log（即使 tier 没变）。每 15 分钟跑一次采集，同 model 一天会写 96 条相同的 tier log。

**修复方向**：仅当 `new_tier != old_tier` 时才写 log。

---

#### P1-5: 本地 channel `priority=0` 抢占聚合平台 fallback

**评审方**：架构师
**定位**：[seed_local_channel.py:155](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/scripts/seed_local_channel.py#L155) + [router.py:303-315](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/gateway/router.py#L303-L315)

本地 channel `priority=0`，注释写"本地 = 高优先级"。硅基流动 `priority=1`（注释"优先级最低"）。`_get_fallback_channel` 用 `min(priority)` 选 fallback → priority=0 的本地 channel 胜出。而正常路由 sort_key 是 `-priority`（越大越优先）。

**冲突**：priority 字段在"正常路由"和"fallback"里方向相反。本地 priority=0 在正常路由里是最末优先级，但在 fallback 里却变成第一选择。用户请求一个本地 Ollama 没有的模型（如 `deepseek-chat`）且无渠道明确支持时，fallback 命中本地 Ollama → Ollama 返回 404。而原本应 fallback 到硅基流动（1 key 通吃 100+ model）。

**修复方向**：[seed_local_channel.py:155](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/scripts/seed_local_channel.py#L155) 本地 channel priority 设为 5；或 [router.py:_get_fallback_channel](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/gateway/router.py#L303-L315) 排除 `provider_type=='local'` 的 channel。

---

#### P1-6: 缓存刷新粒度过粗 + N+1 查询

**评审方**：架构师 + 全栈
**定位**：
- [pricing_sync.py:137-152](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/pricing_sync.py#L137-L152) 每个 model 变动都调 `route_engine.refresh_cache()`（全量重载）
- [healer.py:208-220](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/healer.py#L208-L220) 循环 per-model 调用 → N 个 model 变动 = N 次全量缓存重载
- [pricing_sync.py:71-76](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/pricing_sync.py#L71-L76) 每次调用 `list_channels` → 循环 N 个 model = N 次相同查询

**后果**：硅基流动定价页一次同步 50 个 model，触发 50 次 `refresh_cache`，每次重载全部 channel+registry+group。性能浪费且日志刷屏。

**修复方向**：healer 循环里只调 `sync_price_change`（落库），循环结束后调一次 `apply_price_change_to_route_cache`（批量刷缓存）；pricing_sync 提供 `apply_price_changes_batch(changes: list)`；caller 预查一次 channel，传入 sync_price_change。

---

#### P1-7: approve_capability_change 日志输出在赋值之后，记录不到 old_value

**评审方**：全栈
**定位**：[crud.py:482-485](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/db/crud.py#L482-L485)

```python
if log.field_changed == "tier" and log.new_value:
    registry = await session.get(ModelRegistry, log.model_id)
    if registry:
        registry.tier = log.new_value           # 先赋值
        logger.info("人工批准: model=%s tier %s -> %s",
                   log.model_id, registry.tier, log.new_value)  # 此时 registry.tier 已是新值
```

日志会输出 `tier T3 -> T3` 而不是 `T2 -> T3`，失去审计价值。

**修复方向**：先存 `old_tier = registry.tier`，再赋值，再打日志。

---

#### P1-8 (E2 新增): throttle key 与 docstring 不符 + 跨 provider 误节流

**评审方**：全栈
**定位**：[price_alert.py:8](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/price_alert.py#L8)（docstring 写 `price_{provider}_{model}`）vs [price_alert.py:125](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/price_alert.py#L125)（代码 `f"price_{change.model}"` 缺 provider）

`PriceChange` 无 provider 字段。后果：siliconflow 和 openrouter 同时改 qwen-plus 价格，60s 内只发第一条告警，第二条被吞。

**修复方向**：PriceChange 增加 provider 字段；throttle key 改为 `price_{provider}_{model}`。

---

#### P1-9 (E2 新增): 告警消息缺 provider 标识

**评审方**：全栈
**定位**：[price_alert.py:121](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/price_alert.py#L121)

消息用 `change.source`（默认 "crawler"），不含 provider。运维收到"模型: qwen-plus / 来源: crawler"无法定位是哪家厂商调价。

**修复方向**：告警消息加入 provider/channel 字段。

---

#### P1-10 (E3 新增): E3 拍板不回流，决策悬空

**评审方**：架构师
**定位**：[dashboard/routes.py:679-695](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/routes.py#L679-L695)

resolve 只更新 `cross_source_discrepancies` 自身字段，不写 `model_capabilities_log`、不更新 `model_registry.tier`、不调 `route_engine.refresh_cache()`。人工拍板 T3 后，[router.py:646](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/gateway/router.py#L646) 仍按 registry 旧 tier（占位 T2）过滤。与 P1-1 同类问题，E3 复制了"审批后不生效"的模式。

**修复方向**：resolve endpoint 在 commit 后调 `route_engine.refresh_cache()`，并考虑写一条 `model_capabilities_log` 让 E1 队列消费。

---

#### P1-11 (E3 新增): upsert 失败后 session 未 rollback，后续操作全部静默失败

**评审方**：全栈
**定位**：[cross_source_validator.py:185-200](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/cross_source_validator.py#L185-L200)

循环内 `except Exception` 捕获但无 `session.rollback()`。SQLAlchemy session 在 DB 错误后进入 failed 态，后续 `execute()` 全部抛错被同一 except 吞掉，最终 `await session.commit()` 也可能失败。表现为"只入库了前几条，后面全丢且无报警"。

**修复方向**：except 内补 `await session.rollback()`。

---

#### P1-12 (E3 新增): resolve API 校验问题

**评审方**：全栈
**定位**：[dashboard/routes.py:673-675](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/routes.py#L673-L675)

- `if not resolved_value` 误拒 falsy 合法值（`0`、`""`、`False`）—— 对 context_length=0 或 modality="" 等合法值会被 400
- 强制 `ignored=True` 时也得传假值（测试里被迫传 `"(ignored)"`）

**修复方向**：改 `if resolved_value is None`；`ignored=True` 时允许 `resolved_value` 为空。

---

#### P1-13 (前端): htmx + JSON + innerHTML 渲染模式存在数据完整性隐患

**评审方**：前端
**定位**：[app.js:37](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L37) 等 8 处 `JSON.parse(container.textContent)`（含 E3 新增的 [app.js:393](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L393)）

[index.html](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/index.html#L21) 用 `hx-swap="innerHTML"` 让 htmx 把后端返回的 JSON 文本直接设置为容器的 `innerHTML`。浏览器的 HTML 解析器会把 JSON 中的 `<`、`>`、`&` 当作标签/实体来解析，导致 `container.textContent` 读取到的不再是原始 JSON。

**复现场景**：若某条 `pending-review` 的 `old_value` 为 `"<T2>"`（包含尖括号），后端返回 `{"items":[{"old_value":"<T2>"}]}`，htmx 设置 innerHTML 后 `<T2>` 被解析为未知 HTML 元素，`container.textContent` 变成 `{"items":[{"old_value":""}]}`，`JSON.parse` 成功但 `old_value` 丢失。

**修复方向**：放弃 htmx 运 JSON 的 hack 模式，改用原生 fetch（项目本就有 fetch 用法）。

---

#### P1-14 (前端): approve/reject 与 htmx 30s/60s 轮询竞态，卡片会"复活"

**评审方**：前端
**定位**：[app.js:250-290](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L250-L290)（E1）+ [app.js:332-368](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L332-L368)（E3 新增同类）

approve 流程是"POST → `btn.closest('.pr-card').remove()`"。但容器每 30s/60s 会被 htmx 整体 `innerHTML` 重渲染。若 POST 进行中时正好触发轮询：
1. htmx 重新渲染列表，`btn` 引用变成已脱离 DOM 的旧节点
2. POST 返回后 `btn.closest('.pr-card')` 返回 `null`，`card.remove()` 失效
3. 用户看到刚批准的卡片"复活"，会误以为没生效而重复点击

**修复方向**：approve/reject 期间暂停轮询，并在成功后主动触发一次刷新。

---

#### P1-15 (前端): `esc()` 不转义引号 + `onclick` 内联拼接 → XSS 隐患

**评审方**：前端
**定位**：[app.js:7-12](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L7-L12) `esc()`；E3 新增 [app.js:427-428](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L427-L428) `onclick="resolveCrossSource(${item.id}, this, false)"`

`esc()` 用 `textContent → innerHTML` 只转义 `<`、`>`、`&`，不转义 `"` 和 `'`。在文本节点上下文是安全的；但代码里把它用在了属性上下文：`onclick` 内联拼接 `item.id` 连 `esc()` 都没用，依赖后端 `id` 是整数才安全。一旦后续把 `model_id` 等字符串字段拼进 onclick，即可 XSS。

**修复方向**：拆分 `escText` / `escAttr` 两个函数；`onclick` 内联事件改用 `addEventListener` 绑定彻底消除注入面。

---

#### P1-16 (前端): approve/reject 按钮无 loading/禁用态，可重复点击

**评审方**：前端
**定位**：[app.js:250-290](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L250-L290)（E1）+ [app.js:332-368](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L332-L368)（E3 `resolveCrossSource`）

按钮点击后到 fetch 返回期间，按钮仍然可点。虽然后端 approve/reject 幂等，但会触发多余请求和 UI 抖动。同文件 `runCrossSourceDetection` [app.js:370-389](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L370-L389) 已示范正确写法（`btn.disabled = true` + `finally { btn.disabled = false }`）。

**修复方向**：参照 `runCrossSourceDetection` 范式补齐所有按钮的 disabled/loading。

---

#### P1-17 (E3 前端新增): 分歧分数颜色语义反转

**评审方**：前端
**定位**：[app.js:402-403](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L402-L403) + [style.css:290-303](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/style.css#L290-L303)

```js
const scoreClass = score >= 0.9 ? 'conf-high' : score >= 0.6 ? 'conf-medium' : 'conf-low';
```

```css
.pr-conf.conf-high { background: rgba(0, 186, 124, 0.15); color: #00ba7c; }  /* 绿 */
.pr-conf.conf-low  { background: rgba(244, 33, 46, 0.15);  color: #f4212e; }  /* 红 */
```

高分歧（`score >= 0.9`，分歧最严重）→ `conf-high` → 绿色，与卡片红边框 [style.css:378](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/style.css#L378) `border-left: 3px solid #f4212e` 自相矛盾，会误导操作员优先处理低分歧项。

**修复方向**：为 E3 单独定义 `disc-high/medium/low` 类（high=红、low=绿），或反转 scoreClass 映射。

---

#### P1-18: 三个渲染函数静默吞错，用户永远看到"加载中..."

**评审方**：前端
**定位**：[app.js:55-57](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L55-L57)、[L77-79](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L77-L79)、[L102-104](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L102-L104)

```javascript
} catch (e) {
    // 非 JSON 响应，保持原样
}
```

`renderChannels`/`renderPromotions`/`renderPrices` 的 catch 块完全为空。当后端返回 500 HTML 错误页或网络中断时，`JSON.parse` 抛错被吞掉，容器停留在初始的"加载中..."，用户无从知晓发生了什么。而 `renderRegistryModels` 和 E3 的 `renderCrossSource` 却显示了错误信息，处理风格不一致。

**修复方向**：统一错误反馈，参照 `renderCrossSource` [app.js:433-436](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L433-L436) 范式补齐。

---

### 4.3 P2 建议级（提升可维护性/可访问性/性能，节选）

> **修订记录 (2026-08-07)**：原报告列出 28 项 P2 未区分真 bug / 设计选择 / polish。重新分类后：
> - [B] = 真 bug（本 release 可顺手修）
> - [D] = 设计选择（代码与文档需一致化即可，不需改逻辑）
> - [P] = polish / 性能 / a11y（下个迭代）

| 编号 | 分类 | 问题 | 评审方 | 定位 |
|------|------|------|--------|------|
| P2-1 | [D] | `group_manager.py`（架构 §7.5 规划）未落地，情报层直接写路由表 | 架构 | [model_sync.py:194](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L193-L197) 直接调 `crud.add_model_group_member`。**设计选择**：为不在 Phase 3 引入新模块，model_sync 直接走 crud 是务实现选择；架构文档应取消 group_manager 规划 |
| P2-2 | [B] | `crud.find_candidate_group_for_tier` 是死代码（永远 return None） | 全栈 | [crud.py:420-430](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/db/crud.py#L420-L430)。是 B2 修复的连带项 |
| P2-3 | [B] | `reject_capability_change` 用 `approved=1` 表示"已处理"，审计无法区分 approve/reject | 全栈 | [crud.py:500-518](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/db/crud.py#L500-L518)。建议加 `decision` 字段（approved/rejected/ignored） |
| P2-4 | [B] | `add_model_group_member` 无 UNIQUE 约束，并发可能重复 | 全栈 | [crud.py:356-388](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/db/crud.py#L356-L388) + [db/models.py:280-295](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/db/models.py#L280-L295) |
| P2-5 | [D] | `cost_estimate` 字段从未被写入，`get_group_routing_stats` 聚合永远为 0 | 全栈 | [db/models.py:160](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/db/models.py#L160) + [proxy.py:266-301](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/gateway/proxy.py#L266-L301)。**Phase 2 遗留**字段，本 release 不动 |
| P2-6 | [B] | `is_open_source` 三元表达式写法混乱且把 None 视作开源 | 全栈 | [model_sync.py:147](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L147) |
| P2-7 | [P] | `asyncio.get_event_loop()` 在 Python 3.12+ 已 deprecated | 全栈 | [proxy.py:250](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/gateway/proxy.py#L250) + [proxy.py:334](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/gateway/proxy.py#L334)。**下个迭代**升级 Python 时统一修 |
| P2-8 | [D] | `asyncio.create_task` 未保留引用，可能被 GC 回收 | 全栈 | [scheduler.py:148-149](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/scheduler.py#L148-L149)。**设计选择**：scheduler 在 FastAPI lifespan 中管理，task 在 shutdown 时被 cancel，不是真泄漏 |
| P2-9 | [B] | `_build_aimc_headers` 精确路由时未清空残留 `_registry_*` 字段 | 全栈 | [proxy.py:60-77](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/gateway/proxy.py#L60-L77) |
| P2-10 | [D] | `settings_runtime.value` 明文存敏感字段（bark_key 等），与 API-Sec-1 目标不一致 | 架构 | [db/models.py:339](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/db/models.py#L339)。**设计选择**：API-Sec-1 是存账号 API key（需要加密），alert webhook 是另一类（只需授权、不需加密）。文档需明确区分 |
| P2-11 | [D] | `promotions` 表被 Phase 3 复用存模型元信息（schema 语义污染） | 架构 | [healer.py:318-321](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/healer.py#L318-L321)。**设计选择**：复用现有表 + raw_data 携带 kind 标记，避免加新表。与 architecture.md §5 需一致化 |
| P2-12 | [P] | benchmark 采集源实际是空跑（不写 model_cards） | 架构 | [sources/benchmark.py:7-8](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/sources/benchmark.py#L7-L8)。**预留接口**，不需修 |
| P2-13 | [B] | E3 `_group_models_by_id` 标记 async 但无 await | 全栈 | [cross_source_validator.py:111-139](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/cross_source_validator.py#L111-L139)。去掉 async 装饰 |
| P2-14 | [B] | E3 全表扫描 active promotions 无分页/无增量，B1 不修复时表持续膨胀 | 架构 | [cross_source_validator.py:89-90](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/cross_source_validator.py#L89-90) |
| P2-15 | [B] | E2 直接读 alerter 私有 `_settings_cache` 绕过公共 accessor + 缺锁 | 全栈 | [price_alert.py:59-60](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/price_alert.py#L59-L60) |
| P2-16 | [B] | E2 阈值配置错误静默回退（"abc" 被 except 吞掉回退 30% 无 warning） | 全栈 | [price_alert.py:58-65](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/price_alert.py#L58-L65) |
| P2-17 | [B] | E3 `func.now()` 赋值给 ORM 属性且与 onupdate 冗余 | 全栈 | [crud.py:551](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/db/crud.py#L551) |
| P2-18 | [B] | E3 sources_json 归一化为管道字符串但 resolve 期望 JSON 数组，契约混乱 | 架构 | [cross_source_validator.py:39-43](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/cross_source_validator.py#L39-L43) vs [routes.py:666](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/routes.py#L666) |
| P2-19 | [P] | 前端 CSS 颜色硬编码 20+ 处，无设计 token | 前端 | [style.css](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/style.css) 全文 |
| P2-20 | [P] | 前端死 CSS `.price-high` 定义但从未使用 | 前端 | [style.css:132-134](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/style.css#L132-L134) |
| P2-21 | [P] | 前端完全无响应式，多列表格在窄屏溢出 | 前端 | [style.css](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/style.css) 全文无 `@media` |
| P2-22 | [P] | 前端按钮无 `:focus-visible` 样式，违反 WCAG 2.4.7 | 前端 | [style.css:318-341](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/style.css#L318-L341) |
| P2-23 | [P] | 前端动态内容无 `aria-live`，屏幕阅读器不感知更新 | 前端 | [index.html:21](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/index.html#L21) 等 |
| P2-24 | [P] | 前端 htmx 轮询在标签页隐藏时仍持续请求 | 前端 | [index.html:21](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/index.html#L21) 等 `every Ns` |
| P2-25 | [B] | 前端 E3 `(ignored)` 魔法字符串作为 sentinel，未来真值冲突 | 前端 | [app.js:340](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L340) |
| P2-26 | [B] | 前端 E3 `prompt` 默认值 `'T3'` 硬编码，对 capabilities 不适用 | 前端 | [app.js:337](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L337) |
| P2-27 | [B] | E3 测试 monkeypatch 永久污染 crud 模块无 restore | 全栈 | [test_phase4_e3_cross_source.py:119-121](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/tests/test_phase4_e3_cross_source.py#L119-L121) |
| P2-28 | [B] | 测试注释 "Qwen-Flack" 错别字 | 全栈 | [test_phase3_2_parser.py:113](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/tests/test_phase3_2_parser.py#L113) |

---

## 五、架构数据流闭环分析

### 5.1 完整数据流图

```
parser → healer._save_promotion → promotions.raw_data (model_metadata 堆积)
                                         │
                    ┌────────────────────┼────────────────────┐
                    │ P0-1: 未接通       │ E3: 新增读路径     │
                    ▼                    ▼                    
          model_sync.process_promotion   cross_source_validator
          (从未被调用)                   → cross_source_discrepancies
                    │                    │
                    ▼                    ▼
          model_capabilities_log         dashboard resolve
          (E1 审批队列)                  (E3 拍板)
                    │                    │
                    ▼                    ▼ ❌ P1-10: 不回流
          approve → registry.tier        只更新自身表
                    │
                    ▼ ❌ P1-1: 不刷缓存
          路由层 (router.py 仍用旧 tier)
```

### 5.2 关键架构发现

1. **E1（待确认队列）和 E3（跨源分歧）形成两条独立审批流**，互不消费对方决策。E3 检测到 tier 分歧 → 人工拍板 T3 → `model_registry.tier` 仍是占位 T2 → 路由仍按 T2 过滤。整个 E3 流程对路由层零影响。

2. **E3 未解决 P0-1 死循环，反而绕过 `process_promotion` 直接读表**。E1 审批队列（`model_capabilities_log`）仍收不到 AI 推断的 tier/capabilities（因 `process_promotion` 未被调用）；E3 检测到的分歧也无路径回流到 E1。

3. **E2 与既有架构耦合健康**：E2 只读 `PriceChange` + 调 `alerter.send_alert`，不写 DB、不改路由。耦合面最小。throttle key `price_{model}` 与 5xx 的 `{channel}:{status}`、key 轮换的 `key_rotation:...` 命名空间隔离，无冲突。

4. **情报层 → 数据层直写模式扩散**：`model_sync` 和 `cross_source_validator` 都直接 `from db import crud` 写 DB。E3 只写自己的检测表，可接受；但 `model_sync` 直接写路由表 `model_group_members` 破坏了"情报层只采集不参与路由"原则。

---

## 六、亮点（值得保留的好实践）

1. **三档置信度安全阀设计清晰**（[model_sync.py:26-56](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L26-L56)）：JOIN_THRESHOLD=0.8 / LEAVE_THRESHOLD=0.9 把"信心"和"破坏力"解耦
2. **tier 字段永不自动落库**（[model_sync.py:180-190](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/model_sync.py#L180-L190)）：即使高置信度入组，tier 也强制走 `approved=0` log + 人工确认
3. **响应头污染防护**（[proxy.py:37-59](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/gateway/proxy.py#L37-L59)）：通过 `is_group_request` 判断区分组路由/精确路由，避免上次组路由残留的 `_actual_model` 污染下次精确路由响应头
4. **API-Sec-2 runtime override 设计优雅**（[db/models.py:SettingsRuntime](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/db/models.py#L319-L340) + [alerter.py:_settings_cache](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/alerter.py#L48-L82)）
5. **API-Sec-1 诚实评估**（[security/crypto.py:1-21](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/security/crypto.py#L1-L21)）：明确 XOR 非密码学标准、真正威胁模型是"DB 单独泄露"
6. **E2 异常隔离到位**（[pricing_sync.py:125-129](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/pricing_sync.py#L125-L129)）：告警失败不阻断已 commit 的价格同步主流程
7. **E3 与 model_sync 解耦**（[cross_source_validator.py:11](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/cross_source_validator.py#L11)）：明确"只做发现，不直接改 model_registry"，避免引入新的 P0-5 类问题
8. **E3 数据稀疏保护**（[cross_source_validator.py:162-163](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/intelligence/cross_source_validator.py#L162-L163)）：`MIN_SOURCES=2` 避免单源误判
9. **前端 XSS 防护意识到位**：[app.js:7-12](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L7-L12) 提供统一的 `esc()` 转义函数
10. **前端 API 契约 100% 对齐**：前端调用的所有 URL（含 E3 新增 3 个）与 [routes.py](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/routes.py) 中的路由路径、方法、字段全部一致
11. **前端 `runCrossSourceDetection` 是正确的按钮状态范式**（[app.js:370-389](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L370-L389)）：同文件其他按钮应对照复用
12. **前端 `<details>` 原生折叠**（[app.js:161](file:///C:/Users/lixia/.qoder-cn/worktree/aimc/76apjT/dashboard/static/app.js#L161)）：免 JS、自带键盘/屏幕阅读器支持

---

## 七、合并建议

> **修订记录 (2026-08-07)**：以“修复后业务贯通”为依据重新判断。原报告给 Phase 3.3 全部 P0+  "建议补齐后合并"，现调整为 **Phase 3.3 核心 P0-1/P0-2 是合并门禁，其他是本 release 内必须修**。

| 模块 | 建议 | 理由 |
|------|------|------|
| **Phase 4.E2 价格告警** | ✅ 可独立合并 | 架构健康，注入位置合理，异常隔离到位。需后续顺手修 P1-8/P1-9（throttle key + 消息加 provider） |
| **Phase 4.E3 检测侧**（detect_and_record + scheduler + table） | ✅ 可合并 | 检测逻辑完整，upsert 设计合理（但需先修 P0-7 UNIQUE 约束） |
| **Phase 4.E3 拍板侧**（resolve API + 前端 UI） | ⚠️ 需补 resolve 回流 | resolve 需写 model_capabilities_log + refresh_cache（P1-10）后才能让决策生效；前端 P0-6 SRI + P1-17 颜色反转 + P1-16 按钮禁用是必须修 |
| **Phase 3.3 情报联动** | 🔴 合并门禁 | P0-1（未接通） + P0-2（tier 语义） + P0-4（出组安全阀）不修则 "情报→入组" 核心承诺不成立；不修不能并 |
| **P0-6 (前端 SRI)** | 🔴 立即修复 | Dashboard 当前根本打不开，所有功能不可用 |
| **P1-1 / P1-10 / P1-7 / P1-8 / P1-11 / P1-12** | ⚠️ 本 release 修 | 决策落地 + 可观测性 + E2/E3 鲁棒性 |

---

## 八、修复优先级建议

> **修订记录 (2026-08-07)**：按“修复 ROI（落地后能打通什么）”重排。原报告"7 项 P0 全中后期"过于均势，实际 B6 是独立错（Dashboard 状态）应最先修；B2 是单一根本问题其后修；B1 是独立链间问题；P0-3 严重度因 B2 修复而下降。

| 优先级 | 问题编号 | 修复后收益 |
| | | |
| | | |
| **🔴 P0 立即修（合并门禁）** | P0-6 (SRI) | Dashboard 从打不开 → 可访问，6 个 section 才能验证 |
| **🔴 P0 立即修** | P0-2 (tier 语义) | AI 推断、跨源验证、seed、doc 4 处统一，避免“推断错→全部错” |
| **🔴 P0 立即修** | P0-1 (接通) | Phase 3.3 核心入口走通，promotions.raw_data → registry 闭环 |
| **🔴 P0 立即修** | P0-4 + P0-5 + P1-4 | 安全阀生效 (不重新入组 deprecated) + 审计完整 (ctx/modality 走 log) + tier log 不刷屏 |
| **🟡 P0 本 release 修** | P0-3 (占位 tier) | B2 修复后是唯一遗留路由问题 |
| **🟡 P0 本 release 修** | P0-7 (UNIQUE 约束) | E3 鲁棒性，是表层问题修起来快 |
| **🟡 P1 本 release 修** | P1-1 (approve 刷缓存 + 迁移组) + P1-10 (E3 resolve 回流) | E1/E3 决策落地，不修等于“审批只设 approved=1，不生效” |
| **🟡 P1 本 release 修** | P1-7 (log 输出顺序) + P1-8 (throttle key) + P1-11 (session rollback) + P1-12 (resolve API 校验) | 可观测性 + E2/E3 鲁棒性，1h 搞定 |
| **🟢 P2 本 release 顺手** | P2-2 / P2-3 / P2-4 / P2-6 / P2-9 / P2-13 / P2-14 / P2-15 / P2-16 / P2-17 / P2-18 / P2-25 / P2-26 / P2-27 / P2-28 | [B] 标记的小问题，随手修 |
| **🔵 设计选择** | P1-2 / P2-1 / P2-5 / P2-8 / P2-10 / P2-11 | 文档/注释一致化即可 |
| **⏭️ 下个迭代** | I5 / I6 / I8 / P2-7 / P2-19 ~ P2-24 | 需重构或 polish |

---

## 九、补充：测试盲区分析

所有 Phase 3 / 4 测试都用 mock DB 且不实例化 RouteEngine，无法发现路由层冲突。这是测试设计上的系统性缺陷。

**建议补一个真正的 e2e 测试**：mock 情报源 → healer → model_sync → registry/members 写入 → route_engine.refresh_cache → `_select_by_group` 命中 —— 覆盖目前测试盲区。当前所有 Phase 3 测试通过 ≠ 集成正确。

具体测试用例建议：
1. AI 推断新模型 tier=T0（旗舰） → 自动入组 → 验证路由能命中该 model
2. 模型下线（confidence ≥ 0.9） → 自动出组 → 验证路由不再命中
3. 价格变动 ≥ 30% → 验证告警触发
4. 跨源 tier 分歧 → E3 检测 → 人工拍板 → 验证 registry.tier 更新 + 路由缓存刷新
5. dashboard approve tier log → 验证 model 从旧 tier 组迁移到新 tier 组

---

## 十、评审元信息

- 评审依据：架构文档 v2.1 + 实际代码 + 测试用例
- 评审代码量：约 7700 行新增（52 文件）
- 评审方式：三方并行（架构师 / 全栈开发 / 前端代码评审）
- 评审性质：首轮只读评审，未修改任何业务代码
- 评审分支：release/phase3-4-phase4-e1
- 评审 HEAD：b2611c6
- 评审工具：LS / Read / Glob / Grep + git log/show/diff

---

## 十一、修订与回应 (2026-08-07)

**修订者**：开发本人（评审方原为架构师+全栈+前端三方，2026-08-07 由开发本人重新核对后修订）

**修订动机**：
1. 原报告以"上轮未修复"为基调，但本分支是首轮评审，并无"上轮"基线 → 措辞调整
2. 原报告描述的 B3 推理链不成立（AI 推断 T0 → flagship tier=NULL → 路由放行；只有 strong/balanced/light 三组受影响）→ 严重度调整
3. 原报告未区分"真 bug / 设计选择 / polish" → P2 项分类标记
4. 原报告 P1-2（自动出组不删 members）描写为"违反架构"，实际是我们的设计选择 → 状态调整
5. 原报告总体评级过于严苛（SRI 问题拖低全盘）→ 评审维度分开评级

### 修订项汇总

| 原描述 | 修订后 | 理由 |
|--------|--------|------|
| §一"首轮评审 / 上轮核查表" | "首轮评审基线清单" | 本分支为首轮，无上轮基线 |
| §二 架构 中等偏下 / 后端 B- / 前端 D | 架构 B / 后端 B / 前端 C | 描述平衡 (SRI 是前端独立问题，不应拉低后端评级) |
| §2.1 "B1+B2+B3 三重缺陷互相独立" | "B2 是单一根本问题，B3 推理链不成立" | flagship tier=NULL 放行，B3 严重度仅限 3 个组 |
| §三 表格 "未修复" 措辞 | "确认成立 / 部分成立 / 设计选择 / 待验证 / 下个迭代" | 反映不同处理优先级 |
| §4.3 P2 未分类 | 加 [B] / [D] / [P] 分类标签 | 28 项不能全部当 bug 处理 |
| §七 合并建议 | "Phase 3.3 是合并门禁" 明确化 | 原描述过于笼统 |
| §八 修复优先级 | 按 ROI 重排 | 原报告均势，实际 B6/B2/B1 是顺序依赖 |

### 原报告依然有效的部分

以下论断原始报告与开发人员均认为成立，不需修订：
- §四 P0-1 / P0-4 / P0-5 / P0-6 / P0-7 全部 ✅
- §四 P1-1 / P1-7 / P1-8 / P1-9 / P1-10 / P1-11 / P1-12 全部 ✅
- §四 P1-13 / P1-14 / P1-15 / P1-16 / P1-17 (前端) 全部 ✅
- §五 架构数据流闭环分析 全部 ✅
- §六 亮点 全部 ✅

### 后续动作

按第十一节修订后的优先级，以 9 项 must-fix 为首批修复项：
1. P0-6 SRI 哈希
2. P0-2 Tier 语义统一
3. P0-1 接通 process_promotion
4. P0-4 + P0-5 + P1-4
5. P0-3 占位 tier
6. P0-7 UNIQUE 约束
7. P1-1 approve 刷缓存 + 迁移组
8. P1-7 + P1-8 + P1-11 + P1-12
9. P1-10 E3 resolve 回流

具体实施计划见同步创建的 `docs/fix-plan-phase3-4-phase4-e1.md`。

---

**首轮评审完成 (2026-08-06) + 修订与回应 (2026-08-07)**。本报告以首轮评审为基准，覆盖 Phase 3.1-3.4 / Phase 3.A-E / Phase 4.E1-E3 全部新增代码，共发现 7 项 P0 + 18 项 P1 + 28 项 P2 问题。修订后，7 项 P0 中 6 项需本 release 修复（P0-3 在 P0-2 修复后严重度下降），18 项 P1 中 6 项本 release 必修。修复实施计划见 `docs/fix-plan-phase3-4-phase4-e1.md`。
