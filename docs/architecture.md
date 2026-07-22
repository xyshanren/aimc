# AIMC 架构设计文档

## 一、系统定位

面向个人开发者的轻量级 LLM 网关与优惠情报聚合工具。

**核心价值**：情报发现（哪里最便宜）+ 智能路由（怎么调用最划算）。

## 二、设计原则

| 原则 | 说明 |
|------|------|
| 自用优先 | 允许半自动，不追求 100% 无人值守 |
| 情报层 > 网关层 | 情报发现是核心差异化价值 |
| 先跑通再优化 | Phase 1 快速验证核心价值 |
| 保持简单 | 单进程部署，零外部依赖（无 Redis/MQ） |
| 精细化运营 | 峰谷定价、频限追踪、多账号兜底 |

## 三、系统架构

```
+---------------------------------------------------------------+
|                    用户 / 客户端                                |
|              (OpenAI SDK / HTTP 直接调用)                       |
+-----------------------------+---------------------------------+
                              |
                              v
+---------------------------------------------------------------+
|                 自研网关层 (Python FastAPI)                     |
|  1. 统一 OpenAI 兼容接口（/v1/chat/completions）               |
|  2. 路由决策引擎（时段/配额/价格/频限 多维权重）               |
|  3. 内存滑动窗口频限（QPM/TPM/并发/日限额）                   |
|  4. 流式/非流式原样透传（aiter_raw，不解析 SSE）               |
|  5. 多账号轮询与自动切换                                       |
+------------------------------+---------------------------------+
                              |
                              v
+---------------------------------------------------------------+
|                  SQLite 数据库 (WAL 模式)                       |
|  渠道/账号 │ 价格快照 │ 优惠活动 │ 用量日志 │ 频限配额 │ 规则  |
+------------------------------^---------------------------------+
                              |
+---------------------------------------------------------------+
|                 情报采集层 (同进程异步任务)                      |
|  APScheduler → Playwright抓取 → Diff检测 → AI解析 → 入库/告警  |
+---------------------------------------------------------------+
                              |
                              v
+---------------------------------------------------------------+
|              极简 Dashboard (FastAPI 静态文件 + htmx)            |
+---------------------------------------------------------------+
```

## 四、核心模块设计

### 4.1 路由决策引擎 (`gateway/router.py`)

```
收到请求（model）
    → 筛选支持该 model 的渠道
    → 识别当前时段，获取对应价格
    → 频限过滤（排除 QPM/TPM/并发 达阈值的账号）
    → 配额过滤（排除耗尽的账号）
    → 多维排序：价格 ASC > 剩余配额 DESC > 优先级 DESC
    → 选择最优渠道执行
    → 失败 → fallback 到下一候选（最多重试2次）
    → 成功 → 异步记录用量
```

### 4.2 频限器 (`gateway/limiter.py`)

纯内存实现，支持：
- **QPM**：60s 滑动窗口 deque
- **TPM**：60s 滑动窗口（按 token 累加）
- **并发数**：asyncio.Semaphore
- **日限额**：每日零点重置计数器

### 4.3 代理透传 (`gateway/proxy.py`)

核心原则：**绝不解析 SSE 流，原样透传字节**。
- 流式：`httpx` stream + `aiter_raw()` → `StreamingResponse`
- 非流式：等待完整响应 → `JSONResponse`
- 超时：120s（大模型响应慢）

### 4.4 情报采集层 (`intelligence/`)

插件式采集源设计：
- `base.py` 定义 `IntelSourceBase` 协议
- 每个厂商一个文件，注册到调度器
- 流程：定时抓取 → Diff 检测 → AI 解析 → 入库
- 自愈：置信度 > 0.8 自动入库；< 0.8 告警人工

## 五、数据模型（8张表）

