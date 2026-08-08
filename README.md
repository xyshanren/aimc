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
- **稳定期使用分析 (V4.6.4+)** — `scripts/analyze_usage.py` 从 usage_logs 自动提取 V4.7+ 优先级信号 (Shannon entropy / 4xx 占比 / 时段差异 / 自动 failover 命中率)
- **自愈循环** — AI 解析置信度评估，高置信自动入库，低置信告警人工
- **极简部署** — 单进程、单文件数据库、零外部依赖

**当前状态**：V4.6 全章完成，进入稳定期（详见 `docs/development-plan.md` §7）。

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

## 稳定期跟踪（V4.7+ 决策依据）

V4.6 全章完成后进入稳定期。为驱动后续迭代选择，需累积使用反馈：

```bash
# 每周跑一次: 使用分析 + Markdown 报告 (含 V4.7+ 优先级建议)
python scripts/analyze_usage.py --days 7

# 每月跑一次: 月度趋势
python scripts/analyze_usage.py --days 30

# JSON 输出（给 Dashboard / 其他脚本消费）
python scripts/analyze_usage.py --json-only
```

**报告维度**：
- **status 分布**：V4.6.1 Cooldown 是否生效
- **多账号分散度**：Shannon entropy + 最大账号占比
- **model 集中度 / 4xx Top 5**：V4.7+ 缓存 / drop_params 信号
- **时段分布**：V4.7+ 智能调度价值
- **自动 failover 命中率**：V4.6.1 验证
- **V4.7+ 决策建议**：根据上述信号自动列出

报告输出：`reports/usage_analysis_latest.md`（人读）/ `--json-only` 模式只输出 JSON。

**判定准则**：任何 V4.7+ 候选项的启动 = 该信号连续 4 周出现 + 决策建议触发 + 时间充裕。

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
├── scripts/                # 运维脚本
│   ├── multi_account_import.py  # V4.6.1 多账号批量导入
│   ├── generate_weekly_report.py # V4.4.B 周报生成
│   ├── aggregate_weekly_feedback.py # V4.4.B 周报反债聚合
│   └── analyze_usage.py   # 稳定期使用分析 (V4.7+ 决策依据)
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
