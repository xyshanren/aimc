# 下一阶段规划调研（2026-09-13）

> 受众：决定 AIMC 下一步投入方向的维护者
> 方法：汇总① 现行路线图（[next-major-v6-plan.md](../next-major-v6-plan.md) /
> [dev/automation-closure-plan.md](automation-closure-plan.md)）、② 观察清单
> （见 §4 与 automation-closure-plan.md §5 决策记录）、
> ③ 本次模块间结合诊断（[module-integration-audit.md](module-integration-audit.md)）、
> ④ **生产审查报告（[production-review-2026-09-13.md](production-review-2026-09-13.md)）** 派生的候选工作。
> **本文是调研与建议，不是拍板**；V7.0 / V6.2 等按既有规则等用户口令。

---

## 0. 结论先行

1. **功能路线图（V6.0→V6.4.1）已全部上产并验收**，短期没有「必须做」的功能缺口。
2. 结合诊断 + 生产审查（含生产库实证）暴露出**一批「稳定性/接线/口径」问题**——不新增功能，
   但影响「现有功能是否真在生效」「看板数字是否可信」。建议打包为**稳定性批次（暂称 V6.5）**，
   成本低于任何新功能。
3. **必须处理的确定性缺陷**：失败链响应 `Content-Length` 损坏（S1，已实证）、情报核心链
   「优惠→价格行」生产 0 产出且一旦触发会大面积错价（S3）、**429 隐形拒绝**（2026-09-14 生产实证：
   配额预检把 deepseek_01 恒拒死且拒绝不落库不 failover → tier:flagship 兜底链失效；
   修复 3 项已商定并**当日全部上产**（2026-09-14），见 §2「⚡ 已上产」小节）。
4. **两个真正的用户决策项**：本地限流与账号配额是否接线（S2/A1）。不接也能跑（反应式 429→冷却
   已证明够用），但应把「恒放行」显式化，避免后人误读为护栏。
5. **V7.0（yield + Phase C）与 V6.2（MCP）继续等口令**；F4（看板自定义采集源 + 自定义 tier 规则）
   并入 V7.0。
6. **观察线**按既有截止点收口（最近一个是反馈公式 retry 项，约 2026-09-23~10-07）。

---

## 1. 当前基线（2026-09-13）

- 版本：V6.4.1（时区统一）+ 看板统计口径统一已在本地 master（`c0f57d8`）。
- 测试：**750 全绿**（80 个文件含 conftest）。
- 生产：VM `ubuntu2401`，user 级 systemd `aimc.service`，WSL 内 `ssh ubuntu2401` 运维。
- 情报：37 个源（32 active / 8 disabled），42 个 scheduler job。
- 看板：67 个 API + 6 页前端。

---

## 2. 从结合诊断与生产审查派生的候选工作（建议批次）

> 来源标注：`[审查]` = [production-review-2026-09-13.md](production-review-2026-09-13.md)；
> `[结合]` = [module-integration-audit.md](module-integration-audit.md)。
> 严重度以审查报告的 S1~S12 编号为准。

### ⚡ 已上产（2026-09-16）：sensenova 体量准入 + TPM/RPM 冷却基数

**根因（直连探测 4 轮 + 用户控制台截图定量）**：9/13 起 sensenova 上游 429001 近乎全挂
（9/13-9/16 共 55 次尝试 43 次 429），不是账号积分问题——两账号通用池 5h 窗口可用
94.6%/100%、周余额 488k/599.6k（额度 600k）仍被拒。真实约束 = **免费公测池的共享吞吐
TPM**：同一秒同 key 下 84 token 请求 200、110k token 429（0.5~1.2s 立即返回）；完全静默
300s 后 6k 仍挂；阈值不单调（2k 挂/6k 过/12k 挂）= 抢共享池瞬时余量。独立闸门：**RPM
≈8 次/分**（`code 8 / quota_exceeded_error / rpm exhausted`，与 429001 是两回事）。
上游响应无任何 `x-ratelimit-*` 头。生产影响：sensenova 的 deepseek 价行 0.0 → cheapest
恒第一 → 每个旗舰请求先白跑一趟 429（600~800ms）再 failover；9/12 起 deepseek-v4-pro
成功率仅 21%（16/76）。

**处置（两项，同日上产）**：
1. **路由体量准入**：`config.channel_max_input_tokens`（渠道名→估算输入 token 上限，
   默认 `{"sensenova_official": 30000}`，env `AIMC_CHANNEL_MAX_INPUT_TOKENS` JSON 可调）。
   `router.select_best_channel` 在频限过滤前按 `_estimate_input_tokens`（文本 ~3 字符/
   token，图片 1000/张，宁大勿小）剔除超限候选；**全部候选都超限时保留原候选**
   （宁试一次也不 503，共享池偶发余量窗口如 9/16 10:48 三连 107k 仍能接住）。
2. **TPM/RPM 类 429 冷却基数 30s→300s**：`error_classifier` 对 429 且 code 含 `429001`
   或 code+message 含 `tpm`/`rpm` 的 rate_limited 给 `300s/600s`（指数退避与
   `health_probe_max_backoff=3600` 封顶不变，连续失败 300→600→1200→2400→3600）；
   其他瞬时 429（glm 1305 过载波纹等）保持 30s/60s。依据：实测 63% 的 429 后面跟着
   429、静默 5 分钟不恢复，30s 基数解冻即重撞。

