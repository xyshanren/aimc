# AIMC 文档导航

> AIMC（AI Model Compass）是一个轻量级 LLM 网关 + 优惠情报聚合工具。
> 本文档是**所有文档的入口**。按你的角色选择下面任一入口。

**看板用量页 + 成本页整体设计（2026-09-23，V6.12 + V6.13，全量 912 测试全绿，aliy-hy 上产）**：**用量页**补用户/Key 维度（新增 `/api/usage/by-user`、`/api/usage/user-trend`，`/api/usage/perf` dim 扩 user/key，请求日志加按用户筛选；两张新卡：用户与 Key 用量 + 用户用量趋势）；**成本页**做整体重设计——补缺失的**按渠道**维度（`/api/usage/cost-by-channel`）+ **模型→渠道下钻**（`/api/usage/cost-by-model-channels`，回答"同一模型在哪买最便宜"）、KPI 加**平均单价**与**环比**（`/api/usage/cost-compare`，复用既有 `.kpi-trend-*` CSS）、标价覆盖率降级为徽章、修「思考」死列；**Cache 节省改名为「Cache 收益」并精确化**——旧算法用 model 级价表跨渠道外推，把按次计费/免费渠道的命中 token 按别的付费渠道单价计价，生产 7d 由 ¥1644.75 修正为 ¥25 量级（taotoken_coding 占旧值 89%）；新算法与 `gateway/proxy.py _estimate_cost` 计费公式互为镜像（逐「渠道×模型」取自身缓存价，无缓存价则计 0），并新增「可算价覆盖」提示。详见 [dashboard-phase3-design.md](dashboard-phase3-design.md) §3.5/§3.6。

**项目状态**：**V6.7 流式 failover + 限流分类兜底（2026-09-17，全量 859 测试全绿）**。前一里程碑：V6.6.x 账号配额真实测量（metering 重设计）+ 旗舰修复批（2026-09-16，850 绿）；sensenova 体量准入（上+下界）+ TPM/RPM 冷却基数（2026-09-16）；V6.5 稳定性批次完成（2026-09-14，802 测试全绿，本地 + VM 双基线）。已落地：V6.0（模型组分级 vision-light + GLM 免费模型 + P0-3 收窄；标价双轨成本审计 + 4 个 cost API + 看板成本审计卡）、V6.1（鉴权统一 admin key、发 key REST、统一 CLI `scripts/aimc.py`、agent 契约 [agent-api.md](agent-api.md)、Mimosa 存量清零）、V6.x（qwen 命名规范化 qwen3-vl-*/qwen3-8b + 4 个存量 e2e 失败修复）。2026-09-05 看板补齐：卡片折叠统一 `<details>` + 全局时间范围 24h/7d/30d + fmtCNY + **指标体系统一（Cache 节省/标价节省/路由成功率 命名收敛、fmtTokens/fmtPct/fmtLatency、状态栏去业务指标、组路由跟随时间范围）**，见 [dashboard-metrics-plan.md](dashboard-metrics-plan.md)；**思考 token 计量上产（usage_logs.reasoning_tokens + 看板 4 处展示，注入模式进观察期）**，见 [thinking-control-plan.md](thinking-control-plan.md)。

**V6.7 流式 failover + 限流分类兜底（2026-09-17，生产双 429 报障当日落地）**：① **流式请求参与 failover**——routes.py failover 分支旧 `not is_stream` 门使流式请求（ZCode 全流量）零 failover，上游 429/5xx 直接打穿到客户端（生产实证：modelscope 429 后从未尝试组内其余候选即返回）；`_forward_stream` 对 status>=400 改为**错误体缓冲普通 Response 直返**（此时客户端尚未收到任何字节，换候选安全；旧 prebuffered/StreamConsumed 机制随之移除），routes 重试分支对流式/非流式统一，并加 `body_iterator` 守卫防"failover 后流式成功被错误 JSON 包裹"；② **限流语义兜底**——sensenova 同端点对不同账号对同一 TPM 限流返回不同 code（429001 / insufficient_quota，type 均为 `rate_limit_error`）、modelscope 免费层 token-limit 429 也是 `insufficient_quota`，分类器按 code 误判 quota_exhausted（3600s 冷却 + 假"需人工干预"告警 + 共享池分钟级自愈却被封 1 小时）；新增 hint 正则（rate_limit/tpm/rpm/token-limit）在 code 精确匹配**之前**判 RATE_LIMITED 300s，真计费耗尽 message 不含这些词不受影响；③ **终态 429 语义改写**——所有候选耗尽仍返回 429 时，把语义是限流的 `insufficient_quota` 错误体改写为 `rate_limit_error`（保留原始 message + Retry-After: 30），消除客户端（ZCode）对 insufficient_quota 判 `retryable=false` 直接终止回合的问题。新增 `tests/test_v67_stream_failover.py`（9 项），全量 859 全绿。详见 [dev/architecture.md](dev/architecture.md) 决策表 V6.7 行。

