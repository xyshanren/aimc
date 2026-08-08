"""V4.4.A 隐式反馈聚合 单元测试

覆盖:
1. percentile() 空列表返 None
2. percentile() 单元素返它
3. percentile() 中位数 (ratio=0.5)
4. percentile() p95 (ratio=0.95)
5. model_family() 提取主族
6. week_bounds() 计算 (给定 reference)
7. WeekStats.avg_feedback_score
8. WeekStats.fail_rate / retry_rate
9. aggregate() upsert 幂等 (同 model+period 第二次写覆盖)
10. feedback_score 公式正确性

跑法: python tests/test_v44_feedback_summary.py
"""

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config + 临时 SQLite (隔离进程级单例)
# ============================================================
def _install_mock_env():
    import os
    import types

    # 用临时文件, 避免 :memory: 多 connection 互不可见
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v44a_")
    db_path = os.path.join(tmp_dir, "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"
    os.environ["AIMC_TEST_DB_PATH"] = db_path

    sys.modules.setdefault("config", types.ModuleType("config"))
    # 用 SimpleNamespace 避免 class body 不能引用 enclosing local 的问题
    sys.modules["config"].settings = types.SimpleNamespace(
        openai_api_key="sk-test",
        openai_base_url="https://mock.local/v1",
        parse_model="gpt-4o-mini",
        parse_confidence_threshold=0.6,
        crawl_enabled=True,
        aimc_master_key="test-key",
        route_refresh_interval=600,
        db_url=db_url,
        db_path=db_path,
    )


_install_mock_env()


# ============================================================
# Test 1: percentile 空列表
# ============================================================
def test_percentile_empty():
    print("\n=== Test 1: percentile 空列表返 None ===")
    from scripts.aggregate_weekly_feedback import percentile

    result = percentile([], 0.5)
    assert result is None, f"空列表应返 None, 实际 {result}"
    result = percentile([], 0.95)
    assert result is None
    print("  [OK] 空列表全部返 None")


# ============================================================
# Test 2: percentile 单元素
# ============================================================
def test_percentile_single():
    print("\n=== Test 2: percentile 单元素返它 ===")
    from scripts.aggregate_weekly_feedback import percentile

    result = percentile([42], 0.5)
    assert result == 42, f"单元素 ratio=0.5 应返 42, 实际 {result}"
    result = percentile([42], 0.95)
    assert result == 42
    result = percentile([42], 0.0)
    assert result == 42
    print("  [OK] 单元素任意 ratio 返自身")


# ============================================================
# Test 3: percentile 中位数
# ============================================================
def test_percentile_median():
    print("\n=== Test 3: percentile 中位数 ratio=0.5 ===")
    from scripts.aggregate_weekly_feedback import percentile

    # 1..100 中位数应是 50 (0-indexed int((99)*0.5) = 49 → values[49] = 50)
    values = list(range(1, 101))
    result = percentile(values, 0.5)
    assert result == 50, f"1..100 中位数应 50, 实际 {result}"
    print(f"  [OK] 1..100 中位数 = {result}")


# ============================================================
# Test 4: percentile p95
# ============================================================
def test_percentile_p95():
    print("\n=== Test 4: percentile p95 ===")
    from scripts.aggregate_weekly_feedback import percentile

    values = list(range(1, 101))
    result = percentile(values, 0.95)
    # int(99 * 0.95) = 94 → values[94] = 95
    assert result == 95, f"1..100 p95 应 95, 实际 {result}"
    print(f"  [OK] p95 = {result}")


# ============================================================
# Test 5: model_family 提取
# ============================================================
def test_model_family():
    print("\n=== Test 5: model_family 提取主族 ===")
    from scripts.aggregate_weekly_feedback import model_family

    assert model_family("deepseek-chat") == "deepseek"
    assert model_family("qwen-max") == "qwen"
    assert model_family("GLM-4-Plus") == "glm"
    assert model_family("unknown") == "unknown"
    assert model_family(None) == "unknown"  # type: ignore[arg-type]
    assert model_family("") == "unknown"
    print("  [OK] model_family 全部正确")


# ============================================================
# Test 6: week_bounds 计算
# ============================================================
def test_week_bounds():
    print("\n=== Test 6: week_bounds 计算 ===")
    from scripts.aggregate_weekly_feedback import week_bounds
    from scripts.generate_weekly_report import week_bounds as report_week_bounds

    # 用 2026-08-05 (周三) 作为 reference
    ref = datetime(2026, 8, 5, 12, 0, 0)
    start, end = week_bounds(ref)
    # reference.weekday()=2 (Wed), 本周一 = 8/5-2 = 8/3
    # 上周一 = 8/3-7 = 7/27
    # 返回 (上周一, 本周一) — 查 [上周一, 本周一) 范围
    assert start == datetime(2026, 7, 27, 0, 0, 0), f"start 应 2026-07-27 (上周一), 实际 {start}"
    assert end == datetime(2026, 8, 3, 0, 0, 0), f"end 应 2026-08-03 (本周一), 实际 {end}"
    print(f"  [OK] reference=2026-08-05 → bounds=({start.date()}, {end.date()})")

    # generate_weekly_report.week_bounds 应该语义一致
    start2, end2 = report_week_bounds(ref)
    assert start2 == start and end2 == end, "两个 week_bounds 实现应一致"
    print("  [OK] 与 generate_weekly_report.week_bounds 一致")


# ============================================================
# Test 7: WeekStats.avg_feedback_score
# ============================================================
def test_week_stats_avg_feedback_score():
    print("\n=== Test 7: WeekStats.avg_feedback_score ===")
    from scripts.generate_weekly_report import WeekStats

    # 构造 mock rows (不依赖 DB, 用 SimpleNamespace)
    from types import SimpleNamespace

    rows = [
        SimpleNamespace(request_count=10, success_count=10, retry_count=1,
                         feedback_score=80.0, cost_total=1.0),
        SimpleNamespace(request_count=10, success_count=8, retry_count=2,
                         feedback_score=60.0, cost_total=1.0),
        SimpleNamespace(request_count=10, success_count=9, retry_count=1,
                         feedback_score=None, cost_total=1.0),  # None 应跳过
    ]
    stats = WeekStats(
        period_start=datetime(2026, 8, 3),
        period_end=datetime(2026, 8, 10),
        rows=rows,
    )
    # (80 + 60) / 2 = 70
    assert stats.avg_feedback_score == 70.0, f"应 70.0, 实际 {stats.avg_feedback_score}"
    print(f"  [OK] avg_feedback_score = {stats.avg_feedback_score}")


# ============================================================
# Test 8: WeekStats.fail_rate / retry_rate
# ============================================================
def test_week_stats_rates():
    print("\n=== Test 8: WeekStats.fail_rate / retry_rate ===")
    from scripts.generate_weekly_report import WeekStats
    from types import SimpleNamespace

    rows = [
        SimpleNamespace(request_count=100, success_count=90, retry_count=5,
                         feedback_score=80.0, cost_total=1.0),
    ]
    stats = WeekStats(
        period_start=datetime(2026, 8, 3),
        period_end=datetime(2026, 8, 10),
        rows=rows,
    )
    # fail = (100-90)/100 = 0.1
    assert abs(stats.fail_rate - 0.1) < 0.001, f"fail_rate 应 0.1, 实际 {stats.fail_rate}"
    # retry = 5/100 = 0.05
    assert abs(stats.retry_rate - 0.05) < 0.001, f"retry_rate 应 0.05, 实际 {stats.retry_rate}"
    print(f"  [OK] fail_rate={stats.fail_rate:.2%} retry_rate={stats.retry_rate:.2%}")


# ============================================================
# Test 9: aggregate() upsert 幂等 + 公式 (用 in-memory SQLite)
# ============================================================
def test_aggregate_upsert_idempotent():
    print("\n=== Test 9: aggregate() upsert 幂等 + 反馈分公式 ===")
    from db.engine import async_session_factory
    from db.init_db import init_db
    from db.models import Channel, UsageLog, ModelFeedbackSummary
    from scripts.aggregate_weekly_feedback import aggregate, week_bounds
    from sqlalchemy import select, delete

    async def _run():
        await init_db()

        async with async_session_factory() as session:
            # 1) 干净环境
            await session.execute(delete(ModelFeedbackSummary))
            await session.execute(delete(UsageLog))
            await session.execute(delete(Channel))
            await session.commit()

            # 2) 建 channel
            ch = Channel(
                name="deepseek_official",
                provider="deepseek",
                base_url="https://api.deepseek.com",
                status="active",
            )
            session.add(ch)
            await session.commit()

        # 3) 写 5 条 UsageLog (1 失败)
        start, end = week_bounds()
        async with async_session_factory() as session:
            for i in range(5):
                log = UsageLog(
                    channel_id=ch.id,
                    model="deepseek-chat",
                    input_tokens=100, output_tokens=50,
                    cost_estimate=0.001,
                    status_code=500 if i == 0 else 200,
                    latency_ms=1000,
                    account_id=1,
                    created_at=start + timedelta(hours=i),
                )
                session.add(log)
            await session.commit()

        # 4) 跑 aggregate (1 次)
        n1 = await aggregate()
        assert n1 == 1, f"应聚合 1 个 model, 实际 {n1}"

        # 5) 验证 feedback_score: fail_rate=0.2, retry_rate=0.0, p95=1000
        #    score = max(0, min(100, 100 - 30*0 - 50*0.2 - 0.01*1000)) = max(0, min(100, 100 - 10 - 10)) = 80
        async with async_session_factory() as session:
            row = (await session.execute(
                select(ModelFeedbackSummary)
            )).scalar_one()
            assert row.model_id == "deepseek-chat"
            assert abs(row.feedback_score - 80.0) < 0.01, (
                f"score 应 80.0, 实际 {row.feedback_score}"
            )
            assert row.request_count == 5
            assert row.success_count == 4
            assert abs(row.fail_rate - 0.2) < 0.001
            print(f"  [OK] 第 1 次聚合: score={row.feedback_score} fail_rate={row.fail_rate:.2%}")

        # 6) 跑 aggregate 再一次, 验证幂等 (覆盖而不是新增)
        n2 = await aggregate()
        assert n2 == 1, f"第二次应仍聚合 1 个 model, 实际 {n2}"

        async with async_session_factory() as session:
            count = (await session.execute(
                select(ModelFeedbackSummary)
            )).scalars().all()
            assert len(count) == 1, f"幂等 upsert 应只剩 1 行, 实际 {len(count)}"
            print(f"  [OK] 第 2 次聚合幂等: 仍 {len(count)} 行")

    asyncio.run(_run())


# ============================================================
# 入口
# ============================================================
def main():
    tests = [
        test_percentile_empty,
        test_percentile_single,
        test_percentile_median,
        test_percentile_p95,
        test_model_family,
        test_week_bounds,
        test_week_stats_avg_feedback_score,
        test_week_stats_rates,
        test_aggregate_upsert_idempotent,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            print(f"  [FAIL] {t.__name__}: {e}")
            failed += 1
    print(f"\n=== V4.4.A 反馈聚合测试: {len(tests) - failed}/{len(tests)} passed ===")
    return 1 if failed else 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())