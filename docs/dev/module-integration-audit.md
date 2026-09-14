# 模块间结合诊断（2026-09-13）

> 受众：维护 AIMC 全链路的开发者
> 方法：四层（gateway / db / intelligence / dashboard）逐一读取，用「符号全仓引用计数」
> 判定调用点是否存在，再对可疑项逐一打开源码确认。
> 配套：[architecture.md](architecture.md)（分层与数据流）、
> [production-review-2026-09-13.md](production-review-2026-09-13.md)（生产审查：bug/指标/安全）、
> [next-stage-research.md](next-stage-research.md)（处置规划）

> **判定口径**：行号随代码演进漂移，主键是符号名。本清单每个结论都做过 grep 复核。
> **重要前提**：`data/aimc.db` 是 `.gitignore` 的本地开发库（已腐化），**不能**用它判断
> schema 漂移；权威是 VM `ubuntu2401:~/aimc/data/aimc.db`。

---

## 0. 总体结论

主干是贯通的：`/v1/chat/completions` 的「鉴权 → 路由 → 限额 → 转发 → 冷却/分类 → 用量落库」
全链路真实生效；情报的「采集 → 变化检测 → 解析 → 落库 → 路由」在**价格行**与**模型元信息**
两条支链上也真实接通；dashboard 活跃前后端无断链。

问题集中在三类，按隐蔽度排序：

| 类别 | 数量 | 特征 |
|------|------|------|
| A 「接通但空转」旁路 | 4 | 看起来有调用链，实际计数侧未接线 → 恒放行/白加载 |
| B 确定性 bug | 1 | 会真实抛错的 NameError |
| C 死代码/死资产 | 6 组 | 定义后全项目零调用 |
| D 耦合/反向依赖 | 4 组 | 掏私有成员、双向依赖、反向依赖 scripts |
| E 静默吞异常 | 6+ | 宽 except，历史已因此断链 |
| F 迁移机制 | 1 | 三轨并存、无版本表、约束漂移 |
| G 环境耦合 | 1 | 硬编码部署路径 |

---

## A. 「接通但空转」旁路（最隐蔽，容易误判为已生效）

### A1. 本地限流计数侧从未接线

- `gateway/limiter.py` 的 `record_request`(61) / `record_tokens`(68) / `record_daily`(75) /
  `acquire_concurrent`(86) / `get_status`(137) **全项目零调用**。
- 唯一被调用的是 `is_limited`(32)，被 `router.py:690` 与 `router.py:919`
  （`_is_channel_usable`）调用；它读 `_qpm_windows` 等 deque，deque 恒空 → **`is_limited` 恒 False**。
- 后果：`channel_quotas` 表的 QPM/TPM/日/并发限流**全部装饰性**。真实保护 = 上游 429 →
  短冷却自愈（`cooldown_manager`）。
- **已拍板 defer**（2026-09-07 用户：「本地 qpm 限制咱不启用」）。重启接线触发器见
  [next-stage-research.md](next-stage-research.md) §2.1；7d 实测全渠道峰值 2~15/min，
  远低于限额 30~100，即便接线也永不触发。

### A2. 账号配额累加侧不可达（比 A1 更隐蔽 —— 它看起来接通了）

- `gateway/quota.py:increment_usage`(197) 的**唯一调用者**是 `decrement_concurrent`(210)，
  而 `decrement_concurrent` 自身零调用 → `increment_usage` 实际不可达。
- 全仓 grep `used_value` 的写入点只有 `quota.py` 内部（`_rollover_window` 归零、
  `_get_or_create_usage` 建 0 行、`increment_usage` 累加），**没有任何生产写入方**。
- 而 `check_account_quota` → `_check_dimension` **真实读取** `AccountQuotaUsage.used_value`。
- 后果：rpm / tpm / concurrent / daily_tokens / weekly_tokens / 5h_tokens **恒读到 0 → 恒放行**；
  只有 `balance_cny` 维（依赖外部 sync 写 window_start+余额）真正生效。
- 与 A1 的区别：A1 是「读端也空」，A2 是「读端齐全、写端断路」——更容易被当成已生效。
- 注：用户日/月 token 限额（`check_user_quota` → `_sum_user_tokens` 聚合 `usage_logs.caller_id`）
  是**真实生效**的，不受此影响。

### A3. model_cards 评分未接入路由

