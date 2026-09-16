"""P2-16: 测试 check_account_quota (P0-6 + P1-15)

V6.6 (2026-09-16) 起账号配额 = 纯真实测量:
- token 维 (tpm/daily/weekly/5h): usage_logs 按账号+窗口真实 SUM, 无 estimated_tokens
  预留 — 回归锚点: 旧实现静态门 max_tokens×2>limit 对 128k 请求恒拒 (tpm 假门)
- rpm: usage_logs 近 60s 真实 COUNT
- concurrent: 内存 in-flight 计数 (proxy acquire/release)
- balance_cny: 不变 (account_quota_usage.window_start 作 sync 标记)

设计:
- 用 tmp SQLite (隔离测试, 不影响 prod db)
- pytest-asyncio auto mode
"""
import asyncio
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta
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

from db.models import Account, AccountQuota, AccountQuotaUsage, Channel, UsageLog
from gateway import quota as quota_mod
from gateway.quota import _check_dimension, check_account_quota


async def _init_test_db():
    """建全表 (含 api_base/api_style 等新列)"""
    from db.engine import engine
    from db.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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
    """balance_cny 专用 (V6.6 起 token/rpm/concurrent 不再读这张表)"""
    session.add(AccountQuotaUsage(
        account_id=account_id, dimension=dimension,
        window_start=window_start, used_value=used_value,
    ))
    await session.flush()


async def _add_usage(session, account_id, input_tokens, output_tokens,
                     created_at=None, model="test-model"):
    """V6.6: token/rpm 维度的真实测量数据源 = usage_logs"""
    session.add(UsageLog(
        channel_id=1, account_id=account_id, model=model,
        input_tokens=input_tokens, output_tokens=output_tokens,
        status_code=200, latency_ms=10,
        created_at=created_at or datetime.now(),
    ))
    await session.flush()


async def _get_quota(session, account_id):
    from sqlalchemy import select
    from db.models import AccountQuota
    return (await session.execute(
        select(AccountQuota).where(AccountQuota.account_id == account_id)
    )).scalar_one()


# === check_account_quota 全局 ===
async def test_no_quotas_configured_passes():
    """account 无 quota 配置 -> 通过 (不限)"""
    await _init_test_db()
    from db.engine import async_session_factory
    async with async_session_factory() as session:
        ok, reason = await check_account_quota(session, account_id=999)
        assert ok is True
        assert reason is None


# === P1-15: balance_cny (V6.6 语义不变) ===
async def test_balance_cny_unsync_passes():
    """balance_cny 未 sync (window_start IS NULL) -> 视作未知, 通过"""
    await _init_test_db()
    from db.engine import async_session_factory

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "balance_cny", 10.0, reset_strategy="never")
        await session.commit()

        q = await _get_quota(session, acc_id)
        ok, reason = await _check_dimension(session, acc_id, q)
        assert ok is True, f"unsync 应通过, 实际 reason={reason}"


async def test_balance_cny_synced_zero_blocks():
    await _init_test_db()
    from db.engine import async_session_factory

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "balance_cny", 10.0, reset_strategy="never")
        await _set_usage(session, acc_id, "balance_cny", 0.0, window_start=datetime.now())
        await session.commit()

        q = await _get_quota(session, acc_id)
        ok, reason = await _check_dimension(session, acc_id, q)
        assert ok is False
        assert "余额耗尽" in reason


async def test_balance_cny_synced_healthy_passes():
    await _init_test_db()
    from db.engine import async_session_factory

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "balance_cny", 10.0, reset_strategy="never")
        await _set_usage(session, acc_id, "balance_cny", 5.0, window_start=datetime.now())
        await session.commit()

        q = await _get_quota(session, acc_id)
        ok, reason = await _check_dimension(session, acc_id, q)
        assert ok is True


