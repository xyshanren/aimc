# 架构设计

> 受众：想理解 AIMC 数据流和模块边界的开发者
> 前提：读过 [setup.md](setup.md)（本地跑起来）
> 配套：模块间结合问题的逐条清单见 [module-integration-audit.md](module-integration-audit.md)；
> 下一阶段规划调研见 [next-stage-research.md](next-stage-research.md)

> **文档时点**：2026-09-16（对应旗舰修复批，840 测试全绿）。文中的文件行号会随代码演进漂移，
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

**价行 `model` = 匹配键，`raw_model_id` = 真实上游模型名（V6.6, 2026-09-16）**

`price_snapshots` 的 `model` 列只做**候选匹配键**（请求的规范名精确匹配；fuzzy 仅在无任何精确候选时兜底），
转发上游时 `_channel_model` **优先取 `raw_model_id`**（exact/fuzzy/fallback 三处同规则），body.model 由此改写。
接入"上游模型 id 与规范名不同"的渠道（modelscope `deepseek-ai/*`、siliconflow `zai-org/*`）时，
每个模型**两行并存，各司其职**：

| 价行 model | raw_model_id | 作用 |
|---|---|---|
| 规范名（如 `deepseek-flash`） | 上游 raw id（如 `deepseek-ai/DeepSeek-V4.1-Flash`） | **组路由入口**——成员按规范名命中渠道，上游收到 raw id |
| 上游 raw id（raw_model_id 空） | — | **直连入口**——客户端按 raw id 请求时精确命中（观察线/e2e 用） |

> ⚠️ raw 行**不要删**：删掉后直连 raw id 的请求不再精确命中该渠道（fuzzy 也匹配不上），会掉到
> fallback 聚合渠道。反过来，只补规范名行而不填 `raw_model_id` 也不行——上游会收到规范名导致
> model not exist。同渠道"规范名行 + raw 行"并存是设计内形态，不是脏数据。
> 未接组的模型（如 modelscope 的 `Qwen/*` 两行）只有 raw 行，要进组时补"规范名行 + raw_model_id"即可。

**全渠道价行形态盘点（2026-09-16，有效行口径）**

| 渠道 | 规范名行 | raw/未注册行 | 判定 |
|---|---|---|---|
| deepseek_official | 2 | 0 | ✅ 全规范（v4-flash 系 6 行已清） |
| glm_bigmodel | 4 | 0 | ✅ 全规范 |
| minimax_compat | 8 | 0 | ✅ 全规范 |
| taotoken_coding | 2 | 0 | ✅ 全规范 |
| sensenova_official | 5 | 2 | ✅ 规范为主；2 行供应商自有名（sensenova-6.7-flash-lite / u1-fast）未注册 = 直连专用 |
| modelscope_inference | 3 | 5 | ✅ 本规范首例：3 规范行（raw_model_id 指 raw id）+ 3 raw 行（直连入口）+ 2 Qwen 纯直连 |
| stepfun_official | 1 | 1 | ✅ **2026-09-20 step-5-preview 接入**：规范名行+注册表条目（T1/multimodal/1M ctx，标价 7/20 cache 0.35，状态 preview）= 可进组路由；step-3.5-flash 未注册 = 仅直连/fallback；step-1o 已过时下线（价行移除） |
| siliconflow_aggregator | 13 | 99 | ✅ **2026-09-16 规范化完成**：13 规范名行=<br>（A）deepseek-v3/v3.2 + qwen3-8b/3.5-4b + qwen3-vl-32b-instruct/-thinking/30b-a3b-thinking/8b-instruct = **SF 独供 active 成员从 fallback 黑路径提为一等候选**<br>（B）deepseek-v4-flash / deepseek-v4-pro / glm-5.2 / minimax-m2.5 = 与官方渠道重复，显式付费兜底<br>（C）paddleocr-vl-1.5 = registry 已有规范名，用起来<br>+ glm-4-flash（light 组死成员）移除（glm-4.7-flash 已在组）。**全组 active 成员均有一等候选**，fallback WARN 降至 0。raw 行保留作聚合兜底 + 直连入口；embeddings/rerank 独立直通不受影响 |

**新渠道 / 新模型接入规范（V6.6 起，此后一律照此办理）**

1. 渠道价行 `model` 一律用**客户端会请求的名字**做匹配键：要进组路由的模型用 `model_registry`
   里的规范名；只做直连的模型用上游真名即可（精确路由不查 registry，直接匹配价行名）。
