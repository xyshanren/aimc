"""V4.8.5 P0 断链修复单元测试

覆盖四项 P0 修复:
1. cost_estimate: _estimate_cost 计算逻辑 + 价格查不到返 None
2. css_fallback_title: _infer_title_from_css 各分支 (heading / 属性 / 首行 / 兜底)
3. promotion_to_price_rows: 折扣率解析 (X折/半价/异常) + promotion 价行生成 + 路由优先
4. 索引迁移幂等: ensure_migrations 在有/无重复行场景下都能安全创建四元组唯一索引
5. router _get_current_price: promotion 行优先于 normal 行

跑法: python -m pytest tests/test_v485_broken_links.py -v
"""

import asyncio
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================
# P0-1: _estimate_cost 计算逻辑
# ============================================================

def test_estimate_cost_basic():
    """基础场景: 有 input/output price, 无 cache token"""
    from gateway.proxy import _estimate_cost

    channel = {
        "prices": [
            {"model": "test-model", "price_type": "normal", "time_range": "全天",
             "input_price": 10.0, "output_price": 30.0,
             "cached_input_price": None, "cache_write_price": None,
             "is_promotion": 0},
        ]
    }
    # mock route_engine._get_current_price 返回该 price
    import gateway.router as router_mod

    class _MockEngine:
        def _get_current_price(self, ch, model, hour):
            return channel["prices"][0]

        def _get_fuzzy_price(self, ch, model, hour):
            return None

    orig = router_mod.route_engine
    router_mod.route_engine = _MockEngine()
    try:
        # 1000 input + 500 output, price=10/30 元/百万tokens
        # cost = (1000*10 + 500*30) / 1_000_000 = 25000/1M = 0.025 元
        cost = _estimate_cost(channel, "test-model", 1000, 500)
        assert cost is not None
        assert abs(cost - 0.025) < 1e-9, f"基础费用应 0.025, 实际 {cost}"
    finally:
        router_mod.route_engine = orig


def test_estimate_cost_with_cache_hit():
    """OpenAI 风格 cache hit: cached 部分按 cached_input_price 计费"""
    from gateway.proxy import _estimate_cost

    channel = {
        "prices": [
            {"model": "m", "price_type": "normal", "time_range": "全天",
             "input_price": 10.0, "output_price": 30.0,
             "cached_input_price": 2.0, "cache_write_price": None,
             "is_promotion": 0},
        ]
    }
    import gateway.router as router_mod

    class _MockEngine:
        def _get_current_price(self, ch, model, hour):
            return channel["prices"][0]

        def _get_fuzzy_price(self, ch, model, hour):
            return None

    orig = router_mod.route_engine
    router_mod.route_engine = _MockEngine()
    try:
        # 1000 input (其中 400 cached) + 500 output
        # normal_input = 600, cached = 400
        # cost = (600*10 + 500*30 + 400*2) / 1M = (6000+15000+800)/1M = 0.0218
        cost = _estimate_cost(channel, "m", 1000, 500, cached_input_tokens=400)
        assert cost is not None
        expected = (600 * 10.0 + 500 * 30.0 + 400 * 2.0) / 1_000_000
        assert abs(cost - expected) < 1e-9, f"cache hit 费用应 {expected}, 实际 {cost}"
    finally:
        router_mod.route_engine = orig


def test_estimate_cost_no_price_returns_none():
    """价格查不到时返回 None (e.g. 999 兜底价 / model 不在价格表)"""
    from gateway.proxy import _estimate_cost

    import gateway.router as router_mod

    class _MockEngine:
        def _get_current_price(self, ch, model, hour):
            return None

        def _get_fuzzy_price(self, ch, model, hour):
            return None

    orig = router_mod.route_engine
    router_mod.route_engine = _MockEngine()
    try:
        cost = _estimate_cost({"prices": []}, "unknown-model", 1000, 500)
        assert cost is None, "价格查不到应返 None"
    finally:
        router_mod.route_engine = orig


def test_estimate_cost_cache_write():
    """Anthropic 风格 cache creation: 按 cache_write_price 计费"""
    from gateway.proxy import _estimate_cost

    channel = {
        "prices": [
            {"model": "m", "price_type": "normal", "time_range": "全天",
             "input_price": 10.0, "output_price": 30.0,
             "cached_input_price": 2.0, "cache_write_price": 12.5,
             "is_promotion": 0},
        ]
    }
    import gateway.router as router_mod

    class _MockEngine:
        def _get_current_price(self, ch, model, hour):
            return channel["prices"][0]

        def _get_fuzzy_price(self, ch, model, hour):
            return None

    orig = router_mod.route_engine
    router_mod.route_engine = _MockEngine()
    try:
        # 1000 input + 500 output + 200 cache_creation
        # cost = (1000*10 + 500*30 + 200*12.5) / 1M = (10000+15000+2500)/1M = 0.0275
        cost = _estimate_cost(
            channel, "m", 1000, 500, cache_creation_input_tokens=200
        )
        assert cost is not None
        expected = (1000 * 10.0 + 500 * 30.0 + 200 * 12.5) / 1_000_000
        assert abs(cost - expected) < 1e-9, f"cache write 费用应 {expected}, 实际 {cost}"
    finally:
        router_mod.route_engine = orig


