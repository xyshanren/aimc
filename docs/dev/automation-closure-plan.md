# AIMC 自动化收口与远期演进计划 (V6.3+)

> 状态: **V6.3 已上产（2026-09-05，全量 689 测试全绿）**；观察期运行中（F3 埋点 ≥2 周后决策 R4）
> 日期: 2026-09-05
> 背景: V6.x 上产后（全量 682 测试全绿），下一步计划四项（Phase B 观察期收尾 / 请求日志 model 搜索后端 / V7.0+V6.2 待拍板 / 自动化缺口收口）与自动化收口三路径（A/B/C）综合规划
> 前序: [next-major-v6-plan.md](../next-major-v6-plan.md)（V6.0/V6.1/V6.x 已完成）；本文承接其 §4 路线图

---

## 0. 需求清单

| # | 需求 | 来源 | 优先级 | 状态 |
|---|------|------|--------|------|
| R1 | 首启检测：空渠道时给出部署引导（路径 B） | 自动化收口路径 B | 高 | **✅ V6.3** |
| R2 | 请求日志 model 搜索后端 | 2026-09-05 看板补齐遗留 | 高 | **✅ V6.3** |
| R3 | 自动入组数据积累埋点（路径 A 前置） | 用户指示：先积累数据再定策略 | 高 | **✅ V6.3**（观察期） |
| R4 | AUTO_JOIN_NON_T0 策略决策点 | 用户指示（数据驱动，**不立即开**） | 中 | 观察后决策 |
| R5 | 看板新增采集源 + 自定义 tier 规则（路径 C） | 自动化收口路径 C | 低（V7 级） | 待拍板 |
| R6 | V7.0 yield + Phase C 其余（project/session/guard/compare） | next-major-v6-plan §4 | 低 | 待拍板 |
| R7 | V6.2 MCP server | next-major-v6-plan §4 | 低 | 按需 |
| R8 | 操作审计系统 | V4.x backlog（公网/多用户前必修） | 中 | 待拍板 |
| R9 | 采集失败恢复策略 Phase 1-3 | V4.x backlog §十一 | 中 | 观察后决策 |
| R10 | P1-1..P1-4 + 断链 #5/#8/#10/#11/#12 | V4.x backlog | 低 | 按需 |
| R11 | 看板图-表联动（环图↔渠道表、柱图↔日表） | 2026-09-05 用户提出 | 中（体验优化） | **✅ V6.3** |

---

## 1. 背景与问题

### 1.1 现状（V6.x 之后）

- 采集+入库已全自动：35 个代码注册源（`intelligence/sources/__init__.py get_all_sources()`），scheduler 自动 upsert `intel_sources`，9 个 API model_list 源 confidence=1.0 免解析、无渠道 key 静默跳过、V4.8.6 无渠道置 inactive 自动回翻、自适应降频（no_change ≥4→4x、≥10→8x）。
- 组路由自动化的唯一缺口：**T0 自动入组，T1-T3 落 pending_review 等人工审批**（`intelligence/model_sync.py:31 AUTO_JOIN_NON_T0 = False`，与 P0-3 docstring 设计一致）。
- 首次部署仍需手工跑 `scripts/manual_import.py` 或看板加渠道；采集源不可在看板新增（只支持 run-now / toggle / interval）。
- 看板请求日志（`GET /dashboard/api/requests`，dashboard/routes.py:1827）支持 days/status/分页，**model 过滤仅前端筛选当前页，后端未实现**。

### 1.2 自动化收口三路径（原始提案）

| 路径 | 内容 | 成本 | 本文处理 |
|------|------|------|----------|
| A | 开 `AUTO_JOIN_NON_T0=True`，T1-T3 全自动入组 | 开关一行 | **改造为数据驱动**（见 §3）：先积累数据（R3），分析后决策（R4） |
| B | 首启检测空渠道 → 日志 + 看板引导，manual_import.py 降级为可选捷径 | ~半天 | 近期实施（R1） |
| C | 看板新增采集源 endpoint + 自定义 tier 规则 | V7 级 | 远期并入 V7.0 范围（R5） |

