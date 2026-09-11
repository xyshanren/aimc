"""V4.5 周报渲染 单元测试 (scripts/generate_weekly_report.py)

覆盖:
1. format_pct 精度
2. WeekStats request/success/retry/cost/fail/retry/avg 全部 property
3. render_overview 包含关键字段 + 与上周对比
4. render_top_models 排序 + Top 5 截断
5. render_trends 箭头方向 (↑ / ↓ / — / 🆕)
6. render_alerts >5% 阈值 + 无异常 fallback
7. render_actions 主路由/观察期/异常文案

跑法: python tests/test_v45_reports.py
"""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

# Windows GBK console can't encode emoji (e.g. 🆕 ✅) — force UTF-8 stdout
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config (周报渲染本身不读 settings, 但 import 时会引到)
# ============================================================
def _install_mock_env():
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


_install_mock_env()


def _make_row(model: str, *, score: float | None, req: int = 100, success: int = 100,
              retry: int = 0, cost: float = 1.0, fail_rate: float = 0.0,
              retry_rate: float = 0.0, p95_latency_ms: int | None = None):
    """构造 ModelFeedbackSummary 的 duck type (用 SimpleNamespace)

    req 默认 100: MIN_SAMPLES=20 门槛之下默认样本充足, 门槛行为用 req<20 显式构造.
    """
    return SimpleNamespace(
        model_id=model, feedback_score=score,
        request_count=req, success_count=success, retry_count=retry,
        cost_total=cost, fail_rate=fail_rate, retry_rate=retry_rate,
        p95_latency_ms=p95_latency_ms,
    )


# ============================================================
# Test 1: format_pct 精度
# ============================================================
def test_format_pct():
    print("\n=== Test 1: format_pct 精度 ===")
    from scripts.generate_weekly_report import format_pct

    assert format_pct(0.5) == "50.0%"
    assert format_pct(0.123) == "12.3%"
    assert format_pct(0.0) == "0.0%"
    assert format_pct(1.0) == "100.0%"
    print("  [OK] format_pct 全部正确")


# ============================================================
# Test 2: WeekStats 全部 property
# ============================================================
def test_week_stats_properties():
    print("\n=== Test 2: WeekStats 全部 property ===")
    from scripts.generate_weekly_report import WeekStats

    rows = [
        _make_row("deepseek-chat", score=80.0, req=10, success=10, retry=1, cost=1.5),
        _make_row("qwen-max", score=60.0, req=10, success=8, retry=2, cost=2.0),
    ]
    stats = WeekStats(
        period_start=datetime(2026, 8, 3),
        period_end=datetime(2026, 8, 10),
        rows=rows,
    )

    assert stats.request_count == 20, f"request_count 应 20, 实际 {stats.request_count}"
    assert stats.success_count == 18, f"success_count 应 18, 实际 {stats.success_count}"
    assert stats.retry_count == 3, f"retry_count 应 3, 实际 {stats.retry_count}"
    assert abs(stats.cost_total - 3.5) < 0.001, f"cost_total 应 3.5, 实际 {stats.cost_total}"
    assert abs(stats.fail_rate - 0.1) < 0.001, f"fail_rate 应 0.1, 实际 {stats.fail_rate}"
    assert abs(stats.retry_rate - 0.15) < 0.001, f"retry_rate 应 0.15, 实际 {stats.retry_rate}"
    assert stats.avg_feedback_score == 70.0, f"avg 应 70.0, 实际 {stats.avg_feedback_score}"
    print(f"  [OK] req={stats.request_count} success={stats.success_count} "
          f"retry={stats.retry_count} cost={stats.cost_total:.2f} "
          f"fail={stats.fail_rate:.1%} retry={stats.retry_rate:.1%} avg={stats.avg_feedback_score:.1f}")


# ============================================================
# Test 3: render_overview 包含字段
# ============================================================
def test_render_overview():
    print("\n=== Test 3: render_overview 包含字段 ===")
    from scripts.generate_weekly_report import WeekStats, render_overview

    this_week = WeekStats(
        period_start=datetime(2026, 8, 10), period_end=datetime(2026, 8, 17),
        rows=[_make_row("m1", score=80.0, req=100, success=95, retry=5, cost=2.5, fail_rate=0.05)],
    )
    last_week = WeekStats(
        period_start=datetime(2026, 8, 3), period_end=datetime(2026, 8, 10),
        rows=[_make_row("m1", score=70.0, req=80, success=78, retry=2, cost=2.0, fail_rate=0.025)],
    )
    lines = render_overview(this_week, last_week)
    text = "\n".join(lines)
    assert "本周总览" in text
    assert "总请求" in text
    assert "100" in text  # this_week req
    assert "80" in text   # last_week req
    assert "2.50" in text or "2.5" in text  # this_week cost
    assert "失败率" in text
    assert "平均反馈分" in text
    assert "80.0" in text  # this_week avg score
    assert "70.0" in text  # last_week avg score
    print(f"  [OK] render_overview 含 7 个关键字段 + 周对比")


