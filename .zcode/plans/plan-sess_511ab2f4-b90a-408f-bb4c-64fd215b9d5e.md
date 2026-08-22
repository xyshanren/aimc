# 下一轮计划：修 3 个 flaky 测试（UTC 时钟 bug）+ 路径 A 缓存注入优化 + 远期优化归档

## 任务 1：修复 3 个 flaky 测试（生产级 UTC/本地时钟不一致 bug）

### 根因（已实证复现）
- `db/models.py:322` `UsageLog.created_at` 用 `func.now()` = SQLite `CURRENT_TIMESTAMP` = **UTC**
- `gateway/quota.py:256` `check_user_quota` 用 `datetime.now()` = **本地时间（UTC+8）** 算日窗口
- 本地 00:00–08:00 期间 UTC 日期还是"昨天" → 当天用量 SUM=0 → **daily quota 漏判**（不拦截超限请求）
- 3 个测试失败是同一 bug 的测试侧表现（种子行 UTC 时间落在窗口外）；`job_health` 等表已经是 Python 本地时间写入，`usage_logs` 是唯一不一致的大表

### 修复
1. `db/models.py:322`：`created_at = Column(DateTime, default=func.now())` → `default=datetime.now`（Python 侧本地时间，与所有读取方一致：quota.py / dashboard/routes.py / 周报脚本全用本地 `datetime.now()`）
2. VM 一次性迁移：`UPDATE usage_logs SET created_at = datetime(created_at, '+8 hours')`（把存量 UTC 行拨到本地时间；偏移量从 `datetime.now() - datetime.utcnow()` 动态算，不硬编码 8）
3. 验证：单独跑 `test_v48_multi_user.py` 3 个失败测试 + 全量回归

## 任务 2：路径 A — prompt cache 注入优化（`gateway/proxy.py` `_inject_prompt_cache`）

### 现状问题
- OpenAI 风格只 hash **第一条** system message（循环带 break）
- 无 system 时 hash 空串 → 所有此类请求共享同一个全局 cache key
- 无条件**覆盖**调用方已传的 `prompt_cache_key`（Anthropic 分支却保留已有 cache_control，行为不对称）
- Anthropic 风格盲目给 `messages[:2]` 加 cache_control，不校验 role

### 改法
1. **分段 hash 稳定前缀**：`key = sha256(全量 system 拼接 + "\x1f" + 第一条非 system 消息)[:16]`
   - 多轮 agent loop：system + 首条 user 跨轮不变 → 同 key → 同 cache shard → 前缀命中
   - 每段截断 4096 字符（总 8KB 上限），避免超大 payload hash 开销
2. **无 system fallback**：用第一条 user 消息（消灭全局空串 key）
3. **保留调用方 key**：body 已有 `prompt_cache_key` 时直接返回不覆盖（跟 Anthropic 分支对称）
4. **Anthropic 分支 role 校验**：只给 system 角色消息 + 第一条 user 消息加 cache_control（跳过 assistant/tool）
5. **测试**：扩展 `tests/test_cache_inject.py`
   - 多条 system 全部参与 hash；无 system 时按首条 user 区分；调用方 key 保留；长内容截断等价；anthropic role 校验（[user, assistant, user] 只标第一条 user）
   - 更新受影响的现有断言（"无 system 用空串 hash" 一条行为已变）

## 任务 3：路径 B / C 归档到 `docs/dev/next-step-plan.md`

在 "V4.7+ 远期候选"（现第 133 行）扩写：
- **路径 B 响应级 prefix 缓存**：中等代价（2-3 天，新表 + 流式回放），仅固定 prompt 场景收益，流式透传原则有摩擦 → 远期
- **路径 C 语义缓存**：高代价（1-2 周+，embedding 库 + 向量存储 2-3 个重依赖），与轻量级定位冲突 → 不做，除非项目定位升级
- 注明路径 A 已在本轮完成

## 执行顺序

1. 任务 1 时钟修复（models.py 1 行 + 测试验证）
2. 任务 2 缓存注入优化（proxy.py + 测试）
3. 任务 3 文档更新
4. 全量回归 → commit/push
5. VM：git pull → 存量 usage_logs 时间戳迁移 → 重启 → 验证（quota 测试时段不再敏感 + 真实请求 cache key 生效）

## 风险说明
- 时钟迁移是单向 UPDATE（UTC→本地 +8h），执行前先在 VM 备份 data/aimc.db（cp 一个带时间戳的副本）
- 存量数据迁移后，dashboard "近 7 天" 统计边界会平移 8 小时（一次性，可接受）
- cache key 策略变更后，上游 cache shard 会重新分布（短暂的冷启动，无正确性影响）