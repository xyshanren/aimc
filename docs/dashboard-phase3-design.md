# 看板 Phase 3 — 信息架构重排 + UI 现代化设计 (已批准, 实施待启动)

> 2026-09-05 · 基于生产库近 14 天真实使用数据 + 开源竞品面板调研。
> 前置: 指标体系统一 Phase 1/2 已上线 (docs/dashboard-metrics-plan.md)。
> **决议 (2026-09-05 用户确认)**: ① 导航用侧边栏; ② transcript 仅 admin key 可见; ③ 折叠交互彻底移除; ④ 实施顺序 3a 骨架 → 3b 请求日志 → 3c 皮肤后置。
> **编码尚未开始**, 本文档为实施蓝图。

---

## 0. 决议记录

| # | 问题 | 决议 |
|---|---|---|
| 1 | 导航形态 | **侧边栏** (竞品主流, 240px, <768px 折叠为顶部横滚 tab) |
| 2 | 请求日志 response body | **仅 admin key 可见** (transcript 详情接口服务端校验 `is_admin=1`) |
| 3 | 折叠交互 | **彻底移除**。理由: 页面化后每页 2-4 张卡, 折叠省高度的价值消失; 竞品无卡片折叠; 折叠状态记忆徒增复杂度。长表用固定高度滚动容器 (现有 `.scroll-y-*`), 明细用抽屉, 不用折叠。保留唯一例外: 账号管理内「+ 加新账号」表单披露 (局部表单 UI, 非卡片折叠) |
| 4 | 实施顺序 | **3a 骨架 → 3b 请求日志 → 3c 皮肤后置**。3b 排在皮肤前: 新页面一次进入 IA, 皮肤 pass 一并覆盖避免返工 |

---

## 1. 调研结论

### 1.1 生产库使用数据 (ubuntu2401, 近 14 天)

| 发现 | 数据 | 对设计的含义 |
|---|---|---|
| **单用户自用为主** | 近 7 天 1286 请求中 caller_id=1 占 1250 (97%), api_keys 6 把仅 4 把 7 天内用过 | 不需要多租户式复杂度; 「按用户」「Key 管理」是低频功能 |
| **量级小、非秒级监控** | 日均 11~868 请求 (9/4 峰值 868), 无凌晨流量 | 概览不需要监控大屏式密度; 60s 轮询足够 |
| **晚间活跃** | 请求集中在 10-12 点 + 19-23 点 | 个人作息; 早上打开看板应一眼看到夜间发生了什么 (概览要有"今日摘要") |
| **tier:strong 绝对主力** | strong 985 (77%) > flagship 155 > balanced 125; 其余 tier 合计 <1% | 概览的分组视图只需突出前 3 个 tier |
| **模型双寡头** | glm-5.3-flash + deepseek-v4-flash 占 71% 请求 | top 模型表格 5 行即可覆盖主要信息 |
| **省钱是核心价值** | 近 7 天实付 ¥6.8 vs 标价 ¥395 (省 98%) | 成本是最高频关注点 → 成本独立成页 |
| **审查队列平时是空的** | promotion_review / cross_source / 模型组建议 pending 全部 = 0 | 待处理/待审建议降级为全局 badge + 收进运维页 |
| **请求明细无入口** | request_transcripts 已积 1566 条 / 103MB, 看板没有任何日志查看页 | 排障要登 VM 查库 → 新增请求日志页 (竞品一级公民) |

### 1.2 竞品面板 IA 模式 (LiteLLM Admin UI / NewAPI(one-api 系) / Helicone / Langfuse)

1. **无一例外是「侧边栏导航 + 分区页面」**, 典型分区: Dashboard / Requests or Logs / Usage / Spend / Providers / Keys / Settings。
2. **请求日志是一级公民**: Helicone Requests 表、LiteLLM Logs、NewAPI 日志页都是排障第一入口。
3. **概览页 = 趋势图主视觉 + 少量 KPI**, 运维细节不放概览。
4. **运维/审查类收进子页**, badge 提醒积压。
5. **视觉语言趋同**: 分层背景 + 低对比边框 + 单一强调色 + pill 徽章 + 无竖线表格 + 大数字 KPI (Vercel/Linear/shadcn 风), 明暗双主题。

---

## 2. Phase 3a — 信息架构重排 (下一步实施)

### 2.1 总体结构: 侧边栏 + 6 页, 零构建哈希路由

```
┌──────────────────────────────────────────────────────────────┐
│ status-bar:  AIMC · ● 运行中          [统计窗口控件]  [待处理●1]│  ← 全局顶栏 (跨页)
├──────────┬───────────────────────────────────────────────────┤
│ ◆ 概览    │  页标题                                            │
│ ◆ 成本    │                                                   │
│ ◆ 用量    │   当前页卡片 (2 列自适应网格, 无折叠)                │
│ ◆ 渠道    │                                                   │
│ ◆ 情报    │                                                   │
│ ◆ 运维    │                                                   │
│──────────│                                                   │
│  周报     │                                                   │
│  登出     │                                                   │
└──────────┴───────────────────────────────────────────────────┘
```

