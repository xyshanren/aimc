# 生产审查报告（2026-09-13）

> 受众：决定修复优先级与投入方向的维护者
> 范围：gateway（路由/代理/限额/冷却）、dashboard（聚合 SQL 与指标口径）、
> intelligence（采集→解析→入库管道）、安全与并发/可靠性
> 方法：**四路并行静态审查 → 逐条读源码复核 → 对可证伪项做实证/生产库核对**。
> 配套：[module-integration-audit.md](module-integration-audit.md)（结构性诊断）、
> [next-stage-research.md](next-stage-research.md)（已并入本文建议的排期）

> **证据分级**：`[实证]` = 本机跑代码/查生产库验证过；`[读码]` = 逐行读过源码确认；
> `[待核]` = 代码确认但生产影响未定。生产库 = VM `ubuntu2401:~/aimc/data/aimc.db`
> （10,559 usage_logs，6 渠道全 `api_style=openai`，数据跨度 2026-08-13~09-13）。

---

## 0. 结论摘要

**好消息**：核心链路（鉴权→路由→转发→成本计量）的数据正确性总体可靠——成本审计四端点可互相
对账、滚动窗口统一到位、无 SQL 注入、密钥展示已脱敏。首轮审查中几条「看似严重」的指标问题
（流式 usage 丢失、Anthropic 口径、DeepSeek 原生 cache、cache JOIN 丢行）**经生产核对后不成立
或仅为潜伏**，见 §3。

**需处理的问题**集中在四处：

1. **一个已实证的响应协议 bug**（失败链响应 Content-Length 与 body 不一致）——触发罕见但会直接损坏响应。
2. **情报核心价值链「promotion → 价格行」在生产 0 产出**，且一旦触发会**把一个折扣乘到该渠道全部模型**（对 99 模型的 siliconflow 是灾难）。
3. **若干口径/计数失真**：路由成功率在 30d 被截断高估、cache-aware 折算用了 UTC 窗口（与刚统一的本地时区相悖）、失败重复计数。
4. **停机丢数据窗口**：fire-and-forget 的用量/转录任务与定时采集任务在关停时不被等待。

---

## 1. 严重度汇总

| # | 定级 | 位置 | 一句话 | 证据 |
|---|------|------|--------|------|
| **S1** | 高 | `gateway/routes.py:274-289` | 失败链响应复制旧 `Content-Length`，body 变长 → 响应被截断/h11 协议错 | [实证] |
| **S2** | 高（已知） | `gateway/quota.py:197-214` | 账号多维限额 `used_value` 生产零写入 → rpm/tpm/daily/weekly/5h/并发恒放行 | [读码]+[实证] |
| **S3** | 中高 | `intelligence/healer.py:64-105` | 「优惠→价格行」生产 0 产出；且折扣会乘到该渠道**全部**模型；`effective_to` 缺失可致价格永久打折 | [读码]+[实证] |
| **S4** | 中 | `intelligence/differ.py:72/91/101` | 先提交新 hash 再解析 → 解析失败则该次变化**永久丢失**（页面不再变则永不重试） | [读码] |
| **S5** | 中 | `gateway/proxy.py:847`+`gateway/routes.py:244` | 同一失败被 `mark_failure` 两次 → 冷却计数/`needs_human` 提前触发 | [读码] |
| **S6** | 中 | `dashboard/routes.py:2842,2866` | 周报原文未转义塞进 HTML → 管理域存储型 XSS | [读码] |
| **S7** | 中 | `gateway/proxy.py` create_task + `app.py:104-130` + `scheduler.py:310-336` | 关停时 fire-and-forget 用量/转录任务与定时采集任务不被等待 → 丢计量/半写 | [读码] |
| **S8** | 中 | `dashboard/routes.py:1901-1917` | 精确路由 `LIMIT 20` 后再求和 → 路由成功率被**高估**（30d 实测 27 个精确模型 > 20，已触发） | [读码]+[实证] |
| **S9** | 低中 | `gateway/router.py:1566-1578` | cache 命中率查询用 UTC `datetime('now')` 比对本地 `created_at`（~0.9% 窗口偏移）；且 async 内跑同步 sqlite；按 name 子查询不保证唯一 | [读码]+[实证] |
| **S10** | 低 | `gateway/middleware.py:142-144,245` | DB 查询异常静默回退 legacy 单 key（`logger.debug`，生产不可见） | [读码] |
| **S11** | 低 | `db/engine.py:42-47` | sync_engine 缺 `busy_timeout`/`synchronous` PRAGMA → `job_health` 写入竞争者下易 `database is locked` 且被静默吞 | [读码] |
| **S12** | 低 | `intelligence/scheduler.py:103-111` | 只清旧两类 job id，`batch_crawl_{provider}_{interval}` 改间隔后旧 job 残留 → 重复采集 | [读码] |

