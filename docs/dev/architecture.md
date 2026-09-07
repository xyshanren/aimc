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
│  ┌─────────────────────────────────────────────────────┐ │
│  │  /v1/chat/completions                               │ │
│  │  - Auth (V4.8: api_keys 表 aimc-* → caller_id,       │ │
│  │          fallback AIMC_API_KEY legacy)               │ │
│  │  - 用户限额 (quota.py: check_user_quota, V4.8)       │ │
│  │  - 路由决策 (router.py: select_channel)            │ │
│  │  - 账号限额检查 (quota.py: check_account_quota)     │ │
│  │  - 上游转发 (proxy.py: forward_request)            │ │
│  │    - body.model 改写 (V4.6.4)                       │ │
│  │    - prompt cache 注入 (P1-8 + P3 路径 A 优化)       │ │
│  │  - 失败重试 (P1-15: 429/5xx failover)              │ │
│  │  - 用量日志 (usage_logs + caller_id + cache tokens) │ │
│  └─────────────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  /dashboard/*                                       │ │
│  │  - Auth (AIMC_DASHBOARD_TOKEN, P1-7)               │ │
│  │  - 30+ 端点 (channels/accounts/per-user/keys/...)    │ │
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
│           ~41 个 APScheduler job (provider×间隔分组 + 探活/清理)│
│           + listener 写 job_health (P0-8)               │
└────────────────┬───────────────────────────────────────────┘
                 │
                 ▼
┌────────────────────────────────────────────────────────────┐
│                SQLite (WAL 模式, 单文件)                │
│  channels / accounts / model_registry / model_groups    │
│  model_group_members (FK + UNIQUE, P0-1)              │
│  price_snapshots (raw_model_id + cached_price, P0-2)  │
│  account_quotas / account_quota_usage (P0-6)          │
│  users / api_keys (V4.8 多用户, SHA256 key hash)       │
│  model_change_suggestions / group_change_suggestions   │
│  job_health / settings_runtime                         │
│  usage_logs (caller_id + cache tokens + 本地时间戳)    │
└────────────────────────────────────────────────────────────┘
```

注：周报生成/推送已由独立 systemd timer（`aimc-weekly-report.timer`，周一 09:00）
承担，不在主进程 job 列表内。

## 2. 关键数据流：一次 chat completion 请求

```
1. POST /v1/chat/completions
   ↓
2. middleware Auth (V4.8):
   a. aimc- 前缀 key → api_keys 表 SHA256 查表 → caller_id（30s 缓存）
   b. 其他 → legacy AIMC_API_KEY 全局匹配（caller_id = NULL）
   ↓
3. routes.chat_completions:
   a. parse body_json
   b. await route_engine._auto_refresh_if_stale()    [P0-4: TTL + mtime]
   c. V4.8: check_user_quota (caller 的日/月 token 限额, 超额 429)
   d. P0-6: check_account_quota (rpm/tpm/5h/concurrent/balance)
   e. channel = route_engine.select_channel(model, messages=...)
      → tier:xxx 或 tier:xxx/model 前缀 -> _select_by_group (斜杠后缀 V4.6.5)
        [P0-3 V6.0 收窄: 仅 require_vision=1 组内纯文本跳过 multimodal]
      → 精确路由 -> select_best_channel (score-based, 无 multimodal skip)
   f. P1-15: retry loop on 429/5xx
   g. forward_request(channel, body, headers, requested_model, actual_model)
      → proxy._inject_prompt_cache (P1-8 + P3: 稳定前缀分段 hash)
      → proxy._maybe_rewrite_body_model (V4.6.4: body.model = channel._actual_model)
      → httpx post to upstream
      → return JSONResponse / StreamingResponse
   h. _schedule_usage_log (async fire-and-forget)
      → parse cache tokens (OpenAI cached_tokens / Anthropic cache_read+creation)
      → write UsageLog row (含 caller_id, group_id 归一化, 本地时间戳)
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
│   ├── _has_multimodal_content() 跳过 multimodal [P0-3, V6.0 收窄: 仅 require_vision=1 组]
│   ├── score_account()           5 维加权排序
│   └── 失败 -> fallback_group
└── refresh_cache()               全量重 load（60s TTL + mtime）
```

### `gateway/proxy.py` — 上游转发

```
forward_request(channel, body, headers, requested_model, actual_model)
├── _maybe_rewrite_body_model()  body.model = actual_model [V4.6.4]
├── _inject_prompt_cache()        [P1-8 + P3 路径 A (2026-08-21)]
│   ├── OpenAI 风格: prompt_cache_key = 稳定前缀分段 hash
│   │   (全量 system + 首条非 system, 各截断 4096 字符;
│   │    caller 已传 key 时不覆盖; 无 system fallback 首条 user)
│   └── Anthropic 风格: system + 第一条 user 加 cache_control (role 校验)
├── _forward_stream()             流式 (aiter_raw 原样透传)
└── _forward_normal()             非流式
    ├── httpx post
    ├── response.status_code == 429 -> cooldown_manager.mark_failure [P1-15]
    ├── 5xx / 401 / 403 -> _schedule_alert
    ├── _build_aimc_headers()      X-AIMC-* response headers
    └── _schedule_usage_log()      async fire-and-forget
```

### `gateway/quota.py` — 多维限额检查

```
check_user_quota(caller_id, estimated_tokens) [V4.8]
└── _sum_user_tokens() 按 usage_logs.caller_id 聚合本地日/月窗口
    └── 超限 → 429 daily/monthly_token_limit_exceeded

check_account_quota(account_id, estimated_tokens) [P0-6]
└── _check_dimension()
    ├── balance_cny: window_start IS NULL = 未 sync -> 跳过 [P1-15]
    ├── rpm / tpm: 当前分钟内已用请求/token
    ├── daily_tokens / weekly_tokens / 5h_tokens: 窗口累计
    └── concurrent: in-flight count
```

### `intelligence/scheduler.py` — 定时任务（阶段 2）

```
41 个 APScheduler job (30 个 batch_crawl + 11 个固定 job):
├── batch_crawl_<provider>_<interval> (30 个): 按 provider × poll_interval 分组
│   (V4.8.3 修复旧 min(interval) 批量导致 model_list 源被拉高频;
│    默认间隔: web_page 30min / announcement 1h / price_aggregator 12h /
│    model_list 1d / benchmark 1w; 连续无变化自适应降频 4x/8x)
├── refresh_route_cache (1): 刷新路由缓存 (间隔 ROUTE_REFRESH_INTERVAL, TTL 60s)
├── check_key_rotation (1): 每天检查 key 轮换
├── cross_source_detection (1): 每天跨源交叉验证
├── monthly_official_audit (1): 每月 1 号官方源审查
├── weekly_suggest_changes (1): 周一 09:00 跑 suggest_changes [方案A]
├── v44_alerts_check (1): 每 6 小时跑 V4.4 告警 (配额/账号/成本)
├── channel_health_probe (1): 每 60s 智能探活 [V4.9]
├── cooldown_cleanup (1): 每 5min 清理过期 cooldown entry [V4.9]
├── account_health_log_cleanup (1): 每天凌晨 3 点删 N 天前探活日志 [V4.9.1]
├── promotion_cleanup (1): 每天 04:20 优惠情报过期清理 (expired 保留 30 天) [2026-09-05]
└── transcript_log_cleanup (1): 每 6 小时清理超期请求转录 [V4.9.3]

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
├─ modality (text/vision/multimodal)
├─ supports_prompt_cache (P0-2)
└─ list_input_price / list_output_price (V6.0, 官方标价 元/百万tokens; NULL=未知, 0.0=官方免费; 成本审计用, 不参与路由)

usage_logs
├─ caller_id (V4.8, FK -> users.id, 走全局 key 时 NULL)
├─ input_tokens / output_tokens
├─ cached_input_tokens / cache_read_input_tokens / cache_creation_input_tokens [P0-3-followup]
├─ cost_estimate (实付估算, 元; 免费/订阅渠道 = 0)
├─ list_cost_estimate (V6.0 标价估算, 元; NULL=标价未知; 节省 = 标价 - 实付, 仅标价已知行)
└─ created_at (本地时间, 2026-08-21 起; 存量 UTC 已迁移)

users / api_keys (V4.8 多用户)
├─ users: username / role / status / daily_token_limit / monthly_token_limit
└─ api_keys: key_hash (SHA256) / key_prefix / is_admin / status / user_id FK

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
5. **精细化运营** — 峰谷定价（V4.8.7 已落地：工作日/周末分时段匹配 + DeepSeek 峰谷种子）、频限追踪、多账号兜底
6. **自动化收口以数据为前提** — 「自用优先，允许半自动」不等于永远半自动：扩大自动化（如 T1-T3 自动入组）前先埋点观察真实代价（错分面/审批负担），数据说话后再定策略（2026-09-05 规划，见 dev/automation-closure-plan.md）

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
| P0-3 | multimodal skip | 纯文本请求不选 multimodal model（**V6.0 收窄**：仅 `require_vision=1` 组内生效，多分组 multimodal 模型如 glm-5.3-flash 的文本角色不再被误杀）|
| P0-4 | TTL + mtime | 修 "db 改必 restart" |
| P0-7 | score weights dashboard | 价格 vs 高可用 trade-off 快速迭代 |
| P1-15 | 429 failover | 上游 plan 耗尽自动换 channel |
| V5.1 | 成员优先级激活 | sort key = (price, family_bonus, member_priority, -channel.priority) |
| V6.0 | vision 分级 + P0-3 收窄 | tier:vision（旗舰）→ tier:vision-light（免费兜底）→ tier:light fallback；GLM 免费模型入组 |
| V6.0 | 标价双轨成本审计 | registry 官方标价 + usage_logs.list_cost_estimate；免费/订阅渠道实付 0 也可量化节省（节省仅统计标价已知行）|
| V6.1 | 鉴权统一 + agent 契约 | dashboard API 接受 admin api_key（aimc- 前缀 + is_admin=1）；统一 CLI `scripts/aimc.py`（薄 REST 客户端）；OpenAPI/agent-api.md |
| V6.x | qwen 命名规范化 | validate_model_id 放宽（接受 `{family}<版本数字>`），规范名去双前缀（qwen3-8b / qwen3-vl-*），sync 不再产生 qwen-qwen*，存量迁移 39 改名 + 4 重复合并 |
| 2026-09-05 (规划) | AUTO_JOIN_NON_T0 数据驱动决策 | 不立即开 T1-T3 自动入组：先埋点积累 ≥2 周 pending 增速/tier 分布/审批负担/组命中率，再按 S1-S4 择一（见 dev/automation-closure-plan.md）；错分风险来自 infer_tier 参数量正则盲区（无参数量 → None → pending_review） |
| V6.3 | F3 埋点纯只读上产 | GET /dashboard/api/ops/auto-join-metrics 从现有表推导 5 组指标（pending 周桶/tier 分布/审批负担/组路由命中/infer_tier 盲区），不改任何入组行为；审批负担因 approve/reject 行级同值报合并处理量 |
| V6.3 | F1 首启检测进 gateway startup | 空渠道/无 active 账号时 WARN 引导而非阻断启动（个人项目哲学：服务照起，引导到位）；manual_import.py 从必做降级为可选捷径 |
| V6.3.1 | 顶栏 badge 不为自愈错误报警 | pending-summary 增 `exclude_transient`（badge 传 true）：瞬时冷却（cooldown_degraded，15-60s 自动恢复）不计入 badge，消除「顶栏有数但运维页为空」的观察时差噪音；同时观察点落 `cooldown_events` 流水（同 episode 按 cooldown_until 去重，badge 30s + ops 60s 双观察者不重复），运维页 24h 瞬时冷却记录卡留痕可回看 |
