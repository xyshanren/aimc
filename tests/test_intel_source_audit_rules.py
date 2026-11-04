"""2026-11-04 — 盲区审计 Rule C/D 回归

Rule C: 代码里启用但库里没有任何状态行 → 从未真正执行过 (artificial_analysis /
        opencompass 自 V4.8.3 注册起从未跑过, 旧 Rule A 只扫 active 行抓不到)。
Rule D: 孤儿行 — DB 有行但代码里已无对应源 (URL 改过的旧行, 采集管理页看不到)。

跑法: python -m pytest tests/test_intel_source_audit_rules.py -q
"""

import asyncio
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _install_mock_env():
    class _MockSettings:
        openai_api_key = "sk-test"
        openai_base_url = "https://mock.local/v1"
        parse_model = "gpt-4o-mini"
        parse_confidence_threshold = 0.6
        crawl_enabled = True
        aimc_master_key = "test-key"
        route_refresh_interval = 600
        db_url = "sqlite+aiosqlite:///:memory:"
        db_path = "./data/aimc.db"

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = _MockSettings()


_install_mock_env()


@pytest.fixture()
async def factory(monkeypatch):
    """独立 in-memory 库 + 把 scheduler/db.engine 的 session 工厂指过来"""
    import warnings

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from db.models import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    import db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "async_session_factory", session_factory)

    yield session_factory

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        await engine.dispose()


class _FakeSource:
    def __init__(self, provider, source_type, url, enabled=True):
        self.provider = provider
        self.source_type = source_type
        self.url = url
        self.enabled = enabled


async def test_rule_c_and_d_alerts(factory, monkeypatch):
    from db.models import IntelSource
    import intelligence.alerter as alerter
    import intelligence.sources as sources_mod
    from intelligence.scheduler import _audit_source_structure

    # 内存源: 一个已启用但 DB 无行 (Rule C), 一个正常有行, 一个代码停用
    mem_sources = [
        _FakeSource("never_ran", "model_list", "https://example.com/list"),
        _FakeSource("healthy", "web_page", "https://example.com/p"),
        _FakeSource("disabled_src", "model_list", "https://example.com/d", enabled=False),
    ]
    monkeypatch.setattr(sources_mod, "get_all_sources", lambda: mem_sources)

    alerts = []

    async def _fake_alert(message, key=None, silence_seconds=None):
        alerts.append(key)
        return True

    monkeypatch.setattr(alerter, "send_alert", _fake_alert)

    async with factory() as session:
        # healthy: 正常行 (active 且有 last_check)
        from datetime import datetime
        session.add(IntelSource(
            provider="healthy", url="https://example.com/p",
            source_type="web_page", status="active", last_check_at=datetime.now(),
        ))
        # 孤儿行: provider/url 不在内存源里
        session.add(IntelSource(
            provider="legacy", url="https://old.example.com/x",
            source_type="web_page", status="disabled",
        ))
        await session.commit()

    await _audit_source_structure()

    assert "blindspot_audit:never_rowed" in alerts, "Rule C 应告警 (启用源无状态行)"
    assert "blindspot_audit:orphan_sources" in alerts, "Rule D 应告警 (孤儿行)"
    # 代码停用的源不应进 Rule C (它不是"忘了跑", 是有意停用)
    assert "disabled_src" not in str(alerts)