# ============================================================
# P0-3: _infer_title_from_css 各分支
# ============================================================

def test_css_fallback_title_from_heading():
    """heading 元素 (h1-h6) 文本优先"""
    from intelligence.extractor import _infer_title_from_css

    class _MockTag:
        def __init__(self, html_str):
            self._html = html_str

        def find(self, tags):
            if "h2" in tags and "<h2>" in self._html:
                return _MockTag(self._html.split("<h2>")[1].split("</h2>")[0])
            return None

        def get_text(self, strip=False):
            return self._html

        def get(self, attr):
            return None

    target = _MockTag("<h2>限时五折优惠</h2>促销内容...")
    title = _infer_title_from_css(target, "促销内容...", [".content"])
    assert title == "限时五折优惠", f"heading 文本应被取, 实际 '{title}'"


def test_css_fallback_title_from_attribute():
    """无 heading 时取 title/aria-label 属性"""
    from intelligence.extractor import _infer_title_from_css

    class _MockTag:
        def find(self, tags):
            return None

        def get_text(self, strip=False):
            return "一些文本内容"

        def get(self, attr):
            if attr == "title":
                return "优惠活动详情"
            if attr == "aria-label":
                return None
            return None

    target = _MockTag()
    title = _infer_title_from_css(target, "一些文本内容", [".box"])
    assert title == "优惠活动详情", f"title 属性应被取, 实际 '{title}'"


def test_css_fallback_title_from_aria_label():
    """aria-label 属性兜底"""
    from intelligence.extractor import _infer_title_from_css

    class _MockTag:
        def find(self, tags):
            return None

        def get_text(self, strip=False):
            return "文本"

        def get(self, attr):
            if attr == "title":
                return None
            if attr == "aria-label":
                return "公告区"
            return None

    target = _MockTag()
    title = _infer_title_from_css(target, "文本", [".area"])
    assert title == "公告区", f"aria-label 应被取, 实际 '{title}'"


def test_css_fallback_title_from_first_line():
    """无 heading/属性时取文本首行"""
    from intelligence.extractor import _infer_title_from_css

    class _MockTag:
        def find(self, tags):
            return None

        def get_text(self, strip=False):
            return None

        def get(self, attr):
            return None

    target = _MockTag()
    text = "首行标题内容\n第二行详情\n第三行"
    title = _infer_title_from_css(target, text, [".content"])
    assert title == "首行标题内容", f"首行应被取, 实际 '{title}'"


def test_css_fallback_title_fallback_nonempty():
    """所有推断失败时兜底也保证非空 (修复 _save_promotion 丢弃断链)"""
    from intelligence.extractor import _infer_title_from_css

    class _MockTag:
        def find(self, tags):
            return None

        def get_text(self, strip=False):
            return None

        def get(self, attr):
            return None

    target = _MockTag()
    title = _infer_title_from_css(target, "", [".empty-selector"])
    assert title, "兜底标题不应为空"
    assert "CSS降级" in title or "empty-selector" in title, f"兜底应含 selector 标识, 实际 '{title}'"


# ============================================================
# P0-4: _promotion_to_price_rows 折扣率解析
# ============================================================

def test_promotion_discount_parse_half_price():
    """半价 → discount=0.5"""
    from intelligence.healer import _DISCOUNT_RE

    m = _DISCOUNT_RE.search("全场模型半价优惠")
    assert m is not None
    assert m.group(0) == "半价"


def test_promotion_discount_parse_x_zhe():
    """X折 → discount=X/10"""
    from intelligence.healer import _DISCOUNT_RE

    # 5折
    m = _DISCOUNT_RE.search("限时5折大促")
    assert m is not None
    assert float(m.group(1)) == 5.0

    # 8.5折
    m = _DISCOUNT_RE.search("8.5折优惠")
    assert m is not None
    assert float(m.group(1)) == 8.5

    # 0.5折 (极少见但应匹配)
    m = _DISCOUNT_RE.search("0.5折秒杀")
    assert m is not None
    assert float(m.group(1)) == 0.5


def test_promotion_discount_parse_no_match():
    """无折扣关键词时不匹配"""
    from intelligence.healer import _DISCOUNT_RE

    assert _DISCOUNT_RE.search("新模型上线") is None
    assert _DISCOUNT_RE.search("价格调整通知") is None
    assert _DISCOUNT_RE.search("") is None


