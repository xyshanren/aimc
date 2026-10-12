# intel_auto 出组机制与护栏（2026-10-12）

> 案例驱动：tier:strong 组的 deepseek-v4-flash 于 2026-09-28 被 intel 自动出组，
> 但 sensenova/taotoken 仍 0 元供应该模型。本文梳理机制全貌、本次护栏设计与
> Playwright 促销采集下线决策，供后续维护者理解"intel 到底动了什么"。

## 1. 出组机制（`intelligence/model_sync.py`）

intel 采集 → AI 解析产出 `ModelIntelItem` → `process_model_item()` 分级处理：

| 条件 | 动作 |
|---|---|
| `status="deprecated"` 且 `confidence >= 0.9`（`LEAVE_THRESHOLD`） | **出组分支**（见 §2 护栏） |
| `confidence >= 0.8`（`JOIN_THRESHOLD`，非 T0 还需 ≥0.9） | upsert registry + 自动入对应 tier 组 |
| `0.6 <= confidence < 0.8/0.9` | 写 pending capability log，等 dashboard 人工确认 |
| `confidence < 0.6` | 写 log 标黄，不入 registry |

出组分支的原始全自动路径（护栏放行后）：

1. `registry.status` → `deprecated`（写 `model_capabilities_log`，approved=1 自动生效）
2. `crud.remove_model_group_members_by_model()` **一步软删该模型在所有组的
   member 行**（`is_active=0`，`removed_by='intel_auto'`，
   `removed_reason='deprecated+conf>=0.9'`），幂等（只动 is_active=1 的行）

关键语义：**registry status 是组路由的硬闸**（路由缓存只拉 `status='active'`），
member 软删是第二道保险。两者任一生效，模型即退出组路由。

## 2. 护栏（2026-10-12 上产）

**问题**（v4-flash 案例，2026-09-28 01:12:56）：DeepSeek 官方 9/12 弃用旧名
`deepseek-v4-flash`（替代 `deepseek-flash`），registry 标 deprecated；intel 同步
9/28 判定 deprecated+conf≥0.9 自动软删其 tier:strong(id=163)/tier:code(id=179)
组员行。但 **sensenova_official / taotoken_coding 的旧名价行（0 元）仍在、上游仍
服务** —— 一个还能用的免费模型被静默踢出组路由，且 git/seed/文档零痕迹
（软删只发生在生产 DB），两周后才在健康体检中定位。

**护栏规则**：`should_auto_leave` 成立时先查 `crud.has_available_channel()`
（active 渠道 × 该模型价行）：

- **渠道仍供应** → 不自动出组（registry 不动、组员不动），改为：
  - 写 pending capability log（`log_capability_change` 自带 upsert 幂等，approved=0）
  - 建 `model_change_suggestions(change_type='remove_from_group', status='pending')`
    （幂等：同模型已有 pending 不重复建），进 dashboard「建议中心」人工确认
  - 返回 `action='pending_review'`
- **渠道无供应** → 走原全自动路径（真弃用的模型没有保留价值）

辅助函数：`_guardrail_active_groups()`（查活跃组，join 用 TEXT group_id 两边同为
组名）、`_ensure_remove_suggestion()`。测试：`tests/test_intel_auto_leave_guardrail.py`
（真实 in-memory sqlite + 真 crud）。

**v4-flash 本身的处置**（用户拍板"接受现状"）：不恢复组员行，
`51756a0` 同步 seed（`scripts/seed_model_groups.py` strong/code 两行移除留档）
+ `docs/user/usage.md` 组成员表对齐生产。

## 3. Playwright 促销采集下线（2026-10-12）

**背景**：A-2 验收死线（~2026-10-06"产出 ≥1 条可路由优惠"）已过，
`price_snapshots.is_promotion=1` 仍为 0 行——"情报→价行"转化闭环从未接通；
而 Playwright chromium 常驻 ~100MB（机器 1.8G 内存本就偏紧）、openrouter/lmsys
持续 30s goto 超时。促销链保留成本 > 收益。

**切口**：`_run_source_task` 开头查 `intel_sources.status=='disabled'` 直接 return
（`scheduler.py`，在 API/HTML 分支之前），故按行禁用即断流，无需改代码。

生产 DB 实际执行（26 行 → disabled）：

| 类型 | 行 id |
|---|---|
| announcement（8） | 10, 14, 21, 25, 26, 22, 15, 19 |
| benchmark（2） | 44, 34 |
| model_list 纯 Playwright：sensenova_docs（1） | 43 |
| price_aggregator（9） | 11, 42, 16, 23, 24, 31, 18, 30, 17 |
| web_page（4） | 9, 12, 1, 13 |
| error 反复超时（2）：lmsys / openrouter | 33, 27 |

**保留 active（9 行，全是 model_list httpx）**：id 2/3/4/5/6/7/8（url="" 纯 API，
registry 轻量同步主力）+ id 28 siliconflow / 32 volcengine（API 主路径 httpx，
HTML 仅 API 失败降级兜底，罕见触发）。

**效果**：chromium 是惰性启动（`fetcher.py` `_get_browser` 双重检查锁），全
Playwright 源禁用后不再被拉起；重启后常驻 chromium 进程清零，可用内存
538Mi → 796Mi。禁用经 dashboard 运维页可随时恢复（单源 toggle）。

**连带行为变化**：

- 盲区审计（A-2, 6h）只看 active 的 HTML 型源，禁用后被排除；在采源 <3 静默
  跳过，**不会误报失明**。
- 周报「📡 情报采集健康」章节的在采源列表变短（仅剩 9 个 httpx model_list）。
- `monthly_official_audit`（每月 1 日）不走 intel_sources 表，仍会拉起 chromium
  一次/月（一致性审计，保留）。
- intel_auto 出组护栏的判定不依赖 Playwright 源（读 registry/价行），不受影响。

## 4. 已知边界与遗留

- **`scripts/apply_suggestions.py` remove_from_group 的 join 坑**：`apply_model_suggestion`
  用 `ModelGroupMember.group_id == g.id`（TEXT vs 整型）比较，永不命中 → 批准后
  apply 实际 SKIP。护栏的 suggestion 以"通知+人工在 dashboard 组管理操作"为预期
  动作，apply 链路待修（独立小任务）。
- 生产 DB 是组成员唯一真相，seed/文档会漂移——排查组成员问题直接查
  `model_group_members`（`group_id` 是 TEXT 组名，别 join `model_groups.id`）。
- moonshot model_list 曾出现"解析失败保留旧 hash 下轮重试"（AI 解析腿瞬时失败，
  非致命，下轮自愈）。
