# AIMC 用户使用指引（完整版）

> 面向已部署 AIMC 的最终用户。
> 涵盖: 安装 → 接入账号 → 日常使用 → Dashboard → 运维 → FAQ。
> 配套文档: [README.md](../README.md) (项目概览) / [quickstart.md](quickstart.md) (极简版) / [api.md](api.md) (API 技术参考) / [development-plan.md](development-plan.md) (路线图)。

---

## 目录

- [1. 安装与首次启动](#1-安装与首次启动)
- [2. 接入厂商账号](#2-接入厂商账号)
  - [2.1 Dashboard 方式（推荐）](#21-dashboard-方式推荐)
  - [2.2 CLI 方式](#22-cli-方式)
  - [2.3 多账号批量导入（V4.6.1）](#23-多账号批量导入v461)
  - [2.4 手动编辑脚本方式](#24-手动编辑脚本方式)
- [3. 日常调用 AIMC](#3-日常调用-aimc)
  - [3.1 OpenAI SDK（Python）](#31-openai-sdkpython)
  - [3.2 OpenAI SDK（Node.js）](#32-openai-sdknodejs)
  - [3.3 curl](#33-curl)
  - [3.4 模型组路由（tier:xxx / scene:xxx）](#34-模型组路由tierxxx-sceneexxx)
  - [3.5 本地模型接入（Ollama / vllm）](#35-本地模型接入ollama--vllm)
- [4. Dashboard 全功能](#4-dashboard-全功能)
- [5. 监控 / 周报 / 告警](#5-监控--周报--告警)
- [6. 路由策略说明（用户视角）](#6-路由策略说明用户视角)
- [7. 运维操作](#7-运维操作)
  - [7.1 备份与恢复](#71-备份与恢复)
  - [7.2 重启服务](#72-重启服务)
  - [7.3 更新 AIMC](#73-更新-aimc)
  - [7.4 升级账号加密（API-Sec-1）](#74-升级账号加密api-sec-1)
- [8. FAQ 常见问题](#8-faq-常见问题)

---

## 1. 安装与首次启动

### 1.1 环境要求

- **Python 3.11+**（建议 3.12）
- 无需 Docker / Redis / PostgreSQL（单进程 + SQLite）
- 操作系统: Linux / macOS / Windows 均可（生产建议 Linux）
- 网络: 能访问目标 LLM 厂商 API + 情报源网站

### 1.2 安装步骤

```bash
# 1. 克隆仓库
git clone git@gitee.com:XiaoYRecluse/aimc.git
cd aimc

# 2. 创建虚拟环境（推荐）
python -m venv .venv
source .venv/bin/activate           # Linux/macOS
# .venv\Scripts\activate             # Windows PowerShell

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量
cp .env.example .env
# 编辑 .env 填入必要配置（见下文）
```

### 1.3 .env 必填项

| 变量 | 必填 | 说明 |
|------|------|------|
| `AIMC_HOST` | ❌ | 监听地址，默认 `0.0.0.0` |
| `AIMC_PORT` | ❌ | 监听端口，默认 `8080` |
| `AIMC_DB_PATH` | ❌ | 数据库路径，默认 `./data/aimc.db` |
| `AIMC_API_KEY` | ⚠️ | 网关鉴权 key，留空则不鉴权（**生产强烈建议填**） |
| `AIMC_MASTER_KEY` | ✅ | DB api_key 加密主密钥（API-Sec-1），首次启动自动生成并写入 .env |
| `OPENAI_API_KEY` | ⚠️ | 情报层 AI 解析用，V4.3+ 需要 |
| `OPENAI_BASE_URL` | ⚠️ | 解析模型 base_url，默认 `https://api.openai.com/v1` |
| `AIMC_PARSE_MODEL` | ⚠️ | 解析模型，默认 `gpt-4o-mini` |
| `TELEGRAM_BOT_TOKEN` | ❌ | Telegram 告警（可选） |
| `TELEGRAM_CHAT_ID` | ❌ | Telegram chat id（可选） |

> **安全提示**: `.env` 已在 `.gitignore` 中，**不会被提交到 git**。请勿手动 `git add .env`。

### 1.4 启动服务

```bash
# 开发模式
uvicorn main:app --host 0.0.0.0 --port 8080

# 生产模式（systemd）
sudo cp scripts/deploy/aimc.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aimc
sudo systemctl status aimc

# 验证
curl http://localhost:8080/health
```

启动成功后:
- 网关 API: `http://localhost:8080/v1/...`
- Dashboard: `http://localhost:8080/dashboard`

---

## 2. 接入厂商账号

AIMC 支持 3 种账号接入方式，**推荐 Dashboard 方式**。

### 2.1 Dashboard 方式（推荐）

1. 浏览器打开 `http://localhost:8080/dashboard/accounts`
2. 点击 **"新建账号"** 按钮
3. 填写表单:
   - 选择已有渠道（如 DeepSeek / Qwen / GLM），或先建渠道
   - 账号名（如 `deepseek_01`）
   - API Key（自动加密入库，DB 文件泄露 ≠ key 泄露）
   - 优先级（数字越大越优先）
   - 配额（留 0 表示无限）
   - 备注（如 "个人账号 1"）
4. 点击 **"保存"**，立即生效（无需重启）

**账号操作**:
- **启用 / 禁用**: 点击账号行的"启用"或"禁用"按钮
- **轮换 key**: 点击"轮换"，输入新 key（旧 key 立即失效，新 key 入库）
- **检查过期**: Dashboard 自动展示 30+ 天未轮换的 key，触发轮换提醒

### 2.2 CLI 方式

```bash
# 添加账号
python scripts/add_account.py --provider deepseek --name deepseek_01 --key sk-xxx --priority 1

# 查看账号列表
python scripts/list_accounts.py

# 轮换 key
python scripts/rotate_account.py --provider deepseek --name deepseek_01 --new-key sk-yyy

# 检查账号状态
python scripts/check_accounts_status.py
```

### 2.3 多账号批量导入（V4.6.1）

适用于: 同一厂商多个账号分散流量 + 失败自动切换。

```bash
# 1. 准备 JSON 文件（按渠道分）
cat > multi_account.json <<EOF
{
  "deepseek": [
    {"name": "deepseek_01", "key": "sk-ds-xxx", "priority": 1},
    {"name": "deepseek_02", "key": "sk-ds-yyy", "priority": 1},
    {"name": "deepseek_03", "key": "sk-ds-zzz", "priority": 1}
  ],
  "qwen": [
    {"name": "qwen_01", "key": "sk-qw-xxx", "priority": 1},
    {"name": "qwen_02", "key": "sk-qw-yyy", "priority": 1}
  ]
}
EOF

# 2. 导入（idempotent，重名自动跳过）
python scripts/multi_account_import.py --input multi_account.json

# 3. 验证: 路由应在多账号间 least-used 分散
python scripts/check_accounts_status.py
```

**自动行为**:
- 同一 channel 的多个账号 → 路由按 **least-used** 分散（同一时刻不重复命中同一账号）
- 某账号 5xx 失败 → 自动进入 **60s 冷却**，跳过该账号
- 冷却期后 → 自动恢复，重新参与路由

### 2.4 手动编辑脚本方式

适用于: 首次部署，需要 seed 示例数据。

```bash
# 编辑 scripts/manual_import.py，填入真实 api_key
# 或 scripts/multi_account_import.py（多账号版本）

# 执行 seed
python scripts/manual_import.py
```

---

## 3. 日常调用 AIMC

AIMC 是 **OpenAI 兼容网关**，所有 OpenAI SDK 都能直接接入。

### 3.1 OpenAI SDK（Python）

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",    # AIMC 网关地址
    api_key="your-aimc-key",                # AIMC_API_KEY（不是厂商 key）
)

# 非流式
response = client.chat.completions.create(
    model="deepseek-v4-flash",            # AIMC 自动选最便宜渠道
    messages=[{"role": "user", "content": "你好"}],
)
print(response.choices[0].message.content)

# 流式
stream = client.chat.completions.create(
    model="qwen-max",
    messages=[{"role": "user", "content": "讲个笑话"}],
    stream=True,
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

### 3.2 OpenAI SDK（Node.js）

```javascript
import OpenAI from 'openai';

const client = new OpenAI({
  baseURL: 'http://localhost:8080/v1',
  apiKey: 'your-aimc-key',
});

const response = await client.chat.completions.create({
  model: 'deepseek-v4-flash',
  messages: [{ role: 'user', content: 'Hello' }],
});
console.log(response.choices[0].message.content);
```

### 3.3 curl

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-aimc-key" \
  -d '{
    "model": "deepseek-v4-flash",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

### 3.4 模型组路由（tier:xxx / scene:xxx）

**核心概念**: 不指定具体 model，而是指定**任务级别**或**场景**。AIMC 在组内自动选最便宜的可用 model。

```python
# 按能力档调用
response = client.chat.completions.create(
    model="tier:light",       # 轻量档: 本地小模型 + 云端便宜模型
    messages=[{"role": "user", "content": "总结这段文字"}],
)

response = client.chat.completions.create(
    model="tier:balanced",    # 均衡档: DeepSeek + Qwen 中端
    messages=[{"role": "user", "content": "写一个函数"}],
)

response = client.chat.completions.create(
    model="tier:flagship",    # 旗舰档: 顶级 model
    messages=[{"role": "user", "content": "复杂推理题"}],
)

# 按场景调用
response = client.chat.completions.create(
    model="scene:code",        # 代码场景: DeepSeek-Coder + Qwen-Coder
    messages=[{"role": "user", "content": "Python 实现快排"}],
)

response = client.chat.completions.create(
    model="scene:longctx",     # 长上下文场景: 200k context window
    messages=[{"role": "user", "content": "总结这个 50k 字文档"}],
)
```

**预设模型组**（V4.5）:
- `tier:light` / `tier:balanced` / `tier:flagship`
- `scene:code` / `scene:longctx` / `scene:chitchat`

可通过 Dashboard → 模型组管理 自定义。

**响应头可观测**: AIMC 在响应头返回实际命中信息:

```http
X-AIMC-Actual-Model: deepseek-v4-flash
X-AIMC-Channel: deepseek_official
X-AIMC-Account: deepseek_01
X-AIMC-Group-Id: tier:balanced
X-AIMC-Tier: balanced
X-AIMC-Capabilities: code,reasoning,chinese
```

### 3.5 本地模型接入（Ollama / vllm）

AIMC 支持把本地跑的模型统一接入，享受跨云端/本地的智能路由。

| 服务 | 默认端口 | 默认 base_url |
|------|---------|--------------|
| Ollama | 11434 | `http://localhost:11434/v1` |
| vllm | 8000 | `http://localhost:8000/v1` |
| LM Studio | 1234 | `http://localhost:1234/v1` |
| llama.cpp | 8080 | `http://localhost:8080/v1` |

```bash
# 1. 启动本地服务（Ollama 示例）
ollama serve
ollama pull qwen2:7b

# 2. 自动探测 + 接入
python scripts/seed_local_channel.py

# 3. 验证
curl http://localhost:8080/v1/models | jq '.data[] | select(.id | contains("qwen2"))'

# 4. 在模型组中加本地 model，路由时会自动优先本地（cost=0）
```

---

## 4. Dashboard 全功能

访问 `http://localhost:8080/dashboard/`

| 页面 | 路径 | 功能 |
|------|------|------|
| **首页** | `/dashboard/` | 核心指标卡 + 趋势图 |
| **渠道列表** | `/dashboard/channels` | 所有渠道状态 / 价格 / 配额 |
| **账号管理** | `/dashboard/accounts` | 添加 / 启用 / 禁用 / 轮换 |
| **优惠情报** | `/dashboard/promotions` | 当前厂商活动汇总 |
| **用量统计** | `/dashboard/usage` | 按 model / channel / day 聚合 |
| **价格对比** | `/dashboard/prices` | 同 model 不同渠道价格对比 |
| **模型组管理** | `/dashboard/groups` | 增删 tier:xxx / scene:xxx |
| **周报** | `/dashboard/reports` | 历史周报 Markdown 渲染 |
| **告警配置** | `/dashboard/settings/alert` | Telegram webhook / 阈值 |
| **待审核** | `/dashboard/pending-review` | 情报层低置信结果人工审核（V4.4.1） |

### 4.1 常用 Dashboard 操作

- **手动刷路由缓存**: Dashboard 首页 → "刷新路由缓存" 按钮（添加账号 / 价格变动后立即生效）
- **手动跑情报源**: 优惠情报页 → "立即采集" 按钮
- **看用量**: 用量统计页 → 选时间范围 + 按 model / channel 分组

---

## 5. 监控 / 周报 / 告警

### 5.1 周报（自动）

AIMC 每周一 09:00 自动生成 Markdown 周报：

- 报告文件: `reports/weekly_YYYY-MM-DD.md`
- 最新报告: `reports/weekly-latest.md`
- Dashboard 查看: `/dashboard/reports`
- 内容: 总请求数 / 总成本 / 失败率 / 重试率 / 反馈分排行 / 趋势 / 推荐行动

```bash
# 手动生成
python scripts/generate_weekly_report.py

# systemd timer 已自动部署
systemctl list-timers aimc-weekly-report.timer
```

### 5.2 使用分析（稳定期跟踪）

**与周报互补**: 周报 = 运营向, 本工具 = 工程向（V4.7+ 优先级依据）。

```bash
# 默认 30 天
python scripts/analyze_usage.py

# 7 天 / 30 天 / 自定义起点
python scripts/analyze_usage.py --days 7
python scripts/analyze_usage.py --period-start 2026-07-01

# JSON 输出（给 Dashboard / 其他脚本消费）
python scripts/analyze_usage.py --json-only
```

**输出**:
- `reports/usage_analysis_YYYY-MM-DD.md`（人读）
- `reports/usage_analysis_latest.md`（symlink）

**报告维度**:
1. **status 分布**（V4.6.1 Cooldown 验证）
2. **多账号分散度**（Shannon entropy + 最大账号占比）
3. **channel 错误率 Top 5**
4. **model 集中度 / 4xx Top 5**
5. **token / latency / cost 画像**
6. **时段分布 24h heatmap**
7. **自动 failover 命中率**
8. **V4.7+ 决策建议**（根据上述阈值自动列出）

**调度建议**: 每周一次 `--days 7`，每月一次 `--days 30`。

### 5.3 告警（4 类）

`intelligence/v44_alerts.py` 实现：

| 告警类型 | 触发条件 | 建议动作 |
|---------|---------|---------|
| **配额 80%** | 账号配额使用 ≥ 80% | 注册新账号 |
| **配额 100%** | 账号配额耗尽 | 立即注册新账号 |
| **NoActiveAccount** | 某 provider 无 active 账号 | 检查所有账号状态 |
| **CostRising** | 本月成本 ≥ 上月 1.3 倍 | 查看周报，确认是否异常 |

**告警推送通道**:
- 默认: 写到本地日志
- 可选: Telegram Bot（配置 `.env` 后启用）
- 自定义: 通过 `intelligence/alerter.py` 接入 Bark / 飞书 / 钉钉

```bash
# 手动跑一次告警检查
python -c "
import asyncio
from intelligence.v44_alerts import check_all_alerts
print(f'Sent {asyncio.run(check_all_alerts())} alerts')
"
```

---

## 6. 路由策略说明（用户视角）

> AIMC 自动选择最优渠道，**用户无需配置**。了解原理便于排查问题。

### 6.1 多账号 least-used（V4.6.1）

同厂商多账号时，按"最近最久未用"分散流量，避免单账号过热。

**示例**: 3 个 DeepSeek 账号，第 1 次请求走 `_01`，第 2 次走 `_02`，第 3 次走 `_03`，第 4 次又回到 `_01`（least-used）。

### 6.2 失败账号 Cooldown（V4.6.1）

某账号返回 5xx 或 4xx 网络错误时，进入 60s 冷却，路由跳过该账号。

**4xx 不触发 Cooldown**（用户输入问题，换账号无意义）；**5xx 触发 1 次即冷却**（上游挂了）。

冷却期后自动恢复。

### 6.3 能力评分加权（V4.5）

同价时，能力评分高的 model 优先（`whichllm` 启发）。

**示例**: DeepSeek-Chat 与 Qwen-Max 价格相同时，AIMC 倾向能力评分高的一方。

### 6.4 多源 benchmark 加权（V4.6.3）

能力评分融合 5 个数据源（LMSYS / OpenCompass / LiveBench / SuperCLUE / AA），按 **reliability × recency** 加权。

### 6.5 时段定价

情报层采集到厂商峰谷定价后，路由在谷时优先便宜 channel。

---

## 7. 运维操作

### 7.1 备份与恢复

```bash
# 手动备份（停止服务后）
sudo systemctl stop aimc
cp data/aimc.db backup/aimc_$(date +%Y%m%d_%H%M%S).db
sudo systemctl start aimc

# 自动备份（cron，每天 03:00）
0 3 * * * /path/to/aimc/scripts/deploy/backup.sh

# 恢复
sudo systemctl stop aimc
cp backup/aimc_20260808.db data/aimc.db
sudo systemctl start aimc
```

`scripts/deploy/backup.sh` 已实现: 备份 + 压缩 + 保留 30 天。

### 7.2 重启服务

```bash
# systemd
sudo systemctl restart aimc
sudo systemctl status aimc

# 直接启动（开发）
# Ctrl+C 停止
uvicorn main:app --host 0.0.0.0 --port 8080
```

### 7.3 更新 AIMC

```bash
# 1. 拉取最新代码
cd /path/to/aimc
git pull origin master

# 2. 更新依赖（如有新增）
pip install -r requirements.txt

# 3. 数据库迁移（如有）
python db/init_db.py     # 自动检测 + 执行迁移

# 4. 重启服务
sudo systemctl restart aimc

# 5. 冒烟测试
python scripts/deploy/smoke_v43.py
```

### 7.4 升级账号加密（API-Sec-1）

如果你从早期版本（明文 key）升级，DB 中的 key 仍是明文。需要运行迁移脚本：

```bash
# 1. 生成 master key（如 .env 没有 AIMC_MASTER_KEY）
python scripts/gen_master_key.py
# 把输出的 key 写到 .env 的 AIMC_MASTER_KEY

# 2. 执行迁移（明文 → 加密）
python scripts/migrate_encrypt_keys.py

# 3. 验证 Dashboard 上能看到 key 已被遮蔽（显示为 sk-xxx****）
```

---

## 8. FAQ 常见问题

### Q1: 添加账号后没生效？

A: 手动刷路由缓存（Dashboard 首页按钮）或等 5 分钟自动刷新。

### Q2: 某个 model 一直路由失败？

A:
1. Dashboard → 用量统计 → 看 status code 分布
2. 如果 4xx: 检查 model 名 / 参数是否被目标渠道支持
3. 如果 5xx: 渠道上游挂了，等恢复或切渠道
4. 跑 `python scripts/analyze_usage.py` 看 channel 错误率 Top 5

### Q3: 周报没生成？

A:
```bash
# 检查 systemd timer
systemctl list-timers aimc-weekly-report.timer
sudo systemctl status aimc-weekly-report.timer

# 手动触发
python scripts/generate_weekly_report.py
```

### Q4: AIMC 启动报错 "api_key 明文" 警告？

A: 你没启用 API-Sec-1 加密。运行 §7.4 升级流程。

### Q5: 怎么知道 AIMC 路由到哪个渠道？

A: 看响应头（curl 加 `-i`）:
```bash
curl -i http://localhost:8080/v1/chat/completions ... | grep X-AIMC
```
或在 Python SDK:
```python
response = client.chat.completions.create(...)
print(response._raw_response.headers)
```

### Q6: 配额告警一直触发？

A:
- 如果是真实配额耗尽: 注册新账号（Dashboard → 添加账号）
- 如果是误报: Dashboard → 告警配置 → 调阈值

### Q7: 多个客户端怎么区分用量？

A: 目前所有客户端共用 `usage_logs` 表。如需分开计费，等待 V4.7+ Virtual Key 功能（由 `analyze_usage` 触发）。

### Q8: AIMC 支持哪些模型？

A: 任何 OpenAI 兼容 API 都可以作为 channel（DeepSeek / Qwen / GLM / Kimi / 豆包 / 智谱 / SiliconFlow / 本地 Ollama 等）。Dashboard → 渠道管理 → 添加渠道 即可。

### Q9: 怎么调整路由策略？

A: 路由策略是**自动的**，用户无需配置。如需调整:
- 账号优先级: Dashboard 改 priority 数字（越大越优先）
- 模型组成员: Dashboard 模型组管理
- 不建议修改路由算法代码（已稳定）

### Q10: 数据存在哪里？隐私如何？

A:
- DB 文件: `./data/aimc.db`（SQLite WAL）
- API Key 加密存储（API-Sec-1，DB 文件泄露 ≠ key 泄露）
- `.env` 不入 git（master key 在 .env）
- 不上传任何数据到外部服务（除情报源爬取）

---

## 反馈与贡献

- **Bug 报告 / 功能建议**: 在 Gitee Issues 提单
- **代码贡献**: Fork → 新建分支 → PR 到 master
- **稳定期跟踪**: 每周跑 `analyze_usage.py --days 7`，看 V4.7+ 决策建议
- **设计原则**: 自用优先 / 情报层优先 / 先跑通再优化 / 保持简单 / 精细化运营

详见 [README.md](../README.md) 与 [development-plan.md](development-plan.md)。