### 1.3 T1-T3 自动入组的机制盘点（已确认）

- `JOIN_THRESHOLD = 0.8`（model_sync.py:27）、`LEAVE_THRESHOLD = 0.9`（:28）。
- `_TIER_TO_GROUP`（:35）：T0→tier:flagship、T1→tier:strong、T2→tier:balanced、T3→tier:light。
- 现状分支（:90-91）：`if AUTO_JOIN_NON_T0 and inferred_tier in _TIER_TO_GROUP`；关闭时 T1-T3 走 pending_review（model_capabilities_log approved=0）。
- 自动出组已有 0.9 兜底（:126，auto_left 直接 approved=1 生效）——即使开自动入组，出组风险也已受控；**入组风险是错分（infer_tier 依赖参数量正则：≥200B→T0 / ≥70→T1 / ≥20→T2 / ≥7→T3，无参数量 → None → pending_review）**。
- T4 永远手工。盲区：无参数量的新模型全部积压 pending_review，这正是需要数据回答的问题。

---

## 2. 落地清单

### 2.1 近期（下一版本，建议 V6.3）

**F1 首启检测（R1，路径 B，~半天）**
- `main.py` startup：channels 为空时 log WARN + 写一条系统提示（或看板横幅），引导两条路：看板「账号管理」加渠道（V6.1 POST /api/accounts 已有）或 `python scripts/manual_import.py`。
- 文档同步：user/install.md 把 manual_import.py 从必做降级为可选捷径。
- 验收：删掉 data/aimc.db 冷启动，日志/看板出现引导；配置一个渠道后提示消失。

**F2 请求日志 model 搜索后端（R2，~半天）**
- `GET /dashboard/api/requests` 增加 `model: str = Query(None)`，`UsageLog.model LIKE %model%` 模糊匹配，与 days/status 组合。
- 前端请求日志筛选区把现有前端过滤改为传参（保留当前页兜底）。
- 验收：?model=glm 只返回 glm 系行；与 status=error 组合正确；分页 total 相应收窄。

**F3 自动入组数据积累埋点（R3，~1 天）**
- 目标：回答「开 AUTO_JOIN_NON_T0 会发生什么」需要的四组数据，全部从现有表/事件推导，不改路由行为。
- 采集口径（看板「运维」页新增一张「自动入组观察」卡，或复用 pending-summary 数据源）：
  1. **pending_review 积压量与增速**：model_capabilities_log approved=0 按周计数（现有 pending-summary API 已有部分，补周趋势）。
  2. **tier 分布**：capability_log / promotions 里 AI 推断 tier 的分布（T1/T2/T3 各多少、confidence 分布）。
  3. **审批负担**：每周人工 approve/reject 次数（pending-review API 已记录，补统计）。
  4. **路由命中率**：tier:strong/balanced/light 各组实际请求量（usage_logs 按组统计已有 group 维度），评估「错分入组后会被多少真实流量踩到」。
- 附带补盲区数据：infer_tier 返回 None（无参数量）的模型占比——决定策略是否需要先补参数量数据源。
- 验收：观察卡能看到以上 4+1 指标的周趋势；不改变任何入组行为（纯只读统计）。

**F7 看板图-表联动（R11，~半天，可与 F2 同批或并入 F3 之后的体验批）**

现状盘点（全部 4 张图表 + 毗邻表格，数据源均为同一次 fetch 的 by_channel / 日汇总）：
- **Cache 节省环图 ↔ 按渠道表**（loadCacheStatsList，app.js）：环图与渠道表共用 `by_channel`，联动收益最直接。
- **每日请求柱图 ↔ 用量日表**（loadUsageChart，app.js）：柱图与日表共用 `usage/summary` 数据。
- **概览趋势线图**（loadOverviewTrend）：双系列（标价/实付）无对应表格，**不做联动**。
- **用量页其余卡片**（Top 模型 / 分组概览）：已是纯表格，无图表可联动。

