"""V6.16: scheduler jobstore 独立文件 + wakeup 断链 watchdog 回归测试

背景 (2026-10-01 生产事故): jobstore 与业务共用 aimc.db, 业务写锁持有 >5s 令
AsyncIOScheduler.wakeup 回调里 update_job 抛 database is locked → 调度链断,
全部 in-process job 静默停摆 3.7 天无告警 (探活/采集/告警/retention 全停)。
修复:
① jobstore 切独立文件 data/scheduler_jobs.db + busy timeout 30s
② 独立 asyncio watchdog: next_run_time 滞后 >max_lag 即 wakeup 重排 + 告警
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from intelligence.scheduler import _build_scheduler, _jobstore_url, scheduler_watchdog_task


class _StubJob:
    def __init__(self, job_id, next_run_time):
        self.id = job_id
        self.next_run_time = next_run_time


class _StubScheduler:
    """最小调度器替身: 只暴露 watchdog 用到的面 (timezone/get_jobs/wakeup/running)"""

    def __init__(self, jobs, tz=timezone.utc):
        self._jobs = jobs
        self.timezone = tz
        self.running = True
        self.wakeup_calls = 0

    def get_jobs(self):
        return list(self._jobs)

    def wakeup(self):
        self.wakeup_calls += 1


class _StubAlert:
    def __init__(self):
        self.calls = []

    async def __call__(self, message, key=None, silence_seconds=None):
        self.calls.append(message)
        return True


def _sched_with(stale_ids, tz=timezone.utc):
    """两 job: probe 恒健康 (未来); stale_ids 中的 job 滞后 30min"""
    now = datetime.now(tz=tz)
    jobs = [
        _StubJob("probe", now + timedelta(seconds=10)),
        _StubJob(
            "crawl",
            now - timedelta(minutes=30) if "crawl" in stale_ids else now + timedelta(minutes=5),
        ),
    ]
    return _StubScheduler(jobs, tz=tz)


def _run_watchdog_briefly(sched, alert, seconds=0.15, interval=0.02, max_lag=600):
    async def _run():
        task = asyncio.create_task(
            scheduler_watchdog_task(sched, interval_s=interval, max_lag_s=max_lag, alert_fn=alert)
        )
        await asyncio.sleep(seconds)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())


# ---------- jobstore 独立文件 ----------

def test_jobstore_url_is_sibling_file():
    """V6.16: jobstore URL 指向业务库同目录的 scheduler_jobs.db"""
    assert _jobstore_url(Path("/opt/aimc/data/aimc.db")) == "sqlite:////opt/aimc/data/scheduler_jobs.db"
    assert _jobstore_url(Path("./data/aimc.db")) == "sqlite:///data/scheduler_jobs.db"


def test_build_scheduler_jobstore_engine_options():
    """SQLAlchemyJobStore 收到独立文件 URL + busy timeout 30s"""
    import apscheduler.jobstores.sqlalchemy as js_mod

    captured = {}

    class _Capture(js_mod.SQLAlchemyJobStore):
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    orig = js_mod.SQLAlchemyJobStore
    js_mod.SQLAlchemyJobStore = _Capture
    try:
        _build_scheduler()
    finally:
        js_mod.SQLAlchemyJobStore = orig

    assert captured["url"].endswith("scheduler_jobs.db")
    assert captured["engine_options"]["connect_args"]["timeout"] == 30


# ---------- watchdog 行为 ----------

def test_watchdog_kicks_and_alerts_once_on_stale():
    sched = _sched_with({"crawl"})
    alert = _StubAlert()
    _run_watchdog_briefly(sched, alert)
    assert sched.wakeup_calls >= 1
    assert len(alert.calls) == 1
    assert "自愈" in alert.calls[0]


def test_watchdog_silent_when_healthy():
    sched = _sched_with(set())
    alert = _StubAlert()
    _run_watchdog_briefly(sched, alert)
    assert sched.wakeup_calls == 0
    assert alert.calls == []


def test_watchdog_alert_flag_resets_after_recovery():
    """同一断链事件只告一次; 恢复复位后再次断链可再告"""
    sched = _sched_with({"crawl"})
    alert = _StubAlert()

    async def _run():
        task = asyncio.create_task(
            scheduler_watchdog_task(sched, interval_s=0.02, max_lag_s=600, alert_fn=alert)
        )
        await asyncio.sleep(0.08)
        assert len(alert.calls) == 1  # 断链期间多轮巡检只告一次
        now = datetime.now(tz=timezone.utc)
        sched._jobs = [
            _StubJob("probe", now + timedelta(seconds=10)),
            _StubJob("crawl", now + timedelta(minutes=5)),
        ]
        await asyncio.sleep(0.08)
        assert len(alert.calls) == 1  # 恢复轮不再告警
        sched._jobs[1].next_run_time = datetime.now(tz=timezone.utc) - timedelta(minutes=30)
        await asyncio.sleep(0.08)
        assert len(alert.calls) == 2  # 新事件可再告
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())


def test_watchdog_ignores_stopped_scheduler():
    sched = _sched_with({"crawl"})
    sched.running = False
    alert = _StubAlert()
    _run_watchdog_briefly(sched, alert)
    assert sched.wakeup_calls == 0
    assert alert.calls == []
