# 贡献指南

> 受众：想提 PR / 报 bug / 提 feature 的开发者
> 前提：[conventions.md](conventions.md)（commit / 代码风格）+ [testing.md](testing.md)（测试规范）

## 1. 报 bug

提 issue: https://gitee.com/XiaoYRecluse/aimc/issues

**报 bug 必填**：
- **标题**：一句话描述（如 "tier:flagship 在 minimax 429 时不 failover"）
- **复现步骤**：最小化的命令序列
- **期望行为**：应该发生什么
- **实际行为**：实际发生了什么
- **环境**：Python 版本 / OS / AIMC commit hash
- **日志**：`journalctl --user -u aimc --since "10 minutes ago" | tail -50`

**示例 bug 报告**：

```markdown
### tier:flagship 在 minimax TokenPlan 耗尽时返 503

**复现步骤**：
1. 部署 AIMC + 接入 minimax account
2. 消耗 minimax TokenPlan 全部额度
3. 调 `tier:flagship`

**期望**：failover 到 deepseek_official（flagship fallback chain 里有）

**实际**：返 503 "No available channel for model: tier:flagship"

**环境**：AIMC `f287a9d`, Python 3.12.3, Ubuntu 24.04

**日志**：
```
[P1-15: minimax_compat 429 (quota/plan), 标记 cooldown ...]
[WARNING] gateway.router: 所有渠道频限已满: tier:flagship
[INFO] gateway.middleware: POST /v1/chat/completions -> 503
```

**注意**：这其实是 P1-15 修复后的预期行为，但日志没说明 failover 路径是否尝试过 deepseek_official。建议日志加 X-AIMC-Failover-Attempted。
```

## 2. 提 feature

提 issue 前先看 [docs/index.md](../index.md) 和 [archive/competitive-research.md](../archive/competitive-research.md)，确认不是你漏掉的现有功能。

**feature request 模板**：

```markdown
### [Feature] <一句话标题>

**使用场景**：谁会用 / 解决什么问题

**期望行为**：具体描述（API / UI / 配置）

**替代方案**：现在怎么手动做（如果有）

**优先级**：阻塞 / 重要 / nice-to-have
```

**示例**：

```markdown
### [Feature] Dashboard 加 "数据导出" 按钮

**使用场景**：用户想导出 usage_logs 到 CSV 自己分析

**期望行为**：在 "用量统计" 卡片加 "导出" 按钮，按天/模型生成 CSV

**替代方案**：现在用 `sqlite3 data/aimc.db "SELECT ..."` 手动 dump
```

## 3. 提 PR

### 工作流

```
1. Fork 仓库: Gitee → fork → clone
2. 改代码 + 写测试
3. 跑测试: cd aimc && source .venv/bin/activate && pytest tests/ -v
4. commit: git commit -m "fix(router): ..."
5. push: git push origin fix/your-branch
6. Gitee → Pull Requests → New → 选择 master ← your-branch
7. 等 review + merge
```

### PR 标题 + 描述

**标题**：`type(scope): subject`（同 commit message）

**描述模板**：

```markdown
## 改了什么
- 简述 1
- 简述 2

## 为什么
背景 / 解决什么问题

## 测试
- pytest 输出 (全过)
- 新增的 test (说明测试场景)

## Checklist
- [ ] 测试通过
- [ ] 文档同步（如果是用户可见的改动）
- [ ] migration 写了 up/down (如果改了 db)
```

### PR 规模

- ✅ 小 PR（< 200 行 diff）：1-2 个 commit，1 个 reviewer
- ⚠️ 中 PR（200-500 行）：拆成多个小 PR 或多个 commit
- ❌ 大 PR（> 500 行）：拆分 + 解释为什么必须合并

## 4. 第一次贡献（good first issue）

适合第一次贡献的小任务：

- 加一个 `tests/test_xxx.py`（纯函数测试，零风险）
- 修 typo（docs/README.md）
- 加一个 `scripts/` 工具（如 `scripts/check_db_health.py`）
- 改进某个 dashboard 卡片 UI（前端 htmx + JS）

第一次贡献推荐流程：
```
1. 在 issues 找 "good first issue" 标签
2. 评论 "I'd like to take this"
3. 等 maintainer 确认
4. Fork → branch → code → test → PR
```

## 5. 沟通渠道

| 渠道 | 用途 |
|------|------|
| Gitee Issues | Bug / Feature / Question |
| Gitee Pull Requests | 代码贡献 |
| AIMC 日志（journalctl） | 运行时错误 |

**响应时间**：个人项目，没有 SLA。Maintainer 通常 1-3 天内回复。

## 6. 行为准则

AIMC 是个人项目，**没有 formal Code of Conduct**。但建议：

- **友善** — 评论时假设对方是好意
- **建设性** — 提改进建议而非只批评
- **聚焦技术** — 避免无关讨论

## 7. 第一次贡献后的步骤

PR 合并后：

```
1. AIMC commit log + Gitee contributors list 有你名字
2. 推荐等下次新 feature 时继续参与
3. 你的测试会成为后续重构的回归保护
```

非常感谢贡献！