---

## 2. 需处理问题详述

### S1【高，实证】失败链响应 Content-Length 与 body 不一致

`gateway/routes.py:274-289`：复制 `response.headers`（含上游/原响应的 `content-length`），
再往 `parsed["error"]` 注入 `failed_channels` 使 body 变大，然后 `JSONResponse(headers=new_headers)`。
starlette 的 `init_headers` 仅在「headers 中不存在 content-length」时才重算，因此**沿用了旧长度**。

**本机实证**（starlette 1.3.1）：

```
original content-length = 25, body len = 25
new declared content-length = 25, actual body len = 351   ← MISMATCH
```

**影响**：组路由重试耗尽后返回失败链时，声明长度 < 实际 body。h11/uvicorn 可能抛
`Too much data for declared Content-Length`（连接中断），或客户端截断 JSON 拿不到失败链。
**触发频率**：低（生产 `usage_logs.failed_channels` 非空行 = 0），但一旦触发即损坏响应。

**修法**：构造新 `JSONResponse` 前 `pop` 掉 `content-length`（及 `transfer-encoding`/`connection`）：
```python
new_headers.pop("content-length", None)
```

### S2【高，已知决策项】账号多维限额从未生效

`gateway/quota.py:increment_usage`(197) 唯一调用者 `decrement_concurrent`(210) 同样零调用 →
不可达。全仓 `AccountQuotaUsage.used_value` **无生产写入方**，而 `check_account_quota`→
`_check_dimension` 真实读它 → 除 `balance_cny`（外部 sync）外全部维度恒读 0 恒放行。生产核对：
`accounts` 6 行、`account_quotas` 存在，但无任何请求因 rpm/tpm/daily 被拦。

**这是既有已知项**（2026-09-07 用户拍板 defer，见 [module-integration-audit.md](module-integration-audit.md) §A2）。
本报告不主张立即接线，但建议**至少显式化**：看板限额卡与服务文档注明「本地位限额当前为装饰性，
真实保护=上游 429→冷却」。接线触发器见 [next-stage-research.md](next-stage-research.md) §2.1。

### S3【中高】优惠→价格行：生产 0 产出 + 一旦触发会大面积错价

三个叠加问题（`intelligence/healer.py:64-105`）：

1. **生产 0 产出**：`price_snapshots WHERE is_promotion=1` 生产 **0 行**（37 条 promotions 全部没转成价格行）。
   当前 37 条多为订阅/编程套餐类（无折扣率），解析器自然不转——即「核心差异化卖点」（优惠可参与路由）
   在数据上从未跑通，仅停留在"入库展示"。这是**价值链验证缺口**，不是纯 bug。
2. **折扣乘到渠道全部模型**（`:70-94`）：`rows = 该 channel 全部 price_type=='normal' 行`，逐行 `× discount`。
   若某次解析出「Kimi K2 五折」，会把该 provider 下**所有** normal 价都生成五折行。生产
   `price_snapshots` channel 3（siliconflow）有 **99 个模型**——一旦触发即 99 行错价。
