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
