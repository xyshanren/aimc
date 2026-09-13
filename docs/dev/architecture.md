# 架构设计

> 受众：想理解 AIMC 数据流和模块边界的开发者
> 前提：读过 [setup.md](setup.md)（本地跑起来）
> 配套：模块间结合问题的逐条清单见 [module-integration-audit.md](module-integration-audit.md)；
> 下一阶段规划调研见 [next-stage-research.md](next-stage-research.md)

> **文档时点**：2026-09-13（对应 V6.4.1 上产 + 看板口径统一）。文中的文件行号会随代码演进漂移，
> 引用时以函数名/符号名为主键，行号仅作定位辅助。

## 1. 系统分层

四个顶层包，单向依赖为主、靠函数内延迟 import 打破个别环：

```
用户 (OpenAI SDK / curl / Anthropic SDK* / rag_demo / harness)
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│  接入层  gateway/ (FastAPI, 主进程)                          │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ app.py        应用工厂: 挂载路由/中间件 + lifespan       │ │
│  │ middleware.py 请求日志 + 多 key 鉴权 + dashboard token   │ │
│  │ routes.py     /v1/chat/completions 重试主循环 + /v1/models│ │
│  │ embeddings.py /v1/embeddings + /v1/rerank 直通           │ │
│  │ proxy.py      上游透传 (body 改写/cache 注入/drop_params/ │ │
│  │               用量落库/cooldown/告警)                    │ │
│  │ router.py     路由决策引擎 RouteEngine (单例)            │ │
│  │ quota.py      账号多维限额 + 用户日/月 token 限额         │ │
│  │ limiter.py    内存滑动窗口频限 (QPM/TPM/并发/日)          │ │
│  │ cooldown.py   失败账号冷却状态机 (healthy→failing→…)     │ │
│  │ error_classifier.py  上游错误 → 9 类 ErrorCategory        │ │
│  │ fallback.py   旧故障转移实现 (已死, 见结合诊断)           │ │
│  └────────────────────────────────────────────────────────┘ │
├──────────────────────────────────────────────────────────────┤
│  看板层  dashboard/ (同进程, 挂 /dashboard + /static)         │
│  routes.py    67 个端点 (channels/accounts/keys/usage/cost/  │
│               intel/sources/suggestions/cooldown/ops/settings)│
│  static/      index.html + app.js + style.css (零框架, 6 页)  │
├──────────────────────────────────────────────────────────────┤
│  情报层  intelligence/ (同进程, APScheduler)                  │
│  scheduler.py 42 个 job (30 batch_crawl + 12 固定) + job_health│
│  sources/     37 个采集源 (5 类型) + fetcher (Playwright/httpx)│
│  differ/healer/parser/extractor/content_guard  采集→变化→修复 │
│  model_sync/pricing_sync/pricing_writer/benchmark_aggregator  │
│  cross_source_validator/price_alert/promotion_cleanup/alerter │
│  channel_health.py  智能探活 + 冷却清理 (V4.9)                │
│  analytics/suggest_changes.py  周建议生成                     │
├──────────────────────────────────────────────────────────────┤
│  数据层  db/ + schemas/                                      │
│  models.py  27 个 ORM 表 | crud.py 58 函数 | engine.py        │
│  init_db.py create_all + ensure_migrations (幂等 ALTER)       │
│  migrations/ 23 个手动脚本 (--apply, 无版本表)                │
│  schemas/intel.py 情报数据契约 (channel/pricing/routing 遗留)  │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
   SQLite (WAL, 单文件)  data/aimc.db
```

注：周报生成/推送由独立 systemd timer（`aimc-weekly-report.timer`，周一 09:00）承担，
不在主进程 job 列表内。

## 2. 关键数据流：一次 chat completion 请求