**账号配额真实测量重设计（2026-09-16，metering 立项当日落地）**：account_quotas 原「预留 estimated_tokens=max_tokens×2 + used_value 结算」两段式，因结算半边 `increment_usage` **零调用点**退化为纯静态门 `max_tokens×2>limit`——tpm（60s 短窗）对大 max_tokens 请求恒拒，deepseek_01/glm_01/stepfun_01 三行 tpm 全中招（前两行已删行止血）。V6.6 重设计四点：① **token 维改真实测量**——tpm/daily_tokens/weekly_tokens/5h_tokens 直接对 usage_logs 按账号+窗口 SUM(input+output)（沿用 V4.8 `_sum_user_tokens` 模式；tpm=60s 滚动、daily=本地零点、weekly=ISO 周一或 rolling window_seconds、5h=rolling 18000s），无预留无 used_value；② **rpm 改近 60s 真实 COUNT**；③ **concurrent 改单进程内存 in-flight 计数**（proxy 转发前 `concurrent_acquire`、上游响应完成/流结束 `concurrent_release`，异常路径幂等释放，重启归零属预期；限额值仍配在 account_quotas）；④ **预检不再收 estimated_tokens**——吞吐门不能预押；用户级日/月预算门保留「真实 SUM + 准入预留」语义不变。`increment_usage`/`decrement_concurrent`/`_is_window_expired`/`_rollover_window` 连同窗口状态行维护一并删除，`account_quota_usage` 仅剩 balance_cny sync 标记在用。新增索引 `idx_usage_logs_account_time(account_id, created_at)`（ORM 声明 + 生产手动 CREATE INDEX）。test_quota_check.py 按新语义重写 18 项（含 tpm 假门回归锚点：无真实消耗时 128k 声明值不再触发拒绝），全量 850 全绿。机制详见 [dev/architecture.md](dev/architecture.md) §3 quota.py 小节 + 决策表。

**旗舰修复批（2026-09-16，全部上产 + VM e2e 验收）**：① **DeepSeek thinking 回传 400 修复**——DeepSeek 官方在带 `tools` 的请求里要求历史**每轮** assistant 回传 `reasoning_content`，跨渠道会话（部分轮次无 CoT）被整段 400 且 `retryable=false`；新增 `channels.requires_reasoning_echo` 渠道标记（deepseek_official=1），proxy 转发前对缺 rc 的 assistant 补**空串**占位（实测 200，不注入假文本），并加守卫「入站已含 rc 的会话不再注入 `enable_thinking=false`」（消除 sensenova Phase B 注入抹平 CoT 的来源）；② **modelscope 规范名接入**——组路由候选只认规范名精确价行，ch7 原本全是 raw id 价行进不了候选；补 `deepseek-flash`/`deepseek-v4-pro`/`glm-5.2` 规范价行（0 元）+ `raw_model_id` 接进 `_channel_model`/body 重写（匹配键与上游模型名分离），deepseek-flash 主供给切 modelscope（0 元），**deepseek_official 兜底已实测**（禁用 modelscope → ch1 + rc 补齐 → 200）。价行两行并存设计备忘：`model`=匹配键 / `raw_model_id`=上游名，**规范名行=组路由入口、raw 行=直连入口（勿删，删了直连 raw id 请求会掉 fallback）**；同日完成**全渠道价行形态盘点**（siliconflow 99 行全 raw = 有意 fallback-only、stepfun 2 行未注册 = 仅直连，其余渠道全规范），并沉淀**新渠道/新模型接入规范 6 条**，详见 [dev/architecture.md](dev/architecture.md) §3 router 小节；③ **ch1 模型收敛**——删 deepseek-v4-flash / deepseek-v4-flash-vision-exp 价行 6 行（渠道只剩 deepseek-flash + deepseek-v4-pro）；④ **删 glm id=11 + stepfun id=17 tpm 假门**（`account_quotas` 结算半边 `increment_usage` 零调用点 → 预检退化纯静态门 `max_tokens×2>limit`，两行对 128k 请求恒拒；删行止血，**token 维真实测量重设计单独立项未实现**）；⑤ **失败链可观测**——`X-AIMC-Failed-Channels` 头 4 处改 `ensure_ascii=True`（中文 error_msg → latin-1 编码炸成 500）；`_write_usage_log` 对 `database is locked` 短重试 3 次；⑥ **deepseek-flash 官方标价入库**（registry list 2/8 = peak，存量 367 行 NULL 标价按同口径回填，成本审计 24h 标价从 <0.01 → ¥79.64）。

