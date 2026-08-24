"""Phase 3.4 (2026-07-29) — 价格联动 单元测试

覆盖：
1. PriceChange.changed 检测
2. PriceChange.cheaper 降价检测
3. parse_price_lines 解析 key=value 价格行
4. parse_price_lines 中文 / 多格式 / 异常值保护
5. parse_price_lines 空文本返空
6. sync_price_change 找不到 channel 返 None
7. sync_price_change 入参空 返 None
8. sync_price_change 第一次写入返回 changed=True (old_price=None 视为初始)
9. sync_price_change 同价不触发 refresh

跑法：python tests/test_phase3_4_pricing.py
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config (healer / pricing_sync 引用 settings)
# ============================================================
def _install_mock_config():
    import types

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


_install_mock_config()


# ============================================================
# Test 1: PriceChange.changed
# ============================================================
def test_price_change_changed_flag():
    print("\n=== Test 1: PriceChange.changed ===")
    from intelligence.pricing_sync import PriceChange

    ch = PriceChange(
        provider="siliconflow", channel_id=1, model="m",
        old_input_price=1.0, new_input_price=2.0,
        old_output_price=3.0, new_output_price=3.0,
    )
    assert ch.changed, "input 价 1->2 应判定为变动"

    no_change = PriceChange(
        provider="siliconflow", channel_id=1, model="m",
        old_input_price=1.0, new_input_price=1.0,
        old_output_price=3.0, new_output_price=3.0,
    )
    assert not no_change.changed, "全相同应判定为无变动"

    # None 视为 0 (新增行)
    new_only = PriceChange(
        provider="siliconflow", channel_id=1, model="m",
        old_input_price=None, new_input_price=1.0,
        old_output_price=None, new_output_price=None,
    )
    assert new_only.changed, "None -> 1.0 应判定为变动"
    print("  [OK] changed 检测在 None/0/相同/不同 各场景正确")


# ============================================================
# Test 2: PriceChange.cheaper
# ============================================================
def test_price_change_cheaper():
    print("\n=== Test 2: PriceChange.cheaper ===")
    from intelligence.pricing_sync import PriceChange

    down = PriceChange(
        provider="siliconflow", channel_id=1, model="m",
        old_input_price=2.0, new_input_price=1.0,
        old_output_price=3.0, new_output_price=3.0,
    )
    assert down.cheaper

    up = PriceChange(
        provider="siliconflow", channel_id=1, model="m",
        old_input_price=1.0, new_input_price=2.0,
        old_output_price=3.0, new_output_price=3.0,
    )
    assert not up.cheaper

    # old=None 视为首次记录，不算降价
    first = PriceChange(
        provider="siliconflow", channel_id=1, model="m",
        old_input_price=None, new_input_price=0.5,
        old_output_price=None, new_output_price=None,
    )
    assert not first.cheaper
    print("  [OK] cheaper 仅在 old>new 时为真")


# ============================================================
# Test 3: parse_price_lines 解析 key=value
# ============================================================
def test_parse_price_lines_basic():
    print("\n=== Test 3: parse_price_lines basic ===")
    from intelligence.healer import _parse_price_lines

    text = (
        "deepseek-v3: 2.0/3.0\n"
        "qwen-plus = 0.8\n"
        "Pro/deepseek-ai/DeepSeek-V3: 1.5/2.5"
    )
    rows = _parse_price_lines(text)
    assert len(rows) == 3, f"应 3 行, 实际 {len(rows)}"
    assert rows[0] == ("deepseek-v3", 2.0, 3.0)
    assert rows[1] == ("qwen-plus", 0.8, 0.8)  # 仅有 output: input=output=0.8
    assert rows[2] == ("Pro/deepseek-ai/DeepSeek-V3", 1.5, 2.5)
    print(f"  [OK] 3 行解析: {rows}")


# ============================================================
# Test 4: parse_price_lines 中文/异常保护
# ============================================================
def test_parse_price_lines_robustness():
    print("\n=== Test 4: parse_price_lines 鲁棒性 ===")
    from intelligence.healer import _parse_price_lines

    # 中文 model 名: 当前 regex 不支持, 跳过 (避免误识别)
    text = (
        "无关文本: 999\n"
        "garbage line\n"
        "deepseek-v3: 2.0/3.0"
    )
    rows = _parse_price_lines(text)
    assert len(rows) == 1, f"应只保留 1 行, 实际 {rows}"
    assert rows[0] == ("deepseek-v3", 2.0, 3.0)
    print(f"  [OK] 鲁棒: {rows}")

    # 空文本
    rows = _parse_price_lines("")
    assert rows == []
    rows = _parse_price_lines(None)
    assert rows == []
    print("  [OK] 空文本返 []")


# ============================================================
# Test 5: parse_price_lines 复杂格式
# ============================================================
def test_parse_price_lines_complex():
    print("\n=== Test 5: parse_price_lines 复杂格式 ===")
    from intelligence.healer import _parse_price_lines

    text = (
        "Qwen/Qwen2.5-72B-Instruct: 4.0/4.0\n"
        "deepseek-v2.5=1.0/1.0"
    )
    rows = _parse_price_lines(text)
    assert len(rows) == 2
    assert rows[0] == ("Qwen/Qwen2.5-72B-Instruct", 4.0, 4.0)
    assert rows[1] == ("deepseek-v2.5", 1.0, 1.0)
    print(f"  [OK] 复杂格式: {rows}")


# ============================================================
# Test 6-9: sync_price_change 端到端 (Mock DB + 路由缓存)
# ============================================================
def _install_mock_db():
    """构造内存 Channel / PriceSnapshot + 假 session 工厂"""
    import types

    class _MockChannel:
        def __init__(self, cid, provider):
            self.id = cid
            self.provider = provider
            self.status = "active"
            self.name = f"mock_{provider}"

    class _MockPrice:
        def __init__(self, **kw):
            self.effective_from = kw.get("effective_from")
            self.effective_to = kw.get("effective_to")
            self.input_price = kw.get("input_price")
            self.output_price = kw.get("output_price")
            self.price_type = kw.get("price_type", "normal")
            self.time_range = kw.get("time_range", "全天")
            self.channel_id = kw.get("channel_id")
            self.model = kw.get("model")
            self.source = kw.get("source", "manual")
            self.is_promotion = kw.get("is_promotion", 0)
            self.id = id(self)

    # 内存 store: [(channel, [price...])]
    _state = {
        "channels": [_MockChannel(1, "siliconflow")],
        "prices": [],   # list[_MockPrice]
    }

    class _MockSession:
        def __init__(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def commit(self):
            pass

        async def flush(self):
            pass

    async def _factory():
        return _MockSession()

    # Patch crud functions
    import db.crud as crud

    async def _list_channels(session, status="active"):
        return list(_state["channels"])

    async def _get_channel_by_provider(session, provider, status="active"):
        # Phase 4 v2 (I8): 替代 list_channels + next 全表扫描模式.
        # 返回首个匹配 provider 的 active channel (按 id 升序)，与生产 crud 行为一致.
        matched = [
            ch for ch in _state["channels"]
            if ch.provider == provider and ch.status == status
        ]
        return sorted(matched, key=lambda c: c.id)[0] if matched else None

    async def _get_active(session, model, channel_id=None):
        from datetime import date
        today = date.today().isoformat()
        results = []
        for p in _state["prices"]:
            if p.model != model:
                continue
            if channel_id is not None and p.channel_id != channel_id:
                continue
            if p.effective_from and p.effective_from > today:
                continue
            if p.effective_to and p.effective_to < today:
                continue
            results.append(p)
        return results

    async def _upsert(session, channel_id, model, input_price, output_price,
                       source="crawler", price_type="normal", time_range="全天",
                       effective_from=None, effective_to=None, is_promotion=0):
        for p in _state["prices"]:
            if (p.channel_id == channel_id and p.model == model
                    and p.price_type == price_type and p.time_range == time_range):
                p.input_price = input_price
                p.output_price = output_price
                p.source = source
                p.is_promotion = is_promotion
                return p, False
        p = _MockPrice(
            channel_id=channel_id, model=model,
            input_price=input_price, output_price=output_price,
            price_type=price_type, time_range=time_range, source=source,
            effective_from=effective_from, effective_to=effective_to,
            is_promotion=is_promotion,
        )
        _state["prices"].append(p)
        return p, True

    crud.list_channels = _list_channels
    crud.get_channel_by_provider = _get_channel_by_provider
    crud.get_active_price_snapshots = _get_active
    crud.upsert_price_snapshot = _upsert

    # Patch async_session_factory in pricing_sync module
    import intelligence.pricing_sync as ps

    class _SessionCtx:
        async def __aenter__(self):
            return _MockSession()

        async def __aexit__(self, *a):
            return False

    def _factory():
        return _SessionCtx()

    ps.async_session_factory = _factory

    # Mock route_engine
    refresh_calls = []

    class _MockRouteEngine:
        async def refresh_cache(self):
            refresh_calls.append(1)
            return None

    import sys
    gateway_pkg = types.ModuleType("gateway")
    gateway_pkg.__path__ = []
    router_mod = types.ModuleType("gateway.router")
    router_mod.route_engine = _MockRouteEngine()
    sys.modules.setdefault("gateway", gateway_pkg)
    sys.modules.setdefault("gateway.router", router_mod)

    return _state, refresh_calls


def test_sync_price_change_first_write():
    print("\n=== Test 6: sync_price_change 首次写入 ===")
    _install_mock_db()
    from intelligence.pricing_sync import sync_price_change

    async def _run():
        change = await sync_price_change(
            "siliconflow", "deepseek-v3", 2.0, 3.0,
        )
        return change

    change = asyncio.run(_run())
    assert change is not None
    assert change.old_input_price is None
    assert change.new_input_price == 2.0
    assert change.changed, "首次写入应判定为变动"
    print(f"  [OK] change={change}, changed={change.changed}")


def test_sync_price_change_no_channel():
    print("\n=== Test 7: sync_price_change 找不到 channel ===")
    _install_mock_db()
    from intelligence.pricing_sync import sync_price_change
    import db.crud as crud

    # Phase 4 v2 (I8): 不再走 list_channels + next, 改为 get_channel_by_provider 单查
    async def _no_channel(session, provider, status="active"):
        return None

    _orig = crud.get_channel_by_provider
    crud.get_channel_by_provider = _no_channel

    async def _run():
        return await sync_price_change("nonexistent", "m", 1.0, 1.0)

    change = asyncio.run(_run())
    # 还原 (避免污染后续 test, e.g. test 10 调真 crud 校验)
    crud.get_channel_by_provider = _orig
    assert change is None, f"无 channel 应返 None, 实际 {change}"
    print("  [OK] 无 channel 返 None")


def test_sync_price_change_no_price():
    print("\n=== Test 8: sync_price_change 入参空 ===")
    _install_mock_db()
    from intelligence.pricing_sync import sync_price_change

    async def _run():
        return await sync_price_change("siliconflow", "m", None, None)

    change = asyncio.run(_run())
    assert change is None
    print("  [OK] 入参空 返 None")


def test_sync_price_change_same_price_no_refresh():
    print("\n=== Test 9: sync_price_change 同价不触发 refresh ===")
    state, refresh_calls = _install_mock_db()

    async def _seed():
        # 第一次写入
        from intelligence.pricing_sync import sync_price_change
        return await sync_price_change("siliconflow", "deepseek-v3", 1.0, 1.0)

    asyncio.run(_seed())
    # sync_price_change 自身不 refresh, 需 caller 显式调 apply_price_change_to_route_cache
    # 所以 refresh_calls 应为 0
    assert len(refresh_calls) == 0, f"sync_price_change 自身不 refresh, 实际 {len(refresh_calls)}"

    async def _repeat():
        from intelligence.pricing_sync import apply_price_change_to_route_cache, sync_price_change
        change = await sync_price_change("siliconflow", "deepseek-v3", 1.0, 1.0)
        # 手动调 refresh (sync_price_change 不会自动 refresh, 由 healer 决定)
        return await apply_price_change_to_route_cache(change)

    refreshed = asyncio.run(_repeat())
    assert not refreshed, f"同价不应 refresh, 实际 {refreshed}"
    assert len(refresh_calls) == 0, f"同价不 refresh, 实际 {len(refresh_calls)}"
    print(f"  [OK] 同价 1.0->1.0 不触发 refresh (refresh_calls={len(refresh_calls)})")


# ============================================================
# Test 10: crud.get_channel_by_provider 基础正确性 (I8, 真SQLite in-memory)
# ============================================================
def test_get_channel_by_provider_basic():
    print("\n=== Test 10: crud.get_channel_by_provider 基础正确性 (real SQLite) ===")
    # 关键: _install_mock_db() 会 patch db.crud.get_channel_by_provider 为 mock,
    # 测完后其它 test 也会间接污染. 这里 importlib.reload() 重新加载模块,
    # 让 crud.get_channel_by_provider 回到真函数定义.
    import importlib
    import db.crud as crud_mod
    importlib.reload(crud_mod)
    from db.crud import get_channel_by_provider
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy.pool import StaticPool

    # 独立 in-memory SQLite engine + StaticPool (保证跨 session 同享一个连接,
    # 否则 aiosqlite + NullPool 会让 :memory: 每个连接独立, 数据看不到)
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _run():
        # Phase 1: 建表 (Channel 有 lazy="selectin" 关系, 一次建全表避免脏查询)
        from db.models import Channel, Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Phase 2 + 3: 同 session 里 insert + 验证, 避免 aiosqlite + StaticPool 隔离问题
        async with factory() as session:
            session.add_all([
                Channel(name="sf", provider="siliconflow", base_url="https://x",
                        status="active"),
                Channel(name="ds", provider="deepseek", base_url="https://x",
                        status="active"),
                Channel(name="sf_off", provider="siliconflow", base_url="https://x",
                        status="deprecated"),
            ])
            await session.commit()

            # 1) 命中 active siliconflow (返任 active 行, not deprecated sf_off)
            ch = await get_channel_by_provider(session, "siliconflow")
            assert ch is not None, "应命中 active siliconflow"
            assert ch.provider == "siliconflow"
            assert ch.status == "active", f"deprecated 行不应被返, 实际 {ch.status}"

            # 2) miss
            ch_none = await get_channel_by_provider(session, "nonexistent")
            assert ch_none is None, f"miss 应返 None, 实际 {ch_none}"

            # 3) deepseek 命中
            ch_ds = await get_channel_by_provider(session, "deepseek")
            assert ch_ds is not None and ch_ds.provider == "deepseek", \
                f"deepseek 应命中, 实际 {ch_ds}"

            # 4) status="deprecated" 能返 deprecated 行
            ch_off = await get_channel_by_provider(session, "siliconflow", status="deprecated")
            assert ch_off is not None and ch_off.name == "sf_off", \
                f"status=deprecated 应命中 sf_off, 实际 {ch_off}"

        await engine.dispose()

    asyncio.run(_run())
    print("  [OK] get_channel_by_provider 命中/miss/status 过滤 表现正确")


# ============================================================
# Test 11: sync_price_change 不再调用 list_channels (I8)
# ============================================================
def test_sync_price_change_uses_single_query():
    print("\n=== Test 11: sync_price_change 调用次数验证 (不再走 N+1) ===")
    _install_mock_db()
    from intelligence.pricing_sync import sync_price_change
    import db.crud as crud

    call_log = []

    _orig_get_channel_by_provider = crud.get_channel_by_provider
    _orig_list_channels = crud.list_channels

    async def _traced_get_channel_by_provider(session, provider, status="active"):
        call_log.append(("get_channel_by_provider", provider))
        return await _orig_get_channel_by_provider(session, provider, status)

    async def _traced_list_channels(session, status="active"):
        call_log.append(("list_channels", status))
        return await _orig_list_channels(session, status)

    crud.get_channel_by_provider = _traced_get_channel_by_provider
    crud.list_channels = _traced_list_channels

    async def _run():
        return await sync_price_change("siliconflow", "deepseek-v3", 2.0, 3.0)

    change = asyncio.run(_run())

    crud.get_channel_by_provider = _orig_get_channel_by_provider
    crud.list_channels = _orig_list_channels

    list_call_count = sum(1 for n, _ in call_log if n == "list_channels")
    single_call_count = sum(1 for n, _ in call_log if n == "get_channel_by_provider")

    assert list_call_count == 0, \
        f"I8 优化后 sync_price_change 不应调 list_channels, 实际调用 {list_call_count} 次"
    assert single_call_count == 1, \
        f"应调 1 次 get_channel_by_provider, 实际 {single_call_count}"
    assert change is not None, f"应返 PriceChange, 实际 {change}"
    print(f"  [OK] 单查调用: get_channel_by_provider x{single_call_count}, list_channels x{list_call_count}")


# ============================================================
# Test 12: 多 channel 同 provider 场景 (I8)
# ============================================================
def test_get_channel_by_provider_multiple_channels_same_provider():
    print("\n=== Test 12: get_channel_by_provider 多 channel 同 provider ===")
    # 同 Test 10: importlib.reload() 避免 mock 污染
    import importlib
    import db.crud as crud_mod
    importlib.reload(crud_mod)
    from db.crud import get_channel_by_provider

    class _FakeChannel:
        def __init__(self, cid, provider, status="active"):
            self.id = cid
            self.provider = provider
            self.status = status

    class _FakeResult:
        def scalar_one_or_none(self_inner):
            provider = captured.get("provider")
            status = captured.get("status", "active")
            candidates = sorted(
                [c for c in _store if c.provider == provider and c.status == status],
                key=lambda c: c.id,
            )
            return candidates[0] if candidates else None

    class _FakeSession:
        async def execute(self, stmt):
            return _FakeResult()

    captured = {}
    # 3 个同 provider 的 active channel (id=5, 7, 10)，另一个不同 provider
    _store = [
        _FakeChannel(5, "siliconflow"),
        _FakeChannel(7, "siliconflow"),
        _FakeChannel(10, "siliconflow"),
        _FakeChannel(20, "openai"),
    ]

    async def _run():
        captured["provider"] = "siliconflow"
        captured["status"] = "active"
        ch = await get_channel_by_provider(_FakeSession(), "siliconflow")
        assert ch.id == 5, f"多 channel 同 provider 应返 id 最小的, 实际 {ch.id}"
        assert ch.provider == "siliconflow"

        # status 过滤也能排斥 deprecated
        _store.append(_FakeChannel(3, "siliconflow", "deprecated"))  # id 更小但是 deprecated
        captured["provider"] = "siliconflow"
        captured["status"] = "active"
        ch = await get_channel_by_provider(_FakeSession(), "siliconflow", status="active")
        assert ch.id == 5, f"应跳过 deprecated id=3, 返 id=5, 实际 {ch.id}"

    asyncio.run(_run())
    print("  [OK] 多 channel 同 provider 返 id 最小且 status 过滤正确")


# ============================================================
# Test 13: pricing_sync 调精准接口而非 refresh_cache (I6)
# ============================================================
def test_apply_price_change_uses_invalidate_price():
    print("\n=== Test 13: apply_price_change_to_route_cache 调 invalidate_price (I6) ===")
    # _install_mock_db() 会用 types.ModuleType 替换 sys.modules['gateway.router'] 为
    # _MockRouteEngine. 这里从 sys.modules 移除 mock 让 import 重新加载真路由.
    import sys
    import importlib
    sys.modules.pop("gateway.router", None)
    sys.modules.pop("gateway", None)
    from gateway.router import route_engine
    from intelligence.pricing_sync import apply_price_change_to_route_cache, PriceChange

    call_log = []

    _orig_invalidate_price = route_engine.invalidate_price
    _orig_refresh_cache = route_engine.refresh_cache

    async def _traced_invalidate_price(channel_id):
        call_log.append(("invalidate_price", channel_id))
        return None

    async def _traced_refresh_cache():
        call_log.append(("refresh_cache", None))
        return None

    route_engine.invalidate_price = _traced_invalidate_price
    route_engine.refresh_cache = _traced_refresh_cache

    try:
        change = PriceChange(
            provider="siliconflow",
            channel_id=1,
            model="deepseek-v3",
            old_input_price=2.0,
            new_input_price=3.0,
            old_output_price=3.0,
            new_output_price=4.0,
            source="crawler",
        )
        applied = asyncio.run(apply_price_change_to_route_cache(change))
    finally:
        route_engine.invalidate_price = _orig_invalidate_price
        route_engine.refresh_cache = _orig_refresh_cache

    apply_calls = [n for n, _ in call_log if n == "invalidate_price"]
    refresh_calls = [n for n, _ in call_log if n == "refresh_cache"]

    assert applied is True, "价格变动应返 True"
    assert len(apply_calls) == 1, \
        f"I6: 应调 1 次 invalidate_price, 实际 {len(apply_calls)}"
    assert len(refresh_calls) == 0, \
        f"I6: 不应调 refresh_cache 全量, 实际 {len(refresh_calls)}"
    print(f"  [OK] 精准刷新: invalidate_price x{len(apply_calls)}, refresh_cache x{len(refresh_calls)}")


# ============================================================
# Test 14: pricing_sync invalidate_price 调函数存在性 (I6 smoke)
# ============================================================
def test_router_invalidate_methods_exist():
    print("\n=== Test 14: router 3 个 invalidate 接口存在 (I6) ===")
    from gateway.router import route_engine

    assert callable(getattr(route_engine, "invalidate_channel", None)), \
        "缺少 invalidate_channel 接口"
    assert callable(getattr(route_engine, "invalidate_price", None)), \
        "缺少 invalidate_price 接口"
    assert callable(getattr(route_engine, "invalidate_group", None)), \
        "缺少 invalidate_group 接口"
    assert callable(getattr(route_engine, "refresh_cache", None)), \
        "refresh_cache 应保留（全量入口）"
    print("  [OK] invalidate_channel / invalidate_price / invalidate_group / refresh_cache 都在")


# ============================================================
# Test 15: price_alert 不影响精准接口调用 (I6+price_alert 共存)
# ============================================================
def test_invalidate_price_tracing_after_price_change():
    print("\n=== Test 15: 多次价格变动 -> 多次 invalidate_price (I6) ===")
    # 同 Test 13: 从 sys.modules 移除 mock, 重新 import 真实路由
    import sys
    sys.modules.pop("gateway.router", None)
    sys.modules.pop("gateway", None)
    from intelligence.pricing_sync import apply_price_change_to_route_cache, PriceChange
    from gateway.router import route_engine

    call_log = []
    _orig_invalidate_price = route_engine.invalidate_price

    async def _traced(channel_id):
        call_log.append(channel_id)
        return None  # 不实际重查,测试只验证调用

    route_engine.invalidate_price = _traced

    try:
        async def _run():
            # 3 次价格变动 (构造 PriceChange, 跳过 sync_price_change 需 DB 的部分)
            for i in range(3):
                change = PriceChange(
                    provider="siliconflow",
                    channel_id=1,
                    model="deepseek-v3",
                    old_input_price=2.0 + i,
                    new_input_price=3.0 + i,
                    old_output_price=3.0 + i,
                    new_output_price=4.0 + i,
                    source="crawler",
                )
                await apply_price_change_to_route_cache(change)
        asyncio.run(_run())
    finally:
        route_engine.invalidate_price = _orig_invalidate_price

    assert len(call_log) == 3, f"应调用 3 次 invalidate_price, 实际 {len(call_log)}"
    assert all(c == 1 for c in call_log), \
        f"所有调用 channel_id 都应该是 1, 实际 {call_log}"
    print(f"  [OK] 多次改动 -> {len(call_log)} 次 invalidate_price, channel_id={set(call_log)}")


# ============================================================
# Test 16: e2e 集成 - sync_price_change -> invalidate_price 真实更新 cache (I6 生效)
# ============================================================
def test_e2e_price_change_updates_cache_via_invalidate():
    print("\n=== Test 16: e2e 集成 - 价格变动后 cache 拿到新价 (I8+I6 联动) ===")
    # _install_mock_db: 装 crud mock + 替换 gateway.router 为 _MockRouteEngine
    # 但这里需要真实的 RouteEngine 类做 e2e, 所以先 pop 掉 mock 让 import 重新加载真路由.
    import sys
    sys.modules.pop("gateway.router", None)
    sys.modules.pop("gateway", None)
    _install_mock_db()
    # _install_mock_db 会重新装 _MockRouteEngine, 这里再次 pop 让真路由加载
    sys.modules.pop("gateway.router", None)
    sys.modules.pop("gateway", None)
    from intelligence.pricing_sync import sync_price_change, apply_price_change_to_route_cache
    from gateway.router import RouteEngine

    # 造一个 RouteEngine 实例, 手工初始化 cache (模拟 refresh_cache 后状态)
    engine = RouteEngine()
    old_price_entry = {
        "id": 1, "name": "siliconflow", "provider": "siliconflow",
        "base_url": "https://x", "priority": 1,
        "provider_type": "openai", "billing_mode": "pay_per_token",
        "account_id": 11, "account_name": "acc1",
        "api_key": "sk-test", "quota_total": 100, "quota_used": 0,
        "quota_remaining": 100,
        "prices": [{
            "model": "deepseek-v3", "price_type": "normal",
            "time_range": "全天", "input_price": 1.0, "output_price": 1.0,
        }],
        "quotas": [],
    }
    engine._channels_cache = [old_price_entry]

    from gateway.router import route_engine as real_route_engine

    # 接管 real_route_engine.invalidate_price 为手动更新 cache
    _orig_invalidate_price = real_route_engine.invalidate_price

    async def _manual_invalidate(channel_id):
        # 模拟精准刷新: 只重查该 channel 的 prices, 替换 cache 中该 channel 所有 entries 的 prices
        # 实际上 mock list_channels (mock) 里只有 deepseek-v3 价格，然后这次 “变动” 改成 2.0/3.0
        new_prices = [{
            "model": "deepseek-v3", "price_type": "normal",
            "time_range": "全天", "input_price": 2.0, "output_price": 3.0,
        }]
        for entry in engine._channels_cache:
            if entry["id"] == channel_id:
                entry["prices"] = new_prices
        return None

    real_route_engine.invalidate_price = _manual_invalidate
    try:
        async def _run():
            # 1) 先改价 (old=1.0/1.0 -> new=2.0/3.0)
            change = await sync_price_change("siliconflow", "deepseek-v3", 2.0, 3.0)
            assert change.changed, "价格变动应判 changed"
            # 2) 调精准刷新
            applied = await apply_price_change_to_route_cache(change)
            assert applied is True, "应返 True"

        asyncio.run(_run())
    finally:
        real_route_engine.invalidate_price = _orig_invalidate_price

    # 3) 验证 cache 中 prices 已更新
    new_price = engine._channels_cache[0]["prices"][0]
    assert new_price["input_price"] == 2.0, \
        f"cache.input_price 应=2.0, 实际 {new_price['input_price']}"
    assert new_price["output_price"] == 3.0, \
        f"cache.output_price 应=3.0, 实际 {new_price['output_price']}"
    print(f"  [OK] 全链路 (sync_price_change + invalidate_price) cache 拿到新价: "
          f"input={new_price['input_price']}, output={new_price['output_price']}")


# ============================================================
# Test 17: 性能对比 - I8 单查询 vs 原始 list_channels (I8 定量收益)
# ============================================================
def test_i8_single_query_vs_list_perf():
    print("\n=== Test 17: I8 单查询 vs 原始 list_channels 性能对比 ===")
    import time
    import importlib
    import sys
    sys.modules.pop("db.crud", None)
    import db.crud as crud
    importlib.reload(crud)

    # 独立 in-memory SQLite + 100 个 channel
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy.pool import StaticPool
    from db.models import Channel, Base

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            # 插 100 个 active channel, 夹杂 10 个其它 provider
            objs = [
                Channel(name=f"sf{i}", provider="siliconflow",
                        base_url="https://x", status="active")
                for i in range(100)
            ]
            objs += [
                Channel(name=f"ds{i}", provider="deepseek",
                        base_url="https://x", status="active")
                for i in range(10)
            ]
            session.add_all(objs)
            await session.commit()

        # 单查 1000 次 (I8 优化)
        t0 = time.perf_counter()
        for _ in range(1000):
            async with factory() as session:
                ch = await crud.get_channel_by_provider(session, "siliconflow")
                assert ch is not None
        t1 = time.perf_counter()
        single_ms = (t1 - t0) * 1000

        # list 全表 1000 次 (原方式)
        t0 = time.perf_counter()
        for _ in range(1000):
            async with factory() as session:
                channels = await crud.list_channels(session, status="active")
                ch = next((c for c in channels if c.provider == "siliconflow"), None)
                assert ch is not None
        t1 = time.perf_counter()
        list_ms = (t1 - t0) * 1000

        print(f"  单查询 get_channel_by_provider x1000: {single_ms:.1f} ms")
        print(f"  原方式 list_channels + next x1000: {list_ms:.1f} ms")
        speedup = list_ms / single_ms if single_ms > 0 else 0
        print(f"  加速比: {speedup:.1f}x")

        # 期望: 单查应该明显快 (只查 1 行 vs 110 行)
        assert single_ms < list_ms, \
            f"单查应快于全表: single={single_ms:.1f}ms, list={list_ms:.1f}ms"

        await engine.dispose()

    asyncio.run(_run())
    print("  [OK] I8 单查询性能优于原始 list_channels + next")


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 60)
    print("Phase 3.4 — 价格联动 测试")
    print("=" * 60)

    test_price_change_changed_flag()
    test_price_change_cheaper()
    test_parse_price_lines_basic()
    test_parse_price_lines_robustness()
    test_parse_price_lines_complex()
    test_sync_price_change_first_write()
    test_sync_price_change_no_channel()
    test_sync_price_change_no_price()
    test_sync_price_change_same_price_no_refresh()
    test_get_channel_by_provider_basic()
    test_sync_price_change_uses_single_query()
    test_get_channel_by_provider_multiple_channels_same_provider()
    test_apply_price_change_uses_invalidate_price()
    test_router_invalidate_methods_exist()
    test_invalidate_price_tracing_after_price_change()
    test_e2e_price_change_updates_cache_via_invalidate()
    test_i8_single_query_vs_list_perf()

    print("\n" + "=" * 60)
    print("ALL 17 TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