- `router.py:score_bonus`(约 547) 仅被 docstring 与 `tests/test_v46_score_routing.py` 引用，
  `score_account`(约 740) **不调用它**。
- 后果：`_load_cards_cache`(496) 每次启动都加载 `model_cards`，但评分不参与排序 →
  V4.5.1「价格平手时高分 model 优先」实际无效果；`_cards_cache` 白加载。
- 关联：`model_cards` 运行时的**写入方也只有脚本/种子**（`scripts/seed_model_cards.py`、
  `scripts/refresh_benchmark.py`），`benchmark_aggregator` 未被任何 scheduler job 调度
  （见 C4）——即整条 benchmark 链是「种子 → 读端空转」。

### A4. 结构化价格解析结果被丢弃

- `intelligence/pricing_writer.py:write_pricing_snapshots_from_parse`(31) **生产零调用**
  （仅 `tests/test_pricing_sources.py` 调用）。
- `intelligence/parser.py` 已经能抽结构化 `models[].pricing`（normal / cache_hit / free /
  peak / offpeak），放进 `ParseResult`；但 `healer.handle_parse_result` 的
  price_aggregator 分支只调正则 `_parse_price_lines`（从 summary 文本捞 `model: input/output`）。
- 后果：确定性最高的结构化价格表被丢，实际入库靠正则，命中率低且丢 free/peak 语义。

---

## B. 确定性 bug

### B1. `channel_health.py` 探活失败分支用 `ErrorInfo` 但未 import → NameError

- `intelligence/channel_health.py:30` 只 `from gateway.error_classifier import ErrorCategory, classify`，
  **没有 `ErrorInfo`**；但 `:274` 构造 `ErrorInfo(...)`。
- 触发路径：探活失败 → `:269-282` 构造 `ErrorInfo` → **NameError** →
  `:283 cooldown_manager.probe_result(account_id, success=False, error_info=info)` **永不执行**。
- 影响面：成功分支（`:266-267`）正常；`AccountHealthLog` 写入（`:249-262`）在异常之前已完成，
  所以「探活记录」有，但**失败探活不回写冷却状态机**（下次探活时间/needs_human 升级失效）。
  异常会被 `job_health` listener 记为 job 失败（可观测兜底）。
- 修复：`import` 补 `ErrorInfo`（1 行）。已列入 [next-stage-research.md](next-stage-research.md) §2.2 建议批次。

---

## C. 死代码 / 死资产

> **✅ 已清理（2026-09-14，V6.5 第四批）**：C1 整模块删除、C2 17 个死函数删除
> （16 个 + 复查时 `list_cross_source_discrepancies` 也因列表端点删除而变死）、
> C3 三文件删除（仅留 intel.py）、C6 `fetch_page` 与 base 两方法删除、
> C7 三个静态孤儿删除、4 个死端点删除。**未动**（有意保留）：C4 benchmark 链
> （A3/C4 决策项候选，删了就没了接入基础）、C5 `suggest_main`（V6.5 批次一已删）、
> C8/C9（表/列删除涉及生产迁移与数据，defer——schema_align 版本表已使
> 「是否已跑」可审计，等 SQLite 重建表流程与需求对齐再做）。

