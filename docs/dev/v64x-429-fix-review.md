# 429 隐形拒绝修复批次·上产后审核（2026-09-14）

> 审核人：ZCode 审核会话（非实施方；实施由并行会话完成并于当日上产）
> 对象：`gateway/{cooldown,proxy,router,routes}.py`（+174/-21）+ `tests/test_v64x_cooldown_quota_failover.py`（12 例）+ `tests/test_v46_cooldown.py` 适配
> 方法：对照商定方案逐项核对 diff → 关键行为用最小脚本实证 → 全量测试两次
> 关联：[next-stage-research.md](next-stage-research.md)「⚡ 已上产：429 隐形拒绝修复」、
> [production-review-2026-09-13.md](production-review-2026-09-13.md) S2 修正
> **本文独立成文原因**：实施会话正在并行编辑规划文档，为避免编辑冲突单独归档；定稿后可由维护者择机回链。

---

## 0. 审核结论

**方案①②③的实现与商定一致，方向正确，已确认的部分质量良好**；但发现 **1 个高优先级问题**：
指数退避的升级计数（count）会被冷却过期时的既有 pop 行为清零——**现实主场景「到期→重撞」下退避
失效，冷却恒 30s**（已用最小脚本实证）。等效于「渠道级同步 + mark_success 保留」在撑场面，
退避升级本身没生效。建议尽快跟进修复（约 0.5 天，含测试）。

---

## 1. 已确认的部分

| 项 | 结论 |
|----|------|
| 方案① 预检语义 | ✅ 预检不过 → mark_failure(QUOTA_EXHAUSTED, needs_human=True, 冷却 3600s) + failed_channels + usage_logs 落库 + `attempt < max_retries` 时 continue 重选；最终失败返回 429 且注入失败链头。`DEFAULT_FAILURE_THRESHOLD=1` 使**首败即冷却**，failover 一次生效，无重复选中浪费 |
| 方案② 删 tpm 行 | 数据操作（account_quotas id=8），代码无涉 |
| 方案③ 退避/保留/同步 | ✅ `cooldown_secs × 2^(count-1)` 封顶 3600s，序列 30→60→120→…正确；mark_success 冷却内只记 last_success_at、保留 count 与 cooldown_until，冷却外（含已过期）全量复位；兄弟对齐只对 rate_limited 生效（条件 = 兄弟 `last_error_type==rate_limited` 且其 cooldown_until 更晚），quota/auth 冷却不传染，语义自洽 |
| S5 顺带修复 | ✅ routes 重试路径的二次 mark_failure 已删（消除失败计数翻倍 / needs_human 提前触发）——V6.5 批次一相应缩小 |
| 测试 | ✅ 新增 12 例覆盖退避/封顶/冷却内成功保留/过期复位/兄弟同步/预检 failover；相关 50 例过；复跑全量 **762 passed 全绿**（实施中间态曾观测 3 failed，随后实施会话补了 test_v46_cooldown.py 适配，非最终状态问题） |
| 其他 | `_write_usage_log` 签名匹配（await 直调，SQLite 单行写可接受）；预检拒绝路径构造的 429 响应不带旧 headers 复制，**S1 不受影响**（仍仅在上游失败链 `routes.py:274-289`）；proxy 对所有错误类别传 siblings 但 cooldown 内部按类别过滤，无误同步 |

---

## 2. 待处理问题

### 2.1【高·已实证】指数退避被「过期 pop」架空 —— ✅ 已修复（V6.5 批次一，2026-09-14）

**机制**：`is_in_cooldown()` / `cooldown_remaining()` 在冷却过期时 `self._state.pop(account_id)`
（cooldown.py:92/103，V4.6.1 既有行为——旧实现冷却固定时长，pop 无害）。本批次让 `count` 承载
退避语义后，pop 把 count 一起清掉。

**现实路径**（429 乒乓的主场景）：

```
429 → mark_failure(count=1, 冷却30s) → 到期 → router._is_channel_usable 查 is_in_cooldown
    → 过期 pop(count 丢失) → 该账号被选中 → 429 → mark_failure(新建 state, count=1, 又是 30s) → …
```

**实证**（独立 CooldownManager 实例最小脚本）：