2. 上游真实模型 id 与匹配键不同时（`deepseek-ai/*`、`zai-org/*` 等带 org 前缀），差异填进
   `raw_model_id`，router 会优先用它改写 body.model；相同时留空。
3. 需要"直连 raw id"路径（观察线 / e2e / 临时直测）才额外保留 raw 行，否则不建。
4. 组成员的 model_id 必须在至少一个渠道有规范名精确价行，否则该成员永远选不到
   （fuzzy 只在"零精确候选"时兜底，有任一精确候选即被挡）。
5. `registry.raw_ids` 只有 embeddings 消费；chat 路由消费的是 `price_snapshots.raw_model_id`，
   两者不要混为一谈。
6. 聚合类渠道（siliconflow 模式）可整渠道保持 raw 行、专职 fallback——这是设计内形态；
   一旦要把它的某模型提为"一等候选"（进组/精确路由），再按本规范补规范名行。

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
│   ├── 正常 (status<400): aiter_raw 原样透传 (StreamingResponse)
│   └── status>=400: 错误体缓冲 → 普通 Response 直返 [V6.7]
│       (客户端尚未收到任何字节 → routes 可安全 failover;
│        旧 prebuffered/StreamConsumed 机制随之移除)
└── _forward_normal()             非流式
    ├── httpx post
    ├── _update_cooldown()        mark_failure/mark_success + classify
    ├── _build_aimc_headers()     X-AIMC-* response headers
    └── _schedule_usage_log()      async fire-and-forget
```

### `gateway/quota.py` — 多维限额检查

```
check_user_quota(caller_id, estimated_tokens) [V4.8, 语义不变]
└── _sum_user_tokens() 按 usage_logs.caller_id 聚合本地日/月窗口 + 准入预留
    └── 超限 → 429 daily/monthly_token_limit_exceeded

check_account_quota(account_id) [P0-6 → V6.6 纯真实测量, 无预留]
└── _check_dimension()
    ├── balance_cny: 读 AccountQuotaUsage (window_start=sync 标记) [P1-15]
    ├── tpm/daily/weekly/5h_tokens: usage_logs 按账号+窗口 SUM [V6.6]
    │   └── _token_window_start(): tpm=60s / daily=本地零点 / weekly=ISO周一
    │       或 rolling window_seconds / 5h=rolling 18000s
    ├── rpm: usage_logs 近 60s COUNT [V6.6]
    └── concurrent: 内存 in-flight 计数 (proxy acquire/release) [V6.6]

⚠️ 历史 (已根除): 旧实现读 AccountQuotaUsage.used_value 且结算半边
increment_usage 零调用点 → 预检退化静态门 max_tokens×2>limit, tpm 短窗对
大 max_tokens 请求恒拒 (deepseek_01/glm_01/stepfun_01 三行 tpm 全中招, 均已删)。
V6.6 重设计见决策表; account_quota_usage 仅剩 balance_cny sync 标记在用。
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

## 8. 迁移机制（V6.5+ 收敛为轻方案 A，2026-09-14 上产）

不是 Alembic（个人项目零运维哲学，版本表迁移名格式与 Alembic 兼容保留演进空间），
`init_db()` 四步串联：

1. `Base.metadata.create_all` — 只建缺失的表（不改已有表）。
2. `ensure_migrations()`（`db/init_db.py`）— 历史硬编码幂等迁移（`PRAGMA table_info`
   判断 + 纯字面量 `ALTER TABLE ADD COLUMN`），保留不动。
3. **`ensure_schema_alignment()`（`db/schema_align.py`，V6.5+ 新增）— ORM 单一事实源
   列对齐**：`_ALIGN_DDL` 为生成器（`scripts/gen_orm_column_ddl.py`）从 `db/models.py`
   产出的 238 条显式字面量常量，启动时对实际库逐条比对（`pragma_table_info` 一次取
   全部表全部列），缺列即执行预生成 `ALTER` 并登记版本表（幂等）。覆盖「新 ORM 列
   忘写迁移 → 老库 no such column」这一最高频事故路径。NOT NULL 无 server_default
   列不在常量内（SQLite ALTER 硬限制不可 ADD，只出现在全新库）。
4. `mark_legacy_migrations()` — 23 个历史手动迁移脚本登记 `schema_migrations` 版本表
   （note=`legacy-manual`，仅登记不执行），解决「无法判断某脚本是否已跑」的审计盲区。

**手动**：`db/migrations/*.py` 共 23 个，`argparse + sqlite3` 直连，默认 dry-run，
需 `--apply`；**无任何代码调用，全靠人工按 [../ops/migration.md](../ops/migration.md) 执行**。

