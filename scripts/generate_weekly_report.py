"""生成 AIMC 周报（V4.4.B）

从 model_feedback_summary 读本周和上周数据，生成 Markdown 报告。
输出到 reports/weekly_YYYY-MM-DD.md，并复制为 reports/weekly-latest.md。

由 systemd timer（aimc-weekly-report.timer）每周一 9:00 触发；
也可手动执行: python scripts/generate_weekly_report.py
"""
import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import crud
from db.engine import async_session_factory
from db.init_db import init_db
from db.models import ModelFeedbackSummary

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


@dataclass
class WeekStats:
    """一周的反馈汇总统计"""

    period_start: datetime
    period_end: datetime
    rows: list[ModelFeedbackSummary] = field(default_factory=list)

    @property
    def request_count(self) -> int:
        return sum(r.request_count for r in self.rows)

    @property
    def success_count(self) -> int:
        return sum(r.success_count for r in self.rows)

    @property
    def retry_count(self) -> int:
        return sum(r.retry_count for r in self.rows)

    @property
    def cost_total(self) -> float:
        return sum(r.cost_total or 0.0 for r in self.rows)

    @property
    def fail_rate(self) -> float:
        total = self.request_count
        if total <= 0:
            return 0.0
        return (total - self.success_count) / total

    @property
    def retry_rate(self) -> float:
        total = self.request_count
        if total <= 0:
            return 0.0
        return self.retry_count / total

    @property
    def avg_feedback_score(self) -> float:
        scored = [r.feedback_score for r in self.rows if r.feedback_score is not None]
        if not scored:
            return 0.0
        return sum(scored) / len(scored)


