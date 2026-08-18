# POST-V4.6.4 MERGE FIX GUIDE

> 用户在 2026-08-14 合并 `feat/intel-announcements-2026-08-13` 后发现并修复的
> 5 个 db 状态漂移问题 + 4 个修复脚本的使用顺序。
>
> **本指南适用版本**: aimc @ `2ab00f2` 及之后, 4 个 db 修复脚本已 commit
> (`9a14b71`)。

---

## 背景

aimc @ `450499c` 引入 model_id 命名归一化 (大写 → 小写, namespace 对齐),
但 `seed_model_groups.py` / `manual_import.py` / `db/normalizers.py` 集成不完整,
导致 merge 后 db 出现 5 个数据漂移问题。

`2ab00f2` 合并 `feat/intel-announcements-2026-08-13` 后, 漂移问题被触发为
端到端症状 (router 503, group 路由命中错等)。

---

## 5 个问题 + 4 个修复脚本

### 问题 1: `model_group_members.group_id` 类型不一致 → router 503

**症状**: 任何 `tier:*` / `scene:*` 路由返回 503, 直调 model 名正常 200。

**根因**: `model_group_members.group_id` 字段同时存 int 字符串 (`'1'`, `'2'`)
和 string (`'tier:flagship'`, `'scene:code'`)。router `_group_members_cache` 用
string key 查 int key 永远 miss。

**修复**: `scripts/fix_model_group_members_gid.py` 把 int 字符串转成对应 string。

```bash
python scripts/fix_model_group_members_gid.py            # dry-run
python scripts/fix_model_group_members_gid.py --apply   # 写 db
```

### 问题 2: `check_group_members_consistency.py` 误报 "0 active members"

**症状**: 跑 check 脚本说所有 group "0 active members", 实际 db 有 27 个 member。

**根因**: 跟问题 1 同一 bug 下游症状 — check 脚本 `active_members_per_group` 用
int key 累计, 查询时用 string key → 永远 0。

**修复**: 修问题 1 后此问题自动解决 (check 脚本读取 db, 修 group_id 类型后
check 逻辑才能查对)。

### 问题 3: `tier:flagship` 命中 minimax-m2.7 不是 minimax-m3

**症状**: `tier:flagship` 选 priority=120 的 m2.7, 不选 priority=100 的 m3。

**根因 (猜测)**: `gateway/router.py:1010` 附近对 `modality="multimodal"`
(m3) 的 special handling, 可能跟 `_get_current_price` / `_get_fuzzy_price`
顺序有关, 触发 fallback 选 m2.7。

**修复**: 暂无。Issue 已记录待项目侧排查。其他 group (tier:vision, tier:code,
scene:free) 命中**符合**设计, 行为正确。

**临时绕过**: tier:flagship 用 `tier:vision` 代替 (m3 multimodal 命中正确)。

### 问题 4: `seed_model_groups.py` 没建 `scene:free` group

**症状**: 跑 `seed_model_groups.py` 只 6 个 group, 没 `scene:free`。

**根因**: 450499c 删了 `scene:code` 但漏了 `scene:free` 重建, 后续 `seed_model_groups.py`
也没补。

**修复**: `scripts/redesign_groups_v2.py` 重设计 7 个 group 完整定义
(tier:flagship / strong / balanced / light / vision / code / scene:free),
全用真实可用的 model_id (基于 siliconflow 91 model + minimax 8 model)。

```bash
python scripts/redesign_groups_v2.py            # dry-run
python scripts/redesign_groups_v2.py --apply   # 写 db
```

### 问题 5: `minimax_compat` 渠道 price_snapshots 字段大写

**症状**: db 里 `minimax_compat.price_snapshots.model` 仍存大写 (`MiniMax-M3`),
跟 model_registry 小写 (`minimax-m3`) 不对齐, router fuzzy 部分工作但
`minimax-m3` (multimodal) 失败, 触发 503。

**根因**: 450499c commit message 提过"集成归一化到现有代码"但 `manual_import.py`
的 minimax cfg (1d011e2 才修) 仍用大写, 期间跑的部署在 price_snapshots 留了
8 个大写行。

**修复**: `scripts/normalize_minimax_prices.py` 把 8 个大写行小写化。

```bash
python scripts/normalize_minimax_prices.py            # dry-run
python scripts/normalize_minimax_prices.py --apply   # 写 db
```

---

## 完整 reset_db 之后的修复顺序

