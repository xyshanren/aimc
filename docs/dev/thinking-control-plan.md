# 思考控制方案（观察期决策记录）

> 受众：评估"是否要对思考型模型做服务端关思考/注入"的人；以及观察期结束后的决策执行者。
> 状态：**Phase B 已上产（2026-09-11）**。Phase A（看板暴露 + token 计量）+ Phase B（渠道级注入开关，默认全关）均已上产；各渠道开谁由用户看数据决定。

## 1. 背景与结论摘要

调查结论（2026-09-04）：

- AIMC **已有请求侧关思考能力**（V4.6.4 引入，`gateway/proxy.py::_strip_thinking_fields`）：
  渠道开关 `disable_thinking_pass_through`（默认开）会把请求 body 顶层的
  `thinking` / `enable_thinking` / `reasoning_effort` / `extended_thinking` 四个字段剥离后再转发上游。
  该能力只影响"客户端显式传参"的场景，**不能关掉模型自带的思考行为**（agent 不传参时上游仍会思考）。
- 当时**没有思考 token 计量**：`usage_logs` 无 reasoning 字段，无法评估思考开销，
  也就没有数据支撑"要不要做注入模式"（即在请求里主动注入 `enable_thinking=false` 类参数）。

**决策**：先上 Phase A（计量 + 看板暴露），观察真实数据后再决定是否做 Phase B（注入模式）。

## 2. Phase A：已落地内容（2026-09-05）

### 2.1 计量链路

1. **解析**：`gateway/proxy.py::_parse_reasoning_tokens` — 严格解析 OpenAI 风格
   `usage.completion_tokens_details.reasoning_tokens`（kimi/glm/deepseek 思考型模型按此风格返回；
   Anthropic 风格无此字段）。非 int 一律返回 None。
2. **落库**：非流式（`_schedule_usage_log`）与流式（`_schedule_stream_usage_log`）两条路径均传入
   `_write_usage_log`，写入 `usage_logs.reasoning_tokens`（`db/migrations/2026_09_05_v6x_reasoning_tokens.py`）。
   **NULL = 上游未返回**（区别于 0 = 上游返回了但为 0）；SQL 聚合用 `func.coalesce(..., 0)` 容忍 NULL 行。
3. **测试**：`tests/test_thinking_strip.py::TestParseReasoningTokens`（8 例：OpenAI 风格/0/无字段/None/
   空 dict/非 int/字符串拒绝/非 dict usage）。

### 2.2 看板暴露（4 个面）

| 位置 | 展示 |
|------|------|
| 用量页每日表 | "思考"列（每日 reasoning_tokens 合计，NULL 行不参与 SUM） |
| 请求日志 Token 列 | 🧠 徽标（单请求 reasoning_tokens）+ 详情抽屉 |
| 成本审计·按用户 tab | "思考"列（每人合计） |
| 成本审计·Key 下钻 | 🧠 徽标（每 key 合计） |

渠道页另有既有"思考剥离"开关列（`disable_thinking_pass_through` 0/1 可切换，
关态用 `.btn-ghost` 幽灵按钮样式）。

### 2.3 局限（观察期须知）

- **覆盖面**：只有返回 `completion_tokens_details.reasoning_tokens` 的上游才有数据；
  Anthropic 风格渠道（如 claude）永远 NULL。按渠道看"思考"列时，NULL 多的渠道不代表不思考。
- **不可关**：计量改变不了模型行为。agent 未传参时上游仍按默认思考，这笔开销照记。
- **流式**：依赖上游在 stream 末尾返回 usage 块；个别渠道流式不回 usage 时整条记录 token 均缺失，非本功能问题。

## 3. 观察期与 Phase B 触发条件（决策记录）

**观察窗口**：自上产日起 ≥2 周（覆盖完整周周期），在看板用时间范围 7d/30d 观察。

**触发 Phase B（注入模式）的条件**（满足任一即立项）：

1. **占比持续超标**：在思考型模型（kimi-k3 / glm-5.x / deepseek 等）的流量里，
   `sum(reasoning_tokens) / sum(output_tokens)` 持续（连续两个自然周）> 30%，
   且这些请求的 output 本身并无长答案诉求（即思考在吃钱而非产出）。
2. **延迟拖慢 agent loop**：思考型模型的单请求延迟 P50/P95 显著高于同 tier 非思考模型
   （看板延迟数据），且用户/agent 侧出现等待投诉或超时重试增多。

**不触发则维持现状**：请求侧剥离（客户端显式传参场景）+ 计量即可，不做注入。

### 决策记录（2026-09-11，提前于 9/19 批次，用户拍板）

**触发条件成立，Phase B 立项上产**：

- 条件 1 成立：思考/输出比 W35（8/24~8/30）47.5% + W36（8/31~9/6）48.8%，连续两个完整自然周 >30%。
- 条件 2 方向成立：kimi-k3 131.5s / glm-5.3 31.9s / dsv4-pro 21.4s，显著高于 minimax-m3 7.2s。
- 数据（7d 分渠道，上产当日采集，供逐渠道开关决策）：

