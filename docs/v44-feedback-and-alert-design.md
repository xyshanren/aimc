# V4.4 设计方案 — 隐式反馈 + 告警驱动 + 账号 CLI

> 创建时间：2026-08-08
> 状态：⬜ 待开发
> 关联：[development-plan.md](development-plan.md)（路线总览）/ [architecture.md](architecture.md)（架构）

---

## 一、设计哲学

### 核心思路：从"被动响应"升级到"主动洞察"

```
V4.3 之前: 用户 → 调 → 系统返回  ←  一次性交互
V4.4:      用户 → 调 → 系统返回 → 反馈聚合 → 周报 → 决策依据  ←  闭环
```

### 三个"先做"原则

| 原则 | 含义 |
|---|---|
| **运维先于新功能** | 反馈/告警/CLI 先做（解决当下痛点） |
| **隐式先于显式** | 从 `usage_logs` 推断（0 成本）先做，Dashboard 打星后做 |
| **DB 先于 .env** | 账号配置走 DB（密文 + 即时生效），.env 退化为启动引导 |

### "不做什么"清单

| 不做 | 原因 |
|---|---|
| ❌ 多账号基础架构（v4.4） | 用户决定先用单账号 + 告警驱动（V4.6 再做） |
| ❌ Dashboard 账号 UI | V4.5 再做，先用 CLI 过渡 |
| ❌ whichllm 风格多源 benchmark | V4.6 再做，V4.4/4.5 先做隐式反馈 |
| ❌ 显式用户反馈（打星） | V4.6 再做，V4.4 只做隐式 |
| ❌ 自动多账号轮询 | V4.6 再做 |

---

## 二、现状盘点（基于代码）

### 已具备的能力

| 组件 | 状态 | 关键文件 |
|---|---|---|
| `accounts` 表（密文存储） | ✅ 已有 | `db/models.py` `api_key_ciphertext` |
| `accounts.status` 状态机 | ✅ 已有 | `db/models.py` active/exhausted/disabled |
| `accounts.update_account_quota` | ✅ 已有 | `db/crud.py:107-115`（quota 满 → exhausted） |
| `accounts.expires_at` 字段 | ✅ 已有 | `db/models.py` |
| `usage_logs` 表（latency/cost/tokens） | ✅ 已有 | `db/models.py` |
| `usage_logs.group_id` | ✅ Phase 3.A | 路由组追踪 |
| `intelligence/alerter.py` | ✅ 已有 | 告警框架（throttle 已实现） |
| `SettingsRuntime` 表 | ✅ API-Sec-2 | dashboard 改 alert 配置不重启 |
| ModelCard 表 | ✅ Phase 2D-4 | 单 benchmark 引用 |
| `model_registry` 表 | ✅ Phase 3.3 | tier / capabilities / benchmark_scores |
| 路由注入 tier / capabilities | ✅ Phase 3.E | `gateway/router.py:32-34`, `proxy.py:62-76` |

### 缺的 5 件事

| 缺什么 | 影响 | 何时做 |
|---|---|---|
| **隐式反馈聚合** | 路由无法用真实数据 | V4.4 ⭐⭐⭐ |
| **告警场景扩展** | 额度耗尽/成本上升无提醒 | V4.4 ⭐⭐⭐ |
| **账号管理 CLI** | 每次加账号要 SSH + 编辑 .env | V4.4 ⭐⭐⭐ |
| **周报自动生成** | 数据散落在 usage_logs 无法看趋势 | V4.4 ⭐⭐⭐ |
| **多源 benchmark 整合** | 仅引用 1 个 source | V4.6 |

---

## 三、4 个核心设计

### 设计 A — 隐式反馈系统（`model_feedback_summary`）

#### A.1 目标

从 `usage_logs` 自动聚合**你 4 客户端的真实使用反馈**，让路由决策有数据依据。

#### A.2 反馈评分公式

```python
feedback_score = (
    100 
    - 30 * retry_rate       # 重试 = 不满意 (权重最高)
    - 50 * fail_rate        # 失败 = 严重问题
    - 0.01 * p95_latency_ms # 慢 = 体验差
    - 5 * verbose_penalty   # 同类任务输出 > 2 倍中位数 = 啰嗦
)
# 取值范围: 0~100, 越高越好
```

