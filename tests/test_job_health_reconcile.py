"""2026-11-04 晚 — job_health 启动对账回归

死行: job 改名/停用后旧 id 的 job_health 行永久冻结 → "调度任务健康"卡永久红
(batch_crawl_glm: 成功 1803/失败 13, 红点 = last_error 10-05 > last_success 10-01,
耗时 1790805119.8s = 旧 recorder 把 epoch 毫秒当耗时)。"数据新鲜度"卡同表同污染。

覆盖:
1. 死行删除 (job_id 不在当前 jobstore)
2. 活行垃圾耗时 (>1h) 清 NULL
3. 活行正常耗时不动
4. 空表/全活 不报错

跑法: python -m pytest tests/test_job_health_reconcile.py -q
"""

import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _install_mock_env():
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


@pytest.fixture()
def memory_db(monkeypatch):
    """独立 in-memory sqlite, 把 db.engine.sync_engine 指过去 (reconcile 用 sync Session)"""
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from db.models import Base

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        "db.engine.sync_engine", engine, raising=False,
    )
    return engine, sessionmaker(engine, expire_on_commit=False)


def _seed_health(Session, job_id, *, runs=0, ok=0, fail=0, duration_ms=None):
    from db.models import JobHealth

    with Session() as s:
        s.add(JobHealth(
            job_id=job_id, total_runs=runs, total_successes=ok,
            total_failures=fail, last_duration_ms=duration_ms,
        ))
        s.commit()


def test_dead_rows_removed_and_garbage_duration_cleaned(memory_db):
    from datetime import datetime

    from db.job_health_ops import reconcile_job_health

    engine, Session = memory_db
    _seed_health(Session, "batch_crawl_glm", runs=1816, ok=1803, fail=13,
                 duration_ms=1_790_805_119_802)          # 死行 + epoch 垃圾
    _seed_health(Session, "batch_crawl_glm_86400", runs=24, ok=24, fail=0,
                 duration_ms=1_793_705_470_415)           # 活行 + 垃圾 (待清)
    _seed_health(Session, "batch_crawl_glm_1800", runs=2141, ok=2141, fail=0,
                 duration_ms=6)                            # 活行 + 正常
    _seed_health(Session, "balance_sync", runs=1, ok=1, fail=0,
                 duration_ms=None)                         # 活行 + 无耗时

    removed, cleaned = reconcile_job_health({
        "batch_crawl_glm_86400", "batch_crawl_glm_1800", "balance_sync",
    })
    assert removed == 1, "batch_crawl_glm (旧 id) 应删除"
    assert cleaned == 1, "活行的 >1h 垃圾耗时应清 NULL"

    from db.models import JobHealth

    with Session() as s:
        ids = {r.job_id for r in s.query(JobHealth).all()}
        assert ids == {"batch_crawl_glm_86400", "batch_crawl_glm_1800", "balance_sync"}
        row = s.query(JobHealth).filter_by(job_id="batch_crawl_glm_86400").one()
        assert row.last_duration_ms is None
        keep = s.query(JobHealth).filter_by(job_id="batch_crawl_glm_1800").one()
        assert keep.last_duration_ms == 6, "正常耗时不应被清"
        assert keep.total_successes == 2141, "活行统计保持"


def test_all_live_jobs_noop(memory_db):
    from db.job_health_ops import reconcile_job_health

    engine, Session = memory_db
    _seed_health(Session, "job_a", runs=3, ok=3, duration_ms=42)

    removed, cleaned = reconcile_job_health({"job_a"})
    assert (removed, cleaned) == (0, 0)


def test_empty_table_noop(memory_db):
    from db.job_health_ops import reconcile_job_health

    engine, Session = memory_db
    assert reconcile_job_health({"job_x"}) == (0, 0)