**sensenova 体量准入 + TPM/RPM 冷却基数（2026-09-16）**：直连探测 + 用户控制台截图定位 9/13 起 sensenova 上游 429001 近乎全挂的根因——**免费公测池为全体用户共享吞吐，与账号积分无关**（积分满格 5h 窗口可用 94.6%/100% 仍被拒；同秒 84 token 请求 200 / 110k token 429；完全静默 5 分钟不恢复；阈值不单调 2k 挂/6k 过/12k 挂）；顺带实测独立的 RPM 闸门 ≈8 次/分（`code 8 / rpm exhausted`，与文档"v4-pro 无调用次数限制"不符）。两项处置：① **路由体量准入**——`config.channel_max_input_tokens`（渠道名→估算输入 token 上限，默认 `{"sensenova_official": 30000}`，env `AIMC_CHANNEL_MAX_INPUT_TOKENS` JSON 可调），`router.select_best_channel` 按 `_estimate_input_tokens`（~3 字符/token、图片 1000/张）剔除超限候选，**全部候选超限时保留原候选不 503**；② **TPM/RPM 类 429 冷却基数 30s→300s**（error_classifier 对 code `429001` 或 tpm/rpm 措辞的 rate_limited 给 300s/600s，指数退避与 3600s 封顶不变；其他瞬时 429 如 glm 过载波纹保持 30s）。时段假设（工作日 9-18 更易限流）经簇级 bootstrap 证伪。新增 `tests/test_v66x_size_admission_tpm_cooldown.py`（34 项），全量 840 全绿。同日补充**准入下界**（用户拍板）：taotoken 按**调用次数**计费（~2000 次/5h）而 sensenova 只限制大请求 → `channel_min_input_tokens`（默认 `{"taotoken_coding": 30000}`）把小请求让给 sensenova、taotoken 的次数留给大请求；准入判定移到频限+配额过滤之后（任何可用渠道不会被窗口规则饿死），一方冷却时另一方自动兜底。注意池子饱和期 sensenova 小请求成功率仅 ~9%（<30k，6/69），小请求会先试 sensenova 失败再落 taotoken（300s 冷却自限），属预期行为。详见 [dev/next-stage-research.md](dev/next-stage-research.md)「⚡ 已上产（2026-09-16）」小节。