**测试**：新增 `tests/test_v66x_size_admission_tpm_cooldown.py` 34 项（估算器 8 + 准入
路由 13 + 分类 3 + 冷却序列 2 + 既有行为回归），全量 840 全绿。
**明确不做**：按时段限流（工作日 9-18 更易限流的假设被簇级 bootstrap 证伪：剔 1 个簇即
消失、簇数 7≈暴露期望 6.2、CI 跨 0）；渠道 disabled（会丢掉偶发大窗口的免费额度）。

**补充（2026-09-16 同日，用户拍板）：准入补下界**——taotoken 按**调用次数**计费
（~2000 次/5h + ~15000 次/周）而 sensenova 只限制大请求，故最优分配是小请求走
sensenova、大请求走 taotoken：新增 `channel_min_input_tokens`（默认
`{"taotoken_coding": 30000}`），窗口语义改为 [min, max]，**准入判定移到频限+配额过滤
之后**（窗口内无候选时保留全部，sensenova 冷却期小请求自动由 taotoken 兜底，不 503）。
数据注记：池子饱和期 sensenova 小请求（<30k）成功率仅 6/69≈9%，小请求会先试 sensenova
失败再落 taotoken（300s 冷却自限重试频率），池子恢复后才能真正吃到免费额度。

### ⚡ 已上产（2026-09-14 商定并当日完成，优先于下方所有批次）：429 隐形拒绝修复

**现象**：tier:flagship 组的请求（zcode 调用）在 sensenova_official 被 429 限流期间，两个
sensenova 账号（lixiaoy / sensenova_01）被轮流反复撞击；备用成员 deepseek-flash 被路由选中
3 次却从未真正发出上游请求——每次选中后 ~50ms 内被网关自己 429，客户端拿到看板完全不可见的
隐形 429。glm_01 账号已禁用、glm-5.3 不接盘 → 兜底链仅剩被拒死的 deepseek-flash，整条链等于没有兜底。

**三根因（叠加）**：

- **A（主因）配额预检拒死**：`gateway/routes.py:186` 转发前配额预检，判定式
  `已用token + max_tokens×2 > 限额`。deepseek_01 账号有一条 `tpm=100000` 限额行（8/14 建库时
  拍脑袋估算，非真实上游契约——DeepSeek 实际按并发限流）；token 累加从未落库 → 已用恒 0 →
  判定退化为 `max_tokens×2 > 100000`；zcode 声明 output 128000 → 恒成立 → deepseek_01 永久封死。
  且该拒绝直接 return：**不 failover、不 mark_failure、不落 usage_logs** → 看板完全隐形。
  长期未现形是因为平时 deepseek 系都走 sensenova 0 元，deepseek_official 从未被真正需要。
  > ⚠️ **对 [production-review-2026-09-13.md](production-review-2026-09-13.md) S2 结论的修正**：
  > S2 的「恒放行」只适用于 rpm/concurrent/daily 等维度；**tpm 维度是反面**——max_tokens 预估值
  > 参与判定，在「大 max_tokens + 小 limit 行」下是**恒拒死且隐形**，比恒放行更危险。
- **B 冷却机制对池级限流无效**：429001 是两账号共享池的 TPM 限制，但 429 冷却按账号固定 30s、
  无指数升级、永不进 needs_human → 两账号轮流解冻，渠道层面永远「看起来有一个可用」。
- **C mark_success 擦除冷却**：流式 200 完成时无条件清零该账号的冷却与失败计数（与审查 S5 同源，
  `proxy._update_cooldown` 200 分支）。池饱和期 200/429 交替出现，冷却永远活不满 30 秒。
  日志实证：23:26:50 429 进冷却 → 23:26:52.98 同账号早先的 200 完成 → 23:26:55 又被选中 →
  23:26:56 再 429。

**商定修复（3 项）**：

1. **预检语义修复**：`check_account_quota` 不过时不再直接拒死请求，改为**跳过该账号、重选下一
   候选**（回归 quota.py 注释里 `try_next_account` 的原设计意图），并补 mark_failure + usage_logs
   落库，让失败可见；
2. **删除 deepseek_01 的 tpm=100000 行**（account_quotas id=8，已拍板选删）——真实防护由上游
   429→冷却承担，不受影响；同表的 rpm 行因累加从未落库属惰性，不动；
3. **sensenova 反复被撞**：rate_limited 冷却改**指数退避**（30s→60s→…封顶）；`mark_success`
   **不再清除未到期的冷却**（保留到期自愈语义）；同渠道两账号都 429 时升级为**渠道级冷却**
   （sensenova 共享池场景）。

**上产记录（2026-09-14 当日完成，3 项全部落地）**：

1. **预检语义修复 ✅**：`gateway/routes.py` 预检拒绝改 `mark_failure(QUOTA_EXHAUSTED, 冷却 3600s)`
   + `usage_logs` 落库（此前该路径零痕迹）+ failed_channels 记录注入响应头；组路由 failover 重选
   （重试用尽才返回 429）；用户级限额仍直接拒绝（换渠道无济于事）。
