# AIMC 文档导航

> AIMC（AI Model Compass）是一个轻量级 LLM 网关 + 优惠情报聚合工具。
> 本文档是**所有文档的入口**。按你的角色选择下面任一入口。

**项目状态**：**V6.3.1 完成（2026-09-06，全量 694 测试全绿）**。已落地：V6.0（模型组分级 vision-light + GLM 免费模型 + P0-3 收窄；标价双轨成本审计 + 4 个 cost API + 看板成本审计卡）、V6.1（鉴权统一 admin key、发 key REST、统一 CLI `scripts/aimc.py`、agent 契约 [agent-api.md](agent-api.md)、Mimosa 存量清零）、V6.x（qwen 命名规范化 qwen3-vl-*/qwen3-8b + 4 个存量 e2e 失败修复）。2026-09-05 看板补齐：卡片折叠统一 `<details>` + 全局时间范围 24h/7d/30d + fmtCNY + **指标体系统一（Cache 节省/标价节省/路由成功率 命名收敛、fmtTokens/fmtPct/fmtLatency、状态栏去业务指标、组路由跟随时间范围）**，见 [dashboard-metrics-plan.md](dashboard-metrics-plan.md)；**思考 token 计量上产（usage_logs.reasoning_tokens + 看板 4 处展示，注入模式进观察期）**，见 [thinking-control-plan.md](thinking-control-plan.md)。
**V6.3 自动化收口（2026-09-05）**：F1 首启检测（空渠道 WARN 引导，manual_import 降级可选）+ F2 请求日志 model 搜索后端（`?model=` 模糊过滤）+ F3 自动入组埋点（运维页「自动入组观察」卡，纯只读 5 组指标）+ F7 图-表双向 hover 联动（Cache 环图↔渠道表、柱图↔日表），见 [dev/automation-closure-plan.md](dev/automation-closure-plan.md) §6。
**V6.3.1 瞬时冷却 badge 修复（2026-09-06）**：顶栏待处理 badge 不再为自愈型瞬时冷却（15-60s 自动恢复）报警（`/api/pending-summary?exclude_transient=true`），消除「顶栏有数但运维页为空」的观察时差；同时观察点落 `cooldown_events` 流水（同 episode 按 cooldown_until 去重），运维页新增「24h 瞬时冷却记录」卡留痕可回看。
**请求日志并显上游账号（2026-09-07）**：一渠道多账号落地后的用量可观测补齐——请求日志「渠道」列以 muted 后缀并显服务账号（`sensenova_official · sensenova_01`），详情抽屉 meta 同步显示；list/transcript 端点新增 `account_name` 字段（`usage_logs.account_id` 反查，legacy NULL 行显示 —），零迁移（dcab606）。
版本路线见 [next-major-v6-plan.md](next-major-v6-plan.md)（V6.0/V6.1/V6.x 已完成）；**后续规划见 [dev/automation-closure-plan.md](dev/automation-closure-plan.md)（F3 埋点 ≥2 周观察 → AUTO_JOIN_NON_T0 数据驱动决策（S1-S4）→ V7.0 含看板自定义采集源；V7.0/V6.2 待拍板）**。

---

## 🚀 我是新用户 / 想部署 AIMC

| 我想... | 看这个 |
|--------|--------|
| 5 分钟快速跑起来 | [user/install.md](user/install.md) |
| 接入 API Key / 配置环境 | [user/configure.md](user/configure.md) |
| 给团队成员发多用户 Key（V4.8） | [user/configure.md §4](user/configure.md) |
| 用 OpenAI SDK / curl 调用 | [user/usage.md](user/usage.md) |
| 用 Dashboard 调参 / 看数据 | [user/dashboard.md](user/dashboard.md) |
| 看周报 / shadow log / 报警 | [user/operations.md](user/operations.md) |
| 出错 / 报错 / 排查 | [user/troubleshooting.md](user/troubleshooting.md) |
| 常见问题 | [user/faq.md](user/faq.md) |

---

