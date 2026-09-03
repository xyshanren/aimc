# AIMC V6.0 总体规划（设计稿）

> 状态: **V6.0 Phase 1 已上产并验证** (2026-09-03, commit 76df5a7/83929b2)
> 日期: 2026-09-03（v2，按用户五点反馈修订；v3 补实施记录见文末 §6；v4 补上产验证 §6.5）
> 前置: V5.1 (commit 8c8e010) 成员优先级已激活
> 定位: 下一大版本（V6.0）三条需求统一规划

---

## 0. 需求清单

1. **模型组优化** — GLM 官方免费模型 GLM-4.7-Flash（文本）/ GLM-4.6V-Flash（视觉）接入；light/vision 分组优化；vision 分级落地。
2. **Token 审计** — 借鉴 CodeBurn，规划 token 成本 / 收益追踪。
3. **架构 Agent 友好性** — 全部查询/变更支持 API 或 CLI，看板只做核心数据 + 人工干预入口。

---

## 1. 需求一：模型组优化

### 1.1 定价事实（open.bigmodel.cn/pricing，用户抓取 Markdown 确认）

| 模型 | 上下文 | 输入 | 输出 | 输入模态 | 备注 |
|---|---|---|---|---|---|
| GLM-5.3 | 1M | 8元 | 28元 | 文本 | 旗舰 |
| **GLM-5.3-Flash** | 1M | **0.4元**(5折) | **1.4元**(5折) | **图片/视频/文件/文本** | 多模态，5折限时两周 |
| GLM-5.2 | 1M | 8元 | 28元 | 文本 | |
| GLM-4.7-FlashX | 200K | 0.5元 | 3元 | 文本 | |
| **GLM-4.7-Flash** | 200K | **免费** | **免费** | 文本 | 文本免费 |
| GLM-4.6V-FlashX | 128K | 0.15元 | 1.5元 | 图片/视频/文件/文本 | |
| **GLM-4.6V-Flash** | / | **免费** | **免费** | 图片/视频/文件/文本 | 视觉免费 |
| GLM-4.6V | 128K | 1元 | 3元 | 图片/视频/文件/文本 | 付费视觉 |
| GLM-4.5V | 64K | 2~4元 | 6~12元 | 图片/视频/文件/文本 | **上一代**付费视觉 |

> 注：GLM-4.6V-Flash（免费）与 GLM-4.6V（付费）是**不同模型**。GLM-4.5V 是上一代付费视觉模型，走硅基流动，不在 bigmodel 免费通道。

### 1.1.1 实测结论（2026-09-03，VM 生产 key 实测）

生产 `glm_bigmodel` 渠道 base_url = `https://open.bigmodel.cn/api/coding/paas/v4`（coding plan 端点），标准端点 `/api/paas/v4` 对该账号报 1113「余额不足」——**只有 coding plan 端点可用**。逐模型实测：

| 模型 | @coding/paas/v4 | @paas/v4（标准） | 结论 |
|---|---|---|---|
| glm-4.6v-flash | **200**，model 回显 `glm-4.6v-flash`，无切换 | 429 (1305 过载) | **coding 端点可用，直接接入** |
| glm-4.7-flash | 429 (1305「访问量过大」)×4 次重试 | 429 (1305) | 模型 ID 真实存在、**无静默切换**，但当前持续过载；免费模型可接入靠 failover 兜底 |
| glm-4.7（对照，不带 Flash） | **200 但 model=`glm-5.3-flash`** | — | **自动切换规则只对不带 Flash 的 ID 成立**，不影响 glm-4.7-flash |
| glm-5.3-flash | 200（生产常规流量） | 429 (1113 余额不足) | 仅 coding 端点可用，证实渠道配置 |

- **1305 → RATE_LIMITED（30s 冷却，自愈，不误报余额不足）**：error_classifier 对 429 且非 quota 关键词走 rate_limited 分支（needs_human=False, cooldown=30s）。风险：cooldown 按 account 粒度，glm-4.7-flash 频繁 1305 会让 glm_bigmodel 账号出现 30s 冷却波纹，短时波及同账号 glm-5.3-flash/glm-4.6v-flash 流量 → **上产观察项**，若波纹明显再考虑 per-(account,model) 冷却粒度。
- 本地 `.env` 的 `GLM_API_KEY` 为空（仅注释占位），GLM 实测只能在 VM 上做。