- 路由: `#/overview`(默认) `#/cost` `#/usage` `#/channels` `#/intel` `#/ops` — hashchange 切换, 直链/刷新还原; 与现有 `?range=` query 共存。
- 侧边栏 240px, 6 个 16px 线性图标 (inline SVG 手写, 不引图标库), 当前页 `bg-hover` 高亮 + 左侧 2px 强调色条。
- 顶栏: 品牌 + 服务状态点 (现有) + **全局时间范围选择器** (从成本卡提升, 24h/7d/30d) + 待处理/待审建议 badge。
- 顶栏 badge 逻辑: 复用现有 pending summary + 限额预警数据源, 30s 轮询一次 (全局唯一常驻请求); N>0 才显示, 点击跳 `#/ops`。

### 2.2 页面与卡片映射 (旧 → 新)

| 新页 | 内容 | 来源卡 |
|---|---|---|
| **概览** `#/overview` (默认) | 4 KPI + 标价vs实付趋势图 + Top 模型表 (5 行) + 分组概览 (前 3 tier) + 今日摘要 (昨夜以来请求/错误/告警条) | 实时状态、用量统计(top5)、组路由摘要、限额预警摘要(仅异常时显示摘要条) |
| **成本** `#/cost` | 成本审计 (按模型/分组/用户 tabs) + Cache 节省明细 (按渠道/按模型) | 原两卡平移 |
| **用量** `#/usage` | 用量统计日表 + 路由成功率明细 + 请求日志 (3b 落地后加入) | 用量统计、组路由命中率 |
| **渠道** `#/channels` | 渠道 + 价格 + Model Registry + Groups | 原两卡平移 |
| **情报** `#/intel` | 优惠情报 + 优惠审查 + 待确认能力变更 + 周报 | 原四卡; 周报从页尾升入 |
| **运维** `#/ops` | 数据新鲜度 donut + 限额预警完整表 + 账号管理 + Key 管理 + 采集源管理 + 交叉源核查 + 待审建议列表 | 原右栏全部 + 数据新鲜度 |

- 概览页「今日摘要」: 时间范围固定近 24h (不受全局范围影响), 内容 = 请求数 / 错误数 / 非活跃账号数 / 队列积压数, 全正常时显示一条绿色「过去 24h 一切正常」。

### 2.3 轮询调度器 (3a 核心改造)

现状: 多个 `setInterval` + htmx `every 30s/60s` 全页常驻轮询。目标: **页面注册表 + 单一调度器**。

```js
const PAGE_ROUTES = {
  overview: { title: '概览', load: [loadOverviewKPIs, loadUsageTop, loadQuotaBadge], ms: 60000 },
  cost:     { title: '成本', load: [loadCostAudit, loadCacheStatsList], ms: 60000 },
  usage:    { title: '用量', load: [loadUsageChart, loadGroupRouting /* +3b loadRequests */], ms: 60000 },
  channels: { title: '渠道', load: [loadChannelsPage], ms: 30000 },
  intel:    { title: '情报', load: [loadPromotions, loadPromotionReviews, loadPendingReview, loadWeeklyReport], ms: 60000 },
  ops:      { title: '运维', load: [loadFreshness, loadQuotaWarning, loadAccounts, loadUserKeys, loadSources, loadSuggestionsList], ms: 60000 },
};
// hashchange → 停旧定时器 → 立即执行当前页 loaders → 启动当前页 interval
// 顶栏 badge: 独立 30s 轮询, 跨页常驻
```

- **剩余 htmx 轮询卡全部转为 JS loader** (渠道/价格/registry×2/优惠/待确认/周报): 渲染器已存在 (afterSwap 里的 renderXxx), loader 沿用 `loadGroupRouting` 模式 (textContent 写入 + renderXxx 解析)。转完后 **htmx 无使用点, 移除 `vendor/htmx.min.js` 引用** (少一个全局依赖; afterSwap 分支同步删除)。
- 删除: `details.card-folding` 折叠结构与 chevron CSS、`overview-trend` 死 canvas、旧 suggestions 死代码残留。
- 时间范围选择器提升后, `applyTimeRange` 重拉清单改为「当前页 loaders」。

## 3. Phase 3b — 请求日志页 (唯一后端改动)

### 3.1 API (routes.py 新增, 只读)