设计（Chart.js 4.4.0 已内置，无新依赖）：
- 环图 hover → 高亮渠道表对应行（`tr.hl` class）；表格行 hover → 环图 `setActiveElements` 高亮对应扇区。
- 柱图 hover → 日表对应行高亮；反向同理。
- 纯前端 class 切换 + setActiveElements，两端都为空态时静默跳过；不新增请求、不改后端。
- 验收：hover 环图扇区渠道表行高亮、hover 表行扇区亮起；柱图↔日表同理；深浅主题高亮色均可见。

### 2.2 中期：策略决策点（R4，F3 数据积累 ≥2 周后）

**触发条件**：F3 上产且积累 ≥2 周数据。
**决策输入**（届时出一份一页分析）：pending 积压增速 vs 审批负担、T1-T3 推断 confidence 分布、错分代价评估（各组流量踩雷面）。
**策略候选**（按数据表现选择，非互斥）：
- S1 全开：`AUTO_JOIN_NON_T0=True`（若 pending 量小、confidence 高、错分面小）。
- S2 半开：仅 T2/T3 自动（轻量组错分代价低），T1 保持人工。
- S3 加门槛：新增 `AUTO_JOIN_MIN_CONFIDENCE`（如 0.9），置信度够高才自动，其余仍 pending。
- S4 维持现状：pending 不构成负担则继续人工（半自动哲学本来允许）。
**决策记录**：写入本文 §5。

### 2.3 远期（V7.0 框架下，待拍板）

**F4 路径 C：看板新增采集源 + 自定义 tier 规则（R5）**
- 新增采集源 endpoint（POST /api/sources 自定义 provider+url+解析方式）+ infer_tier 自定义规则（覆盖参数量正则盲区）。
- 与 R3 埋点联动：自定义规则可消掉 None 盲区，是 S3 门槛策略的配套。
- 体量 V7 级（sources 表结构 + 调度注册动态化 + 规则引擎），与 V7.0 yield/Phase C 同批立项。

**F5 V7.0 其余（R6）**：yield（git 关联收益追踪）+ Phase C 其余（project/session 维度、guard 预算守护、compare 性价比对比），风险中高，按 next-major-v6-plan §4 原范围。

**F6 V6.2 MCP（R7）**：按需，REST/CLI 已满足 agent 需求（V6.1 §3.5 决策不变）。

### 2.4 V4.x 孤儿 backlog 排序（R8-R10）

| 项 | 建议槽位 | 理由 |
|----|----------|------|
| 操作审计系统（audit_log + middleware 钩子，~1 天） | V6.3 可选 / 公网部署前必修 | 若近期仍纯内网自用，可继续观察 |
| 采集失败恢复 Phase 1-3（失败分类停调度 / 看板恢复入口 / 429 降频） | F3 数据积累期顺带（观察后决策） | 429 降频已有自适应基础 |
| P1-2 API 抽取直连 model_sync | 与 F3 同批（降低 pending 噪音） | 跳过 AI 重解析，直接提升自动入组可信度 |
| P1-1 differ 先 clean 再 hash / P1-3 score_bonus 接路由 sort_key / P1-4 promotions 拆表 | 按需 | 独立小项 |
| 断链 #5 cross-source-list 容器 / #8(并入 P1-2) / #10 Chart.js 挂载 / #11/#12 cache 权重+cached_input_price 参与路由 | 按需 | #11/#12 与路由策略相关，可并入中期决策 |

---

## 3. 路线图

