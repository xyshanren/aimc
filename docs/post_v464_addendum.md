

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
| 单元测试 CI（GitHub Actions） | 0.3d | ⭐⭐⭐ | 高 |
| P2-17 Prometheus /metrics | 0.4d | ⭐⭐⭐ | 中 |
| P2-19 自动应用（基于 shadow log） | 0.5d | ⭐⭐ | 中（需 3-6 月 shadow log 累积） |
| Anthropic Messages API 端点（P2-20） | 0.5d | ⭐⭐ | 低（当前 OpenAI 协议为主） |
| SF cache 价格真值爬取（P1-13 v2） | 0.3d | ⭐ | 低（hardcode 53 行已覆盖主要用到的 model） |
| 单元测试覆盖率到 80% | 1.0d | ⭐⭐ | 中 |

---

**作者**: 用户 + agent 协作
**本节更新时间**: 2026-08-15
**相关 commit**: `5eab24a` (最新) ← `c7967a4` ← `f287a9d` ← `503f215` ← `88f072a` ← `d7114f9` ← `7cf09e8` ← `59deb96` ← `c284e5b` ← `ae8843b` ← `d67c9b4` ← `3ea4412` ← `a2a9d6b` ← `eec77d2` ← `038e1ff` ← `d36c384` ← `7a74176` ← `388c00e` ← `356d9f8` ← `df01e78` ← `c54dd48` ← `5f9711a`
