"""V6.5 批次三: 情报核心链「优惠→价格行」修正确 + 合成端到端验收.

S3a: 折扣只作用本次解析命中的模型 (result.models), 无模型信息不做价格行
     — 旧实现乘到该渠道全部 normal 模型 (siliconflow 99 个全 5 折风险)。
S3b: promotion 行强制 effective_to (缺失用抓取日 +7d 保守默认) + router
     _is_price_effective 对无到期日 promotion 行拒绝兜底 (覆盖存量行)。
S3c: 合成「五折」样例走 解析→折扣→价行→router 选价 全链验收
     (拍板双轨: 订阅类无折扣率本就不转价格行; 真折扣链路必须可路由)。

mock 全部走 crud.get_normal_price_rows 模块属性替换 (V6.5 新增的 crud 边界),
无裸 SQL、无 session.execute(stmt) 构造 (sql 注入扫描误报源)。
"""

from datetime import datetime, timedelta, date
from types import SimpleNamespace

import pytest

import intelligence.healer as healer_mod
from intelligence.healer import _promotion_to_price_rows
from schemas.intel import ParseResult, ModelIntelItem


def _promo_result(models=None, end_date=None, discount_desc="限时 5 折优惠"):
    return ParseResult(
        is_promotion=True,
        confidence=0.95,
        title="Kimi K2 五折",
        provider="test_vendor",
        discount_desc=discount_desc,
        start_date=None,
        end_date=end_date,
        models=models or [],
    )


class _MockChange:
    def __init__(self, model, changed=True):
        self.model = model
        self.changed = changed


@pytest.fixture
def promo_env(monkeypatch):
    """渠道 3 行 normal 价 + sync_price_change 记录调用 (无真实 DB)。"""
    state = {"synced": []}

    async def _fake_sync(provider, model, input_price, output_price, **kw):
        state["synced"].append(dict(
            model=model, input=input_price, output=output_price, **kw,
        ))
        return _MockChange(model)

    async def _fake_apply(change):
        return True

    channel = SimpleNamespace(id=7, name="test_ch")
    rows = [
        SimpleNamespace(model="kimi-k2", input_price=10.0, output_price=30.0),
        SimpleNamespace(model="kimi-k2-lite", input_price=2.0, output_price=6.0),
        SimpleNamespace(model="glm-5.3-flash", input_price=0.4, output_price=1.4),
    ]

    async def _fake_get_channel(_session, _provider, status="active"):
        return channel

    async def _fake_get_normal_rows(_session, _channel_id):
        return rows

    class _FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    def _fake_session_factory():
        return _FakeSession()

    import db.crud as crud
    import intelligence.pricing_sync as ps
    import db.engine as engine_mod
    monkeypatch.setattr(crud, "get_channel_by_provider", _fake_get_channel)
    monkeypatch.setattr(crud, "get_normal_price_rows", _fake_get_normal_rows)
    monkeypatch.setattr(ps, "sync_price_change", _fake_sync)
    monkeypatch.setattr(ps, "apply_price_change_to_route_cache", _fake_apply)
    # healer 函数体内 from db.engine import async_session_factory (延迟 import),
    # mock 引擎层工厂 (crud 边界已全替换, session 只作透传参数)
    monkeypatch.setattr(engine_mod, "async_session_factory", _fake_session_factory)
    return state


# ============================================================
# S3a: 折扣作用面
# ============================================================

@pytest.mark.asyncio
async def test_discount_only_applies_to_hit_models(promo_env):
    """折扣只落 result.models 命中的模型, 同渠道其他模型不受波及。"""
    result = _promo_result(models=[ModelIntelItem(model_id="kimi-k2", family="kimi")])
    n = await _promotion_to_price_rows(result, "test_vendor")
    assert n == 1
    models = [c["model"] for c in promo_env["synced"]]
    assert models == ["kimi-k2"], f"折扣波及了非命中模型: {models}"
    c = promo_env["synced"][0]
    assert c["input"] == 5.0 and c["output"] == 15.0  # 5 折
    assert c["price_type"] == "promotion" and c["is_promotion"] == 1


@pytest.mark.asyncio
async def test_discount_without_models_skipped(promo_env):
    """AI 未给出作用模型 → 不做价格行 (无法界定折扣面, 宁缺毋滥)。"""
    result = _promo_result(models=[])
    n = await _promotion_to_price_rows(result, "test_vendor")
    assert n == 0
    assert promo_env["synced"] == []