| # | 位置 | 符号 | 证据 |
|---|------|------|------|
| C1 | `gateway/fallback.py` | `forward_with_fallback`(20) | **整模块零 import**（`gateway.fallback` 全仓无引用）。被 `routes.py` 重试循环取代 |
| C2 | `db/crud.py` | 16 个函数 | 全仓零引用（排除定义处）：`get_channel`(32) / `update_channel_status`(87) / `update_account_quota`(107) / `get_prices_by_channel`(168) / `get_prices_by_model`(174) / `list_promotions`(269) / `expire_promotion`(280) / `get_quotas_by_channel`(326) / `create_usage_log`(334) / `get_parse_rules`(375) / `update_intel_source`(426) / `find_candidate_group_for_tier`(757, **`return None` stub**) / `upsert_model_card`(1045) / `list_model_cards`(1065) / `get_best_model_for_tier`(1076) / `get_runtime_setting`(1097) |
| C3 | `schemas/` | 13 个 Pydantic 类 | `channel.py`(3, 实为 5) / `pricing.py`(4) / `routing.py`(2) 全部 1 引用=定义处；仅 `intel.py` 的 `ParseResult`(48) / `ModelIntelItem`(66) / `ModelPricingItem`(17) 真被消费 |
| C4 | `intelligence/` | benchmark 链 | `benchmark_aggregator.aggregate/to_model_cards_rows` 仅 `scripts/refresh_benchmark.py` 调用，**无 scheduler job 注册**；`sources/benchmark.py` 各 `fetch()` 空 stub。榜单被通用 HTML 抓取当优惠送 LLM → 落 promotions（语义污染） |
| C5 | `intelligence/scheduler.py` | `suggest_main`(749) | 导入后从未使用（实际走 `subprocess` 跑 `-m intelligence.analytics.suggest_changes`） |
| C6 | `intelligence/` | `fetcher.fetch_page` / `sources/base.py get_fetch_url`(102) / `get_css_selector`(106) | 零调用 |
| C7 | `dashboard/static/` | `app.js.p0ui.js` / `app.js.quota-warning.js` / `vendor/htmx.min.js` | 均未被 `index.html` 引用（htmx 已在 Phase 3a 移除）。`quota-warning.js` 还引用了**不存在的** `/dashboard/api/quota-warning` |
| C8 | `channels_quotas` 表 + `ChannelQuota` ORM + 2 crud 函数 | — | 运行时零读；被 `account_quotas`(P0-6) 取代 |
| C9 | 9 个 ORM 列有写无读/完全无引用 | `accounts.encryption_version` / `accounts.quota_plan` / `price_snapshots.currency` / `model_registry.benchmark_scores` / `model_group_members.max_cost_ratio` / `model_feedback_summary.avg_output_tokens` / `model_feedback_summary.client_breakdown` / `parse_rules.url_pattern` / `parse_rules.ai_strategy` | grep 确认无业务读点 |

### dashboard 死端点（定义后全仓无调用方，含 app.js / CLI / 文档）

| 端点 | 位置 | 说明 |
|------|------|------|
| `POST /api/auth/logout` | `routes.py:203` | 完全无引用 |
| `GET /api/transcripts` | `routes.py:3517` | 前端用的是 `/api/requests/{id}/transcript` |
| `GET /api/transcripts/{transcript_id}` | `routes.py:3552` | 同上 |
| `GET /api/cross-source/discrepancies`（列表） | `routes.py:2636` | 前端只调 resolve(POST) 与 run-detection |

**仅文档留档、无调用方**（属 agent/CLI 通道设计，可保留）：`GET /api/cooldown` /
`GET|PUT /api/settings/alert` / `GET|PUT /api/settings/router-score-weights` /
`POST /api/sources/{provider}/interval`。⚠️ 但 `docs/user/dashboard.md` 把
`PUT router-score-weights` 描述成看板可改项，而 `index.html` **没有该 UI**（见 H2）。

**CLI 专用**（前端未调，属有意设计）：`POST /api/cache/refresh`（存在，`:1700`）/
`POST /api/keys` / `GET /api/cooldown/needs-human`。

---

## D. 耦合与反向依赖

### D1. 同级模块掏另一模块的私有实现

- `gateway/embeddings.py:31-37` **顶层** import `gateway.proxy` 的 5 个下划线私有函数
  （`_estimate_cost` / `_estimate_list_cost` / `_log_task_result` /
  `_resolve_effective_base_url` / `_write_usage_log`）→ proxy 改名即断。
- 跨模块读/写单例私有成员：
  - `dashboard/routes.py:787,1002,1730` 读 `route_engine._channels_cache`；`:2202` 读写 `_score_weights`
  - `gateway/embeddings.py:83,99`、`gateway/proxy.py:126` 读 `_channels_cache` / `_registry_cache`
  - `dashboard/routes.py:3309` 直接 `mw._apikey_cache.clear()`
  - `dashboard/routes.py` 导入 `intelligence.channel_health._probe_one`、`intelligence.healer._save_promotion`

### D2. gateway ↔ intelligence 双向依赖（靠延迟 import 维持）

- gateway → intelligence：`proxy.py:1512/1526`（alerter）、`router.py:559`（recency）
- intelligence → gateway：`scheduler.py:654-655`（router.refresh_cache）、
  `channel_health.py:155/267/283`（cooldown）、`pricing_sync.py:163`（router.invalidate_price）
- 现状**全部是函数体内延迟 import**，因此无 import 环。**一旦任一侧改成顶层 import 即循环导入**——
  这是硬约束，`app.py` 顶层只 import config/db 也是为此。

### D3. dashboard 反向依赖 scripts 包