```
1. POST /v1/chat/completions
   ↓
2. middleware.py RequestLoggingMiddleware.dispatch
   a. aimc- 前缀 key → api_keys 表 SHA256 查表 → caller_id/key_id (30s 缓存)
   b. 其他 → legacy AIMC_API_KEY 全局匹配 (caller_id = NULL)
   ↓
3. routes.chat_completions
   a. 解析 body / model / extra_body.aimc_preferences
   b. route_engine._auto_refresh_if_stale()   [P0-4: TTL + mtime]
   c. 重试循环:
      - route_engine.select_channel(model, prefs, messages)
          → tier:/scene: 前缀 → _select_by_group() (斜杠后缀 V4.6.5)
              [P0-3 V6.0 收窄: 仅 require_vision=1 组内纯文本跳过 multimodal]
          → 精确路由 → select_best_channel() (score-based, 无 multimodal skip)
          → 候选过滤 _is_channel_usable(): cooldown.is_in_cooldown + limiter.is_limited
      - check_account_quota() (quota.py; rpm/tpm/5h/并发/余额)
      - check_user_quota()   (caller 日/月 token 限额)
      - forward_request(channel, body, headers, requested_model, actual_model)
          → _strip_thinking_fields / _inject_thinking_off (Phase B 渠道开关)
          → _drop_params / _auto_learn_drop_params (V4.8.7)
          → _maybe_rewrite_body_model (body.model = actual_model)
          → _inject_prompt_cache (P1-8 + P3 稳定前缀分段 hash)
          → httpx post → JSONResponse / StreamingResponse
          → _update_cooldown: mark_success/mark_failure + error_classifier.classify
          → _schedule_usage_log / _schedule_stream_usage_log (fire-and-forget)
              → parse cache tokens + _estimate_cost + _estimate_list_cost
              → write UsageLog (caller_id/key_id/group_id/cost/本地时间戳)
      - 429/5xx/错误分类 → continue 下一候选; 汇总 X-AIMC-Failed-Channels
   ↓
4. response + X-AIMC-* headers
```

## 3. 模块边界

### `gateway/router.py` — 路由决策（最复杂）

```
RouteEngine (单例 route_engine)
├── refresh_cache()               全量重 load (TTL 由 route_refresh_interval 驱动)
├── _load_channels_cache()        从 db 读 channel + accounts + prices
├── _load_groups_cache()          从 db 读 model_groups + members
├── _load_cards_cache()           从 db 读 model_cards (whichllm 启发)
├── _load_score_weights()         从 settings_runtime 读 score 权重 [P0-7]
├── select_channel(model, prefs, messages)
│   ├── tier:/scene: 前缀 -> _select_by_group()
│   └── 精确路由 -> select_best_channel()
├── select_best_channel()         score-based 5 维加权 [阶段3]
│   └── _is_channel_usable()      过滤 cooldown / 限额 / 限速
│       ├── cooldown_manager.is_in_cooldown()
│       └── rate_limiter.is_limited()
├── _select_by_group()            tier:xxx 组路由
│   ├── 组过滤: tier / capabilities / min_context / require_vision
│   ├── _has_multimodal_content() 跳过 multimodal [P0-3, V6.0 收窄]
│   ├── score_account()           5 维加权排序
│   ├── _get_input_price()        cache-aware 有效输入价折算 [V6.4 #11/#12]
│   └── 失败 -> fallback_group
├── _get_current_price()/_get_fuzzy_price()
│   ├── _is_price_effective()     按 effective_from/to 过滤
│   ├── _match_time_range()       峰谷定价: 工作日/周末/非高峰 token [V4.8.7]
│   └── promotion 行优先 [V4.8.5]
└── invalidate_price() / invalidate_group()  情报侧精准失效
```

### `gateway/proxy.py` — 上游转发

```
forward_request(channel, body, headers, requested_model, actual_model)
├── body 加工: _strip_thinking_fields / _inject_thinking_off / _drop_params
│             / _auto_learn_drop_params / _maybe_rewrite_body_model
├── _inject_prompt_cache()        [P1-8 + P3 (2026-08-21)]
│   ├── OpenAI 风格: prompt_cache_key = 稳定前缀分段 hash
│   │   (全量 system + 首条非 system, 各截断 4096 字符;
│   │    caller 已传 key 时不覆盖; 无 system fallback 首条 user)
│   └── Anthropic 风格: system + 第一条 user 加 cache_control (role 校验)
├── _forward_stream()             流式
│   ├── 正常: aiter_raw 原样透传
│   └── status>=400: 已 aread() 的缓冲直通 (StreamConsumed 修复 6069281)
└── _forward_normal()             非流式
    ├── httpx post
    ├── _update_cooldown()        mark_failure/mark_success + classify
    ├── _build_aimc_headers()     X-AIMC-* response headers
    └── _schedule_usage_log()      async fire-and-forget
```

### `gateway/quota.py` — 多维限额检查

