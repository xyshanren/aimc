# 🧭 AIMC - AI Model Compass

> 让你的 Token 花在刀刃上

AIMC（AI Model Compass）是一款专为个人开发者设计的**轻量级 LLM 网关与优惠情报聚合工具**。

它持续监控 DeepSeek、Qwen、GLM 等主流厂商的定价与活动变动，结合实时时段与频限状态，在完全不侵入业务代码的前提下，将你的 API 请求自动透传路由至当前最具成本效益的渠道。旨在用最低的维护成本，榨干每一分优惠额度。

## 核心特性

- **智能路由** — 基于时段/价格/配额/频限多维权重，自动选择最优渠道
- **多维模型组 (Phase 2)** — 按 `tier:xxx` / `scene:xxx` 前缀调用，AIMC 在组内跨 model 选最便宜；支持 Agent 偏好 (prefer_family / exclude_providers / max_input_price)
- **零侵入透传** — 兼容 OpenAI SDK，流式/非流式原样透传，不解析 SSE
- **情报聚合** — 定时爬取厂商定价页+活动页，AI 结构化提取优惠信息
- **频限管理** — 内存滑动窗口追踪 QPM/TPM/并发/日限额，自动切换
- **多账号 least-used + Cooldown (V4.6.1)** — 同厂商多账号轮询分散流量；失败账号自动冷却跳过，冷却期后自动恢复
- **能力评分路由 (V4.5)** — whichllm 启发，model_cards 加权 tie-break，价格平手时高分 model 优先
- **多源 benchmark 加权 (V4.6.3)** — LMSYS / OpenCompass / LiveBench / SuperCLUE / Artificial Analysis 融合，reliability × recency 加权
- **响应头可观测 (Phase 2.5 + Phase 3.E)** — `X-AIMC-Actual-Model/Channel/Group-Id/Tier/Capabilities` 告诉 Agent 实际命中
- **API Key 加密 (API-Sec-1)** — DB 文件泄露 ≠ channel key 泄露（XOR+Base64 轻量化，零外部依赖）
- **告警 throttle (Phase 2.5)** — 同 key 60s 内只发 1 次，防止飞书刷屏
- **周报自动生成 (V4.4.B)** — 周一 9:00 自动 Markdown 周报 + Bark 推送关键结论
- **告警驱动多账号 (V4.4)** — 80%/100% 配额 + NoActiveAccount + CostRising 4 类告警
- **Dashboard 账号管理 UI (V4.6)** — 无需 SSH, Web 加/轮换/启用/禁用账号
- **Dashboard 周报查看页 (V4.5)** — Web 渲染历史 Markdown 周报
- **自愈循环** — AI 解析置信度评估，高置信自动入库，低置信告警人工
- **极简部署** — 单进程、单文件数据库、零外部依赖

## 近期变更（2026-08-14 一日内 14 个 commit）

> 以下功能自上次 README 更新后新增。完整 commit 列表见 `git log --oneline | head -20`

### 鉴权分层（P1-7）
- `AIMC_API_KEY` 保护 `/v1/*` API（OpenAI / Anthropic 兼容协议）
- `AIMC_DASHBOARD_TOKEN` 保护 `/dashboard/*`（**新加**，跟 API key 分开管理）
- 三种 token 传递方式：Authorization header / Cookie / `?token=` query
- 留空 = legacy 行为（LAN 内裸开，兼容旧部署）

### Prompt Cache 真正实现（P1-8）
- 自动注入 `cache_control` 字段（Anthropic 风格）/ `prompt_cache_key`（OpenAI 风格）
- 只在 `channel.supports_prompt_cache=1` 时启用
- 硅基流动 Qwen3-32B 实测 cache 命中率 4.6%，节省成本约 ¥0.004 / 7 天
- dashboard "Prompt Cache 节省" 卡片：按渠道 / TOP 10 model 实时统计

### 模型 / 分组调整建议（方案A）
- 4 个数据源综合分析（model_cards / usage / price / cache hit rate）
- 4 个 model 规则 + 4 个 group 规则
- 置信度 0–1，按 confidence DESC 排序
- **dashboard "建议中心"** UI：批准 / 拒绝 / 一键应用
- **周报集成**：周一 09:00 跑完 suggest_changes → 周报自动显示建议
- **影子模式**：`intelligence/auto_apply_shadow.log` 积累数据，为远期自动应用做准备

### 多维限额（P0-6）
- `account_quotas` + `account_quota_usage` 表，支持 7 种限额维度：
  - `rpm` / `tpm`（每分钟请求/Token）
  - `daily_tokens` / `weekly_tokens` / `5h_tokens`（窗口累计）
  - `concurrent`（并发请求数）
  - `balance_cny`（余额元）
- minimax TokenPlanMax 5h + 周 rolling 限额已配置
- sensenova 免费试用 + 并发限制
- routes.py 路由后 `await check_account_quota()`，超额返 429 + `retry-after`

