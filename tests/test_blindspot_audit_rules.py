"""轴3 (2026-11-04): 盲区审计扩展判定测试 — 结构性死源 + 重复源

背景: qwen/hunyuan/moonshot model_list 三个 active 源从未成功执行
(配了 api_key_from 但渠道不存在 → 静默跳过), 旧盲区判据 (参与源 7 天
零变化) 结构性抓不到。新增: Rule A never_ran + Rule B duplicates。

跑法: python -m pytest tests/test_blindspot_audit_rules.py -v
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.models import Base, IntelSource


@pytest_asyncio.fixture
async def db_session(monkeypatch):
    """内存库 + create_all; 函数内 import 的 db.engine.async_session_factory 打桩"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    TestSession = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    import db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "async_session_factory", TestSession,
                        raising=False)
    async with TestSession() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def captured(monkeypatch):
    """打桩 send_alert, 捕获调用"""
    import intelligence.alerter as alerter_mod
    calls = []

    async def fake_alert(text, key=None, silence_seconds=None):
        calls.append({"text": text, "key": key})

    monkeypatch.setattr(alerter_mod, "send_alert", fake_alert)
    return calls


def _add_source(session, sid, provider, stype, status="active", last_check=None):
    session.add(IntelSource(id=sid, provider=provider, source_type=stype,
                            url="", poll_interval=900, status=status,
                            last_check_at=last_check))
    return session


class TestAuditSourceStructure:
    @pytest.mark.asyncio
    async def test_never_ran_source_alerts(self, db_session, captured):
        """Rule A: active 源 last_check NULL (从未执行) → 告警"""
        from datetime import datetime
        from intelligence.scheduler import _audit_source_structure

        _add_source(db_session, 3, "qwen", "model_list", "active", None)
        _add_source(db_session, 2, "glm", "model_list", "active",
                    datetime.now())
        await db_session.commit()

        await _audit_source_structure()

        assert len(captured) == 1
        assert "结构性死源" in captured[0]["text"]
        assert "qwen/model_list" in captured[0]["text"]
        assert captured[0]["key"] == "blindspot_audit:never_ran"

    @pytest.mark.asyncio
    async def test_duplicate_active_sources_alert(self, db_session, captured):
        """Rule B: 同 provider+type 双 active → 告警 (minimax api:// 死约定行形态)"""
        from datetime import datetime
        from intelligence.scheduler import _audit_source_structure

        now = datetime.now()
        _add_source(db_session, 45, "minimax", "model_list", "active", now)
        _add_source(db_session, 46, "minimax", "model_list", "active", now)
        await db_session.commit()

        await _audit_source_structure()

        assert len(captured) == 1
        assert "重复" in captured[0]["text"]
        assert "id=45" in captured[0]["text"]
        assert captured[0]["key"] == "blindspot_audit:duplicate_sources"

    @pytest.mark.asyncio
    async def test_clean_sources_no_alert(self, db_session, captured):
        """健康源 (全 checked + 无重复) → 零告警"""
        from datetime import datetime
        from intelligence.scheduler import _audit_source_structure

        now = datetime.now()
        _add_source(db_session, 2, "glm", "model_list", "active", now)
        _add_source(db_session, 6, "deepseek", "model_list", "active", now)
        await db_session.commit()

        await _audit_source_structure()

        assert captured == []

    @pytest.mark.asyncio
    async def test_disabled_never_ran_not_flagged(self, db_session, captured):
        """disabled 源即使 last_check NULL 也不算盲区 (显性死 ≠ 盲区)"""
        from intelligence.scheduler import _audit_source_structure

        _add_source(db_session, 3, "qwen", "model_list", "disabled", None)
        await db_session.commit()

        await _audit_source_structure()

        assert captured == []

    @pytest.mark.asyncio
    async def test_stale_check_not_flagged(self, db_session, captured):
        """last_check 距今 >48h 但非 NULL → 不告警 (自适应降频是健康态,
        8 天一查的 deepseek model_list 不得误报)"""
        from datetime import datetime, timedelta
        from intelligence.scheduler import _audit_source_structure

        old = datetime.now() - timedelta(days=8)
        _add_source(db_session, 6, "deepseek", "model_list", "active", old)
        await db_session.commit()

        await _audit_source_structure()

        assert captured == []

    @pytest.mark.asyncio
    async def test_never_ran_and_dupe_both_alert(self, db_session, captured):
        """两规则独立: 同时命中时各告各的 (不同 key 独立节流)"""
        from datetime import datetime
        from intelligence.scheduler import _audit_source_structure

        now = datetime.now()
        _add_source(db_session, 3, "qwen", "model_list", "active", None)
        _add_source(db_session, 45, "minimax", "model_list", "active", now)
        _add_source(db_session, 46, "minimax", "model_list", "active", now)
        await db_session.commit()

        await _audit_source_structure()

        keys = {c["key"] for c in captured}
        assert keys == {"blindspot_audit:never_ran",
                        "blindspot_audit:duplicate_sources"}
