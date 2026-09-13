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
3. **必须处理的两个确定性缺陷**：失败链响应 `Content-Length` 损坏（S1，已实证）、情报核心链
   「优惠→价格行」生产 0 产出且一旦触发会大面积错价（S3）。
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

### 2.0 建议的稳定性批次（P0→P1，~1.5~2 天，来源=审查批次一/二）

**批次一（P0，~0.5 天）— 低成本、高确定性、直接修可见缺陷**

| 项 | 位置 | 改法 | 来源 |
|----|------|------|------|
| S1 失败链响应 Content-Length 损坏 | `gateway/routes.py:274-289` | 构造新 JSONResponse 前 `new_headers.pop("content-length", None)`；补回归测试 | [审查] 实证 |
| S5 失败重复 mark_failure | `gateway/proxy.py:847` + `gateway/routes.py:244` | routes 复用 proxy 的分类，不重复 mark；或 proxy 只返回分类由 routes 统一 mark | [审查] |
| S6 周报存储型 XSS | `dashboard/routes.py:2842,2866` | 改返回 `text/plain`/JSON，或 `html.escape`；前端 `textContent` | [审查] |
| B1 探活 NameError | `intelligence/channel_health.py:30,274` | import 补 `ErrorInfo` + 失败探活回写单测 | [结合] |
| G 部署路径硬编码 | `intelligence/scheduler.py:756` | `cwd` 改 `Path(__file__).resolve().parents[1]`；删死导入 | [结合] |

**批次二（P1，~1 天）— 数据正确性**

| 项 | 位置 | 改法 | 来源 |
|----|------|------|------|
| S8 路由成功率 LIMIT 截断 | `dashboard/routes.py:1901-1917` | unmatched 求和改**不带 limit** 的 `count()`；`limit(20)` 只用于 top_models；两边 `model.isnot(None)` 一致（30d 实证已触发，27>20） | [审查] 实证 |
| S9 cache 命中率时区/实现 | `gateway/router.py:1566-1578` | cutoff 改 Python `datetime.now()` 传参（与 quota 一致）；按 `channel_id` 而非 name 子查询；必要时走 async session | [审查] 实证 |
| S4 differ 先落 hash | `intelligence/differ.py:72/91/101` | 拆 `peek_hash`/`commit_hash`，仅解析成功后提交 hash | [审查] |
| S11 sync_engine PRAGMA | `db/engine.py:42-47` | 补 `busy_timeout=5000`/`synchronous=NORMAL`（与 async 一致） | [审查] |
| S12 调度器旧 job 残留 | `intelligence/scheduler.py:103-111` | 启动时按当前 `batch_crawl_{provider}_{interval}` 集合枚举清理不匹配 job | [审查] |

**批次三（P1，~1~1.5 天）— 情报核心链修复 + 验收（价值最高，需先确认产品意图）**

| 项 | 位置 | 改法 | 来源 |
|----|------|------|------|
| S3a 折扣乘到全部模型 | `intelligence/healer.py:70-94` | 折扣只作用于 `result.models` 命中的模型集合，无模型信息则不做价格行 | [审查] |
| S3b promotion 永久打折风险 | `healer.py:81-94` + `router.py:828` | promotion 强制 `effective_to`（缺失用抓取日 +7/+30 天）；router 对 promotion 加"过期回退 normal" | [审查] |
| S3c 促销可路由验收线 | tests | 端到端"五折"样例测试，确立「promotion 参与路由」的验收标准（当前生产 `is_promotion=1` = 0 行） | [审查] 实证 |
| S7 停机 drain | `gateway/proxy.py` create_task + `app.py:104-130` + `scheduler.py:310-336` | 模块级 `set[Task]` 登记并在 `engine.dispose()` 前 `gather(timeout=...)`；定时采集也纳入 in-flight | [审查] |

### 2.1 决策项：本地限流 / 账号配额是否接线

| 项 | 现状 | 不接线的代价 | 接线的价值 | 建议 |
|----|------|-------------|-----------|------|
| S2 账号多维配额 | `quota.increment_usage` 不可达 → `used_value` 生产零写入 → 除余额外恒放行 | 无账号级本地护栏，全靠上游 429→冷却自愈 | 提前避免打爆配额；tpm/daily 可做**每日花费护栏** | **维持 defer**，但写进看板/文档口径 |
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

