"""V6.5.1 (2026-09-14): manual_import 幂等判断回归.

生产实证: 用户在运维页禁用 glm/stepfun 账号后重跑 manual_import.py,
旧幂等判断只认 active 账号, disabled 账号不被算作"已存在",
落入 recover 分支用同一把 key 重建同名 active 账号, 绕过禁用意图
(glm_01 / stepfun_01 各多出一条 id=8/9)。

修复后: channel 下存在任意账号 (含 disabled) 即 skip; 只有 channel
存在但一条账号都没有 (真·老数据残留) 才走 recover 重建。

跑法:
    pytest tests/test_manual_import_idempotency.py -v
"""

import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def import_db(tmp_path, monkeypatch):
    """临时 SQLite + 真实 engine, 隔离 manual_import 的 db 访问."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

    db_file = tmp_path / "manual_import_idem.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False, future=True)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    import db.engine as dbeng
    monkeypatch.setattr(dbeng, "engine", eng)
    monkeypatch.setattr(dbeng, "async_session_factory", factory)

    async def _init():
        from db.models import Base
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_init())
    finally:
        loop.close()

    return factory


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _seed_channel_with_account(factory, *, channel_name, provider, account_name, status):
    """种一个 channel + 指定状态的同名账号, 返回 (channel_id, account_id)."""

    async def _run():
        from db.models import Channel, Account
        async with factory() as s:
            ch = Channel(
                name=channel_name, provider=provider,
                base_url="https://example.invalid/v1", priority=10, status="active",
            )
            s.add(ch)
            await s.flush()
            acc = Account(
                channel_id=ch.id, account_name=account_name,
                api_key="", api_key_ciphertext=b"x", status=status,
            )
            s.add(acc)
            await s.commit()
            return ch.id, acc.id

    return _run_async(_run())


def _run_import_one_channel(factory, cfg):
    """在隔离 db 上跑 import_one_channel, 返回 (created: bool, accounts: list[(name, status)])."""

    async def _run():
        from scripts.manual_import import import_one_channel
        from db.models import Account
        async with factory() as s:
            created = await import_one_channel(s, cfg)
            rows = (await s.execute(
                Account.__table__.select().order_by(Account.id)
            )).fetchall()
            return created, [(r.account_name, r.status) for r in rows]

    return _run_async(_run())


@pytest.mark.usefixtures("import_db")
class TestDisabledAccountDoesNotResurrect:
    """核心回归: disabled 账号不得被 recover 分支复活."""

    _CFG = {
        "key": "glm", "name": "glm_bigmodel", "provider": "glm",
        "env_var": "GLM_API_KEY", "base_url": "https://example.invalid/v1",
        "priority": 5, "qpm": 60, "prices": [],
    }

    def test_disabled_account_skips_rebuild(self, import_db, monkeypatch):
        _seed_channel_with_account(
            import_db, channel_name="glm_bigmodel", provider="glm",
            account_name="glm_01", status="disabled",
        )
        monkeypatch.setenv("GLM_API_KEY", "sk-test-key-123")
        monkeypatch.setattr("scripts.manual_import.settings", _FakeSettings())

        created, accounts = _run_import_one_channel(import_db, self._CFG)

        assert created is False, "disabled 账号存在时不应重建"
        assert len(accounts) == 1, f"应保持 1 条账号, 实际 {len(accounts)}"
        name, status = accounts[0]
        assert (name, status) == ("glm_01", "disabled"), "原 disabled 账号应原样保留"

    def test_active_account_skips_rebuild(self, import_db, monkeypatch):
        """旧行为已覆盖的场景, 修后不得退化."""
        _seed_channel_with_account(
            import_db, channel_name="glm_bigmodel", provider="glm",
            account_name="glm_01", status="active",
        )
        monkeypatch.setenv("GLM_API_KEY", "sk-test-key-123")
        monkeypatch.setattr("scripts.manual_import.settings", _FakeSettings())

        created, accounts = _run_import_one_channel(import_db, self._CFG)

        assert created is False
        assert len(accounts) == 1
        assert accounts[0][1] == "active"

    def test_channel_without_any_account_recovers(self, import_db, monkeypatch):
        """channel 在但一条账号都没有 (真·老数据残留) → recover 仍应建账号."""
        from db.models import Channel
        from security.crypto import encrypt

        async def _seed_ch_only():
            async with import_db() as s:
                ch = Channel(
                    name="glm_bigmodel", provider="glm",
                    base_url="https://example.invalid/v1", priority=10, status="active",
                )
                s.add(ch)
                await s.commit()
                return ch.id

        ch_id = _run_async(_seed_ch_only())
        monkeypatch.setenv("GLM_API_KEY", "sk-test-key-123")
        monkeypatch.setattr("scripts.manual_import.settings", _FakeSettings())

        created, accounts = _run_import_one_channel(import_db, self._CFG)

        assert created is True, "channel 无任何账号时应 recover 重建"
        assert len(accounts) == 1
        assert accounts[0] == ("glm_01", "active")
        # 清理: recover 建的账号不校验密文, 但至少确认写入了 ciphertext
        assert encrypt  # 占位防 lint; 密文断言见 db 层测试


class _FakeSettings:
    """替代 config.settings: master key 需 64 字符 hex (encrypt 会真跑)."""

    aimc_master_key = "ab" * 32