### 1.2 最终方案（按用户反馈修订）

**tier:light 加入 GLM-4.7-Flash（文本免费）**

- registry 新增 `glm-4.7-flash`（family=glm, tier=T3, modality=text, 免费）。
- 加入 light，priority=130（glm-4-flash 之后）；免费 0 元自然排最前，成为 light 免费默认，补回 V5.1 丢掉的免费车道。
- 保留 glm-4-flash 作付费兜底。

**tier:vision 重排（旗舰视觉）**

- **tier 限定 T1 → NULL**（跟 strong/code/flagship 同例，避免 T2 成员被判死）。
- 成员（2 个，互为备份）:
  - `deepseek-v4-flash-vision-exp`（T1, modality=vision, 实验免费）
  - `glm-5.3-flash`（T2, **modality 改 multimodal**, coding plan 免费额度内、多模态）
- **glm-4.5v 直接移除**（用户决策 2026-09-03）：上一代付费模型，与新一代免费 GLM-4.6V-Flash 孰强存疑，不再进任何分组；registry/price_snapshots 保留（硅基流动直连不受影响）。

**tier:vision-light 落地（轻量视觉，免费兜底）**

- 新建组（require_vision=1, **tier=NULL**）:
  - `glm-4.6v-flash`（免费, 多模态）— 免费视觉兜底，priority 100
  - `qwen-vl-32b-instruct`（T1）110
  - `qwen-vl-32b-thinking`（T1）120
  - `qwen-vl-30b-a3b-thinking`（T1）130
  - `qwen-vl-8b-instruct`（T3）140
- qwen-vl 系列 registry tier **不动**，靠 NULL-tier + priority 排序。

**fallback 链**：

```
tier:vision → tier:vision-light → tier:light
```

### 1.3 关键机制修复：glm-5.3-flash 多分组不跳过文本

glm-5.3-flash 同时在 strong / code / vision 三个组。若 registry modality 改为 multimodal：

- `router.py:1390` require_vision 过滤认 `(vision, multimodal)` → 进 vision 组 ✓
- 但 `router.py:1403` P0-3 规则对「纯文本请求」跳过 `modality=multimodal` → **会误杀它在 strong/code 的文本角色** ✗

**修复**（1 行 + 测试）：P0-3 跳过规则收窄为**只在 `require_vision=1` 的组内生效**：

```python
# 现状 (router.py:1403)
if not has_mm and registry.modality == "multimodal":
# 改为
if group.require_vision and not has_mm and registry.modality == "multimodal":
```

理由：P0-3 的原始意图是「vision-focused 的多模态模型拒纯文本」；但 glm-5.3-flash 定价页明确接受文本。在非 vision 组（strong/code），成员是人工精选的，入组即代表「这个流量下该模型可用」，不应再被 modality 启发式误杀。vision 组内仍保留「纯文本不选 multimodal」的保护。

回归测试：`tests/test_multimodal_skip.py` 需补「multimodal 成员在 require_vision=0 组内纯文本不被跳过」的用例。

### 1.4 落地清单

1. `scripts/seed_model_registry.py`：新增 `glm-4.7-flash`、`glm-4.6v-flash`；`glm-5.3-flash` modality text→multimodal；`glm-4.5v` 保留 registry（仅移组）。
2. `scripts/seed_model_groups.py`：light + glm-4.7-flash；新建 tier:vision-light；tier:vision tier→NULL + 成员重排 + fallback 改 vision-light；qwen-vl/glm-4.5v 移入 vision-light。
3. `scripts/seed_local_channel.py` / price_snapshots：glm_bigmodel 渠道补 glm-4.7-flash、glm-4.6v-flash 免费快照（0 元，与现有 glm-5.3-flash 0.0 口径一致；coding plan 订阅制不计价，不录 5折标准价）。
4. `gateway/router.py`：P0-3 规则收窄（§1.3）。
5. 回归：`test_multimodal_skip.py` / `test_router_member_priority.py` / `test_group_members_consistency.py`。

---

## 2. 需求二：Token 审计（借鉴 CodeBurn）

### 2.1 现状（基础已很好）

`usage_logs` 已存全维度：model / channel_id / account_id / caller_id / key_id / group_id / input & output & cached tokens / cost_estimate / latency / error。

已有分析能力：`analyze_usage.py`、`/api/stats/per-user`、`/api/stats/group_routing`、`/api/cache/stats`、`/api/usage/summary`、周报。