### 2.3 死代码清理（建议批次，~半天）

按 [module-integration-audit.md](module-integration-audit.md) §C 清单：
`gateway/fallback.py`（整模块）、`db/crud.py` 16 个死函数、`schemas/{channel,pricing,routing}.py`、
静态孤儿 `app.js.p0ui.js` / `app.js.quota-warning.js` / `vendor/htmx.min.js`、
4 个 dashboard 死端点。
**删前确认**：`GET /api/cooldown` 等「仅文档留档」端点属 agent 契约（保留）；
死端点删除前 grep 一次 `docs/agent-api.md` 与 `scripts/aimc.py`，避免删掉对外契约。

### 2.4 迁移机制收敛（建议立项，1~2 天）

见 [module-integration-audit.md](module-integration-audit.md) §F。二选一：
- **A（轻）**：扩展 `ensure_migrations` 覆盖全部 ORM 列（以 `db.models` 为单一事实源，
  create_all 之外补「列存在性 + 缺失即 ALTER」），加 `schema_migrations` 版本表记录已跑脚本。
- **B（重）**：引入 Alembic（autogenerate + 版本链）。

个人项目定位下建议 A；触发提前做的事件：出现一次「老库 no such column」生产事故。

### 2.5 静默吞异常收窄（建议批次，~半天）

优先 `middleware._query_api_key`（鉴权降级，安全相关）与 `proxy` 用量/费用链：
把「预期异常」（如价格缺失）与「非预期异常」（NameError/AttributeError）区分，
后者至少 `logger.error` 不静默 pass。历史已因此发生过 route_engine NameError 断链。

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
| 1 | sensenova 积分池压力（60k/5h + 600k/周，池级共享 TPM） | 持续观察；输入分组再平衡已上产 |
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
现在        稳定性批次 V6.5（建议 ~3~4 天，分三批）
            ├─ 批次一 P0 (~0.5 天): S1 Content-Length / S5 去重复 mark_failure /
            │                        S6 周报转义 / B1 探活 NameError / G 部署路径
            ├─ 批次二 P1 (~1 天):   S8 路由成功率截断 / S9 cache 时区 /
            │                        S4 differ hash / S11 PRAGMA / S12 旧 job 清理
            ├─ 批次三 P1 (~1~1.5 天): S3 优惠→价格行修正确 + 促销可路由验收线 /
            │                        S7 停机 drain
            └─ 随批次: 死代码清理（审计 §C）+ 文档漂移收口（§H/H2-H4）
                    ↓
按截止点    观察线收口（反馈 retry 项 9/23~10/7 拍板；S3 pending 9/14 首查）
                    ↓
待口令      V7.0（yield + Phase C + F4） / V6.2 MCP / R8 操作审计
            └─ 触发条件驱动的：S2/A1 接线、F 迁移机制收敛、C4 benchmark 接入、Anthropic 口径
```

**建议先做稳定性批次的三点理由**：
1. 确定性高、**不引入新功能面**；批次一含一个实证的响应协议 bug（S1），修复成本 1 行；
2. 修的是「现有能力是否真在生效 / 数字是否可信」——B1 恢复冷却状态机、S8 纠正被高估的路由成功率、
   S9 消除与时区统一相悖的窗口偏移；
3. **批次三是「差异化卖点是否真成立」的验证**：优惠参与路由当前生产 0 产出（`is_promotion=1` = 0 行），
   若确认订阅类优惠本就设计为不转价格行，则需明确该验收线，避免一直停留在"入库展示"。

---

## 6. 待用户决策清单（汇总）

1. 是否立项**稳定性批次 V6.5**（三批：P0 修复 / 数据正确性 / 情报链修复）？
2. **S3 情报核心链**：订阅类优惠是否本就设计为「不转价格行」？决定批次三是"修复"还是"明确边界"。
3. **S2/A1** 本地限流与账号配额：维持 defer（仅显式化）还是接线？
4. **F 迁移机制**：现在收敛（轻方案 A）还是等事故触发？
5. **C4** benchmark → model_cards：是否让评分参与路由（决定 A3 是否接线）？
6. **V7.0 / V6.2 / R8 / F4** 的口令（既有规则：勿自行开工）。

---

*本文档为 2026-09-13 调研快照，结论随观察数据与用户口令更新。*