```
check_user_quota(caller_id, estimated_tokens) [V4.8]
└── _sum_user_tokens() 按 usage_logs.caller_id 聚合本地日/月窗口
    └── 超限 → 429 daily/monthly_token_limit_exceeded

check_account_quota(account_id, estimated_tokens) [P0-6]
└── _check_dimension() 读 AccountQuotaUsage.used_value
    ├── balance_cny: window_start IS NULL = 未 sync -> 跳过 [P1-15]
    ├── rpm / tpm / concurrent / daily_tokens / weekly_tokens / 5h_tokens
    └── ⚠️ used_value 生产无写入方 → 除 balance 外恒放行 (见结合诊断 §A2)
```

### `gateway/cooldown.py` — 失败账号冷却状态机（V4.9）

```
CooldownManager (单例 cooldown_manager)
├── mark_failure(account_id, error_info=ErrorInfo)  按 ErrorCategory 差异化冷却
├── mark_success(account_id)                        恢复 healthy
├── probe_result(account_id, success, error_info)   探活回写 + 退避
├── is_in_cooldown(account_id)                      路由候选过滤
├── snapshot()                                      dashboard 只读快照
└── 状态: healthy / failing / degraded / needs_human / manual_disabled
```

### `gateway/error_classifier.py` — 上游错误分类

```
classify(status_code, body, ...) -> ErrorInfo
└── 9 类 ErrorCategory: QUOTA_EXHAUSTED / AUTH_FAILED / MODEL_UNSUPPORTED /
    RATE_LIMITED / UPSTREAM_5XX / NETWORK_TRANSIENT / CLIENT_ERROR /
    CONTEXT_WINDOW (行为中性, 2026-09-10) / UNKNOWN
    → 建议 cooldown 秒数 / 下次探活 / 是否 needs_human
```

### `gateway/embeddings.py` — embedding/rerank 直通（2026-09-11）

```
POST /v1/embeddings, POST /v1/rerank
├── 复用 proxy 的转发/计量私有函数 (_estimate_cost/_write_usage_log/...)
└── 复用 router route_engine 选路 + cooldown/error_classifier
```

### `intelligence/scheduler.py` — 定时任务

```
42 个 APScheduler job (AsyncIOScheduler + SQLAlchemyJobStore → apscheduler_jobs 表):
├── batch_crawl_{provider}_{interval} (动态, 当前 30 个): 按 (provider, poll_interval) 分组
│   (V4.8.3 修复旧 min(interval) 批量导致 model_list 源被拉高频;
│    默认间隔: web_page 30min / announcement 1h / price_aggregator 12h /
│    model_list 1d / benchmark 1w; 连续无变化自适应降频 4x/8x)
├── refresh_route_cache (1): 刷新路由缓存
├── check_key_rotation (1): 每天检查 key 轮换
├── cross_source_detection (1): 每天跨源交叉验证
├── monthly_official_audit (1): 每月 1 号官方源审查
├── weekly_suggest_changes (1): 周一 09:00 跑 suggest_changes [方案A, 见结合诊断 §C]
├── v44_alerts_check (1): 每 6 小时跑 V4.4 告警 (配额/账号/成本)
├── channel_health_probe (1): 每 60s 智能探活 [V4.9, 见结合诊断 §B]
├── cooldown_cleanup (1): 每 5min 清理过期 cooldown entry [V4.9]
├── account_health_log_cleanup (1): 每天凌晨 3 点删 N 天前探活日志 [V4.9.1]
├── promotion_cleanup (1): 每天 04:20 优惠情报过期清理 [2026-09-05]
├── transcript_log_cleanup (1): 每 6 小时清理超期请求转录 [V4.9.3]
└── intel_blindspot_audit (1): 每 6h 情报系统失明审计 [A-2, 2026-09-08]

每个 job 完成后:
├── EVENT_JOB_EXECUTED / EVENT_JOB_ERROR 事件
├── _record_job_event() listener 写 job_health [P0-8]
└── 失败多为 log-only 吞异常 (见结合诊断 §E)
```

### `intelligence/sources/` — 采集源（37 个实例, 5 类型）

| 类型 | 数量 | 说明 |
|------|------|------|
| price_aggregator | 10 | 定价页/聚合站，正则文本解析 → price_snapshots |
| announcement | 9 | 厂商公告 |
| model_list | 9 | API/页面模型清单（confidence=1.0 免 AI 解析） |
| benchmark | 5 | 榜单（LMSYS/SuperCLUE/ArtificialAnalysis/LiveBench/OpenCompass） |
| web_page | 4 | 厂商首页/活动页 |

