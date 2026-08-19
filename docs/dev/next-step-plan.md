# 下一步编码计划 (2026-08-19)

> 承接 P0-1/2/3 修复 (commit `f6c841c` + merge `43d8900` 已推送)。
> 本计划聚焦历史测试债 + P1 审计系统 + 路由正确性。

---

## 当前测试基线

全量 `pytest tests/` 结果: **108 failed, 287 passed** (合并后)。
经逐文件单独运行排查, 108 个失败分两类:

| 类别 | 数量 | 说明 |
|------|------|------|
| **测试隔离污染** (单独跑全过, 全量跑挂) | 62 | 8 个测试文件, 根因是 `sys.modules` mock 泄漏 + 无 table-create fixture |
| **真实失败** (单独跑也挂) | 37 | schema 漂移 / 函数重命名 / 硬编码 count 过时 / 需运行服务 |

P0 新增的 15 个测试全过, 无回归。

---

## P1: 测试隔离治理 (62 个污染失败 → 0)

**根因**: 多个测试文件在模块顶层调 `_install_mock_config()` 往 `sys.modules["config"]`
塞 mock settings, 但不还原。后续测试 `import config` 拿到的是 mock, 导致
`settings.aimc_master_key` / `settings.db_path` 错乱 → crypto 解密失败 / scheduler
路径错。

**受影响文件** (单独跑全过, 全量跑挂):
- `test_v46_dashboard_accounts.py` (16)
- `test_usage_log_parse.py` (11)
- `test_phase3_6_api_models.py` (10)
- `test_v44_alerts.py` (9)
- `test_v44_cli.py` (7)
- `test_v46_score_routing.py` (4)
- `test_phase3_5_verification.py` (4)
- `test_v44_feedback_summary.py` (1)

**修复方案**: 在 `tests/conftest.py` 加 autouse fixture, 每个 test 后还原
`sys.modules` 快照:

```python
@pytest.fixture(autouse=True)
def _isolate_sys_modules():
    """防止测试往 sys.modules 塞 mock 后泄漏到后续测试"""
    snapshot = dict(sys.modules)
    yield
    # 删掉测试中新加的模块, 还原被改的
    for key in list(sys.modules):
        if key not in snapshot:
            del sys.modules[key]
    sys.modules.update(snapshot)
```

**验收**: 这 8 个文件全量跑全过 (62 → 0)。

---

## P2: 真实失败修复 (37 个, 按根因分组)

### P2-A: schema 漂移 — `test_quota_check.py` (8 个)

**症状**: `no such table: account_quotas` / `table channels has no column named api_base`

**根因**: 测试注释写 "Base.metadata.create_all 在 conftest 自动跑", 但 conftest
实际没有这个 fixture。测试用 prod engine (`db.engine.engine`) 但没建表。

**修复**: 给 `test_quota_check.py` 加 tmp SQLite + `create_all` fixture
(参照 `test_p0_usage_fixes.py` 的 `tmp_db` 模式), monkeypatch
`db.engine.async_session_factory`。

### P2-B: 函数重命名 — `test_proxy_body_rewrite.py` (7 个)

**症状**: `ImportError: cannot import name '_maybe_rewrite_body_model'`

**根因**: 该函数已被重构成 `forward_request` 内联代码 (proxy.py:210-223)。
测试测的是已不存在的私有函数。

**修复**: 两个选择 —
1. **更新测试** (推荐): 改测 `forward_request` 的 body 替换行为 (用 mock upstream)
2. **抽取回函数**: 把内联逻辑抽回 `_rewrite_body_model(body, actual_model)` 方便单测

倾向选项 2 — 抽函数代价小, 测试不用大改, 且 `forward_request` 更可读。

### P2-C: 函数重命名 — `test_v46_cooldown.py` (1 个)

**症状**: `module 'gateway.proxy' has no attribute '_update_cooldown'`

**根因**: cooldown 逻辑已迁到 `gateway.cooldown.cooldown_manager.mark_failure`
(proxy.py:393)。测试还在 mock 旧函数。

**修复**: 更新 mock 目标到 `gateway.cooldown.cooldown_manager`。

### P2-D: 硬编码 count 过时 (8 个)

