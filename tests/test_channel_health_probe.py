"""V4.9 (2026-08-25): channel_health_probe 测试

跑法:
    python tests/test_channel_health_probe.py
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


_passed = 0
_failed = 0


def check(desc, got, expected):
    global _passed, _failed
    if got == expected:
        _passed += 1
        print(f"  PASS  {desc}")
    else:
        _failed += 1
        print(f"  FAIL  {desc}  got={got!r}  expected={expected!r}")


def make_mock_response(status_code=200, text='{"data":[]}'):
    """mock httpx.Response"""
    r = AsyncMock()
    r.status_code = status_code
    r.text = text
    return r


# ===== _probe_one 单元测试 =====
def test_probe_one_success():
    """GET /v1/models 200 → success=True"""
    from intelligence.channel_health import _probe_one

    async def _run():
        mock_resp = make_mock_response(200, '{"data":[{"id":"gpt-4"}]}')
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("intelligence.channel_health._get_probe_client", return_value=mock_client):
            result = await _probe_one(1, "https://api.deepseek.com/v1", "sk-test")
        check("success=True", result["success"], True)
        check("status_code=200", result["status_code"], 200)
        check("error_type=unknown (成功无错)", result["error_type"], "unknown")

    asyncio.run(_run())


def test_probe_one_auth_failed():
    """401 → success=False, error_type=auth_failed"""
    from intelligence.channel_health import _probe_one

    async def _run():
        mock_resp = make_mock_response(401, '{"error":{"code":"invalid_api_key","message":"Invalid"}}')
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("intelligence.channel_health._get_probe_client", return_value=mock_client):
            result = await _probe_one(1, "https://api.deepseek.com/v1", "sk-bad")
        check("success=False", result["success"], False)
        check("error_type=auth_failed", result["error_type"], "auth_failed")
        check("error_code=invalid_api_key", result["error_code"], "invalid_api_key")

    asyncio.run(_run())


def test_probe_one_quota_exhausted():
    """429 + 余额不足 → quota_exhausted"""
    from intelligence.channel_health import _probe_one

    async def _run():
        mock_resp = make_mock_response(429, '{"code":"1113","message":"余额不足"}')
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("intelligence.channel_health._get_probe_client", return_value=mock_client):
            result = await _probe_one(1, "https://api.deepseek.com/v1", "sk-test")
        check("success=False", result["success"], False)
        check("error_type=quota_exhausted", result["error_type"], "quota_exhausted")
        check("error_code=1113", result["error_code"], "1113")

    asyncio.run(_run())


def test_probe_one_5xx():
    """500 → upstream_5xx, success=False"""
    from intelligence.channel_health import _probe_one

    async def _run():
        mock_resp = make_mock_response(500, "Internal Server Error")
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("intelligence.channel_health._get_probe_client", return_value=mock_client):
            result = await _probe_one(1, "https://api.deepseek.com/v1", "sk-test")
        check("success=False", result["success"], False)
        check("error_type=upstream_5xx", result["error_type"], "upstream_5xx")

    asyncio.run(_run())


def test_probe_one_timeout():
    """TimeoutException → network_transient"""
    import httpx
    from intelligence.channel_health import _probe_one

    async def _run():
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        with patch("intelligence.channel_health._get_probe_client", return_value=mock_client):
            result = await _probe_one(1, "https://api.deepseek.com/v1", "sk-test")
        check("success=False", result["success"], False)
        check("error_type=network_transient", result["error_type"], "network_transient")

    asyncio.run(_run())


# ===== cooldown_cleanup_task 测试 =====
def test_cleanup_task_returns_zero():
    """cleanup_task 返回清理数 (空时 0)"""
    from gateway.cooldown import cooldown_manager
    from intelligence.channel_health import cooldown_cleanup_task

    async def _run():
        n = await cooldown_cleanup_task()
        check("空清理返 0", n, 0)

    asyncio.run(_run())


def test_cleanup_task_cleans_expired():
    """清理过期 entry"""
    from gateway.cooldown import CooldownManager
    from intelligence.channel_health import cooldown_cleanup_task

    async def _run():
        from intelligence.channel_health import cooldown_cleanup_task
        from gateway.cooldown import cooldown_manager as global_cm
        # 直接在全局实例上构造「完全过期」entry (避免替换引用)
        import time as _time
        global_cm._state[1] = {
            "count": 1, "cooldown_until": _time.time() - 100,
            "first_failure_at": _time.time() - 200, "last_failure_at": _time.time() - 100,
            "last_error_type": "upstream_5xx", "last_error_code": None, "last_error_msg": "",
            "needs_human": False, "next_probe_at": _time.time() - 50,
            "probe_attempts": 1, "last_success_at": None, "manual_disabled_at": None,
        }
        try:
            n = await cooldown_cleanup_task()
            check("清理 1 条", n, 1)
        finally:
            global_cm._state.pop(1, None)

    asyncio.run(_run())


def test_probe_no_detached_error_after_session_close():
    """V6.9 regression: 旧 acc.channel 懒加载在 session 关闭后抛 DetachedInstanceError
    (生产探活 job 每分钟全失败); 新实现直读实例 dict, channel 未预载时走现查分支。

    用真实 sqlite 内存库跑 channel_health_probe 全流程 (account/channel/health_log
    三张表真实建表), 仅 _probe_one 打桩避免真实网络.
    """
    import intelligence.channel_health as ch_mod
    from db.models import Account, Base, Channel
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    mock_probe = AsyncMock(return_value={
        "account_id": 1, "success": True, "status_code": 200, "latency_ms": 5,
        "error_type": "unknown", "error_code": None, "error_msg": "",
    })

    async def _run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        TestSession = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with TestSession() as s:
            s.add(Channel(id=5, name="ch5", provider="x", base_url="https://api.example.com/v1"))
            s.add(Account(id=1, account_name="acc1", channel_id=5,
                          api_key="sk-test", api_key_ciphertext=None))
            await s.commit()
        with (
            patch.object(ch_mod, "async_session_factory", TestSession),
            patch.object(ch_mod, "_probe_one", mock_probe),
            patch.object(ch_mod.cooldown_manager, "accounts_due_for_probe", lambda: [1]),
        ):
            ok = await ch_mod.channel_health_probe()
        await engine.dispose()
        check("探活 1 ok", ok, 1)

    asyncio.run(_run())
    check("以 channel.base_url 发起探活", mock_probe.call_args[0][1], "https://api.example.com/v1")
    check("探活账号 id", mock_probe.call_args[0][0], 1)


def main():
    tests = [
        test_probe_one_success,
        test_probe_no_detached_error_after_session_close,
        test_probe_one_auth_failed,
        test_probe_one_quota_exhausted,
        test_probe_one_5xx,
        test_probe_one_timeout,
        test_cleanup_task_returns_zero,
        test_cleanup_task_cleans_expired,
    ]
    for t in tests:
        print(f"\n--- {t.__name__} ---")
        try:
            t()
        except Exception as e:
            global _failed
            _failed += 1
            print(f"  EXCEPTION: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n=== 结果: {_passed} passed, {_failed} failed ===")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())