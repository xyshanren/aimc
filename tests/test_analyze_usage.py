"""AIMC 稳定期使用分析 (analyze_usage.py) 单元测试

跑法: $env:PYTHONIOENCODING='utf-8'; python tests/test_analyze_usage.py

覆盖:
1. 基础统计: 空 / 单请求 / 多请求
2. Shannon entropy (多账号分散度)
3. status 分布归类 (2xx/4xx/5xx)
4. 自动 failover 命中率
5. token 画像 (median/p95/p99/max)
6. 时段分布 (24 hour bucket)
7. V4.7+ 决策建议触发: 4xx 高 → drop_params / 5xx 高 → Cooldown / 多账号不均 / 长上下文 / 流量集中 / 时段均匀
8. JSON 输出格式
"""

import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config + 临时 SQLite
# ============================================================
def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_analyze_usage_")
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
        log_level="warning",
        request_timeout=30,
    )


_install_mock_env()


def _make_log(
    created_at=None,
    channel_id=1,
    account_id=1,
    model="deepseek-chat",
    group_id=None,
    input_tokens=100,
    output_tokens=50,
    cost_estimate=0.001,
    status_code=200,
    latency_ms=500,
):
    """构造 UsageLog 字典 (避开 ORM)."""
    return SimpleNamespace(
        id=1,
        channel_id=channel_id,
        account_id=account_id,
        model=model,
        group_id=group_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_estimate=cost_estimate,
        status_code=status_code,
        latency_ms=latency_ms,
        created_at=created_at or datetime.now(),
    )


# ============================================================
# Test 1: 空数据
# ============================================================
def test_analyze_empty():
    print("\n=== Test 1: 空数据 ===")
    from scripts.analyze_usage import _analyze, UsageAnalysis
    now = datetime.now()
    a = _analyze([], now - timedelta(days=30), now)
    assert a.request_count == 0
    assert a.success_count == 0
    assert a.error_rate_overall == 0.0
    assert a.status_distribution == {}
    assert a.channel_account_spread == []
    assert a.model_concentration == []
    # 空数据: token_stats 是有 None 字段的占位 dict (运行后会填充)
    assert a.token_stats.get("input_tokens", {}).get("median") is None
    assert a.token_stats.get("input_tokens", {}).get("p95") is None
    assert a.token_stats.get("latency_ms", {}).get("median") is None
    assert a.recommendations, "空数据也要给一个推荐"
    # 样本不足场景推荐
    assert "样本不足" in a.recommendations[0] or "累积" in a.recommendations[0]
    print(f"  [OK] 空数据安全降级 + 推荐: {a.recommendations[0][:50]}...")


# ============================================================
# Test 2: Shannon entropy - 1 个 account
# ============================================================
def test_entropy_single_account():
    print("\n=== Test 2: Shannon entropy (单 account) ===")
    from scripts.analyze_usage import _shannon_entropy
    # 单个 bucket → 熵 0
    assert _shannon_entropy([100]) == 0.0
    # 多个但只有一个非零 → 熵 0
    assert _shannon_entropy([100, 0]) == 0.0
    print(f"  [OK] entropy([100]) = 0, entropy([100,0]) = 0")


# ============================================================
# Test 3: Shannon entropy - 均匀分布
# ============================================================
def test_entropy_uniform():
    print("\n=== Test 3: Shannon entropy (均匀 2 个) ===")
    from scripts.analyze_usage import _shannon_entropy
    # 50/50 分布, log2(2) = 1.0
    e = _shannon_entropy([50, 50])
    assert 0.95 < e <= 1.0, f"50/50 分布 entropy 应接近 1.0, 实际 {e}"
    print(f"  [OK] entropy([50,50]) = {e} (期望 ~1.0)")