| 渠道 | 请求量 | 计量填充率 | 思考/输出比 | 均延迟 |
|------|--------|-----------|------------|--------|
| deepseek_official | 1398 | 95.9% | 43.1% | 5.4s |
| sensenova_official | 1179 | 83.8% | 59.1% | 20.9s |
| glm_bigmodel | 774 | 60.1% | 43.5% | 26.3s |
| minimax_compat | 708 | 73.3% | 47.9% | 7.2s |
| siliconflow_aggregator | 26 | 0% | — | — |

- 分模型 7d：dsv4-pro 59.0%（21.4s）、6.8-flash-lite 53.1%、glm-5.3-flash 53.6%、minimax-m3 47.9%、dsv4-flash 46.0%、kimi-k3 80.5%（131.5s，n=6）。

### Phase B 已落地内容（2026-09-11）

- `channels.disable_thinking_inject` 列（迁移 `db/migrations/2026_09_11_v6x_thinking_inject.py`，幂等，默认 0）。
- `gateway/proxy.py::_inject_thinking_off`：与 `_strip_thinking_fields` 同位置注入 `enable_thinking: false`（OpenAI 风格）；**客户端显式传 enable_thinking 不覆盖**；上游不认参数靠既有 drop_params/auto_drop_params 防 400。
- 渠道页"思考注入"开关列（与"思考剥离"并排）；channels GET/PUT 暴露。
- 测试 `tests/test_v64_decision_batch.py`（注入 5 例 + S3 门槛 9 例 + cache 折算 8 例）。
- **各渠道开关默认全关**，生产冒烟确认 6 渠道 inject=0 行为不变；开谁由用户看上表数据决定（建议先开 sensenova/glm_bigmodel 这类"思考比高 + 延迟高"渠道，逐渠道观察 reasoning_tokens 下降量）。

### Phase B 上游兼容矩阵与开关现状（2026-09-12 实测拍板）

全渠道对照实测（开关注入 vs 关，比对 reasoning_tokens）：**当前没有任何上游执行 `enable_thinking:false`**——

| 渠道 | 结论 |
|------|------|
| glm_bigmodel | **400 拒绝**（错误码 1210"该模型始终思考，不支持关闭思考；请使用 low、high 或 max"）→ 开关已关回 |
| sensenova_official | 忽略（6.8-lite / kimi-k3 照跑） |
| siliconflow | 忽略（GLM-5.3 三轮对照无差异） |
| minimax_compat | 忽略（41 vs 40） |
| deepseek_official | 忽略（deepseek-flash 三轮对照无缩减；注意须以实际路由到的渠道为准测） |
| stepfun_official | 不可观测（上游不返回 reasoning_tokens；渠道已禁用） |

**开关现状（用户拍板）**：sensenova_official=1（无害观察），其余全 0。GLM low/high/max 档位降思考列为扩展项（设计要点：参数形态实测 + 暴露方式二选一——渠道级配置 or agent 可传参，后者需动剥离白名单否则客户端 thinking 参数先被剥离）。`gateway/router.py` 渠道缓存 dict 须携带 `disable_thinking_inject`（漏拷会导致注入静默不生效，c5363bf）。

### Phase B 预案（届时再细化，此处只记方向）

- 注入点：`gateway/proxy.py` 与 `_strip_thinking_fields` 同位置，按渠道/模型粒度
  注入 `enable_thinking: false`（OpenAI 风格）或映射到各上游等价参数。
- 风险：部分上游对未知参数报 400（已有 drop_params/auto_drop_params 机制可复用）；
  强关思考可能降低回答质量，应做成渠道级开关（默认关）而非全局。
- 成本预估：实现 ~1 天（proxy 注入 + 开关 + 测试），复用本计量链路验证效果。

## 4. 相关文件

- `gateway/proxy.py` — `_strip_thinking_fields`（V4.6.4 请求侧剥离）、`_parse_reasoning_tokens`（V6.x 计量）、`_inject_thinking_off`（Phase B 注入）
- `gateway/router.py` — 渠道缓存 dict 携带 `disable_thinking_inject`（漏拷=注入静默失效）；#11/#12 cache-aware 有效输入价
- `db/migrations/2026_09_05_v6x_reasoning_tokens.py` — `usage_logs.reasoning_tokens` 列
- `db/migrations/2026_09_11_v6x_thinking_inject.py` — `channels.disable_thinking_inject` 列
- `db/models.py` — `UsageLog.reasoning_tokens`、`Channel.disable_thinking_inject`
- `dashboard/routes.py` — usage/summary、requests、stats/per-user、channels GET/PUT
- `dashboard/static/app.js` — 用量表思考列、请求日志 🧠、成本审计思考列、渠道剥离/注入开关
- `tests/test_thinking_strip.py` — 剥离 + 解析测试；`tests/test_v64_decision_batch.py` — 注入 + S3 门槛 + cache 折算