### `dashboard/routes.py` — 看板 API（67 端点）

按功能分：auth/page(5) / channels(3) / accounts(8) / cooldown(4) / users·keys·用量(12)
/ intel·sources·建议·审批·报告(27) / ops·请求日志·设置(8)。
完整清单与前后端对应见 [api.md](api.md)；agent/CLI 契约见 [../agent-api.md](../agent-api.md)。

## 4. 模块依赖图

```
main.py ──► gateway/app.py ──► gateway/{routes,embeddings,middleware}
                  │                    │
                  │                    ▼
                  │             gateway/proxy.py ◄──(私有函数)── gateway/embeddings.py
                  │                    │  ▲
                  │                    ▼  │(延迟 import)
                  │             gateway/router.py
                  │                    │
                  │        ┌───────────┼────────────┐
                  │        ▼           ▼            ▼
                  │  gateway/cooldown gateway/limiter security/crypto
                  │        │
                  │        ▼
                  │  gateway/error_classifier
                  ▼
          intelligence/scheduler.py ──► sources/* ──► fetcher
                  │                              └─► differ ──► healer ──► {model_sync, pricing_sync}
                  │                                              └─► alerter ──► (feishu/bark/serverchan)
                  └─► channel_health ──► gateway/cooldown   (intelligence→gateway, 延迟 import)
                       pricing_sync ──► gateway/router.invalidate_price

 gateway ──► intelligence: proxy→alerter, router→recency  (延迟 import)
 intelligence ──► gateway: scheduler→router.refresh_cache, channel_health→cooldown

 dashboard/routes.py ──► gateway/{router,cooldown,middleware,error_classifier}
                    └──► intelligence/{alerter,channel_health,cross_source_validator,healer}
                    └──► scripts/{apply_suggestions,expire_accounts}   (反向依赖 scripts)
 所有层 ──► db/{models,crud,engine}
```

**依赖原则**：gateway 是核心被依赖方；intelligence 和 dashboard 都依赖 gateway 与 db；
gateway 只在「告警」(alerter) 与「时间衰减」(recency) 两处依赖 intelligence，且均为
函数内延迟 import。**这个双向依赖只差一步就是 import 环** —— 保持延迟 import 是硬约束。

## 5. 配置系统 (`config.py`)

```python
class Settings(BaseSettings):
    # 优先级: 环境变量 > .env > 默认值; 统一前缀 AIMC_
    # 例外 (validation_alias): AIMC_MASTER_KEY / OPENAI_API_KEY / OPENAI_BASE_URL
    host / port / log_level
    aimc_master_key        # Fernet 主密钥, 64 hex, 启动校验格式
    db_path                # ./data/aimc.db
    api_key                # AIMC_API_KEY, 网关鉴权 (空=不鉴权)
    dashboard_token        # AIMC_DASHBOARD_TOKEN, 看板鉴权 (空=legacy 裸开)
    openai_api_key / openai_base_url / parse_model   # AI 解析引擎 (经网关回环)
    alert_channel / bark_key / serverchan_key / feishu_webhook_url   # 告警通道
    proxy_url / route_refresh_interval / max_fallback_retries / request_timeout
    prompt_cache_inject_enabled / transcript_log_*   # V4.9.3 缓存 A/B + 转录
    health_probe_*                                   # V4.9 智能探活
    needs_human_{quota,auth,model}_threshold         # V4.9 阈值
    crawl_* / diff_similarity_threshold / parse_confidence_threshold
    browser_max_concurrent / browser_fetch_timeout   # V4.8.4 Playwright 防护
    price_alert_threshold_pct
```

> ⚠️ **厂商 API Key 不在 config.py**：渠道/账号密钥存在 DB（`channels` / `accounts`，
> 密文列 `api_key_ciphertext`），运行时由 router/exporter 解密。不要照旧文档在
> `.env` 里找 `SILICONFLOW_API_KEY` 之类的字段（历史文档曾错列）。

## 6. 异步 / 并发模型

```
FastAPI + uvicorn (async event loop)
  ↓
APScheduler AsyncIOScheduler (job_defaults: coalesce=True, max_instances=1)
  ↓
SQLAlchemy 2.0 async (asyncio + aiosqlite) + 独立 sync_engine (job listener)
  ↓
httpx.AsyncClient (上游调用)
```