# ============================================================
# Test 4: Shannon entropy - 倾斜分布
# ============================================================
def test_entropy_skewed():
    print("\n=== Test 4: Shannon entropy (倾斜 95/5) ===")
    from scripts.analyze_usage import _shannon_entropy
    # 95/5 分布 → entropy 偏低
    e = _shannon_entropy([95, 5])
    assert 0.0 < e < 0.5, f"95/5 分布 entropy 应低, 实际 {e}"
    print(f"  [OK] entropy([95,5]) = {e} (期望 < 0.5)")


# ============================================================
# Test 5: status 分布归类
# ============================================================
def test_status_distribution():
    print("\n=== Test 5: status 分布归类 ===")
    from scripts.analyze_usage import _status_category, _analyze
    assert _status_category(200) == "2xx"
    assert _status_category(299) == "2xx"
    assert _status_category(400) == "4xx"
    assert _status_category(429) == "4xx"
    assert _status_category(499) == "4xx"
    assert _status_category(500) == "5xx"
    assert _status_category(502) == "5xx"
    assert _status_category(599) == "5xx"
    assert _status_category(301) == "301xx"

    now = datetime.now()
    logs = [
        _make_log(status_code=200),
        _make_log(status_code=200),
        _make_log(status_code=400),
        _make_log(status_code=500),
        _make_log(status_code=502),
    ]
    a = _analyze(logs, now - timedelta(days=1), now)
    assert a.request_count == 5
    assert a.status_distribution["2xx"] == 2
    assert a.status_distribution["4xx"] == 1
    assert a.status_distribution["5xx"] == 2
    assert a.error_rate_overall == 3 / 5
    print(f"  [OK] 2xx=2, 4xx=1, 5xx=2, error_rate=60%")


# ============================================================
# Test 6: 多账号分散度
# ============================================================
def test_multi_account_spread():
    print("\n=== Test 6: 多账号流量分散度 ===")
    from scripts.analyze_usage import _analyze
    now = datetime.now()
    logs = []
    # channel_id=1, 3 个 account, account_1 占 80%
    for i in range(80):
        logs.append(_make_log(channel_id=1, account_id=1))
    for i in range(10):
        logs.append(_make_log(channel_id=1, account_id=2))
    for i in range(10):
        logs.append(_make_log(channel_id=1, account_id=3))
    # channel_id=2 只有 1 个 account (不应进入分散度列表)
    for i in range(20):
        logs.append(_make_log(channel_id=2, account_id=10))

    a = _analyze(logs, now - timedelta(days=1), now)
    assert len(a.channel_account_spread) == 1, "只有 channel_id=1 应进入"
    ch = a.channel_account_spread[0]
    assert ch["channel_id"] == 1
    assert ch["account_count"] == 3
    assert ch["request_count"] == 100
    assert ch["max_account_pct"] == 80.0
    # 80/10/10 entropy 应该偏低
    assert ch["shannon_entropy_bits"] < 1.0
    print(f"  [OK] channel 1: 3 accounts, max=80%, entropy={ch['shannon_entropy_bits']}")


# ============================================================
# Test 7: 自动 failover 命中率
# ============================================================
def test_failover_success_rate():
    print("\n=== Test 7: 自动 failover 命中率 (cluster 检测) ===")
    from scripts.analyze_usage import _analyze
    base = datetime.now()
    logs = []
    # 场景 A: 5 个独立失败 cluster, 每次失败后切到 account_id=2 (成功)
    for i in range(5):
        t = base + timedelta(seconds=i * 10)
        logs.append(_make_log(created_at=t, channel_id=1, account_id=1, status_code=500))
        logs.append(_make_log(created_at=t + timedelta(seconds=2), channel_id=1, account_id=2, status_code=200))
    # 场景 B: 连续 5 次失败 (200-204s, 1 个 cluster),
    #   + 后续 210s 同账号再失败 (间隔 6s > 5s, 不合并; 作为 next_req 同账号 → 切换失败)
    fail_start = base + timedelta(seconds=200)
    for i in range(5):
        logs.append(_make_log(
            created_at=fail_start + timedelta(seconds=i),
            channel_id=1, account_id=1, status_code=500,
        ))
    logs.append(_make_log(
        created_at=fail_start + timedelta(seconds=10),  # 210s
        channel_id=1, account_id=1, status_code=500,
    ))

    a = _analyze(logs, base, base + timedelta(days=1))
    # 场景 A: 5 个独立 cluster, 每个 next_req 都切账号 → 5 成功
    # 场景 B: 1 个 cluster, next_req 同账号 → 0 成功
    # 总: 5/(5+1) = 0.8333
    assert a.auto_failover_success_rate == 0.8333, (
        f"期望 0.8333, 实际 {a.auto_failover_success_rate}"
    )
    print(f"  [OK] failover 命中率 = {a.auto_failover_success_rate} (期望 0.8333, 5/6 cluster 切成功)")


