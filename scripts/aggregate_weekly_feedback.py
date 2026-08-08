"""从 usage_logs 聚合上一周的模型隐式反馈。"""

import argparse
import asyncio
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from db.crud import upsert_feedback_summary
from db.engine import async_session_factory
from db.models import UsageLog
from sqlalchemy import select

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def week_bounds(reference: datetime | None = None) -> tuple[datetime, datetime]:
    reference = reference or datetime.now()
    start = datetime.combine((reference - timedelta(days=reference.weekday())).date(), datetime.min.time())
    return start - timedelta(days=7), start


def percentile(values: list[int], ratio: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * ratio)))
    return ordered[index]


def model_family(model: str) -> str:
    return (model or "unknown").split("-", 1)[0].lower()


async def aggregate(period_start: datetime | None = None) -> int:
    start, end = (period_start, period_start + timedelta(days=7)) if period_start else week_bounds()
    async with async_session_factory() as session:
        result = await session.execute(
            select(UsageLog).where(UsageLog.created_at >= start, UsageLog.created_at < end)
        )
        logs = list(result.scalars().all())
        grouped: dict[str, list[UsageLog]] = defaultdict(list)
        for log in logs:
            if log.model:
                grouped[log.model].append(log)
        count = 0
        for model, rows in grouped.items():
            rows = sorted(rows, key=lambda r: r.created_at or start)
            latencies = [r.latency_ms for r in rows if r.latency_ms is not None]
            outputs = [r.output_tokens for r in rows if r.output_tokens is not None]
            failures = sum(1 for r in rows if r.status_code is not None and r.status_code != 200)
            clients = Counter(str(r.account_id or "unknown") for r in rows)
            request_count = len(rows)
            retry_count = 0
            for previous, current in zip(rows, rows[1:]):
                if previous.account_id == current.account_id and current.created_at and previous.created_at and current.created_at - previous.created_at <= timedelta(minutes=5):
                    retry_count += 1
            retry_rate = retry_count / request_count if request_count else 0.0
            fail_rate = failures / request_count if request_count else 0.0
            p95 = percentile(latencies, 0.95)
            score = max(0.0, min(100.0, 100 - 30 * retry_rate - 50 * fail_rate - 0.01 * (p95 or 0)))
            await upsert_feedback_summary(
                session,
                model_id=model,
                family=model_family(model),
                period_start=start,
                period_end=end,
                request_count=request_count,
                success_count=request_count - failures,
                retry_count=retry_count,
                input_tokens=sum(r.input_tokens or 0 for r in rows),
                output_tokens=sum(r.output_tokens or 0 for r in rows),
                cost_total=sum(r.cost_estimate or 0 for r in rows),
                fail_rate=fail_rate,
                retry_rate=retry_rate,
                p50_latency_ms=percentile(latencies, 0.50),
                p95_latency_ms=p95,
                avg_output_tokens=round(sum(outputs) / len(outputs)) if outputs else None,
                feedback_score=score,
                client_breakdown=json.dumps(clients, ensure_ascii=False),
            )
            count += 1
        await session.commit()
    logger.info("反馈聚合完成: period=%s~%s models=%d", start.date(), end.date(), count)
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--period-start", help="周期起始日期，默认上一完整周")
    args = parser.parse_args()
    start = datetime.fromisoformat(args.period_start) if args.period_start else None
    asyncio.run(aggregate(start))