```bash
cd ~/aimc
source .venv/bin/activate

# 1. 标准 4 步部署 (项目侧 commit message 给的)
python scripts/seed_model_registry.py
python scripts/seed_model_groups.py
python scripts/manual_import.py
python scripts/refresh_siliconflow_models.py

# 2. 同步 siliconflow 91 个 model 到 model_registry (含 cleanup 18 虚标)
python scripts/sync_siliconflow_to_registry.py --apply --cleanup

# 3. 重建 7 个 model group + 27 个 member (修复问题 1, 4, 2 的一部分)
python scripts/redesign_groups_v2.py --apply

# 4. 修复 minimax_compat price_snapshots 8 个大写行 (修复问题 5)
python scripts/normalize_minimax_prices.py --apply

# 5. 修复 model_group_members.group_id 类型 (修复问题 1 残余)
python scripts/fix_model_group_members_gid.py --apply

# 6. 重启 service 让 router 缓存重建
sudo systemctl --user restart aimc

# 7. 端到端验证
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"tier:flagship","messages":[{"role":"user","content":"ping"}],"max_tokens":10}' \
  | grep -i x-aimc
# 期望: x-aimc-actual-channel: minimax_compat
# 实际: minimax-m2.7 (问题 3 未修, 但 200 OK)

curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"tier:flagship","messages":[{"role":"user","content":"ping"}],"max_tokens":10}' \
  | grep -i x-aimc
# 期望: x-aimc-actual-model: minimax-m3, x-aimc-actual-channel: minimax_compat

curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"tier:vision","messages":[{"role":"user","content":"ping"}],"max_tokens":10}' \
  | grep -i x-aimc
# 期望: x-aimc-actual-model: minimax-m3, x-aimc-actual-channel: minimax_compat
```

---

## 验证清单

跑完 1-7 步后, 验证:

```bash
# model_registry 应该有 95+ 个 model
python -c "import sqlite3; c=sqlite3.connect('data/aimc.db'); print(c.execute('select count(*) from model_registry').fetchone())"
# 期望: 90+ (95 = 30 旧 + 91 同步 - 18 虚标 - 8 重复等)

# model_group_members 应该 27 行
python -c "import sqlite3; c=sqlite3.connect('data/aimc.db'); print(c.execute('select count(*) from model_group_members where is_active=1').fetchone())"

# check_model_consistency 应该 0 ERROR
python scripts/check_model_consistency.py --check db
# 期望: 0 ERROR (之前 2 deepseek 老 model 也修了)

# check_group_members_consistency 应该 0 issues
python scripts/check_group_members_consistency.py
# 期望: 0 issues

# 端到端 9/9 HTTP 200
for cmd in "tier:flagship" "tier:strong" "tier:balanced" "tier:light" "tier:vision" "tier:code" "scene:free" "minimax-m3" "qwen-qwen3-vl-32b-instruct"; do
  curl -isS -X POST http://localhost:8080/v1/chat/completions \
    -H "Content-Type: application/json" \
    -o /dev/null -m 60 -w "[$cmd] HTTP=%{http_code}\n" \
    -d "{\"model\":\"$cmd\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":10}"
done
# 期望: 9 行 200
```

---

## 已知遗留问题 (不阻塞, 提给项目侧)

1. 5 个账号 ciphertext 解密失败 → 跟项目侧 master key 不一致, 未修 (不影响路由, 靠 active_status 过滤)
2. 6 个虚标 model 残留 (kimi-k3 / qwen-max 等) → 已通过本地 4 脚本清理
3. 项目侧 6 个 issue 草稿 → 已发；项目侧 v2 方案：4 commit (C1-C4) 在 fix/group-gid-routing-2026-08-14 分支 (待 push)
4. 硅基流动 raw_id vs model_registry 归一化错配 → 等项目侧 C4 schema 加 raw_model 字段
5. tier:flagship 命中 m2.7 不是 v4-pro → 等项目侧 C3 sort_key 加 member_priority tie-break
6. 本地 Ollama (qwen3-vl:4b) → 未接入 (之前决定不用本地模型)
7. dashboard 鉴权 → LAN 内无鉴权 (用户确认不处理)

当前端到端可用性
9/9 group + 直调 HTTP 200，service 健康跑着，db 干净 (check_model_consistency.py 0 ERROR)。

---

## 4 个修复脚本之间的关系