3. **`effective_to` 缺失致永久打折**（`:81-94` + `crud.py:238`）：AI 未给 end_date 时为 None，
   `upsert` 的 `effective_to = effective_to or snapshot.effective_to` 首写 None 后永久 None；
   router `_get_current_price` 见 `is_promotion==1` 直接返回 → 该模型价格永久打折（实扣高于估算）。

**修法**：(a) 折扣只作用于 `result.models` 命中的模型集合，无模型信息则不做价格行；
(b) promotion 行强制 `effective_to`（缺失用抓取日 +7/+30 天保守默认）；router 对 promotion 加"过期回退 normal"兜底；
(c) 补一条端到端测试用真实"五折"样例验证整链，确立"促销可路由"的验收线。

### S4【中】differ 先落 hash 后解析 → 解析失败即永久丢变化

`intelligence/differ.py:72/91/101` 的 `has_changed()` 内部调 `_update_source_hash` **提交新 hash**，
`scheduler._run_source_task` 之后才 `handle_parse_result`。若解析（LLM 超时/异常/无 CSS）失败，
新 hash 已前移 → 只要页面字节不再变，**该次变化永不重试**（无重扫补偿）。生产影响：静默漏采。

**修法**：拆分 `peek_hash` / `commit_hash`，仅在解析成功（或明确判定"无优惠/无模型"）后提交 hash。

### S5【中】同一失败被 mark_failure 两次

`gateway/proxy.py:847`（`_forward_normal` 内 `_update_cooldown`）已对非 200 调 `mark_failure`；
`gateway/routes.py:244` 重试分支对**同一 response** 再调一次 → `count` 每次 +1。
`quota_exhausted` 的 `needs_human` 阈值 = 2 → 一次真实失败即 count=2 → 直接 needs_human。
与注释「避免单次偶发误判」的设计意图相反。

**修法**：routes 复用 proxy 已算的分类结果，不重复 mark；或 proxy 只返回分类、由 routes 统一 mark。

### S6【中】周报端点存储型 XSS

`dashboard/routes.py:2842,2866`：`f"<pre class='report-md'>{text}</pre>"`，`text` 是磁盘上周报原文
（含采集来的模型名/URL 等第三方数据），未 HTML 转义。admin 打开 `/dashboard/api/reports/latest`
即执行脚本，可读 `localStorage` 的 dashboard token 并调管理 API。

**修法**：返回 `text/plain` 或 JSON，或后端 `html.escape`；前端用 `textContent`。

### S7【中】关停丢数据窗口

- `gateway/proxy.py` 的 usage/transcript/alert 用 `loop.create_task` 且不登记；`app.py:104-130`
  lifespan 关闭时直接 `engine.dispose()`，未 drain → 重启瞬间在途日志丢失。
- `intelligence/scheduler.py:310-336`：`wait_inflight_fetches` 只等待模块级 `_inflight_tasks`，
  而该集合仅在 `run_source_now` 填充；定时 job `_run_provider_batch` **不在**集合内 →
  关停时正在跑的采集不被等待，可能出现「hash 已更新、内容未入库」的半写。

**修法**：模块级 `set[Task]` 登记并在 `engine.dispose()` 前 `gather(timeout=...)`；
`stop_scheduler` 用 `shutdown(wait=True)` 或把定时任务也纳入 in-flight 集合。

### S8【中，实证】路由成功率在 30d 被截断高估

`dashboard/routes.py:1901-1914`：精确路由先 `.limit(20)` 再 `unmatched_total = sum(rows)`
（`:1914`），`total_requests = group + unmatched_total`（`:1917`）→ `group_routed_pct` 分母偏小 →
成功率被高估。生产核对 distinct 精确模型数：24h=3 / **7d=17** / **30d=27** / all=28
→ **30d 视图已触发截断**（27>20），前端 `gr-grouped-pct` 当前偏高。

