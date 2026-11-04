"""2026-11-04 — 长间隔采集 job 重启不重锚 (next_run_time 保留)

背景: `add_job(..., replace_existing=True)` 会按 trigger 从"现在"重算
next_run_time。周级源 (benchmark 604800s) 只要重启间隔 < 7 天就永远轮不到执行。
生产实证: 4 个 `batch_crawl_*_604800` 的 next_run_time 全部等于最近一次重启时刻
+ 7 天, lmsys/livebench/superclue 的 last_check_at 停在 2026-10-12。

覆盖 _batch_job_add_kwargs 四种情形:
1. 短间隔 (< 7 天) → 不保留 ({}): 重启顺延无害, 且避免启动瞬间集体补跑
2. 周级 + 原计划在未来 → 保留原 next_run_time
3. 周级 + 原计划已过期 → 立即补跑 (now)
4. 周级 + 无既有 job / 暂停(job 无 next_run) → 不传 (让 trigger 算)

跑法: python -m pytest tests/test_intel_batch_job_next_run.py -q
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class _FakeJob:
    def __init__(self, next_run_time):
        self.next_run_time = next_run_time


async def _noop_job():
    """模块级函数 — APScheduler 持久化 job 需要可被 pickle 的 func_ref"""
    pass


class _FakeScheduler:
    def __init__(self, job=None, error=False):
        self._job = job
        self._error = error

    def get_job(self, job_id):
        if self._error:
            raise RuntimeError("jobstore down")
        return self._job


def _naive(dt):
    return dt.replace(tzinfo=None)


def test_short_interval_not_touched():
    from intelligence.scheduler import _batch_job_add_kwargs

    now = datetime.now()
    future = now + timedelta(minutes=5)
    sched = _FakeScheduler(job=_FakeJob(future))
    assert _batch_job_add_kwargs(sched, "batch_crawl_x_1800", 1800, now=now) == {}, (
        "短间隔 job 不应动 (重启顺延无害)"
    )


def test_weekly_future_preserved():
    from intelligence.scheduler import _batch_job_add_kwargs

    now = datetime.now()
    future = now + timedelta(days=3)  # 原计划在 3 天后 (未过期)
    sched = _FakeScheduler(job=_FakeJob(future))
    kwargs = _batch_job_add_kwargs(sched, "batch_crawl_lmsys_604800", 604800, now=now)
    assert "next_run_time" in kwargs, "周级 job 必须保留原计划"
    assert kwargs["next_run_time"] == future, "未来计划原样保留"


def test_weekly_past_catches_up_immediately():
    from intelligence.scheduler import _batch_job_add_kwargs

    now = datetime.now()
    past = now - timedelta(days=2)  # 本应 2 天前就跑
    sched = _FakeScheduler(job=_FakeJob(past))
    kwargs = _batch_job_add_kwargs(sched, "batch_crawl_lmsys_604800", 604800, now=now)
    assert "next_run_time" in kwargs, "过期计划应补跑"
    assert kwargs["next_run_time"] == now, "过期 → now (启动即补跑)"


def test_weekly_no_existing_job_falls_back_to_trigger():
    from intelligence.scheduler import _batch_job_add_kwargs

    assert _batch_job_add_kwargs(_FakeScheduler(None), "batch_crawl_new_604800", 604800) == {}, (
        "首次注册交给 trigger 计算"
    )


def test_weekly_paused_job_not_woken():
    from intelligence.scheduler import _batch_job_add_kwargs

    sched = _FakeScheduler(job=_FakeJob(None))  # next_run_time=None = 手动暂停
    assert _batch_job_add_kwargs(sched, "batch_crawl_x_604800", 604800) == {}, (
        "暂停状态的 job 不应被启动补跑唤醒"
    )


def test_helper_requires_started_scheduler(tmp_path):
    """持久化 job 对调度器公开 API 的可见性依赖 start()。

    APScheduler 语义 (源码实证): STATE_STOPPED 时 get_job/get_jobs 只查本进程
    _pending_jobs, 从不读 jobstore — 2026-11-04 生产实证: start() 放在注册之后,
    周级 next_run 保留 (_batch_job_add_kwargs) 与残留清理 (_prune_stale_crawl_jobs)
    都拿到空 → 双双静默失效。修法=start() 提前到注册之前, 本用例锁死该顺序契约。
    """
    import asyncio

    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    from intelligence.scheduler import _batch_job_add_kwargs

    db_url = f"sqlite:///{tmp_path / 'jobs.db'}"
    future = datetime.now() + timedelta(days=2)  # 模拟上次重启留下的周级计划

    async def _run():
        # 会话 1: 把周级 job 持久化进 jobstore (上次重启留下的老计划)
        s1 = AsyncIOScheduler(jobstores={"default": SQLAlchemyJobStore(url=db_url)})
        s1.add_job(
            _noop_job, trigger=IntervalTrigger(seconds=604800),
            id="batch_crawl_probe_604800", next_run_time=future,
        )
        s1.start()
        s1.shutdown(wait=False)

        # 会话 2: 模拟重启 — 库里已有 job, 但 scheduler 尚未 start()
        s2 = AsyncIOScheduler(jobstores={"default": SQLAlchemyJobStore(url=db_url)})
        try:
            assert s2.get_job("batch_crawl_probe_604800") is None, (
                "start() 前公开 API 只查本进程 pending, 持久化 job 不可见"
            )
            assert not _batch_job_add_kwargs(
                s2, "batch_crawl_probe_604800", 604800,
            ), "start() 前不可依赖公开 API — 顺序契约"
            s2.start()
            kwargs = _batch_job_add_kwargs(
                s2, "batch_crawl_probe_604800", 604800,
            )
            assert kwargs["next_run_time"].replace(tzinfo=None) == future, (
                "start() 后应保留周级原计划 (未来不变)"
            )
        finally:
            s2.shutdown(wait=False)

    asyncio.run(_run())


def test_weekly_jobstore_error_falls_back():
    from intelligence.scheduler import _batch_job_add_kwargs

    sched = _FakeScheduler(error=True)
    assert _batch_job_add_kwargs(sched, "batch_crawl_x_604800", 604800) == {}, (
        "jobstore 异常时降级为默认行为, 不阻断启动"
    )


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-q"]))