### 2.2 差距 vs CodeBurn

| CodeBurn | AIMC 现状 | 差距 |
|---|---|---|
| 本地优先/无代理 | ✅ 本机 SQLite | 一致 |
| 按 model/project/task 拆解 | ⚠️ 有 model/user/group，无 project/task/session | 缺项目维度 |
| cost 精确 + 每日刷新定价 | ⚠️ cost_estimate 估算，免费/订阅归 0 | 缺标价双轨 |
| optimize 扫浪费 | ⚠️ analyze_usage recommendations 部分覆盖 | 可增强 |
| guard 预算守护 | ⚠️ 有 per-user 日/月限额，无预算阈值告警闭环 | 缺 guard |
| compare 性价比 | ⚠️ 有数据，无交互对比 | 缺 UI |
| yield 关联 git commit | ❌ 无 | 缺 yield（→ V7.0） |
| MCP 出口 | ❌ 无 | 缺 MCP（→ V6.1 架构） |

### 2.3 分阶段（按用户决策修订）

- **Phase A（成本精确化，V6.0 / P0）**：cost_estimate JOIN price_snapshots 真实价；免费/订阅模型「标价双轨」（真实标价 + 实付 0），量化节省金额。
- **Phase B（成本看板 + API，V6.0 / P0）**：新增 `/api/usage/cost-by-model`、`/cost-by-user`、`/cost-by-group`、`/cost-trend` + Dashboard 页；analyze_usage 升级为成本审计口径。
- **Phase C（进阶，按需）**：project/session 维度、guard、compare —— 视优先级排入后续 V6.x。
- **yield（git 关联）→ V7.0**。

---

## 3. 需求三：架构 Agent 友好性

### 3.1 判断

「查询/变更全走 API 或 CLI，看板只做核心数据 + 人工干预入口」**方向正确**，但当前是半成品：变更 API 覆盖不全、CLI 散装、鉴权两套、缺 OpenAPI/MCP 契约。

### 3.2 目标架构

- 查询 = REST GET；变更 = REST POST/PUT/DELETE + 统一 CLI 子命令（等价）。
- Dashboard 只留「关注（看板）」+「需人工判断的写操作」（tier/优惠审批/跨源拍板/加 key）。
- 操作分三类：纯自动（API/CLI）、半自动（提交 + 人工审批）、纯人工（只看板）。

### 3.3 V6.1 附带事项：Mimosa 存量风险清理（2026-09-03 用户列入）

commit/pash 时 Mimosa 扫出 11 high + 1 medium，**全部为存量文件**（V6.0 Phase 1 新增代码零命中）。每次 git 操作都会重复告警，清理同时消除 hook 噪音：

| 位置 | 类别 | 定性 | 处置方向 |
|---|---|---|---|
| `db/migrations/2026_08_14_p02_price_cache_schema.py` :67/:133/:142/:184 | SQL 注入 (high) | 历史件：SQL 经变量中转（与 V6.0 迁移被拦同因） | 改写为「调用点内联字面量 + ? 参数」同款写法 |
| `db/migrations/2026_08_14_p01_mgm_fk_unique.py` :118/:131 | SQL 注入 (high) | 同上 | 同上 |
| `db/migrations/2026_08_14_p03followup_cache_tokens.py` :89 | SQL 注入 (high) | 同上 | 同上 |
| `dashboard/routes.py` :2348 | 硬编码凭据 (high) | 误报：docstring 示例 `"sk-new-key-xxx"` | 示例改 `<new-api-key>` 占位符 |
| `scripts/check_env.py` :128 | 硬编码凭据 (high) | 误报：字典键是 env **变量名字面量**（含 "API_KEY" 字样），非凭据 | 调整结构避开凭据规则（如描述映射分离） |
| `scripts/_verify_models.py` :8 | SSRF (high) | 低危：本地验证脚本固定打 localhost | 加显式 host 白名单/URL 校验 |
| `scripts/deploy/verify_clients.py` :68 | SSRF (high) | 同上 | 同上 |
| `dashboard/routes.py` :118 | 跨文件污点 (medium) | **真问题**：fallback 登录页 `next` 参数未校验/未转义，直接拼进 JS `location.href='{next_path}'`（JS 注入/开放跳转） | 校验 next 仅允许站内相对路径 + 转义；同步检查 static/login.html 同款逻辑 |

