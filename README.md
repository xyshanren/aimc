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
- **多账号兜底** — 同厂商多账号轮询，配额耗尽自动切换
- **响应头可观测 (Phase 2.5)** — `X-AIMC-Actual-Model/Channel/Group-Id` 头告诉 Agent 实际命中
- **API Key 加密 (API-Sec-1)** — DB 文件泄露 ≠ channel key 泄露（XOR+Base64 轻量化，零外部依赖）
- **告警 throttle (Phase 2.5)** — 同 key 60s 内只发 1 次，防止飞书刷屏
- **自愈循环** — AI 解析置信度评估，高置信自动入库，低置信告警人工
- **极简部署** — 单进程、单文件数据库、零外部依赖

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

## 项目结构

```
aimc/
├── main.py                 # 入口
├── config.py               # 全局配置
├── gateway/                # 网关核心
│   ├── app.py              # FastAPI 工厂
│   ├── routes.py           # API 端点
│   ├── proxy.py            # 流式透传
│   ├── router.py           # 路由决策引擎
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
│   └── sources/            # 采集源插件
├── db/                     # 数据库层
├── schemas/                # Pydantic 模型
├── dashboard/              # 极简 Dashboard
├── scripts/                # 运维脚本
└── docs/                   # 项目文档
```

## 文档

- [架构设计文档](docs/architecture.md) — 系统设计与数据模型（含 Phase 2 多维模型组）
- [开发计划](docs/development-plan.md) — 路线总览 (Phase 1/2.5/2/3/4 + API-Sec-1/2/3)
- [Phase 2 设计文档](docs/phase2-design.md) — 模型组设计 + 实施步骤
- [API Key 安全设计](docs/api-key-security.md) — 加密方案 + 现状盘点
- [快速开始指南](docs/quickstart.md) — 安装、配置、部署
- [API 接口文档](docs/api.md) — 网关接口说明
- [竞品调研](docs/competitive-research.md) — OpenRouter/9Router/LiteLLM/RouteLLM 对比

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
