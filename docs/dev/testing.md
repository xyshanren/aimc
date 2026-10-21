# 测试规范

> 受众：所有改代码的开发者
> 前提：[setup.md](setup.md)（本地跑起来）+ [conventions.md §测试规范](conventions.md#测试规范)

## 1. 跑测试

```bash
# 全部 (1037 个 test, 基线 2026-10-22: 全绿; V6.18 400 failover 换路 + 失败分类落库 + 失败链落库)
python -m pytest tests/ -q

# 单文件
python -m pytest tests/test_cache_inject.py -v

# 指定 test
python -m pytest tests/test_quota_check.py::test_balance_cny_unsync_passes -v

# 跑 + 看 coverage (可选)
pip install pytest-cov
python -m pytest tests/ --cov=gateway --cov=intelligence --cov-report=term-missing
```

pytest-asyncio mode 已在 `pyproject.toml` 配置（`asyncio_mode = "auto"`），async test 自动运行。

## 2. 现有测试结构（1037 个，114 个 test 文件 + conftest）

> 注：下表为历史版本逐文件明细（数字已甩旧），当前以全量基线为准。
> V6.x (2026-09-04) 起全量全绿 — 此前 4 个存量 e2e 失败
> （`test_phase2_e2e` ×2 / `test_v46_e2e` ×2，FK 子表清理 + mock settings +
> V4.9 冷却语义三重漂移）已修复，不再有"已知失败"白名单。

| 文件 | 测试数 | 测试内容 | 对应 commit |
|------|--------|---------|------------|
| `test_multimodal_skip.py` | 11 | `_has_multimodal_content` case | P0-3 |
| `test_score_based.py` | 16 | score 5 维加权公式 | P0-7 + 阶段3 |
| `test_quota_check.py` | 8 | `check_account_quota`（含 P1-15 balance_cny 修复）| P0-6 + P1-15 |
| `test_cache_inject.py` | 15 | `_inject_prompt_cache`（含路径 A 稳定前缀 hash）| P1-8 + P3 路径 A |
| `test_usage_log_parse.py` | 14 | cached_tokens 解析 + group_id 归一化 | P0-3-followup |
| `test_sf_prices.py` | 11 | `find_sf_prices` | 阶段B |
| `test_group_id_fk.py` | 5 | FK + UNIQUE 约束 | P0-1 |
| `test_p0_usage_fixes.py` | 15 | streaming usage 记录 + cache token 入库 | P0-1/2/3 |
| `test_vision_routing.py` | 19 | tier:vision 400 专项修复（5 fixes）| P2-A |
| `test_drop_params.py` | 19 | drop_params 按 channel 剥离上游参数 | P2-B |
| `test_proxy_body_rewrite.py` | 7 | body.model 改写 + fuzzy 匹配 | V4.6.4 |
| `test_thinking_strip.py` | 10 | thinking 字段剥离 | V4.7 |
| `test_null_price_routing.py` | 3 | 聚合渠道价格 999 兜底 + 模糊匹配 | cadddb5 |
| `test_router_member_priority.py` | 5 | 组内 member priority 排序 | P0-1 |
| `test_group_members_consistency.py` | 6 | group member 一致性检查 | P0-1 |
| `test_cleanup_inactive_members.py` | 4 | 失效 member 清理 | P2-A |
| `test_models_api.py` | 6 | `/v1/models` 端点 | 阶段3 |
| `test_model_consistency.py` | 7 | model_registry 一致性 | P0-2 |
| `test_model_sync_auto_group.py` | 6 | SF 同步 + 自动分组 | P2-A |
| `test_migrate_price_snapshots.py` | 9 | price_snapshots 迁移 | P0-2 |
| `test_pricing_sources.py` | 5 | 定价源解析 | 阶段B |
| `test_phase2_e2e.py` | 4 | e2e（MockTransport 化，CI 可跑）| P3-1 |
| `test_phase3_*.py` (6 个) | 73 | 情报采集层（sources/parser/sync/pricing/verification/api）| 阶段2-3 |
| `test_phase3e_headers.py` | 7 | 响应头 X-AIMC-* | 阶段3 |
| `test_phase4_*.py` (4 个) | 44 | 建议审批 / 价格告警 / 跨源验证 / 软删除 | Phase 4 |
| `test_v44_*.py` (3 个) | 27 | 告警 / CLI / 反馈汇总 | V4.4 |
| `test_v45_reports.py` | 7 | 周报生成 | V4.5 |
| `test_v46_*.py` (5 个) | 51 | cooldown / 多账号 / score 路由 / dashboard / e2e / benchmark | V4.6 |
| `test_v48_multi_user.py` | 18 | 多用户鉴权 + caller_id + 个人限额 | V4.8 |
| `test_v48_cli.py` | 10 | add_user_key / list_user_keys CLI | V4.8 |
| `test_v48_dashboard.py` | 6 | per-user 用量 + Key 管理 Dashboard | V4.8 |
| `test_analyze_usage.py` | 13 | 稳定期使用分析 | V4.7+ |
| `test_qianwenai_source.py` | 6 | 千问 AI 源采集 | 阶段2 |
| `test_v486_channel_check.py` | 11 | model_sync 入库前渠道可用性校验 | V4.8.6 |
| `test_v486_pending_dedup.py` | 9 | pending 行 upsert 去重 | V4.8.6 |
| `test_v486_provider_tracking.py` | 12 | log_capability_change provider 参数 | V4.8.6 |
| `test_v487_auto_drop_params.py` | 15 | 上游 400 自动学习 drop_params | V4.8.7 |
| `test_v487_time_pricing.py` | 72 | 分时段定价（工作日/周末/非高峰） | V4.8.7 |
| `test_v487_cooldown_429.py` | 54 | 429 失败链 + cooldown API + stream 429 | V4.8.7 |
| `test_v631_cooldown_events.py` | 5 | pending-summary exclude_transient / cooldown_events 落库去重 / cooldown-events 端点过滤 | V6.3.1 |
| `test_phase3_5_verification.py` | 4 | 情报采集 → registry/group 闭环验证 | 阶段3-5 |
| `test_v67_stream_failover.py` | 9 | 流式 failover（429 换候选成功 / 终态改写 rate_limit_error / 真配额不改写 / 流式成功不被包裹）+ classify 限流语义兜底 | V6.7 |

**总计**：94 个 test 文件（+conftest）/ 883 个 test / ~7 分钟跑通（V6.10 基线 2026-09-20 全绿; 表内逐文件数字为历史版本快照）

> V6.10 新增：`test_taotoken_balance.py`（9：quarantine 模型桶隔离 / legacy 兼容 / 大请求收敛 taotoken / 组路由 LRU 轮换 / 隔离接棒）、`test_v610_first_token_ms.py`（4：TTFT 落库 / 默认 NULL / 透传 / 错误流）、`test_v610_perf_endpoint.py`（2：perf 分位与 eff tok/s / 渠道聚合与状态过滤）。

## 3. 写新测试

### 选风格

**优先：纯函数测试**（无 DB / 网络）：

```python
# tests/test_xxx.py
def test_my_function():
    assert my_function("input") == "expected output"
```

**次选：async + in-memory DB**（验证 ORM + SQL 逻辑）：

```python
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

@pytest.fixture
async def fresh_session():
    """每个 test 一个新 in-memory SQLite"""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(eng) as s:
        yield s
    await eng.dispose()

async def test_my_db_op(fresh_session):
    # ... 用 fresh_session 操作
```

**最后：真 tmp db 文件**（需要 FK + 触发器等 SQLite 特性）：

```python
@pytest.fixture
def db_path(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(eng)
    # 跑 migration (如 FK trigger)
    yield tmp_path / "test.db"
```

### 测试结构

```python
# tests/test_my_module.py
"""<一句话描述>
覆盖: <commit hash 列表>
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

from gateway.my_module import my_function


def test_simple_case():
    """<一句话描述测试场景>"""
    assert my_function("input") == "expected"


def test_edge_case():
    """边界条件"""
    assert my_function("") == ""  # 或其他合理预期


async def test_async_case(fresh_session):
    """async 用例 (auto mode 自动 await)"""
    result = await my_async_function(fresh_session, ...)
    assert result == "expected"
```

### 命名约定

- 函数：`test_<行为>_<预期结果>` 或 `test_<场景>`
- 文件：`test_<被测模块>.py`
- Fixture：`fresh_<东西>` / `mem_<东西>`（每次新建）

### 不要

- ❌ 跳过测试（`@pytest.mark.skip` 不写注释）
- ❌ mock 整个 SQLAlchemy（用真 db 测试）
- ❌ `assert True` 占位（必须有具体断言）
- ❌ 测试依赖外部网络 / 真实 API

## 4. 调试失败的测试

```bash
# 单个 test -v 看输出
python -m pytest tests/test_xxx.py::test_specific -v -s

# 启动 pdb
python -m pytest tests/test_xxx.py::test_specific --pdb

# 看 print 输出
python -m pytest tests/test_xxx.py::test_specific -s
```

## 5. CI（待 P2-16）

e2e 已 MockTransport 化无网络依赖（P3-1），CI 可直接跑。CI 平台集成（GitHub Actions / Gitee Go）仍待后续：

```yaml
# .github/workflows/test.yml (待做)
name: test
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt pytest pytest-asyncio
      - run: pytest tests/ -v
```

详见 [archive/P2-16 CI 集成](../archive/competitive-research.md)（TODO）。

## 6. 覆盖率目标

| 模块 | 目标 |
|------|------|
| `gateway/router.py` | ≥ 80%（路由决策最复杂）|
| `gateway/proxy.py` | ≥ 70%（含 cache 注入、模型改写）|
| `gateway/quota.py` | ≥ 90%（限额逻辑简单）|
| `intelligence/analytics/suggest_changes.py` | ≥ 80%（建议规则）|
| `scripts/sync_*.py` | ≥ 50%（数据迁移脚本）|

## 7. 写测试的小技巧

### 用 parametrize 跑多个 case

```python
@pytest.mark.parametrize("input,expected", [
    ("纯文本", False),
    ("image_url", True),
    ("audio", True),
    ("", False),
])
def test_multimodal(input, expected):
    assert _has_multimodal_content(input) is expected
```

### 用 fixture 复用

```python
@pytest.fixture
def mock_account():
    return {"id": 1, "name": "test", "channel_id": 1, "status": "active"}

def test_check_account(mock_account):
    result = check(mock_account)
    assert result["healthy"] is True
```

### 异步 fixture + auto mode

```python
# pyproject.toml: asyncio_mode = "auto"
# async fixture 自动运行, async test 自动 await

@pytest.fixture
async def db_session():
    async with async_session_factory() as s:
        yield s

async def test_query(db_session):
    result = await db_session.execute(...)
```

### 测异常

```python
import pytest

def test_bad_input_raises():
    with pytest.raises(ValueError, match="invalid"):
        my_function("bad")
```

### 测边界

```python
def test_zero():
    assert compute_quota_ratio(0, 100) == 0

def test_negative():
    # q_remaining < 0 不应该出现, 但兜底逻辑要测
    with pytest.raises(ZeroDivisionError):
        compute_quota_ratio(0, 0)  # q_total=0 -> 除零 (测试边界)
```

## 8. 重构 + 测试

重构前的安全网：

```bash
# 1. 看现有测试覆盖率
python -m pytest tests/ --cov=gateway --cov-report=term-missing | head -30

# 2. 重构某函数 (例如 score 公式)
# 3. 跑测试
python -m pytest tests/test_xxx.py -v

# 4. 如果测试仍过, 重构安全
# 5. 如果测试挂了, 重构有 bug, 修
```

**铁律**：重构时**不改测试**。如果需要改测试，说明行为变化，应该新加 commit 而不是混在重构 commit 里。

## 9. CI 现状（2026-08-15）

- ✅ 本地 547 tests 全过
- ❌ GitHub Actions / Gitee Go 集成（个人项目无必要）
- ❌ Coverage 报告（可选）

详见 [dev/conventions.md §CI 检查清单](conventions.md#pr-检查清单)。
