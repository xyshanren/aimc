"""P2-16: 测试 check_account_quota (P0-6 + P1-15)

设计:
- 用 tmp SQLite (隔离测试, 不影响 prod db), 每次 test 用 session 清理
- pytest-asyncio auto mode
- 需 mock config (db.engine import 时读 settings.db_url) + init_db 建全表
"""
import asyncio
import os
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _install_mock_env():
    """mock config + 临时 SQLite (同 test_v44_cli.py 模式)"""
    tmp_dir = tempfile.mkdtemp(prefix="aimc_quota_")
    db_path = os.path.join(tmp_dir, "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = types.SimpleNamespace(
        openai_api_key="sk-test",
        openai_base_url="https://mock.local/v1",
        parse_model="gpt-4o-mini",
        parse_confidence_threshold=0.6,
        crawl_enabled=True,
        aimc_master_key="a" * 64,
        route_refresh_interval=600,
        db_url=db_url,
        db_path=db_path,
    )


_install_mock_env()

import pytest
from sqlalchemy import event

from db.models import Account, AccountQuota, AccountQuotaUsage, Channel
from gateway.quota import _check_dimension, check_account_quota


async def _init_test_db():
    """建全表 (含 api_base/api_style 等新列)"""
    from db.engine import engine
    from db.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# === 测试隔离: 每个 test 用 SAVEPOINT 包裹, 自动回滚 ===
# pytest-asyncio + async session + savepoint pattern
# 这里简化: 直接 async session, 每次 test 清理


async def _setup_channel_account(session, account_name="test_01"):
    """建 channel + account, 返 account_id"""
    ch = Channel(
        name="test_ch", provider="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        status="active", priority=1,
        provider_type="aggregator", billing_mode="pay_per_token",
    )
    session.add(ch)
    await session.flush()
    acc = Account(
        channel_id=ch.id, account_name=account_name,
        api_key="placeholder", status="active",
        quota_plan="pay_as_you_go",
    )
    session.add(acc)
    await session.flush()
    return acc.id


async def _add_quota(session, account_id, dimension, limit_value, **kwargs):
    session.add(AccountQuota(
        account_id=account_id, dimension=dimension,
        limit_value=limit_value, **kwargs,
    ))
    await session.flush()


async def _set_usage(session, account_id, dimension, used_value, window_start=None):
    session.add(AccountQuotaUsage(
        account_id=account_id, dimension=dimension,
        window_start=window_start, used_value=used_value,
    ))
    await session.flush()


# === check_account_quota 全局 ===
async def test_no_quotas_configured_passes():
    """account 无 quota 配置 -> 通过 (不限)"""
    await _init_test_db()
    from db.engine import async_session_factory
    async with async_session_factory() as session:
        ok, reason = await check_account_quota(session, account_id=999, estimated_tokens=0)
        assert ok is True
        assert reason is None


# === P1-15: balance_cny 修复 ===
async def test_balance_cny_unsync_passes():
    """balance_cny 未 sync (window_start IS NULL) -> 视作未知, 通过"""
    await _init_test_db()
    from db.engine import async_session_factory
    from sqlalchemy import select

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "balance_cny", 10.0, reset_strategy="never")
        await session.commit()

        # 用 _check_dimension 直接测 (避开 select_channel)
        from db.models import AccountQuota
        q = (await session.execute(
            select(AccountQuota).where(AccountQuota.account_id == acc_id)
        )).scalar_one()
        ok, reason = await _check_dimension(session, acc_id, q, 0)
        assert ok is True, f"unsync 应通过, 实际 reason={reason}"


async def test_balance_cny_synced_zero_blocks():
    await _init_test_db()
    from db.engine import async_session_factory
    from sqlalchemy import select

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "balance_cny", 10.0, reset_strategy="never")
        await _set_usage(session, acc_id, "balance_cny", 0.0, window_start=datetime.now())
        await session.commit()

        from db.models import AccountQuota
        q = (await session.execute(
            select(AccountQuota).where(AccountQuota.account_id == acc_id)
        )).scalar_one()
        ok, reason = await _check_dimension(session, acc_id, q, 0)
        assert ok is False
        assert "余额耗尽" in reason


async def test_balance_cny_synced_healthy_passes():
    await _init_test_db()
    from db.engine import async_session_factory
    from sqlalchemy import select

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "balance_cny", 10.0, reset_strategy="never")
        await _set_usage(session, acc_id, "balance_cny", 5.0, window_start=datetime.now())
        await session.commit()

        from db.models import AccountQuota
        q = (await session.execute(
            select(AccountQuota).where(AccountQuota.account_id == acc_id)
        )).scalar_one()
        ok, reason = await _check_dimension(session, acc_id, q, 0)
        assert ok is True


# === 5h_tokens ===
async def test_5h_tokens_within_limit():
    await _init_test_db()
    from db.engine import async_session_factory
    from sqlalchemy import select

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "5h_tokens", 50_000_000,
                          window_seconds=18000, reset_strategy="rolling")
        await _set_usage(session, acc_id, "5h_tokens", 10_000_000,
                          window_start=datetime.now())
        await session.commit()

        from db.models import AccountQuota
        q = (await session.execute(
            select(AccountQuota).where(AccountQuota.account_id == acc_id)
        )).scalar_one()
        ok, _ = await _check_dimension(session, acc_id, q, 100)
        assert ok is True


async def test_5h_tokens_over_limit_with_estimate_blocks():
    await _init_test_db()
    from db.engine import async_session_factory
    from sqlalchemy import select

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "5h_tokens", 50_000_000,
                          window_seconds=18000, reset_strategy="rolling")
        await _set_usage(session, acc_id, "5h_tokens", 49_999_900,
                          window_start=datetime.now())
        await session.commit()

        from db.models import AccountQuota
        q = (await session.execute(
            select(AccountQuota).where(AccountQuota.account_id == acc_id)
        )).scalar_one()
        ok, reason = await _check_dimension(session, acc_id, q, estimated_tokens=200)
        assert ok is False
        assert "5h_tokens" in reason
        assert "超额" in reason


# === concurrent ===
async def test_concurrent_within_limit():
    await _init_test_db()
    from db.engine import async_session_factory
    from sqlalchemy import select

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "concurrent", 10, reset_strategy="rolling")
        await _set_usage(session, acc_id, "concurrent", 5)
        await session.commit()

        from db.models import AccountQuota
        q = (await session.execute(
            select(AccountQuota).where(AccountQuota.account_id == acc_id)
        )).scalar_one()
        ok, _ = await _check_dimension(session, acc_id, q, 0)
        assert ok is True


async def test_concurrent_at_limit_blocks():
    await _init_test_db()
    from db.engine import async_session_factory
    from sqlalchemy import select

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "concurrent", 10, reset_strategy="rolling")
        await _set_usage(session, acc_id, "concurrent", 10)
        await session.commit()

        from db.models import AccountQuota
        q = (await session.execute(
            select(AccountQuota).where(AccountQuota.account_id == acc_id)
        )).scalar_one()
        ok, reason = await _check_dimension(session, acc_id, q, 0)
        assert ok is False
        assert "并发" in reason
