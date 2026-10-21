"""V4.8 CLI 单元测试: add_user_key.py / list_user_keys.py

覆盖:
1. add_user_key: 新建 user + 生成 key (明文返回一次)
2. add_user_key: key_hash 是 SHA256(明文), key_prefix 是前 8 字符
3. add_user_key: admin flag → is_admin=1, user.role=admin
4. add_user_key: 已有 user 复用 (不建新 user)
5. add_user_key: --daily-limit / --monthly-limit 限额写入
6. add_user_key: 已有 user 传 --daily-limit 更新限额
7. list_user_keys: 列出 user + key, 格式正确
8. list_user_keys: --username 过滤
9. list_user_keys: --status 过滤 (active / revoked)
10. list_user_keys: 无 user 时输出提示

跑法: python -m pytest tests/test_v48_cli.py -v
"""

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================
# 复用 v48_multi_user 的临时 DB fixture 模式
# ============================================================

@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    """临时 SQLite, 含 users + api_keys + usage_logs 表

    V6.18 (2026-10-22) 隔离修复: 被测的 scripts/add_user_key.py 顶部是
    `from db.engine import async_session_factory` + `from db.init_db import init_db`
    —— **导入时绑定**。只 monkeypatch db.engine.async_session_factory 不够:
    ① db.init_db.engine 仍绑真实库 (init_db() 会往真实库建表);
    ② 脚本模块若在 monkeypatch 之前就被 import 过, 其模块级引用也不会变。
    这里把 engine / init_db.engine / 脚本模块引用一起重绑到临时库。
    """
    db_file = tmp_path / "v48_cli_test.db"
    async_url = f"sqlite+aiosqlite:///{db_file}"

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from db.models import Base

    eng = create_async_engine(async_url, echo=False, future=True)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    async def _init():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_init())

    # engine 层重绑 (db.engine.engine + db.init_db.engine)
    import db.engine as db_engine
    import db.init_db as db_init_db
    _orig_engine = db_engine.engine
    _orig_init_engine = db_init_db.engine
    _orig_factory = db_engine.async_session_factory
    db_engine.engine = eng
    db_init_db.engine = eng
    db_engine.async_session_factory = factory
    # 断言: 必须是临时库
    assert str(eng.url).endswith("v48_cli_test.db"), f"engine 必须指向临时库, 实际 {eng.url}"

    # 脚本模块的模块级引用 (若已 import 过) 一并重绑
    _script_mods = []
    for _mod_name in ("scripts.add_user_key", "scripts.list_user_keys"):
        _mod = sys.modules.get(_mod_name)
        if _mod is not None:
            if hasattr(_mod, "async_session_factory"):
                _script_mods.append((_mod, _mod.async_session_factory))
                _mod.async_session_factory = factory
            if hasattr(_mod, "init_db"):
                _script_mods.append((_mod, _mod.init_db))
                _mod.init_db = _init_db_stub(eng)

    def _restore():
        db_init_db.engine = _orig_init_engine
        db_engine.engine = _orig_engine
        db_engine.async_session_factory = _orig_factory
        for _mod, _orig in _script_mods:
            if _orig is _orig_factory:
                _mod.async_session_factory = _orig
            else:
                _mod.init_db = _orig
        loop.close()

    try:
        yield db_file, factory, eng
    finally:
        _restore()


def _init_db_stub(eng):
    """init_db 的替代版 —— 在给定 engine 上建表 (不碰真实库)"""
    async def _init():
        from db.models import Base
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    return _init


