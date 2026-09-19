"""V6.9.x (2026-09-19): 数据新鲜度接口 SQLite 版本兼容回归

根因: aliy-hy 生产 pyenv Python 3.11 链接系统 SQLite 3.26 (<3.30),
不支持 ORDER BY ... NULLS LAST 语法 → /dashboard/api/data-freshness
每次轮询 500 (near "NULLS": syntax error), 前端 resp.ok 不成立静默
跳过, 运维页 "数据新鲜度" 卡片恒显 "0 / 0 健康".

防护 (两层):
1. SQL 文本防护: 真实执行查询并捕获 SQL, 断言不发 "NULLS LAST"
   (sqlite 3.26 必炸; 本地 3.4x 能跑过所以单功能测试测不出来)
2. 功能回归: 内存 sqlite 跑 get_data_freshness 全流程,
   NULL last_success_at 排最后 + health 分级正确

跑法:
    python tests/test_data_freshness_sqlite_compat.py
"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_passed = 0
_failed = 0


def check(desc, got, expected):
    global _passed, _failed
    if got == expected:
        _passed += 1
        print(f"  PASS  {desc}")
    else:
        _failed += 1
        print(f"  FAIL  {desc}  got={got!r}  expected={expected!r}")


def _capture_sql(engine):
    """挂 before_cursor_execute 钩子, 收集真实下发的 SQL 文本"""
    from sqlalchemy import event

    statements = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listens_for(engine.sync_engine, "before_cursor_execute")(_record)
    return statements


async def _run():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from db.models import Base, JobHealth
    from dashboard import routes as dashboard_routes

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    statements = _capture_sql(engine)
    TestSession = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = datetime.now()
    async with TestSession() as s:
        s.add_all([
            JobHealth(job_id="job_fresh", last_run_at=now, last_success_at=now,
                      total_runs=10, total_successes=10, total_failures=0),
            JobHealth(job_id="job_stale", last_run_at=now - timedelta(days=10),
                      last_success_at=now - timedelta(days=10),
                      total_runs=5, total_successes=5, total_failures=0),
            # 从未成功过的 job (last_success_at=NULL) — 触发原 NULLS LAST 排序路径
            JobHealth(job_id="job_never", last_run_at=now, last_success_at=None,
                      last_error_at=now, last_error_msg="boom",
                      total_runs=3, total_successes=0, total_failures=3),
        ])
        await s.commit()

    async with TestSession() as s:
        result = await dashboard_routes.get_data_freshness(session=s)

    await engine.dispose()
    return result, statements


def test_no_nulls_last_emitted():
    """防护1: 实际执行的 SQL 不得含 NULLS LAST (sqlite 3.26 语法炸弹)"""
    result, statements = asyncio.run(_run())
    offenders = [s for s in statements if "NULLS LAST" in s.upper()]
    check("执行过程无 NULLS LAST SQL", offenders, [])


def test_freshness_endpoint_logic():
    """防护2: 接口返回完整 + NULL last_success 排最后 + health 分级"""
    result, _ = asyncio.run(_run())
    jobs = result["jobs"]
    check("返回 3 个 job", len(jobs), 3)

    order = [j["job_id"] for j in jobs]
    check("NULL last_success 排最后", order[-1], "job_never")
    check("非 NULL 按成功时间倒序", order[:2], ["job_fresh", "job_stale"])

    by_id = {j["job_id"]: j for j in jobs}
    check("fresh → healthy", by_id["job_fresh"]["health"], "healthy")
    check("10天前 → stale", by_id["job_stale"]["health"], "stale")
    check("从未成功 → unknown", by_id["job_never"]["health"], "unknown")
    check("错误信息保留", by_id["job_never"]["last_error_msg"], "boom")
    check("累计次数带出", by_id["job_never"]["total_failures"], 3)


if __name__ == "__main__":
    test_no_nulls_last_emitted()
    test_freshness_endpoint_logic()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