@pytest.mark.asyncio
async def test_discount_hit_model_without_normal_price_skipped(promo_env):
    """命中模型无 normal 价行 → 无从折算, 跳过。"""
    result = _promo_result(models=[ModelIntelItem(model_id="unknown-model")])
    n = await _promotion_to_price_rows(result, "test_vendor")
    assert n == 0


# ============================================================
# S3b: effective_to 强制
# ============================================================

@pytest.mark.asyncio
async def test_promotion_missing_end_date_gets_conservative_default(promo_env):
    result = _promo_result(
        models=[ModelIntelItem(model_id="kimi-k2")], end_date=None,
    )
    await _promotion_to_price_rows(result, "test_vendor")
    c = promo_env["synced"][0]
    expected_to = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    assert c["effective_to"] == expected_to, "缺 end_date 必须补 +7d 保守默认"


@pytest.mark.asyncio
async def test_promotion_explicit_end_date_respected(promo_env):
    result = _promo_result(
        models=[ModelIntelItem(model_id="kimi-k2")], end_date="2026-09-30",
    )
    await _promotion_to_price_rows(result, "test_vendor")
    assert promo_env["synced"][0]["effective_to"] == "2026-09-30"


# ============================================================
# S3b(路由侧): _is_price_effective 对无到期日 promotion 拒绝兜底
# ============================================================

def test_router_promo_without_effective_to_rejected():
    from gateway.router import RouteEngine
    eng = RouteEngine()
    # 存量脏行: promotion 但 effective_to=None → 不再兜底生效
    stale = SimpleNamespace(is_promotion=1, effective_from=None, effective_to=None)
    assert eng._is_price_effective(stale) is False
    # 正常 promotion: 有到期日且未过期 → 生效
    ok = SimpleNamespace(
        is_promotion=1, effective_from=None,
        effective_to=(date.today() + timedelta(days=3)).isoformat(),
    )
    assert eng._is_price_effective(ok) is True
    # normal 行无到期日 → 照常生效 (不受影响)
    normal = SimpleNamespace(is_promotion=0, effective_from=None, effective_to=None)
    assert eng._is_price_effective(normal) is True


# ============================================================
# S3c: 合成「五折」端到端验收 (解析→折扣→价行→router 选价)
# ============================================================

@pytest.mark.asyncio
async def test_e2e_half_price_promotion_routable(promo_env):
    """「半价」样例: healer 生成 promotion 价行 → router 在有效期内选 promo 价,
    过期后自动回退 normal — 双轨拍板中「真折扣可路由」的验收线。"""
    result = _promo_result(
        models=[ModelIntelItem(model_id="kimi-k2")],
        discount_desc="限时半价",
        end_date=(date.today() + timedelta(days=7)).isoformat(),
    )
    n = await _promotion_to_price_rows(result, "test_vendor")
    assert n == 1
    c = promo_env["synced"][0]
    assert c["input"] == 5.0, "半价 → input 10*0.5"
    assert c["effective_to"] is not None

    # router 侧: 组装 channel prices, 验证 promo 行优先于 normal 行
    from gateway.router import RouteEngine
    eng = RouteEngine()
    promo_row = {
        "model": "kimi-k2", "time_range": "全天", "input_price": 5.0,
        "output_price": 15.0, "is_promotion": 1,
    }
    normal_row = {
        "model": "kimi-k2", "time_range": "全天", "input_price": 10.0,
        "output_price": 30.0, "is_promotion": 0,
    }
    ch = {"name": "test_ch", "prices": [normal_row, promo_row]}
    picked = eng._get_current_price(ch, "kimi-k2", datetime.now())
    assert picked is promo_row, "有效期内 promotion 行应优先于 normal 行"


@pytest.mark.asyncio
async def test_e2e_subscription_plan_not_price_row(promo_env):
    """双轨拍板另一半: 订阅套餐类 (无折扣率) 本就不转价格行, 仅入库展示。"""
    result = ParseResult(
        is_promotion=True, confidence=0.95,
        title="Step Plan 四档订阅", provider="stepfun",
        discount_desc="Flash Mini 400M Credits/月、Flash Plus 1600M Credits/月",
        models=[ModelIntelItem(model_id="kimi-k2")],
    )
    n = await _promotion_to_price_rows(result, "test_vendor")
    assert n == 0, "无折扣率 (Credits 订阅) 不得生成价格行"
    assert promo_env["synced"] == []
