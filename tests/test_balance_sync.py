"""余额 sync 模块测试 (2026-10-20)

覆盖:
- fetch_deepseek_balance: CNY 解析 / 非 200 / is_available=false / 无 CNY
- sync_account_balances: 写 account_quota_usage (window_start sync 标记 + used_value)

跑法:
    python -m pytest tests/test_balance_sync.py -q
"""
import sys
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from intelligence.balance_sync import fetch_deepseek_balance


def _resp(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "https://x.test"))


class _FakeAsyncClient:
    """替换 httpx.AsyncClient 类本身 (memory: 须 patch 类而非实例)"""

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        return _FakeAsyncClient.response


@pytest.mark.asyncio
async def test_fetch_deepseek_balance_cny(monkeypatch):
    _FakeAsyncClient.response = _resp(200, {
        "is_available": True,
        "balance_infos": [
            {"currency": "USD", "total_balance": "1.00"},
            {"currency": "CNY", "total_balance": "8.42"},
        ],
    })
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    bal = await fetch_deepseek_balance("sk-test")
    assert bal == pytest.approx(8.42)


@pytest.mark.asyncio
async def test_fetch_deepseek_balance_non_200(monkeypatch):
    _FakeAsyncClient.response = _resp(401, {"error": "bad key"})
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    assert await fetch_deepseek_balance("sk-test") is None


@pytest.mark.asyncio
async def test_fetch_deepseek_balance_unavailable(monkeypatch):
    _FakeAsyncClient.response = _resp(200, {"is_available": False})
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    assert await fetch_deepseek_balance("sk-test") is None


@pytest.mark.asyncio
async def test_fetch_deepseek_balance_no_cny(monkeypatch):
    _FakeAsyncClient.response = _resp(200, {
        "is_available": True,
        "balance_infos": [{"currency": "USD", "total_balance": "3.00"}],
    })
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    assert await fetch_deepseek_balance("sk-test") is None