#### A.3 数据模型

```sql
-- init_db.py 增量迁移 (V4.4.A)
CREATE TABLE model_feedback_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL,                -- "qwen-max"
    family TEXT NOT NULL,                  -- "qwen"
    period_start DATE NOT NULL,            -- 周一日期
    period_end DATE NOT NULL,              -- 周日日期

    -- 基础统计
    request_count INTEGER NOT NULL,
    success_count INTEGER NOT NULL,
    retry_count INTEGER NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_total REAL,

    -- 反馈指标
    fail_rate REAL,                        -- 0~1
    retry_rate REAL,                       -- 0~1
    p50_latency_ms INTEGER,
    p95_latency_ms INTEGER,
    avg_output_tokens INTEGER,
    feedback_score REAL,                   -- 0~100

    -- 元数据
    client_breakdown TEXT,                 -- JSON: {"hermes_tray": 423, ...}
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(model_id, period_start)
);
CREATE INDEX idx_feedback_period ON model_feedback_summary(period_start);
CREATE INDEX idx_feedback_score ON model_feedback_summary(feedback_score DESC);
```

#### A.4 聚合脚本

```python
# scripts/aggregate_weekly_feedback.py
# cron: 0 9 * * 1  (每周一 9:00, 由 systemd timer 触发)

"""
聚合上周 usage_logs → 写入 model_feedback_summary

输入: usage_logs (created_at >= period_start, < period_end)
输出: 每个 model_id 一行汇总

指标计算:
- retry_rate: 同 client_breakdown + 5min 窗口内同 model_id 重复请求 / 总请求
- fail_rate: status_code != 200 / 总请求
- p50/p95: PERCENTILE_DISC 在 SQLite 用 ROW_NUMBER() 模拟
- 性价比: feedback_score / cost_total
"""
```

#### A.5 文件清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 新增 | `db/models.py` | 加 `ModelFeedbackSummary` ORM |
| 修改 | `db/init_db.py` | 增量迁移（CREATE TABLE IF NOT EXISTS） |
| 新增 | `db/crud.py` | `upsert_feedback_summary` / `list_feedback_summary` |
| 新增 | `scripts/aggregate_weekly_feedback.py` | 周聚合脚本 |

---

### 设计 B — 周报自动生成

#### B.1 目标

每周自动生成 Markdown 报告 + Bark 推送关键结论，决策有依据。

#### B.2 报告结构（4 维度）

```
# AIMC 周报 (2026-08-04 ~ 2026-08-10)

## 📊 本周总览
- 总请求 / 总成本 / 平均延迟 / 失败率 / 重试率
- vs 上周对比 + 变化幅度

## 🏆 反馈分排行 Top 5
- 模型 / 反馈分 / 调用次数 / 成本 / 性价比 (反馈分/成本)

## 📈 趋势对比
- 反馈分本周 vs 上周 + 箭头

## ⚠️ 异常告警
- 失败率持续上升 / 成本异常 / 重试率超阈值

## 🎯 推荐行动
- 主路由调整建议 / 观察期模型 / 新增备选
```

#### B.3 输出形式

| 形式 | 实现 | 触发 |
|---|---|---|
| `reports/weekly_YYYY-MM-DD.md` | 本地 Markdown | 聚合脚本完成后 |
| Bark / 飞书推送 | `scripts/publish_report.py` | 报告生成后 |
| Dashboard 软链 | V4.5 UI 阶段 | — |

#### B.4 推送消息模板（≤ 200 字）

```
📊 AIMC 周报已生成 (2026-08-04 ~ 2026-08-10)
🥇 qwen-max 反馈分 92.3 (↑2.1)
⚠️ glm-4-plus 失败率 +0.8% (3 天持续)
💸 总成本 ¥45.6 (↑19.4%)
详情: http://192.168.56.12:8080/dashboard/reports
```