**仍需人工迁移的场景**：列类型变更、删列、数据回填、NOT NULL 无默认列的补齐
（重建表流程）。ORM 列变更日常维护：改 `db/models.py` → 跑生成器替换 `_ALIGN_DDL`
→ 提交，启动自动对齐。

## 9. 模块间结合诊断（摘要，2026-09-14 更新）

> 逐条清单、证据与建议见 [module-integration-audit.md](module-integration-audit.md)。
> 这里只列结论。**V6.5 三批（2026-09-14）已修复其中大部分**，各项现状态见该文档 §I。

**主干贯通**：`/v1/chat/completions` 的「鉴权 → 路由 → 限额 → 转发 → 冷却/分类 →
用量落库」全链路真实生效；情报「采集 → 变化 → 解析 → 落库 → 路由」的价格与元信息
两条支链贯通；dashboard 活跃前后端无断链。

**三类结构性问题的现状态**：

1. **「看起来接通、实际空转」的旁路**（最隐蔽）：
   - 本地限流 `limiter.record_*` 全项目零调用 → `is_limited` 恒 False（真实保护=429→冷却）。
     **用户拍板 defer**（触发器见 next-stage-research §2.1）；账号配额 tpm 维度曾恒拒死且
     隐形（deepseek_01 tpm 行已删 + 预检改 failover+落库，2026-09-14）。
   - `router.score_bonus`（model_cards 评分）未被 `score_account` 调用 → 卡片缓存白加载。
     **待 C4 决策**（与 V7.0/F4 同批）。
   - `pricing_writer.write_pricing_snapshots_from_parse` 生产零调用 → 结构化价格被丢弃。
     **未接**（healer 走正则 + 折扣链已修正，见下）。
