"""优惠去重第二道 (URL + 内容相似度组合) 单元测试 (2026-09-09)

背景: 9/8 stepfun 同一事件重复入库 11 条 — AI 每次生成措辞不同的标题,
精确 (provider, title) 匹配打不中; 同事件文本相似度实测 0.23~0.87,
纯文本阈值也不可靠。同一页面每轮询周期都会重抓 → 同 URL 短窗可激进。

规则 (healer._save_promotion):
- (provider, url) 24h 内                  → 无条件跳过
- (provider, url) 7d 内 且相似度 >= 0.50  → 跳过
- 任意 url 7d 内 且相似度 >= 0.70         → 跳过
"""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 夹具文本: A~A2 相似 0.917, A~DIFF 相似 0.142 (2026-09-09 本地实测)
_TEXT_A = (
    "阶跃星辰推出全新 Step Plan 订阅制套餐方案，包含 Flash Mini / Flash Plus / Flash Pro / Flash Max 四档，"
    "分别提供 400M、1600M、8000M、40000M Credits 月使用量。所有套餐均支持旗舰模型与智能路由，并附带 MCP 工具、"
    "多设备登录等基础功能。高阶套餐额外提供优先 API 速率与优先技术支持。"
)
_TEXT_A2 = _TEXT_A + "订阅用户还可参与限时开发者社区活动，获得额外 Credits 奖励。"
_TEXT_DIFF = (
    "Moonshot Kimi 研究员套餐五折优惠，月费 49 元含 2000 万 tokens，限新用户，"
    "覆盖 kimi-k3 与 kimi-v4-pro 模型，活动截至本月底，不接受延期。"
)

URL_MAIN = "https://platform.stepfun.com/step-plan"
URL_OTHER = "https://platform.stepfun.com/announce"


@pytest.fixture
def dedup_env(tmp_path, monkeypatch):
    """tmp sqlite 库 + 绑定 async_session_factory"""
    db_file = tmp_path / "dedup_test.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False, future=True)
    factory = async_sessionmaker(eng, expire_on_commit=False)

    import db.engine as dbeng
    monkeypatch.setattr(dbeng, "async_session_factory", factory)

    async def _setup():
        from db.models import Base
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_setup())
    yield factory
    asyncio.run(eng.dispose())


def _make_source():
    return SimpleNamespace(
        provider="stepfun",
        url=URL_MAIN,
        source_type="announcement",
    )


def _make_result(title, content, url=URL_MAIN, confidence=0.9):
    from schemas.intel import ParseResult
    return ParseResult(
        is_promotion=True,
        confidence=confidence,
        title=title,
        provider="stepfun",
        content=content,
        url=url,
        discount_desc=None,
    )


async def _insert_promo(factory, title, content, url, age):
    """直接插一条既有 promotion (created_at = now - age)"""
    from db.models import Promotion
    async with factory() as session:
        session.add(Promotion(
            provider="stepfun",
            title=title,
            content=content,
            url=url,
            status="active",
            confidence=0.9,
            raw_data="{}",
            flagged=0,
            created_at=datetime.now() - age,
        ))
        await session.commit()


async def _count_promos(factory):
    from db.models import Promotion
    from sqlalchemy import select, func
    async with factory() as session:
        return (await session.execute(select(func.count(Promotion.id)))).scalar()


def test_same_url_within_24h_blocked(dedup_env):
    """同 URL 24h 内无条件跳过 — 即使标题内容完全不同 (同页重解析)"""
    from intelligence.healer import _save_promotion

    asyncio.run(_insert_promo(dedup_env, "旧标题", _TEXT_DIFF, URL_MAIN, timedelta(hours=2)))
    result = _make_result("全新标题", _TEXT_A2, URL_MAIN)
    asyncio.run(_save_promotion(result, _make_source()))
    assert asyncio.run(_count_promos(dedup_env)) == 1, "同 URL 24h 内应去重跳过"


def test_same_url_3d_similar_blocked(dedup_env):
    """同 URL 3 天前 + 相似度 >= 0.50 → 跳过 (跨日同事件)"""
    from intelligence.healer import _save_promotion

    asyncio.run(_insert_promo(dedup_env, "Step Plan 订阅套餐上线", _TEXT_A, URL_MAIN, timedelta(days=3)))
    result = _make_result("StepFun 推出新版 Step Plan 订阅", _TEXT_A2, URL_MAIN)
    asyncio.run(_save_promotion(result, _make_source()))
    assert asyncio.run(_count_promos(dedup_env)) == 1, "同 URL 7d 内高相似应去重"


def test_same_url_3d_different_allowed(dedup_env):
    """同 URL 3 天前但内容完全不同 (相似 0.14 < 0.50) → 真新优惠, 放行"""
    from intelligence.healer import _save_promotion

    asyncio.run(_insert_promo(dedup_env, "Step Plan 订阅套餐上线", _TEXT_A, URL_MAIN, timedelta(days=3)))
    result = _make_result("Kimi 研究员套餐五折", _TEXT_DIFF, URL_MAIN)
    asyncio.run(_save_promotion(result, _make_source()))
    assert asyncio.run(_count_promos(dedup_env)) == 2, "同 URL 但真新事件应放行"


def test_diff_url_similar_blocked(dedup_env):
    """不同 URL (公告页 vs 价格页) + 相似度 >= 0.70 → 跳过 (同事件多页分发)"""
    from intelligence.healer import _save_promotion

    asyncio.run(_insert_promo(dedup_env, "Step Plan 上线", _TEXT_A, URL_OTHER, timedelta(hours=5)))
    result = _make_result("Step Plan 订阅方案发布", _TEXT_A2, URL_MAIN)
    asyncio.run(_save_promotion(result, _make_source()))
    assert asyncio.run(_count_promos(dedup_env)) == 1, "跨 URL 高相似应去重"


def test_diff_url_different_allowed(dedup_env):
    """不同 URL + 低相似 → 放行"""
    from intelligence.healer import _save_promotion

    asyncio.run(_insert_promo(dedup_env, "Step Plan 上线", _TEXT_A, URL_OTHER, timedelta(hours=5)))
    result = _make_result("Kimi 研究员套餐五折", _TEXT_DIFF, URL_MAIN)
    asyncio.run(_save_promotion(result, _make_source()))
    assert asyncio.run(_count_promos(dedup_env)) == 2


def test_same_url_beyond_7d_allowed(dedup_env):
    """同 URL 超过 7 天窗口 (8 天前) → 不再去重 (窗口外放行)"""
    from intelligence.healer import _save_promotion

    asyncio.run(_insert_promo(dedup_env, "Step Plan 订阅套餐上线", _TEXT_A, URL_MAIN, timedelta(days=8)))
    result = _make_result("StepFun 推出新版 Step Plan 订阅", _TEXT_A2, URL_MAIN)
    asyncio.run(_save_promotion(result, _make_source()))
    assert asyncio.run(_count_promos(dedup_env)) == 2, "7d 窗口外应放行"