# ============================================================
# Test 4: render_top_models 排序 + Top 5 截断
# ============================================================
def test_render_top_models():
    print("\n=== Test 4: render_top_models 排序 + Top 5 截断 + 样本门槛 ===")
    from scripts.generate_weekly_report import WeekStats, render_top_models

    # 7 行 (req 默认 100 ≥ MIN_SAMPLES), 应只取 Top 5
    rows = [
        _make_row(f"m{i}", score=float(100 - i*10))
        for i in range(7)
    ]
    # 分数: 100, 90, 80, 70, 60, 50, 40
    stats = WeekStats(
        period_start=datetime(2026, 8, 10), period_end=datetime(2026, 8, 17),
        rows=rows,
    )
    lines = render_top_models(stats)
    text = "\n".join(lines)

    # Top 5 应包含 m0..m4, 不含 m5/m6
    for i in range(5):
        assert f"m{i}" in text, f"应包含 m{i} (Top 5)"
    assert "m5" not in text, "m5 (第6名) 不应在 Top 5"
    assert "m6" not in text, "m6 (第7名) 不应在 Top 5"

    # 表格顺序: m0=100 排第一
    m0_pos = text.find("`m0`")
    m4_pos = text.find("`m4`")
    assert m0_pos < m4_pos, f"m0 (100) 应排在 m4 (60) 之前"
    # p95 延迟列存在
    assert "p95 延迟" in text
    print(f"  [OK] Top 5 排序正确 (100 > 90 > 80 > 70 > 60), p95 列在表头")

    # 子测试: req < MIN_SAMPLES 的模型进 "样本不足" 行, 不进排名
    rows_low = [
        _make_row("big", score=99.0, req=200),
        _make_row("tiny_a", score=94.6, req=2),
        _make_row("tiny_b", score=88.0, req=5),
    ]
    stats_low = WeekStats(
        period_start=datetime(2026, 8, 10), period_end=datetime(2026, 8, 17),
        rows=rows_low,
    )
    lines_low = render_top_models(stats_low)
    text_low = "\n".join(lines_low)
    assert "`big`" in text_low and "94.6" not in text_low.split("样本不足")[0].split("`tiny_a`")[0]
    assert "样本不足" in text_low
    assert "`tiny_a`(2次,94.6分)" in text_low
    assert "`tiny_b`(5次,88.0分)" in text_low
    # 样本不足行在表格尾部, 不占排名
    assert text_low.find("样本不足") > text_low.find("`big`")
    # p95 格式: ≥1s 显示秒, <1s 显示 ms
    rows_lat = [
        _make_row("fast", score=90.0, req=100, p95_latency_ms=800),
        _make_row("slow", score=90.0, req=100, p95_latency_ms=104_000),
        _make_row("nolat", score=90.0, req=100, p95_latency_ms=None),
    ]
    stats_lat = WeekStats(
        period_start=datetime(2026, 8, 10), period_end=datetime(2026, 8, 17),
        rows=rows_lat,
    )
    text_lat = "\n".join(render_top_models(stats_lat))
    assert "800ms" in text_lat
    assert "104.0s" in text_lat
    assert "—" in text_lat
    print(f"  [OK] 样本不足行 + p95 格式 (800ms / 104.0s / —)")