def week_bounds(reference: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """返回 (上周一起点, 本周一起点)；本周范围 = [上周一, 本周一)."""
    reference = reference or datetime.now()
    this_week_start = datetime.combine(
        (reference - timedelta(days=reference.weekday())).date(),
        datetime.min.time(),
    )
    return this_week_start - timedelta(days=7), this_week_start


async def collect_week(session, period_start: datetime) -> WeekStats:
    """从 model_feedback_summary 读指定 period_start 的所有行。"""
    rows = await crud.list_feedback_summary(
        session,
        period_start=period_start,
        limit=1000,  # 单周模型数远小于 1000
    )
    return WeekStats(
        period_start=period_start,
        period_end=period_start + timedelta(days=7),
        rows=rows,
    )


def format_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_overview(this_week: WeekStats, last_week: WeekStats) -> list[str]:
    lines = [
        "## 📊 本周总览",
        "",
        f"- **总请求**: {this_week.request_count:,}（上周 {last_week.request_count:,}）",
        f"- **总成本**: ¥{this_week.cost_total:.2f}（上周 ¥{last_week.cost_total:.2f}）",
        f"- **失败率**: {format_pct(this_week.fail_rate)}（上周 {format_pct(last_week.fail_rate)}）",
        f"- **重试率**: {format_pct(this_week.retry_rate)}（上周 {format_pct(last_week.retry_rate)}）",
        f"- **平均反馈分**: {this_week.avg_feedback_score:.1f}（上周 {last_week.avg_feedback_score:.1f}）",
        "",
    ]
    return lines


def render_top_models(this_week: WeekStats) -> list[str]:
    scored = sorted(
        [r for r in this_week.rows if r.feedback_score is not None],
        key=lambda r: r.feedback_score,
        reverse=True,
    )[:5]
    if not scored:
        return ["## 🏆 反馈分排行 Top 5", "", "本周暂无反馈数据", ""]
    lines = [
        "## 🏆 反馈分排行 Top 5",
        "",
        "| 排名 | 模型 | 反馈分 | 调用次数 | 成本 (¥) | 失败率 |",
        "|---|---|---|---|---|---|",
    ]
    for idx, row in enumerate(scored, 1):
        cost = row.cost_total or 0.0
        fail = row.fail_rate or 0.0
        lines.append(
            f"| {idx} | `{row.model_id}` | {row.feedback_score:.1f} | "
            f"{row.request_count:,} | {cost:.2f} | {format_pct(fail)} |"
        )
    lines.append("")
    return lines


def render_trends(this_week: WeekStats, last_week: WeekStats) -> list[str]:
    last_by_model = {r.model_id: r for r in last_week.rows}
    lines = [
        "## 📈 趋势对比",
        "",
        "| 模型 | 本周分 | 上周分 | 变化 |",
        "|---|---|---|---|",
    ]
    has_any = False
    for row in this_week.rows:
        if row.feedback_score is None:
            continue
        has_any = True
        prev = last_by_model.get(row.model_id)
        prev_score = prev.feedback_score if prev else None
        if prev_score is None:
            arrow = "🆕 新"
            prev_text = "—"
        else:
            delta = row.feedback_score - prev_score
            prev_text = f"{prev_score:.1f}"
            if abs(delta) < 0.5:
                arrow = "—"
            elif delta > 0:
                arrow = f"↑ +{delta:.1f}"
            else:
                arrow = f"↓ {delta:.1f}"
        lines.append(
            f"| `{row.model_id}` | {row.feedback_score:.1f} | {prev_text} | {arrow} |"
        )
    if not has_any:
        lines.append("| — | — | — | — |")
    lines.append("")
    return lines


def render_alerts(this_week: WeekStats) -> list[str]:
    lines = ["## ⚠️ 异常告警", ""]
    items: list[str] = []
    for row in this_week.rows:
        if row.fail_rate and row.fail_rate > 0.05:
            items.append(f"- `{row.model_id}` 失败率 **{format_pct(row.fail_rate)}**（>5%）")
        if row.retry_rate and row.retry_rate > 0.05:
            items.append(f"- `{row.model_id}` 重试率 **{format_pct(row.retry_rate)}**（>5%）")
    if not items:
        lines.append("本周无模型级异常 ✅")
    else:
        lines.extend(items)
    lines.append("")
    return lines


def render_actions(this_week: WeekStats) -> list[str]:
    lines = ["## 🎯 推荐行动", ""]
    scored = [r for r in this_week.rows if r.feedback_score is not None]
    if not scored:
        lines.append("- 暂无足够数据生成建议")
        lines.append("")
        return lines
    scored.sort(key=lambda r: r.feedback_score, reverse=True)
    best = scored[0]
    worst = scored[-1]
    lines.append(
        f"- 🥇 主路由候选: `{best.model_id}`（反馈分 {best.feedback_score:.1f}）"
    )
    if len(scored) > 1 and worst.feedback_score < 70:
        lines.append(
            f"- 👀 观察期: `{worst.model_id}`（反馈分 {worst.feedback_score:.1f}, "
            "低于 70, 建议下周持续监控）"
        )
    if this_week.fail_rate > 0.05:
        lines.append(
            f"- ⚠️ 总体失败率 {format_pct(this_week.fail_rate)} 偏高, "
            "检查 v44_alerts 日志确认根因"
        )
    if this_week.cost_total > 0:
        lines.append(
            f"- 💰 本周成本 ¥{this_week.cost_total:.2f}, "
            "关注下月 v44_alerts CostRising 告警"
        )
    lines.append("- 🔄 持续周环比, 下周此时再看趋势")
    lines.append("")
    return lines


async def generate(
    period_start: Optional[datetime] = None,
    output_dir: Path = REPORTS_DIR,
) -> Path:
    """生成周报，返回报告文件路径。

    Args:
        period_start: 统计周期起点（周一）。None 表示上周一。
        output_dir: 输出目录，默认 ./reports。
    """
    if period_start is None:
        last_week_start, _ = week_bounds()
        period_start = last_week_start

    await init_db()

    # 上周起点 = 本周起点 - 7d，用于对比
    last_week_start = period_start - timedelta(days=7)

    async with async_session_factory() as session:
        this_week = await collect_week(session, period_start)
        last_week = await collect_week(session, last_week_start)

    output_dir.mkdir(parents=True, exist_ok=True)

    sections: list[str] = [
        f"# AIMC 周报 ({this_week.period_start:%Y-%m-%d} ~ {this_week.period_end:%Y-%m-%d})",
        "",
        f"_生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}_",
        "",
    ]
    sections.extend(render_overview(this_week, last_week))
    sections.extend(render_top_models(this_week))
    sections.extend(render_trends(this_week, last_week))
    sections.extend(render_alerts(this_week))
    sections.extend(render_actions(this_week))

    body = "\n".join(sections)
    report_path = output_dir / f"weekly_{this_week.period_start:%Y-%m-%d}.md"
    latest_path = output_dir / "weekly-latest.md"
    report_path.write_text(body, encoding="utf-8")
    latest_path.write_text(body, encoding="utf-8")

    logger.info(
        "周报生成完成: %s (rows=%d, requests=%d, cost=¥%.2f)",
        report_path,
        len(this_week.rows),
        this_week.request_count,
        this_week.cost_total,
    )
    return report_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 AIMC 周报 (V4.4.B)")
    parser.add_argument(
        "--period-start",
        default=None,
        help="统计周期起点（周一），默认上周一；格式 YYYY-MM-DD",
    )
    parser.add_argument(
        "--output-dir",
        default=str(REPORTS_DIR),
        help="报告输出目录，默认 ./reports",
    )
    return parser


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    args = build_parser().parse_args()
    period_start: Optional[datetime] = None
    if args.period_start:
        try:
            period_start = datetime.strptime(args.period_start, "%Y-%m-%d")
        except ValueError:
            print(
                f"无法解析 --period-start: {args.period_start}，应为 YYYY-MM-DD",
                file=sys.stderr,
            )
            return 2
    output_dir = Path(args.output_dir)
    report_path = asyncio.run(generate(period_start=period_start, output_dir=output_dir))
    print(f"周报已生成: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())