```
seed_model_registry.py    (项目侧已有, 30 个 model)
       │
       ▼
sync_siliconflow_to_registry.py --apply --cleanup   (91 个 + 删 18 虚标)
       │
       ▼
manual_import.py          (项目侧已有, 创建 channels)
       │
       ▼
refresh_siliconflow_models.py  (项目侧已有, 91 个 price_snapshot)
       │
       ▼
normalize_siliconflow_prices.py --apply   (8 个大写 → 小写)
       │
       ▼
seed_model_groups.py      (项目侧已有, 但漏 scene:free)
       │
       ▼
redesign_groups_v2.py --apply   (重建 7 group + 27 member)
       │
       ▼
fix_model_group_members_gid.py --apply  (group_id 类型统一)
       │
       ▼
systemctl --user restart aimc  (router cache 重建)
```

---

## 时间线

- 2026-08-12: 1005111 (dashboard-v2a) 引入 MiniMax 缩水 bug
- 2026-08-12: 4a2cfc5 (minimax-family-align) 修 family 数量
- 2026-08-13: 183ef25 (MiniMax 8 model 注册) 价格表大写
- 2026-08-13: 450499c (命名归一化) model_id 改小写
- 2026-08-13: 1d011e2 (manual_import 归一化) 修 minimax cfg
- 2026-08-13: 6251e6e (优惠情报) 加 7 source + 4 plan
- 2026-08-13: cb5acb0 (proxy 修复) 修 1005111 引入的 NameError
- 2026-08-14: 2ab00f2 (合并 feat/intel-announcements) 触发 5 个 db 漂移症状
- 2026-08-14: 9a14b71 (4 db 修复脚本) 本指南
- 2026-08-14: 5f9711a (Phase 0: merge fix-branch)
- 2026-08-14: c54dd48 (P0-1: ModelGroupMember FK + UNIQUE 强约束)
- 2026-08-14: df01e78 (P0-2: price_snapshots + model_registry schema 扩展)
- 2026-08-14: 356d9f8 (P0-3: 纯文本请求跳过 multimodal candidate)
- 2026-08-14: 388c00e (P0-4: router cache TTL + db mtime 监听)
- 2026-08-14: 7a74176 (P0-5: accounts.encryption_version + master_key 启动校验)
- 2026-08-14: d36c384 (P0-6: 多维限额模型)
- 2026-08-14: 038e1ff (P1-7: dashboard 鉴权 (跟 api_key 分开))
- 2026-08-14: eec77d2 (P1-12: channels api_base + api_style + supports_prompt_cache)
- 2026-08-14: a2a9d6b (P1-8: prompt cache 真正实现)
- 2026-08-14: 08a8002 (P0-3 follow-up: usage_logs 加 cached_tokens + SF cache 价格填充)
- 2026-08-14: 3ea4412 (阶段1: cache 节省成本可视化)
- 2026-08-14: d67c9b4 (方案A: 建议生成器 + 置信度 + shadow mode)
- 2026-08-14: ae8843b (方案A: 建议中心 UI + apply 脚本)
- 2026-08-14: c284e5b (方案A: 周报 + scheduler weekly trigger)
- 2026-08-14: 59deb96 (阶段2: 38→20 jobs + 数据新鲜度)
- 2026-08-14: 7cf09e8 (阶段3: score-based 路由)
- 2026-08-15: d7114f9 (P0-7: score weights dashboard 可调)
- 2026-08-15: 88f072a (P0-8: listener sync_engine 修复)
- 2026-08-15: 503f215 (阶段C: README 同步)
- 2026-08-15: c7967a4 (阶段B: SF 53 行真实价格)
- 2026-08-15: f287a9d (P1-15: 上游 429 failover)
- 2026-08-15: 5eab24a (C: 74 测试)
- 2026-08-15: 05fed73 (D: POST_V464_FIXES.md 同步)

---

## 2026-08-14~15 架构整改（追加章节，2026-08-15）

> 上一节"时间线"到 `9a14b71`（2026-08-14）。本节续 `5eab24a`（2026-08-15 测试套件）。

### 背景

`9a14b71` 后的 db 修复脚本（4 个）**只解决 db 数据漂移**，没解决架构层面的 5 类缺陷：
1. **`model_group_members.group_id` 双形**（int-string / string）→ FK + UNIQUE 没强约束
2. **`price_snapshots` 与 `model_registry` 命名空间错配**（raw_id vs mid）→ cache 价格只能估算
3. **router cache 与 db 不同步**→ "db 改必重启 service"
4. **`tier:flagship` 选 multimodal 拒纯文本**（路由选错 model modality）
5. **`AIMC_DASHBOARD_TOKEN` 没设**→ dashboard 在 LAN 内裸开

加上 `minimax TokenPlanMax 用量到上限`（2026-08-15 用户真实遇到），需要
**上游 429 自动 failover**。

### 14 个新 commit

