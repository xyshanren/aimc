# 架构设计

> 受众：想理解 AIMC 数据流和模块边界的开发者
> 前提：读过 [setup.md](setup.md)（本地跑起来）

## 1. 系统分层

```
用户 (OpenAI SDK / curl / Anthropic SDK*)
        │
        ▼
┌────────────────────────────────────────────────────────────┐
│                  AIMC 网关层 (FastAPI)                    │
│  �─────────────────────────────────────────────────────┐ │
│  │  /v1/chat/completions                               │ │
│  │  - Auth (AIMC_API_KEY)                              │ │
│  │  - 路由决策 (router.py: select_channel)            │ │
│  │  - 限额检查 (quota.py: check_account_quota)         │ │
│  │  - 上游转发 (proxy.py: forward_request)            │ │
│  │    - body.model 改写 (V4.6.4)                       │ │
│  │    - prompt cache 注入 (P1-8)                       │ │
│  │  - 失败重试 (P1-15: 429/5xx failover)              │ │
│  │  - 用量日志 (usage_logs + cached_tokens 解析)       │ │
│  └─────────────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  /dashboard/*                                       │ │
│  │  - Auth (AIMC_DASHBOARD_TOKEN, P1-7)               │ │
│  │  - 30+ 端点 (channels/accounts/quotas/suggestions)  │ │
│  └─────────────────────────────────────────────────────┘ │
└────────────────┬───────────────────────────────────────────┘
                 │
                 ▼
┌────────────────────────────────────────────────────────────┐
│           情报采集层 (intelligence/, APScheduler)         │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  厂商采集 (sources/*.py) - Playwright + AI 解析     │ │
│  │  价格同步 (pricing_sync.py + sync_siliconflow_*)    │ │
│  │  建议生成 (analytics/suggest_changes.py)            │ │
│  │  报警 (alerter.py - Bark/飞书/Server酱)            │ │
│  └─────────────────────────────────────────────────────┘ │
│           20 个 APScheduler job (阶段 2)                │
│           + listener 写 job_health (P0-8)               │
└────────────────┬───────────────────────────────────────────┘
                 │
                 ▼
�────────────────────────────────────────────────────────────┐
│                SQLite (WAL 模式, 单文件)                │
│  channels / accounts / model_registry / model_groups    │
│  model_group_members (FK + UNIQUE, P0-1)              │
│  price_snapshots (raw_model_id + cached_price, P0-2)  │
│  account_quotas / account_quota_usage (P0-6)          │
│  model_change_suggestions / group_change_suggestions   │
│  job_health / settings_runtime                         │
│  usage_logs (含 cached_input_tokens 等, P0-3-followup) │
└────────────────────────────────────────────────────────────�
```

## 2. 关键数据流：一次 chat completion 请求

```
1. POST /v1/chat/completions
   ↓
2. middleware: Auth (Authorization: Bearer <AIMC_API_KEY>)
   ↓
3. routes.chat_completions:
   a. parse body_json
   b. await route_engine._auto_refresh_if_stale()    [P0-4: TTL + mtime]
   c. P0-6: check_account_quota (rpm/tpm/5h/concurrent/balance)
   d. channel = route_engine.select_channel(model, messages=...)  [P0-3: skip multimodal]
      → _select_by_group 或 select_best_channel (score-based)
   e. P1-15: retry loop on 429/5xx
   f. forward_request(channel, body, headers, requested_model, actual_model)
      → proxy._inject_prompt_cache (P1-8: cache_control / prompt_cache_key)
      → proxy._maybe_rewrite_body_model (V4.6.4: body.model = channel._actual_model)
      → httpx post to upstream
      → return JSONResponse / StreamingResponse
   g. _schedule_usage_log (async fire-and-forget)
      → parse cached_tokens (P0-3-followup)
      → write UsageLog row (含 cached_input_tokens)
4. response with X-AIMC-* headers
```

## 3. 模块边界

### `gateway/router.py` — 路由决策（最复杂）

```
RouteEngine
├── _load_channels_cache()       从 db 读 channel + accounts + prices
├── _load_groups_cache()         从 db 读 model_groups + members
├── _load_cards_cache()          从 db 读 model_cards (whichllm 启发)
├── _load_score_weights()         从 settings_runtime 读 score 权重 [P0-7]
├── select_channel(model, prefs, messages)
│   ├── tier:/scene: 前缀 -> _select_by_group()
│   └── 精确路由 -> select_best_channel()
├── select_best_channel()         score-based 5 维加权 [阶段3]
│   └── _is_channel_usable()      过滤 cooldown / 限额 / 限速
├── _select_by_group()            tier:xxx 组路由
│   ├── 组过滤: tier / capabilities / min_context / require_vision
│   ├── _has_multimodal_content() 跳过 multimodal [P0-3]
│   ├── score_account()           5 维加权排序
│   └── 失败 -> fallback_group
└── refresh_cache()               全量重 load（60s TTL + mtime）
```

### `gateway/proxy.py` — 上游转发

```
forward_request(channel, body, headers, requested_model, actual_model)
├── _maybe_rewrite_body_model()  body.model = actual_model [V4.6.4]
├── _inject_prompt_cache()        cache_control / prompt_cache_key [P1-8]
├── _forward_stream()             流式
└── _forward_normal()             非流式
    ├── httpx post
    ├── response.status_code == 429 -> cooldown_manager.mark_failure [P1-15]
    ├── 5xx / 401 / 403 -> _schedule_alert
    ├── _build_aimc_headers()      X-AIMC-* response headers
    └── _schedule_usage_log()      async fire-and-forget
```

### `gateway/quota.py` — 多维限额检查（P0-6）