# ============================================================
# Test 8: token 画像
# ============================================================
def test_token_stats():
    print("\n=== Test 8: token / latency 画像 ===")
    from scripts.analyze_usage import _analyze, _percentile
    assert _percentile([], 0.5) is None
    assert _percentile([10, 20, 30], 0.5) == 20
    assert _percentile([10, 20, 30, 40], 0.95) in (30, 40)

    now = datetime.now()
    logs = []
    for i in range(100):
        logs.append(_make_log(input_tokens=i * 100, output_tokens=i * 50, latency_ms=100 + i * 5))
    a = _analyze(logs, now - timedelta(days=1), now)
    assert a.token_stats["input_tokens"]["max"] == 9900
    assert a.token_stats["input_tokens"]["p95"] >= 9400
    assert a.token_stats["latency_ms"]["median"] >= 300
    print(
        f"  [OK] input_tokens: max={a.token_stats['input_tokens']['max']}, "
        f"p95={a.token_stats['input_tokens']['p95']}"
    )


# ============================================================
# Test 9: 时段分布
# ============================================================
def test_hourly_distribution():
    print("\n=== Test 9: 时段分布 ===")
    from scripts.analyze_usage import _analyze
    base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    logs = []
    # 10 点 50 个, 14 点 100 个, 22 点 20 个, 其他 0
    for _ in range(50):
        logs.append(_make_log(created_at=base + timedelta(hours=10)))
    for _ in range(100):
        logs.append(_make_log(created_at=base + timedelta(hours=14)))
    for _ in range(20):
        logs.append(_make_log(created_at=base + timedelta(hours=22)))

    a = _analyze(logs, base, base + timedelta(days=1))
    assert a.hourly_distribution[10] == 50
    assert a.hourly_distribution[14] == 100
    assert a.hourly_distribution[22] == 20
    assert a.hourly_distribution[0] == 0
    # peak/avg = 100 / (170/24) ≈ 14.1 → 时段差异大
    print(f"  [OK] hour[10]=50, hour[14]=100, hour[22]=20")


# ============================================================
# Test 10: V4.7+ 决策建议 - 4xx 高 → drop_params
# ============================================================
def test_recommendation_4xx_high():
    print("\n=== Test 10: 决策建议 - 4xx 高 → drop_params ===")
    from scripts.analyze_usage import _analyze
    now = datetime.now()
    logs = []
    # 90 个 200, 10 个 400 (10% 4xx)
    for _ in range(90):
        logs.append(_make_log(status_code=200))
    for _ in range(10):
        logs.append(_make_log(status_code=400))

    a = _analyze(logs, now - timedelta(days=1), now)
    recs_text = "\n".join(a.recommendations)
    assert "drop_params" in recs_text or "4xx 占比" in recs_text, (
        f"4xx 10% 应触发 drop_params 建议, 实际 recs:\n{recs_text}"
    )
    print(f"  [OK] 触发: 4xx 占比高 → drop_params")