```
场景A 连续 mark_failure 不经查询:  count 1/2/3 → 冷却 30/60/120s ✓ (新测试的用法)
场景B 每轮经 is_in_cooldown 查询:  4 轮全部 count=1 → 冷却恒 30s ✗
```

新测试没有覆盖「过期 → 查询 → 重撞」路径，所以全绿通过但退避在生产流量下不生效。

**修复记录（V6.5 批次一）**：`is_in_cooldown`/`cooldown_remaining` 过期分支改为置
`cooldown_until=None` + 记 `cooldown_expired_at`（不 pop state，count 存活）；`cleanup()` 加
600s 保留窗（过期未超窗保留，超窗物理清理）；`mark_failure` 对超保留窗的陈年 count 归零
（视作新系列，防偶发失败直接吃满退避上限）；顺带理顺 §2.5——`needs_human` 不再被过期查询
顺带洗白（洗白走 ack_recover / mark_success / cleanup 保留语义一致）。
测试 `tests/test_v65_cooldown_expiry_retention.py` 8 例，含端到端乒乓升级序列
（30→60→120s），全量 779 全绿。

**建议修法**（约 0.5 天含测试）：
- 过期时只视为「不在冷却」，**不 pop state**：`is_in_cooldown`/`cooldown_remaining` 过期分支改
  `s["cooldown_until"] = None`（或仅按时间比较返回 False）；
- 物理清理交给 `cleanup()`，并加保留窗（如过期 10 分钟内不清，给 count 延续机会）；
- `mark_failure` / `mark_success` 无需改动（state 在，count 自然延续；mark_success 的
  `now >= cu` 分支已兼容 cooldown_until=None）；
- 补测试：模拟「失败 → 时间推进过期 → is_in_cooldown 查询 → 再失败」，断言第二次冷却 > 首次。

### 2.2【低】兄弟对齐是启发式

条件是兄弟 `last_error_type==rate_limited`，对共享池渠道（sensenova）正确；独立池多账号渠道
（如 deepseek/minimax 同渠道多账号）会把健康兄弟的冷却对齐到饱和兄弟——轻度过度保守，代价秒级。
可接受；观察期留意独立池渠道的可用性数据，必要时给 accounts 加「共享池」标记精确化。

### 2.3【低】退避封顶借用探活常量

封顶用 `max_probe_backoff_seconds`（探活退避上限 3600s）——两个语义共用一个常量，将来调探活
参数会误改冷却封顶。建议独立 `MAX_RATE_LIMIT_COOLDOWN_SECONDS`。

### 2.4【低】预检拒绝落库的 model 口径

预检拒绝的 usage_logs.model 用请求原始名（`tier:flagship` 等会原样进 Top 模型统计）；流式失败
路径用 `_resolve_actual_model`。建议统一（预检拒绝发生在转发前、无 actual model，可落请求名但
在 group_id 或 error_type 维度可辨识即可，或在看板过滤 tier: 前缀）。

### 2.5【低·既有】needs_human 清理语义不一致

`is_in_cooldown` 的过期 pop 会连 `needs_human=True` 一起清（3600s 后首个查询即洗白）；
`cleanup()` 则永远保留 needs_human。两处矛盾。修 2.1 时一并理顺：needs_human 的洗白应走
ack_recover / mark_success，过期 pop 不应顺带清除。

---

## 3. 对下一阶段规划的影响

| 影响 | 内容 |
|------|------|
| V6.5 批次一缩小 | **S5 移除**（本批次已完成）；批次一剩余 = S1（Content-Length）/ S6（周报转义）/ B1（探活 NameError）/ G（部署路径） |
| 新增跟进项 | **2.1 退避架空修复**建议插队进批次一（否则指数退避「上线了但等效固定 30s」，本次生产问题的升级诉求落空一半） |
| 观察线（本批次上产后） | ① 冷却时长分布——验证退避在真实流量下生效（若恒 30s 即 2.1 未修）；② 渠道级同步对独立池多账号渠道的误伤面；③ 预检拒绝的 usage_logs 可见性（看板应出现 quota_exhausted 行） |
| 时序 | V6.5 仍等本批次收口（2.1 修复 + VM 全量全绿）后启动——S5 冲突已消，但 2.1 同样改 `is_in_cooldown`/`cleanup`，避免并行 |

---

*审核快照：2026-09-14。实施会话仍在并行迭代，行号以审核时工作树为准。*