# ============================================================
# Test 5: render_trends 箭头
# ============================================================
def test_render_trends_arrows():
    print("\n=== Test 5: render_trends 箭头方向 ===")
    from scripts.generate_weekly_report import WeekStats, render_trends

    this_rows = [
        _make_row("up", score=90.0),
        _make_row("down", score=50.0),
        _make_row("flat", score=70.0),
        _make_row("new_model", score=85.0),
    ]
    last_rows = [
        _make_row("up", score=70.0),       # +20 ↑↑
        _make_row("down", score=80.0),     # -30 ↓↓
        _make_row("flat", score=70.4),     # -0.4 — (|delta|<0.5)
        # new_model 上周没有
    ]
    this_week = WeekStats(
        period_start=datetime(2026, 8, 10), period_end=datetime(2026, 8, 17),
        rows=this_rows,
    )
    last_week = WeekStats(
        period_start=datetime(2026, 8, 3), period_end=datetime(2026, 8, 10),
        rows=last_rows,
    )
    lines = render_trends(this_week, last_week)
    text = "\n".join(lines)

    # 验证箭头
    up_line = next(l for l in lines if "`up`" in l)
    assert "↑" in up_line, f"up 应有 ↑, 实际 {up_line}"
    assert "+20" in up_line, f"up 应显示 +20, 实际 {up_line}"

    down_line = next(l for l in lines if "`down`" in l)
    assert "↓" in down_line, f"down 应有 ↓, 实际 {down_line}"
    assert "-30" in down_line or "30" in down_line, f"down 应显示 delta"

    flat_line = next(l for l in lines if "`flat`" in l)
    assert "—" in flat_line or "-" in flat_line, f"flat 应用 —, 实际 {flat_line}"

    new_line = next(l for l in lines if "`new_model`" in l)
    assert "🆕" in new_line, f"new_model 应有 🆕, 实际 {new_line}"
    print(f"  [OK] 4 种箭头: ↑ +20 / ↓ -30 / — / 🆕 全部正确")


# ============================================================
# Test 6: render_alerts 阈值 + fallback
# ============================================================
def test_render_alerts():
    print("\n=== Test 6: render_alerts >5% 阈值 + 无异常 fallback ===")
    from scripts.generate_weekly_report import WeekStats, render_alerts

    # 子测试 a: 有异常
    rows_with_alerts = [
        _make_row("m_bad_fail", score=80.0, fail_rate=0.10),       # > 5% 触发
        _make_row("m_bad_retry", score=80.0, retry_rate=0.08),     # > 5% 触发
        _make_row("m_ok", score=80.0, fail_rate=0.02, retry_rate=0.03),  # 都 <5% 不触发
    ]
    stats_a = WeekStats(
        period_start=datetime(2026, 8, 10), period_end=datetime(2026, 8, 17),
        rows=rows_with_alerts,
    )
    lines_a = render_alerts(stats_a)
    text_a = "\n".join(lines_a)
    assert "m_bad_fail" in text_a
    assert "10.0%" in text_a
    assert "m_bad_retry" in text_a
    assert "8.0%" in text_a
    assert "m_ok" not in text_a
    print(f"  [OK] 异常模型被高亮 + 百分比")

    # 子测试 b: 无异常
    rows_clean = [
        _make_row("m1", score=80.0, fail_rate=0.01),
        _make_row("m2", score=70.0, retry_rate=0.02),
    ]
    stats_b = WeekStats(
        period_start=datetime(2026, 8, 10), period_end=datetime(2026, 8, 17),
        rows=rows_clean,
    )
    lines_b = render_alerts(stats_b)
    text_b = "\n".join(lines_b)
    assert "无模型级异常" in text_b, f"无异常应显示 fallback, 实际 {text_b}"
    print(f"  [OK] 无异常 → '本周无模型级异常 ✅'")


# ============================================================
# Test 7: render_actions
# ============================================================
def test_render_actions():
    print("\n=== Test 7: render_actions 主路由/观察期/异常文案 ===")
    from scripts.generate_weekly_report import WeekStats, render_actions

    rows = [
        _make_row("m_best", score=95.0, req=100, success=100, cost=10.0),
        _make_row("m_worst", score=60.0, req=100, success=85, cost=5.0, fail_rate=0.15),
        _make_row("m_mid", score=80.0, req=100, success=99, cost=8.0, fail_rate=0.01),
    ]
    stats = WeekStats(
        period_start=datetime(2026, 8, 10), period_end=datetime(2026, 8, 17),
        rows=rows,
    )
    lines = render_actions(stats)
    text = "\n".join(lines)

    # 主路由候选: m_best
    assert "🥇" in text
    assert "m_best" in text
    # 观察期: m_worst (60 < 70)
    assert "👀" in text
    assert "m_worst" in text
    # 总体失败率 > 5% → 异常建议
    assert "总体失败率" in text
    # 成本建议
    assert "成本" in text
    # 持续周环比
    assert "下周" in text or "🔄" in text
    print(f"  [OK] 4 类建议全部正确")


# ============================================================
# 入口
# ============================================================
def main():
    tests = [
        test_format_pct,
        test_week_stats_properties,
        test_render_overview,
        test_render_top_models,
        test_render_trends_arrows,
        test_render_alerts,
        test_render_actions,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            print(f"  [FAIL] {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n=== V4.5 周报渲染测试: {len(tests) - failed}/{len(tests)} passed ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())