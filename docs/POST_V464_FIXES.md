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
  -d '{"model":"tier:vision","messages":[{"role":"user","content":"ping"}],"max_tokens":10}' \
  | grep -i x-aimc
# 期望: x-aimc-actual-model: minimax-m3, x-aimc-actual-channel: minimax_compat

curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"minimax-m3","messages":[{"role":"user","content":"ping"}],"max_tokens":10}' \
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

1. `gateway/router.py` 的 `model_group_members.group_id` cache key 类型
   不一致 (问题 1, 应该在 router 层适配, 不只是 db 修复)
2. `tier:flagship` 选 m2.7 不是 m3 (问题 3, router multimodal 处理)
3. `seed_model_groups.py` 仍漏 `scene:free` (问题 4, 永久修复)
4. `manual_import.py` 的 `deepseek_official` cfg 仍引用
   `deepseek-chat` / `deepseek-reasoner` 老 model (已 commit `1d011e2`
   修了 deepseek 部分, 但 price_snapshots 里仍有 2 个老行)
5. `check_model_consistency.py` 仍报 2 个 deepseek 老 model ERROR
   (需 `cleanup_deepseek_old_prices.py` 类似脚本)

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
normalize_minimax_prices.py --apply   (8 个大写 → 小写)
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

---

**作者**: 用户 + agent 协作
**最后更新**: 2026-08-14
**相关 commit**: `9a14b71`