**V6.5 稳定性批次（2026-09-14，三批全部上产验收）**：源自 [dev/production-review-2026-09-13.md](dev/production-review-2026-09-13.md) 生产审查 + [dev/v64x-429-fix-review.md](dev/v64x-429-fix-review.md) 429 批次审核，排期见 [dev/next-stage-research.md](dev/next-stage-research.md) §2.0。
**批次一（75069ea，779 绿）**：**冷却退避架空修复**（审核插队项——`is_in_cooldown` 过期 pop 整个 state 致指数退避在「到期→router 查询→重撞」主场景下恒 30s，改为过期不 pop + `cooldown_expired_at` 标记 + cleanup 600s 保留窗 + 超窗 count 归零 + needs_human 不被过期洗白）；S1 失败链响应剥 `content-length`/`transfer-encoding`/`connection`（声明 25B/实际 351B 实证）；S6 周报两端点 `html.escape`（前端 DOMParser 自动反转义，零前端改动）；B1 探活 `ErrorInfo` NameError（失败探活恢复回写冷却状态机）；G scheduler cwd 改 `_PROJECT_ROOT` 相对定位。
**批次二（7ffa295，789 绿）**：S8 路由成功率 `LIMIT 20` 截断（unmatched 改无 limit count）；S9 cache 命中率 cutoff 改 Python 本地时间 + 按 `channel_id`（原 UTC 窗口偏移 8h / name 子查询串号）；S4 differ peek/commit 两段式（解析失败保留旧 hash 下轮自动重试，不再永久吞变化）；S11 sync_engine 补 `busy_timeout`/`synchronous`；S12 调度器清理残留 `batch_crawl_*`（改 poll_interval 后旧 job 重复采集）。
**批次三（434d5e8，802 绿）**：S3a 折扣只作用解析命中模型集（旧实现乘到渠道全部 normal 模型）；S3b promotion 永久打折双层修复（healer 缺 end_date 补 +7d + router 对存量 `effective_to=None` 脏行拒绝兜底）；S3c 合成「半价」端到端验收 + 订阅类不转价格行反向断言（双轨拍板两边都有验收线）；S7 停机 drain（proxy `_spawn()` 登记式 fire-and-forget 全迁移 + dispose 前 `shutdown_drain_tasks` + 定时采集注册 in-flight）。

**V6.3 自动化收口（2026-09-05）**：F1 首启检测（空渠道 WARN 引导，manual_import 降级可选）+ F2 请求日志 model 搜索后端（`?model=` 模糊过滤）+ F3 自动入组埋点（运维页「自动入组观察」卡，纯只读 5 组指标）+ F7 图-表双向 hover 联动（Cache 环图↔渠道表、柱图↔日表），见 [dev/automation-closure-plan.md](dev/automation-closure-plan.md) §6。
**V6.4 中期决策批次（2026-09-11，提前于 9/19）**：① **思考 Phase B 上产** — `channels.disable_thinking_inject` 渠道级注入开关（默认全关，开谁看数据定），见 [thinking-control-plan.md](thinking-control-plan.md) §3 决策记录；② **F3 收口 S3** — AUTO_JOIN_NON_T0=True + `AUTO_JOIN_MIN_CONFIDENCE=0.9`（非 T0 门槛，T0 恒免人工），见 [dev/automation-closure-plan.md](dev/automation-closure-plan.md) §5；③ **R9 不立项** — Phase 1-3 不做，8 死源运维禁用，见 [dev/next-step-plan.md](dev/next-step-plan.md) §十一；④ **#11/#12** — 路由有效输入价按渠道 7d cache 命中率折算（hit=0 退化原价）；⑤ 顺带修 `_update_cooldown` unawaited 告警协程。反馈公式 retry 项保留双分现状，观察 2~4 周后再取舍。
**V6.4 收尾（2026-09-12）**：⑥ **DeepSeek V4.1 迁移** — 官方 id 只剩 `deepseek-flash`（=V4.1-Flash，多模态，价格降 33%）+ `deepseek-v4-pro`；deepseek-flash T0 进 flagship/vision 组，旧名 deepseek-v4-flash 保留 strong/code（sensenova 0 元 + SF 仍供，registry 已恢复 active——deprecated=退出组路由实证），vision-exp 无渠道提供已下线，r1 系 T0 事实错误修正（→T2/T3）；⑦ **SF org 段命名修复** — org 与模型行名不同源时不再塞进 model_id，存量改名 paddleocr-vl-1.5 / longcat-2.0 / nex-n2-pro，8 行厂商品牌前缀保留（tongyi-mai/teleai/fnlp/funaudiollm/baai）；⑧ **全渠道思考注入兼容矩阵** — 实测无上游执行 enable_thinking:false（glm_bigmodel 400 强制思考、sensenova/siliconflow/minimax/deepseek 忽略），sensenova 开关注入观察中其余全关；⑨ **OCR 模型调用面确认** — deepseek-ocr/paddleocr-vl-1.5 无独立端点，走 chat 多模态（image_url 内容段），SF 拒不可下载图 URL（20040），base64 data URI 可用。全量 747 测试全绿。
**V6.4.1 库内时区统一（2026-09-12）**：根除 `func.now()`（SQLite CURRENT_TIMESTAMP=UTC）与 P3 本地时间并存的混写——全库 DATETIME 列默认/onupdate 改 `datetime.now`，建议审批 `func.now()` 与 auto_join 端点 utcnow 双窗口一并归一（4198b8e）；存量 UTC 行按"无小数秒"特征停服迁移 +8h（scripts/tmp_tz_migrate.sql，33 列含 usage_logs 1661 条 P3 前旧行），审计脚本 scripts/tmp_tz_audit.sql。全仓读取方本地窗口过滤自此零漂移。