- `dashboard/routes.py:1470` `from scripts.apply_suggestions import ...`；
  `:3141` `from scripts.expire_accounts import expire_accounts`。
- 运行时代码 import 脚本包，边界模糊（scripts 本应是「薄客户端/运维入口」）。

### D4. 单例共享可变 dict 的隐式写-读协议

- `select_best_channel` / `_select_by_group` 直接改写 `_channels_cache` 中共享 dict 的
  `_channel_model` / `_actual_model` / `_registry_model` / `_group_id`；`routes.py:159-160`
  用浅拷贝兜底，`proxy._build_aimc_headers` / `_resolve_actual_model` 再读这些残留字段。
- 多处注释自承历史污染（router/proxy）。这是「隐式协议」而非显式返回值，易回归。

---

## E. 静默吞异常（历史已被此坑过）

> **✅ 已收窄（2026-09-14，V6.5 第四批）**：`middleware._query_api_key` 拆两档
> （OperationalError=老库预期降级 debug / 非预期异常 warning）；proxy 五处
> `except Exception: pass`/debug 全部升级 warning 带上下文（`_schedule_usage_log` /
> `_schedule_stream_usage_log` / `_schedule_transcript_log` / `_update_cooldown` /
> `_auto_learn_drop_params`）。`_estimate_cost`/`_estimate_list_cost` 维持 debug
> +None（价格缺失是高频预期态，warning 会刷屏；断链由 cost 覆盖率指标暴露）。

历史实证：`dashboard/routes.py` 的 `approve_pending_review` / `resolve_cross_source_discrepancy`
曾调 `route_engine.refresh_cache()` 但缺局部 import → `NameError` 被外层 `except Exception`
吞成 warning → 路由缓存从未刷新（修复 commit `82c63b1` / `153e966`，且保留 try/except）。

当前仍存在的「宽 except 包住了含 import/属性访问的逻辑」高风险点：

| 位置 | 行为 | 后果 |
|------|------|------|
| `proxy._schedule_usage_log`(约 1048-1110) | `except Exception: pass`(1109) | 费用/用量链任一名字错误 → 静默丢日志 |
| `proxy._schedule_stream_usage_log`(约 1332-1435) | `except Exception: pass`(1434) | 同上 |
| `proxy._estimate_cost`(约 72-82) / `_estimate_list_cost`(约 124-135) | `except Exception` → debug + None | 价格查询断链被吞（cost 静默 NULL） |
| `proxy._update_cooldown`(约 909-956) | `except Exception` → debug | cooldown/分类 import 失败 → 冷却安全网静默失效 |
| `proxy._auto_learn_drop_params`(约 302-330) | `except Exception` → debug | 参数学习断链 |
| `middleware._query_api_key`(约 116-144) | `except Exception` → 返回 None | **静默降级为 legacy 单 key 鉴权（安全相关）** |

对照：`router.py:684-685/915-916`、`routes.py:178-179/236-237` 的懒 import 在 try 之外，
import 缺失会抛 500，属相对安全写法。

---

## F. 迁移机制风险（三轨并存、无版本表）

> **✅ 轻方案 A 已上产（2026-09-14）**：`db/schema_align.py` 以 ORM 为单一事实源
> 列对齐（238 条生成器产出的纯字面量 ALTER 常量，启动时自动补缺列）+
> `schema_migrations` 版本表（align 补列 note=`orm-align`、23 个历史脚本基线
> note=`legacy-manual`）。**R1 双轨漂移 / R2 无版本表已解决**；R3 约束漂移
> （create_all 不给已存在表补索引）与 R5 启动路径 DELETE 属残余风险仍开放；
> R4 为 SQLite 固有限制已知未动。详见 docs/ops/migration.md §0。

- 机制：`create_all`（只建缺失表）+ `init_db.ensure_migrations()`（硬编码约 30 列/索引的
  幂等 ALTER）+ `db/migrations/*.py`（23 个手动脚本，`--apply`，**无调用方**）。
- 风险：
  - **R1 双轨漂移**：新增 ORM 列若忘写 `ensure_migrations` 且不跑脚本，老库启动不报错，
    用到时才 `no such column`（历史上 cache tokens / channels.api_* / list_*_price 等即靠手动脚本加）。
  - **R2 无版本表**：无法判断某脚本是否已执行；幂等性靠每个脚本自己 `PRAGMA table_info`，质量参差。
  - **R3 约束漂移**：`create_all` 不给已存在表加约束/索引 → ORM 声明的
    `uq_mgm_group_model`、`idx_mgm_*`、`idx_usage_logs_error_type/needs_human` 只对全新库生效。
  - **R4 类型/可空漂移**：如 `Account.api_key` ORM 为 nullable 而老库 NOT NULL（SQLite 限制，已知未动）。
  - **R5 启动路径含破坏性清理**：`init_db` 内有 price_snapshots 重复行 DELETE、
    model_capabilities_log pending DELETE（隐含数据删除）。