## �️ 我是开发者 / 想改代码

| 我想... | 看这个 |
|--------|--------|
| 了解架构 / 数据流 | [dev/architecture.md](dev/architecture.md) |
| 看 API 端点（OpenAI + Dashboard）| [dev/api.md](dev/api.md) |
| **Agent 编程接入（REST/CLI 契约, V6.1）** | [agent-api.md](agent-api.md) |
| 本地搭建开发环境 | [dev/setup.md](dev/setup.md) |
| commit / 分支 / 测试 规范 | [dev/conventions.md](dev/conventions.md) |
| 写测试 / 提 PR / 贡献代码 | [dev/contributing.md](dev/contributing.md) |
| 跑 694 个测试 + 加新测试 | [dev/testing.md](dev/testing.md) |
| 查 post-merge 修复（21 commit 整改）| [dev/post-merge-fixes.md](dev/post-merge-fixes.md) |

---

## 🔧 我是运维 / 部署 / 监控

| 我想... | 看这个 |
|--------|--------|
| 部署 / systemd / LXC / 周报 timer | [ops/deploy.md](ops/deploy.md) |
| 数据新鲜度 / shadow log / 限额监控 | [ops/monitor.md](ops/monitor.md) |
| 跑 12 个 migration 升级数据库 | [ops/migration.md](ops/migration.md) |
| 出错回滚（自动 tier 调整等）| [ops/rollback.md](ops/rollback.md) |
| **公网部署安全（VPN / Cloudflare Tunnel / nginx）** | **[ops/security.md](ops/security.md)** |
| **应急响应（master_key 泄露 / API key 泄露 / SSH 入侵）** | **[ops/incidents.md](ops/incidents.md)** |

---

## 📚 历史归档（只读，新内容见上面）

- [archive/agent-integration.md](archive/agent-integration.md) — Agent 集成历史（早期）
- [archive/competitive-research.md](archive/competitive-research.md) — 竞品调研
- [archive/phase2-design.md](archive/phase2-design.md) — Phase 2 模型组设计历史
- [archive/v44-feedback-and-alert-design.md](archive/v44-feedback-and-alert-design.md) — V4.4 设计文档
- [archive/review-phase3-4-phase4-e1.md](archive/review-phase3-4-phase4-e1.md) — Phase 3-4 评审
- [archive/fix-plan-phase3-4-phase4-e1.md](archive/fix-plan-phase3-4-phase4-e1.md) — Phase 3-4 fix plan
- [archive/e2e-results.md](archive/e2e-results.md) — 早期集成测试结果
- [archive/model-data-source.md](archive/model-data-source.md) — 模型数据源设计
- [archive/crawl-improvements.md](archive/crawl-improvements.md) — 采集改进
- [archive/hermes-agent-cn-integration-pr.md](archive/hermes-agent-cn-integration-pr.md) — Hermes Agent 集成 PR
- [dev/p0p1-fixes.md](dev/p0p1-fixes.md) — P0/P1 早期修复（历史）

---

## 文档元信息

- **最后更新**：2026-09-05
- **测试覆盖**：682 tests（pytest，~5 分钟跑通，V6.x 基线全绿）
- **观察期关注**：缓存命中率（路径 A）、周报 timer 首次自动触发（周一）、vision 400 率、**思考 token 占 output 比例（触发条件见 [thinking-control-plan.md](thinking-control-plan.md) §3）**、**自动入组 4 项指标（pending 增速/tier 分布/审批负担/组命中率，见 [dev/automation-closure-plan.md](dev/automation-closure-plan.md) F3，积累 ≥2 周后决策 AUTO_JOIN_NON_T0）**
- **V4.9 新功能**：Dashboard **🔴 待处理 (N)** tab（余额不足 / Key 失效 / 智能探活状态），见 [user/dashboard.md](user/dashboard.md)
- **观察期后待办**：操作审计系统（内网自用继续观察，公网/多用户前必修；见 [dev/automation-closure-plan.md](dev/automation-closure-plan.md) §2.4）