| 文件 | 失败数 | 期望 → 实际 |
|------|--------|------------|
| `test_phase3_1_sources.py` | 3 | 12 源 → 34 |
| `test_phase3_6_extra_sources.py` | 2 | 20 源 → 34 |
| `test_qianwenai_source.py` | 2 | 20 源 → 34 |
| `test_v46_multi_account.py` | 1 | 9 provider → 10 |

**根因**: 上游持续加 source (现 34 个 / 15 provider), 测试硬编码旧 count。

**修复**: 更新断言到当前值, 或改用 `>= N` 语义断言避免未来再碎。

### P2-E: 需运行服务 — `test_phase2_e2e.py` (4 个)

**症状**: `httpx.ConnectError: 目标计算机积极拒绝` (连 localhost:8080)

**根因**: 真实 e2e 测试, 需要先 `uvicorn` 起服务。

**修复**: 加 `pytest.mark.integration` marker, 默认 skip, CI 里手动跑。
或改用 `httpx.MockTransport` mock 掉上游。

### P2-F: FastAPI import 污染 — `test_phase4_e3_cross_source.py` (4 个)

**症状**: `ImportError: cannot import name 'Request' from 'fastapi'`

**根因**: 同 P1 — 某个前置测试往 `sys.modules["fastapi"]` 塞了残缺 mock,
后续 `from fastapi import Request` 失败。P1 的 sys.modules 隔离 fixture
应连带修复。

### P2-G: 路由逻辑 — `test_null_price_routing.py` (2 个)

**症状**: 应选最便宜的 qwen (0.8 元), 实际选 `siliconflow_aggregator`

**根因**: 可能是 `siliconflow_aggregator` 有 score_bonus 加成, 或 null price
过滤逻辑变了。需排查 `select_best_channel` 排序。

**修复**: 排查 router 排序逻辑, 确认是 score_bonus 合理覆盖价格, 还是 bug。

### P2-H: cooldown e2e — `test_v46_e2e.py` (2 个)

**症状**: `_01 应已进入 cooldown` (assert False)

**根因**: cooldown 触发条件可能变了 (P1-15 只对 429 标 cooldown, 旧测试可能
期望 5xx 也标)。需对齐 cooldown 触发条件。

### P2-I: 浮点精度 — `test_v46_3_benchmark.py` (1 个)

**症状**: `assert 79.99999999999999 == 80.0`

**修复**: 改 `pytest.approx(80.0)`。

---

## P3: 审计系统 (从 P0 轮延后)

原审查建议里的 "审计系统缺失" 项 — 记录每次 API 调用的 caller 身份。
当前 `usage_logs` 表无 caller 字段, 无法按调用方统计用量。

**方案**:
1. `UsageLog` 加 `caller_id` 字段 (从 `X-AIMC-Caller` header 或 API key 推导)
2. middleware 透传 caller_id 到 proxy
3. `_write_usage_log` 写入 caller_id
4. Dashboard 加按 caller 聚合视图

**优先级**: P3, 本轮不做, 待 P1/P2 清完再排。

---

## 执行顺序

1. **P1 conftest 隔离 fixture** (1 处改动, 修 62 个) — 先做, 立即见效
2. **P2-B 抽回 `_rewrite_body_model`** (修 7 个)
3. **P2-A quota_check tmp_db fixture** (修 8 个)
4. **P2-D 更新硬编码 count** (修 8 个)
5. **P2-C cooldown mock 目标** (修 1 个)
6. **P2-I 浮点 approx** (修 1 个)
7. **P2-E integration marker** (修 4 个, 标 skip)
8. **P2-G null_price 路由排查** (需调研)
9. **P2-H cooldown e2e 排查** (需调研)
10. **P2-F** 预期被 P1 连带修复, 跑完验证

预期: P1+P2-A~I 完成后, 108 → ~4 (仅剩需调研的 P2-G/H)。

---

## 已完成

- ✅ P0-1: streaming usage 记录 (尾部缓冲扫描)
- ✅ P0-2: cache token 入库 (3 字段持久化)
- ✅ P0-3: model 名守卫 (组路由 `_actual_model` 残留污染)
- ✅ 对齐上游 V4.6.4 asyncio 修复 (`get_running_loop` 替代 `get_event_loop`)
- ✅ merge 冲突解决 + 推送 (`43d8900`)