## G. 环境耦合

- `intelligence/scheduler.py:756` `cwd="/home/lxc/aimc"` 硬编码，带 `# TODO: 改 config-based path`。
- 现状：**匹配当前生产 VM（ubuntu2401，用户 lxc）路径，故生产不报错**；但换部署路径即静默失效
  （suggest_changes 子进程起不来，`model_change_suggestions` 周维度断更）。
- 建议改 `Path(__file__).resolve().parents[1]` 之类的相对定位。已列入 next-stage 建议批次。

---

## H. 文档与代码漂移

| # | 文档 | 漂移 | 实际 |
|---|------|------|------|
| H1 | `docs/dev/architecture.md` | 41 job / tier 组 6 / dashboard「30+ 端点」/ §5 config 列了不存在的 per-vendor key | 42 job / 7 组 / 67 端点 / 密钥在 DB；**本文件已重写** |
| H2 | `docs/user/dashboard.md`(186,199) | 把 `PUT router-score-weights` 说成看板可改项 | index.html 无该 UI，仅 REST/agent 可调 |
| H3 | `docs/index.md` / `docs/dev/testing.md` / `docs/dev/frontend.md` | 测试数 747（部分处 694）/「78 个文件」/ 最后更新 2026-09-07 | 750 tests / 79 个 test 文件（+conftest=80）/ 2026-09-13 |
| H4 | `gateway/app.py` | `version="6.1.0"` | 已到 V6.4.1，未回写 |

---

## I. 处置建议（按性价比排序；2026-09-14 落状态）

> 均为**建议**；A1/A2 属用户决策项（勿自行开工），V7.0 级别事项等口令。
> **状态更新（2026-09-14，V6.5 三批 + 429 批次上产后）**：

| 优先级 | 项 | 状态 |
|--------|----|------|
| P0 | B1 修 `channel_health` `ErrorInfo` import | ✅ 已修（V6.5 批次一，抽 `_probe_error_info` 可单测） |
| P0 | G 修 `scheduler` 硬编码 cwd + 删死导入 | ✅ 已修（V6.5 批次一，`_PROJECT_ROOT` 相对定位） |
| P1 | C 死代码清理 | ✅ **已清理（2026-09-14）**：fallback.py / crud 17 函数 / schemas 3 文件 / fetch_page+base 方法 / 静态孤儿 3 / 死端点 4；C4 benchmark 链与 C8/C9 表列**有意保留**（决策项/需生产迁移） |
| P1 | H 文档漂移 | ✅ 已修（architecture 重写 + 测试/job/端点数字收口 + H2 标注无 UI） |
| P2 | F 迁移机制收敛 | ✅ **已上产（2026-09-14，轻方案 A）**：schema_align ORM 列对齐 + schema_migrations 版本表；R1/R2 关闭，R3/R5 残余见 §F |
| P2 | E 静默 except 收窄 | ✅ **已收窄（2026-09-14）**：middleware._query_api_key 两档 + proxy 5 处升级 warning；_estimate_cost 维持 debug（高频预期态） |
| P3 | A3/A4 接线评分与结构化价格 | 待决策（与 V7.0/F4 同批；折扣链 S3a/S3b 已修但 `pricing_writer` 仍未接） |
| 决策项 | A1/A2 限流与配额是否接线 | **维持 defer 仅显式化**（2026-09-14 部分落地：删 deepseek_01 tpm 行 + 预检改 failover+落库；累加落库式接线触发器不变） |
| （审查新增） | S1/S4~S9/S11/S12/S3a-c/S7 生产审查项 | ✅ 已全部修复（V6.5 三批，750→802 测试；明细见 [next-stage-research.md](next-stage-research.md) §2.0 与 [production-review-2026-09-13.md](production-review-2026-09-13.md)） |

---

*本文档基于 2026-09-13 代码快照（各项现状态以 §I 状态列为准）；`docs/dev/architecture.md` §9 为其摘要版。*
