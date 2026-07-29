# AIMC 开发计划

> 更新时间：2026-07-29
> 状态标记：✅ 已完成 | 🔨 进行中 | ⬜ 待开发
> 配套文档：[架构设计](architecture.md) / [竞品调研](competitive-research.md)

---

## 路线总览

| 阶段 | 主题 | 状态 | 说明 |
|------|------|------|------|
| Phase 1 | 网关核心 | ✅ | FastAPI 路由/频限/透传/8 张表基础架构 |
| **Phase 2.5** | **运维加固** | **✅** | **4 个日常痛点改进（详见下文）** |
| Phase 2 | 多维模型组 | 🔨 当前重点 | 4 张新表 + 组内选优 + Agent 偏好 |
| Phase 3 | 情报联动模型组 | ⬜ | AI 解析 → 自动入组/出组 |
| Phase 4 | 智能优化 | ⬜ 远期 | 用量分析 / 延迟追踪 / 自动权重 |

设计原则：**运维先于新功能**（新增"二、设计原则"）。Phase 2.5 在 Phase 2 实施前补的，让"日常运维不痛"先于"模型组新能力"。

---

## Phase 1: 网关核心（已完成）

> 目标：跑通"请求进来 → 选最便宜渠道 → 透传出去"的核心链路

- ✅ FastAPI 网关骨架 + OpenAI 兼容接口
- ✅ 流式/非流式透传（aiter_raw，不解析 SSE）
- ✅ 路由决策引擎（同模型多渠道选优）
- ✅ 内存频限器（QPM/TPM/并发/日限额）
- ✅ 多账号轮询 + 故障转移（fallback）
- ✅ SQLite WAL + SQLAlchemy 2.0（8 张核心表）
- ✅ 峰谷时段定价支持
- ✅ 网关鉴权（Bearer Token）
- ✅ 极简 Dashboard（htmx + Chart.js）
- ✅ pydantic-settings 配置管理
- ✅ 手动录入脚本（`scripts/manual_import.py`）

补充完成（不在原 Phase 1 计划内，但属于"基础架构"）：
- ✅ ModelCard 评测卡表（`scripts/seed_model_cards.py`，43 张国内+开源模型 + OpenCompass 4 榜引用分）
- ✅ 9 个 channel（5 直连 + 1 聚合硅基流动 + 3 国内直连火山/混元/阶跃）
- ✅ Proxy 失败告警（5xx + 401/403 → 飞书/Bark/Server酱）
- ✅ 精确模型路由 fallback 到 priority 最低 channel（聚合平台兜底）

---

## Phase 2.5: 运维加固（已完成）

> 目标：让"日常运维不痛"——重启、改 key、查日志、接新 model 这 4 件事不再需要重启 uvicorn 或刷屏告警

| # | 改动 | commit | 痛点 |
|---|------|--------|------|
| #1 | 拉硅基流动 91 个 model 入库 | `636c334` | `/v1/models` 之前只列 2 个，掩盖了"硅基流动 1 key 通吃"的核心能力 |
| #3 | 告警 throttle（同 key 60s 只发 1 次） | `244f279` | 硅基流动挂掉时飞书会被刷屏 |
| #4 | pre-commit hook 防 `.env` 误 commit | local config | 真实 API key 不能进 git |
| #5 | `POST /dashboard/api/cache/refresh` 手动刷新 | `e50bb7a` | 改 channel key 后不想等 5 分钟自动刷新 |

未做：~~#2 router 503 告警~~（fallback 启用后几乎不返回 503，价值小，留作未来）。