**同步 vs 异步边界**：
- `proxy._schedule_usage_log` / `_schedule_alert`: async fire-and-forget（不阻塞响应）
- `middleware`: sync middleware（不破坏 async flow）
- `scheduler._record_job_event` listener: **用独立 sync_engine**（P0-8，避免 greenlet 冲突）

## 7. db 模型关系

```
channels ─┬─ accounts (1:N, FK)
           │    ├─ account_quotas / account_quota_usage (P0-6)
           │    ├─ account_health_log (V4.9 探活时间序列)
           │    └─ cooldown_event (V6.3.1 瞬时冷却流水)
           └─ price_snapshots (1:N)
                 ├─ input_price + output_price
                 ├─ cached_input_price + cache_write_price
                 └─ is_promotion + effective_from/to (V4.8.5)

model_groups ─┬─ 7 个 tier:* 组 (flagship/strong/balanced/light/vision/vision-light/code)
              └─ model_group_members (P0-1: FK + UNIQUE)
                   ├─ group_id (FK -> model_groups.id)
                   └─ model_id (FK -> model_registry.model_id)

model_registry
├─ model_id (PK) / tier / modality / supports_prompt_cache
└─ list_input_price / list_output_price (V6.0 官方标价, 不参与路由)

model_cards (benchmark 评分, whichllm 启发)
usage_logs
├─ caller_id / key_id / account_id / channel_id / group_id
├─ input/output/cached/… tokens / reasoning_tokens (V6.3)
├─ cost_estimate (实付) / list_cost_estimate (V6.0 标价)
├─ error_msg/error_type/error_code/needs_human/failed_channels
└─ created_at (本地时间; V6.4.1 起全库 DATETIME 统一 datetime.now 本地时间)

users / api_keys (V4.8 多用户) ; settings_runtime (P0-7 运行时配置)
job_health (P0-8) ; request_transcripts (V4.9.3) ; promotions / promotion_review_queue
intel_sources / parse_rules ; model_change_suggestions / group_change_suggestions
model_capabilities_log ; cross_source_discrepancies ; model_feedback_summary
```

共 27 个 ORM 表（另有 APScheduler 自建的 `apscheduler_jobs`）。

## 8. 迁移机制（重要，有已知风险）

不是 Alembic，没有版本表。两套并存：

- **自动**：`init_db()` = `Base.metadata.create_all`（只建缺失的表）+ `ensure_migrations()`
  （`db/init_db.py`，用 `PRAGMA table_info` 判断 + 手写 `ALTER TABLE ADD COLUMN`，
  硬编码覆盖约 30 个列/索引，幂等）。
- **手动**：`db/migrations/*.py` 共 23 个，`argparse + sqlite3` 直连，默认 dry-run，
  需 `--apply`；**无任何代码调用，全靠人工按 [../ops/migration.md](../ops/migration.md) 执行**。

**已知风险**（详见 [module-integration-audit.md](module-integration-audit.md) §F）：
新增 ORM 列若忘写 `ensure_migrations` 又不跑手动脚本，老库启动不报错、用到时才
`no such column`；`create_all` 不会给已存在表补约束/索引，ORM 声明的唯一索引在
「老库升级」与「全新库」上不一致；无版本表 → 无法判断某脚本是否已跑。

## 9. 模块间结合诊断（摘要）

> 逐条清单、证据与建议见 [module-integration-audit.md](module-integration-audit.md)。
> 这里只列结论。

**主干贯通**：`/v1/chat/completions` 的「鉴权 → 路由 → 限额 → 转发 → 冷却/分类 →
用量落库」全链路真实生效；情报「采集 → 变化 → 解析 → 落库 → 路由」的价格与元信息
两条支链贯通；dashboard 活跃前后端无断链。

**但存在三类结构性问题**：

1. **「看起来接通、实际空转」的旁路**（最隐蔽）：
   - 本地限流 `limiter.record_*` 全项目零调用 → `is_limited` 恒 False（真实保护=429→冷却）。
   - 账号配额 `quota.increment_usage` 不可达 → `AccountQuotaUsage.used_value` 生产零写入
     → 除余额外全部限额恒放行。
   - `router.score_bonus`（model_cards 评分）未被 `score_account` 调用 → 卡片缓存白加载。
   - `pricing_writer.write_pricing_snapshots_from_parse` 生产零调用 → 结构化价格被丢弃。