| 表名 | 用途 |
|------|------|
| `channels` | 渠道（厂商接入点） |
| `accounts` | 账号（多账号兜底） |
| `price_snapshots` | 价格快照（支持峰谷时段） |
| `promotions` | 优惠活动（情报核心） |
| `channel_quotas` | 频限配额配置 |
| `usage_logs` | 用量日志（归档查账） |
| `parse_rules` | 解析规则（自愈循环） |
| `intel_sources` | 情报采集源配置 |

### 关键表结构

```sql
-- 价格快照（支持峰谷）
CREATE TABLE price_snapshots (
    id              INTEGER PRIMARY KEY,
    channel_id      INTEGER REFERENCES channels(id),
    model           TEXT NOT NULL,
    price_type      TEXT DEFAULT 'normal',  -- normal/peak/offpeak/cache_hit/free
    time_range      TEXT DEFAULT '全天',    -- "00:00-08:00" 或 "全天"
    input_price     REAL,
    output_price    REAL,
    effective_from  TEXT,
    effective_to    TEXT
);

-- 频限配额
CREATE TABLE channel_quotas (
    id              INTEGER PRIMARY KEY,
    channel_id      INTEGER REFERENCES channels(id),
    account_id      INTEGER REFERENCES accounts(id),
    quota_type      TEXT NOT NULL,  -- qpm/tpm/daily/total/concurrent
    limit_value     INTEGER NOT NULL,
    reset_strategy  TEXT DEFAULT 'rolling_window'
);
```

## 六、关键设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 自研网关 vs OneAPI | 自研 FastAPI | 更轻量、完全可控、与情报层同进程 |
| SQLite vs PostgreSQL | SQLite WAL | 零运维、单文件、个人工具并发足够 |
| 同进程 vs 独立爬虫 | 同进程异步 | 简化部署 |
| 内存频限 vs Redis | 内存 | 单进程无需共享状态 |
| Dashboard | htmx + 静态文件 | 零构建，维护成本极低 |

## 七、部署方案

```bash
# 单命令启动（网关+调度器+Dashboard 同进程）
uvicorn main:app --host 0.0.0.0 --port 8080

# 进程守护（Linux）
supervisorctl start aimc

# 数据备份（cron 每日）
python scripts/backup.py
```

环境要求：Python 3.11+，无需 Docker。


## 八、开发计划

> aimc 还在早期(1 个 feat commit,9103c37 之后),没有正式 release tag。
> 这里按 Phase 跟进展分,不绑死版本号;首次 tag(v0.1.0)再统一开 ROADMAP.md。

### 当前状态 (2026-07-22 14:35)

- Phase 1 脚手架完成:
  - 路由决策引擎 (`gateway/router.py`) — 时段/价格/配额/频限 多维权重选最优渠道
  - 内存频限器 (`gateway/limiter.py`) — QPM/TPM/并发/日限
  - 代理透传 (`gateway/proxy.py`) — 流式/非流式原样字节透传
  - OpenAI 兼容接口 (`gateway/routes.py`) — `/v1/chat/completions` + `/v1/models` + `/health`
  - SQLite schema (8 表) + `usage_logs` 写入
  - 情报采集层 (`intelligence/`) 脚手架,OpenAI 解析引擎 (`parse_model: gpt-4o-mini`)
  - 极简 Dashboard 框架 (FastAPI 静态 + htmx)
- License 已统一为 MulanPSL-2.0 (commit 9103c37)
- **没动**: SSE 路由决策元数据透出、promotion 表实际使用、Dashboard 实装、
  fallback 跨渠道自动切换、API key 轮询。`routes.py:list_models()` 只回
  `{"id": m, "object": "model", "owned_by": "aimc"}` 3 字段。

### Phase 2 — SSE 路由决策元数据注入 (跨 hermes-tray, ~0.5d)

**触发**: hermes-tray v0.2.1 的 footer model pill + stats page 拿不到
aimc 实际选的渠道和 cost,前端只能显示 routing 意图(用户的 `model`
请求),tray 端 stats 的 cost 是用自己 pricing 表拍的,可能跟 aimc 实际
收的对不上。Phase 1 必做。