**修法**：`unmatched_total` 用单独的**不带 limit** 的 `count()`；`limit(20)` 只用于 `top_models`。
另 `model.isnot(None)` 只加在 unmatched 侧，两边应一致。

### S9【低中】cache-aware 折算的 UTC 窗口与本地时区相悖

`gateway/router.py:1571` 用 `created_at >= datetime('now','-7 days')`（SQLite `datetime('now')` = **UTC**），
而 `usage_logs.created_at` 自 V6.4.1 起是**本地时间**。生产量化：同一 7d 条件 UTC 窗口命中
6778 行 vs 本地窗口 6718 行（**多纳入 ~60 行 / 0.9% 的 8h 偏移**）→ 命中率与折算轻微失真，
且与刚上产的时区统一（4198b8e）相悖。附带：`:1566` 在 async 事件循环内做**同步** sqlite 查询
（5min 缓存缓解首查/过期时的阻塞）；`:1572` 按 `channels.name` 子查询关联而 `name` 无唯一约束。

**修法**：cutoff 改 Python `datetime.now()` 计算后传参（与 quota 一致）；查 `channel_id` 而非 name；
必要时走 async session。

### S10-S12（低）

- **S10** `middleware._query_api_key` 宽 `except` 返回 None → 调用方静默回退 legacy 单 key，
  日志为 `debug`（生产 INFO 不可见）。非鉴权旁路（legacy 分支仍要求等值），但把 DB 抖动翻译成
  「多用户鉴权临时失效」。建议区分「表不存在」与「查询异常」，后者 `error` 级。
- **S11** `db/engine.py:42-47` sync_engine 只设 `journal_mode=WAL`，缺 async 侧已有的
  `busy_timeout=5000`/`synchronous=NORMAL`；`job_health` 经它写，竞争下 `database is locked`
  会被 `scheduler.py:296-298` 静默吞。
- **S12** `scheduler.py:103-111` 只删 `crawl_{provider}_{type}` 与 `batch_crawl_{provider}`，
  未删旧的 `batch_crawl_{provider}_{old_interval}` → 改 `poll_interval` 后旧 job 残留在 JobStore
  继续跑 → 同源重复采集。建议启动时按当前 id 集合枚举清理。

---

## 3. 潜伏问题（当前不触发，条件具备即生效）

生产 6 渠道**全部 `api_style=openai`**，因此以下「Anthropic 风格」相关缺陷当前**不产生影响**，
但加任一 Anthropic 风格渠道即生效：

| 项 | 位置 | 说明 |
|----|------|------|
| Anthropic 流式 usage 漏读 | `gateway/proxy.py:1309` | `_extract_stream_usage` 只取顶层 `obj["usage"]`，Anthropic `message_start` 的 usage 嵌在 `obj["message"]["usage"]` → input/cache 字段全丢 |
| Anthropic input 语义误减 | `gateway/proxy.py:90-91` | Anthropic `input_tokens` 本不含 cache_read，代码仍 `input - cache_hit` → 成本低估 |
| cache 命中率 >100% | `dashboard/routes.py:1291,1321` | 分子含 `cache_read`、分母 `input_tokens` 不含 → Anthropic 行命中率可 >100% |

生产实证均不触发：`cache_read_input_tokens` 非空仅 **1 行**；各渠道 7d 命中率
97.0%(deepseek)/88.3%(sensenova)/83.5%(minimax)/95.4%(glm)/0.0%(siliconflow)，**无一 >100%**。

---

## 4. 首轮审查中被纠正/撤回的结论

> 记录在此，避免后人重复走弯路；也说明「静态审查必须生产核对」。

