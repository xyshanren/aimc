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
                       effective_from=None, effective_to=None):
        for p in _state["prices"]:
            if (p.channel_id == channel_id and p.model == model
                    and p.price_type == price_type and p.time_range == time_range):
                p.input_price = input_price
                p.output_price = output_price
                p.source = source
                return p, False
        p = _MockPrice(
            channel_id=channel_id, model=model,
            input_price=input_price, output_price=output_price,
            price_type=price_type, time_range=time_range, source=source,
            effective_from=effective_from, effective_to=effective_to,
        )
        _state["prices"].append(p)
        return p, True

    crud.list_channels = _list_channels
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
    from intelligence.pricing_sync import sync_price_change
    import db.crud as crud

    async def _empty(session, status="active"):
        return []

    crud.list_channels = _empty

    async def _run():
        return await sync_price_change("nonexistent", "m", 1.0, 1.0)

    change = asyncio.run(_run())
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

    print("\n" + "=" * 60)
    print("ALL 9 TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