2. **删 tpm 行 ✅**：生产库 `DELETE FROM account_quotas WHERE account_id=1 AND dimension='tpm'`
   （id=8，行内容留档可回插）。生产 e2e 实证：`deepseek-flash` 直连 200（1.1s 真实转发，
   此前 50ms 隐形 429；上一条该模型 200 还停留在 9/12——印证此前基本封死）。
3. **冷却升级 ✅**：`gateway/cooldown.py`——rate_limited **指数退避**（30s×2^(连续败-1)，
   封顶 `max_probe_backoff_seconds`=3600s，永不 needs_human 保持自愈）；`mark_success`
   **迟到成功守卫**（冷却期内的在途 200 只记 `last_success_at`，不清 count / 不清
   cooldown_until，冷却外成功仍全量复位）；`mark_failure` 新增 `sibling_account_ids`——
   兄弟账号在 rate_limited 冷却中时对齐其剩余时长（共享池渠道级同步），`proxy._update_cooldown`
   与 routes 预检两处接线（`RouteEngine.sibling_account_ids` 取同渠道账号）。
- **顺带（批次一 S5 主修复点）**：routes.py failover 块移除与 `proxy._update_cooldown` 的
  **双重 mark_failure**（连续失败计数翻倍，指数退避下首败冷却即失真）。
- **回归**：新增 `tests/test_v64x_cooldown_quota_failover.py`（26 项）；`test_v46_cooldown.py`
  3 处断言按新语义更新（冷却内迟到成功不再清冷却）；全量 **762 全绿**。

### 2.0 建议的稳定性批次（P0→P1，~1.5~2 天，来源=审查批次一/二）

**批次一（P0）— ✅ 已完成（2026-09-14，V6.5 首批，全量 779 全绿）**

| 项 | 位置 | 状态 |
|----|------|------|
| S5 失败重复 mark_failure | `gateway/proxy.py` + `gateway/routes.py` | ✅ 已随 429 批次完成（8bc0f25） |
| **2.1 退避架空**（审核发现插队） | `gateway/cooldown.py` | ✅ `is_in_cooldown`/`cooldown_remaining` 过期不再 pop 整个 state（置 `cooldown_until=None` + 记 `cooldown_expired_at`），count 供退避延续；`cleanup()` 加 600s 保留窗；超窗 `mark_failure` 视作新系列；`needs_human` 不再被过期查询顺带洗白。`tests/test_v65_cooldown_expiry_retention.py` 8 例（含端到端乒乓升级序列） |
| S1 失败链 Content-Length | `gateway/routes.py` | ✅ 构造重包装响应前 pop `content-length`/`transfer-encoding`/`connection`。`tests/test_v65_failover_content_length.py` 2 例（真实 ASGI 链路断言声明长度==实际长度） |
| S6 周报存储型 XSS | `dashboard/routes.py` 两端点 | ✅ 后端 `html.escape`（前端 DOMParser textContent 自动反转义，marked 链路零改动）。`tests/test_v65_report_xss_escape.py` 2 例 |
| B1 探活 NameError | `intelligence/channel_health.py` | ✅ 补 `ErrorInfo` import + 抽模块级 `_probe_error_info()` 可单测。`tests/test_v65_intel_batch1.py` 5 例 |
| G 部署路径硬编码 | `intelligence/scheduler.py` | ✅ 模块级 `_PROJECT_ROOT` 相对定位替换 `cwd="/home/lxc/aimc"`；删死导入 `suggest_main`（顺带消除 except 求值 UnboundLocalError 隐患）；源码断言防回归 |

**批次二（P1）— 数据正确性 — ✅ 已完成（2026-09-14，V6.5 第二批，全量 789 全绿）**

| 项 | 位置 | 状态 |
|----|------|------|
| S8 路由成功率 LIMIT 截断 | `dashboard/routes.py` | ✅ unmatched 总量改不带 limit 的 `session.scalar(count)`；limit(20) 仅用于 top_models（30d 实证 27>20 已触发）。测试种子 27 精确+5 组路由断言 27/32 |
| S9 cache 命中率时区/实现 | `gateway/router.py` | ✅ cutoff 改 Python 本地时间传参（原 `datetime('now')`=UTC 比对本地 created_at，窗口偏移 8h）；channel 关联改按 `channel_id`（原 name 子查询，重名串号）；缓存 key 同步改 id。tmp sqlite 库断言窗口外行不计入 |
| S4 differ 先落 hash | `differ.py` + `healer.py` + `scheduler.py` | ✅ `has_changed(commit=False)` peek + 新增 `commit_source_hash()`；`handle_parse_result` 返回 bool（AI/CSS 任一入库成功）；scheduler 解析成功才提交 hash，失败保留旧 hash 下轮自动重试 |
| S11 sync_engine PRAGMA | `db/engine.py` | ✅ 补 `busy_timeout=5000`/`synchronous=NORMAL`（job_health 经 sync_engine 写，原缺 → 并发 locked 被 listener 吞） |
| S12 调度器旧 job 残留 | `intelligence/scheduler.py` | ✅ 注册后按本轮新 id 集合清理不匹配 `batch_crawl_*`（改 poll_interval 后旧 interval job 永久残留重复采集）；抽 `_prune_stale_crawl_jobs` 可单测 |

**批次三（P1）— 情报核心链修复 + 验收 — ✅ 已完成（2026-09-14，V6.5 第三批，全量 802 全绿）**