| Commit | 阶段 | 关键改动 | 解决缺陷 |
|--------|------|---------|---------|
| `5f9711a` | Phase 0 | merge fix-branch (C1 group_id 容错 + C2 6 tier + C3 priority) | #1 部分 |
| `c54dd48` | P0-1 | ModelGroupMember FK + UNIQUE 约束 | #1 |
| `df01e78` | P0-2 | price_snapshots + model_registry schema 扩展 (raw_model_id + cached_input_price + supports_prompt_cache) | #2 |
| `356d9f8` | P0-3 | 纯文本请求跳过 multimodal candidate | #4 |
| `388c00e` | P0-4 | router cache TTL (60s) + db mtime 监听 | #3 |
| `7a74176` | P0-5 | accounts.encryption_version + master_key 启动校验 (64 hex) | 安全 |
| `d36c384` | P0-6 | 多维限额 (account_quotas + quota_check, 7 维度) | — |
| `038e1ff` | P1-7 | dashboard 鉴权 (AIMC_DASHBOARD_TOKEN) | #5 |
| `eec77d2` | P1-12 | channels api_base + api_style + supports_prompt_cache | — |
| `a2a9d6b` | P1-8 | prompt cache 真正实现 (cache_control + prompt_cache_key) | — |
| `3ea4412` | 阶段1 | cache 节省成本可视化 (`/api/cache/stats` + dashboard 卡片) | — |
| `d67c9b4` | 方案A | 模型/分组 调整建议生成器 (4 model + 4 group 规则 + 置信度) | — |
| `ae8843b` | 方案A | dashboard "建议中心" UI + apply 脚本 | — |
| `c284e5b` | 方案A | 周报集成 + scheduler weekly trigger | — |
| `59deb96` | 阶段2 | 38→20 jobs + 数据新鲜度卡片 | — |
| `7cf09e8` | 阶段3 | router score-based 多账户选择 | — |
| `d7114f9` | P0-7 | score weights dashboard 可调 | — |
| `88f072a` | P0-8 | listener sync_engine 修复 (greenlet_spawn) | — |
| `503f215` | 阶段C | README 同步 | — |
| `c7967a4` | 阶段B | SF 53 个 model 真实价格 (input/output) | — |
| `f287a9d` | P1-15 | 上游 429 failover + balance_cny 未 sync 不拦截 | minimax 真实问题 |
| `5eab24a` | 阶段D | 测试套件 (74 tests) | 回归保护 |

### 新 migration 执行顺序（reset_db 后）

```bash
cd ~/aimc
source .venv/bin/activate

# 老的 4 个 (a62deb8 / 9a14b71 时代)
python scripts/seed_model_registry.py
python scripts/seed_model_groups.py
python scripts/manual_import.py
python scripts/refresh_siliconflow_models.py

# 新增的 7 个 (按依赖顺序)
python db/migrations/2026_08_14_p01_mgm_fk_unique.py --apply   # 清理 + 加 FK/UNIQUE
python db/migrations/2026_08_14_p02_price_cache_schema.py --apply  # 加 cache 价列 + raw_model_id
python db/migrations/2026_08_14_p05_encryption_version.py --apply  # 加 encryption_version 列
python db/migrations/2026_08_14_p06_account_quotas.py --apply      # 建限额表
python db/migrations/2026_08_14_p_suggestions_tables.py --apply   # 建建议表
python db/migrations/2026_08_14_p112_channel_api_base.py --apply  # 加 channels api_base 等
python db/migrations/2026_08_14_p03followup_cache_tokens.py --apply  # usage_logs 加 cached_tokens

# 新 migration
python db/migrations/2026_08_14_p2_scheduler_cleanup.py --apply  # 删旧 crawl_* jobs
python db/migrations/2026_08_14_p2_job_health_table.py --apply   # 建 job_health 表

# sync 价格 (阶段B)
python scripts/sync_siliconflow_cache_prices.py --apply  # 老的 9 行 cache 价 (兼容)
python scripts/sync_siliconflow_prices.py --apply        # 新的 53 行 input/output/cache

# 跑测试 (阶段D)
pip install pytest pytest-asyncio
python -m pytest tests/ -v

# 重启
systemctl --user restart aimc
```

### 验证清单（新增）

