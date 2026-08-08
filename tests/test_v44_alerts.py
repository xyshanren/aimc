"""V4.4 告警 (intelligence/v44_alerts.py) 单元测试

覆盖:
1. check_quota_alerts: 80% 触发 (key=quota_near_limit)
2. check_quota_alerts: 100% 触发 (key=account_exhausted)
3. check_quota_alerts: 50% 不触发
4. check_quota_alerts: quota_total=0 不触发
5. check_no_active_accounts: 0 active 触发 (key=no_active_account)
6. check_no_active_accounts: 1+ active 不触发
7. check_cost_rising: 涨幅 >=30% 触发
8. check_cost_rising: 涨幅 <30% 不触发
9. check_all_alerts: 单 checker 抛异常不影响其他

跑法: python tests/test_v44_alerts.py
"""

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config + 临时 SQLite
# ============================================================
def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v44_alerts_")
    db_path = os.path.join(tmp_dir, "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"

    import types

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = types.SimpleNamespace(
        openai_api_key="sk-test",
        openai_base_url="https://mock.local/v1",
        parse_model="gpt-4o-mini",
        parse_confidence_threshold=0.6,
        crawl_enabled=True,
        aimc_master_key="test-key",
        route_refresh_interval=600,
        db_url=db_url,
        db_path=db_path,
        alert_channel="log",
        bark_key="",
        bark_server="",
        serverchan_key="",
        feishu_webhook_url="",
    )


_install_mock_env()


async def _reset_db():
    """清掉所有 V4.4 涉及的表, 保证测试隔离。"""
    from db.engine import async_session_factory
    from db.init_db import init_db
    from db.models import Channel, Account, UsageLog
    from sqlalchemy import delete

    await init_db()
    async with async_session_factory() as session:
        await session.execute(delete(UsageLog))
        await session.execute(delete(Account))
        await session.execute(delete(Channel))
        await session.commit()


async def _make_channel(provider: str, status: str = "active") -> int:
    from db.engine import async_session_factory
    from db.models import Channel

    async with async_session_factory() as session:
        ch = Channel(
            name=f"{provider}_official",
            provider=provider,
            base_url=f"https://{provider}.example.com",
            status=status,
        )
        session.add(ch)
        await session.commit()
        return ch.id


async def _make_account(
    channel_id: int, name: str, *,
    status: str = "active",
    quota_total: int | None = None,
    quota_used: int = 0,
    expires_at: datetime | None = None,
) -> int:
    from db.engine import async_session_factory
    from db.models import Account

    async with async_session_factory() as session:
        acc = Account(
            channel_id=channel_id,
            account_name=name,
            api_key="",
            api_key_ciphertext=None,
            status=status,
            quota_total=quota_total,
            quota_used=quota_used,
            expires_at=expires_at,
        )
        session.add(acc)
        await session.commit()
        return acc.id


# ============================================================
# Test 1: check_quota_alerts 80%
# ============================================================
def test_quota_alerts_80pct():
    print("\n=== Test 1: check_quota_alerts 80% 触发 ===")
    async def _run():
        await _reset_db()
        ch_id = await _make_channel("deepseek")
        await _make_account(ch_id, "ds_01", quota_total=100, quota_used=85)

        from intelligence.v44_alerts import check_quota_alerts
        with patch("intelligence.v44_alerts.send_alert", new=AsyncMock()) as mock:
            sent = await check_quota_alerts()
            assert sent == 1, f"应发 1 条告警, 实际 {sent}"
            assert mock.call_count == 1
            key = mock.call_args.kwargs.get("key") or mock.call_args.args[1]
            assert "quota_near_limit" in key, f"key 应含 quota_near_limit, 实际 {key}"
            print(f"  [OK] 85/100 触发, key={key}")

    asyncio.run(_run())


# ============================================================
# Test 2: check_quota_alerts 100% 触发 (exhausted)
# ============================================================
def test_quota_alerts_100pct():
    print("\n=== Test 2: check_quota_alerts 100% 触发 (exhausted) ===")
    async def _run():
        await _reset_db()
        ch_id = await _make_channel("deepseek")
        await _make_account(ch_id, "ds_01", quota_total=100, quota_used=100)

        from intelligence.v44_alerts import check_quota_alerts
        with patch("intelligence.v44_alerts.send_alert", new=AsyncMock()) as mock:
            sent = await check_quota_alerts()
            assert sent == 1, f"应发 1 条告警, 实际 {sent}"
            key = mock.call_args.kwargs.get("key") or mock.call_args.args[1]
            assert "account_exhausted" in key, f"key 应含 account_exhausted, 实际 {key}"
            print(f"  [OK] 100/100 触发, key={key}")

    asyncio.run(_run())


# ============================================================
# Test 3: check_quota_alerts 50% 不触发
# ============================================================
def test_quota_alerts_50pct_no_alert():
    print("\n=== Test 3: check_quota_alerts 50% 不触发 ===")
    async def _run():
        await _reset_db()
        ch_id = await _make_channel("deepseek")
        await _make_account(ch_id, "ds_01", quota_total=100, quota_used=50)

        from intelligence.v44_alerts import check_quota_alerts
        with patch("intelligence.v44_alerts.send_alert", new=AsyncMock()) as mock:
            sent = await check_quota_alerts()
            assert sent == 0, f"应发 0 条告警, 实际 {sent}"
            mock.assert_not_called()
            print(f"  [OK] 50/100 不触发 (低于 80%)")

    asyncio.run(_run())


# ============================================================
# Test 4: check_quota_alerts quota_total=0 不触发
# ============================================================
def test_quota_alerts_zero_total():
    print("\n=== Test 4: check_quota_alerts quota_total=0 不触发 ===")
    async def _run():
        await _reset_db()
        ch_id = await _make_channel("deepseek")
        await _make_account(ch_id, "ds_01", quota_total=0, quota_used=999)

        from intelligence.v44_alerts import check_quota_alerts
        with patch("intelligence.v44_alerts.send_alert", new=AsyncMock()) as mock:
            sent = await check_quota_alerts()
            assert sent == 0, f"应发 0 条告警, 实际 {sent}"
            mock.assert_not_called()
            print(f"  [OK] quota_total=0 跳过 (避免除零/无意义告警)")

    asyncio.run(_run())


# ============================================================
# Test 5: check_no_active_accounts 0 active 触发
# ============================================================
def test_no_active_accounts_triggers():
    print("\n=== Test 5: check_no_active_accounts 0 active 触发 ===")
    async def _run():
        await _reset_db()
        ch_id = await _make_channel("qwen")
        # 所有账号 expired / disabled
        await _make_account(ch_id, "qwen_01", status="expired")
        await _make_account(ch_id, "qwen_02", status="disabled")

        from intelligence.v44_alerts import check_no_active_accounts
        with patch("intelligence.v44_alerts.send_alert", new=AsyncMock()) as mock:
            sent = await check_no_active_accounts()
            assert sent == 1, f"应发 1 条告警, 实际 {sent}"
            key = mock.call_args.kwargs.get("key") or mock.call_args.args[1]
            assert "no_active_account" in key
            assert "qwen" in key
            print(f"  [OK] 0 active 触发, key={key}")

    asyncio.run(_run())


# ============================================================
# Test 6: check_no_active_accounts 1+ active 不触发
# ============================================================
def test_no_active_accounts_quiet():
    print("\n=== Test 6: check_no_active_accounts 1+ active 不触发 ===")
    async def _run():
        await _reset_db()
        ch_id = await _make_channel("qwen")
        await _make_account(ch_id, "qwen_01", status="active")
        await _make_account(ch_id, "qwen_02", status="expired")  # 1 active 足够

        from intelligence.v44_alerts import check_no_active_accounts
        with patch("intelligence.v44_alerts.send_alert", new=AsyncMock()) as mock:
            sent = await check_no_active_accounts()
            assert sent == 0
            mock.assert_not_called()
            print(f"  [OK] 1 active 账号不触发")

    asyncio.run(_run())


# ============================================================
# Test 7: check_cost_rising 涨幅 >=30% 触发
# ============================================================
def test_cost_rising_triggers():
    print("\n=== Test 7: check_cost_rising 涨幅 50% 触发 ===")
    async def _run():
        await _reset_db()
        ch_id = await _make_channel("glm")
        from db.engine import async_session_factory
        from db.models import UsageLog

        now = datetime.now()
        # 上月 1 号 ~ 本月 1 号
        this_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        previous_end = this_start
        previous_start = (this_start - timedelta(days=1)).replace(day=1)

        async with async_session_factory() as session:
            # 上月: 10 元
            for _ in range(10):
                session.add(UsageLog(
                    channel_id=ch_id, model="glm-4", cost_estimate=1.0,
                    created_at=previous_start + timedelta(hours=1),
                ))
            # 本月: 20 元 (涨幅 100% > 30%)
            for _ in range(20):
                session.add(UsageLog(
                    channel_id=ch_id, model="glm-4", cost_estimate=1.0,
                    created_at=this_start + timedelta(hours=1),
                ))
            await session.commit()

        from intelligence.v44_alerts import check_cost_rising
        with patch("intelligence.v44_alerts.send_alert", new=AsyncMock()) as mock:
            sent = await check_cost_rising()
            assert sent == 1, f"应发 1 条告警, 实际 {sent}"
            key = mock.call_args.kwargs.get("key") or mock.call_args.args[1]
            assert "cost_rising" in key
            print(f"  [OK] 涨幅 100% 触发, key={key}")

    asyncio.run(_run())


# ============================================================
# Test 8: check_cost_rising 涨幅 <30% 不触发
# ============================================================
def test_cost_rising_quiet():
    print("\n=== Test 8: check_cost_rising 涨幅 10% 不触发 ===")
    async def _run():
        await _reset_db()
        ch_id = await _make_channel("glm")
        from db.engine import async_session_factory
        from db.models import UsageLog

        now = datetime.now()
        this_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        previous_end = this_start
        previous_start = (this_start - timedelta(days=1)).replace(day=1)

        async with async_session_factory() as session:
            # 上月: 10 元
            for _ in range(10):
                session.add(UsageLog(
                    channel_id=ch_id, model="glm-4", cost_estimate=1.0,
                    created_at=previous_start + timedelta(hours=1),
                ))
            # 本月: 11 元 (涨幅 10% < 30%)
            for _ in range(11):
                session.add(UsageLog(
                    channel_id=ch_id, model="glm-4", cost_estimate=1.0,
                    created_at=this_start + timedelta(hours=1),
                ))
            await session.commit()

        from intelligence.v44_alerts import check_cost_rising
        with patch("intelligence.v44_alerts.send_alert", new=AsyncMock()) as mock:
            sent = await check_cost_rising()
            assert sent == 0
            mock.assert_not_called()
            print(f"  [OK] 涨幅 10% 不触发 (低于 30% 阈值)")

    asyncio.run(_run())


# ============================================================
# Test 9: check_all_alerts 单点失败不中断
# ============================================================
def test_check_all_alerts_isolation():
    print("\n=== Test 9: check_all_alerts 单点失败不中断 ===")
    async def _run():
        await _reset_db()
        ch_id = await _make_channel("deepseek")
        await _make_account(ch_id, "ds_01", quota_total=100, quota_used=85)  # 80%

        from intelligence.v44_alerts import check_all_alerts, check_quota_alerts

        # 让 quota checker 抛异常, 其余两个应仍跑
        original = check_quota_alerts

        async def boom():
            raise RuntimeError("simulated DB error")

        # 替换 module 里的 checker 引用
        import intelligence.v44_alerts as va
        va.check_quota_alerts = boom

        with patch("intelligence.v44_alerts.send_alert", new=AsyncMock()) as mock:
            # 不会抛异常 (因为 except)
            sent = await check_all_alerts()
            # quota 失败 → quota 不发; no_active 与 cost 应正常跑 (没数据都不发)
            assert sent == 0, f"quota 失败, 其余 0 触发, 应 0, 实际 {sent}"
            mock.assert_not_called()
            print(f"  [OK] quota checker 抛异常被 except, 其余不中断")

        # 恢复
        va.check_quota_alerts = original

    asyncio.run(_run())


# ============================================================
# 入口
# ============================================================
def main():
    tests = [
        test_quota_alerts_80pct,
        test_quota_alerts_100pct,
        test_quota_alerts_50pct_no_alert,
        test_quota_alerts_zero_total,
        test_no_active_accounts_triggers,
        test_no_active_accounts_quiet,
        test_cost_rising_triggers,
        test_cost_rising_quiet,
        test_check_all_alerts_isolation,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            print(f"  [FAIL] {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n=== V4.4 告警测试: {len(tests) - failed}/{len(tests)} passed ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())