| 项 | 位置 | 状态 |
|----|------|------|
| S3a 折扣乘到全部模型 | `intelligence/healer.py` | ✅ 折扣只作用 `result.models` 命中的模型集合（`hit_models` 过滤）；无模型信息不做价格行（宁缺毋滥）；命中模型无 normal 价行跳过 |
| S3b promotion 永久打折风险 | `healer.py` + `gateway/router.py` | ✅ 双层修复：healer 缺 `end_date` 补抓取日 +7d 保守默认；router `_is_price_effective` 对 `is_promotion=1 且 effective_to=None` 的**存量脏行**拒绝兜底（过期回退 normal） |
| S3c 促销可路由验收线 | `tests/test_v65_batch3_promotion_routable.py` 8 例 | ✅ 合成「半价」样例走 解析→折扣→价行→router 选价 全链断言；反向断言「订阅套餐类（无折扣率）不转价格行」——双轨拍板两边都有验收线 |
| S7 停机 drain | `gateway/proxy.py` + `gateway/app.py` + `intelligence/scheduler.py` | ✅ proxy 新增 `_spawn()` 登记式 fire-and-forget（全部 10 处裸 `loop.create_task` 迁移）+ `_pending_tasks` 集合；lifespan 关停在 `engine.dispose()` 前 `shutdown_drain_tasks(timeout=10)`；`_run_provider_batch` 自身注册进 `_inflight_tasks`（定时采集关停可等待） |
| （顺带）healer 内联 select 收敛 | `db/crud.py` | ✅ 新增 `crud.get_normal_price_rows()`，healer 不再内联 select 构造（Mimosa SQL 注入误报源收敛，同 pricing_sync 模式） |

### 2.1 决策项：本地限流 / 账号配额是否接线

| 项 | 现状 | 不接线的代价 | 接线的价值 | 建议 |
|----|------|-------------|-----------|------|
| S2 账号多维配额 | `quota.increment_usage` 不可达 → `used_value` 生产零写入。**两面性（2026-09-14 实证）**：rpm/concurrent/daily 等=恒放行；tpm=大 max_tokens + 小 limit 行时**恒拒死且隐形**（deepseek_01 tpm=100000 封死 zcode 128000 output，拒绝不落库）。已落地（2026-09-14）：删 tpm 行 + 预检改跳过重选+落库+failover | 无账号级本地护栏，全靠上游 429→冷却自愈 | 提前避免打爆配额；tpm/daily 可做**每日花费护栏** | **累加落库式全面接线维持 defer**；「显式化」因 tpm 恒拒死事件更有必要（看板限额卡标注判定语义） |
| A1 本地 QPM/TPM 限流 | `limiter.record_*` 零调用 → `is_limited` 恒 False | 与 S2 同 | 与 S2 同 | **维持 defer**，但显式化 |

**重启接线的触发器**（任一出现，约半天/项）：
① 某渠道持续 >50% 限额；② 某上游对 429 有长封禁/处罚（自愈变事故）；
③ 需要 tpm/daily 记账做每日花费护栏（成本治理角度，而非限流角度）。

> 注：S2 的读端（`check_account_quota`）是「看起来已生效」的假象来源，
> `architecture.md` 已写明；建议看板限额卡再加一句「本地位限额当前为装饰性」。

### 2.2 潜伏项（条件具备才修）：Anthropic 口径

当前生产 6 渠道**全部 `api_style=openai`**，以下缺陷不触发，加 Anthropic 风格渠道即生效：
`proxy._extract_stream_usage` 漏读 `obj["message"]["usage"]`、`_estimate_cost` 对 Anthropic
`input_tokens` 误减 cache、cache 命中率分母不含 cache_read（可 >100%）。**可等接入 Anthropic
渠道时一并做**，或作为低优先级加固。

### 2.2 P0 缺陷修复批次（建议立即做，~1 小时）

| 项 | 位置 | 改法 |
|----|------|------|
| B1 探活失败 NameError | `intelligence/channel_health.py` | `import` 补 `ErrorInfo`（`:30`）；补一个「失败探活回写冷却」的单测 |
| G 部署路径硬编码 | `intelligence/scheduler.py:756` | `cwd` 改相对定位（`Path(__file__).resolve().parents[1]`）；删 `:749` 死导入 |

### 2.3 死代码清理（✅ 已上产，2026-09-14）

已删：`gateway/fallback.py`（整模块）、`db/crud.py` 17 个死函数（16 + 复查新增
`list_cross_source_discrepancies`）、`schemas/{channel,pricing,routing}.py`（仅留 intel.py）、
`fetcher.fetch_page` + `sources/base.py get_fetch_url/get_css_selector`、
静态孤儿 `app.js.p0ui.js` / `app.js.quota-warning.js` / `vendor/htmx.min.js`、
4 个 dashboard 死端点（auth/logout、transcripts×2、cross-source 列表）。
**有意保留**：agent 契约端点（cooldown 等）、C4 benchmark 链（V7.0/F4 决策项基础）、
C8/C9 表与列（需生产迁移，defer）。
删前已 grep `docs/agent-api.md` 与 `scripts/aimc.py` 确认无契约命中。

### 2.4 迁移机制收敛（✅ 已上产，2026-09-14）