```
V6.3 (近期, 待拍板后 ~2-3 天)
  ├─ F1 首启检测 (路径 B)
  ├─ F2 请求日志 model 搜索后端
  ├─ F3 自动入组数据积累埋点 (+ P1-2 可选同批)
  └─ F7 看板图-表联动 (体验, ~半天)
        ↓
观察期 ≥2 周 (不改行为, 只攒数据)
  ├─ 每周看板巡检: pending 增速 / tier 分布 / 审批负担 / 组命中率
  └─ 顺带决策: 采集失败恢复 Phase 1-3 是否立项
        ↓
V6.4 / 中期决策点
  ├─ 出一页分析 → S1/S2/S3/S4 择一 (AUTO_JOIN_NON_T0 策略)
  └─ 决策记录回写本文 §5
        ↓
V7.0 (远期, 拍板后立项)
  ├─ F4 看板新增采集源 + 自定义 tier 规则 (路径 C)
  ├─ F5 yield + Phase C 其余 (project/session/guard/compare)
  └─ F6 V6.2 MCP (按需)
```

## 4. 验收总则

- V6.3 三项（F1/F2/F3）：新增/改动均补 pytest；全量测试维持全绿；部署 VM 后按各 F 验收条款逐条过。
- 观察期：无行为变更，只读统计；看板新卡不引入 N+1（沿用两遍查询惯例）。
- 中期决策：只改一个开关/阈值 + 决策记录，禁止夹带其他改动。

## 5. 决策记录

| 日期 | 决策 | 状态 |
|------|------|------|
| 2026-09-05 | 路径 A 改造：不立即开 AUTO_JOIN_NON_T0，先 F3 积累 ≥2 周数据，分析后按 S1-S4 择一 | **用户指示，生效** |
| 2026-09-05 | 路径 B（F1）+ model 搜索（F2）+ 埋点（F3）列入近期 | **生效（V6.3 上产）** |
| 2026-09-05 | 路径 C 并入 V7.0（与 yield/Phase C 同批），不单独提前 | 待拍板 |
| 2026-09-05 | 操作审计：内网自用继续观察，公网/多用户前必修 | 待拍板 |
| 2026-09-05 | 看板图-表联动（F7）纳入 V6.3 体验项：环图↔渠道表、柱图↔日表做双向 hover 联动；趋势线图无对应表格不做 | **生效（V6.3 上产）** |
| 2026-09-11 | **S3 生效**：AUTO_JOIN_NON_T0=True + 新增 `AUTO_JOIN_MIN_CONFIDENCE=0.9`（非 T0 自动入组门槛；T0 恒免人工不受约束）。决策依据（观察期收口，提前于 9/19）：pending 恒 0 无审批负担、组命中 96.5%、盲区 33.3%→18.2% 良性（2 个低流量 SenseNova 模型）、kimi-k2.6 两次推断 T1→T2 不一致支持置信门控。不达标走既有 pending 人工路径 | **用户拍板，生效（V6.4 上产）** |
| 2026-09-11 | R9 采集失败恢复 Phase 1-3 **不做**：告警频率观察达标（9/9 全天仅已知坏源告警，无新退化）；8 个持续 error 死源改为运维禁用处置（qwen/aliyun ×3、deepseek api ×2、hunyuan、sensenova news、SF pricing 旧 URL） | **用户拍板，生效** |
| 2026-09-11 | #11/#12 cache 权重 + cached_input_price 进路由：合并为一步落地 — `_get_input_price` 按渠道 7d 实测命中率折算有效输入价（hit=0 退化原价，行为不变） | **用户拍板，生效（V6.4 上产）** |
| 2026-09-11 | 反馈公式 retry 项：**保留双分现状**（feedback_score + feedback_score_no_retry 并存），观察满 2~4 周（~2026-09-23~10-07）后再取舍 | **用户拍板** |
| （待补） | 思考注入各渠道开谁（Phase B 已上产默认全关） | 看一周数据后用户定 |

## 6. 实施记录

### V6.3（2026-09-05 上产，全量 689 测试全绿）

