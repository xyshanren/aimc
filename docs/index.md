# AIMC 文档导航

> AIMC（AI Model Compass）是一个轻量级 LLM 网关 + 优惠情报聚合工具。
> 本文档是**所有文档的入口**。按你的角色选择下面任一入口。

**项目状态**：**V6.4 完成（2026-09-12，全量 747 测试全绿）**。已落地：V6.0（模型组分级 vision-light + GLM 免费模型 + P0-3 收窄；标价双轨成本审计 + 4 个 cost API + 看板成本审计卡）、V6.1（鉴权统一 admin key、发 key REST、统一 CLI `scripts/aimc.py`、agent 契约 [agent-api.md](agent-api.md)、Mimosa 存量清零）、V6.x（qwen 命名规范化 qwen3-vl-*/qwen3-8b + 4 个存量 e2e 失败修复）。2026-09-05 看板补齐：卡片折叠统一 `<details>` + 全局时间范围 24h/7d/30d + fmtCNY + **指标体系统一（Cache 节省/标价节省/路由成功率 命名收敛、fmtTokens/fmtPct/fmtLatency、状态栏去业务指标、组路由跟随时间范围）**，见 [dashboard-metrics-plan.md](dashboard-metrics-plan.md)；**思考 token 计量上产（usage_logs.reasoning_tokens + 看板 4 处展示，注入模式进观察期）**，见 [thinking-control-plan.md](thinking-control-plan.md)。
**V6.3 自动化收口（2026-09-05）**：F1 首启检测（空渠道 WARN 引导，manual_import 降级可选）+ F2 请求日志 model 搜索后端（`?model=` 模糊过滤）+ F3 自动入组埋点（运维页「自动入组观察」卡，纯只读 5 组指标）+ F7 图-表双向 hover 联动（Cache 环图↔渠道表、柱图↔日表），见 [dev/automation-closure-plan.md](dev/automation-closure-plan.md) §6。
**V6.4 中期决策批次（2026-09-11，提前于 9/19）**：① **思考 Phase B 上产** — `channels.disable_thinking_inject` 渠道级注入开关（默认全关，开谁看数据定），见 [thinking-control-plan.md](thinking-control-plan.md) §3 决策记录；② **F3 收口 S3** — AUTO_JOIN_NON_T0=True + `AUTO_JOIN_MIN_CONFIDENCE=0.9`（非 T0 门槛，T0 恒免人工），见 [dev/automation-closure-plan.md](dev/automation-closure-plan.md) §5；③ **R9 不立项** — Phase 1-3 不做，8 死源运维禁用，见 [dev/next-step-plan.md](dev/next-step-plan.md) §十一；④ **#11/#12** — 路由有效输入价按渠道 7d cache 命中率折算（hit=0 退化原价）；⑤ 顺带修 `_update_cooldown` unawaited 告警协程。反馈公式 retry 项保留双分现状，观察 2~4 周后再取舍。
**V6.4 收尾（2026-09-12）**：⑥ **DeepSeek V4.1 迁移** — 官方 id 只剩 `deepseek-flash`（=V4.1-Flash，多模态，价格降 33%）+ `deepseek-v4-pro`；deepseek-flash T0 进 flagship/vision 组，旧名 deepseek-v4-flash 保留 strong/code（sensenova 0 元 + SF 仍供，registry 已恢复 active——deprecated=退出组路由实证），vision-exp 无渠道提供已下线，r1 系 T0 事实错误修正（→T2/T3）；⑦ **SF org 段命名修复** — org 与模型行名不同源时不再塞进 model_id，存量改名 paddleocr-vl-1.5 / longcat-2.0 / nex-n2-pro，8 行厂商品牌前缀保留（tongyi-mai/teleai/fnlp/funaudiollm/baai）；⑧ **全渠道思考注入兼容矩阵** — 实测无上游执行 enable_thinking:false（glm_bigmodel 400 强制思考、sensenova/siliconflow/minimax/deepseek 忽略），sensenova 开关注入观察中其余全关；⑨ **OCR 模型调用面确认** — deepseek-ocr/paddleocr-vl-1.5 无独立端点，走 chat 多模态（image_url 内容段），SF 拒不可下载图 URL（20040），base64 data URI 可用。全量 747 测试全绿。
**V6.3.1 瞬时冷却 badge 修复（2026-09-06）**：顶栏待处理 badge 不再为自愈型瞬时冷却（15-60s 自动恢复）报警（`/api/pending-summary?exclude_transient=true`），消除「顶栏有数但运维页为空」的观察时差；同时观察点落 `cooldown_events` 流水（同 episode 按 cooldown_until 去重），运维页新增「24h 瞬时冷却记录」卡留痕可回看。
**请求日志并显上游账号（2026-09-07）**：一渠道多账号落地后的用量可观测补齐——请求日志「渠道」列以 muted 后缀并显服务账号（`sensenova_official · sensenova_01`），详情抽屉 meta 同步显示；list/transcript 端点新增 `account_name` 字段（`usage_logs.account_id` 反查，legacy NULL 行显示 —），零迁移（dcab606）。
**V6.3.2 优惠情报 provider 规范化（2026-09-08）**：修复 AI 解析自由文本 provider（"Kimi (Moonshot AI)"/"SenseNova 商汤" 等 17 种变体）覆盖采集源规范值入库的根因——SYSTEM_PROMPT 移除 provider 字段 + parse_content 固定返回 source.provider；生产库 29 行存量脏数据一次性归一（06c63b3）。
**V6.3.3 请求日志渠道/Key 筛选（2026-09-09）**：用量页请求日志在状态/模型搜索之外新增渠道与 Key 两个下拉筛选（`/api/requests?channel_id=&key_id=` 精确过滤 + 回显）；Key 选项与列表同规则消歧（同名 label 跨用户追加 username），已吊销 key 保留可选（标注区分）以支持历史请求回查；选项为配置类数据走 PAGE_ROUTES once（进用量页刷新，不进 60s 轮询）。用户维度不加独立筛选——每个 key 唯一归属用户，按人下钻已有「按用户 tab（key 下钻）→ key 筛选」闭环。
**流式失败 400 落库 + CONTEXT_WINDOW 分类（2026-09-10）**：修复 deepseek-harness 压缩上下文后「400 (no body) CONTEXT_WINDOW_EXCEEDED」失败在 AIMC 侧完全不可观测的问题——(1) 流式响应 usage 缺失且 status≥400 时补写 usage_logs 行（tokens 0 + error_msg/error_type/error_code/needs_human），此前此类失败从不落库；(2) error_classifier 新增 `context_window` 分类（识别 deepseek `CONTEXT_WINDOW_EXCEEDED` / OpenAI `context_length_exceeded` 等，haystack=code+message 匹配，deepseek 把标记放 error.code 字段）；行为中性——不冷却、不探测、不 needs_human，为将来「上下文超限路由更大窗口渠道」留分类基础。生产实测：400 失败行落库（id 6651/6653/6654），CONTEXT_WINDOW 分类经单测覆盖（上游 400 空体无法实测触发）。
**流式 StreamConsumed 双重消费修复（2026-09-11，6069281）**：`_forward_stream` 在上游 status≥400 时先 `aread()` 读错误体（V4.9.1 起），随后 `stream_generator` 仍调 `aiter_raw()` 抛 `httpx.StreamConsumed`（生产 9/5~9/9 共 326 例）——客户端只收到错误 status 而 body 为空，tail/full 缓冲也为空导致 usage 行错误体丢失。修复：检测 `response.is_stream_consumed` 时 generator 改为直接 yield 已缓冲字节，tail_buffer/full_buffer 语义与正常路径一致（错误体重新进入 usage_logs 与转录）。回归测试 `tests/test_v6x_stream_consumed.py`（429 错误体完整送达 + 200 正常路径不受影响）。
**/v1/embeddings + /v1/rerank 直通端点（2026-09-11，de92643）**：网关新增 embedding/rerank 直通（siliconflow 免费规范名 baai-bge-m3 / baai-bge-reranker-v2-m3，1024 维，双端一致无需重灌），usage_logs 照常计量；配套 siliconflow 价格两轮校准 55 行上产（d7b5e92，75 priced / 22 有意 NULL=生图/语音/LoRA 按张按秒计费）+ raw_ids 已批准存量回填 79 行（纯运维映射，gateway 无代码消费）。
**周报反馈分四项修复（2026-09-11，55956e9）**：重试判定收窄为同账号+前次失败+间隔≤60s（旧 5 分钟相邻启发式误判 85.8% 正常流量为重试）；延迟扣分改分档（2s/5s/10s/30s→0/5/10/15/20 分，思考类模型豁免）；双分数并存（新列 feedback_score_no_retry 完全去重试项，与主分并行对比，取舍观察 2~4 周待拍板）；周报 MIN_SAMPLES=20（样本不足模型移出排名进注记行，Top 表新增 p95 延迟列）。
**channels 页价格表轮询改 once（2026-09-11，1a111e6）**：渠道页价格表属配置类数据，进页刷新一次即可（进页即新），不再进 60s 轮询——与 V6.3.3 Key 选项同一原则。

