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

from intelligence.balance_sync import (
    fetch_deepseek_balance,
    plan_sync,
    MAX_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
)


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


# === 自适应调度 (plan_sync 纯函数) ===

def test_plan_sync_idle_channel_uses_long_heartbeat():
    """空闲渠道 (burn=0): 6 小时心跳, 不频繁打上游"""
    due, interval, est = plan_sync(
        used_value=27.56, limit_value=5.0, elapsed_sec=600, burn_rate_cny_per_sec=0.0
    )
    assert due is False
    assert interval == MAX_INTERVAL_SECONDS
    assert est == 27.56  # 无流量 → 余额不变


def test_plan_sync_idle_channel_syncs_after_max_interval():
    due, interval, est = plan_sync(
        used_value=27.56, limit_value=5.0,
        elapsed_sec=MAX_INTERVAL_SECONDS + 1, burn_rate_cny_per_sec=0.0,
    )
    assert due is True


def test_plan_sync_active_channel_shorter_interval():
    """有流量: 间隔随 headroom/burn 缩短 (但不低于 2 分钟)"""
    due, interval, est = plan_sync(
        used_value=27.56, limit_value=5.0, elapsed_sec=60,
        burn_rate_cny_per_sec=0.02,  # 1.2 元/分钟
    )
    assert interval < MAX_INTERVAL_SECONDS
    assert interval >= MIN_INTERVAL_SECONDS
    # 60s * 0.02 = 1.2 元已花掉 → 推算余额 26.36
    assert est == pytest.approx(26.36)


def test_plan_sync_near_floor_syncs_immediately():
    """逼近余额下限: 立即 sync (快照已不可信)"""
    due, interval, est = plan_sync(
        used_value=6.0, limit_value=5.0, elapsed_sec=30,
        burn_rate_cny_per_sec=0.05,
    )
    assert due is True
    assert interval == MIN_INTERVAL_SECONDS


def test_plan_sync_below_floor_syncs_immediately():
    """推算余额已低于下限 → 立即 sync"""
    due, interval, est = plan_sync(
        used_value=5.5, limit_value=5.0, elapsed_sec=30,
        burn_rate_cny_per_sec=0.05,
    )
    assert due is True
    assert est < 5.0


def test_plan_sync_not_due_within_interval():
    """间隔未到 → 不 sync"""
    due, interval, est = plan_sync(
        used_value=27.56, limit_value=5.0, elapsed_sec=10,
        burn_rate_cny_per_sec=0.001,
    )
    assert due is False


def test_plan_sync_high_burn_clamps_to_min_interval():
    """烧钱极快 → 间隔压到下限 2 分钟"""
    due, interval, est = plan_sync(
        used_value=100.0, limit_value=5.0, elapsed_sec=5,
        burn_rate_cny_per_sec=10.0,
    )
    assert interval == MIN_INTERVAL_SECONDS
    assert due is False