见 [module-integration-audit.md](module-integration-audit.md) §F。**已实施轻方案 A**：
`db/schema_align.py`（ORM 单一事实源列对齐，238 条生成器纯字面量常量 +
`schema_migrations` 版本表，init_db 四步串联自动跑）+ `scripts/gen_orm_column_ddl.py`
（ORM 变更后重生成常量）。测试 `tests/test_v65_schema_align.py`（7 例）。
R1 双轨漂移 / R2 无版本表关闭；R3 约束漂移、R5 启动 DELETE 残余开放。
Alembic（方案 B）不引入，版本表名格式兼容保留演进空间。

### 2.5 静默吞异常收窄（✅ 已上产，2026-09-14）

`middleware._query_api_key` 拆两档（OperationalError=老库预期降级 debug /
非预期异常 warning）+ proxy 5 处 `except Exception: pass`/debug 升级 warning
带上下文（非流式/流式 usage_log 预处理、transcript 预处理、`_update_cooldown`、
`_auto_learn_drop_params`）。`_estimate_cost`/`_estimate_list_cost` 维持 debug
（价格缺失高频预期态，断链由 cost 覆盖率指标暴露）。
同批死代码清理见 [module-integration-audit.md](module-integration-audit.md) §C 状态块。

### 2.6 免费渠道扩容：ModelScope 接入 + Free-LLM-Collection 情报源（已拍板 2026-09-14，当日实施）