def _run_async(coro):
    """在独立 event loop 里跑协程 (跟 test_v48_multi_user 一致的模式)"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ============================================================
# Test 1: 新建 user + 生成 key
# ============================================================

def test_add_user_key_creates_new_user(cli_db):
    """新建 user + key, 返回 (user_id, key_id, key_plain)"""
    db_file, factory, eng = cli_db

    from scripts.add_user_key import build_parser, add_user_key

    args = build_parser().parse_args([
        "--username", "zhangsan",
        "--display-name", "张三",
    ])
    user_id, key_id, key_plain = _run_async(add_user_key(args))

    assert user_id > 0
    assert key_id > 0
    assert key_plain.startswith("aimc-")
    assert len(key_plain) > 20  # aimc- + 32 bytes base64

    # 验证 DB
    async def _verify():
        from db.models import User, ApiKey
        async with factory() as s:
            user = (await s.execute(
                __import__("sqlalchemy").select(User).where(User.username == "zhangsan")
            )).scalars().first()
            assert user is not None
            assert user.display_name == "张三"
            assert user.role == "user"

            keys = (await s.execute(
                __import__("sqlalchemy").select(ApiKey).where(ApiKey.user_id == user.id)
            )).scalars().all()
            assert len(keys) == 1
            assert keys[0].key_hash == hashlib.sha256(key_plain.encode()).hexdigest()
            assert keys[0].key_prefix == key_plain[:8]
            assert keys[0].is_admin == 0
            assert keys[0].status == "active"

    _run_async(_verify())


# ============================================================
# Test 2: admin flag
# ============================================================

def test_add_user_key_admin_flag(cli_db):
    """--admin → is_admin=1, user.role=admin"""
    db_file, factory, eng = cli_db

    from scripts.add_user_key import build_parser, add_user_key

    args = build_parser().parse_args([
        "--username", "boss",
        "--admin",
    ])
    user_id, key_id, key_plain = _run_async(add_user_key(args))

    async def _verify():
        from db.models import User, ApiKey
        async with factory() as s:
            user = await s.get(User, user_id)
            assert user.role == "admin"
            key = await s.get(ApiKey, key_id)
            assert key.is_admin == 1

    _run_async(_verify())


# ============================================================
# Test 3: 已有 user 复用
# ============================================================

def test_add_user_key_reuse_existing_user(cli_db):
    """给已有 user 再加一把 key"""
    db_file, factory, eng = cli_db

    from scripts.add_user_key import build_parser, add_user_key

    args1 = build_parser().parse_args(["--username", "lisi"])
    user_id_1, key_id_1, key_plain_1 = _run_async(add_user_key(args1))

    args2 = build_parser().parse_args(["--username", "lisi", "--label", "笔记本"])
    user_id_2, key_id_2, key_plain_2 = _run_async(add_user_key(args2))

    assert user_id_1 == user_id_2  # 复用同一 user
    assert key_id_1 != key_id_2    # 不同 key
    assert key_plain_1 != key_plain_2

    async def _verify():
        from db.models import ApiKey
        async with factory() as s:
            keys = (await s.execute(
                __import__("sqlalchemy").select(ApiKey).where(ApiKey.user_id == user_id_1)
            )).scalars().all()
            assert len(keys) == 2
            labels = {k.label for k in keys}
            assert "笔记本" in labels

    _run_async(_verify())


# ============================================================
# Test 4: 限额写入
# ============================================================

def test_add_user_key_with_limits(cli_db):
    """--daily-limit / --monthly-limit 写入 user"""
    db_file, factory, eng = cli_db

    from scripts.add_user_key import build_parser, add_user_key

    args = build_parser().parse_args([
        "--username", "wangwu",
        "--daily-limit", "100000",
        "--monthly-limit", "1000000",
    ])
    user_id, key_id, key_plain = _run_async(add_user_key(args))

    async def _verify():
        from db.models import User
        async with factory() as s:
            user = await s.get(User, user_id)
            assert user.daily_token_limit == 100000
            assert user.monthly_token_limit == 1000000

    _run_async(_verify())


# ============================================================
# Test 5: 已有 user 传 --daily-limit 更新限额
# ============================================================

def test_add_user_key_updates_limits(cli_db):
    """给已有 user 发第二把 key 时, 传 --daily-limit 更新限额"""
    db_file, factory, eng = cli_db

    from scripts.add_user_key import build_parser, add_user_key

    # 第一次: 不设限额
    args1 = build_parser().parse_args(["--username", "zhaoliu"])
    user_id_1, _, _ = _run_async(add_user_key(args1))

    # 第二次: 设日限额
    args2 = build_parser().parse_args([
        "--username", "zhaoliu",
        "--daily-limit", "50000",
    ])
    user_id_2, _, _ = _run_async(add_user_key(args2))

    assert user_id_1 == user_id_2

    async def _verify():
        from db.models import User
        async with factory() as s:
            user = await s.get(User, user_id_1)
            assert user.daily_token_limit == 50000
            assert user.monthly_token_limit is None  # 未设

    _run_async(_verify())


# ============================================================
# Test 6: --role 显式指定
# ============================================================

def test_add_user_key_explicit_role(cli_db):
    """--role admin 显式指定角色 (即使不加 --admin)"""
    db_file, factory, eng = cli_db

    from scripts.add_user_key import build_parser, add_user_key

    args = build_parser().parse_args([
        "--username", "admin_explicit",
        "--role", "admin",
    ])
    user_id, key_id, key_plain = _run_async(add_user_key(args))

    async def _verify():
        from db.models import User, ApiKey
        async with factory() as s:
            user = await s.get(User, user_id)
            assert user.role == "admin"
            key = await s.get(ApiKey, key_id)
            assert key.is_admin == 0  # --admin 没传, key 不是 admin key

    _run_async(_verify())


# ============================================================
# Test 7: list_user_keys 基本列出
# ============================================================

def test_list_user_keys_basic(cli_db, capsys):
    """列出 user + key"""
    db_file, factory, eng = cli_db

    from scripts.add_user_key import build_parser as add_parser, add_user_key
    from scripts.list_user_keys import build_parser as list_parser, list_user_keys

    _run_async(add_user_key(add_parser().parse_args(["--username", "u1"])))
    _run_async(add_user_key(add_parser().parse_args(["--username", "u2", "--admin"])))
    capsys.readouterr()  # 清掉 add_user_key 的输出

    args = list_parser().parse_args([])
    count = _run_async(list_user_keys(args))

    assert count == 2
    out = capsys.readouterr().out
    assert "u1" in out
    assert "u2" in out
    assert "aimc-" in out  # key_prefix 含 aimc- 前缀


# ============================================================
# Test 8: list_user_keys --username 过滤
# ============================================================

def test_list_user_keys_filter_username(cli_db, capsys):
    """--username 只列指定 user"""
    db_file, factory, eng = cli_db

    from scripts.add_user_key import build_parser as add_parser, add_user_key
    from scripts.list_user_keys import build_parser as list_parser, list_user_keys

    _run_async(add_user_key(add_parser().parse_args(["--username", "alpha"])))
    _run_async(add_user_key(add_parser().parse_args(["--username", "beta"])))
    capsys.readouterr()  # 清掉 add_user_key 的输出

    args = list_parser().parse_args(["--username", "alpha"])
    count = _run_async(list_user_keys(args))

    assert count == 1
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "beta" not in out


# ============================================================
# Test 9: list_user_keys --status 过滤
# ============================================================

def test_list_user_keys_filter_status(cli_db, capsys):
    """--status active 只列 active key"""
    db_file, factory, eng = cli_db

    from scripts.add_user_key import build_parser as add_parser, add_user_key
    from scripts.list_user_keys import build_parser as list_parser, list_user_keys

    _run_async(add_user_key(add_parser().parse_args(["--username", "gamma"])))

    # 手动 revoke 一把 key
    async def _revoke():
        from db.models import ApiKey
        from sqlalchemy import select, update
        async with factory() as s:
            await s.execute(update(ApiKey).where(ApiKey.id == 1).values(status="revoked"))
            await s.commit()
    _run_async(_revoke())

    # 再加一把 active key
    _run_async(add_user_key(add_parser().parse_args(["--username", "gamma"])))
    capsys.readouterr()  # 清掉前面的输出

    # 过滤 active
    args = list_parser().parse_args(["--username", "gamma", "--status", "active"])
    _run_async(list_user_keys(args))
    out = capsys.readouterr().out
    assert "active" in out
    assert "revoked" not in out


# ============================================================
# Test 10: list_user_keys 无 user
# ============================================================

def test_list_user_keys_empty(cli_db, capsys):
    """无 user 时输出提示"""
    db_file, factory, eng = cli_db

    from scripts.list_user_keys import build_parser, list_user_keys

    args = build_parser().parse_args([])
    count = _run_async(list_user_keys(args))

    assert count == 0
    out = capsys.readouterr().out
    assert "无用户记录" in out


# ============================================================
# 入口 (可 python tests/test_v48_cli.py 直接跑)
# ============================================================
if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.call(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"]
    ))
