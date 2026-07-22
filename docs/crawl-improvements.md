# AIMC 情报层鲁棒性改进计划

> 调研来源：crawl4ai (53k⭐, Apache-2.0) / firecrawl (25.6k⭐, AGPL-3.0) / crawlee-python (Apache-2.0)
> 创建时间：2026-07-22
> 状态：Phase 2A 编码中

## 一、为什么需要改进

AIMC 爬虫监控 3 家厂商的 6 个 source（DeepSeek×2 / Qwen×2 / GLM×2），间隔 15~30 分钟轮询。
当前实现跟参考项目对比，有 9 项 P0/P1 缺失，按优先级排列如下。

## 二、Phase 2A — 必做（最小可用改进）

**目标**：让情报层敢开生产。预算 ~1-2 天。

### 2A-1. fetch 失败重试 + 指数退避【P0】

**借鉴**：firecrawl `apps/api/src/lib/utils/retry.ts`（1s/2s/4s 退避，4xx 不重试）

**现状**：
```python
# intelligence/fetcher.py:42
try:
    page = await browser.new_page()
    # ... no retry on failure
except Exception as e:
    logger.error(f"页面抓取失败: {url} - {e}")
    return None
```

**目标**：
- `fetch_page_with_retry(url, max_attempts=3, base_delay=1.0)` 函数
- 退避：`1s → 2s → 4s`，可配
- 4xx 状态码（除 408/429）不重试（重试无意义）
- 5xx / Timeout / ConnectError 重试

**影响文件**：`intelligence/fetcher.py`

### 2A-2. differ 实际用上相似度【P0】

**借鉴**：crawl4ai / difflib.SequenceMatcher（**当前代码已写 `compute_similarity` 但 `has_changed` 没调用**）

**现状**：
```python
# intelligence/differ.py:35
async def has_changed(source, new_content: str) -> bool:
    new_hash = compute_hash(new_content)
    if not source.last_hash:  # 第一次
        return True
    if source.last_hash == new_hash:  # 哈希同
        return False
    # 哈希不同 → 直接返回 True（**没用上相似度**）
    return True
```

**问题**：网页加 1 个字符就会触发 AI 解析，钱白花。

**目标**：
- `IntelSource` 表加 `last_content TEXT NULL` 字段（存上次完整内容）
- `has_changed` 改成：哈希同 → false；哈希不同 → 算相似度，< 阈值 → true
- `update_source_hash` 同步写入 `last_content`（但要限制大小，截断存前 50KB）

**影响文件**：`intelligence/differ.py` + `db/models.py` + `db/crud.py`

**迁移**：SQLite 不支持 `ALTER TABLE ADD COLUMN IF NOT EXISTS`，需要先 `PRAGMA table_info` 检查列存在再添加。给 `db/init_db.py` 加 `ensure_migrations()` 幂等函数。

### 2A-3. fetch 资源拦截（text_mode）【P1】

**借鉴**：crawl4ai `text_mode=True`（`page.route("**/*.{png,css,font}", abort)`）

**现状**：`page.content()` 全加载，包括图片/CSS/Font/广告追踪器。

**目标**：
- 在 `browser.new_context()` 后注册全局 route
- 屏蔽：图片（png/jpg/jpeg/gif/webp/svg/ico）、样式（css）、字体（woff/woff2/ttf/otf）
- 收益：抓取速度 3-5x，浏览器内存 ↓↓

**影响文件**：`intelligence/fetcher.py`

### 2A-4. 手动触发入口【P1】

**借鉴**：firecrawl 异步 job 模式 + 自定义 UI 触发；crawlee `enqueue_links` 手动入队

**现状**：scheduler 启动后只能等定时；调试时改完代码要等 15 分钟才能验证。

**目标**（**简化版**）：
- `intelligence/scheduler.py` 加 `async def run_source_now(provider: str)` 方法
- 立即执行该 provider 下所有 source 的抓取（不等定时器）
- `dashboard/routes.py` 加 `POST /dashboard/api/sources/run-now?provider=deepseek`
- 不接 DB（Phase 2A 简化版；完整版要做 `IntelSource` 跟 dataclass 的关联，Phase 2C 再做）

**影响文件**：`intelligence/scheduler.py` + `dashboard/routes.py`

## 三、Phase 2B — 强烈建议（性价比高）

**目标**：AI 解析成本 ↓ 60%，质量 ↑，自愈覆盖 70% 失败。预算 ~2-3 天。

### 2B-1. HTML → 干净 Markdown 净化【P1 - 最大 ROI】

**借鉴**：crawl4ai `DefaultMarkdownGenerator` + `PruningContentFilter`