版本路线见 [next-major-v6-plan.md](next-major-v6-plan.md)（V6.0/V6.1/V6.x 已完成）；**后续规划见 [dev/automation-closure-plan.md](dev/automation-closure-plan.md)（F3 埋点 ≥2 周观察 → AUTO_JOIN_NON_T0 数据驱动决策（S1-S4）→ V7.0 含看板自定义采集源；V7.0/V6.2 待拍板）**。

---

## 🚀 我是新用户 / 想部署 AIMC

| 我想... | 看这个 |
|--------|--------|
| 5 分钟快速跑起来 | [user/install.md](user/install.md) |
| 接入 API Key / 配置环境 | [user/configure.md](user/configure.md) |
| 给团队成员发多用户 Key（V4.8） | [user/configure.md §4](user/configure.md) |
| 用 OpenAI SDK / curl 调用 | [user/usage.md](user/usage.md) |
| 用 Dashboard 调参 / 看数据 | [user/dashboard.md](user/dashboard.md) |
| 看周报 / shadow log / 报警 | [user/operations.md](user/operations.md) |
| 出错 / 报错 / 排查 | [user/troubleshooting.md](user/troubleshooting.md) |
| 常见问题 | [user/faq.md](user/faq.md) |

---

## �️ 我是开发者 / 想改代码