```
GET /dashboard/api/requests?days=7&status=all|ok|error&model=&key_id=&page=1&page_size=50
  → { total, page, page_size, items: [{ id, created_at, model, channel_name, group_id,
        status_code, latency_ms, input_tokens, output_tokens, cached_tokens,
        cost_yuan, list_cost_yuan, error_code }] }
  · SQL: usage_logs 联 channels(name); 按 created_at DESC; LIMIT/OFFSET; days 上限 30

GET /dashboard/api/requests/{id}/transcript
  → { request_body, response_body, truncated: bool }
  · 服务端校验调用 key is_admin=1, 非 admin → 403
  · 单侧截断保护: body >256KB 截断并置 truncated=true (transcripts 表 103MB, 防单次拉爆)
```

### 3.2 前端 (挂在 `#/usage` 页)

- 列表表: 时间/模型/渠道/状态(徽章)/延迟/token/实付, 复用 METRIC_DEFS 格式; 过滤条 (状态/模型/时间范围)。
- 行点击 → 右侧抽屉 (drawer) 显示 request/response body: `<pre>` + JSON 缩进, 顶部显示截断标记; 抽屉非折叠交互, Esc/遮罩关闭。
- 全部走 authFetch。

## 4. Phase 3c — UI 视觉刷新 (皮肤后置)

### 4.1 设计 tokens (在现有 CSS 变量体系上改值, 不换体系)

```css
:root {
  /* 分层背景: 页面底 < 卡片面 < 悬浮层 (替代"重边框"的层次手法) */
  --bg-base: #0b0e14;  --bg-surface: #11151d;  --bg-elevated: #1a2029;  --bg-hover: #161c26;
  /* 边框全面降对比: 只留发丝线 */
  --border-default: #232a35;  --border-muted: #1a2029;
  /* 文字三级 */
  --text-primary: #e8edf4;  --text-secondary: #9aa7b8;  --text-muted: #5d6879;
  /* 强调色仅 1 个, 语义色只用于状态 */
  --color-accent: #4d9fff;  --color-success: #3dd68c;  --color-warning: #f0b429;  --color-danger: #f6685e;
  /* 圆角统一 10/14; 字号阶梯 12/13/14/16/20/28 */
  --radius-md: 10px;  --radius-lg: 14px;
}
```

浅色主题同步这套层级 (改 `prefers-color-scheme: light` 块的值即可)。

### 4.2 组件规格

| 组件 | 现状 | 目标 |
|---|---|---|
| 卡片 | 1px 实边框 + 折叠箭头 | `bg-surface` + 发丝线边框, 圆角 14, 标题 13px medium, 无折叠 |
| KPI 卡 | 小数字 16px | 大数字 28px + 12px 灰标签, 无边框色块 |
| 表格 | 全边框密排 + 竖线 | 去竖线, 表头 11px 大写灰字, 行 hover, 数字右对齐等宽 |
| 徽章/状态 | 彩色文字散落 | pill 徽章 (半透明底 + 语义色文字) |
| 侧边栏 | 无 | 见 2.1 |
| 图表 | Chart.js 默认 | 线宽 1.5、去点 (hover 显示)、网格用 `--border-muted` |
| 按钮 | 多种杂色 | 主=强调色实底, 次=透明+边框, 危险=语义红; sm/md 两档 |

### 4.3 保留不变

零构建 (不引 Tailwind/组件库)、`authFetch` + `aimc_dashboard_token` 鉴权链路、Chart.js、明暗双主题、现有后端 API (3b 新增只读除外)。

## 5. 验收清单

**3a**
- [ ] `#/` 六页可切换、刷新还原当前页、直链可分享; 侧边栏当前页高亮
- [ ] 顶栏 badge 仅在积压 >0 时出现, 点击跳 `#/ops`
- [ ] 切页后只有当前页的 API 在轮询 (Network 面板验证); htmx 引用移除且无报错
- [ ] 折叠交互全站消失; 长表在滚动容器内
- [ ] 旧书签 `?range=30` 仍生效; `node --check` + 标签配对 + 浏览器 1600/900px 渲染通过

**3b**
- [ ] 请求日志列表可过滤/翻页, 数字走 fmt 模块; 非 admin key 拉 transcript 返回 403
- [ ] >256KB transcript 显示截断标记
- [ ] routes.py 新增接口有测试; `systemctl --user restart aimc` 后 VM 生效 (static 类不需重启, 本期需要)

**3c**
- [ ] tokens 落地后明暗两套主题观感一致; 无 4 位小数金额; 组件与 §4.2 规格一致

## 6. 风险与回滚

- 3a 是 index.html 全量重写: e2e 已确认无 DOM 结构断言 (grep 验证过); 每期独立 commit, 任一期可回滚 (static 文件 revert + VM pull 即可, 无需重启)。
- 3b 涉及 routes.py: 部署需 `export XDG_RUNTIME_DIR=/run/user/$(id -u) && systemctl --user restart aimc`; 接口只读, 无 schema 变更。
- 移除 htmx 属不可逆简化: 若回滚 3a 则一起回滚, 无兼容包袱。