**现状**：直接截断 3000 字符 HTML 喂 LLM，含 script/style/nav/footer 噪音。

**目标**：
- 加 `intelligence/cleaner.py`，用 `BeautifulSoup` + 自定义规则
- 移除：script/style/nav/footer/aside/header（除内容区 header）/comments
- 保留：main/article 优先，否则 body 全部
- 输出 Markdown：保留代码块语言标记 + 表格转 markdown
- `parser.py` 喂净化后的内容（截断阈值可以从 3000 降到 1500，等价信息）

**收益**：AI 解析 token 成本 ↓ 50%，解析质量 ↑（不再被广告 CSS 干扰）

**影响文件**：`intelligence/cleaner.py`（新增）+ `intelligence/parser.py`

### 2B-2. 失败降级到 CSS selector（接 ParseRule 表）【P1】

**借鉴**：crawl4ai 多种 extraction strategy fallback

**现状**：
- `db.models.ParseRule` 表已存在（provider / url_pattern / css_selector / ai_strategy / confidence）
- `db.crud.upsert_parse_rule / get_parse_rules` 已写好
- **但 `healer.py` 没用上** — AI 失败直接告警，不尝试 CSS selector 降级

**目标**：
- `healer.handle_parse_result` 流程改造：
  1. AI 解析成功 + 置信度 ≥ 阈值 → 入库（不变）
  2. AI 解析成功 + 置信度 < 阈值 → 告警（不变）
  3. AI 解析失败 → 查询 ParseRule 找该 provider 的 css_selector → 走 CSS 提取 fallback → 成功则入库（**新增**）
  4. 全部失败 → 告警人工

**影响文件**：`intelligence/healer.py` + `db/crud.py`（可能要加 `get_active_parse_rule`）

### 2B-3. 等待策略精细化【P1】

**借鉴**：crawl4ai `wait_until` (domcontentloaded/load/networkidle) + `wait_for` (CSS selector)

**现状**：固定 `wait_for_timeout(2000)`，不论页面复杂度。

**目标**：
- 加 `wait_strategy` 字段到 `IntelSourceBase`：可选 `domcontentloaded` / `load` / `networkidle` / `css:<selector>`
- 改 `fetch_page` 接收 `wait_strategy` 参数
- 默认：定价页 `domcontentloaded`（内容少）；活动页 `load` 或 `css:.announcement`

**影响文件**：`intelligence/fetcher.py` + `intelligence/sources/base.py` + `intelligence/sources/*.py`

## 四、Phase 2C — 按需（业务增长后）

| 项 | 借鉴 | 工作量 | 触发条件 |
|----|------|-------|---------|
| 多引擎策略（Http / Playwright 自适应） | crawl4ai AdaptiveCrawler | 1-2 天 | 出现 SPA 站点 / 静态页太多想分流 |
| Browser context 复用 + storage_state | crawlee persistent context | 半天 | 需要登录态抓取 |
| 反爬指纹伪装 | crawl4ai enable_stealth | 半天 | 被反爬时 |
| AI 解析 few-shot examples | LangChain | 半天 | 解析质量不够时 |
| ParseRule 自动升级 | Reflexion 思路 | 1 天 | 人工反馈攒够 5+ 条时 |
| 持久化队列 | firecrawl BullMQ | 1 天 | 多进程 / 任务很多时 |
| scheduler 错峰 + 并发限制 | firecrawl BullMQ priority + delay | 1 小时 | source > 10 个时 |
| 结构化 diff（按节点 / 段落） | difflib.SequenceMatcher 升级 | 1 天 | differ 阈值 0.9 仍误报多时 |

## 五、不做的（避免过度工程）

- **crawlee label-based 路由系统** — 6 个 source 写死够用
- **代理池** — 3 家厂商不会反爬
- **Webhook 异步通知** — 单进程同步就行
- **Dataset 抽象层** — SQLite 直接存
- **CDP 模式接已有 Chrome** — 个人自用不折腾

## 六、Phase 排期

| 阶段 | 范围 | 工作量 | 触发条件 |
|------|------|-------|---------|
| 2A（做）| 4 项 P0/P1 必做 | 1-2 天 | 现在 |
| 2B（按需）| 3 项 P1 强建议 | 2-3 天 | 跑通 2A 后 |
| 2C（按需）| 8 项 P2 可选 | - | 业务增长后 |

## 七、跟 hermes-tray 联调关系

爬虫改进跟 SSE 元数据注入（hermes-tray v0.3.0 联调）**独立**，可以并行做：
- 这条线（crawl-improvements.md）：情报层内部质量
- hermes-tray 联调线（architecture.md Phase 2）：网关层 SSE 注入

两侧各自 commit 走各自仓库，**没有代码耦合**。