| 我想... | 看这个 |
|--------|--------|
| 了解架构 / 数据流 | [dev/architecture.md](dev/architecture.md) |
| 看 API 端点（OpenAI + Dashboard）| [dev/api.md](dev/api.md) |
| **Agent 编程接入（REST/CLI 契约, V6.1）** | [agent-api.md](agent-api.md) |
| 本地搭建开发环境 | [dev/setup.md](dev/setup.md) |
| commit / 分支 / 测试 规范 | [dev/conventions.md](dev/conventions.md) |
| 写测试 / 提 PR / 贡献代码 | [dev/contributing.md](dev/contributing.md) |
| 跑 694 个测试 + 加新测试 | [dev/testing.md](dev/testing.md) |
| 查 post-merge 修复（21 commit 整改）| [dev/post-merge-fixes.md](dev/post-merge-fixes.md) |

---

## 🔧 我是运维 / 部署 / 监控

| 我想... | 看这个 |
|--------|--------|
| 部署 / systemd / LXC / 周报 timer | [ops/deploy.md](ops/deploy.md) |
| 数据新鲜度 / shadow log / 限额监控 | [ops/monitor.md](ops/monitor.md) |
| 跑 22 个 migration 升级数据库 | [ops/migration.md](ops/migration.md) |
| 出错回滚（自动 tier 调整等）| [ops/rollback.md](ops/rollback.md) |
| **公网部署安全（VPN / Cloudflare Tunnel / nginx）** | **[ops/security.md](ops/security.md)** |
| **应急响应（master_key 泄露 / API key 泄露 / SSH 入侵）** | **[ops/incidents.md](ops/incidents.md)** |

---

## 📚 历史归档（只读，新内容见上面）

- [archive/agent-integration.md](archive/agent-integration.md) — Agent 集成历史（早期）
- [archive/competitive-research.md](archive/competitive-research.md) — 竞品调研
- [archive/phase2-design.md](archive/phase2-design.md) — Phase 2 模型组设计历史
- [archive/v44-feedback-and-alert-design.md](archive/v44-feedback-and-alert-design.md) — V4.4 设计文档
- [archive/review-phase3-4-phase4-e1.md](archive/review-phase3-4-phase4-e1.md) — Phase 3-4 评审
- [archive/fix-plan-phase3-4-phase4-e1.md](archive/fix-plan-phase3-4-phase4-e1.md) — Phase 3-4 fix plan
- [archive/e2e-results.md](archive/e2e-results.md) — 早期集成测试结果
- [archive/model-data-source.md](archive/model-data-source.md) — 模型数据源设计
- [archive/crawl-improvements.md](archive/crawl-improvements.md) — 采集改进
- [archive/hermes-agent-cn-integration-pr.md](archive/hermes-agent-cn-integration-pr.md) — Hermes Agent 集成 PR
- [dev/p0p1-fixes.md](dev/p0p1-fixes.md) — P0/P1 早期修复（历史）

---

## 文档元信息

- **最后更新**：2026-09-07
- **测试覆盖**：747 tests（pytest，~6 分钟跑通，2026-09-12 基线全绿）
- **观察期关注**：缓存命中率（路径 A）、周报 timer 首次自动触发（周一）、vision 400 率、**思考注入各渠道开谁（Phase B 已上产默认全关，见 [thinking-control-plan.md](thinking-control-plan.md) §3）**、**S3 生效后 pending 变化与门槛拦截量（AUTO_JOIN_MIN_CONFIDENCE=0.9，见 [dev/automation-closure-plan.md](dev/automation-closure-plan.md) §5）**、**反馈公式 retry 项取舍（双分并存，观察 2~4 周 ~2026-09-23~10-07）**
- **V4.9 新功能**：Dashboard **🔴 待处理 (N)** tab（余额不足 / Key 失效 / 智能探活状态），见 [user/dashboard.md](user/dashboard.md)
- **观察期后待办**：操作审计系统（内网自用继续观察，公网/多用户前必修；见 [dev/automation-closure-plan.md](dev/automation-closure-plan.md) §2.4）