def test_promotion_to_price_rows_no_discount_skips():
    """无折扣率 → 跳过, 返回 0"""
    from intelligence.healer import _promotion_to_price_rows
    from schemas.intel import ParseResult

    result = ParseResult(
        is_promotion=True, confidence=0.9,
        title="新模型上线通知",
        provider="siliconflow",
        discount_desc="新模型上线",  # 无折扣关键词
        content="欢迎体验新模型",
    )
    # 不需要 mock DB, 因为折扣率解析阶段就 return 0
    synced = asyncio.run(_promotion_to_price_rows(result, "siliconflow"))
    assert synced == 0, "无折扣率应跳过返 0"


def test_promotion_to_price_rows_invalid_discount_skips():
    """折扣率异常 (≤0 或 ≥1) → 跳过"""
    from intelligence.healer import _promotion_to_price_rows
    from schemas.intel import ParseResult

    # 10折 = 1.0 → 异常 (discount >= 1)
    result = ParseResult(
        is_promotion=True, confidence=0.9,
        title="10折优惠",
        provider="siliconflow",
        discount_desc="10折优惠",
        content="",
    )
    synced = asyncio.run(_promotion_to_price_rows(result, "siliconflow"))
    assert synced == 0, "折扣率>=1 应跳过返 0"


# ============================================================
# P0-4: router _get_current_price promotion 优先
# ============================================================

def test_get_current_price_prefers_promotion():
    """同一 model 有 normal + promotion 行时, promotion 优先返回"""
    from gateway.router import RouteEngine

    engine = RouteEngine()
    channel = {
        "prices": [
            {"model": "m1", "price_type": "normal", "time_range": "全天",
             "input_price": 10.0, "output_price": 30.0,
             "cached_input_price": None, "cache_write_price": None,
             "is_promotion": 0},
            {"model": "m1", "price_type": "promotion", "time_range": "全天",
             "input_price": 5.0, "output_price": 15.0,
             "cached_input_price": None, "cache_write_price": None,
             "is_promotion": 1},
        ]
    }
    price = engine._get_current_price(channel, "m1", 12)
    assert price is not None
    assert price["is_promotion"] == 1, "应优先返回 promotion 价行"
    assert price["input_price"] == 5.0


def test_get_current_price_normal_only():
    """只有 normal 行时返回 normal"""
    from gateway.router import RouteEngine

    engine = RouteEngine()
    channel = {
        "prices": [
            {"model": "m1", "price_type": "normal", "time_range": "全天",
             "input_price": 10.0, "output_price": 30.0,
             "cached_input_price": None, "cache_write_price": None,
             "is_promotion": 0},
        ]
    }
    price = engine._get_current_price(channel, "m1", 12)
    assert price is not None
    assert price.get("is_promotion") == 0


def test_get_current_price_no_match():
    """model 不在价格表 → None"""
    from gateway.router import RouteEngine

    engine = RouteEngine()
    channel = {
        "prices": [
            {"model": "m1", "price_type": "normal", "time_range": "全天",
             "input_price": 10.0, "output_price": 30.0,
             "cached_input_price": None, "cache_write_price": None,
             "is_promotion": 0},
        ]
    }
    price = engine._get_current_price(channel, "nonexistent", 12)
    assert price is None


def test_get_fuzzy_price_prefers_promotion():
    """fuzzy 匹配也优先 promotion 行"""
    from gateway.router import RouteEngine

    engine = RouteEngine()
    channel = {
        "prices": [
            {"model": "Qwen/Qwen2.5-72B", "price_type": "normal", "time_range": "全天",
             "input_price": 4.0, "output_price": 4.0,
             "cached_input_price": None, "cache_write_price": None,
             "is_promotion": 0},
            {"model": "Qwen/Qwen2.5-72B", "price_type": "promotion", "time_range": "全天",
             "input_price": 2.0, "output_price": 2.0,
             "cached_input_price": None, "cache_write_price": None,
             "is_promotion": 1},
        ]
    }
    # fuzzy: "qwen2.5-72b" 匹配 "Qwen/Qwen2.5-72B"
    price = engine._get_fuzzy_price(channel, "qwen2.5-72b", 12)
    assert price is not None
    assert price["is_promotion"] == 1, "fuzzy 也应优先 promotion"
    assert price["input_price"] == 2.0


# ============================================================
# P0-3: 索引迁移幂等 (真实 SQLite)
# ============================================================

