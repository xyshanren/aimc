# 本地开发环境搭建

> 受众：第一次改 AIMC 代码的开发者
> 前提：会 Python + Git + 命令行

## 1. 克隆 + 虚拟环境

```bash
git clone git@gitee.com:XiaoYRecluse/aimc.git
cd aimc
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements.txt pytest pytest-asyncio  # 开发用
```

## 2. 配置 .env

```bash
cp .env.example .env
nano .env
# 至少填一个渠道 API Key (如 SILICONFLOW_API_KEY=sk-xxx)
# 其他默认
```

## 3. 启动

```bash
# 一次性录入账号 (也可 Dashboard 加)
python scripts/manual_import.py

# 启动网关 (前台, 方便看 log)
python main.py
```

启动后监听 `http://0.0.0.0:8080`。

## 4. 跑测试

```bash
# 全跑 (502 个 test)
python -m pytest tests/ -v

# 单文件
python -m pytest tests/test_cache_inject.py -v

# 指定 test
python -m pytest tests/test_quota_check.py::test_balance_cny_unsync_passes -v
```

详见 [testing.md](testing.md)。

## 5. 跑 lint（可选）

AIMC 当前没强制 lint，但建议本地装 `ruff`：

```bash
pip install ruff
ruff check gateway/ intelligence/ scripts/
ruff format gateway/ intelligence/ scripts/
```

CI 配置（待 P2-16）：未来在 `.github/workflows/test.yml` 自动跑 pytest。

## 6. 项目结构（开发视角）

```
aimc/
├── gateway/                # 网关核心
│   ├── app.py              # FastAPI app 工厂 + lifespan
│   ├── routes.py           # /v1/* + /dashboard/* 路由
│   ├── router.py           # 路由决策 (score-based + tier:xxx)
│   ├── proxy.py            # 上游转发 + body.model 改写 + cache 注入
│   ├── cooldown.py         # CooldownManager (V4.6.1)
│   ├── limiter.py          # RateLimiter (V4.6.1)
│   ├── quota.py            # P0-6 多维限额检查
│   ├── fallback.py         # fallback 链
│   └── middleware.py       # 请求日志 + 鉴权 + P1-7 dashboard token
├── intelligence/           # 情报采集层
│   ├── scheduler.py        # APScheduler (V4.8.3: provider×interval 分组 ~36 jobs + listener)
│   ├── sources/            # 厂商采集源
│   │   ├── deepseek.py / glm.py / qwen.py / minimax.py / ...
│   ├── pricing_sync.py     # 价格拉取
│   ├── model_sync.py       # model registry 同步
│   ├── alerter.py          # Bark / 飞书 / Server 酱
│   ├── cross_source_validator.py
│   ├── analytics/          # 方案 A
│   │   └── suggest_changes.py  # 4 model + 4 group 规则 + 置信度
│   ├── fetcher.py          # Playwright 抓取
│   ├── parser.py           # AI 解析优惠信息
│   ├── differ.py           # Diff 检测
│   └── healer.py           # 自愈循环
├── db/                     # 数据库层
│   ├── engine.py           # async + sync_engine (P0-8)
│   ├── models.py           # ORM (含 P0-1~P0-6 列)
│   ├── crud.py             # CRUD helpers
│   ├── init_db.py          # create_all + ensure_migrations
│   └── migrations/         # 12 个迁移脚本
├── dashboard/              # Dashboard (后端 routes + 前端 htmx)
│   ├── routes.py           # 30+ endpoints
│   └── static/             # index.html / app.js / login.html
├── schemas/                # Pydantic 模型 (API 序列化)
├── security/               # API-Sec-1 XOR 加密 (历史, P0-5 改 Fernet)
├── scripts/                # 运维 + seed + sync 脚本
├── tests/                  # 547 tests (pytest)
├── docs/                   # 文档
├── main.py                 # 入口
├── config.py               # Pydantic settings
└── pyproject.toml          # 项目元数据
```

## 7. 常用开发命令

```bash
# 改 router.py 后看效果
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AIMC_API_KEY" \
  -d '{"model":"tier:flagship","messages":[{"role":"user","content":"hi"}],"max_tokens":3}' \
  -i | head -20

# 触发 router cache 立即刷新 (不等 TTL)
curl -X POST http://localhost:8080/dashboard/api/cache/refresh

# 看 router 日志 (DEBUG 级)
AIMC_LOG_LEVEL=DEBUG python main.py

# 改 migration 后测
python db/migrations/<new_migration>.py --dry-run
python db/migrations/<new_migration>.py --apply
```

## 8. 开发工作流

```
改代码 -> pytest -> commit -> push
```

详见 [conventions.md](conventions.md)（commit 规范 / 分支策略 / 测试要求）+ [contributing.md](contributing.md)（PR 流程）。