# ============================================================
# Test 11: V4.7+ 决策建议 - 多账号不均 → 排查
# ============================================================
def test_recommendation_account_skewed():
    print("\n=== Test 11: 决策建议 - 多账号不均 → 排查 ===")
    from scripts.analyze_usage import _analyze
    now = datetime.now()
    logs = []
    # channel 1, 95/5 分布
    for _ in range(95):
        logs.append(_make_log(channel_id=1, account_id=1))
    for _ in range(5):
        logs.append(_make_log(channel_id=1, account_id=2))

    a = _analyze(logs, now - timedelta(days=1), now)
    recs_text = "\n".join(a.recommendations)
    assert "多账号不均" in recs_text or "least-used" in recs_text or "entropy" in recs_text
    print(f"  [OK] 触发: 多账号分散不均")


# ============================================================
# Test 12: V4.7+ 决策建议 - 全部健康
# ============================================================
def test_recommendation_all_healthy():
    print("\n=== Test 12: 决策建议 - 全部健康 ===")
    from scripts.analyze_usage import _analyze
    now = datetime.now()
    logs = []
    # 200 个请求, 8 个 model (top5 < 90%), 时段均匀, 账号均匀, 全部 200
    models = [
        "deepseek-chat", "qwen-max", "gpt-4o-mini", "claude-haiku",
        "glm-4-plus", "doubao-pro", "moonshot-v1", "yi-large",
    ]
    base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(200):
        logs.append(_make_log(
            created_at=base + timedelta(hours=(i * 24 // 200), minutes=i),
            channel_id=1, account_id=(i % 4) + 1,
            model=models[i % len(models)],
            status_code=200,
        ))

    a = _analyze(logs, now - timedelta(days=1), now)
    recs_text = "\n".join(a.recommendations)
    # 应该至少有一条 "保持当前架构 / 健康范围"
    assert any(
        keyword in recs_text
        for keyword in ("健康范围", "保持当前架构", "时段", "无明确驱动")
    ), f"全部健康时, recs 应提示健康或时段均匀, 实际:\n{recs_text}"
    # 不应该触发: 4xx 占比, 5xx 占比, 多账号不均, 长上下文, 流量集中
    assert "drop_params" not in recs_text, f"不应该触发 drop_params"
    assert "Cooldown 阈值" not in recs_text, f"不应该触发 Cooldown 调阈"
    assert "多账号不均" not in recs_text, f"不应该触发多账号不均"
    assert "长上下文" not in recs_text, f"不应该触发长上下文"
    assert "流量高度集中" not in recs_text, f"不应该触发流量集中"
    print(f"  [OK] 健康场景: recs = {a.recommendations}")
    print(f"  [PASS] 所有不健康信号均未触发")


# ============================================================
# Test 13: JSON 输出格式
# ============================================================
def test_analyze_function_json_output():
    print("\n=== Test 13: analyze() 返回 dict 可 JSON 化 ===")
    from scripts.analyze_usage import _analyze
    now = datetime.now()
    logs = [_make_log()]
    a = _analyze(logs, now - timedelta(days=1), now)
    # 字段可序列化 (除 datetime)
    assert isinstance(a.period_start, datetime)
    assert isinstance(a.status_distribution, dict)
    assert isinstance(a.channel_account_spread, list)
    assert isinstance(a.recommendations, list)
    # 模拟 dict 转换
    d = {
        "period_start": a.period_start.isoformat(),
        "request_count": a.request_count,
        "recommendations": a.recommendations,
    }
    s = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(s)
    assert parsed["request_count"] == 1
    print(f"  [OK] JSON 序列化往返成功")


def main():
    tests = [
        test_analyze_empty,
        test_entropy_single_account,
        test_entropy_uniform,
        test_entropy_skewed,
        test_status_distribution,
        test_multi_account_spread,
        test_failover_success_rate,
        test_token_stats,
        test_hourly_distribution,
        test_recommendation_4xx_high,
        test_recommendation_account_skewed,
        test_recommendation_all_healthy,
        test_analyze_function_json_output,
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
    print(f"AIMC 使用分析 (analyze_usage) 单元测试: {passed} passed, {failed} failed")
    print(f"{'=' * 60}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()