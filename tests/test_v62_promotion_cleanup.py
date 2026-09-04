"""Phase 3 体验: 优惠情报过期清理测试

覆盖 cleanup_expired_promotions 的状态刷新与保留期删除:
1. end_date 已过的 active → 标记 expired
2. expired 且 created_at 超保留期 (30 天) → 物理删除
3. expired 未超期 → 保留
4. end_date 无法解析 (如 "长期有效") → 不误判过期
5. end_date 在未来 → 保持原状态

注: _install_mock_env 每次运行建全新临时库, 无需清表; 标题全局唯一即可断言。

跑法:
    python -m pytest tests/test_v62_promotion_cleanup.py -v
"""

import asyncio
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v62_promo_")
    db_path = os.path.join(tmp_dir, "test.db")
    db_url = "sqlite+aiosqlite:///" + db_path

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
        host="0.0.0.0",
        port=8080,
        log_level="info",
    )


_install_mock_env()


async def _seed_promotion(factory, title, status="active", end_date=None,
                          created_at=None):
    from db.models import Promotion

    async with factory() as session:
        p = Promotion(provider="zhipu", title=title, status=status,
                      end_date=end_date, created_at=created_at or datetime.now())
        session.add(p)
        await session.commit()
        return p.id


async def _load_promotion_status_map(factory):
    from sqlalchemy import select

    from db.models import Promotion
    async with factory() as session:
        all_promotions = (await session.execute(
            select(Promotion.status, Promotion.title)
        )).all()
        return {title: status for status, title in all_promotions}


def test_cleanup_marks_expired_and_purges_over_retention():
    """end_date 已过 → 刷 expired; expired 超 30 天 → 删除; 未超期/未来日期保留"""
    from db.engine import async_session_factory
    from intelligence.promotion_cleanup import cleanup_expired_promotions

    async def scenario():
        from db.init_db import init_db
        await init_db()
        from db.engine import async_session_factory as factory

        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        # 保留: 未来截止的 active
        await _seed_promotion(factory, "keep-active-future",
                              status="active", end_date=tomorrow)
        # 刷过期: 昨天截止的 active
        await _seed_promotion(factory, "mark-expired",
                              status="active", end_date=yesterday)
        # 删除: expired 且 40 天前采集
        await _seed_promotion(factory, "purge-old-expired",
                              status="expired",
                              created_at=now - timedelta(days=40))
        # 保留: expired 但仅 10 天前采集
        await _seed_promotion(factory, "keep-recent-expired",
                              status="expired",
                              created_at=now - timedelta(days=10))
        # 保留: 无 end_date 的 active
        await _seed_promotion(factory, "keep-no-enddate",
                              status="active", end_date=None)

        result = await cleanup_expired_promotions(retention_days=30)
        status_map = await _load_promotion_status_map(factory)
        return result, status_map

    result, status_map = asyncio.run(scenario())

    assert result["marked_expired"] == 1
    assert result["deleted"] == 1
    assert "purge-old-expired" not in status_map
    assert status_map["mark-expired"] == "expired"
    assert status_map["keep-active-future"] == "active"
    assert status_map["keep-recent-expired"] == "expired"
    assert status_map["keep-no-enddate"] == "active"


def test_cleanup_unparseable_end_date_not_expired():
    """end_date 是自由文本 ("长期有效") → 解析失败, 不误判过期"""
    from intelligence.promotion_cleanup import cleanup_expired_promotions

    async def scenario():
        from db.init_db import init_db
        await init_db()
        from db.engine import async_session_factory as factory

        await _seed_promotion(factory, "long-term",
                              status="active", end_date="长期有效")
        result = await cleanup_expired_promotions(retention_days=30)
        status_map = await _load_promotion_status_map(factory)
        return result, status_map

    result, status_map = asyncio.run(scenario())
    assert result["marked_expired"] == 0
    assert status_map["long-term"] == "active"
