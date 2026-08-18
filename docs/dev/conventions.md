# 开发规范（commit / 分支 / 代码风格）

> 受众：所有改 AIMC 代码的开发者
> 前提：[setup.md](setup.md)（本地跑起来）+ [testing.md](testing.md)（测试会跑）

## 1. Git 工作流

### 分支策略

```
master                  ← 主分支（稳定 + 已部署到 VM）
├── feat/*             ← 新功能分支 (e.g. feat/cache-stats-api)
├── fix/*              ← bug 修复分支 (e.g. fix/multimodal-skip)
├── refactor/*         ← 重构（无功能变化）
└── docs/*             ← 文档修改分支 (e.g. docs/update-readme)
```

### Commit 消息格式

使用 **Conventional Commits** 风格：

```
<type>(<scope>): <subject>

<body>

<footer>
```

**type**（必填）：
- `feat`: 新功能
- `fix`: bug 修复
- `docs`: 仅文档
- `refactor`: 重构（无功能变化）
- `test`: 仅测试
- `chore`: 构建/工具/CI 等

**scope**（可选）：改动模块
- `router` / `proxy` / `dashboard` / `scheduler` / `quota` / `intelligence` / `db` / `scripts` / `tests` / `docs`

**subject**（必填）：
- ≤ 50 字符
- 动词用现在时（"add" / "fix" / "update"）
- 首字母不大写
- 末尾不加句号

**body**（可选）：
- ≤ 72 字符/行
- 说明"为什么"（不只是"做了什么"）
- 用 `1.` `2.` `3.` 列要点

**footer**（可选）：
- `Refs: #issue` 或 `Breaking change: <description>`

**示例**（AIMC 真实 commit）：

```
feat(router): P0-3 纯文本请求跳过 multimodal candidate

背景: tier:flagship 选 minimax-m3 (multimodal) 拒纯文本 → 上游 400
修复: _has_multimodal_content() 检测 image/audio/file
      纯文本请求跳过 modality=multimodal 的 member

测试:
- 7/7 e2e HTTP 200 (tier:flagship/strong/balanced/light/code + 直调)
- tier:vision multimodal request 仍可选 multimodal (用户显式)
```

### 提 PR 流程

```
1. 创建分支: git checkout -b fix/your-fix
2. 改代码 + 写测试
3. 跑测试: pytest tests/ -v (全过)
4. commit: git commit -m "fix(router): 你的 fix 描述"
5. push: git push origin fix/your-fix
6. Gitee 上提 PR: master ← fix/your-fix
7. CI 通过后 reviewer 合并
```

## 2. 代码风格

### Python 版本

- **3.11+**（最低 3.11，推荐 3.12）
- 用 `from __future__ import annotations`（如果用 type hints 引用未来定义）

### 风格（无强制 lint，但建议）

- **命名**：
  - 类名 `PascalCase`
  - 函数 / 变量 `snake_case`
  - 常量 `UPPER_SNAKE_CASE`
  - 私有 `_leading_underscore`
- **行长**：≤ 100 字符（软约束）
- **import**：分组（标准库 / 第三方 / 本地），空行分隔
- **类型 hint**：公共函数必填，私有函数可选

```python
# 标准库
import asyncio
import json
from pathlib import Path

# 第三方
from fastapi import APIRouter, Depends
from sqlalchemy import select

# 本地
from db.engine import async_session_factory
from db.models import ModelRegistry
```

### Async vs Sync

AIMC 主进程是 async（FastAPI + APScheduler）。但**有些 sync 函数需要**：

| 场景 | 用 |
|------|---|
| FastAPI endpoint | `async def` |
| APScheduler job | `async def` |
| apscheduler event listener (P0-8) | **def**（同步，用 `sync_engine`） |
| 写 usage_log 的 helper | 混：触发用 async，DB 操作用 sync |
| CLI 脚本（`scripts/`）| 大部分 sync |

**重要**：async 函数里**不能用** sync SQLAlchemy Session（会 `greenlet_spawn` 错误）。要么用 `async_session`，要么用独立的 `sync_engine`（P0-8 修法）。

### 错误处理

```python
# 标准错误
try:
    result = await risky_call()
except SpecificError as e:
    logger.warning(f"操作失败: {e}")
    # 优雅降级 / 重试 / 透传给用户

# 不要:
except Exception:
    pass  # 隐藏错误, 难排查
```