# === tpm (V6.6 真实测量; 回归锚点 = tpm 静态假门) ===
async def test_tpm_no_real_usage_passes_despite_huge_claim():
    """回归: 旧静态门 max_tokens×2>limit 对 128k 请求恒拒; 新实现无预留,
    窗口内无真实消耗即通过 — 声明值 (max_tokens) 不再参与判定"""
    await _init_test_db()
    from db.engine import async_session_factory

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "tpm", 200_000, reset_strategy="rolling")
        await session.commit()

        q = await _get_quota(session, acc_id)
        ok, reason = await _check_dimension(session, acc_id, q)
        assert ok is True, f"窗口内无消耗应通过 (旧假门会拒), reason={reason}"


async def test_tpm_real_usage_over_limit_blocks():
    await _init_test_db()
    from db.engine import async_session_factory

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "tpm", 200_000, reset_strategy="rolling")
        await _add_usage(session, acc_id, input_tokens=150_000, output_tokens=100_000)
        await session.commit()

        q = await _get_quota(session, acc_id)
        ok, reason = await _check_dimension(session, acc_id, q)
        assert ok is False
        assert "tpm" in reason
        assert "超额" in reason


async def test_tpm_window_60s_ignores_older_rows():
    """90s 前的消耗不计入 60s 吞吐窗"""
    await _init_test_db()
    from db.engine import async_session_factory

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "tpm", 200_000, reset_strategy="rolling")
        await _add_usage(session, acc_id, 300_000, 0,
                         created_at=datetime.now() - timedelta(seconds=90))
        await session.commit()

        q = await _get_quota(session, acc_id)
        ok, reason = await _check_dimension(session, acc_id, q)
        assert ok is True, f"窗口外行不应计数, reason={reason}"


async def test_tpm_counts_only_own_account():
    """SUM 按账号隔离: 别的账号消耗不计入"""
    await _init_test_db()
    from db.engine import async_session_factory

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session, "acc_a")
        other_id = await _setup_channel_account(session, "acc_b")
        await _add_quota(session, acc_id, "tpm", 200_000, reset_strategy="rolling")
        await _add_usage(session, other_id, 500_000, 0)
        await session.commit()

        q = await _get_quota(session, acc_id)
        ok, reason = await _check_dimension(session, acc_id, q)
        assert ok is True


# === 5h_tokens (rolling 窗) ===
async def test_5h_tokens_within_limit():
    await _init_test_db()
    from db.engine import async_session_factory

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "5h_tokens", 50_000_000,
                          window_seconds=18000, reset_strategy="rolling")
        await _add_usage(session, acc_id, 6_000_000, 4_000_000)
        await session.commit()

        q = await _get_quota(session, acc_id)
        ok, _ = await _check_dimension(session, acc_id, q)
        assert ok is True


async def test_5h_tokens_over_limit_blocks():
    await _init_test_db()
    from db.engine import async_session_factory

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "5h_tokens", 50_000_000,
                          window_seconds=18000, reset_strategy="rolling")
        await _add_usage(session, acc_id, 30_000_000, 20_000_001)
        await session.commit()

        q = await _get_quota(session, acc_id)
        ok, reason = await _check_dimension(session, acc_id, q)
        assert ok is False
        assert "5h_tokens" in reason
        assert "超额" in reason


async def test_5h_tokens_old_rows_outside_window_ignored():
    """6h 前的消耗不计入 5h 滚动窗 (旧行为靠 used_value 手工 rollover, 新行为现算)"""
    await _init_test_db()
    from db.engine import async_session_factory

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "5h_tokens", 50_000_000,
                          window_seconds=18000, reset_strategy="rolling")
        await _add_usage(session, acc_id, 60_000_000, 0,
                         created_at=datetime.now() - timedelta(hours=6))
        await session.commit()

        q = await _get_quota(session, acc_id)
        ok, reason = await _check_dimension(session, acc_id, q)
        assert ok is True