**优先级建议**：medium 登录页 taint 先修（真实攻击面，公网部署场景 docs/ops/security.md 尤其相关）；其余按表顺序随 V6.1 架构重构顺带清。

### 3.4 落地步骤（V6.1）

1. 统一 CLI：`aimc`（或 `python -m aimc`）子命令封装现有 scripts，统一 `--help`/JSON。
2. 补齐 REST 变更端点：散装 scripts 关键写操作对齐 REST。
3. 鉴权统一：dashboard 变更端点接受网关 admin api_key。
4. 契约化：聚合 OpenAPI + agent 消费文档 + MCP server。

---

## 4. V6.0 路线图（按用户决策修订）

| 版本 | 内容 | 风险 |
|---|---|---|
| **V6.0** | Phase 1 模型组优化（light/vision + GLM 免费 + vision-light + P0-3 收窄） + Phase 2 Token 审计基础（成本精确化 + 看板/API） | 低~中 |
| **V6.1** | Phase 4 架构 Agent 友好化（统一 CLI + REST 补齐 + 鉴权统一 + 契约/MCP）+ **Mimosa 存量清理**（11H+1M，见 §3.3，其中登录页 taint 为真问题建议先修） | 高、横切 |
| **V6.x 靠后** | 命名规范化（qwen-vl → qwen3-vl，放宽 validate_model_id + 迁移）；**e2e 存量修复**（`test_phase2_e2e` / `test_v46_e2e` teardown 阶段 `DELETE FROM accounts` 外键失败，干净 master 可复现，2026-09-03 用户列入） | 中 |
| **V7.0** | yield（git 关联收益追踪） + Phase C 其余（project/guard/compare 按需） | 中高 |

---

## 5. 决策记录

1. ~~GLM-4.6V-Flash 放 vision 还是 vision-light~~ → **vision-light**（用户确认）。
2. ~~qwen-vl 系列 registry tier 是否降档~~ → **不动 tier**，vision-light NULL-tier + priority。
3. ~~命名是否本次做~~ → **延后到 V6.x 靠后小版本**（不进 V7.0）。
4. ~~yield 做不做~~ → **放 V7.0**。
5. ~~Phase 4 是否拆 V6.1~~ → **拆 V6.1**。
6. **glm-4.5v 定位**（用户质疑其旗舰地位）→ **直接移除出所有分组**（不降入 vision-light）；registry/直连保留。
7. **免费模型端点**（原实施前确认项）→ **已实测关闭**：glm-4.6v-flash 在 coding plan 端点 200 可用；glm-4.7-flash 真实存在但当前 1305 过载（无静默切换，自动切换规则只影响不带 Flash 的 glm-4.7）；渠道维持 coding plan 端点不变。

---

## 6. 实施记录 (2026-09-03)

### 6.1 已完成（commit 76df5a7，已上产）

| 文件 | 改动 |
|---|---|
| `scripts/seed_model_registry.py` | +`glm-4.7-flash`(T3/text/200K)、+`glm-4.6v-flash`(T3/vision/128K)；`glm-5.3-flash` modality text→multimodal；**回填 5 个存量漂移**：glm-5.2 / deepseek-v3 / deepseek-v3.2 / qwen-qwen3-8b / qwen-qwen3.5-4b（V4.9.5 起经生产迁移入组但 seed 漏了，全新环境 reseed 会丢成员） |
| `scripts/seed_model_groups.py` | light +glm-4.7-flash(130)；tier:vision tier→NULL、fallback→vision-light、收缩双旗舰；新建 tier:vision-light(5 成员)；glm-4.5v 移除；文档串更新（7 组 24 成员） |
| `gateway/router.py` | P0-3 收窄：`group.require_vision and not has_mm and modality=="multimodal"`（仅 vision 组内跳过） |
| `db/migrations/2026_09_03_v60_glm_flash_vision.py` | 生产一次性迁移（幂等, dry-run 默认）：registry 变更 + 组/成员变更（软删保审计）+ glm_bigmodel 0 元快照×2。SQL 全部调用点内联字面量 + ? 参数（Mimosa 过审） |
| `tests/test_vision_routing.py` | vision 成员断言改为 V6.0 终态；+vision-light 组定义/成员断言；+glm-4.5v 全组移除断言；+跨脚本全组成员一致性检查（该检查顺手抓出上述 5 个 seed 漂移） |
| `tests/test_v60_p03_narrow.py` | 新增 5 个行为测试（mock caches 调 `_select_by_group`）：非 vision 组 multimodal 不跳过 / vision 组纯文本跳过 / 带图选中 / vision 模态顶上 / 源码断言 |