### Router 强化（Phase 0-3）
- **C1 防御**：router cache 启动时容错 group_id 双形（int-string / string）
- **C2 6 tier 重组**：flagship / strong / balanced / light / vision / code
- **C3 priority tie-break**：同价同分时按 priority 排序
- **P0-3 multimodal 跳过**：纯文本请求跳过 modality=multimodal candidate（修 flagship 选 m3 拒纯文本的根因）
- **P0-4 TTL + mtime**：router cache 60s 自动 refresh，db 修改 60s 内生效
- **P0-7 score-based**：5 维加权（价格 0.4 / 配额 0.3 / 成功 0.2 / recency 0.05 / priority 0.05）替代 round-robin
- **P0-7 weights 调权**：dashboard `PUT /api/settings/router-score-weights` 改权重，60s 内 refresh 生效

### Channels 灵活配置（P1-12）
- `channels` 表加 3 列：`api_base` / `api_style` / `supports_prompt_cache`
- dashboard 改 base_url 不需 restart service
- `api_style` 区分 OpenAI / Anthropic 协议（后续 P2-20 用）
- 兼容 `SENSENOVA_BASE_URL` env 覆盖

### 数据通路完整性
- `usage_logs` 加 `cached_input_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens` 3 列
- 硅基流动 9 个 model 的 cache 价格已 hardcode（Qwen2.5/3 + DeepSeek V3/R1）
- `job_health` 表 + apscheduler event listener：每个 job 的 last_run / last_success / last_error / 累计成功失败
- dashboard "数据新鲜度" 卡片：38 → 20 jobs 后清晰展示每个 job 的健康度

### Scheduler 优化（阶段 2）
- 38 → 20 jobs：31 个 crawl_* 合并成 15 个 batch_crawl_<provider>
- `coalesce=True`：堆积任务合并到最新 1 次
- `max_instances=1`：同 job 不并发（避免限流）
- `misfire_grace_time=300`：5 分钟内延迟任务仍执行
- 启动时自动清理旧 per-source job（migration 脚本）

### 关键 Bug 修复
- **P0-5 encryption_version**：accounts 加 version 列，master_key 启动校验 64 字符 hex（错格式直接启动失败）
- **P0-8 listener greenlet_spawn**：用独立 `sync_engine`（`create_engine + check_same_thread=False`），避免 async 进程里用 sync Session 冲突
- **P1-7 dashboard 鉴权**：跟 api_key 分开，跟 `authFetch` JS 集成
- **routes.py 漏传 actual_model**：proxy.py body.model 改写恢复，body 不再透传 "tier:flagship" 给上游

## 系统架构

```
用户 (OpenAI SDK / HTTP)
        │
        ▼
┌─────────────────────────────────┐
│     自研网关层 (FastAPI)         │
│  路由引擎 │ 频限器 │ 流式透传    │
└────────────────┬────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐
│     SQLite (WAL 模式，单文件)    │
│  渠道 │ 价格 │ 优惠 │ 用量日志   │
└────────────────▲────────────────┘
                 │
┌────────────────┴────────────────┐
│     情报采集层 (同进程异步)       │
│  爬虫 │ Diff检测 │ AI解析 │ 告警 │
└─────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐
│     极简 Dashboard (htmx)       │
│  优惠排行 │ 渠道状态 │ 用量统计  │
└─────────────────────────────────┘
```

## 技术栈

| 层级 | 选型 | 理由 |
|------|------|------|
| 网关框架 | FastAPI + httpx | 原生异步，流式透传 |
| 数据库 | SQLite (WAL) + SQLAlchemy 2.0 | 零运维，单文件 |
| 定时调度 | APScheduler | 轻量，同进程 |
| 爬虫引擎 | Playwright (async) | JS 渲染支持 |
| AI 解析 | gpt-4o-mini (or MiniMax/OpenAI 兼容) | 成本极低 |
| 告警推送 | 飞书 / Bark / Server酱 | 国内首选，无需翻墙 |
| Dashboard | htmx + Chart.js | 零构建 |

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 API Key 等配置

# 3. 初始化数据库 + 录入渠道
python scripts/manual_import.py

# 4. 启动网关
uvicorn main:app --host 0.0.0.0 --port 8080
```

启动后，将 OpenAI SDK 的 `base_url` 指向 `http://localhost:8080/v1` 即可：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="your-aimc-key",  # 如配置了 AIMC_API_KEY
)

response = client.chat.completions.create(
    model="deepseek-chat",  # 自动路由到最便宜的渠道
    messages=[{"role": "user", "content": "Hello!"}],
)
```

## 本地模型接入（Ollama / vllm / LM Studio / llama.cpp）

AIMC 支持把本地跑的模型统一接入，享受跨云端/本地的智能路由。例如：
- `tier:light` 组里有云端免费档 + 本地小模型，AIMC 自动选最便宜的（本地永远 0 元）
- `scene:code` 组里同时有 DeepSeek-Coder 云端版 + 本地 Qwen2.5-Coder

支持的本地服务（OpenAI 兼容协议）：

| 服务        | 默认端口    | 默认 base_url                  |
|-------------|------------|--------------------------------|
| Ollama      | 11434      | `http://localhost:11434/v1`    |
| vllm        | 8000       | `http://localhost:8000/v1`     |
| LM Studio   | 1234       | `http://localhost:1234/v1`     |
| llama.cpp   | 8080       | `http://localhost:8080/v1`     |