### 日志

```python
import logging
logger = logging.getLogger(__name__)  # 模块级 logger

# 5 级使用规范:
logger.debug("详细诊断信息 (默认不显示)")
logger.info("正常流程节点")  # ← 90% 的日志
logger.warning("异常但服务可继续 (如 1 次 429 触发 cooldown)")
logger.error("单次请求失败但不影响其他")
logger.critical("服务级异常 (如 db 不可用)")
```

**绝对不要**：
- `print()` 调试（用 `logger.debug` + `--log-level=DEBUG`）
- `logger.info(f"password={pwd}")`（敏感信息）
- 在 `for` 循环里 `logger.info`（刷屏）

## 3. db 迁移规范

### 新增列 / 新增表

1. **永远用 ALTER TABLE 增量**（不要 `Base.metadata.create_all` 重建表）
2. 写在 `db/migrations/<date>_<scope>.py`
3. 提供 `--dry-run` 和 `--apply` 两个 mode
4. 必须幂等（重复跑不报错）

```python
# db/migrations/2026_08_15_p_xxx.py
from db.engine import sync_engine
from sqlalchemy import text

def upgrade():
    with sync_engine.begin() as conn:
        conn.execute(text("ALTER TABLE channels ADD COLUMN api_base TEXT"))

def downgrade():
    with sync_engine.begin() as conn:
        conn.execute(text("ALTER TABLE channels DROP COLUMN api_base"))

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if args.apply:
        upgrade()
        print("[OK] applied")
    else:
        print("[DRY-RUN] would add column api_base")
```

### 测试

```bash
# dry-run
python db/migrations/2026_08_15_p_xxx.py

# 真跑
python db/migrations/2026_08_15_p_xxx.py --apply

# 验证
sqlite3 data/aimc.db "PRAGMA table_info(channels)"
```

### 回滚

迁移要可逆（`downgrade()` 函数）。如果不可逆（如删表），要在 commit message 写明 `Breaking change:`。

## 4. 测试规范

### 何时必须写测试

- ✅ **新功能**：每个用户场景 ≥ 1 个测试
- ✅ **Bug fix**：先写一个能复现 bug 的测试，确认它失败，再修代码，确认它通过
- ✅ **重构**：跑现有测试 + 加新测试覆盖新场景
- ❌ 文档、注释、纯重命名：不需要测试

### 测试位置

- `tests/test_<module>.py`（如 `test_router.py` / `test_quota_check.py`）
- 一个测试文件对应一个被测模块

### 测试风格

- 用 **纯函数** 而非 mock 真实 db（优先）
- 必须用真 db 时：用 `pytest` 的 `tmp_path` fixture 建临时 SQLite
- async 测试：`pytest-asyncio` mode=auto（已在 pyproject.toml）

```python
# 纯函数测试 (推荐)
def test_quota_ratio_full():
    assert compute_quota_ratio(100, 100) == 1.0

# async 真 db 测试 (必要时)
@pytest.fixture
async def fresh_session():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(eng) as s:
        yield s
    await eng.dispose()
```

详见 [testing.md](testing.md)。

## 5. PR 检查清单

提 PR 前确认：

- [ ] 测试通过（`pytest tests/ -v`）
- [ ] 新功能有测试（覆盖率 ≥ 80% 该模块）
- [ ] commit message 含 type(scope) + subject + body
- [ ] 没引入新依赖（如要，加到 `requirements.txt` + commit message 注明）
- [ ] 没改无关代码（避免 "顺手 fix"）
- [ ] 文档同步（user/dev/ops/docs）
- [ ] migration 写了 down/up 双向
- [ ] 没破坏现有 API（向后兼容）

## 6. Release / Deploy

**当前没有 formal release process**（个人项目）。master 分支 = 部署到 VM。

更新 VM：

```bash
ssh ubuntu2401
cd /home/lxc/aimc
git pull
source .venv/bin/activate
pip install -r requirements.txt
# 如果有 migration
for m in db/migrations/2026_08_*.py; do python $m --apply; done
systemctl --user restart aimc
python -m pytest tests/ -v
```

详见 [ops/deploy.md](../ops/deploy.md)。
