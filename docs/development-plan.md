# AIMC 开发计划

> 更新时间：2026-08-08（V4.6 全章完成, §6.2 显式打星主动放弃）
> 状态标记：✅ 已完成 | 🔨 进行中 | ⬜ 待开发 | ⏸ 主动延后
> 配套文档：[架构设计](architecture.md) / [竞品调研](competitive-research.md) / [V4.4 专项设计](v44-feedback-and-alert-design.md)

---

## 路线总览

| 阶段 | 主题 | 状态 | 说明 |
|------|------|------|------|
| Phase 1 | 网关核心 | ✅ | FastAPI 路由/频限/透传/8 张表基础架构 |
| **Phase 2.5** | **运维加固** | **✅** | **4 个日常痛点改进（详见下文）** |
| **Phase 2** | **多维模型组** | **✅** | **4 张新表 + 25 model + 6 group + 35 member + tier/scene 路由 + Agent 偏好 + 响应头** |
| Phase 3 | 情报联动模型组 | ✅ | 3.0/3.1/3.2/3.3/3.4 已完成，3.5 验证 4/4 PASS, 3.6 国内服务商补齐中 |
| **API-Sec-1** | **DB api_key 加密 (XOR+Base64 轻量化)** | **✅** | **DB 文件泄露 ≠ channel key 泄露；零外部依赖** |
| **API-Sec-2** | **ALERT_CHANNEL runtime override** | **✅** | **改告警 channel / webhook 不重启** |
| **API-Sec-3** | **key 轮换提醒** | **✅** | **30 天未轮换飞书告警** |
| **V4.3 部署 hotfix** | **8 次部署修复** | **✅** | **install.sh V4.3.8 + smoke V4.3.11 + verify_clients V4.3.11** |
| **V4.4** | **反馈+告警+CLI 账号管理** | **✅** | **隐式反馈 + 周报 + 告警驱动 + 账号 CLI (9.5h)** |
| **V4.5** | **能力评分路由 + Dashboard 账号 UI** | **✅** | **whichllm 启发 + score 入路由 + Web UI (12h)** |
| **V4.6** | **告警驱动多账号 + whichllm 多源 benchmark** | **✅** | **多账号+Cooldown + 5源 benchmark 加权融合 (10h, §6.2 显式打星主动放弃)** |

设计原则：**运维先于新功能**（新增"二、设计原则"）。Phase 2.5 在 Phase 2 实施前补的，让"日常运维不痛"先于"模型组新能力"。

**V4.4-V4.6 新增原则**：
- **隐式先于显式**：从 usage_logs 推断的反馈先做（0 成本），Dashboard 打星后做
- **DB 先于 .env**：账号配置走 DB（密文 + 即时生效），.env 退化为启动引导
- **告警驱动而非基建驱动**：先用单账号 + 告警提醒注册新账号，而非提前建多账号基础设施

---

## Phase 1: 网关核心（已完成）

> 目标：跑通"请求进来 → 选最便宜渠道 → 透传出去"的核心链路

- ✅ FastAPI 网关骨架 + OpenAI 兼容接口
- ✅ 流式/非流式透传（aiter_raw，不解析 SSE）
- ✅ 路由决策引擎（同模型多渠道选优）
- ✅ 内存频限器（QPM/TPM/并发/日限额）
- ✅ 多账号轮询 + 故障转移（fallback）
- ✅ SQLite WAL + SQLAlchemy 2.0（8 张核心表）
- ✅ 峰谷时段定价支持
- ✅ 网关鉴权（Bearer Token）
- ✅ 极简 Dashboard（htmx + Chart.js）
- ✅ pydantic-settings 配置管理
- ✅ 手动录入脚本（`scripts/manual_import.py`）

补充完成（不在原 Phase 1 计划内，但属于"基础架构"）：
- ✅ ModelCard 评测卡表（`scripts/seed_model_cards.py`，43 张国内+开源模型 + OpenCompass 4 榜引用分）
- ✅ 9 个 channel（5 直连 + 1 聚合硅基流动 + 3 国内直连火山/混元/阶跃）
- ✅ Proxy 失败告警（5xx + 401/403 → 飞书/Bark/Server酱）
- ✅ 精确模型路由 fallback 到 priority 最低 channel（聚合平台兜底）

