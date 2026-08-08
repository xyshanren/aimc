"""V4.6.3 多源 benchmark 单元测试

覆盖:
1. BenchmarkEntry dataclass — 字段 / to_dict
2. BenchmarkSource 抽象类 — reliability 默认 / fetch() 抛 NotImplementedError
3. Concrete source — OpenCompass / LiveBench / AA / LMSYS / SuperCLUE 都返回 BenchmarkEntry list
4. AggregateScore — 字段 / to_dict
5. benchmark_aggregator.aggregate():
   - 单 source → composite = score
   - 多 source → weighted avg (按 reliability)
   - recency 衰减 (新数据权重高)
   - 空 entries → 空 dict
   - 全部过期 → composite=0
6. to_model_cards_rows() — 输出格式
7. refresh_benchmark 端到端 mock 测试 (用 mock source 注入)

跑法: python tests/test_v46_3_benchmark.py
"""

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config + 临时 SQLite
# ============================================================
def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v463_benchmark_")
    db_path = os.path.join(tmp_dir, "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"

    import types
    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = SimpleNamespace(
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
        request_timeout=30,
    )


_install_mock_env()


# ============================================================
# Test 1: BenchmarkEntry 字段 + to_dict
# ============================================================
def test_benchmark_entry():
    print("\n=== Test 1: BenchmarkEntry 字段 + to_dict ===")
    from intelligence.sources.benchmark import BenchmarkEntry

    e = BenchmarkEntry(
        model="deepseek-v3", provider="deepseek", score=75.2,
        source="opencompass", url="https://opencompass.org.cn/rank",
        category="general",
    )
    assert e.model == "deepseek-v3"
    assert e.score == 75.2
    assert e.source == "opencompass"
    assert e.category == "general"
    assert e.fetched_at is not None

    d = e.to_dict()
    assert d["model"] == "deepseek-v3"
    assert d["score"] == 75.2
    assert "fetched_at" in d
    print(f"  [OK] 字段 / to_dict / fetched_at 自动填")


# ============================================================
# Test 2: BenchmarkSource 抽象类 + reliability
# ============================================================
def test_benchmark_source_abstract():
    print("\n=== Test 2: BenchmarkSource 抽象类 ===")
    from intelligence.sources.benchmark import BenchmarkSource

    # 直接实例化抽象类应能拿到字段
    base = BenchmarkSource(provider="test", url="https://x")
    assert base.reliability == 0.8, f"默认 reliability 应 0.8, 实际 {base.reliability}"
    assert base.enabled is True

    # fetch() 应抛 NotImplementedError
    async def _check():
        try:
            await base.fetch()
            return False, "应抛 NotImplementedError"
        except NotImplementedError:
            return True, None

    ok, msg = asyncio.run(_check())
    assert ok, msg
    print(f"  [OK] 抽象类可实例化 / fetch() 抛 NotImplementedError")


# ============================================================
# Test 3: 各 concrete source 实例化 + reliability
# ============================================================
def test_concrete_sources():
    print("\n=== Test 3: 各 concrete source 实例化 ===")
    from intelligence.sources.opencompass import OpenCompassBenchmarkSource
    from intelligence.sources.livebench import LiveBenchBenchmarkSource
    from intelligence.sources.artificial_analysis import ArtificialAnalysisBenchmarkSource
    from intelligence.sources.benchmark import LMSYSArenaBenchmarkSource, SuperCLUEBenchmarkSource

    sources = [
        (LMSYSArenaBenchmarkSource(),        0.95),
        (OpenCompassBenchmarkSource(),       0.90),
        (SuperCLUEBenchmarkSource(),         0.80),
        (LiveBenchBenchmarkSource(),         0.85),
        (ArtificialAnalysisBenchmarkSource(), 0.85),
    ]
    for src, expected_rel in sources:
        assert src.reliability == expected_rel, (
            f"{src.provider}: reliability 应 {expected_rel}, 实际 {src.reliability}"
        )
    print(f"  [OK] LMSYS=0.95 OpenCompass=0.90 SuperCLUE=0.80 LiveBench=0.85 AA=0.85")


# ============================================================
# Test 4: source.fetch() 返回 BenchmarkEntry list (mock fallback)
# ============================================================
def test_source_fetch_returns_entries():
    print("\n=== Test 4: 各 source.fetch() 返 BenchmarkEntry list ===")
    from intelligence.sources.opencompass import OpenCompassBenchmarkSource
    from intelligence.sources.livebench import LiveBenchBenchmarkSource
    from intelligence.sources.artificial_analysis import ArtificialAnalysisBenchmarkSource
    from intelligence.sources.benchmark import LMSYSArenaBenchmarkSource, SuperCLUEBenchmarkSource
    from intelligence.sources.benchmark import BenchmarkEntry

    async def _run():
        results = {}
        for src in [
            LMSYSArenaBenchmarkSource(),
            OpenCompassBenchmarkSource(),
            SuperCLUEBenchmarkSource(),
            LiveBenchBenchmarkSource(),
            ArtificialAnalysisBenchmarkSource(),
        ]:
            entries = await src.fetch()
            results[src.provider] = (len(entries), entries)
            for e in entries:
                assert isinstance(e, BenchmarkEntry), f"{src.provider} 应返 BenchmarkEntry"
                assert 0 <= e.score <= 100, f"{src.provider}: score {e.score} 越界"
                assert e.source == src.provider
                assert e.url, "source 应填 url"
        return results

    results = asyncio.run(_run())
    # LMSYS / SuperCLUE 是 stub, 返空; 其他返 mock
    assert results["lmsys"][0] == 0
    assert results["superclue"][0] == 0
    assert results["opencompass"][0] > 0, "OpenCompass 应返 mock"
    assert results["livebench"][0] > 0, "LiveBench 应返 mock"
    assert results["artificial_analysis"][0] > 0, "AA 应返 mock"
    print(f"  [OK] LMSYS={results['lmsys'][0]} / OpenCompass={results['opencompass'][0]} / "
          f"SuperCLUE={results['superclue'][0]} / LiveBench={results['livebench'][0]} / "
          f"AA={results['artificial_analysis'][0]}")


# ============================================================
# Test 5: aggregate() 单 source → composite = score
# ============================================================
def test_aggregate_single_source():
    print("\n=== Test 5: aggregate 单 source ===")
    from intelligence.sources.benchmark import BenchmarkEntry
    from intelligence.benchmark_aggregator import aggregate

    entries = [
        BenchmarkEntry(model="m1", provider="p1", score=80.0, source="lmsys"),
        BenchmarkEntry(model="m2", provider="p2", score=70.0, source="lmsys"),
    ]
    agg = aggregate(entries)
    assert agg["m1"].composite_score == 80.0
    assert agg["m2"].composite_score == 70.0
    assert agg["m1"].sample_size == 1
    print(f"  [OK] m1=80, m2=70 (单 source 无加权变化)")


# ============================================================
# Test 6: aggregate() 多 source 加权
# ============================================================
def test_aggregate_multi_source_weighted():
    print("\n=== Test 6: aggregate 多 source 加权 ===")
    from intelligence.sources.benchmark import BenchmarkEntry
    from intelligence.benchmark_aggregator import aggregate

    # m1: opencompass (rel=0.9, score=70) + lmsys (rel=0.95, score=80)
    # weight_total = 0.9 + 0.95 = 1.85
    # composite = (70*0.9 + 80*0.95) / 1.85 = (63 + 76) / 1.85 = 139/1.85 ≈ 75.135
    entries = [
        BenchmarkEntry(model="m1", provider="p", score=70.0, source="opencompass"),
        BenchmarkEntry(model="m1", provider="p", score=80.0, source="lmsys"),
    ]
    agg = aggregate(entries)
    expected = (70.0 * 0.9 + 80.0 * 0.95) / (0.9 + 0.95)
    assert abs(agg["m1"].composite_score - expected) < 0.01, (
        f"weighted 应 {expected:.3f}, 实际 {agg['m1'].composite_score:.3f}"
    )
    assert agg["m1"].sample_size == 2
    print(f"  [OK] m1 composite={agg['m1'].composite_score:.3f} (期望 {expected:.3f})")


# ============================================================
# Test 7: aggregate() recency 衰减
# ============================================================
def test_aggregate_recency_decay():
    print("\n=== Test 7: aggregate recency 衰减 ===")
    from intelligence.sources.benchmark import BenchmarkEntry
    from intelligence.benchmark_aggregator import aggregate

    now = datetime.now(timezone.utc)
    # m1: opencompass (新, recency=1) + lmsys (1 年前, recency≈0)
    # 新数据占主导
    entries = [
        BenchmarkEntry(model="m1", provider="p", score=70.0, source="opencompass",
                       fetched_at=now),
        BenchmarkEntry(model="m1", provider="p", score=99.0, source="lmsys",
                       fetched_at=now - timedelta(days=400)),  # 已衰减
    ]
    agg = aggregate(entries)
    # opencompass weight = 0.9 * 1.0 = 0.9
    # lmsys weight = 0.95 * 0.0 = 0  (400 天 > 2*180=360, 完全衰减)
    # composite = (70*0.9) / 0.9 = 70.0
    assert abs(agg["m1"].composite_score - 70.0) < 0.01, (
        f"老数据应被衰减, 实际 {agg['m1'].composite_score}"
    )
    print(f"  [OK] 老 lmsys (400 天) 被 recency 衰减到 0, opencompass 主导: "
          f"composite={agg['m1'].composite_score:.2f}")


# ============================================================
# Test 8: aggregate() 空 entries
# ============================================================
def test_aggregate_empty():
    print("\n=== Test 8: aggregate 空 ===")
    from intelligence.benchmark_aggregator import aggregate

    assert aggregate([]) == {}
    print(f"  [OK] 空 entries → 空 dict")


# ============================================================
# Test 9: aggregate() 全部过期 → composite=0
# ============================================================
def test_aggregate_all_expired():
    print("\n=== Test 9: 全部过期 → composite=0 ===")
    from intelligence.sources.benchmark import BenchmarkEntry
    from intelligence.benchmark_aggregator import aggregate

    old = datetime.now(timezone.utc) - timedelta(days=1000)  # 远超 360 天
    entries = [
        BenchmarkEntry(model="m1", provider="p", score=80.0, source="lmsys", fetched_at=old),
        BenchmarkEntry(model="m1", provider="p", score=70.0, source="opencompass", fetched_at=old),
    ]
    agg = aggregate(entries)
    assert agg["m1"].composite_score == 0.0, (
        f"全部过期应 composite=0, 实际 {agg['m1'].composite_score}"
    )
    print(f"  [OK] 1000 天前数据 → composite=0.0")


# ============================================================
# Test 10: AggregateScore.to_dict()
# ============================================================
def test_aggregate_to_dict():
    print("\n=== Test 10: AggregateScore.to_dict() ===")
    from intelligence.benchmark_aggregator import AggregateScore

    agg = AggregateScore(
        model="m1", composite_score=75.5,
        contributing_sources=[
            {"source": "lmsys", "score": 80, "weight": 0.95,
             "contribution_pct": 50.0, "reliability": 0.95, "recency": 1.0},
        ],
        sample_size=1,
    )
    d = agg.to_dict()
    assert d["model"] == "m1"
    assert d["composite_score"] == 75.5
    assert d["sample_size"] == 1
    assert d["sources"][0]["source"] == "lmsys"
    print(f"  [OK] to_dict 输出包含 model/composite_score/sample_size/sources")


# ============================================================
# Test 11: to_model_cards_rows()
# ============================================================
def test_to_model_cards_rows():
    print("\n=== Test 11: to_model_cards_rows() ===")
    from intelligence.benchmark_aggregator import AggregateScore, to_model_cards_rows

    aggregates = {
        "m1": AggregateScore(model="m1", composite_score=75.5, sample_size=2,
                             contributing_sources=[
                                 {"source": "opencompass"},
                                 {"source": "lmsys"},
                             ]),
        "m2": AggregateScore(model="m2", composite_score=60.0, sample_size=1,
                             contributing_sources=[{"source": "livebench"}]),
    }
    rows = to_model_cards_rows(aggregates)
    assert len(rows) == 2
    by_model = {r["model"]: r for r in rows}
    assert by_model["m1"]["score"] == 75.5
    assert by_model["m1"]["benchmark"] == "aggregated"
    assert "opencompass" in by_model["m1"]["notes"]
    print(f"  [OK] 2 行 model_cards 格式输出")


# ============================================================
# Test 12: end-to-end — OpenCompass + LiveBench mock 跑 refresh
# ============================================================
def test_refresh_end_to_end():
    print("\n=== Test 12: refresh_benchmark 端到端 (mock) ===")
    async def _run():
        from intelligence.sources.benchmark import BenchmarkEntry
        from intelligence.benchmark_aggregator import aggregate
        from db.init_db import init_db
        from db.models import ModelCard
        from db.engine import async_session_factory
        from sqlalchemy import delete, select

        await init_db()
        async with async_session_factory() as session:
            await session.execute(delete(ModelCard))
            await session.commit()

            # 模拟 2 个 source 各返 3 条
            now = datetime.now(timezone.utc)
            entries = [
                # opencompass
                BenchmarkEntry(model="deepseek-v3", provider="deepseek", score=75.0, source="opencompass", fetched_at=now),
                BenchmarkEntry(model="qwen3-max",   provider="qwen",     score=76.0, source="opencompass", fetched_at=now),
                BenchmarkEntry(model="glm-4.5",     provider="glm",      score=73.0, source="opencompass", fetched_at=now),
                # lmsys
                BenchmarkEntry(model="deepseek-v3", provider="deepseek", score=78.0, source="lmsys",       fetched_at=now),
                BenchmarkEntry(model="qwen3-max",   provider="qwen",     score=74.0, source="lmsys",       fetched_at=now),
                BenchmarkEntry(model="glm-4.5",     provider="glm",      score=70.0, source="lmsys",       fetched_at=now),
            ]
            aggregates = aggregate(entries)
            assert len(aggregates) == 3
            # deepseek-v3: weighted avg of (75*0.9 + 78*0.95)/(0.9+0.95)
            expected_ds = (75.0 * 0.9 + 78.0 * 0.95) / (0.9 + 0.95)
            assert abs(aggregates["deepseek-v3"].composite_score - expected_ds) < 0.01

            # 写入 DB (用 refresh_benchmark 的同款 upsert)
            from scripts.refresh_benchmark import write_to_model_cards
            result = await write_to_model_cards(aggregates, session)
            await session.commit()
            assert result["created"] == 3, f"首次应建 3 条, 实际 {result}"
            print(f"  第 1 次写: created={result['created']}")

            # 第二次写 (相同 source) → 应全部 update, 0 create
            result2 = await write_to_model_cards(aggregates, session)
            await session.commit()
            assert result2["updated"] == 3, f"重跑应 update 3 条, 实际 {result2}"
            assert result2["created"] == 0
            print(f"  第 2 次写 (重跑): updated={result2['updated']}")

            # 验证 DB 里只有 3 条 (没累积)
            count = (await session.execute(select(ModelCard))).scalars().all()
            assert len(count) == 3, f"DB 应只有 3 条, 实际 {len(count)}"
            print(f"  DB 验证: {len(count)} 条 model_cards (无重复)")

    asyncio.run(_run())
    print(f"  [OK] 端到端: 抓取 → 聚合 → 写入 → 重跑幂等")


# ============================================================
# Test 13: get_source_reliability 默认值
# ============================================================
def test_source_reliability_lookup():
    print("\n=== Test 13: get_source_reliability 默认值 ===")
    from intelligence.benchmark_aggregator import get_source_reliability

    assert get_source_reliability("lmsys") == 0.95
    assert get_source_reliability("opencompass") == 0.90
    assert get_source_reliability("superclue") == 0.80
    # 未知 source 应返保守值
    assert get_source_reliability("unknown_xxx") == 0.70, (
        f"未知 source 应 0.70, 实际 {get_source_reliability('unknown_xxx')}"
    )
    print(f"  [OK] lmsys=0.95 opencompass=0.90 unknown=0.70")


def main():
    tests = [
        test_benchmark_entry,
        test_benchmark_source_abstract,
        test_concrete_sources,
        test_source_fetch_returns_entries,
        test_aggregate_single_source,
        test_aggregate_multi_source_weighted,
        test_aggregate_recency_decay,
        test_aggregate_empty,
        test_aggregate_all_expired,
        test_aggregate_to_dict,
        test_to_model_cards_rows,
        test_refresh_end_to_end,
        test_source_reliability_lookup,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"\n[FAIL] {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n{'=' * 60}")
    print(f"V4.6.3 多源 benchmark 单元测试: {passed} passed, {failed} failed")
    print(f"{'=' * 60}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()