#### B.5 文件清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 新增 | `scripts/generate_weekly_report.py` | 报告生成（Markdown 模板渲染） |
| 新增 | `scripts/publish_report.py` | Bark / 飞书推送 |
| 新增 | `scripts/deploy/aimc-weekly-report.timer` | systemd timer（每周一 9:00） |
| 新增 | `scripts/deploy/aimc-weekly-report.service` | systemd service |
| 新增 | `reports/` 目录 | 报告输出（gitignore） |

---

### 设计 C — 告警驱动场景

#### C.1 现有告警

| 场景 | 实现 |
|---|---|
| Proxy 5xx 失败 | ✅ `intelligence/alerter.py` |
| Proxy 401/403 鉴权失败 | ✅ 同上 |
| 模型低置信度变更 | ✅ Phase 3.3 |
| 30 天未轮换 key | ✅ API-Sec-3 |

#### C.2 V4.4 新增 4 个场景

| 场景 | 阈值 | 告警模板 |
|---|---|---|
| **QuotaNearLimit** | `quota_used / quota_total > 0.8` | "GLM_01 额度用 80% (80K/100K)，建议注册新账号" |
| **AccountExhausted** | `quota_used >= quota_total` | "GLM_01 已耗尽，现有 active 账号 1 个" |
| **NoActiveAccount** | `active_count == 0 for provider` | "GLM 已无 active 账号，自动降级到 DeepSeek" |
| **CostRising** | 月成本环比 +30% | "本月 ¥45 (上月 ¥35, +28%)，建议查看周报" |

#### C.3 实现位置

```python
# intelligence/alerter.py 加 4 个 AlertKind 枚举值
# 加 4 个 check_* 函数
# scheduler.py 调度: daily check (已有 scheduler framework)
```

#### C.4 文件清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 修改 | `intelligence/alerter.py` | 加 4 个 AlertKind |
| 修改 | `intelligence/scheduler.py` | daily check 任务 |
| 新增 | `scripts/expire_accounts.py` | 扫 expires_at < now → status=expired |

---

### 设计 D — 账号管理 CLI

#### D.1 目标

替代"SSH + 编辑 .env + 重启"模式，提供 **1 条命令加账号** 的体验。

#### D.2 CLI 设计

```bash
# scripts/add_account.py
python scripts/add_account.py \
  --channel deepseek \
  --name deepseek_02 \
  --api-key sk-new-key-xxxxxxxx \
  --quota-total 100000 \
  --expires-at 2026-11-08 \
  --notes "新用户优惠 7000 万 tokens"

# 输出:
# ✅ 账号创建成功
#    ID: 5
#    Channel: deepseek (id=1)
#    Account Name: deepseek_02
#    Status: active
#    Quota: 100,000
#    Expires: 2026-11-08
#    Key 加密: Fernet(aimc_master_key)
#
# 下一步:
#   - 验证: curl http://192.168.56.12:8080/v1/models -H "Authorization: Bearer xxx"
#   - 路由: 已在 cache 中, 下一请求生效
```

```bash
# scripts/list_accounts.py
python scripts/list_accounts.py [--provider deepseek] [--status active]

# 输出:
# 📋 账号列表 (provider=deepseek, status=active)
# ┌────┬──────────────┬───────────┬────────┬──────────┬─────────────┐
# │ ID │ Name         │ Status    │ Used   │ Total    │ Expires     │
# ├────┼──────────────┼───────────┼────────┼──────────┼─────────────┤
# │ 1  │ deepseek_01  │ active    │ 30K    │ 100K     │ 2026-09-08  │
# │ 2  │ deepseek_02  │ active    │ 0      │ 100K     │ 2026-11-08  │
# └────┴──────────────┴───────────┴────────┴──────────┴─────────────┘
# 显示: sk-***-xxxx (脱敏)
```

```bash
# scripts/rotate_account.py
python scripts/rotate_account.py disable --id 5      # 禁用
python scripts/rotate_account.py enable --id 5        # 启用
python scripts/rotate_account.py rotate --id 5 --new-key sk-new  # 轮换 key
python scripts/rotate_account.py expire-check          # 扫过期
```

#### D.3 .env 角色调整