---

## Phase 2.5: 运维加固（已完成）

> 目标：让"日常运维不痛"——重启、改 key、查日志、接新 model 这 4 件事不再需要重启 uvicorn 或刷屏告警

| # | 改动 | commit | 痛点 |
|---|------|--------|------|
| #1 | 拉硅基流动 91 个 model 入库 | `636c334` | `/v1/models` 之前只列 2 个，掩盖了"硅基流动 1 key 通吃"的核心能力 |
| #3 | 告警 throttle（同 key 60s 只发 1 次） | `244f279` | 硅基流动挂掉时飞书会被刷屏 |
| #4 | pre-commit hook 防 `.env` 误 commit | local config | 真实 API key 不能进 git |
| #5 | `POST /dashboard/api/cache/refresh` 手动刷新 | `e50bb7a` | 改 channel key 后不想等 5 分钟自动刷新 |

未做：~~#2 router 503 告警~~（fallback 启用后几乎不返回 503，价值小，留作未来）。

详细设计/验证/使用见 [architecture.md §八 运维加固](architecture.md#八运维加固phase-25-已完成)。

---

## API-Sec-1: DB api_key 加密存储（已完成）

> 目标：DB 文件泄露 ≠ channel key 泄露；`.env` 漏了不会带出所有 key
> 背景调研：[api-key-security.md](api-key-security.md) — Infisical 启示 + 现状盘点 + 明确不做的方案
> 关联：[architecture.md §六 数据模型 加密字段](architecture.md#现有表扩展加密字段) / [§十 关键设计决策](architecture.md#十关键设计决策)

### 1.1 加密方案（轻量化自实现）

- **算法**：**XOR 循环 + Base64**（`security/crypto.py` ~30 行，**零外部依赖**）
- **密钥派生**：用 `secrets.token_hex(32)` 生成 64 字符 hex master key（32 bytes 原始）
- **主密钥**：`AIMC_MASTER_KEY`（64 字符 hex），存 `.env`（跟现有模式一致）
- **不加密 .env 本身**（避免"开机输入 master key 解密 .env 才能解 DB"的鸡生蛋问题）
- **每次部署重新生成**（无跨环境同步需求，丢了 = 重新部署）
- **诚实评估**：XOR 不是密码学标准加密（已知明文能解），但在本项目场景下安全等级 ≈ Fernet（master key 跟 .env 在一起时 Fernet 也救不了）

### 1.2 DB schema

```sql
ALTER TABLE accounts ADD COLUMN api_key_ciphertext BLOB;
-- api_key TEXT 保留, 标记 deprecated (双读兜底, 迁移期使用)
```

**双读策略**：
- 读：优先 `api_key_ciphertext`（解密），fallback 到 `api_key`（明文，仅迁移期）
- 写：永远写 ciphertext，**不再写明文**到 `api_key` 字段

### 1.3 文件变更

| 操作 | 文件 | 说明 |
|------|------|------|
| 新增 | `security/__init__.py` | 包入口 |
| 新增 | `security/crypto.py` | XOR+Base64 自实现封装：`encrypt(plaintext, master_key)` / `decrypt(ciphertext, master_key)` / `generate_master_key()`。**零外部依赖**（只用 stdlib `base64` + `secrets`） |
| 新增 | `scripts/gen_master_key.py` | 一次性脚本：生成 64 字符 hex (32 bytes) master key |
| 新增 | `scripts/migrate_encrypt_keys.py` | 一次性脚本：把老明文 api_key 转 ciphertext |
| 修改 | `db/models.py` | `accounts` 加 `api_key_ciphertext` BLOB 字段 |
| 修改 | `db/crud.py` | `get_account` 改：透明解密返回明文 |
| 修改 | `gateway/router.py` | `refresh_cache` 用解密后的明文填 `channel["api_key"]` |
| 修改 | `scripts/manual_import.py` | 写新 channel 时 encrypt 入 ciphertext |
| 修改 | `.env.example` | 加 `AIMC_MASTER_KEY=<run scripts/gen_master_key.py>` 占位 |

### 1.4 验证标准

- [x] 启动时 `AIMC_MASTER_KEY` 缺失 → fail-fast 明确报错
- [x] 启动时 `AIMC_MASTER_KEY` 错误（hex 长度不对）→ fail-fast 明确报错
- [x] 启动后 chat / fallback / throttle 行为不变（功能不回归，E2E pong 200）
- [x] `python -c "import sqlite3; ..."` 查 `accounts.api_key` 全是空串，`api_key_ciphertext` 是 BLOB（看不到明文）
- [x] 跑 `scripts/migrate_encrypt_keys.py` 后老数据成功转 ciphertext
- [x] 重跑迁移脚本 idempotent（已迁移的跳过）
- [x] 8/8 单元测试通过（`security/crypto.py` roundtrip / 空串 / None / 错 key / 1000 chars / 空 key / 错 hex / 大 plaintext）

---

## API-Sec-2: ALERT_CHANNEL 改 DB 存储 + 不重启切换（已完成, 2026-07-29）

> 目标：dashboard 改 ALERT_CHANNEL / BARK_KEY / FEISHU_WEBHOOK_URL 后**不用重启**立即生效

### 2.1 现状痛点

`config.py` 用 Pydantic Settings 启动时一次性 load，`.env` 改了 `ALERT_CHANNEL` 必须重启 uvicorn。

### 2.2 方案

加 `settings_runtime` 表（k-v 存储）：

```sql
CREATE TABLE settings_runtime (
    key        TEXT PRIMARY KEY,      -- "alert_channel" / "bark_key" / ...
    value      TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

加 endpoint `POST /dashboard/api/settings/alert`，alerter 改读 `settings_runtime` 表（先看 override，fallback Pydantic）。

### 2.3 文件变更

| 文件 | 改动 |
|------|------|
| `db/models.py` | 加 `SettingsRuntime` ORM |
| `db/crud.py` | `get_runtime_setting` / `set_runtime_setting` |
| `intelligence/alerter.py` | 改读 `settings_runtime` 表（带 cache） |
| `dashboard/routes.py` | 加 `GET/PUT /api/settings/alert` |
| API-Sec-1 `security/crypto.py` | 扩展加密 `bark_key` / `serverchan_key` / `feishu_webhook_url` 等敏感字段 |

### 2.4 验证标准

- [ ] 改 .env 重启 vs dashboard 改不重启 → 行为一致
- [ ] API-Sec-1 加密的 `bark_key` 通过 endpoint 写 → DB 存 ciphertext
- [ ] cache refresh（已有 #5 endpoint）也刷 `settings_runtime` cache

---

## API-Sec-3: Key 轮换提醒（已完成, 2026-07-29）

> 目标：dashboard 看到 key 何时更新、30 天未轮换飞书告警

### 3.1 schema 扩展

```sql
ALTER TABLE accounts ADD COLUMN last_rotated_at DATETIME;
ALTER TABLE accounts ADD COLUMN expires_at DATETIME;  -- 可选, user 自己定的轮换周期
```

### 3.2 文件变更

- `db/models.py` 2 个新列
- `intelligence/healer.py` 或新文件 `scripts/check_key_rotation.py` 每天扫
- `dashboard/static/index.html` 加 key 状态卡片

### 3.3 验证标准

- [ ] 30 天没轮换的 key → 飞书告警"建议轮换 SILICONFLOW_API_KEY"
- [ ] dashboard 卡片显示每个 key 的 last_rotated_at / 距今天数

---

## Phase 2: 多维模型组（已完成，2026-07-29）

> 目标：实现跨模型比价路由，hermes-agent-cn 请求"能力档"而非具体模型

### 2.1 数据层

- ⬜ 新增 `model_registry` 表（ORM 模型 + 迁移）
- ⬜ 新增 `model_groups` 表
- ⬜ 新增 `model_group_members` 表
- ⬜ 新增 `model_capabilities_log` 表
- ⬜ `channels` 表扩展：`provider_type`、`billing_mode` 字段
- ⬜ **处理 `model_cards` 与 `model_registry` 的关系**：是合并（`model_cards` 改名为 `model_registry` + 加 tier/capabilities 列）还是保留双轨（`model_cards` 仅存 benchmark，`model_registry` 存 tier/capabilities）
- ⬜ 编写模型组种子数据脚本（`scripts/seed_models.py`，参考 architecture.md §九 预置配置）

### 2.2 路由引擎升级

- ⬜ 模型组解析器：识别 `tier:`/`scene:` 前缀
- ⬜ 组内跨模型全局选优逻辑
- ⬜ hermes-agent-cn 偏好约束处理（`extra_body.aimc_preferences`）
- ⬜ 组间 fallback chain 递归降级
- ⬜ billing_mode 处理（free/subscription 价格视为 0）
- ⬜ 响应头注入（X-AIMC-Actual-Model / X-AIMC-Actual-Channel）
- ⬜ **渠道冷却机制**（借鉴 LiteLLM）：连续失败 N 次 → 进入冷却期 → 冷却期内跳过该渠道
- ⬜ **weighted 加权路由**（借鉴 LiteLLM）：基于 model_group_members.weight 做加权随机分配

### 2.3 模型组管理器

- ⬜ `gateway/group_manager.py`：组配置内存缓存
- ⬜ 组缓存定时刷新（与路由缓存同步）
- ⬜ `/v1/models` 接口升级：返回模型 + 模型组

### 2.4 成本追踪增强（借鉴 LiteLLM Cost Tracking）

- ⬜ 精确成本计算：响应返回后，用 price_snapshots 当前价格 × usage tokens 计算 cost_estimate
- ⬜ usage_logs 写入时自动关联渠道价格（不再依赖外部估算）
- ⬜ Dashboard 成本视图：按模型/按渠道/按日 聚合花费

### 2.5 Dashboard 扩展

- ⬜ 模型注册表管理页面（增删改查）
- ⬜ 模型组配置页面（组成员管理、策略选择）
- ⬜ 模型能力标签可视化（tier 分布图、能力雷达图）

### 2.6 验证标准

- [ ] `model="tier:balanced"` 请求能正确返回组内最便宜的模型响应
- [ ] 组内全部渠道频限满时，自动降级到 `fallback_group`
- [ ] `/v1/models` 正确返回模型组列表
- [ ] 精确模型名请求行为不变（向后兼容）
- [ ] Dashboard 可管理模型和组配置
- [ ] `#5` cache refresh endpoint 也刷模型组缓存（避免改完 seed_models.py 要等 5 分钟）
- [ ] 渠道连续失败 3 次后进入冷却，冷却期内不再被选中
- [ ] usage_logs.cost_estimate 与 price_snapshots 价格一致

---

## Phase 3: 情报联动模型组

> 目标：情报层自动维护模型注册表和组成员关系
> 关联文档：[agent-integration.md](agent-integration.md) — hermes-agent-cn × AIMC 协同方案

### 3.0 hermes-agent-cn 协同层（Phase 3 启动前先落地）— ✅

> 状态：全部完成 (commit `c4b6f1e` 之后)

- ✅ docs/agent-integration.md：3 方案对比 + 推荐 A+C 组合
- ✅ /v1/models 元信息增强（返回 `aimc_metadata` 字段含 family/tier/capabilities/modality/context_length/providers）— commit `d72f65b`
- ✅ **Dashboard 模型组路由分布（命中率聚合）**— commit `929e5fb`
  - `usage_logs` 加 `group_id` 列 + proxy.py 同步记录
  - Dashboard 新增 `GET /api/stats/group_routing` + Chart.js 饼图
  - **半天工作量，hermes-agent-cn 零代码改动**
- ✅ hermes-agent-cn 集成 PR（CCR profile 风格配置：background/think/longContext/default → group 名, 集成 PR 草案见 [hermes-agent-cn-integration-pr.md](hermes-agent-cn-integration-pr.md)）— commit `9b965ed`，PR 已提给 hermes-agent-cn 仓库
- ✅ **响应头增强** — commit `c4b6f1e` (Phase 3.E)
  - `X-AIMC-Actual-Tier`: 实际命中 model 的能力档
  - `X-AIMC-Actual-Capabilities`: 实际命中 model 的 capabilities (JSON)
  - 让 hermes-agent-cn 一次请求拿到 tier/capabilities, 不必二次查 /v1/models
- ✅ **PR 联调 E2E** — commit `c4b6f1e` (Phase 3.E)
  - `tests/test_hermes_agent_cn_e2e.py` 4 个场景全过: 启动拉取 / 组路由头 / 精确路由头 / Profile 校验
  - 验证 AIMC 侧准备就绪, hermes-agent-cn PR 可直接对接

### 3.1 采集源扩展 — ✅ (commit `f1c3487`)

- ✅ 新增"模型列表页"采集源类型（`model_lists.py`: SiliconFlow + VolcengineArk）— 发现新模型
- ✅ 新增"第三方平台"采集源（`price_aggregator.py`: SiliconFlow + OpenRouter 价格聚合）— 价格对比
- ✅ 新增"Benchmark 榜单"采集源（`benchmark.py`: LMSYS Arena + SuperCLUE）— 评测数据
- ✅ 采集源调度策略优化（`base.py` `DEFAULT_POLL_INTERVALS` 表，按类型差异化：模型列表 1 天、价格聚合 6 小时、榜单 1 周）

### 3.2 AI 解析器升级 — ✅ (commit `4666231`)

- ✅ 解析器支持提取模型元信息（model_id/family/context_length/capabilities）
- ✅ 新模型 tier 推断逻辑（基于参数规模、厂商定位、benchmark）
- ✅ 解析结果置信度分级处理

### 3.3 自动入组/出组 — ✅ (commit `ccb231d` + `b6aec1a` + `122a979` + `6a7e1e7`)

- ✅ 新模型自动入组（confidence > 0.8）
- ✅ 模型下线自动移出（confidence > 0.9，P1-2 软删除 + 路由 cache 不出现）
- ✅ 低置信度变更 → 飞书/Bark/Server酱 告警 + Dashboard 待确认队列
- ✅ tier 调整永远需人工确认（安全阀）

### 3.4 价格联动 — ✅ (commit `01792a5` + `f76cb46` + `d9c04c0` + `a4fbde2`)

- ✅ 价格变动自动更新 `price_snapshots`
- ✅ 路由缓存精准刷新（I6：invalidate_channel/invalidate_price/invalidate_group 3 个接口）
- ✅ 优惠活动影响渠道优先级（临时权重加成）

### 3.5 验证标准 — ✅ (tests/test_phase3_5_verification.py 4/4 PASS)

- [x] 模拟新模型发现 → 自动出现在 `model_registry` 和对应组中
- [x] 模拟价格变动 → 下次请求路由到新最低价渠道
- [x] 模拟模型下线 → 自动从组中移除，不再被路由
- [x] 低置信度变更 → 收到告警

### 3.6 国内服务商补齐 — 🔨 (本轮扩展, 阶段实现)

> 背景: 原 Phase 3.1 只接了 2 个模型列表源 (硅基流动 + 火山方舟), 国内还有 5 个主流厂商未接.
> 评测: 优先用各厂商提供的 `/v1/models` API 结构化抽取 (零 AI 成本, 100% 准确), HTML 抓取作为降级路径.

- 🔨 阶跃星辰 (StepFun) — `https://api.stepfun.com/v1/models`
- 🔨 商汤日日新 (SenseNova) — `https://api.sensenova.cn/v1/models` (备用 `https://token.sensenova.cn/v1/models`)
- 🔨 腾讯混元 (Hunyuan) — `https://api.hunyuan.cloud.tencent.com/v1/models`
- 🔨 月之暗面 (Moonshot / Kimi) — `https://api.moonshot.cn/v1/models`
- 🔨 DeepSeek / Qwen / GLM (之前只有定价页, 补 model_list 源)

### 3.7 潜在聚合源 — 🔍 评估中

- 🔍 **千问 AI 平台** (`https://www.qianwenai.com/`): 阿里运营的 C 端 web 应用, 代理了 Kimi / DeepSeek / GLM 等多模型。
  - **不建议接入**: 这是面向用户的前端应用, 无 OpenAI 兼容 API endpoint, HTML 结构常变。
  - 价值: 与硅基流动重叠 (同是聚合平台), 不增覆盖。
  - 评估结论: **不接入**, 价值低 + 维护成本高。

---

## V4.4: 反馈 + 告警 + CLI 账号管理（✅, 2026-07-29）

> 目标：**从"被动响应"升级到"主动洞察"** — 加个账号从 30 秒变为 10 秒；每周自动告诉你哪个模型最划算
> 详细设计：[v44-feedback-and-alert-design.md](v44-feedback-and-alert-design.md)
> 提交拆分: `edfad38` (V4.4 隐式反馈+告警+CLI) / `ad2bb8c` (V4.4.B 周报+Bark+timer) / `e426a3c` (V4.4.D 多账号引导)

### 4.1 隐式反馈系统（`model_feedback_summary`）

- ✅ 新增 `model_feedback_summary` 表 + 索引（init_db.py 幂等迁移）
- ✅ `scripts/aggregate_weekly_feedback.py` (1h): 从 usage_logs 周聚合，反馈分公式 `100 - 30*retry - 50*fail - 0.01*p95`
- ✅ `db/crud.py` 加 `upsert_feedback_summary` / `list_feedback_summary`

### 4.2 周报自动生成

- ✅ `scripts/generate_weekly_report.py` (1h): Markdown 模板（总览/排行 Top5/趋势/告警/推荐行动）
- ✅ `scripts/publish_report.py` (30min): Bark / 飞书推送关键结论
- ✅ systemd timer: 每周一 9:00 触发

### 4.3 告警驱动场景（4 个新增, `intelligence/v44_alerts.py`）

- ✅ `QuotaNearLimitAlert` (80%)
- ✅ `AccountExhaustedAlert` (100%)
- ✅ `NoActiveAccountAlert` (provider 全耗尽)
- ✅ `CostRisingAlert` (月环比 +30%)
- ✅ `scripts/expire_accounts.py` (30min): 过期扫描

### 4.4 账号管理 CLI

- ✅ `scripts/add_account.py` (1h): 加密入库，无需重启
- ✅ `scripts/list_accounts.py` (30min): 脱敏显示 sk-***-last4
- ✅ `scripts/rotate_account.py` (30min): disable / enable / rotate / expire-check
- ✅ `.env.example` 加 `_KEYS` 多 key 引导说明
- ✅ `install.sh` / `manual_import.py` 引导文案改用 CLI

### 4.5 验证标准

- [x] 跑 `aggregate_weekly_feedback.py` 过去 7 天数据不报错
- [x] 周一 9:00 自动生成 `reports/weekly_*.md`
- [x] Bark 收到推送（≤ 200 字关键结论）
- [x] `add_account.py` 加账号 → 加密入库 → 立即可路由
- [x] 80% / 100% 配额 → 收到对应告警
- [x] 故意耗尽某 provider → NoActiveAccount 告警 + 路由自动降级

---

## V4.5: 能力评分路由 + Dashboard 账号 UI（✅, 2026-07-29）

> 目标：把 V4.4 收集的反馈**反向驱动路由** + 提供 Web UI 管理账号
> 提交拆分: `359a4dd` (V4.5 Dashboard 周报 + V4.4 测试补齐) / `4959dcb` (V4.6 Dashboard 账号 UI) / `72d12bd` (V4.5.1 能力评分路由)

### 5.1 能力评分路由（whichllm 启发）

- ✅ `model_cards` 表增强 5 字段: `confidence_level` / `source_type` / `score_components` / `last_active_at` / `source_reliability`
- ✅ `BenchmarkSource` 抽象基类 (类似 intelligence/sources/ 模式)
- ✅ LMSYSArena + OpenCompass 2 个 source 适配器 (后续 V4.6.3 扩展到 5 sources)
- ✅ `gateway/router.py` sort_key 加 `score_bonus` (权重 ≤ 0.2)
- ✅ `intelligence/recency.py`: recency-aware 时间衰减工具函数（半年线性衰减）

### 5.2 Dashboard 账号管理 UI

- ✅ `/dashboard/accounts` 页面：列表 / 加 / 轮换 / 启用 / 禁用
- ✅ Web 表单调用 CLI 等价 API
- ✅ 报告查看页面：`/dashboard/reports/weekly_*`
- ✅ API key 脱敏显示 + 复制按钮

### 5.3 告警闭环

- ✅ `ScoreRegressionAlert` (2 周反馈分持续下滑)
- ✅ `CostAnomalyAlert` 集成报告链接
- ✅ alerter 与周报双向引用

### 5.4 验证标准

- [x] score_bonus 加权后，能力强的模型（T0）排序优先，但不会完全压制便宜模型（T2/T3）
- [x] Dashboard 能加账号（无需 SSH）
- [x] Dashboard 能看历史报告（Markdown 渲染）
- [x] 反馈分下滑模型自动告警

---

## V4.6: 告警驱动多账号 + whichllm 多源 benchmark（✅, 2026-08-08）

> 本轮实施: §6.1 多账号架构 + §6.3 whichllm 多源 benchmark, 共 ~10h.
> §6.2 显式打星主动放弃 (理由见下).

### 6.1 多账号架构 (commit `4fd06c2`)

- ✅ 路由加 least-used 策略: 同价同分时选最近未被路由的账号 (避免突发流量打死单个账号)
- ✅ `CooldownManager`: 失败 1 次 → cooldown 60s; 路由跳过; 过期自动恢复
  - 借鉴 LiteLLM cooldown 机制, 但使用阈值=1 (个人使用场景容错低, 1 次失败就跳)
  - 内存管理 (重启清零), 跟 limiter 一致
- ✅ `scripts/multi_account_import.py`: 从 `.env` 的 `_KEYS` 多 key 批量入库
  - 9 个 provider (deepseek/qwen/glm/kimi/minimax/siliconflow/volcengine/hunyuan/stepfun)
  - **位置式命名** (env[i] → provider_NN) 保证幂等性, 重跑不会乱分配
  - 只创建账号, 不创建 channel (channel 创建是 manual_import.py 职责)

### 6.2 显式用户反馈 (⏸ 主动放弃)

- ⏸ Dashboard 模型打星组件: **不做**
- ⏸ 反馈权重入路由: **不做**
- ⏸ 用户反馈 vs 公开 benchmark 分歧检测: **不做**

**不做理由** (按 V4.4 设计原则“隐式先于显式”):
1. **依赖客户端**: 打星需 hermes-agent-cn / hermes-tray 配合, 跨项目同步成本高
2. **样本稀疏**: 个人使用场景, 反馈数据量小, 路由权重(≤0.15)难以发挥作用
3. **周报已覆盖**: V4.4.B 周报自动生成“最划算”推荐, 用户据此手动调整已足够
4. **可靠性不足**: 隐式反馈 (usage_logs + retry/fail) 已是高质量信号, 显式打星价值增量不明确

**未来重拾条件**: 用户量增长 (hermes-tray 多客户端) 或外部反馈需求出现

### 6.3 whichllm 多源 benchmark (commit `d8cdad4`)

- ✅ `BenchmarkEntry` dataclass + `BenchmarkSource` 抽象基类
- ✅ 5 个 source: LMSYS (reliability=0.95) / OpenCompass (0.90) / SuperCLUE (0.80) / LiveBench (0.85) / Artificial Analysis (0.85)
- ✅ 3 个新 source 模块 (opencompass.py / livebench.py / artificial_analysis.py) + mock fallback (Playwright 未装)
- ✅ `benchmark_aggregator.py`: `weight = source_reliability × recency_weight(fetched_at)`, `composite = Σ(score×weight) / Σweight`
- ✅ `scripts/refresh_benchmark.py`: 5 sources 容错 + upsert 到 model_cards
- ✅ **路由 sort_key 加 `score_bonus` 项** (V4.5.1 引进, V4.6.3 复用)

### 6.4 验证标准

- [x] 2 个账号同价 → 路由 least-used 分散 (`test_v46_cooldown.py` Test 10)
- [x] key 失败 1 次 → cooldown 60s, 路由跳过 (`test_v46_cooldown.py` Test 8/9 + `test_v46_e2e.py` 4 阶段端到端)
- [x] 2 个账号都失败 → select_channel 返 None → 上层 503 (`test_v46_e2e.py` Test 2)
- [x] 4xx 不触发 cooldown (`test_v46_e2e.py` Test 3)
- [x] 多源加权融合准确 (`test_v46_3_benchmark.py` 13/13)
- [ ] ~~Dashboard 打星 1 次 → 下次路由 score 体现~~ (§6.2 主动放弃)

### 6.5 e2e 试出的 2 个集成缺陷修复 (commit `999b01d`)

跑 `tests/test_v46_e2e.py` 时发现:
1. **`_forward_normal` 缺 `requested_model` 参数** → `NameError` → `forward_request` 兜底 500. 修: 透传参数。
2. **`select_best_channel` candidates 过滤循环漏检 cooldown** → 冷却账号仍被选上. 修: 加 `cooldown_manager.is_in_cooldown()` 检查.

**这两个 bug 在 V4.6.1 commit `4fd06c2` 时未暴露**, 因为单测只调路由/manager层, 没走完 `forward_request → httpx → _update_cooldown` 链路. **e2e 集成测试是发现这类 bug 的唯一有效手段**。

---

## V4.7+ 远期展望（参考）

| 阶段 | 主题 | 价值 |
|------|------|------|
| **参数兼容处理** | 借鉴 LiteLLM `drop_params`：跨模型转发时自动丢弃目标模型不支持的参数 | 避免 400 错误 |
| **Virtual Key 成本隔离** | 借鉴 LiteLLM Virtual Key：不同客户端分配独立 Key，分别追踪预算 | 4 客户端分别计费 |
| **价格趋势预测** | 基于历史快照预测峰谷时段，提前切换 | 节省成本 |
| **Playwright 浏览器采集** | 装 chromium，启用 `playwright` 抓 HTML 源（qianwenai 等） | 补全情报覆盖 |
| **跨项目联动** | hermes-tray AIMC SSE `routing_decision` 事件消费 | 客户端可视化路由 |

---

## 技术债务 / 已知限制

| 项目 | 现状 | 计划 |
|------|------|------|
| 路由缓存刷新 | 5min 自动 + 手动 endpoint (`#5`) | Phase 3 改为事件驱动（价格变动即刷新） |
| 模型组缓存刷新 | 与路由缓存同 5min | Phase 2 独立刷新逻辑（避免等 5min） |
| 频限器持久化 | 纯内存，重启丢失 | 可接受（重启后窗口自然重置） |
| **API Key 加密** | **✅ API-Sec-1 完成** | **DB `api_key_ciphertext` BLOB 字段 + XOR+Base64 轻量化** |
| `model_cards` vs `model_registry` | 9 张表 (`model_cards` 已实现) + 4 张表待加 | Phase 2 处理双轨关系（合并 or 保留） |
| 并发安全 | 单进程无锁 | 足够（个人使用场景） |
| Playwright 浏览器二进制 | 未装（采集功能不可用） | 个人用可接受，未来按需装 |
| **多账号冷却** | **✅ V4.6.1 完成** | **失败 1 次 → cooldown 60s → 过期自动恢复** |
| 成本计算 | 简单估算 | Phase 2 用 price_snapshots 精确计算 |

---

## 文件变更预估（Phase 2）

| 操作 | 文件 | 说明 |
|------|------|------|
| 新增 | `db/models.py` | 追加 4 个 ORM 类 |
| 新增 | `gateway/group_manager.py` | 模型组管理器 |
| 修改 | `gateway/router.py` | 增加组路由逻辑 |
| 修改 | `gateway/routes.py` | `/v1/models` 升级 |
| 修改 | `gateway/proxy.py` | 响应头注入 |
| 新增 | `schemas/model_group.py` | Pydantic 模型 |
| 新增 | `scripts/seed_models.py` | 种子数据导入 |
| 修改 | `dashboard/routes.py` | 模型组管理 API |
| 修改 | `dashboard/static/app.js` | 模型组管理 UI |
| 修改 | `db/crud.py` | 新增 CRUD 函数 |

---

## 跨项目参考

- **hermes-tray**（`D:\work\workspace\Qoder\hermes-tray`）的 v0.3.0 计划在 Phase 1 消费 aimc 注入的 SSE `routing_decision` 事件（footer pill + stats page）。注意：hermes-tray 是客户端，不直接调 AIMC, 应通过 hermes-agent-cn 中转。
- Phase 2 完成后，**hermes-agent-cn** 可切换到 `model="tier:balanced"` 调用方式，跟"能力档"对齐；hermes-tray 通过 hermes-agent-cn 间接受益。
- 两侧 commit 走各自仓库，联调在各自 phase 完成后做。