2. **确定性 bug**：探活 `ErrorInfo` NameError **已修**（V6.5 批次一，失败探活恢复回写冷却）。
3. **工程债**：S1 Content-Length / S4 differ 吞变化 / S6 周报 XSS / S7 停机丢计量 /
   S8 路由成功率截断 / S9 cache 时区 / S11 sync_engine PRAGMA / S12 旧 job 残留
   **已全部修复**（V6.5 三批，2026-09-14）；迁移机制收敛轻方案 A **已上产**
   （schema_migrations 版本表 + ORM 列对齐，见 §8）；死代码清理（`gateway/fallback.py`、
   crud 17 死函数、schemas 3 文件、`fetcher.fetch_page`、静态孤儿 3、死端点 4）与
   静默 except 收窄（middleware._query_api_key 两档 + proxy 5 处 warning 化）
   **已上产**（V6.5 第四批，2026-09-14）。

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
| 2026-09-14 | 配额预检拒绝改 failover + 落库 | 预检静态估算（max_tokens×2>limit 恒真）拒死 deepseek-flash 兜底且隐形 429（不 mark_failure/不落 usage_logs）；回归 quota.py "try_next_account" 原设计意图 |
| 2026-09-14 | rate_limited 冷却指数退避 + 迟到成功守卫 + 渠道级同步 | sensenova 双账号共享 TPM 池：固定 30s 账号冷却解冻即重撞；冷却期内在途 200 完成会擦掉冷却（mark_success 守卫）；兄弟账号 rate_limited 冷却对齐 = 共享池渠道级升级 |
| 2026-09-14 (V6.5) | 冷却过期不 pop state | 过期 pop 连 count 一起丢 → 「到期→router 查询→重撞」主场景下退避恒 30s（审核实证）；改 `cooldown_expired_at` 标记 + cleanup 600s 保留窗 + 超窗 count 归零；needs_human 不再被过期查询洗白 |
| 2026-09-14 (V6.5) | differ peek/commit 两段式 | 先落 hash 再解析，解析失败即永久吞掉该次变化（页面不再变则永不重试）；改 `has_changed(commit=False)` + `commit_source_hash()`，解析成功才提交 |
| 2026-09-14 (V6.5) | 折扣只作用解析命中模型 + promotion 强制 effective_to | 旧实现折扣乘到渠道全部 normal 模型（siliconflow 99 个）；promotion 行缺 end_date 经 upsert 旧值兜底永久生效（永久打折）；router 对存量 `effective_to=None` promotion 脏行拒绝兜底。订阅类（无折扣率）不转价格行（双轨拍板） |
| 2026-09-14 (V6.5) | fire-and-forget 任务登记 + 停机 drain | 裸 create_task 不保存引用，lifespan dispose 前未等待 → 重启丢计量；`proxy._spawn()` 登记 + `shutdown_drain_tasks(timeout=10)`；定时采集 job 注册进 `_inflight_tasks` |
| 2026-09-16 | requires_reasoning_echo 渠道级 CoT 回传 | DeepSeek 官方带 tools 要求每轮 assistant 回传 reasoning_content，跨渠道混合历史整轮 400；缺 rc 补空串占位 + CoT 会话不注入 enable_thinking=false（Phase B 注入会抹平 CoT） |
| 2026-09-16 | 规范名匹配键与上游模型名分离 | 价行 `model` 只做匹配键，`_channel_model` 优先 `raw_model_id`（modelscope `deepseek-ai/*` 规范价行接入的前提）；同渠道"规范名行+raw 行"并存是设计内形态（raw 行=直连入口勿删），见 §3 router 小节 |
| 2026-09-16 | 删 glm/stepfun tpm 假门，metering 立项未实现 | `increment_usage` 零调用点 → account_quotas 预检=纯静态门 `max_tokens×2>limit`；删假阳性行止血（deepseek_01 先例），token 维改 usage_logs SUM 真实测量单独立项 |
| 2026-09-16 (V6.6) | 账号配额改纯真实测量（metering 重设计落地） | token 维=usage_logs 按账号+窗口 SUM（tpm 60s/daily 零点/weekly/5h rolling）、rpm=近 60s COUNT、concurrent=内存 in-flight（proxy acquire/release，重启归零属预期）；预检不再收 estimated_tokens——吞吐门不能预押，结算半边（increment_usage）连同窗口状态行一并废除；balance_cny 语义不变 |
| 2026-09-17 (V6.7) | 流式请求参与 failover | 旧 `not is_stream` 门使流式请求零 failover（ZCode 全流量流式）→ 上游 429 直接打穿到客户端（2026-09-17 生产双 429 实证）；`_forward_stream` status>=400 改缓冲普通 Response 直返（客户端尚未收到字节，换候选安全），routes 重试分支对流式/非流式统一 |
| 2026-09-17 (V6.7) | 限流语义兜底先于 error.code 精确匹配 | sensenova 同端点对不同账号对同一 TPM 限流返回不同 code（429001 / insufficient_quota，type 均为 rate_limit_error）、modelscope 免费层 token-limit 429 也是 insufficient_quota——按 code 误判 quota_exhausted（3600s 冷却 + 假"需人工干预"告警）；429 + 文案/type 带限流语义（rate_limit/tpm/rpm/token-limit）→ RATE_LIMITED 300s，真计费耗尽 message 不含这些词不受影响 |
| 2026-09-17 (V6.7) | 终态 429 insufficient_quota 改写 rate_limit_error | 所有候选耗尽仍返回 429 时，语义是限流的错误体改写 code=rate_limit_exceeded/type=rate_limit_error（保留原始 message + Retry-After: 30）——客户端（ZCode）对 insufficient_quota 判 retryable=false 直接终止回合不再重试 |
| 2026-09-20 (V6.10) | taotoken 双模型桶 LRU 轮换 + cooldown 模型级配额隔离 | tier:strong 大请求（≥30000 输入，sensenova 被 max 窗剔除）全部砸 deepseek@taotoken → deepseek_for_coding 桶每 ~3h 触顶 429，账号级冷却拖死 glm 桶 → fallback glm_bigmodel、taotoken glm 桶空转（9/20 实证）。改动：① cooldown 新增 `_quarantine[(account,model)]`——QUOTA_EXHAUSTED+model 只封本桶（无 model 调用保持旧账号级语义）；② select_best_channel 大请求且 taotoken 有最低价健康候选时收敛到 taotoken（直连大请求同效）；③ 组路由同账号 ≥2 模型桶时 LRU 轮换（赢家 member_priority 覆盖 0）。生产验证：路由时刻 13:12 / 用量 21:20 均衡，glm 桶承接 143k-302k 输入全 200 |
| 2026-09-20 (V6.10) | usage_logs.first_token_ms（TTFT）+ 性能统计卡 | 现有计量只有端到端 latency_ms（eff tok/s = Σout/Σlat 被 prefill 稀释，大上下文不可横向比）；stream_generator 首个数据 chunk 记 TTFT（nullable，非流式/错误流 NULL，schema_align 自动 ALTER）；dashboard `/api/usage/perf` Python 侧 nearest-rank P50/P95 + eff_tok_per_s + TTFT 覆盖，用量页「性能统计」卡三 tab。SQLite 无 percentile，聚合在 Python 侧 |
