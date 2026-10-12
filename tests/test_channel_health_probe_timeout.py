"""探活 per-channel 超时覆盖测试 (2026-10-12)

背景: taotoken 上游 /models 7d 实测 avg 3.2s / max 5.0s 贴默认 5s 超时,
偶发 network_transient 假阳性. PROBE_TIMEOUT_OVERRIDES 按渠道名单请求级
覆盖 (httpx timeout= 单请求参数, 不动共享 client 的全局默认).

跑法: python -m pytest tests/test_channel_health_probe_timeout.py -q
"""

import asyncio
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _install_mock_config():
    class _MockSettings:
        openai_api_key = "sk-test"
        openai_base_url = "https://mock.local/v1"
        parse_model = "gpt-4o-mini"
        parse_confidence_threshold = 0.6
        crawl_enabled = False
        aimc_master_key = "0" * 64
        route_refresh_interval = 600
        db_url = "sqlite+aiosqlite:///:memory:"
        db_path = ":memory:"
        health_probe_timeout = 5.0
        health_probe_concurrency = 8
        health_probe_max_batch = 50
        health_probe_total_timeout = 60.0

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = _MockSettings()


_install_mock_config()

import httpx  # noqa: E402


def test_override_table_has_taotoken():
    """覆盖表: taotoken_coding=8.0, 未列渠道=None (走 client 默认)"""
    from intelligence.channel_health import PROBE_TIMEOUT_OVERRIDES

    assert PROBE_TIMEOUT_OVERRIDES.get("taotoken_coding") == 8.0
    assert PROBE_TIMEOUT_OVERRIDES.get("deepseek_official") is None
    assert PROBE_TIMEOUT_OVERRIDES.get("sensenova_official") is None
    print("  [OK] 覆盖表: taotoken_coding=8.0, 其它渠道 None")


def test_probe_one_passes_timeout_kwarg():
    """_probe_one timeout 参数透传 httpx 请求; None → USE_CLIENT_DEFAULT"""
    import intelligence.channel_health as ch

    captured = {}

    class _FakeResp:
        status_code = 200
        text = '{"data": []}'

    class _FakeClient:
        async def get(self, url, headers=None, timeout=httpx.USE_CLIENT_DEFAULT):
            captured["timeout"] = timeout
            return _FakeResp()

    ch._PROBE_CLIENT = _FakeClient()
    try:
        r = asyncio.run(ch._probe_one(1, "https://x/v1", "sk-test", timeout=8.0))
        assert captured["timeout"] == 8.0, "timeout=8.0 应透传到请求"
        assert r["success"] is True

        r2 = asyncio.run(ch._probe_one(1, "https://x/v1", "sk-test"))
        assert captured["timeout"] == httpx.USE_CLIENT_DEFAULT, (
            "timeout=None 应回落 client 默认 (USE_CLIENT_DEFAULT)"
        )
        assert r2["success"] is True
    finally:
        ch._PROBE_CLIENT = None
    print("  [OK] timeout kwarg 透传 + None 回落 client 默认")


def test_probe_one_timeout_exception_still_classified():
    """慢渠道即使超时也走 network_transient 分类 (覆盖表只是放宽不是豁免)"""
    import intelligence.channel_health as ch

    class _SlowClient:
        async def get(self, url, headers=None, timeout=httpx.USE_CLIENT_DEFAULT):
            raise httpx.ReadTimeout("timed out", request=None)

    ch._PROBE_CLIENT = _SlowClient()
    try:
        r = asyncio.run(ch._probe_one(1, "https://x/v1", "sk-test", timeout=8.0))
        assert r["success"] is False
        assert r["error_type"] == "network_transient"
    finally:
        ch._PROBE_CLIENT = None
    print("  [OK] 超时仍分类 network_transient (放宽非豁免)")


if __name__ == "__main__":
    test_override_table_has_taotoken()
    test_probe_one_passes_timeout_kwarg()
    test_probe_one_timeout_exception_still_classified()
    print("3 tests passed")