### 6.2 测试结果

- 定向 51 个（vision 路由 + P0-3 + 成员优先级 + v497 污染回归）：全过
- 全量 636：**632 过 / 4 败** — 4 个失败（`test_phase2_e2e` ×2 + `test_v46_e2e` ×2）为**存量问题**（`DELETE FROM accounts` FK 清理失败），已用 `git stash` 在干净 master 上复现，与本次改动无关；**已列入 V6.x 修复项**（与命名规范化并列）
- 注：项目 `.venv` 是空壳（无 pytest/fastapi），测试用系统 Python 跑

### 6.3 VM 部署步骤（待执行）

```bash
# 0. commit + push 后, VM 上拉代码
wsl -e bash -lc "ssh ubuntu2401 'cd ~/aimc && git pull'"

# 1. dry-run 检查迁移动作清单
wsl -e bash -lc "ssh ubuntu2401 'cd ~/aimc && python3 db/migrations/2026_09_03_v60_glm_flash_vision.py'"

# 2. 应用迁移（幂等, 失败自动 ROLLBACK）
wsl -e bash -lc "ssh ubuntu2401 'cd ~/aimc && python3 db/migrations/2026_09_03_v60_glm_flash_vision.py --apply'"

# 3. 立即刷新路由缓存（或等 TTL）
curl -X POST http://localhost:8080/dashboard/api/cache/refresh   # VM 上, 带 dashboard token

# 4. 验证
# - tier:vision 带图请求 → deepseek-v4-flash-vision-exp
# - tier:vision-light 带图请求 → glm-4.6v-flash
# - tier:light 纯文本 → glm-4.7-flash（1305 过载时 failover 到 qwen/glm-4-flash 属正常）
# - tier:strong 纯文本 → deepseek-v4-flash（不变; glm-5.3-flash 不再被文本跳过）
# - x-aimc-actual-* 响应头核对实际命中
```

**注意**：VM 上**不要**跑 `seed_model_groups.py`（先清后插会丢 siliconflow 同步的模型与审计历史），只用迁移脚本。seed 文件变更只服务全新环境。

### 6.4 上产观察项

1. glm-4.7-flash 1305 过载频率 → glm_bigmodel 账号 30s 冷却波纹是否波及 glm-5.3-flash 流量（RATE_LIMITED 分类已确认不误报余额不足）
2. glm-4.6v-flash 免费 vision 兜底命中率
3. vision→vision-light→light 新 fallback 链的实际降级路径

### 6.5 上产验证结果 (2026-09-03)

迁移在 VM ubuntu2401 执行完成（dry-run 19 项 → apply 全 OK → 刷缓存 6 渠道/113 模型），真实请求验证：

| # | 场景 | 实际命中 | 判定 |
|---|---|---|---|
| 1 | tier:vision + 图 | `glm-5.3-flash` @glm_bigmodel | ✅ 0 元领跑（见下方定位修正） |
| 2 | tier:vision-light + 图 | `glm-4.6v-flash` @glm_bigmodel | ✅ 免费视觉兜底生效 |
| 3 | tier:light + 文本 | glm-4.7-flash **429(1305)** → failover `qwen-qwen3.5-4b` @siliconflow | ✅ failover 链按设计工作 |
| 4 | tier:strong + 文本 | `deepseek-v4-flash` @deepseek_official | ✅ 不变 |
| 5 | tier:vision + 文本（加检） | `deepseek-v4-flash-vision-exp` @deepseek_official | ✅ **P0-3 收窄生效**：multimodal 的 5.3-flash 被跳过，vision 模态成员接住 |

- **观察项①当场闭环**：usage_logs 确认 glm-4.7-flash 429 → glm_01 账号 `last_error_type=rate_limited`、`needs_human=false`、冷却 30s 自愈，未误报余额不足。
- **vision-exp 定位修正**：生产价格快照为付费价（价格同 v4-flash，非 0 元），故带图场景 glm-5.3-flash（0 元）按 cheapest 正确领跑；vision-exp 实际定位是 **glm Lite 池耗尽时的付费视觉兜底**（场景 5 证明可用）。

---

*文档结束*
