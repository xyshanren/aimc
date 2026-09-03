"""AIMC 稳定期使用分析 (V4.6.4 后续待定)

定位: 给开发者看的产品决策依据, 不是给运营看的周报
目的: 从 usage_logs 自动提取信号, 回答 "V4.7+ 该做什么 / 哪些功能验证成功了 / 哪里有真实痛点"

跟 V4.4.B 周报 (scripts/generate_weekly_report.py) 的区别:
- 周报: 业务向, "本周用了什么 / 反馈分排行 / 推荐行动"
- 本脚本: 工程向, "V4.6 多账号是否真的分散 / 哪些痛点最值得做 / V4.7+ 优先级"

输出:
- reports/usage_analysis_YYYY-MM-DD.md (人读)
- reports/usage_analysis_latest.md (symlink)
- --json-only 模式只输出 JSON (给 Dashboard 用)

用法:
    python scripts/analyze_usage.py                    # 默认 30 天
    python scripts/analyze_usage.py --days 7           # 7 天
    python scripts/analyze_usage.py --json-only        # 只 JSON
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.engine import async_session_factory
from db.init_db import init_db
from db.models import UsageLog
from sqlalchemy import func, select

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


# ============================================================
# 数据结构
# ============================================================
@dataclass
class DailyBucket:
    """按天聚合的指标"""
    day: str  # YYYY-MM-DD
    request_count: int = 0
    error_count: int = 0
    total_cost: float = 0.0
    total_latency_ms: int = 0
    p95_latency_ms: int = 0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.request_count if self.request_count else 0.0


@dataclass
class UsageAnalysis:
    """一次分析的完整结果"""
    period_start: datetime
    period_end: datetime
    request_count: int = 0
    success_count: int = 0

    # status code 分布 (V4.6.1 验证依据)
    status_distribution: dict[str, int] = field(default_factory=dict)

    # channel 维度 (V4.6 多账号分散度验证)
    channel_account_spread: list[dict] = field(default_factory=list)
    channel_error_rate: list[dict] = field(default_factory=list)

    # model 维度 (V4.7+ 痛点信号)
    model_concentration: list[dict] = field(default_factory=list)  # Top 5 model 占总流量
    model_error_breakdown: list[dict] = field(default_factory=list)  # 哪些 model 最常 4xx
    long_context_requests: int = 0  # input_tokens > 32k

    # token 画像
    token_stats: dict = field(default_factory=dict)

    # V6.0 Phase 2 (2026-09-03): 成本审计 (标价 vs 实付)
    cost_audit: dict = field(default_factory=dict)

    # 时段分布 (V4.7+ 缓存/调度决策)
    hourly_distribution: list[int] = field(default_factory=list)  # 24 个 bucket

    # V4.6.1 Cooldown 触发率
    error_rate_overall: float = 0.0
    auto_failover_success_rate: float = 0.0  # 失败后是否真的换了 account

    # V4.7+ 决策建议 (核心价值)
    recommendations: list[str] = field(default_factory=list)


# ============================================================
# SQL 聚合 (核心)
# ============================================================
async def _fetch_logs(session, start: datetime, end: datetime) -> list[UsageLog]:
    """拉取时间窗口内的全部 usage_logs."""
    result = await session.execute(
        select(UsageLog).where(
            UsageLog.created_at >= start,
            UsageLog.created_at < end,
        )
    )
    return list(result.scalars().all())


def _percentile(values: list[int | float], ratio: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * ratio)))
    return ordered[idx]


def _shannon_entropy(counts: list[int]) -> float:
    """Shannon entropy (bits). 越均匀越大. 范围 [0, log2(N)]."""
    total = sum(counts)
    if total == 0 or len(counts) <= 1:
        return 0.0
    entropy = 0.0
    for c in counts:
        if c > 0:
            p = c / total
            entropy -= p * math.log2(p)
    return round(entropy, 3)


def _status_category(code: int) -> str:
    """将 status code 归类为 2xx / 4xx / 5xx."""
    if 200 <= code < 300:
        return "2xx"
    if 400 <= code < 500:
        return "4xx"
    if 500 <= code < 600:
        return "5xx"
    return f"{code}xx"


def _analyze(logs: list[UsageLog], period_start: datetime, period_end: datetime) -> UsageAnalysis:
    """同步分析 (CPU-bound, 不阻塞 loop)."""
    a = UsageAnalysis(period_start=period_start, period_end=period_end)
    # 不做 early return, 让 _generate_recommendations 看到 request_count=0 自行处理

    a.request_count = len(logs)
    a.success_count = sum(1 for r in logs if r.status_code and 200 <= r.status_code < 300)

    # ----- 1. status 分布 (V4.6.1 验证) -----
    cat_counter: Counter = Counter()
    code_counter: Counter = Counter()
    for r in logs:
        if r.status_code is None:
            cat_counter["unknown"] += 1
            continue
        cat_counter[_status_category(r.status_code)] += 1
        code_counter[r.status_code] += 1
    a.status_distribution = dict(
        sorted(cat_counter.items(), key=lambda kv: -kv[1])
    )
    # 4xx/5xx 子分布
    a.error_rate_overall = (a.request_count - a.success_count) / a.request_count if a.request_count else 0.0

    # ----- 2. channel 维度: 多账号分散度 -----
    ch_acc_map: dict[int, Counter] = defaultdict(Counter)  # channel_id -> {account_id: count}
    ch_total: Counter = Counter()
    ch_err: Counter = Counter()
    for r in logs:
        if r.channel_id is None:
            continue
        if r.account_id is not None:
            ch_acc_map[r.channel_id][r.account_id] += 1
        ch_total[r.channel_id] += 1
        if r.status_code and r.status_code >= 400:
            ch_err[r.channel_id] += 1

    for ch_id, acc_counter in ch_acc_map.items():
        if len(acc_counter) < 2:
            continue  # 只有 1 个 account 的 channel 不评估
        counts = list(acc_counter.values())
        entropy = _shannon_entropy(counts)
        max_account_pct = max(counts) / ch_total[ch_id] * 100
        # 找 channel_name (没有就 fallback id)
        # 日志里没存 channel_name, 用 channel_id 当 key 即可 (Dashboard 反查名字)
        a.channel_account_spread.append({
            "channel_id": ch_id,
            "account_count": len(acc_counter),
            "request_count": ch_total[ch_id],
            "shannon_entropy_bits": entropy,
            "max_account_pct": round(max_account_pct, 1),
        })
    a.channel_account_spread.sort(key=lambda r: -r["request_count"])

    # channel 错误率 Top 5
    for ch_id, total in ch_total.most_common(20):
        errs = ch_err.get(ch_id, 0)
        a.channel_error_rate.append({
            "channel_id": ch_id,
            "request_count": total,
            "error_count": errs,
            "error_rate": round(errs / total, 4) if total else 0.0,
        })
    a.channel_error_rate.sort(key=lambda r: -r["error_count"])
    a.channel_error_rate = a.channel_error_rate[:5]

    # ----- 3. model 维度: 集中度 + 错误分布 -----
    model_total: Counter = Counter()
    model_err: Counter = Counter()
    model_4xx: Counter = Counter()
    for r in logs:
        if not r.model:
            continue
        model_total[r.model] += 1
        if r.status_code and r.status_code >= 400:
            model_err[r.model] += 1
        if r.status_code and 400 <= r.status_code < 500:
            model_4xx[r.model] += 1

    # Top 5 集中度
    total = sum(model_total.values())
    top5 = model_total.most_common(5)
    top5_pct = sum(c for _, c in top5) / total * 100 if total else 0.0
    for model, cnt in top5:
        a.model_concentration.append({
            "model": model,
            "request_count": cnt,
            "share_pct": round(cnt / total * 100, 2) if total else 0.0,
            "error_rate": round(model_err[model] / cnt, 4) if cnt else 0.0,
        })

    # model 4xx Top 5 (V4.7+ drop_params 信号)
    for model, cnt in model_4xx.most_common(5):
        a.model_error_breakdown.append({
            "model": model,
            "4xx_count": cnt,
            "total_requests": model_total[model],
            "4xx_rate": round(cnt / model_total[model], 4) if model_total[model] else 0.0,
        })

    # ----- 4. 长上下文检测 (V4.7+ 上下文能力决策) -----
    a.long_context_requests = sum(
        1 for r in logs
        if r.input_tokens is not None and r.input_tokens > 32_000
    )

    # ----- 5. token 画像 -----
    in_tokens = [r.input_tokens for r in logs if r.input_tokens is not None]
    out_tokens = [r.output_tokens for r in logs if r.output_tokens is not None]
    latencies = [r.latency_ms for r in logs if r.latency_ms is not None]
    costs = [r.cost_estimate for r in logs if r.cost_estimate is not None]
    a.token_stats = {
        "input_tokens": {
            "median": _percentile(in_tokens, 0.5),
            "p95": _percentile(in_tokens, 0.95),
            "p99": _percentile(in_tokens, 0.99),
            "max": max(in_tokens) if in_tokens else None,
        },
        "output_tokens": {
            "median": _percentile(out_tokens, 0.5),
            "p95": _percentile(out_tokens, 0.95),
            "p99": _percentile(out_tokens, 0.99),
            "max": max(out_tokens) if out_tokens else None,
        },
        "latency_ms": {
            "median": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
        },
        "cost_yuan": {
            "total": round(sum(costs), 2),
            "avg": round(sum(costs) / len(costs), 4) if costs else 0.0,
        },
    }

    # ----- 6. 时段分布 -----
    hourly = [0] * 24
    for r in logs:
        if r.created_at is not None:
            hourly[r.created_at.hour] += 1
    a.hourly_distribution = hourly

    # ----- 6b. V6.0 Phase 2: 成本审计 (标价 vs 实付) -----
    # 节省只统计标价已知行 (list_cost_estimate 非空) — 标价未知行不计入,
    # 避免把节省算成负数; 覆盖率单列暴露数据完整度
    # getattr 防御: mock/异构 log 对象可能没有该属性
    priced_logs = [
        r for r in logs if getattr(r, "list_cost_estimate", None) is not None
    ]
    cost_total = sum(r.cost_estimate or 0 for r in logs)
    list_total = sum(r.list_cost_estimate or 0 for r in priced_logs)
    priced_cost_total = sum(r.cost_estimate or 0 for r in priced_logs)
    by_model_cost: dict[str, dict] = {}
    for r in priced_logs:
        m = by_model_cost.setdefault(
            r.model or "(未知)", {"requests": 0, "cost": 0.0, "list": 0.0}
        )
        m["requests"] += 1
        m["cost"] += r.cost_estimate or 0
        m["list"] += r.list_cost_estimate or 0
    top_cost_models = sorted(by_model_cost.items(), key=lambda kv: -kv[1]["list"])[:10]
    a.cost_audit = {
        "request_count": a.request_count,
        "priced_request_count": len(priced_logs),
        "coverage_pct": round(len(priced_logs) / a.request_count * 100, 1) if a.request_count else 0.0,
        "cost_total": round(cost_total, 4),
        "list_cost_total": round(list_total, 4),
        "saved_total": round(list_total - priced_cost_total, 4),
        "by_model": [
            {
                "model": m,
                "requests": d["requests"],
                "cost": round(d["cost"], 4),
                "list_cost": round(d["list"], 4),
                "saved": round(d["list"] - d["cost"], 4),
            }
            for m, d in top_cost_models
        ],
    }

    # ----- 7. 自动 failover 命中率 (V4.6.1 验证) -----
    # 算法: 把连续失败聚合成 cluster, 关注 cluster 后的第一个请求是否切了账号
    # 不设 5s 间隔限制 (按顺序就足以区分 cluster vs 下一个独立请求)
    sorted_logs = sorted(
        [r for r in logs if r.channel_id is not None and r.account_id is not None and r.created_at is not None],
        key=lambda r: (r.channel_id, r.created_at),
    )
    failover_total = 0
    failover_success = 0
    n = len(sorted_logs)
    i = 0
    while i < n:
        cur = sorted_logs[i]
        if not (cur.status_code and cur.status_code >= 400):
            i += 1
            continue
        # 合并同 channel 间隔 < 5s 的连续失败
        cluster_end = i
        while (
            cluster_end + 1 < n
            and sorted_logs[cluster_end + 1].channel_id == cur.channel_id
            and sorted_logs[cluster_end + 1].status_code
            and sorted_logs[cluster_end + 1].status_code >= 400
            and sorted_logs[cluster_end + 1].created_at - sorted_logs[cluster_end].created_at < timedelta(seconds=5)
        ):
            cluster_end += 1
        # cluster 后 1 个请求 (同 channel) 才是 next_req
        next_req: Optional[UsageLog] = None
        for j in range(cluster_end + 1, n):
            nxt = sorted_logs[j]
            if nxt.channel_id != cur.channel_id:
                continue
            next_req = nxt
            break
        if next_req is not None:
            failover_total += 1
            if next_req.account_id != sorted_logs[cluster_end].account_id:
                failover_success += 1
        i = cluster_end + 1
    a.auto_failover_success_rate = round(failover_success / failover_total, 4) if failover_total else None

    # ----- 8. V4.7+ 决策建议 -----
    a.recommendations = _generate_recommendations(a, top5_pct)

    return a


def _generate_recommendations(a: UsageAnalysis, top5_pct: float) -> list[str]:
    """根据聚合数据自动给出 V4.7+ 优先级建议."""
    # 空数据 / 极少量数据 (< 100 请求): 不做信号判断, 避免误报
    if a.request_count < 100:
        return [
            f"⏸ **样本不足** ({a.request_count} < 100 请求) — "
            "V4.7+ 优先级无明确驱动, 建议累积 100+ 请求后再判断"
        ]
    recs: list[str] = []

    # 信号 1: 4xx 占比 → V4.7+ drop_params
    rate_4xx = (a.status_distribution.get("4xx", 0)) / a.request_count if a.request_count else 0.0
    if rate_4xx >= 0.02:
        recs.append(
            f"⚠️ **4xx 占比 {rate_4xx:.1%}** (>= 2%) — **V4.7+ drop_params** 优先级提升 "
            f"(跨模型转发时自动丢弃目标不支持的参数, 避免 400 错误)"
        )
    elif rate_4xx >= 0.005:
        recs.append(
            f"👀 **4xx 占比 {rate_4xx:.1%}** (0.5%-2%) — 观察期, drop_params 可做可不做"
        )

    # 信号 2: 5xx 占比 → Cooldown 是否够激进
    rate_5xx = a.status_distribution.get("5xx", 0) / a.request_count if a.request_count else 0.0
    if rate_5xx >= 0.05:
        recs.append(
            f"⚠️ **5xx 占比 {rate_5xx:.1%}** (>= 5%) — Cooldown 阈值 (当前 1) 可能过于激进, "
            f"或某上游反复挂. 排查 top-5 error channel"
        )

    # 信号 3: 多账号分散度
    if a.channel_account_spread:
        # entropy_bits, log2(N_accounts) 是 max, 计算 max_entropy
        for ch in a.channel_account_spread:
            if ch["account_count"] >= 2 and ch["shannon_entropy_bits"] < 0.5:
                recs.append(
                    f"⚠️ **channel_id={ch['channel_id']} 多账号不均** "
                    f"({ch['max_account_pct']}% 流量走单账号, entropy={ch['shannon_entropy_bits']}) "
                    f"— least-used 排序失效或单账号配置问题"
                )

    # 信号 4: 自动 failover 命中率
    if a.auto_failover_success_rate is not None and a.auto_failover_success_rate < 0.8:
        recs.append(
            f"⚠️ **自动 failover 命中率 {a.auto_failover_success_rate:.1%}** "
            f"(< 80%) — 失败后未切账号, 可能是 cooldown 未触发或单账号 channel"
        )

    # 信号 5: 长上下文占比
    if a.long_context_requests > 0 and a.request_count > 0:
        long_ctx_pct = a.long_context_requests / a.request_count
        if long_ctx_pct >= 0.05:
            recs.append(
                f"👀 **长上下文请求 {long_ctx_pct:.1%}** (>= 5%, > 32k tokens) "
                f"— 可能需要 V4.7+ 长上下文优化"
            )

    # 信号 6: 流量集中度 → 缓存价值
    if top5_pct >= 90:
        recs.append(
            f"💡 **Top 5 model 占流量 {top5_pct:.1f}%** (>= 90%) — "
            f"流量高度集中, V4.7+ 语义缓存有显著价值"
        )

    # 信号 7: 流量平稳 → 不必做调度优化
    hourly = a.hourly_distribution
    if hourly and max(hourly) > 0:
        peak_ratio = max(hourly) / (sum(hourly) / 24)
        if peak_ratio < 2.0:
            recs.append(
                f"✅ **流量时段分布均匀** (peak/avg={peak_ratio:.2f}) — "
                f"V4.7+ 价格趋势预测/智能调度收益不大, 优先度低"
            )

    # 信号 8: 时段差异大 → 调度优化
    if hourly and max(hourly) > 0:
        peak_ratio = max(hourly) / (sum(hourly) / 24) if sum(hourly) else 0
        if peak_ratio >= 3.0:
            recs.append(
                f"⚠️ **流量时段差异大** (peak/avg={peak_ratio:.2f}) — "
                f"V4.7+ 智能调度/缓存价值提升"
            )

    # 信号 9: 免费渠道节省量化 (V6.0 Phase 2)
    ca = a.cost_audit
    if ca.get("coverage_pct", 0) >= 50 and ca.get("saved_total", 0) > 0:
        recs.append(
            f"💰 **免费/订阅渠道节省 ¥{ca['saved_total']:.2f}** "
            f"(标价口径 ¥{ca['list_cost_total']:.2f}, 标价覆盖率 {ca['coverage_pct']}%) — "
            "免费车道有真实价值, 优先把流量引向免费/订阅渠道"
        )
    if 0 < ca.get("coverage_pct", 100) < 50 and a.request_count >= 100:
        recs.append(
            f"👀 **成本标价覆盖率仅 {ca['coverage_pct']}%** — 部分 model_registry 缺"
            "官方标价 (list_input_price/list_output_price), 节省量化不完整, 可补标价数据"
        )

    # 信号 10: 没数据
    if not recs:
        recs.append(
            "✅ **所有信号在健康范围内** — V4.7+ 优先级无明确驱动, "
            "建议保持当前架构, 继续累积数据"
        )

    return recs


# ============================================================
# Markdown 渲染
# ============================================================
def render_markdown(a: UsageAnalysis) -> str:
    lines = [
        f"# AIMC 使用分析 ({a.period_start:%Y-%m-%d} ~ {a.period_end:%Y-%m-%d})",
        "",
        f"_生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}_",
        "_定位: 产品决策依据 (V4.7+ 优先级 / V4.6 验证), 不替代 V4.4.B 周报_",
        "",
        "## 🎯 V4.7+ 决策建议 (核心价值)",
        "",
    ]
    for rec in a.recommendations:
        lines.append(f"- {rec}")
    lines.append("")

    # 1. status 分布
    lines.append("## 📊 Status Code 分布 (V4.6.1 Cooldown 验证)")
    lines.append("")
    if a.status_distribution:
        lines.append("| 类别 | 请求数 | 占比 |")
        lines.append("|---|---|---|")
        for cat, cnt in a.status_distribution.items():
            pct = cnt / a.request_count * 100 if a.request_count else 0.0
            lines.append(f"| {cat} | {cnt:,} | {pct:.2f}% |")
    else:
        lines.append("无数据")
    lines.append("")
    lines.append(
        f"- **整体错误率**: {a.error_rate_overall:.2%} "
        f"({'偏高 ⚠️' if a.error_rate_overall > 0.05 else '正常 ✅'})"
    )
    if a.auto_failover_success_rate is not None:
        lines.append(
            f"- **自动 failover 命中率**: {a.auto_failover_success_rate:.2%} "
            f"({'正常 ✅' if a.auto_failover_success_rate >= 0.8 else '偏低 ⚠️'})"
        )
    lines.append("")

    # 2. channel 分散度
    lines.append("## 🌀 多账号流量分散度 (V4.6.1 least-used 验证)")
    lines.append("")
    if a.channel_account_spread:
        lines.append("| channel_id | account 数 | 请求数 | entropy (bits) | max 账号占比 |")
        lines.append("|---|---|---|---|---|")
        for ch in a.channel_account_spread:
            lines.append(
                f"| {ch['channel_id']} | {ch['account_count']} | {ch['request_count']:,} | "
                f"{ch['shannon_entropy_bits']} | {ch['max_account_pct']}% |"
            )
    else:
        lines.append("无 multi-account channel")
    lines.append("")

    # 3. channel 错误率
    if a.channel_error_rate:
        lines.append("## ⚠️ Channel 错误率 Top 5")
        lines.append("")
        lines.append("| channel_id | 请求数 | 错误数 | 错误率 |")
        lines.append("|---|---|---|---|")
        for ch in a.channel_error_rate:
            if ch["error_count"] > 0:
                lines.append(
                    f"| {ch['channel_id']} | {ch['request_count']:,} | {ch['error_count']} | "
                    f"{ch['error_rate']:.2%} |"
                )
        lines.append("")

    # 4. model 集中度
    if a.model_concentration:
        lines.append("## 🎯 Model 流量集中度 (V4.7+ 缓存价值判断)")
        lines.append("")
        lines.append("| 排名 | 模型 | 请求数 | 占比 | 错误率 |")
        lines.append("|---|---|---|---|---|")
        for idx, m in enumerate(a.model_concentration, 1):
            lines.append(
                f"| {idx} | `{m['model']}` | {m['request_count']:,} | "
                f"{m['share_pct']}% | {m['error_rate']:.2%} |"
            )
        lines.append("")

    # 5. model 4xx 分布
    if a.model_error_breakdown:
        lines.append("## 🚫 Model 4xx Top 5 (V4.7+ drop_params 信号)")
        lines.append("")
        lines.append("| 模型 | 4xx 次数 | 总请求 | 4xx 占比 |")
        lines.append("|---|---|---|---|")
        for m in a.model_error_breakdown:
            lines.append(
                f"| `{m['model']}` | {m['4xx_count']} | {m['total_requests']:,} | "
                f"{m['4xx_rate']:.2%} |"
            )
        lines.append("")

    # 6. token 画像
    if a.token_stats.get("input_tokens", {}).get("median"):
        lines.append("## 📏 Token / Latency / Cost 画像")
        lines.append("")
        ts = a.token_stats
        lines.append("| 指标 | median | p95 | p99 | max |")
        lines.append("|---|---|---|---|---|")
        for label, key in [
            ("Input tokens", "input_tokens"),
            ("Output tokens", "output_tokens"),
            ("Latency (ms)", "latency_ms"),
        ]:
            stats = ts.get(key, {})
            lines.append(
                f"| {label} | {stats.get('median') or '—'} | "
                f"{stats.get('p95') or '—'} | {stats.get('p99') or '—'} | "
                f"{stats.get('max') or '—'} |"
            )
        cost = ts.get("cost_yuan", {})
        lines.append("")
        lines.append(
            f"- **总成本**: ¥{cost.get('total', 0):.2f} | "
            f"**单次均成本**: ¥{cost.get('avg', 0):.4f}"
        )
        if a.long_context_requests > 0:
            lines.append(
                f"- **长上下文请求** (>32k tokens): {a.long_context_requests} 次 "
                f"({a.long_context_requests / a.request_count:.1%})"
            )
        lines.append("")

    # 6b. 成本审计 (V6.0 Phase 2)
    ca = a.cost_audit
    if ca.get("request_count"):
        lines.append("## 💰 成本审计 (标价 vs 实付)")
        lines.append("")
        lines.append(
            f"- **实付**: ¥{ca.get('cost_total', 0):.2f} | "
            f"**官方标价口径**: ¥{ca.get('list_cost_total', 0):.2f} | "
            f"**节省**: ¥{ca.get('saved_total', 0):.2f}"
        )
        lines.append(
            f"- **标价覆盖率**: {ca.get('coverage_pct', 0)}% "
            f"({ca.get('priced_request_count', 0)}/{ca.get('request_count', 0)} 请求)"
            " — 覆盖率低时节省数字偏保守 (标价未知的模型不计入)"
        )
        if ca.get("by_model"):
            lines.append("")
            lines.append("| 模型 | 请求 | 实付 | 标价 | 节省 |")
            lines.append("|---|---|---|---|---|")
            for m in ca["by_model"]:
                lines.append(
                    f"| `{m['model']}` | {m['requests']:,} | ¥{m['cost']:.4f} | "
                    f"¥{m['list_cost']:.4f} | ¥{m['saved']:.4f} |"
                )
        lines.append("")

    # 7. 时段分布
    if any(a.hourly_distribution):
        lines.append("## ⏰ 时段分布 (V4.7+ 调度/缓存决策)")
        lines.append("")
        lines.append("```")
        lines.append("Hour | count | bar")
        lines.append("-----+-------+--------")
        max_cnt = max(a.hourly_distribution)
        for h, cnt in enumerate(a.hourly_distribution):
            bar = "█" * int(cnt / max_cnt * 20) if max_cnt else ""
            lines.append(f"  {h:02d} | {cnt:5d} | {bar}")
        lines.append("```")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "_📝 本报告是稳定期跟踪工具, 与 V4.4.B 周报互补: 周报 = 运营向 (用了什么/反馈分), "
        "本报告 = 工程向 (V4.7+ 该做什么). 建议每月跑一次, 累积趋势判断._"
    )
    return "\n".join(lines) + "\n"


# ============================================================
# 主流程
# ============================================================
async def analyze(
    days: int = 30,
    period_start: Optional[datetime] = None,
    output_dir: Path = REPORTS_DIR,
    json_only: bool = False,
) -> dict[str, Any]:
    """执行分析, 返回结构化结果 (dict 形式, 便于 JSON 输出)."""
    if period_start is None:
        end = datetime.now()
        start = end - timedelta(days=days)
    else:
        start = period_start
        end = start + timedelta(days=days)

    await init_db()
    async with async_session_factory() as session:
        logs = await _fetch_logs(session, start, end)

    analysis = _analyze(logs, start, end)

    # 转 dict (供 JSON 序列化)
    result = {
        "period_start": analysis.period_start.isoformat(),
        "period_end": analysis.period_end.isoformat(),
        "request_count": analysis.request_count,
        "success_count": analysis.success_count,
        "error_rate_overall": analysis.error_rate_overall,
        "status_distribution": analysis.status_distribution,
        "auto_failover_success_rate": analysis.auto_failover_success_rate,
        "channel_account_spread": analysis.channel_account_spread,
        "channel_error_rate": analysis.channel_error_rate,
        "model_concentration": analysis.model_concentration,
        "model_error_breakdown": analysis.model_error_breakdown,
        "token_stats": analysis.token_stats,
        "cost_audit": analysis.cost_audit,
        "hourly_distribution": analysis.hourly_distribution,
        "long_context_requests": analysis.long_context_requests,
        "recommendations": analysis.recommendations,
    }

    if json_only:
        return result

    output_dir.mkdir(parents=True, exist_ok=True)
    md = render_markdown(analysis)
    report_path = output_dir / f"usage_analysis_{start:%Y-%m-%d}.md"
    latest_path = output_dir / "usage_analysis_latest.md"
    report_path.write_text(md, encoding="utf-8")
    latest_path.write_text(md, encoding="utf-8")
    logger.info(
        "使用分析完成: %s (requests=%d, recommendations=%d)",
        report_path,
        analysis.request_count,
        len(analysis.recommendations),
    )
    result["report_path"] = str(report_path)
    return result


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="AIMC 稳定期使用分析 (产品决策依据)"
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="统计窗口天数 (默认 30)",
    )
    parser.add_argument(
        "--period-start",
        help="周期起点 (YYYY-MM-DD), 默认 = 今天 - days",
    )
    parser.add_argument(
        "--output-dir", default=str(REPORTS_DIR),
        help="报告输出目录, 默认 ./reports",
    )
    parser.add_argument(
        "--json-only", action="store_true",
        help="只输出 JSON (给 Dashboard / 其他脚本消费)",
    )
    args = parser.parse_args()

    period_start: Optional[datetime] = None
    if args.period_start:
        try:
            period_start = datetime.strptime(args.period_start, "%Y-%m-%d")
        except ValueError:
            print(f"无法解析 --period-start: {args.period_start}", file=sys.stderr)
            return 2

    result = asyncio.run(analyze(
        days=args.days,
        period_start=period_start,
        output_dir=Path(args.output_dir),
        json_only=args.json_only,
    ))
    if args.json_only:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"使用分析已生成: {result.get('report_path')}")
        print(f"建议数: {len(result['recommendations'])} 条 (查看 {result.get('report_path')} 首节)")
    return 0


if __name__ == "__main__":
    sys.exit(main())