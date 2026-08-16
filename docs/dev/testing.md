# 测试规范

> 受众：所有改代码的开发者
> 前提：[setup.md](setup.md)（本地跑起来）+ [conventions.md §测试规范](conventions.md#测试规范)

## 1. 跑测试

```bash
# 全部 (74 个 test)
python -m pytest tests/ -v

# 单文件
python -m pytest tests/test_cache_inject.py -v

# 指定 test
python -m pytest tests/test_quota_check.py::test_balance_cny_unsync_passes -v

# 跑 + 看 coverage (可选)
pip install pytest-cov
python -m pytest tests/ --cov=gateway --cov=intelligence --cov-report=term-missing
```

pytest-asyncio mode 已在 `pyproject.toml` 配置（`asyncio_mode = "auto"`），async test 自动运行。

## 2. 现有测试结构（74 个）

| 文件 | 测试内容 | 对应 commit |
|------|---------|------------|
| `test_multimodal_skip.py` | `_has_multimodal_content` 11 个 case | P0-3 |
| `test_score_based.py` | score 5 维加权公式 16 个 | P0-7 + 阶段3 |
| `test_quota_check.py` | `check_account_quota` 8 个（含 P1-15 balance_cny 修复）| P0-6 + P1-15 |
| `test_cache_inject.py` | `_inject_prompt_cache` 10 个 | P1-8 |
| `test_usage_log_parse.py` | cached_tokens 解析 13 个 + _extract_group_id 2 个 | P0-3-followup |
| `test_sf_prices.py` | `find_sf_prices` 11 个 | 阶段B |
| `test_group_id_fk.py` | FK + UNIQUE 约束 5 个 | P0-1 |

**总计**：7 个文件 / 74 个 test / 9.6s 跑通

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

当前 74 tests 只在本地跑通。CI 集成（GitHub Actions / Gitee Go）待后续：

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

- ✅ 本地 74 tests 全过
- ❌ GitHub Actions / Gitee Go 集成（个人项目无必要）
- ❌ Coverage 报告（可选）

详见 [dev/conventions.md §CI 检查清单](conventions.md#pr-检查清单)。