```bash
# 1. db 结构 + FK
sqlite3 data/aimc.db "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%quota%' OR name LIKE '%suggestion%' OR name LIKE '%job_health%'"
# 期望: account_quotas, account_quota_usage, model_change_suggestions, group_change_suggestions, job_health

# 2. 限额配置 (P0-6)
sqlite3 data/aimc.db "SELECT COUNT(*) FROM account_quotas"
# 期望: 18 (6 channel x 3 quota)

# 3. SF 价格 (阶段B)
sqlite3 data/aimc.db "SELECT COUNT(*) FROM price_snapshots WHERE channel_id=3 AND input_price IS NOT NULL"
# 期望: 53 (input_price + output_price)

# 4. 测试套件 (阶段D)
python -m pytest tests/ -v
# 期望: 74 passed

# 5. dashboard 端到端 (P1-7)
curl -s http://127.0.0.1:8080/dashboard/api/data-freshness | python3 -m json.tool | head -20
# 期望: jobs 列表含 batch_crawl_* + refresh_route_cache, last_success_at 距今 < 1d

# 6. 上游 429 failover (P1-15)
curl -sS -X POST http://127.0.0.1:8080/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer $AIMC_API_KEY" -d '{"model":"tier:flagship","messages":[{"role":"user","content":"hi"}],"max_tokens":3}' | head -c 200
# 期望: 200 (即使 minimax m2.7 429, 也会 failover 到 deepseek 等其他渠道)

# 7. 实时 cache 节省
curl -s http://127.0.0.1:8080/dashboard/api/cache/stats?days=7 | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(f\"hit_rate={d['hit_rate_pct']}%, saved={d['saved_cost_cny']}元\")"
# 期望: hit_rate > 0%, saved > 0 元 (SF Qwen3-32B 实际 cache hit)

# 8. 建议中心
curl -s http://127.0.0.1:8080/dashboard/api/suggestions?status=pending | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(f\"pending={len(d['model'])+len(d['group'])} 条\")"
# 期望: 0 条 (P0-3 follow-up 已经 apply 过; 或者 pending=3+)
```

### 新增 dashboard 卡片位置（index.html 顺序）

1. **模型分组建议** (方案A, 最早)
2. **Prompt Cache 节省** (P1-8, 早期)
3. **数据新鲜度** (阶段2, 任务健康)
4. **用量统计** (原有用)
5. **组路由命中率** (Phase 3.A)
6. **价格波动 / 跨源差异 / 待确认** (原有)

### 已知遗留（新增 / 更新）

| 项 | 状态 | 备注 |
|----|------|------|
| 5 个账号 ciphertext 解密失败 | ⚠️ 仍未修 | 不影响路由（active_status 过滤） |
| minimax TokenPlan 429 时无第二渠道的 model（如 m3） | ⚠️ P1-15 透传 429 | cooldown 60s 自动恢复；建议升级 Plan |
| balance_cny 维度 sync 脚本未做 | ⚠️ | sync 脚本待 P2；目前不影响（未 sync 不过滤） |
| SF cache 命中价（cache_input_price）是估算 | ⚠️ `* 0.1` 比例 | SF 网页/API 不公开真实 cache 价；只能估算 |
| 上游真实余额同步 | ⚠️ 待 P2 | deepseek 提供余额查询 API，可加 sync job |
| 6 个旧 crawl_* jobs 已删（migration 已跑） | ✅ | scheduler 现在 20 个 jobs |
| ~~score weights 写死 hardcode~~ | ✅ | 现在 `settings_runtime.router_score_weights` 可改 |

### 远期 roadmap（参考）

| 项 | 工作量 | 价值 | 推荐 |
|----|--------|------|------|
| 单元测试 CI（GitHub Actions） | 0.3d | �⭐⭐ | 高 |
| P2-17 Prometheus /metrics | 0.4d | ⭐⭐⭐ | 中 |
| P2-19 自动应用（基于 shadow log） | 0.5d | ⭐⭐ | 中（需 3-6 月 shadow log 累积） |
| Anthropic Messages API 端点（P2-20） | 0.5d | ⭐⭐ | 低（当前 OpenAI 协议为主） |
| SF cache 价格真值爬取（P1-13 v2） | 0.3d | ⭐ | 低（hardcode 53 行已覆盖主要用到的 model） |
| 单元测试覆盖率到 80% | 1.0d | ⭐⭐ | 中 |

---

**作者**: 用户 + agent 协作
**本节更新时间**: 2026-08-15
**相关 commit**: `5eab24a` (最新) ← `c7967a4` ← `f287a9d` ← `503f215` ← `88f072a` ← `d7114f9` ← `7cf09e8` ← `59deb96` ← `c284e5b` ← `ae8843b` ← `d67c9b4` ← `3ea4412` ← `a2a9d6b` ← `eec77d2` ← `038e1ff` ← `d36c384` ← `7a74176` ← `388c00e` ← `356d9f8` ← `df01e78` ← `c54dd48` ← `5f9711a`