详细设计/验证/使用见 [architecture.md §八 运维加固](architecture.md#八运维加固phase-25-已完成)。

---

## Phase 2: 多维模型组（当前重点）

> 目标：实现跨模型比价路由，Agent 请求"能力档"而非具体模型

### 2.1 数据层

- ⬜ 新增 `model_registry` 表（ORM 模型 + 迁移）
- ⬜ 新增 `model_groups` 表
- ⬜ 新增 `model_group_members` 表
- ⬜ 新增 `model_capabilities_log` 表
- ⬜ `channels` 表扩展：`provider_type`、`billing_mode` 字段
- ⬜ **处理 `model_cards` 与 `model_registry` 的关系**：是合并（`model_cards` 改名为 `model_registry` + 加 tier/capabilities 列）还是保留双轨（`model_cards` 仅存 benchmark，`model_registry` 存 tier/capabilities）
- ⬜ 编写模型组种子数据脚本（`scripts/seed_models.py`，参考 architecture.md §九 预置配置）

### 2.2 路由引擎升级

- ⬜ 模型组解析器：识别 `tier:`/`scene:` 前缀
- ⬜ 组内跨模型全局选优逻辑
- ⬜ Agent 偏好约束处理（`extra_body.aimc_preferences`）
- ⬜ 组间 fallback chain 递归降级
- ⬜ billing_mode 处理（free/subscription 价格视为 0）
- ⬜ 响应头注入（X-AIMC-Actual-Model / X-AIMC-Actual-Channel）

### 2.3 模型组管理器

- ⬜ `gateway/group_manager.py`：组配置内存缓存
- ⬜ 组缓存定时刷新（与路由缓存同步）
- ⬜ `/v1/models` 接口升级：返回模型 + 模型组

### 2.4 Dashboard 扩展

- ⬜ 模型注册表管理页面（增删改查）
- ⬜ 模型组配置页面（组成员管理、策略选择）
- ⬜ 模型能力标签可视化（tier 分布图、能力雷达图）

### 2.5 验证标准

- [ ] `model="tier:balanced"` 请求能正确返回组内最便宜的模型响应
- [ ] 组内全部渠道频限满时，自动降级到 `fallback_group`
- [ ] `/v1/models` 正确返回模型组列表
- [ ] 精确模型名请求行为不变（向后兼容）
- [ ] Dashboard 可管理模型和组配置
- [ ] `#5` cache refresh endpoint 也刷模型组缓存（避免改完 seed_models.py 要等 5 分钟）

---

## Phase 3: 情报联动模型组

> 目标：情报层自动维护模型注册表和组成员关系

### 3.1 采集源扩展

- ⬜ 新增"模型列表页"采集源类型（发现新模型）
- ⬜ 新增"第三方平台"采集源（硅基流动/火山引擎模型+价格）
- ⬜ 新增"Benchmark 榜单"采集源（LMSYS Arena / SuperCLUE）
- ⬜ 采集源调度策略优化（不同类型不同频率）

### 3.2 AI 解析器升级

- ⬜ 解析器支持提取模型元信息（model_id/family/context_length/capabilities）
- ⬜ 新模型 tier 推断逻辑（基于参数规模、厂商定位、benchmark）
- ⬜ 解析结果置信度分级处理

### 3.3 自动入组/出组

- ⬜ 新模型自动入组（confidence > 0.8）
- ⬜ 模型下线自动移出（confidence > 0.9）
- ⬜ 低置信度变更 → 飞书/Bark/Server酱 告警 + Dashboard 待确认队列
- ⬜ tier 调整永远需人工确认（安全阀）

### 3.4 价格联动

- ⬜ 价格变动自动更新 `price_snapshots`
- ⬜ 路由缓存刷新后组内排序自动变化（无需额外触发）
- ⬜ 优惠活动影响渠道优先级（临时权重加成）

### 3.5 验证标准

- [ ] 模拟新模型发现 → 自动出现在 `model_registry` 和对应组中
- [ ] 模拟价格变动 → 下次请求路由到新最低价渠道
- [ ] 模拟模型下线 → 自动从组中移除，不再被路由
- [ ] 低置信度变更 → 收到告警

---

## Phase 4: 智能优化（远期）

> 目标：基于历史数据持续优化路由决策

- ⬜ 用量分析：统计各模型/渠道的实际使用频率和成本
- ⬜ 延迟追踪：记录各渠道历史延迟，支持 latency 策略
- ⬜ 组内权重自动调整：基于成功率和延迟动态调权
- ⬜ 价格趋势预测：基于历史快照预测峰谷，提前切换
- ⬜ Agent 偏好学习：记录哪些路由决策效果好
- ⬜ Dashboard 成本报表：月度/周度花费趋势、节省金额估算
- ⬜ 多渠道分散策略：避免单渠道频限，自动分散用量

---

## 技术债务 / 已知限制

| 项目 | 现状 | 计划 |
|------|------|------|
| 路由缓存刷新 | 5min 自动 + 手动 endpoint (`#5`) | Phase 3 改为事件驱动（价格变动即刷新） |
| 模型组缓存刷新 | 与路由缓存同 5min | Phase 2 独立刷新逻辑（避免等 5min） |
| 频限器持久化 | 纯内存，重启丢失 | 可接受（重启后窗口自然重置） |
| API Key 加密 | 明文存 SQLite | Phase 2 考虑 Fernet 对称加密 |
| `model_cards` vs `model_registry` | 9 张表 (`model_cards` 已实现) + 4 张表待加 | Phase 2 处理双轨关系（合并 or 保留） |
| 并发安全 | 单进程无锁 | 足够（个人使用场景） |
| Playwright 浏览器二进制 | 未装（采集功能不可用） | 个人用可接受，未来按需装 |

---

## 文件变更预估（Phase 2）

| 操作 | 文件 | 说明 |
|------|------|------|
| 新增 | `db/models.py` | 追加 4 个 ORM 类 |
| 新增 | `gateway/group_manager.py` | 模型组管理器 |
| 修改 | `gateway/router.py` | 增加组路由逻辑 |
| 修改 | `gateway/routes.py` | `/v1/models` 升级 |
| 修改 | `gateway/proxy.py` | 响应头注入 |
| 新增 | `schemas/model_group.py` | Pydantic 模型 |
| 新增 | `scripts/seed_models.py` | 种子数据导入 |
| 修改 | `dashboard/routes.py` | 模型组管理 API |
| 修改 | `dashboard/static/app.js` | 模型组管理 UI |
| 修改 | `db/crud.py` | 新增 CRUD 函数 |

---

## 跨项目参考

- **hermes-tray**（`D:\work\workspace\Qoder\hermes-tray`）的 v0.3.0 计划在 Phase 1 消费 aimc 注入的 SSE `routing_decision` 事件（footer pill + stats page）。
- Phase 2 完成后，hermes-tray 可切换到 `model="tier:balanced"` 调用方式，跟"能力档"对齐。
- 两侧 commit 走各自仓库，联调在各自 phase 完成后做。
