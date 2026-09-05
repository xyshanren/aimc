# 思考控制方案（观察期决策记录）

> 受众：评估"是否要对思考型模型做服务端关思考/注入"的人；以及观察期结束后的决策执行者。
> 状态：**观察期**（2026-09-05 起）。Phase A（看板暴露 + token 计量）已上产，注入模式未实现。

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

### Phase B 预案（届时再细化，此处只记方向）

- 注入点：`gateway/proxy.py` 与 `_strip_thinking_fields` 同位置，按渠道/模型粒度
  注入 `enable_thinking: false`（OpenAI 风格）或映射到各上游等价参数。
- 风险：部分上游对未知参数报 400（已有 drop_params/auto_drop_params 机制可复用）；
  强关思考可能降低回答质量，应做成渠道级开关（默认关）而非全局。
- 成本预估：实现 ~1 天（proxy 注入 + 开关 + 测试），复用本计量链路验证效果。

## 4. 相关文件

- `gateway/proxy.py` — `_strip_thinking_fields`（V4.6.4 请求侧剥离）、`_parse_reasoning_tokens`（V6.x 计量）
- `db/migrations/2026_09_05_v6x_reasoning_tokens.py` — `usage_logs.reasoning_tokens` 列
- `db/models.py` — `UsageLog.reasoning_tokens`
- `dashboard/routes.py` — usage/summary、requests、stats/per-user、channels GET/PUT
- `dashboard/static/app.js` — 用量表思考列、请求日志 🧠、成本审计思考列、渠道剥离开关
- `tests/test_thinking_strip.py` — 剥离 + 解析测试