| 旧 | 新 |
|---|---|
| `DEEPSEEK_API_KEY=sk-xxx`（必填） | `DEEPSEEK_API_KEYS=sk-key1,sk-key2`（可选，引导） |
| `GLM_API_KEY=glm-xxx`（必填） | `GLM_API_KEYS=glm-key1`（可选） |
| `AIMC_MASTER_KEY`（必填） | 不变 |
| `AIMC_API_KEY`（必填） | 不变 |
| `DATABASE_URL`（必填） | 不变 |

**演进路径**：
- V4.4：兼容 .env（首次 init 时入库），CLI 优先
- V4.5：Dashboard 账号 UI
- V4.6：.env 仅保留 master + auth + db

#### D.4 文件清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 新增 | `scripts/add_account.py` | 加账号（加密入库） |
| 新增 | `scripts/list_accounts.py` | 列账号（脱敏） |
| 新增 | `scripts/rotate_account.py` | 启用/禁用/轮换/过期扫描 |
| 修改 | `.env.example` | 加 `_KEYS` 多 key 引导说明 |
| 修改 | `scripts/manual_import.py` | 改为引导用户改用 CLI（保留兼容） |
| 修改 | `scripts/deploy/install.sh` | 检测到单 key 时引导用 add_account.py |

---

## 四、完整数据模型变更

```sql
-- V4.4.A: model_feedback_summary (新表)
CREATE TABLE model_feedback_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL,
    family TEXT NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    request_count INTEGER NOT NULL,
    success_count INTEGER NOT NULL,
    retry_count INTEGER NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_total REAL,
    fail_rate REAL,
    retry_rate REAL,
    p50_latency_ms INTEGER,
    p95_latency_ms INTEGER,
    avg_output_tokens INTEGER,
    feedback_score REAL,
    client_breakdown TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(model_id, period_start)
);
CREATE INDEX idx_feedback_period ON model_feedback_summary(period_start);
CREATE INDEX idx_feedback_score ON model_feedback_summary(feedback_score DESC);
```

**无其他 schema 变更**（沿用现有 `accounts` / `usage_logs` / `promotions`）。

---

## 五、文件变更清单（V4.4 完整）

| 操作 | 文件 | 工作量 | 优先级 |
|------|------|--------|--------|
| 修改 | `db/models.py`（加 `ModelFeedbackSummary` ORM） | 15 分钟 | ⭐⭐⭐ |
| 修改 | `db/init_db.py`（建表 + 索引） | 15 分钟 | ⭐⭐⭐ |
| 修改 | `db/crud.py`（`upsert_feedback_summary` / `list_feedback_summary`） | 30 分钟 | ⭐⭐⭐ |
| 新增 | `scripts/aggregate_weekly_feedback.py`（聚合脚本） | 1 小时 | ⭐⭐⭐ |
| 新增 | `scripts/generate_weekly_report.py`（报告生成） | 1 小时 | ⭐⭐⭐ |
| 新增 | `scripts/publish_report.py`（Bark / 飞书推送） | 30 分钟 | ⭐⭐⭐ |
| 新增 | `scripts/expire_accounts.py`（过期扫描） | 30 分钟 | ⭐⭐ |
| 修改 | `intelligence/alerter.py`（加 4 个 AlertKind） | 1 小时 | ⭐⭐⭐ |
| 修改 | `intelligence/scheduler.py`（daily 调度） | 15 分钟 | ⭐⭐⭐ |
| 新增 | `scripts/add_account.py` | 1 小时 | ⭐⭐⭐ |
| 新增 | `scripts/list_accounts.py` | 30 分钟 | ⭐⭐⭐ |
| 新增 | `scripts/rotate_account.py` | 30 分钟 | ⭐⭐ |
| 修改 | `.env.example`（`_KEYS` 多 key 引导） | 15 分钟 | ⭐⭐ |
| 修改 | `scripts/manual_import.py`（引导改用 CLI） | 30 分钟 | ⭐⭐ |
| 修改 | `scripts/deploy/install.sh`（引导用 add_account.py） | 30 分钟 | ⭐⭐ |
| 新增 | `scripts/deploy/aimc-weekly-report.{timer,service}` | 15 分钟 | ⭐⭐⭐ |
| 新增 | `reports/` 目录 + `.gitignore` | 5 分钟 | ⭐ |

