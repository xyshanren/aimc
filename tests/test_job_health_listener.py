"""轴2 (2026-11-04): job_health 写入器测试 — 失败现场保留 + 真实耗时

背景: 旧 listener 实现两个数据质量缺陷 — ① 成功把 last_error_* 清空
(45 天 1024 次探活失败零可诊断性); ② last_duration_ms 误填
scheduled_run_time 的 epoch 毫秒 (量级=当前时间戳, 完全不可用)。

跑法: python -m pytest tests/test_job_health_listener.py -v
"""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from sqlalchemy import create_engine

from db.models import Base, JobHealth


@pytest.fixture
def sync_db(monkeypatch, tmp_path):
    """tmp 文件库 + db.engine.sync_engine 打桩 (函数内 import)"""
    eng = create_engine("sqlite:///" + str(tmp_path / "jh.db"))
    Base.metadata.create_all(eng)
    import db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "sync_engine", eng, raising=False)
    return eng


class TestRecordJobHealthEvent:
    def test_failure_then_success_preserves_error_fields(self, sync_db):
        """核心修复: 失败写现场 → 后续成功不清空 (last_error_* = 最近一次失败)"""
        from intelligence.scheduler import record_job_health_event

        record_job_health_event("job_a", success=False,
                                exception=RuntimeError("upstream hang"))
        record_job_health_event("job_a", success=True)

        from sqlalchemy.orm import Session
        with Session(sync_db) as s:
            row = s.get(JobHealth, "job_a")
            assert row.total_failures == 1
            assert row.total_successes == 1
            assert row.last_error_at is not None, "成功不得抹掉失败现场"
            assert "upstream hang" in (row.last_error_msg or "")

    def test_failure_records_error_detail(self, sync_db):
        """两次失败: 计数累加, 现场取最近一次"""
        from intelligence.scheduler import record_job_health_event

        record_job_health_event("job_b", success=False,
                                exception=RuntimeError("boom"))
        record_job_health_event("job_b", success=False,
                                exception=RuntimeError("boom2"))

        from sqlalchemy.orm import Session
        with Session(sync_db) as s:
            row = s.get(JobHealth, "job_b")
            assert row.total_failures == 2
            assert "boom2" in (row.last_error_msg or "")

    def test_duration_is_elapsed_not_epoch(self, sync_db):
        """last_duration_ms = now - scheduled_run_time (真实耗时近似),
        旧实现误填 scheduled_run_time 的 epoch 毫秒 (量级 ~1.7e12 不可用)"""
        from intelligence.scheduler import record_job_health_event

        sched = datetime.now(timezone.utc) - timedelta(milliseconds=1500)
        record_job_health_event("job_c", success=True,
                                scheduled_run_time=sched)

        from sqlalchemy.orm import Session
        with Session(sync_db) as s:
            row = s.get(JobHealth, "job_c")
            assert row.last_duration_ms is not None
            assert row.last_duration_ms < 3_600_000, (
                "耗时应在小时级以内 (旧实现的 epoch 毫秒 ~1.7e12)")
            assert row.last_duration_ms >= 1400, "至少覆盖 1.5s 的模拟延迟"

    def test_no_scheduled_time_ok(self, sync_db):
        """scheduled_run_time 缺失 → duration None, 主流程不受影响"""
        from intelligence.scheduler import record_job_health_event

        record_job_health_event("job_d", success=True, scheduled_run_time=None)

        from sqlalchemy.orm import Session
        with Session(sync_db) as s:
            row = s.get(JobHealth, "job_d")
            assert row.last_duration_ms is None
            assert row.last_run_at is not None
            assert row.total_runs == 1