**改动**: `gateway/proxy.py` `_forward_stream` 当前是 `aiter_raw()`
原样透传;改成在 first chunk yield 后,先 yield 一段自定义 SSE event,
再继续透传剩余字节。**不解析上游 SSE**(架构原则不动),只插一段。

注入格式用 OpenAI SSE 兼容的 `event:` + `data:` 两行结构
(hermes-tray 端 SSE parser 只看 `data:` 行,新 event type 是
forward-compatible):

```
event: routing_decision
data: {"chosen_channel": "AWS-Bedrock Account-1", "model": "claude-opus-4-6", "cost_estimate_usd": 0.0234}

```

字段:
- `chosen_channel` (str): aimc 选中的渠道名(`channel["name"]`)
- `model` (str): 用户请求的 routing key (与 SSE `data.model` 同)
- `cost_estimate_usd` (float, 可选): aimc 按选中渠道价格算的预估成本,
  由 `_forward_normal` 已有 `_write_usage_log` 同源计算
- `routing_strategy` (str, 可选): `price_first` / `quota_first` / `fallback`,
  方便 debug

**实现要点**:
- `injected_routing` 布尔 flag,first chunk 之后置 true,只注入 1 次
- 流式响应:在 `stream_generator()` 内部 `await response.aiter_raw()` 循环里
- 非流式响应:不注入(没有 SSE 流;非流式走 HTTP response headers 或
  响应 body 顶层加 `routing_decision` 字段,跟 hermes-tray 联调时再定)
- 错误路径:aimc 自己 error 时不注入(走 502/503/504,直接 JSONResponse)

**测试**:
- `tests/gateway/test_routing_injection.py`:mock upstream stream,
  验证 first chunk 之后是 `event: routing_decision\ndata: {...}\n\n`
  + 后续字节原样
- happy path + 错误路径 + 多次请求隔离

**联调**: hermes-tray 侧 v0.3.0 Phase 1 一并做:
- `src/lib/chat-stream.ts` 解析新字段 → 写 state
- footer pill 读 `chosen_channel` + `model`
- stats page by_model 表加 "渠道" 列 (key 改 model+channel 二元组)
- 详见 hermes-tray ROADMAP.md Phase 1 「aimc SSE 注入 + tray 端 consumed」

### Phase 3+ — 待定 (不写死,等 Phase 1 + 2 用起来再说)

- `routes.py:list_models()` 加 `owned_by` (从 `channel.provider` 透出) +
  `display_name` (跟 model id 一致或单独映射) — hermes-tray 端能本地
  split 渲染(虽然 aimc routing key 没 `/`)
- `routes.py:list_models()` 还可以加 `context_window` / `capabilities`
  / `pricing`,给 hermes-tray 做 cost 估算(目前 tray 用自己 pricing 表
  跟 aimc 不一致,Phase 2 注入的 `cost_estimate_usd` 才是真值)
- `usage_logs` 已写但没暴露查询接口,Dashboard 加 "by channel /
  by model / by day" 透视
- promotion 表当前 schema 落库但没在路由里用,等"自动用最便宜的
  活动价"逻辑
- API key 轮询 + 跨账号 fall back(目前 channel 绑死 account)
- Dashboard 实装:htmx 写"渠道对比" / "用量热图" / "价格日历"
- 跟 hermes-tray 的 `recent_routing_decision` 字段对齐(Phase 2
  注入就是这块的 producer)

### 跨项目 cross-ref

- **hermes-tray** (D:\work\workspace\Qoder\hermes-tray) 的 v0.3.0
  Phase 1 同时做了 aimc SSE 注入的 consumer 侧。两侧 commit 走各自
  仓库,联调在 v0.3.0 alpha-33 release 后做。
- 一旦 aimc 进入"被 hermes-tray 当中介 gateway 用"为主场景的阶段,
  Phase 2/3 的优先级会跟着 hermes-tray 的 stats page UX 迭代。
