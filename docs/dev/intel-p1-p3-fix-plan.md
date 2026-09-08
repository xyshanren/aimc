# 情报系统遗留问题设计方案：P1 重复入库去重 + P3 七个 error 源修复

> 2026-09-09 起草。背景：9/8 A-2 采集健康盲区告警上产后，排查确认两项遗留：
> 问题 1 = stepfun 同一事件重复入库（AI 措辞标题绕过去重）；问题 3 = 7 个 error 源 URL/形态失效。
> 本文档只做方案设计，未实施。问题 2（content_guard 签名）与问题 4（_resolve_api_key accounts 回退）
> 已于 9/9 修复上产（d1bbe08 / e69c2cb）。

---

## 问题 1：stepfun 同一事件重复入库

### 现象与根因

- 9/8 一天内同一事件「Step Plan 订阅套餐上线」入库 11 条（id 1072-1082），全部指向同一 URL
  `platform.stepfun.com/step-plan`，标题为 AI 每次总结的不同措辞（"上线/上新/新…上线/推出新版…"）。
- 根因链：
  1. stepfun price_aggregator 源页面含动态内容，**每次抓取 hash 都不同**（differ 相似度恒 ~0.463），
     每个轮询周期都触发 AI 全量解析；
  2. AI 对同一页面每次生成**措辞不同的标题**；
  3. `_save_promotion` 的去重是 **7 天内 (provider, title) 精确匹配**（healer.py ~L422），
     措辞变体永远打不中。
- 历史同样模式：8/18-8/21 四天入库 4/5/11/9 条。

### 设计方案（两层去重，向后兼容）

**层 1（核心）：内容相似度去重 — `_save_promotion` 精确去重之后加第二道**

- 位置：`intelligence/healer.py` `_save_promotion`，现有 (provider, title) 精确匹配之后。
- 逻辑：查同 provider 最近 7 天的 promotions（现有查询扩为取 id/title/content 前 N 条，LIMIT ~20），
  用 `intelligence/differ.compute_similarity`（已有函数，零新依赖）计算候选 content 与每条旧 content 的相似度；
  **max 相似度 ≥ 0.70 → 判定重复，跳过入库**。
- 判据说明：同一页面的 AI 摘要措辞会变但事实集合（四档套餐名、Credits 数字、权益列表）不变，
  实测 9/8 的 4 条 content 两两相似度在 0.6~0.8 区间；不同事件的 content 相似度显著低于此
  （阈值 0.70 起步，观察期后校准）。
- 日志：`优惠去重(内容相似 {sim:.2f} vs promo_id={id}): {title}` INFO 级，便于观察期统计误杀率。
- 可选增强（默认不做）：重复时改走 UPDATE——刷新既有行的 updated_at / confidence 取高者，
  避免同一事件永远只有首抓时的旧摘要。观察期后视需要再开。

**层 2（降本）：differ 伪变更抑制 — 可选，观察层 1 效果后决定**

- stepfun 页相似度恒 ~0.463 意味着每 30min 一次无效 AI 解析（token 成本）。
- 思路：differ 记录「上次相似度」，若连续 N 次相似度都落在相近区间（±0.05）且内容长度方差小，
  判定为动态噪声（时间戳/随机数），跳过 AI 解析、只更新 hash 基线。
- 风险：改 differ 影响全部源，需要 per-source 灰度（先用 settings 开关只对 price_aggregator 类型启用）。
- 注：P1-1「differ 先 clean 再 hash」已在旧路线图，与本层互补（clean 掉动态片段后 hash 本身会稳定），
  若实施 P1-1 则本层可降级为观察项。

### 验收

- 重放 9/8 的 11 条 stepfun 内容 → 仅第 1 条入库，其余 10 条命中相似度去重（测试用固定夹具断言）。
- 不同事件（正常新优惠）不受影响：构造相似度 < 0.7 的夹具正常入库。
- 存量清理（一次性脚本，实施时单独立项）：按 (provider, url, 7 天窗) 聚类，
  每簇保留 confidence 最高一条、其余标记 status='duplicate' 或直接删除（删前导出备份 JSON）。

### 改动面

| 文件 | 改动 |
|------|------|
| `intelligence/healer.py` | `_save_promotion` 增加相似度去重段（~30 行） |
| `tests/test_healer*.py` 或新增 | 相似度去重 3 个夹具测试（重复/不重复/阈值边界） |
| `scripts/tmp_promo_dedup_cleanup.py`（实施时） | 存量一次性清理 |

---

## 问题 3：七个 error 源修复

### 现状与探测结论（2026-09-09 从 VM 实测）

| 源 | 旧 URL | 实测状态 | 结论 |
|----|--------|---------|------|
| qwen web_page (id1) | help.aliyun.com/zh/model-studio/billing-overview | 200 但 `<title>404` SPA 壳 | **已迁移** |
| qwen web_page (id13) | aliyun.com/activity/intelligent | 404 | **已下线** |
| qwen announcement (id15) | help.aliyun.com/.../developer-reference/announcement | 200 但 404 壳 | **已迁移** |
| deepseek web_page (id9) | api.deepseek.com/pricing | **401 Authentication Fails**（84B） | 端点已转需鉴权 |
| deepseek announcement (id10) | api.deepseek.com/news | 401（81B） | 同上 |
| sensenova announcement (id20) | platform.sensenova.cn/news | 404（610B） | **已下线** |
| hunyuan announcement (id21) | cloud.tencent.com/announce/product/AI | 404（9.8KB 腾讯 404 页） | **已下线** |