2. **确定性 bug**：`intelligence/channel_health.py` 探活失败分支用 `ErrorInfo` 但未 import
   → NameError → 失败探活不回写冷却状态机（成功分支正常）。
3. **工程债**：`gateway/fallback.py` 整模块死代码；dashboard 4 个死端点 + 2 个孤儿前端资产；
   `schemas/` 除 intel 外全部遗留；跨模块掏私有成员（`route_engine._channels_cache` 等）
   与宽 `except` 静默吞异常广泛存在（历史已因此发生过 route_engine NameError 断链）。

## 10. 设计哲学

1. **自用优先** — 允许半自动，不追求 100% 无人值守
2. **情报层 > 网关层** — 情报发现是核心差异化价值
3. **先跑通再优化** — 快速验证核心价值
4. **保持简单** — 单进程部署，零外部依赖
5. **精细化运营** — 峰谷定价（V4.8.7）、频限追踪、多账号兜底、标价双轨成本审计（V6.0）
6. **自动化收口以数据为前提** — 扩大自动化（如 T1-T3 自动入组）前先埋点观察真实代价，
   数据说话后再定策略（V6.3 F3 → V6.4 S3 即此路径的实证）

## 11. 历史架构决策记录

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
| P0-3 | multimodal skip | 纯文本请求不选 multimodal model（**V6.0 收窄**：仅 `require_vision=1` 组内生效） |
| P0-4 | TTL + mtime | 修 "db 改必 restart" |
| P0-7 | score weights dashboard | 价格 vs 高可用 trade-off 快速迭代 |
| P1-15 | 429 failover | 上游 plan 耗尽自动换 channel |
| V5.1 | 成员优先级激活 | sort key = (price, family_bonus, member_priority, -channel.priority) |
| V6.0 | vision 分级 + P0-3 收窄 | tier:vision（旗舰）→ tier:vision-light（免费兜底）→ tier:light |
| V6.0 | 标价双轨成本审计 | registry 官方标价 + usage_logs.list_cost_estimate；免费/订阅渠道实付 0 也可量化节省 |
| V6.1 | 鉴权统一 + agent 契约 | dashboard API 接受 admin api_key；统一 CLI `scripts/aimc.py`；agent-api.md |
| V6.x | qwen 命名规范化 | validate_model_id 放宽，规范名去双前缀，存量迁移 39+4 |
| 2026-09-05 | AUTO_JOIN_NON_T0 数据驱动决策 | 不立即开，先 F3 埋点积累 ≥2 周再按 S1-S4 择一 |
| V6.3 | F3 埋点纯只读上产 | 从现有表推导 5 组指标，不改任何入组行为 |
| V6.3 | F1 首启检测进 gateway startup | 空渠道/无 active 账号时 WARN 引导而非阻断启动 |
| V6.3.1 | 顶栏 badge 不为自愈错误报警 | `exclude_transient=true` + `cooldown_events` 流水留痕 |
| 2026-09-10 | CONTEXT_WINDOW 分类行为中性 | 不冷却/不探测/不 needs_human，仅可观测 + 为「上下文超限路由更大窗口渠道」留基础 |
| V6.4 | S3：AUTO_JOIN 开启 + 置信门槛 | `AUTO_JOIN_NON_T0=True` + `AUTO_JOIN_MIN_CONFIDENCE=0.9`（非 T0 门槛） |
| V6.4 | Phase B 渠道级注入默认关 | 强关思考可能降质量，先零行为上产逐渠道看数据 |
| V6.4 | cache-aware 有效输入价折算不落库 | 命中率是渠道行为非模型属性，内存 5min TTL，hit=0 退化原价 |
| V6.4 | SF org 段不进 model_id | family 取模型行系（FAMILY_HINTS），品牌前缀不叠词 |
| 2026-09-11 | 流式错误体走缓冲直通 | `_forward_stream` status≥400 已 aread() → 改 yield 缓冲字节，修 StreamConsumed |
| V6.4.1 | 全库时区统一本地时间 | 根除 `func.now()`(UTC) 与 `datetime.now`(本地) 混写，存量 UTC 行 +8h 迁移 |
| 2026-09-13 | 看板统计口径统一为滚动窗口 | 三语义（滚动/自然日 LIMIT/滚动分桶）同页不可对账；LIMIT 语义废弃，桶轴 Python 补零；rollup 表不立项（30d 聚合 11ms） |