```
check_account_quota(account_id, estimated_tokens)
└── _check_dimension()
    ├── balance_cny: window_start IS NULL = 未 sync -> 跳过 [P1-15]
    ├── rpm / tpm: 当前分钟内已用请求/token
    ├── daily_tokens / weekly_tokens / 5h_tokens: 窗口累计
    └── concurrent: in-flight count
```

### `intelligence/scheduler.py` — 定时任务（阶段 2）

```
20 个 APScheduler job:
├── batch_crawl_<provider> (15 个): 合并 announcement + model_list + price_aggregator
├── refresh_route_cache (1): 60s TTL 刷新路由缓存
├── check_key_rotation (1): 每天检查 key 轮换
├── cross_source_detection (1): 每天跨源交叉验证
├── monthly_official_audit (1): 每月 1 号官方源审查
└── weekly_suggest_changes (1): 周一 09:00 跑 suggest_changes [方案A]

每个 job 完成后:
├── EVENT_JOB_EXECUTED / EVENT_JOB_ERROR 事件
├── _record_job_event() listener 写 job_health [P0-8]
└── 失败 -> cooldown_manager.mark_failure (account 进入冷却)
```

## 4. db 模型关系

```
channels ─┬─ accounts (1:N, FK)
           │    └─ account_quotas (P0-6)
           │    └─ account_quota_usage
           └─ price_snapshots (1:N)
                 ├─ input_price + output_price [P0-2: 53 行真实价]
                 └─ cached_input_price + cache_write_price

model_groups ─┬─ tier:xxx (C2 6 个)
               │    ├─ flagship / strong / balanced / light / vision / code
               └─ model_group_members (P0-1: FK + UNIQUE)
                    ├─ group_id (FK -> model_groups.id)
                    └─ model_id (FK -> model_registry.model_id)

model_registry
├─ model_id (PK)
├─ tier (T0/T1/T2/T3/T4)
├─ modality (text/multimodal)
└─ supports_prompt_cache (P0-2)

usage_logs
├─ input_tokens / output_tokens
└─ cached_input_tokens / cache_read_input_tokens / cache_creation_input_tokens [P0-3-followup]

model_change_suggestions / group_change_suggestions [方案A]
├─ status (pending / approved / rejected / applied)
├─ confidence (0-1)
└─ based_on (JSON)

job_health [P0-8]
├─ job_id (PK)
├─ last_run_at / last_success_at / last_error_at
└─ total_runs / total_successes / total_failures

settings_runtime
└─ router_score_weights (JSON) [P0-7]
```

## 5. 配置系统 (`config.py`)

```python
class Settings(BaseSettings):
    # 网关
    host: str = "0.0.0.0"
    port: int = 8080
    api_key: str = ""
    dashboard_token: str = ""  # [P1-7]

    # db
    db_path: str = "./data/aimc.db"

    # 厂商
    siliconflow_api_key: str = ""
    deepseek_api_key: str = ""
    # ... 7 个渠道

    # 路由 / 缓存 [P0-4]
    route_refresh_interval: int = 300
    # AIMC_ROUTER_CACHE_TTL env

    # 情报
    crawl_enabled: bool = True
    price_alert_threshold: float = 30.0

    # AI 解析
    openai_api_key: str = ""
    parse_model: str = "gpt-4o-mini"

    # 告警 [V4.4]
    bark_key: str = ""
    feishu_webhook_url: str = ""
```

## 6. 异步 / 并发模型

```
FastAPI + uvicorn (async event loop)
  ↓
APScheduler AsyncIO 兼容 (job_defaults: coalesce=True, max_instances=1)
  ↓
SQLAlchemy 2.0 async (asyncio + aiosqlite)
  ↓
httpx.AsyncClient (上游调用)
```

**同步 vs 异步边界**：
- `gateway/proxy.py` `_schedule_usage_log` / `_schedule_alert`: async fire-and-forget（不阻塞响应）
- `gateway/middleware.py`: sync middleware（不破坏 async flow）
- `intelligence/scheduler.py` `_record_job_event` listener: **用独立 sync_engine**（P0-8，避免 greenlet 冲突）

## 7. 设计哲学

1. **自用优先** — 允许半自动，不追求 100% 无人值守
2. **情报层 > 网关层** — 情报发现是核心差异化价值
3. **先跑通再优化** — 快速验证核心价值
4. **保持简单** — 单进程部署，零外部依赖
5. **精细化运营** — 峰谷定价、频限追踪、多账号兜底

## 8. 历史架构决策记录

| 时间 | 决策 | 理由 |
|------|------|------|
| V2.1 | 单进程 + SQLite | 个人项目零运维 |
| V2.2 | APScheduler 同进程 | 避免外部依赖（独立 worker） |
| V2.3 | Playwright async | JS 渲染支持（厂商页面多数是 SPA） |
| V2.4 | tier:/scene: 组路由 | 智能 vs 静态调用的中间方案 |
| V3 | model_cards 加权 | 价格平手时高分 model 优先（不只看价格） |
| V4.6.1 | least-used + Cooldown | 多账号分散频限 + 失败账号冷却 |
| V4.6.4 | body.model 改写 | router fallback 选中的 model 替换给上游 |
| P0-1 | FK + UNIQUE | 修 int-string 漂移 |
| P0-3 | multimodal skip | 纯文本请求不选 multimodal model |
| P0-4 | TTL + mtime | 修 "db 改必 restart" |
| P0-7 | score weights dashboard | 价格 vs 高可用 trade-off 快速迭代 |
| P1-15 | 429 failover | 上游 plan 耗尽自动换 channel |