**看板统计口径统一（2026-09-13）**：三张卡三种时间语义（滚动窗口 / "最近 N 个有数据的自然日"LIMIT / 滚动按日分桶）导致的同页不可对账收口为**滚动窗口单一口径**——`usage/summary` 补 cutoff + 桶轴 Python 侧补零（取代无 cutoff 全表扫描的 LIMIT 语义，零流量日不再凭空消失），24h 档 `usage/summary`+`cost-trend` 改按小时分桶（消灭跨日半截柱），7d/30d 维持按日；概览 Top 模型卡改按请求数排序取前 5（成本审计页保持按标价排序，6c8fd61）；口径就地亮出（Top 模型"按请求数"、用量统计"按小时/日分桶"副标题 + 全局 24h/7d/30d 选择器滚动窗口 tooltip）+ 运维页新增「指标口径速查」卡（METRIC_DEFS + CARD_DEFS 同源渲染，与表头 tooltip 永远一致）。agent-api.md 契约同步。**每日预聚合 rollup 表不立项**（实测 30d 聚合 11ms、余量 30 倍），触发器三条见观察清单：usage_logs>50 万行或 30d 聚合>300ms / 引入 usage_logs 留存清理 / 跨季度分析需求。

**架构梳理与模块结合诊断（2026-09-13）**：对 gateway / db / intelligence / dashboard 四层做了一次全链路梳理，产出两份文档——**[dev/architecture.md](dev/architecture.md) 重写**（四层结构 + 依赖图 + 请求数据流 + 迁移机制 + 结合诊断摘要 + 决策表，修正 41→42 job / 6→7 tier 组 / 30+→67 端点 / 删除 config 中不存在的 per-vendor key 说明）；新增 **[dev/module-integration-audit.md](dev/module-integration-audit.md)**（逐条证据：4 类「接通但空转」旁路——本地限流计数侧零调用、账号配额 `used_value` 生产零写入、model_cards 评分未接入路由、结构化价格解析被丢弃；1 个确定性 bug——`channel_health` 探活失败分支 `ErrorInfo` 未 import 致 NameError；死代码清单 + 4 死端点 + 3 孤儿前端资产 + 3 遗留 schema 文件；双向依赖/掏私有成员/静默吞异常/迁移三轨风险）。

**生产审查（2026-09-13）**：在结合诊断基础上做四路静态审查 + 逐条读码复核 + **生产库实证**（VM ubuntu2401，10,559 usage_logs），产出 **[dev/production-review-2026-09-13.md](dev/production-review-2026-09-13.md)**。**需处理**：① S1 失败链响应复制旧 `Content-Length` → body 变长被截断/h11 协议错（**本机实证** starlette 1.3.1：声明 25B / 实际 351B）；② S3 情报「优惠→价格行」生产 0 产出且一旦触发会把折扣乘到该渠道**全部**模型（siliconflow 99 模型）；③ S8 路由成功率 30d 因 `LIMIT 20` 被高估（实测 27>20）；④ S9 cache 命中率查询用 UTC 窗口与本地时区相悖（实测 ~0.9% 偏移）；⑤ S5 失败重复计数、S6 周报 XSS、S4 differ 先落 hash、S7 停机 drain。**同时纠正 4 条首轮误判**（流式 usage 未丢、DeepSeek cache 已解析、cache JOIN 无丢行、Anthropic 口径为潜伏项——6 渠道全 `api_style=openai`）。建议修复排期已并入 [dev/next-stage-research.md](dev/next-stage-research.md) §2.0。

