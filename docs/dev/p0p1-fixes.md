# P0/P1 早期修复（历史归档）

> **本文档已归档**（2026-08-15 文档重组时迁移到 docs/dev/p0p1-fixes.md）。
> 新内容见 [post-merge-fixes.md](post-merge-fixes.md)（涵盖全部 14 commit 整改）。

本文档保留 2026-08-14 早期 P0/P1 修复的细节（`P0P1_FIXES_2026_08_14.md`），便于溯源。

## P0/P1 修复（4 个 db 脚本）

`9a14b71` commit 含 4 个 db 修复脚本（`scripts/`）：

| 脚本 | 作用 |
|------|------|
| `fix_model_group_members_gid.py` | 把 `model_group_members.group_id` int-string 转 string |
| `normalize_minimax_prices.py` | 把 minimax_compat 渠道 price_snapshots 大写改小写 |
| `redesign_groups_v2.py` | 重建 7 个 model_groups（含 scene:free）+ 27 个 member |
| `sync_siliconflow_to_registry.py` | 拉硅基流动 91 个 model 到 model_registry（含清理 18 虚标） |

## 4 个脚本之间的关系

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

完整 reset_db 后的执行顺序和验证清单见 [post-merge-fixes.md](post-merge-fixes.md)。

## 历史时间线（2026-08-12 ~ 2026-08-13）

- 2026-08-12: 1005111 (dashboard-v2a) 引入 MiniMax 缩水 bug
- 2026-08-12: 4a2cfc5 (minimax-family-align) 修 family 数量
- 2026-08-13: 183ef25 (MiniMax 8 model 注册) 价格表大写
- 2026-08-13: 450499c (命名归一化) model_id 改小写
- 2026-08-13: 1d011e2 (manual_import 归一化) 修 minimax cfg
- 2026-08-13: 6251e6e (优惠情报) 加 7 source + 4 plan
- 2026-08-13: cb5acb0 (proxy 修复) 修 1005111 引入的 NameError
- 2026-08-14: 2ab00f2 (合并 feat/intel-announcements) 触发 5 个 db 漂移症状
- 2026-08-14: 9a14b71 (4 db 修复脚本) 本文档

## 相关 commit

`9a14b71` (P0P1 修复) → `5f9711a` (Phase 0: merge fix-branch) → 后续 14 commit 整改 → 详见 [post-merge-fixes.md](post-merge-fixes.md)。

## 已知遗留（早期标记）

| 项 | 状态 |
|----|------|
| 5 个账号 ciphertext 解密失败 | 未修（不影响路由）|
| minimax TokenPlan 耗尽 | 未处理（后续 P1-15 修复）|
| 6 个虚标 model 残留 | 已通过本地 4 脚本清理 |
| 项目侧 6 个 issue 草稿 | 已发；项目侧 v2 方案：4 commit (C1-C4) 在 fix/group-gid-routing-2026-08-14 分支（待 push）|
| 硅基流动 raw_id vs model_registry 归一化错配 | 等项目侧 C4 schema 加 raw_model 字段 |
| tier:flagship 命中 m2.7 不是 v4-pro | 等项目侧 C3 sort_key 加 member_priority tie-break |
| 本地 Ollama (qwen3-vl:4b) | 未接入（之前决定不用本地模型）|
| dashboard 鉴权 | LAN 内无鉴权（用户确认不处理）|

**当前端到端可用性**：9/9 group + 直调 HTTP 200，service 健康跑着，db 干净。

---

**作者**：用户 + agent 协作
**最后更新**：2026-08-14
**相关 commit**：`9a14b71`
