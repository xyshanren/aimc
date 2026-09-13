# 下一阶段规划调研（2026-09-13）

> 受众：决定 AIMC 下一步投入方向的维护者
> 方法：汇总① 现行路线图（[next-major-v6-plan.md](../next-major-v6-plan.md) /
> [dev/automation-closure-plan.md](automation-closure-plan.md)）、② 观察清单
> （见 §4 与 automation-closure-plan.md §5 决策记录）、
> ③ 本次模块间结合诊断（[module-integration-audit.md](module-integration-audit.md)）派生的候选工作。
> **本文是调研与建议，不是拍板**；V7.0 / V6.2 等按既有规则等用户口令。

---

## 0. 结论先行

1. **功能路线图（V6.0→V6.4.1）已全部上产并验收**，短期没有「必须做」的功能缺口。
2. 本次诊断暴露出**一批「稳定性/接线」问题**——不新增功能，但影响「现有功能是否真在生效」。
   建议把它们打包为一个**稳定性小批次（暂称 V6.5）**，成本低于任何新功能。
3. **两个真正的用户决策项**：本地限流与账号配额是否接线（A1/A2）。不接也能跑（反应式 429→冷却
   已证明够用），但应把「恒放行」显式化，避免后人误读为护栏。
4. **V7.0（yield + Phase C）与 V6.2（MCP）继续等口令**；F4（看板自定义采集源 + 自定义 tier 规则）
   并入 V7.0。
5. **观察线**按既有截止点收口（最近一个是反馈公式 retry 项，约 2026-09-23~10-07）。

---

## 1. 当前基线（2026-09-13）

- 版本：V6.4.1（时区统一）+ 看板统计口径统一已在本地 master（`c0f57d8`）。
- 测试：**750 全绿**（80 个文件含 conftest）。
- 生产：VM `ubuntu2401`，user 级 systemd `aimc.service`，WSL 内 `ssh ubuntu2401` 运维。
- 情报：37 个源（32 active / 8 disabled），42 个 scheduler job。
- 看板：67 个 API + 6 页前端。

---

## 2. 从结合诊断派生的候选工作（建议批次）

### 2.1 决策项：本地限流 / 账号配额是否接线

| 项 | 现状 | 不接线的代价 | 接线的价值 | 建议 |
|----|------|-------------|-----------|------|
| A1 本地 QPM/TPM 限流 | `limiter.record_*` 零调用 → `is_limited` 恒 False | 无本地提前拦截，全靠上游 429→冷却自愈；已实测够用（7d 成功 99.0%，峰值 2~15/min 远低于限额） | 提前避免打爆配额；tpm/daily 可做**每日花费护栏** | **维持 defer**，但写进文档/看板口径 |
| A2 账号多维配额 | `quota.increment_usage` 不可达 → `used_value` 恒 0 → 除余额外恒放行 | 与 A1 同（无账号级本地护栏） | 与 A1 同 | **维持 defer**，但显式化 |

**重启接线的触发器**（任一出现，约半天/项）：
① 某渠道持续 >50% 限额；② 某上游对 429 有长封禁/处罚（自愈变事故）；
③ 需要 tpm/daily 记账做每日花费护栏（成本治理角度，而非限流角度）。

> 注：A2 的读端（`check_account_quota`）是「看起来已生效」的假象来源，建议至少在
> `architecture.md`（已在本轮写明）或看板限额卡加一句「本地位限额当前为装饰性」。

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
现在        稳定性批次 V6.5（建议 ~1.5~2 天）
            ├─ P0: B1 探活 NameError + G 部署路径解耦        (~1h)
            ├─ P1: 死代码清理（对照审计 §C 清单）             (~半天)
            ├─ P1: 文档漂移收口（H2/H3/H4）                   (随批次)
            └─ P2: 静默 except 收窄（鉴权 + 用量链优先）       (~半天)
                    ↓
按截止点    观察线收口（反馈 retry 项 9/23~10/7 拍板；S3 pending 9/14 首查）
                    ↓
待口令      V7.0（yield + Phase C + F4） / V6.2 MCP / R8 操作审计
            └─ 触发条件驱动的：A1/A2 接线、F 迁移机制收敛、C4 benchmark 接入
```

**建议先做稳定性批次的三点理由**：
1. 成本最低（合计 ~2 天），且**不引入新功能面/新风险**；
2. 修的是「现有能力是否真在生效」——B1 让冷却状态机恢复完整、G 让周建议不因换路径断更；
3. 死代码清理与迁移机制收敛会**直接降低后续每次改动的认知负担与断链概率**
   （本项目历史多次栽在「单测全绿但链路不通」）。

---

## 6. 待用户决策清单（汇总）

1. 是否立项**稳定性批次 V6.5**（含 P0 修复 + 清理 + except 收窄）？
2. **A1/A2** 本地限流与账号配额：维持 defer（仅显式化）还是接线？
3. **F 迁移机制**：现在收敛（轻方案 A）还是等事故触发？
4. **C4** benchmark → model_cards：是否让评分参与路由（决定 A3 是否接线）？
5. **V7.0 / V6.2 / R8 / F4** 的口令（既有规则：勿自行开工）。

---

*本文档为 2026-09-13 调研快照，结论随观察数据与用户口令更新。*