**接入步骤（1 分钟）**：

```bash
# 1. 确认本地服务在跑（Ollama 示例）
ollama serve   # 默认监听 :11434
ollama pull qwen2:7b

# 2. （可选）自定义 base_url — 编辑 .env
# OLLAMA_BASE_URL=http://192.168.1.100:11434/v1

# 3. 运行接入脚本（自动探测 4 个服务 + 列出可用 model + 让你选）
python scripts/seed_local_channel.py

# 4. 重启 uvicorn 或 POST /dashboard/api/cache/refresh 刷新路由缓存

# 5. 验证
curl http://localhost:8080/v1/models | jq '.data[] | select(.id | contains("qwen2"))'
```

**特性**：
- `provider_type='local'` 自动识别，proxy 跳过 Authorization header
- `billing_mode='free'` 价格视为 0，路由时 `cheapest` 策略永远优先本地（节省云端费用 + 流量）
- 频限给 `qpm=9999`（本地无上游限速），但仍记录 TPM 用于成本/用量观测
- 模型名直接透传本地（如 `qwen2:7b`、`deepseek-r1:7b`），不需要改写

**典型用法 — 任务分级走不同档位**：
```python
# 后台日志摘要 → 本地小模型（免费）
response = client.chat.completions.create(model="tier:light", messages=...)

# 复杂代码生成 → 云端旗舰
response = client.chat.completions.create(model="tier:flagship", messages=...)

# 长上下文 RAG → 长上下文档
response = client.chat.completions.create(model="scene:longctx", messages=...)
```

## 项目结构

```
aimc/
├── main.py                 # 入口
├── config.py               # 全局配置
├── gateway/                # 网关核心
│   ├── app.py              # FastAPI 工厂
│   ├── routes.py           # API 端点
│   ├── proxy.py            # 流式透传 + cooldown hook (V4.6.1)
│   ├── router.py           # 路由决策引擎 + least-used (V4.6.1)
│   ├── cooldown.py         # CooldownManager (V4.6.1)
│   ├── limiter.py          # 频限器
│   ├── fallback.py         # 故障转移
│   └── middleware.py       # 日志/鉴权
├── intelligence/           # 情报采集层
│   ├── scheduler.py        # 定时调度
│   ├── fetcher.py          # 页面抓取
│   ├── differ.py           # Diff 检测
│   ├── parser.py           # AI 解析
│   ├── healer.py           # 自愈循环
│   ├── alerter.py          # 告警推送
│   ├── price_alert.py      # 价格波动告警 (V4.4)
│   ├── benchmark_aggregator.py # 多源 benchmark 加权 (V4.6.3)
│   └── sources/            # 采集源插件
├── db/                     # 数据库层
├── schemas/                # Pydantic 模型
├── dashboard/              # 极简 Dashboard
├── scripts/                # 运维脚本 (含 V4.6.1 multi_account_import.py)
├── security/               # API-Sec-1 XOR+Base64 加密 (V4.5)
└── docs/                   # 项目文档
```

## 文档

- [架构设计文档](docs/architecture.md) — 系统设计与数据模型（含 Phase 2 多维模型组 + V4.6.1 CooldownManager）
- [开发计划](docs/development-plan.md) — 路线总览 (Phase 1/2.5/2/3/4 + API-Sec-1/2/3 + V4.4-V4.6)
- [V4.4 设计文档](docs/v44-feedback-and-alert-design.md) — 隐式反馈 + 告警驱动多账号
- [Phase 2 设计文档](docs/phase2-design.md) — 模型组设计 + 实施步骤
- [API Key 安全设计](docs/api-key-security.md) — 加密方案 + 现状盘点
- [快速开始指南](docs/quickstart.md) — 安装、配置、部署
- [API 接口文档](docs/api.md) — 网关接口说明
- [竞品调研](docs/competitive-research.md) — OpenRouter/9Router/LiteLLM/RouteLLM 对比
- [E2E 测试报告](docs/e2e-results.md) — hermes-agent-cn 集成联调结果

## 设计原则

1. **自用优先** — 允许半自动，不追求 100% 无人值守
2. **情报层 > 网关层** — 情报发现是核心差异化价值
3. **先跑通再优化** — 快速验证核心价值
4. **保持简单** — 单进程部署，零外部依赖
5. **精细化运营** — 峰谷定价、频限追踪、多账号兜底

## 参与贡献

1. Fork 本仓库
2. 新建 Feature 分支 (`git checkout -b feature/xxx`)
3. 提交代码 (`git commit -m 'feat: xxx'`)
4. 推送分支 (`git push origin feature/xxx`)
5. 新建 Pull Request

## 许可证

[MulanPSL-2.0（木兰宽松许可证）](LICENSE)
