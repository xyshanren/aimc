"""P2-16 (2026-08-15): 测试 fixture

设计:
- 用 in-memory SQLite (sqlite:///:memory:) 隔离测试, 不影响 prod db
- 每个 test function 创建新 engine + 新 session (避免状态污染)
- pytest-asyncio mode=auto (所有 async test 自动 async)
"""
import asyncio
import sys
from pathlib import Path

import pytest

# 项目根目录: tests/conftest.py -> 父级父级 = project root
ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _event_loop_policy():
    """强制 event loop scope = function (避免 async fixture 跨 test 复用 loop)"""
    import asyncio
    loop_policy = asyncio.DefaultEventLoopPolicy()
    asyncio.set_event_loop_policy(loop_policy)


@pytest.fixture
def event_loop():
    """每个 test 一个新 event loop"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
