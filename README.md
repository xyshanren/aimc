# 🧭 AIMC - AI Model Compass

> 让你的 Token 花在刀刃上

AIMC（AI Model Compass）是一款专为个人开发者设计的**轻量级 LLM 网关与优惠情报聚合工具**。

它持续监控 DeepSeek、Qwen、GLM 等主流厂商的定价与活动变动，结合实时时段与频限状态，在完全不侵入业务代码的前提下，将你的 API 请求自动透传路由至当前最具成本效益的渠道。旨在用最低的维护成本，榨干每一分优惠额度。

## 📖 文档

**→ 所有文档：[docs/index.md](docs/index.md)**（按用户 / 开发者 / 运维分类）

新用户从 [docs/user/install.md](docs/user/install.md) 开始；开发者从 [docs/dev/setup.md](docs/dev/setup.md) 开始。

## 核心特性

- **智能路由** — 基于时段/价格/配额/频限多维权重，自动选择最优渠道
- **多维模型组 (Phase 2)** — 按 `tier:xxx` / `scene:xxx` 前缀调用，AIMC 在组内跨 model 选最便宜
- **零侵入透传** — 兼容 OpenAI SDK，流式/非流式原样透传
- **情报聚合** — 定时爬取厂商定价页 + 活动页，AI 结构化提取优惠信息
- **多维限额** — 7 维度（rpm/tpm/daily/weekly/5h/concurrent/balance），超限自动 failover
- **Prompt Cache 真实集成** — 自动注入 `cache_control`（Anthropic）/ `prompt_cache_key`（OpenAI），cache 命中省钱
- **多账号 cooldown + score-based 选择** — 失败账号自动冷却，5 维加权选最优
- **Dashboard 账号管理 UI** — 无需 SSH，Web 加/轮换/启用/禁用账号
- **周报自动生成 + 建议中心** — 周一 09:00 周报 + 自动发现 model 错配建议
- **稳定期使用分析** — `scripts/analyze_usage.py` 自动提取 V4.7+ 优先级信号

## 快速开始

```bash
# 1. 安装
git clone git@gitee.com:XiaoYRecluse/aimc.git
cd aimc
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置
cp .env.example .env
# 编辑 .env 填入 API Key

# 3. 启动
python scripts/manual_import.py    # 录入渠道
python main.py                       # 启动网关 (默认 :8080)

# 4. 使用（OpenAI SDK）
from openai import OpenAI
client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="your-aimc-key",
)
response = client.chat.completions.create(
    model="deepseek-v4-pro",  # 或 "tier:flagship" / "scene:code"
    messages=[{"role": "user", "content": "Hello!"}],
)
```

更详细的安装 / 接入 / Dashboard / 故障排查见 [docs/user/](docs/user/)。

## 项目结构

```
aimc/
├── gateway/                # 网关核心 (router / proxy / quota / cooldown)
├── intelligence/           # 情报采集 (scheduler / sources / analytics)
├── db/                     # 数据库层 (含 7 个 migration)
├── dashboard/              # Dashboard 后端 (FastAPI routes) + 前端 (htmx + chart.js)
├── scripts/                # 运维脚本 (sync_prices / apply_suggestions / weekly_report)
├── tests/                  # 74 个测试 (pytest, 9.6s)
└── docs/                   # 完整文档 (见 docs/index.md)
```

## 技术栈

| 层级 | 选型 |
|------|------|
| 网关 | FastAPI + httpx |
| 数据库 | SQLite (WAL) + SQLAlchemy 2.0 |
| 调度 | APScheduler |
| 爬虫 | Playwright (async) |
| Dashboard | htmx + Chart.js |
| 测试 | pytest + pytest-asyncio |

## 最近变更（2026-08-14~15）

21 个 commit 架构整改完成：
- **P0-1~6**：db 强约束 / cache 同步 / multimodal 跳过 / TTL / encryption / 多维限额
- **P1-7~12**：dashboard 鉴权 / prompt cache / channels 灵活配置
- **阶段 1/2/3**：cache 可视化 / 38→20 jobs / score-based 路由
- **方案 A**：模型/分组 调整建议（4 model + 4 group 规则 + 置信度 + dashboard UI）
- **阶段 B/C/D**：SF 53 行真实价格 / 74 tests / 文档同步

详细列表见 [docs/dev/post-merge-fixes.md](docs/dev/post-merge-fixes.md)。

## 许可证

[MulanPSL-2.0（木兰宽松许可证）](LICENSE)