@pytest.fixture
def migration_db(tmp_path, monkeypatch):
    """临时 SQLite, 用真实 engine 跑 ensure_migrations"""
    db_file = tmp_path / "v485_migration_test.db"
    async_url = f"sqlite+aiosqlite:///{db_file}"

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

    eng = create_async_engine(async_url, echo=False, future=True)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    # patch db.engine 的 engine 和 async_session_factory
    import db.engine as dbeng
    monkeypatch.setattr(dbeng, "engine", eng)
    monkeypatch.setattr(dbeng, "async_session_factory", factory)

    return db_file, eng, factory


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_migration_creates_index_on_fresh_db(migration_db):
    """新库: create_all 建表后, ensure_migrations 创建四元组唯一索引"""
    db_file, eng, factory = migration_db

    async def _run():
        from db.models import Base
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        from db.init_db import ensure_migrations
        await ensure_migrations()

        # 验证索引存在
        from sqlalchemy import text
        async with eng.connect() as conn:
            result = await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='price_snapshots'"
            ))
            names = {row[0] for row in result.fetchall()}
        return names

    names = _run_async(_run())
    assert "uq_ps_channel_model_type" in names, "新库应创建四元组唯一索引"
    assert "uq_ps_channel_model" not in names, "不应有旧二元组索引"


def test_migration_idempotent(migration_db):
    """幂等: 连续跑两次 ensure_migrations 不报错"""
    db_file, eng, factory = migration_db

    async def _run():
        from db.models import Base
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        from db.init_db import ensure_migrations
        await ensure_migrations()
        await ensure_migrations()  # 第二次应无操作

    _run_async(_run())  # 不抛异常即通过


def test_migration_dedup_before_index(migration_db):
    """有重复行时: 迁移先清理重复行 (保留 MAX(id)), 再建索引"""
    db_file, eng, factory = migration_db

    async def _run():
        from db.models import Base, PriceSnapshot
        from sqlalchemy import text

        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # 先手动插入重复行 (绕过 ORM 的唯一索引, 用 raw SQL)
        # 注意: create_all 建表时 __table_args__ 的 Index 会自动建,
        # 所以重复行插入会失败. 先 DROP 索引再插重复行, 模拟老库场景.
        async with eng.begin() as conn:
            await conn.execute(text("DROP INDEX IF EXISTS uq_ps_channel_model_type"))
            # 插入 channel (最小字段集)
            await conn.execute(text(
                "INSERT INTO channels (name, provider, base_url, status, priority) "
                "VALUES ('test_ch', 'test_provider', 'https://x', 'active', 100)"
            ))
            # 插入两条完全相同 (channel_id, model, price_type, time_range) 的行
            await conn.execute(text(
                "INSERT INTO price_snapshots (channel_id, model, price_type, time_range, "
                "input_price, output_price, source) "
                "VALUES (1, 'dup-model', 'normal', '全天', 10.0, 20.0, 'manual')"
            ))
            await conn.execute(text(
                "INSERT INTO price_snapshots (channel_id, model, price_type, time_range, "
                "input_price, output_price, source) "
                "VALUES (1, 'dup-model', 'normal', '全天', 10.0, 20.0, 'manual')"
            ))

        # 跑迁移 — 应先清理重复行再建索引
        from db.init_db import ensure_migrations
        await ensure_migrations()

        # 验证: 索引已建 + 重复行被清理 (只剩 1 条)
        async with eng.connect() as conn:
            result = await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='price_snapshots'"
            ))
            names = {row[0] for row in result.fetchall()}

            result = await conn.execute(text(
                "SELECT COUNT(*) FROM price_snapshots "
                "WHERE model='dup-model'"
            ))
            count = result.scalar()

        return names, count

    names, count = _run_async(_run())
    assert "uq_ps_channel_model_type" in names, "清理后应成功建索引"
    assert count == 1, f"重复行应被清理到 1 条, 实际 {count}"


def test_migration_old_index_replaced(migration_db):
    """老库有旧二元组索引时, 迁移 DROP 旧 + CREATE 新"""
    db_file, eng, factory = migration_db

    async def _run():
        from db.models import Base
        from sqlalchemy import text

        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # 模拟老库: DROP 新索引, CREATE 旧二元组索引
        async with eng.begin() as conn:
            await conn.execute(text("DROP INDEX IF EXISTS uq_ps_channel_model_type"))
            await conn.execute(text(
                "CREATE UNIQUE INDEX uq_ps_channel_model "
                "ON price_snapshots(channel_id, model)"
            ))

        from db.init_db import ensure_migrations
        await ensure_migrations()

        async with eng.connect() as conn:
            result = await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='price_snapshots'"
            ))
            names = {row[0] for row in result.fetchall()}

        return names

    names = _run_async(_run())
    assert "uq_ps_channel_model_type" in names, "应创建新四元组索引"
    assert "uq_ps_channel_model" not in names, "应删除旧二元组索引"