**下阶段拍板与 429 隐形拒绝批次（2026-09-14）**：决策清单 #2（S3 双轨：订阅类维持仅入库展示 + 修真折扣链路缺陷）、#4（迁移机制 V6.5 后做轻方案 A）已拍板。同日生产实证 **tier:flagship 兜底链失效**三根因——① 配额预检判定式 `已用+max_tokens×2 > 限额` 在 used 恒 0 时退化为 `max_tokens×2 > limit`，deepseek_01 的 tpm=100000 行（8/14 拍脑袋估算）把 zcode 声明 128000 output 的请求恒拒死，且拒绝路径直接 return 不 failover/不落库 → 看板隐形（**同时修正审查报告 S2 结论**：tpm 维度非恒放行而是恒拒死）；② 429001 池级限流撞上账号级固定 30s 冷却，无升级；③ 流式 200 的 mark_success 无条件擦除冷却。**3 项修复已于当日全部上产**：预检拒绝改 mark_failure + usage_logs 落库 + 组路由 failover 重选；删 deepseek_01 tpm 行（生产库操作留档可回插，e2e 实证 deepseek-flash 从隐形 429 变真实转发 200）；rate_limited 冷却指数退避（30s×2^n 封顶 3600s）+ mark_success 迟到成功守卫（冷却期内在途 200 不再擦冷却）+ 同渠道兄弟账号冷却对齐（渠道级同步）；顺带移除 routes failover 块与 proxy._update_cooldown 的双重 mark_failure（批次一 S5 主修复点）。新增 `tests/test_v64x_cooldown_quota_failover.py`（26 项）+ test_v46_cooldown.py 3 处断言按新语义更新，全量 762 全绿。详见 [dev/next-stage-research.md](dev/next-stage-research.md)「⚡ 已上产」小节。

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
| 查模块间结合诊断（断链/死代码/耦合）| [dev/module-integration-audit.md](dev/module-integration-audit.md) |
| 看生产审查报告（bug/指标口径/安全）| [dev/production-review-2026-09-13.md](dev/production-review-2026-09-13.md) |
| 查已知路由行为：flagship glm-5.3 经 taotoken 降级为 flash（已拍板保持现状）| [dev/glm53-taotoken-flash-downgrade.md](dev/glm53-taotoken-flash-downgrade.md) |
| 看下一阶段规划调研 | [dev/next-stage-research.md](dev/next-stage-research.md) |
| 看 API 端点（OpenAI + Dashboard）| [dev/api.md](dev/api.md) |
| **Agent 编程接入（REST/CLI 契约, V6.1）** | [agent-api.md](agent-api.md) |
| 本地搭建开发环境 | [dev/setup.md](dev/setup.md) |
| commit / 分支 / 测试 规范 | [dev/conventions.md](dev/conventions.md) |
| 写测试 / 提 PR / 贡献代码 | [dev/contributing.md](dev/contributing.md) |
| 跑 859 个测试 + 加新测试 | [dev/testing.md](dev/testing.md) |
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

- **最后更新**：2026-09-17
- **测试覆盖**：859 tests（pytest，~7 分钟跑通，2026-09-17 V6.7 基线全绿）
- **观察期关注**：缓存命中率（路径 A）、周报 timer 首次自动触发（周一）、vision 400 率、**思考注入各渠道开谁（Phase B 已上产默认全关，见 [thinking-control-plan.md](thinking-control-plan.md) §3）**、**S3 生效后 pending 变化与门槛拦截量（AUTO_JOIN_MIN_CONFIDENCE=0.9，见 [dev/automation-closure-plan.md](dev/automation-closure-plan.md) §5）**、**反馈公式 retry 项取舍（双分并存，观察 2~4 周 ~2026-09-23~10-07）**
- **模块结合待办**：[dev/module-integration-audit.md](dev/module-integration-audit.md) 的 P0（探活 NameError / 部署路径硬编码）与 V6.5 三批（S1/S4~S9/S11/S12/S3a-c/S7）**已全部修复**；剩余 = 死代码清理（审计 §C）、静默 except 收窄（§E）、迁移机制收敛（已拍板 V6.5 后做轻方案 A）与决策项（本地限流/账号配额接线），排期见 [dev/next-stage-research.md](dev/next-stage-research.md)
- **V4.9 新功能**：Dashboard **🔴 待处理 (N)** tab（余额不足 / Key 失效 / 智能探活状态），见 [user/dashboard.md](user/dashboard.md)
- **观察期后待办**：操作审计系统（内网自用继续观察，公网/多用户前必修；见 [dev/automation-closure-plan.md](dev/automation-closure-plan.md) §2.4）