**F1 首启检测（R1 / 路径 B）**
- `gateway/app.py:_check_empty_channels()`（startup 钩子调用）：无渠道 → WARN 引导（看板「账号管理」/ `manual_import.py` 两条路）；有渠道无 active 账号 → WARN 引导加账号；就绪时静默。
- 文档：`user/install.md` 第 5 步补首启引导说明（manual_import 降级为可选捷径）+ 故障表新增「首启引导」行。
- 测试：`test_v63_auto_join_metrics.py` 3 例（空库 / 有渠道无账号 / 就绪静默，caplog 断言）。

**F2 请求日志 model 搜索后端（R2）**
- `dashboard/routes.py GET /api/requests` 新增 `model` 参数（`UsageLog.model LIKE %model%`），与 days/status 组合，total 同步收窄；前端请求日志筛选区已改为传参。
- 测试：`test_v62_request_logs.py::test_requests_list_model_filter_v63`（glm 收窄 / 精确匹配 / 与 status 组合 / 回显 / 无命中）。
- V6.3.3 扩展（2026-09-09）：同一端点再加 `channel_id` / `key_id` 精确过滤 + 前端两个下拉（选项复用 /api/channels 与 /api/keys，once 加载；Key 消歧规则与列表 key_label_map 同源，已吊销 key 标注保留）；用户维度不加独立筛选（key 唯一归属用户，按人下钻走按用户 tab）。测试：`test_requests_list_channel_key_filter`。

**F3 自动入组数据积累埋点（R3，纯只读）**
- `dashboard/routes.py GET /api/ops/auto-join-metrics?days=14`：5 组指标 —— ① pending 积压（当前 + 0-7d/7-14d 周桶）② tier 分布（ai_infer tier 行 GROUP BY new_value）③ 审批负担（近 N 天 approved=1 处理量；approve/reject 行级同值不可拆分，报合并量并注明）④ 组路由命中（usage_logs GROUP BY group_id，tier:* 占比 vs NULL 精确路由）⑤ infer_tier 盲区（metadata 行 new_value JSON 里 inferred_tier=null 占比）。
- 扩展（2026-09-11）：新增顶层键 `rag_embedding_rerank` {count, pct, note} —— rag_demo 的 /v1/embeddings、/v1/rerank 流量自 2026-09-09 起直连 aimc，usage_logs.model 存上游 raw 名（`BAAI/bge-m3`、`BAAI/bge-reranker-v2-m3`），按 raw 名单列标注；属非 LLM 计量面，对比 LLM 调量曲线（如 R6-1 检索前改写上线前后）时请扣除，total 口径不变。周报总览同步加"其中 rag_demo embedding/rerank"行（`scripts/generate_weekly_report.py`，可选参数不破坏 2 参调用）。
- 看板「运维」页新增「自动入组观察」卡（60s 轮询 + 手动刷新），每指标带阈值 badge（积压 >20 红 / 盲区 >30% 红），脚注指向本文档。
- 行为零变更：AUTO_JOIN_NON_T0 保持 False，响应里 `auto_join_enabled: false` 快照。
- 测试：`test_v63_auto_join_metrics.py` 3 例（全量种子 / 空库 / 非法 JSON 跳过）；扩展后断言 rag count==2 / pct==33.3（全量种子）与 count==0（空库）。

**F7 看板图-表联动（R11）**
- `app.js _chartTableLink(chart, tableEl)`：图 onHover → 表行 `tr.hl`；表行 mouseenter → `setActiveElements` + tooltip 定位，mouseleave 复位。接入两处：Cache 环图↔按渠道表、每日请求柱图↔用量日表（概览趋势线图无对应表格，按设计不做）。
- `style.css` `tbody tr.hl td`：`var(--bg-elevated)` 背景 + `inset 2px 0 0 var(--color-info)` 左描边，深浅主题均取变量。
- 纯前端实现，无新请求、无后端改动。