# === daily_tokens (fixed_daily 本地零点窗) ===
async def test_daily_tokens_yesterday_rows_not_counted():
    await _init_test_db()
    from db.engine import async_session_factory

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "daily_tokens", 1_000,
                         reset_strategy="fixed_daily")
        yesterday = datetime.now().replace(hour=23, minute=59, second=0) - timedelta(days=1)
        await _add_usage(session, acc_id, 999, 0, created_at=yesterday)
        await _add_usage(session, acc_id, 500, 0)
        await session.commit()

        q = await _get_quota(session, acc_id)
        ok, reason = await _check_dimension(session, acc_id, q)
        assert ok is True, f"昨日行不应计入当日窗, reason={reason}"


async def test_daily_tokens_today_over_blocks():
    await _init_test_db()
    from db.engine import async_session_factory

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "daily_tokens", 1_000,
                         reset_strategy="fixed_daily")
        await _add_usage(session, acc_id, 600, 500)
        await session.commit()

        q = await _get_quota(session, acc_id)
        ok, reason = await _check_dimension(session, acc_id, q)
        assert ok is False
        assert "daily_tokens" in reason


# === rpm (V6.6 真实 COUNT) ===
async def test_rpm_counts_recent_requests():
    await _init_test_db()
    from db.engine import async_session_factory

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "rpm", 5, reset_strategy="rolling")
        for _ in range(5):
            await _add_usage(session, acc_id, 10, 10)
        await session.commit()

        q = await _get_quota(session, acc_id)
        ok, reason = await _check_dimension(session, acc_id, q)
        assert ok is False
        assert "RPM" in reason


async def test_rpm_old_requests_not_counted():
    await _init_test_db()
    from db.engine import async_session_factory

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "rpm", 5, reset_strategy="rolling")
        await _add_usage(session, acc_id, 10, 10,
                         created_at=datetime.now() - timedelta(seconds=120))
        await session.commit()

        q = await _get_quota(session, acc_id)
        ok, reason = await _check_dimension(session, acc_id, q)
        assert ok is True


# === concurrent (V6.6 内存 in-flight) ===
async def test_concurrent_inflight_over_blocks_and_release_recovers():
    await _init_test_db()
    from db.engine import async_session_factory

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "concurrent", 2, reset_strategy="rolling")
        await session.commit()

        q = await _get_quota(session, acc_id)
        quota_mod._INFLIGHT.clear()

        ok, _ = await _check_dimension(session, acc_id, q)
        assert ok is True

        quota_mod.concurrent_acquire(acc_id)
        quota_mod.concurrent_acquire(acc_id)
        ok, reason = await _check_dimension(session, acc_id, q)
        assert ok is False
        assert "并发" in reason

        quota_mod.concurrent_release(acc_id)
        ok, _ = await _check_dimension(session, acc_id, q)
        assert ok is True

        # 幂等: 多 release 不会变负
        quota_mod.concurrent_release(acc_id)
        quota_mod.concurrent_release(acc_id)
        assert quota_mod.concurrent_inflight(acc_id) == 0


# === check_account_quota 组合 (多维度同时配置) ===
async def test_check_account_quota_mixed_dims_all_real():
    """token/rpm 全部真实测量: 配了 tpm+rpm 但窗口内无消耗 -> 通过"""
    await _init_test_db()
    from db.engine import async_session_factory

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "tpm", 200_000, reset_strategy="rolling")
        await _add_quota(session, acc_id, "rpm", 60, reset_strategy="rolling")
        await session.commit()

        ok, reason = await check_account_quota(session, acc_id)
        assert ok is True, f"无真实消耗应全维通过, reason={reason}"


async def test_check_account_quota_blocks_on_any_dim():
    await _init_test_db()
    from db.engine import async_session_factory

    async with async_session_factory() as session:
        acc_id = await _setup_channel_account(session)
        await _add_quota(session, acc_id, "tpm", 100, reset_strategy="rolling")
        await _add_quota(session, acc_id, "rpm", 60, reset_strategy="rolling")
        await _add_usage(session, acc_id, 101, 0)
        await session.commit()

        ok, reason = await check_account_quota(session, acc_id)
        assert ok is False
        assert "tpm" in reason