> 注：deepseek 的 401 不是缺 key（content_guard 收缩的 182B "Authentication Fails" 页即此），
> 而是这两个 api.deepseek.com 端点本身已转为鉴权制；公开镜像已迁 api-docs.deepseek.com。

### 候选新 URL 探测结果（content_guard 实测健康）

| 候选 | 实测 | 判定 |
|------|------|------|
| `api-docs.deepseek.com/zh-cn/quick_start/pricing/` | 200, 21.5KB, 健康 | ✅ deepseek 计费页 |
| `api-docs.deepseek.com/zh-cn/news` | 200, 46KB, 健康 | ✅ deepseek 新闻页 |
| `help.aliyun.com/zh/model-studio/model-pricing` | 200, 1.25MB, 健康（旧 billing-overview 302 落点即此） | ✅ qwen 计费页 |
| `help.aliyun.com/zh/model-studio/release-notes` | 200, 38KB, 健康, 含业务词 | ✅ qwen 公告页 |
| `help.aliyun.com/zh/model-studio/models` | 200, 87KB, 健康 | ✅ qwen 模型清单页 |
| `console.sensecore.cn/help/docs/.../release-notes` | 200 但 1.1KB JS 壳 | ❌ 需渲染 |
| `platform.sensenova.cn/release-notes`、`sensenova.cn/news` | 404 | ❌ |
| `cloud.tencent.com/document/product/1729`（hunyuan 文档索引） | 200, 36KB, SSR 健康 | ⚠️ 备选（文档更新≠优惠） |

### 修复方案（分三组）

**A 组：改 URL 即恢复（qwen×3 + deepseek×2，共 5 源）**

1. 改源类默认 URL（代码层，`intelligence/sources/qwen.py` / `deepseek.py`）：
   - qwen id1 → `model-pricing`；qwen id15 → `release-notes`；
   - qwen id13（activity 优惠聚合页已死）→ **改挂 `models` 模型清单页**（web_page 型），
     顺带补上 qwen 无渠道、API model_list 走不了的模型发现缺口；
   - deepseek id9 → api-docs `quick_start/pricing`；id10 → api-docs `news`。
2. 改 `intel_sources` 表存量行的 url（dashboard 源管理 UI 或 SQL），并清零
   error_count / status 复位为 active（或依赖既有 error→active 自动恢复路径：健康内容
   抓回即复位并发恢复通知）。
3. 验收：部署后 2 个轮询周期内 5 源全部 active、last_changed_at 更新、无 error 增长；
   content_guard 无新拦截。

**B 组：需渲染或暂停（sensenova announcement id20）**

- sensecore 文档站是 console SPA（1.1KB 壳），plain fetch 无解；平台 news 页已 404。
- 方案 B1（优先尝试）：fetcher 已支持 Playwright（wait_until/wait_for），对
  `console.sensecore.cn/help/docs/model-as-a-service/nova/release-notes` 配 wait_for
  正文选择器探测一次；若渲染后能出内容则挂上（poll 降到日级）。
- 方案 B2（B1 失败）：源管理 UI 暂停该源（status 语义不变，停止无效轮询），
  等 sensenova 有真实公告诉求再挂公众号/RSS 等替代源。
- 注意：sensenova 是历史优惠第一大来源（43 条），B1 值得一试。

**C 组：暂停（hunyuan announcement id21，与用户 9/9 拍板一致）**

- 腾讯 announce 旧页 404，AI 产品公告无公开列表页；文档索引（SSR 健康）只能给
  "文档更新"型情报，与优惠情报目标错位。
- 方案：源管理 UI 暂停；等 hunyuan 有真实优惠活动页再建（与 qwen/hunyuan/moonshot
  不建渠道的决策同节奏）。

### 改动面（A 组实施时）

| 文件/对象 | 改动 |
|------|------|
| `intelligence/sources/qwen.py` | 2 个 URL 常量替换 + 1 个源改挂 models 页 |
| `intelligence/sources/deepseek.py` | 2 个 URL 常量替换 |
| `intel_sources` 存量行 | url 同步更新 + error 复位（SQL 或 UI，一次执行） |
| `tests/` | 源 URL 常量断言测试更新（如有） |

### 风险与注意

- 阿里云帮助站存在「200 + 404 壳」形态（product-updates/latest-release 即是），
  A 组上线后前两天需盯 content_guard 拦截日志，确认新 URL 稳定健康。
- api-docs.deepseek.com 是 docusaurus 站，结构稳定但正文在 HTML 里为 SSR 输出，
  css_selector 可能需要按新站调整（实施时抓一次 DOM 确认）。
- intel_sources.url 与源类默认 URL 两处要同步改，避免下次 ensure_seed 重建又回退旧值
  （需确认 seed 逻辑是 upsert 还是 skip-existing，若是 upsert 以代码为准即可）。

---

## 实施顺序建议

1. **A 组 5 源 URL 修复**（半天内：改常量 + 存量 SQL + 验证）— 收益最大、风险最低。
2. **问题 1 层 1 内容相似度去重**（半天：healer ~30 行 + 测试）+ 存量重复清理脚本。
3. **B1 sensenova 渲染探测**（1 小时试探，结论决定挂起或暂停）。
4. **C 组 hunyuan 暂停**（UI 操作，1 分钟）。
5. 层 2 伪变更抑制与 P1-1 differ clean：观察 1/2 上线一个完整周后按数据决定。