| 首轮结论 | 核对结果 |
|----------|----------|
| 流式 usage 大面积丢失（未注入 `stream_options.include_usage`） | **不成立**。生产 `usage_logs.input_tokens IS NULL` = **0 行**，6,613 行有 cache token → 流式 usage 实际被采集（客户端/上游已带 usage）。代码确未主动注入，属可选的健壮性改进，非当前 bug |
| DeepSeek 原生 `prompt_cache_hit_tokens` 未解析 → 成本高估 | **不成立**。`deepseek_official` 7d 1,352 行中 **1,348 行** 有 `cached_input_tokens`（命中率 97.0%）→ 该上游实际返回 OpenAI 风格字段，已被解析 |
| cache/stats 用 INNER JOIN 丢 NULL channel 行 | **不成立**。生产 `channel_id IS NULL` 行 = **0**。属防御性改进（加 `outerjoin` 或用无 join 聚合），非当前失真 |
| 「路由成功率含 fallback 计数」是文档漂移 | 确认是文档描述与实现不符（`/api/stats/group_routing` 只做 model 分布），清文档即可 |

---

## 5. 已核实正确的部分（可放心）

- **成本审计四端点可对账**：`cost-by-model/group/user/trend` 同滚动窗口；「标价节省」=
  `list_cost − cost`，且 `cost` 仅对 `list_cost_estimate IS NOT NULL` 行求和（标价未知不计节省），
  口径一致，有 `test_v60_cost_audit.py` 覆盖。
- **看板滚动窗口统一到位**（2026-09-13）：`usage/summary`/`cost-trend` 均含 cutoff，桶轴 Python
  生成升序无重复键，24h 小时分桶格式与 Python `strftime` 一致；`/api/requests` 分页 total 与 list
  同条件。
- **无 SQL 注入**：全仓用户可控 SQL 均参数绑定/ORM；唯一 `text(f"...")` 在 `scripts/reset_db.py`，
  表名为内部白名单常量。
- **密钥处理规范**：`_api_key_to_dict`/`_mask_key_display` 只回 `key_prefix`；未发现明文 key 日志/回显。
- **`_validate_upstream_url`**（embeddings）已挡 loopback/私有/保留地址；开放跳转历史 medium 已前后端双白名单修复。
- **`max_instances=1`/`coalesce` 全局生效**（在 `add_job` 前设 job_defaults）。

---

## 6. 建议修复排期（已并入 next-stage-research.md）

```
批次一（P0，~0.5 天）— 低成本高确定性
  ├─ S1 失败链响应 pop content-length（1 行 + 回归测试）
  ├─ S5 去重复 mark_failure（避免 needs_human 误报）
  ├─ S6 周报端点转义/改 JSON
  └─ 既有 P0：channel_health ErrorInfo import + scheduler 硬编码 cwd

批次二（P1，~1 天）— 数据正确性
  ├─ S8 路由成功率 LIMIT 截断（unmatched 改无 limit count）
  ├─ S9 cache 命中率改本地时区 cutoff + 按 channel_id
  ├─ S4 differ 拆分 peek/commit hash（解析失败可重试）
  └─ S11/S12 engine PRAGMA 补齐 + scheduler 旧 job 清理

批次三（P1，~1~1.5 天）— 情报核心链修复 + 验收
  ├─ S3a 折扣只作用于命中模型集合
  ├─ S3b promotion 强制 effective_to + router 过期回退
  ├─ S3c 端到端"五折"样例测试，确立"促销可路由"验收线
  └─ S7 停机 drain（用量任务 + 定时采集 in-flight）

决策项（用户拍板）
  ├─ S2 本地限流/账号配额是否接线（不接则显式化"装饰性"）
  └─ 潜伏项：是否提前补 Anthropic 口径（当前无 Anthropic 渠道，可等）
```

优先级依据：批次一/二的修复**确定、低风险、直接纠正可见数字**；批次三触及情报核心价值链，
是「差异化卖点是否真成立」的验证，价值最高但需先确认产品意图（订阅类优惠是否本就设计为不转价格行）。
