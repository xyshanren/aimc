# 🧭 AIMC - AI Model Compass

> 让你的 Token 花在刀刃上

AIMC（AI Model Compass）是一款专为个人开发者设计的**轻量级 LLM 网关与优惠情报聚合工具**。

它持续监控 DeepSeek、Qwen、GLM 等主流厂商的定价与活动变动，结合实时时段与频限状态，在完全不侵入业务代码的前提下，将你的 API 请求自动透传路由至当前最具成本效益的渠道。旨在用最低的维护成本，榨干每一分优惠额度。

## 📖 文档

**→ 所有文档：[docs/index.md](docs/index.md)**（按用户 / 开发者 / 运维分类）

新用户从 [docs/user/install.md](docs/user/install.md) 开始；开发者从 [docs/dev/setup.md](docs/dev/setup.md) 开始。

## 核心特性

- **智能路由** — 基于时段/价格/配额/频限多维权重，自动选择最优渠道
- **多维模型组 (V6.0 分级)** — 按 `tier:xxx` 前缀调用（支持 `tier:vision/deepseek-v4-flash-vision-exp` 组内指定），组内跨 model 选最便宜；vision → vision-light → light 分级 fallback，接入 GLM 官方免费模型（GLM-4.7-Flash 文本 / GLM-4.6V-Flash 视觉）
- **标价双轨成本审计 (V6.0)** — 免费/订阅渠道实付 0 也按官方标价量化"省了多少"，4 个成本 API + Dashboard 成本审计卡 + 周分析 💰 信号
- **零侵入透传** — 兼容 OpenAI SDK，流式/非流式原样透传
- **多用户 Key (V4.8)** — `aimc-` 前缀按人发 key、按人记账（caller_id）、个人日/月限额、Dashboard 按人用量 + Key 管理
- **情报聚合** — 定时爬取厂商定价页 + 活动页，AI 结构化提取优惠信息（低置信度自动过滤）
- **多维限额** — 账号 7 维度（rpm/tpm/daily/weekly/5h/concurrent/balance）+ 用户日/月维度，超限自动 failover
- **Prompt Cache 真实集成** — 稳定前缀分段 hash 注入 `prompt_cache_key`（OpenAI）/ `cache_control`（Anthropic），cache 命中省钱（实测 7 天命中率 91.8%，省 ¥137）
- **多账号 cooldown + score-based 选择** — 失败账号自动冷却，5 维加权选最优
- **Dashboard 账号管理 UI** — 无需 SSH，Web 加/轮换/启用/禁用账号；采集源管理（V4.8.3）
- **周报自动生成 + 建议中心** — systemd timer 周一 09:00 周报 + 飞书/Bark 推送 + 自动发现 model 错配建议
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
python scripts/manual_import.py    # 录入渠道 (可选, V6.3 起空渠道仅 WARN 引导, 也可启动后 Dashboard 加)
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
├── db/                     # 数据库层 (含 21 个 migration)
├── dashboard/              # Dashboard 后端 (FastAPI routes) + 前端 (原生 JS + Chart.js)
├── scripts/                # 运维脚本 (sync_prices / apply_suggestions / weekly_report)
├── tests/                  # 694 个测试 (pytest, ~5min, 全绿)
└── docs/                   # 完整文档 (见 docs/index.md)
```

## 技术栈

| 层级 | 选型 |
|------|------|
| 网关 | FastAPI + httpx |
| 数据库 | SQLite (WAL) + SQLAlchemy 2.0 |
| 调度 | APScheduler |
| 爬虫 | Playwright (async) |
| Dashboard | 原生 JS + Chart.js |
| 测试 | pytest + pytest-asyncio |

## 最近变更（2026-08-16~25）

- **A-2 采集健康盲区告警（2026-09-08）**：修复情报系统"失明两周无人知晓"（错误页字节级稳定 → differ 判无变化 → 14 天零变化检测零告警）。新增 `intelligence/content_guard.py` 内容健康检查（错误页特征库：`__next_error__`/404 title/Chromium 网络错误/未渲染 SPA + 500B 最短长度，只查前 4KB 防误报），fetch 后 differ 前挂钩——不健康内容不写 last_content（不污染 hash 基线），走既有 handle_fetch_error 通道（error_count+1，连续 3 次禁用+告警，恢复自动通知）；新增 `intel_blindspot_audit_task`（每 6h，全部在采 HTML 源 `last_changed_at` 距今 >7 天 → "系统疑似失明"告警，throttle 3 天）；intel_sources 加 `last_changed_at` 列（ensure_migrations 幂等 ALTER）；周报新增「情报采集健康」章节（error 态源/缺 key 空转/距上次变化天数）

- **V4.9.5 SenseNova 模型接入**：sensenova-6.8-flash-lite 加入 model_registry + tier:balanced（modality=text，公测免费 price=0.0）；glm-5.2 加入 sensenova_official 渠道 price_snapshots 作为免费第二渠道（cheapest 策略下 0.0 胜过 siliconflow 8.0）；VM 迁移脚本 `db/migrations/2026_08_25_add_sensenova_models.py` 幂等 INSERT

- **V4.8 多用户**：users/api_keys 表、`aimc-` key 按人记账 + 日/月限额、CLI（add_user_key）、Dashboard 按人用量 + Key 管理
- **P3 工程加固**：e2e 测试 MockTransport 化（CI 可跑）；周报 systemd timer + 飞书推送
- **路径 A 缓存优化**：prompt_cache_key 稳定前缀分段 hash（多轮对话命中率提升）
- **生产级修复**：usage_logs UTC→本地时钟（quota 漏判）、聚合渠道价格 999 兜底、group_id 归一化、.env 行内注释陷阱
- **Dashboard 治理**：V4.8 CSS 补全、待审 JSON 格式化、优惠情报垃圾过滤（1002→81 条）
- **V4.8.1 告警分级静默 + 按人用量下钻**：采集失败首次静默 30min/连续静默 2h；按人用量可展开到 key label
- **V4.8.3 采集系统优化**：调度按 (provider, poll_interval) 分组修复 model_list 源被拉高频、自适应降频（无变化 ≥4 次降 4x）、采集源管理卡片（总源 34→37）
- **V4.8.4 Playwright 进程回收 + 低置信度审查队列**：fetcher 4 层防护（Lock + pw 句柄 + Semaphore + wait_for 超时）；scheduler in-flight 追踪 + 优雅关闭；systemd ExecStartPre 清孤儿 + TimeoutStopSec；低置信度优惠审查队列闭环（promotion_review_queue 表 + 3 API + Dashboard 审查卡片）
- **V4.8.5 断链修复 + env_prefix + 浮点修复**：cost_estimate 实写入（激活成本告警/周报/看板省钱量化）；route_engine import 补全（批准/拍板后缓存即刻刷新）；CSS 降级 title 推断（降级路径不再被丢弃）；高置信度优惠→限时 promotion 价行（打通"情报→省钱"核心链路）+ price_snapshots 唯一索引迁移四元组；config.py `env_prefix="AIMC_"` 修复 .env 变量名不生效；benchmark aggregator 浮点精度 round(,2)
- **V4.8.6 渠道可用性校验 + 采集来源追踪 + pending 去重**：model_sync 入库前校验 has_available_channel（无渠道时 status=inactive 待用户配置）；log_capability_change 加 provider 参数记录采集来源；pending 行 upsert 去重避免同类信息无限堆积
- **V4.8.7 P5 drop_params 自动学习 + P6 分时段峰谷定价 + P7 429 渠道定位**：上游 400 时自动解析被拒参数并追加到 channel.drop_params（fire-and-forget + 白名单保护）；`_match_time_range` 支持工作日/周末/非高峰 token + DeepSeek 种子峰谷修正（工作日 9-12/14-18 高峰，其余低谷）；429 失败渠道链注入 `X-AIMC-Failed-Channels` 响应头 + `error.failed_channels` 响应体；新增 `GET /dashboard/api/cooldown` 冷却状态 API（含 remaining_seconds/in_cooldown）；stream 路径 429 cooldown 标记补齐
- **V4.9 失败链路增强 + 智能探活 + 人工干预**：(1) 错误分类器 `gateway/error_classifier.py` 8 种 ErrorCategory（quota_exhausted/auth_failed/rate_limited/model_unsupported/network_transient/upstream_5xx/client_error/unknown），failed_channels 响应头扩展 `error_type/error_code/error_msg/needs_human`，新增 `X-AIMC-Last-Error-Type` / `X-AIMC-Last-Error-Code` / `Retry-After: 3600`（needs_human 触发）；(2) CooldownManager 状态机化 5 状态（healthy/failing/degraded/needs_human/manual_disabled）+ 指数退避 + 15 个新方法；(3) `usage_logs` 加 error_msg 等 5 列 + `accounts` 加 last_error_at 等 4 列 + 新表 `account_health_log`（探活时间序列，走 `ensure_migrations` 幂等 ALTER，不生成新 .py migration 脚本）；(4) 智能探活 job `channel_health_probe`（每 60s，仅探 `accounts_due_for_probe()` 的 account，needs_human 不主动探）+ `cooldown_cleanup_task`（每 5min 清理过期 entry）；(5) Dashboard **🔴 待处理 (N)** tab + 5 个新 API：`/api/pending-summary` 聚合 + `/api/cooldown/needs-human` 详情 + `/api/cooldown/{aid}/ack-recover` 确认恢复（立即探活）+ `/api/accounts/{aid}/manual-{disable,enable}`；(6) 8 个新 env：`AIMC_HEALTH_PROBE_{ENABLED,INTERVAL,CONCURRENCY,TIMEOUT,MAX_BACKOFF}` + `AIMC_NEEDS_HUMAN_{QUOTA,AUTH,MODEL}_THRESHOLD`
- **V4.9.1 探活防护（吸取 Playwright OOM 教训）**：(1) **共享 httpx.AsyncClient 单例**（模块级 `_PROBE_CLIENT`，连接池上限 20 / keepalive 10，app shutdown 调 `close_probe_client()` 释放）；(2) **防雪崩**：`AIMC_HEALTH_PROBE_MAX_BATCH=50`（每 job 最多探活数，超出下个 job 再探）+ `AIMC_HEALTH_PROBE_TOTAL_TIMEOUT=60s`（job 整体 hard timeout，超时回填 NETWORK_TRANSIENT）；(3) **account_health_log 7 天 retention**：新 job `account_health_log_cleanup`（凌晨 3 点跑），`AIMC_ACCOUNT_HEALTH_LOG_RETENTION_DAYS=7` 可调，防探活日志表无限增长；(4) **APScheduler pickle bug 修复**：新增模块级 wrapper `account_health_log_cleanup_task`（lambda 不能被 SQLAlchemyJobStore pickle）。**部署验证清单**：部署后 `grep 'Added job' /tmp/aimc.log` 应出现 `探活日志清理 (保留 N 天)`，且 `grep ValueError /tmp/aimc.log` 应为空（否则 scheduler 静默挂了）。

更早的 21 commit 架构整改（2026-08-14~15）见 [docs/dev/post-merge-fixes.md](docs/dev/post-merge-fixes.md)，
路线图见 [docs/dev/next-step-plan.md](docs/dev/next-step-plan.md)。

**V5.0 ~ V6.3.1 之后的变更**（2026-08-27 起：模型组分级 vision-light、标价双轨成本审计、
V6.1 agent 契约 + 统一 CLI、qwen 命名规范化、思考 token 计量、看板 Phase3 重排 + 指标统一、
V6.3 自动化收口、V6.3.1 冷却 badge）逐项见 [docs/index.md](docs/index.md) 项目状态区。

## 许可证

[MulanPSL-2.0（木兰宽松许可证）](LICENSE)