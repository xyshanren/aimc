# AIMC 文档导航

> AIMC（AI Model Compass）是一个轻量级 LLM 网关 + 优惠情报聚合工具。
> 本文档是**所有文档的入口**。按你的角色选择下面任一入口。

**项目状态**：V4.6.4 post-merge + 21 commit 架构整改完成（2026-08-15），项目进入稳定期。

---

## 🚀 我是新用户 / 想部署 AIMC

| 我想... | 看这个 |
|--------|--------|
| 5 分钟快速跑起来 | [user/install.md](user/install.md) |
| 接入 API Key / 配置环境 | [user/configure.md](user/configure.md) |
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
| 本地搭建开发环境 | [dev/setup.md](dev/setup.md) |
| commit / 分支 / 测试 规范 | [dev/conventions.md](dev/conventions.md) |
| 写测试 / 提 PR / 贡献代码 | [dev/contributing.md](dev/contributing.md) |
| 跑 74 个测试 + 加新测试 | [dev/testing.md](dev/testing.md) |
| 查 post-merge 修复（21 commit 整改）| [dev/post-merge-fixes.md](dev/post-merge-fixes.md) |

---

## 🔧 我是运维 / 部署 / 监控

| 我想... | 看这个 |
|--------|--------|
| 部署 / systemd / LXC | [ops/deploy.md](ops/deploy.md) |
| 数据新鲜度 / shadow log / 限额监控 | [ops/monitor.md](ops/monitor.md) |
| 跑 7 个 migration 升级数据库 | [ops/migration.md](ops/migration.md) |
| 出错回滚（自动 tier 调整等）| [ops/rollback.md](ops/rollback.md) |
| **公网部署安全（VPN / Cloudflare Tunnel / nginx）** | **[ops/security.md](ops/security.md)** |

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

- **最后更新**：2026-08-15
- **commit 范围**：21 个新 commit（Phase 0 到阶段 D，2026-08-14~15）
- **测试覆盖**：74 tests（pytest，9.6s 跑通）
- **下次大改**：P2-19 真自动应用（稳定期 1 个月后启动）