**总工作量**：9.5 小时（约 1.5 工作日）

---

## 六、执行顺序

```
Day 1 上午 (4 小时):
  1. [A] model_feedback_summary 表 + 聚合脚本     2 小时
  2. [C] 告警 4 个新场景                         1.5 小时
  3. expire_accounts.py                          0.5 小时

Day 1 下午 (4 小时):
  4. [D] CLI: add_account / list_accounts / rotate_account  2.5 小时
  5. install.sh / manual_import.py 引导文案       1 小时
  6. .env.example 加 _KEYS 说明                   0.5 小时

Day 2 上午 (1.5 小时):
  7. [B] generate_weekly_report.py                1 小时
  8. publish_report.py                            0.5 小时
```

---

## 七、验证标准

### 数据层

- [ ] `model_feedback_summary` 表创建成功（init_db.py 幂等）
- [ ] `aggregate_weekly_feedback.py` 跑过去 7 天数据无报错
- [ ] 重跑同一周数据是 UPSERT（不重复插入）
- [ ] `add_account.py` 加的账号 status=active，立即可路由

### 路由层

- [ ] 加新账号后，下一次请求能命中（无需重启）
- [ ] 旧账号 quota_used >= quota_total → status=exhausted，路由跳过
- [ ] 旧账号 expires_at < now → status=expired，路由跳过

### 告警层

- [ ] 80% 配额 → 收到 QuotaNearLimit 告警
- [ ] 100% 配额 → 收到 AccountExhausted 告警
- [ ] 某 provider 全耗尽 → 收到 NoActiveAccount 告警
- [ ] 月成本 +30% → 收到 CostRising 告警

### 周报层

- [ ] 周一 9:00 自动生成 Markdown 报告
- [ ] Bark 收到推送消息（≤ 200 字关键结论）
- [ ] 报告路径在 Dashboard 可访问
- [ ] 报告内容包含 4 个维度（总览/排行/趋势/告警）

### CLI 层

- [ ] `add_account.py` 加账号 → 加密入库（DB 看不到明文）
- [ ] `list_accounts.py` 列表显示脱敏（`sk-***-last4`）
- [ ] `rotate_account.py disable/enable` → 立即生效
- [ ] `rotate_account.py rotate` → 新 key 加密入库

### E2E（E2E 验证）

- [ ] 注册新账号 → CLI 加 → 下一请求命中 → 周一报告能看到
- [ ] 故意触发配额耗尽 → 告警发出 → 路由跳过该账号

---

## 八、风险与缓解

| 风险 | 缓解 |
|---|---|
| **聚合脚本影响线上** | 跑批任务，不阻塞 API；用独立 session |
| **重试率计算不准** | 仅统计"5min 内同 client 重复请求"（明确边界） |
| **feedback_score 误导** | V4.4 只做展示，不入路由（V4.5 才入路由，权重 ≤ 0.2） |
| **Bark 推送失败** | fallback 到日志 + Dashboard 通知 |
| **CLI 误删/误改** | 永远不改已存在账号；disable 是软删 |
| **MASTER_KEY 失效** | 启动时 fail-fast；定期 backup 提醒 |

---

## 九、监控指标（V4.4 上线后）

- **报告生成成功率**：周报任务成功率 ≥ 95%
- **告警准确率**：人工确认"有用"的告警 ≥ 70%
- **CLI 使用频次**：每周 add/rotate 操作 ≥ 1 次
- **路由命中率**：新加账号 24h 内被命中 ≥ 1 次

---

## 十、关联文档

- [development-plan.md](development-plan.md) — 路线总览
- [architecture.md](architecture.md) — 架构设计
- [api-key-security.md](api-key-security.md) — API-Sec 加密方案
- Phase 3.E 响应头注入 — 客户端如何拿到 model 信息
- [competitive-research.md](competitive-research.md) — 竞品参考（OmniRoute / LiteLLM / 9Router）

---

> 设计冻结日期：2026-08-08
> 实施开始：待用户确认