> 调研背景：[github.com/for-the-zero/Free-LLM-Collection](https://github.com/for-the-zero/Free-LLM-Collection)
> ——单人手工维护的免费 LLM API 静态清单（26 来源/100+ 模型，仅 OpenAI 兼容格式，
> 无价格无 key 无自动化）。价值=**线索清单**而非数据源；用户拍板：① 接 ModelScope
> 渠道（已有账号）；② README 加为情报采集源；③ OpenRouter（海外聚合器）先不做。
> **NVIDIA NIM 已放弃（2026-09-14）**：注册需手机号收短信验证，国内手机号收不到
> ——海外源（OpenRouter/Groq/Gemini/HuggingFace/NIM）默认全部不接，除非用户主动提出。

#### 2.6.1 方案 A：ModelScope 渠道接入（实施主体）

**为什么选它**：README 中对 AIMC 价值最高的国内源——2000 RPD 免费额度、模型阵容
与现有主力高度重合（DeepSeek-V4-Pro/Flash、GLM-5.x、Qwen3-235B）、国内直连稳定。
接入后给 tier:flagship/strong 的免费兜底链增加第三供给方（现有 sensenova 池级
429 是最大单点，87 次/7d）。

| 配置项 | 值 | 说明 |
|--------|-----|------|
| provider / key | `modelscope` | 新 provider |
| channel name | `modelscope_inference` | |
| base_url | `https://api-inference.modelscope.cn/v1` | OpenAI 兼容 |
| env_var | `MODELSCOPE_API_KEY` | 用户已有账号，token 放 .env |
| priority | `10` | 最低档兜底（同 sensenova 的 5 之下的新阶梯；免费源 priority 越大越靠后，让已有 0 元渠道先走） |
| qpm | `30` | channel_quotas 装饰性配置，保持一致性（见 [[channel-quotas-dead-code]]） |
| 价格行 | 全部 `0.0 / 0.0` | 免费额度内 0 元；超 2000 RPD 由 429→冷却自愈，同 sensenova 模式 |

**入选价格行（首期 4 个，宁缺毋滥）**：

| model（ModelScope raw 名） | 归一化名 | 理由 |
|---|---|---|
| `deepseek-ai/DeepSeek-V4-Pro` | 不入组，精确路由备用 | v4-pro 第二免费供给（现仅 deepseek 官方付费） |
| `deepseek-ai/DeepSeek-V4-Flash` | 不入组，精确路由备用 | v4-flash 第二免费供给（现仅 sensenova，池级 429 单点） |
| `ZhipuAI/GLM-5.1` | 暂不入 | 与 glm 渠道重合，观察后再定 |
| `Qwen/Qwen3-235B-A22B-Instruct-2507` | 暂不入 | 大参数新面孔，观察后定 tier |

**关键设计决策**：
1. **raw 名直录 price_snapshots**（`deepseek-ai/DeepSeek-V4-Flash`）——与 siliconflow
   同模式（[[model-channel-binding-price-snapshots]]：price_snapshots 行动态决定渠道
   候选，无映射表）。usage_logs 落上游 raw 名，dashboard 展示与 siliconflow 现状一致。
   不做归一化改名（`normalize_model_id` 对斜杠族名不适用，参考 SF paddleocr 教训只
   改厂商品牌前缀行——本批不涉及）。
2. **首批不入 tier 组**——deepseek 系已有精确路由消费（无组成员走精确匹配，
   cheapest 优先会把 modelscope 的 0 元行排进候选），先观察一周：请求数/失败率/
   429 分布，再决定是否把 V4-Flash 加入 tier:strong 成员（模型名与 sensenova 行
   同名，路由天然多渠道竞争，无需改组）。
3. **接入流程**：`manual_import.py` 加 modelscope 配置块 → VM 跑 `manual_import.py
   --channel modelscope` → `add_account.py --channel modelscope --name modelscope_01
   --api-key <token>` → 重启 → e2e 验证（`tier:balanced` 与精确 `deepseek-v4-flash`
   各一发）。
4. **观察项**（进 §4 观察线）：modelscope 请求量/失败率、2000 RPD 是否够用
   （超了会 429→冷却，无硬伤）、与 sensenova 的 deepseek-v4-flash 流量分配变化。

#### 2.6.2 方案 B：Free-LLM-Collection README 情报源

**为什么值得**：intel 系统正处验收死线（~2026-10-06 产出 ≥1 条可路由优惠），
「新免费源出现/免费额度变更」是比厂商降价页更高频的可路由情报品类。README 是
纯静态 Markdown（GitHub raw 直出），内容变化 = 上新免费源。

| 配置项 | 值 |
|--------|-----|
| provider | `free_llm_collection` |
| source_type | `price_aggregator`（第三方聚合语义；含免费额度/模型清单信息） |
| url | `https://raw.githubusercontent.com/for-the-zero/Free-LLM-Collection/main/README.md` |
| poll_interval | `86400`（1 天；清单更新频率低，自适应降频会自动拉长） |
| css_selector | 不设（raw markdown 无需 selector；fetcher 默认全文） |
| wait_until | `domcontentloaded`（纯静态文本，非 SPA） |
| api_endpoint | 不设 |

**实施**：新建 `intelligence/sources/free_llm_collection.py`
（仿 [qianwenai.py](../intelligence/sources/qianwenai.py) 观察性源，~30 行）+
注册进 `get_all_sources()`。**DB 侧无需手工插入**——scheduler 启动时按
`get_all_sources()` 与 `intel_sources` 表对齐建行（`_sync_sources_to_db` 既有逻辑）。

**预期产出**：README 变化 → AI 解析出「新免费源/额度变化」条目 → 落 promotions
审批队列（低置信度）或 intel 观察——**人工审批后**才可能转价格行，与 S3 双轨
拍板一致（订阅/免费类仅入库展示）。

**风险与边界**：
- GitHub raw 直连在生产 VM（境内网络）可能超时——首次采集若连续失败会被
  content_guard 抓禁用+告警（既有机制），不影响其他源；届时评估走镜像前缀。
- 解析质量：LLM 对 markdown 清单的解析置信度可能偏低 → 多数进审批队列人工看，
  符合预期，不是缺陷。

#### 2.6.3 交付物清单

> **✅ 已上产（2026-09-14，a08da05→1760a9d，814 测试全绿）**
>
> **✅ 方案 A 验收完成（2026-09-14 晚）**：modelscope_inference 渠道 id=7 +
> account modelscope_01 + 5 价格行（按用户 shouyi01 账号当日全量实测矩阵精选，
> 见 §2.6.1 表下注）落库；e2e 实证 `deepseek-ai/DeepSeek-V4.1-Flash` 精确路由
> 200（usage_logs id=11278, latency 6.9s, 34/15 tokens）、`tier:flagship` 组
> 路由 200 不受影响。观察期 1b 启动。

1. `scripts/manual_import.py`：+modelscope 配置块（channel/prices/qpm）✅
2. `intelligence/sources/free_llm_collection.py`（新）+ `sources/__init__.py` 注册（38 源）✅
3. VM 生产操作序列 ✅/⏳：
   - 方案 B **已完成验收**：`intel_sources` id=42 active、首轮采集成功、AI 解析产出
     promotions id=1097（confidence 0.92，"免费大模型 API 合集"摘要，含硅基流动
     1000 RPM / OpenRouter 20 RPM/200 RPD 额度信息）、content hash 已 commit、
     调度 job `batch_crawl_free_llm_collection_86400` 注册。
   - 方案 A **✅ 验收完成**：渠道 id=7 / account modelscope_01 / 5 价格行
     （deepseek-ai/DeepSeek-V4.1-Flash、deepseek-ai/DeepSeek-V4-Pro、ZhipuAI/GLM-5.2、
     Qwen/Qwen3-Next-80B-A3B-Instruct、Qwen/Qwen3-235B-A22B，全 0 元）。
     价格行按用户 2026-09-14 实测矩阵选定（49 目录实测 31 可用；只录大参数
     直接可用行——小模型硅基流动已长期免费覆盖、需流式 3 个、不可用 18 个均不录；
     ModelScope 无 DeepSeek-V4-Flash 这个 id，其 V4.1-Flash ≈ 官方 deepseek-flash
     与 sensenova 行不同代各自独立）。e2e：精确 raw 名 200 + flagship 组 200。
     **接入过程修复 manual_import 三个幂等缺陷**（284cea0/8264a76/1760a9d）：
     价格行 create→upsert（UNIQUE 中断致后续渠道漏建）、skip 分支补齐缺失
     价格行（渠道已存在但 0 价格行自愈）、命名空间 raw 名跳过 normalize
     （小写化会致上游 404，与 siliconflow api sync 同口径）。
4. 测试 ✅：`test_v65_free_channel_expansion.py` 5 例 + 3 处源总数快照 37→38
5. 文档 ✅：本节 + index 状态行 + agent-api 不涉及

**首采暴露并顺带修复的两个通用缺陷**（不止影响本源）：

- **clean_html 对纯文本/markdown 源失效**（f9e8c0d）：GitHub raw README 无
  `<html>` 包装时 BS4 行为退化 + 1500 字截断丢清单主体。修复：无 html/body
  标签特征 → 去空行直通截断。注：实际抓取经 Playwright 后 raw.githubusercontent
  返回 `<pre>` 包装页（含 html 标签），该修复对**不经浏览器的未来文本源**仍有效，
  本源实际受益的是下一条。
- **parse_content max_tokens 1200→4000**（51f249e）：parse_model=tier:balanced
  实际路由 minimax-m3（思考模型），1200 token 时思考未完即截断 → content 无
  JSON → S4 peek/commit 下轮同条件重试同失败 → 该源变化被永久吞。对照实验：
  2000 token 即 finish_reason=stop；JSON 本体 ~1k，4000 留 3k 思考余量。
  **所有情报源解析稳定性共同受益**。

---

## 3. 已规划但未拍板的版本（等口令）

| 版本 | 内容 | 状态 | 前置 |
|------|------|------|------|
| **V7.0** | yield（git commit ↔ 收益关联追踪）+ Phase C 其余（project/session 维度、guard 预算守护、compare 性价比） | 待拍板 | 用户口令；风险中高 |
| **F4（并入 V7.0）** | 看板新增采集源 + 自定义 tier 规则（覆盖 infer_tier 参数量正则盲区） | 待拍板 | 与 V7.0 同批 |
| **V6.2** | MCP server | 按需 | REST/CLI 已满足 agent 需求，属封装形态差异 |
| **R8** | 操作审计系统（audit_log + middleware 钩子，~1 天） | 待拍板 | **公网/多用户前必修**；纯内网自用可继续观察 |
| **C4 修复** | benchmark → model_cards 接入调度（激活 score_bonus） | 建议批次候选 | 需先决定是否让评分参与路由（A3） |

---

## 4. 观察线（等数据 / 等口令，不需开工）

| # | 观察线 | 现状 / 最近截止 |
|---|--------|----------------|
| 1 | sensenova 429 观察线（2026-09-16 探测定性：429001=免费公测池**共享吞吐**，与积分无关——积分满格仍拒；另有独立 RPM≈8/分闸门 code 8；积分窗口 60k/5h 仅在重载日成为附加约束） | 体量准入 [min,max] 双界已上产：>30k 走 taotoken、<30k 优先 sensenova（饱和期小请求成功率 ~9%，失败自动落 taotoken）；观察 channel_id=5 的 429 率与小请求成功率，池子恢复（大请求重新可过）再评估放宽阈值 |
| 1b | **modelscope 渠道观察**：请求数/失败率/免费额度消耗（官方"动态调整"无固定 RPD）、与 sensenova 的 deepseek 系流量分配变化、V4.1-Flash 与 sensenova deepseek-v4-flash 的代际混淆是否造成路由困扰 | 2026-09-14 渠道上线，e2e 200 |
| 1c | **free_llm_collection 情报源观察**：README 变化 → 审批队列产出节奏（首批 conf 0.92 已入库）；GitHub raw 经 Playwright `<pre>` 包装的解析质量 | 2026-09-14 首采成功 |
| 2 | glm_bigmodel 1305 过载波纹（同账号 30s 冷却波及免费模型） | 持续观察 |
| 3 | vision / 总体 400 率（<3% 达标） | qwen3-vl-8b-instruct 全 400 小信号跟踪 |
| 4 | 思考 Phase B：6 渠道开谁（全渠道实测无上游执行 `enable_thinking:false`） | **等用户口令**；glm 可试 low/high/max 档位 |
| 5 | 反馈公式 retry 项去留（双分并存 `feedback_score` / `feedback_score_no_retry`） | **~2026-09-23 ~ 10-07 拍板** |
| 6 | S3 生效后 pending 变化与门槛拦截量（`AUTO_JOIN_MIN_CONFIDENCE=0.9`） | 9/14 周报首查 |
| 7 | 看板 usage 每日 rollup 预聚合表 | 不立项；触发器：usage_logs >50 万行 / 30d 聚合 >300ms / 引入留存清理 / 跨季度分析 |
| 8 | sensenova-6.8-flash-lite `reasoning` 大字段裁剪 | 未触发（客户端兼容问题或 token 显著超预期才做） |
| 9 | P1-2 API 抽取直连 model_sync（降 pending 噪音、提升自动入组可信度） | 挂账 |

> 机制：周一 09:00 的 ZCode automation 覆盖第 1-7+9 的周报汇总（见自动化收口计划 §6）。

---

## 5. 推荐排期（建议，非拍板）

```
已完成上产  429 隐形拒绝修复（2026-09-14 商定并当日完成 3 项:
            预检改跳过重选+落库 / 删 deepseek_01 tpm 行 / 指数退避+mark_success 保留+渠道级冷却；
            顺带完成批次一 S5 主修复点——routes failover 块双重 mark_failure 移除）
            ↓
接着        稳定性批次 V6.5（建议 ~3~4 天，分三批）
            ├─ 批次一 P0 (~0.5 天): S1 Content-Length / S6 周报转义 /
            │                        B1 探活 NameError / G 部署路径
            │                        （S5 双重 mark_failure 已随 429 批次完成）
            ├─ 批次二 P1 (~1 天):   S8 路由成功率截断 / S9 cache 时区 /
            │                        S4 differ hash / S11 PRAGMA / S12 旧 job 清理
            ├─ 批次三 P1 (~1~1.5 天): S3 优惠→价格行修正确 + 促销可路由验收线
            │                        （范围已拍板 2026-09-14：双轨——订阅类仅展示 + 修真折扣缺陷）
            └─ 随批次: 死代码清理（审计 §C）+ 文档漂移收口（§H/H2-H4）
                    ↓
V6.5 之后   F 迁移机制收敛·轻方案 A（已拍板 2026-09-14: ensure_migrations 以 ORM 为
            单一事实源 + schema_migrations 版本表, ~1~2 天）
                    ↓
按截止点    观察线收口（反馈 retry 项 9/23~10/7 拍板；S3 pending 9/14 首查）
                    ↓
待口令      V7.0（yield + Phase C + F4） / V6.2 MCP / R8 操作审计
            └─ 触发条件驱动的：S2/A1 累加落库式接线、C4 benchmark 接入、Anthropic 口径
```

> **编码时序约束（2026-09-14）**：~~429 修复批次正在另一会话实施中~~ **已上产（2026-09-14 当日完成）**，
> V6.5 三个批次以其落地后代码为基线开工即可——批次一的 S5（去重复 mark_failure）主修复点已随
> 429 批次完成（routes failover 块的双重标记移除），S5 剩余部分（如有）需对照当前实现适配。

**建议先做稳定性批次的三点理由**：
1. 确定性高、**不引入新功能面**；批次一含一个实证的响应协议 bug（S1），修复成本 1 行；
2. 修的是「现有能力是否真在生效 / 数字是否可信」——B1 恢复冷却状态机、S8 纠正被高估的路由成功率、
   S9 消除与时区统一相悖的窗口偏移；
3. **批次三是「差异化卖点是否真成立」的验证**：优惠参与路由当前生产 0 产出（`is_promotion=1` = 0 行），
   若确认订阅类优惠本就设计为不转价格行，则需明确该验收线，避免一直停留在"入库展示"。

---

## 6. 待用户决策清单（汇总，含调研建议）

> 「建议」列为 2026-09-13 调研立场（基于生产审查证据），最终以用户拍板为准。
> 真正需要现在拍板的只有 #1/#2（解锁全部工作）；#3~#6 建议均为「维持现状」。

| # | 决策项 | 建议 | 依据 |
|---|--------|------|------|
| 1 | 立项**稳定性批次 V6.5**？ | **立项，三批都做（~3~4 天）**；若要压缩，批次一不可省 | S1 是本机实证的响应协议 bug 但修复仅 1 行；B1 NameError 1 行；S8 已实证 30d 视图成功率虚高；全程零新功能面风险 |
| 2 | **S3 订阅类是否本就不转价格行？** | **「明确边界 + 修缺陷」双轨** → ✅ **已拍板（2026-09-14）：按此执行，批次三范围确认**——订阅套餐类（无折扣率）维持「仅入库展示」（stepfun 先例）；真折扣链路修（折扣只作用命中模型 + 强制 effective_to + 合成「五折」端到端验收） | 生产 37 条 promotions 全为订阅类 → `is_promotion=1`=0 行是数据形态而非 bug；但 healer 把折扣乘到渠道全部 normal 模型（siliconflow 99 个）是真实隐患，无论产品意图如何都该修 |
| 3 | **S2/A1** 本地限流与账号配额接线？ | **部分已落地（2026-09-14，随 429 批次）**：deepseek_01 的 tpm=100000 行已删除、预检改跳过重选+落库已上产；「累加落库式全面接线」仍**维持 defer，仅显式化** | 7d 实测路由成功 99%；但 429 事件证明「零累加 + max_tokens×2 判定」会在小 limit 行上恒拒死且隐形——显式化标注判定语义更有必要；三个重启触发器不变 |
| 4 | **F 迁移机制**收敛时机？ | **轻方案 A** → ✅ **已拍板（2026-09-14）：V6.5 之后做**（ensure_migrations 以 ORM 为单一事实源 + schema_migrations 版本表，~1~2 天），不必等事故 | 升级流程目前靠「每次全量重跑 23 个脚本 + 各自幂等质量」；ORM 唯一索引只在全新库生效；V6.4.1 已出现脚本体系外的手工 SQL 存量迁移——迭代活跃下机制性成本持续累积 |
| 5 | **C4** benchmark 评分接入路由？ | **defer，与 V7.0/F4 一起拍板**；本轮顺带把 `_load_cards_cache` 白加载列入死代码清理 | 评分要生效需先修 benchmark 数据链（无调度、源质量未知）= V7 级投入；当前排序已有价格+优先级+cache 折算，价格平手场景少；现在接了也没有新数据 |
| 6 | **V7.0 / V6.2 / R8 / F4** 口令 | **本周期全部维持现状**；唯一留意 R8 | R8 操作审计是唯一有明确触发器的（公网/多用户前必修）——若近期有公网化打算，提前到 V6.5 之后做；V7.0 建议等 V6.5 落地 + retry 项观察（~2026-09-23~10-07）收口后再拍；V6.2 保持按需 |

---

*本文档为 2026-09-13 调研快照，结论随观察数据与用户口令更新。*
