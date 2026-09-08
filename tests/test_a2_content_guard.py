"""A-2 (2026-09-08) 采集健康盲区告警测试

覆盖:
1. content_guard.classify_content: 健康页 / 空内容 / 过短 / 各错误页特征 /
   特征超出 4KB 扫描窗口不误报
2. scheduler._run_source_task 挂钩: 不健康内容 → handle_fetch_error 且不进 differ
   (last_content 不被污染)
3. intel_blindspot_audit_task: 全部在采源超 7 天无变化 → 告警; 任一源新鲜 → 不告警;
   在采源不足 → 跳过
4. ensure_migrations: intel_sources.last_changed_at 列迁移 + 幂等

跑法: python -m pytest tests/test_a2_content_guard.py -v
"""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================
# 1. classify_content 分支
# ============================================================

def test_guard_healthy_page():
    from intelligence.content_guard import classify_content
    html = "<html><head><title>DeepSeek Pricing</title></head>" + "<p>内容</p>" * 200
    ok, reason = classify_content(html)
    assert ok, f"正常长页面应健康, reason={reason}"


def test_guard_empty():
    from intelligence.content_guard import classify_content
    ok, reason = classify_content("")
    assert not ok
    assert "空内容" in reason


def test_guard_too_short():
    from intelligence.content_guard import classify_content
    ok, reason = classify_content("<html><body>hi</body></html>")
    assert not ok
    assert "过短" in reason


def test_guard_next_error():
    from intelligence.content_guard import classify_content
    page = '<html id="__next_error__"><head></head><body>' + "x" * 600 + "</body></html>"
    ok, reason = classify_content(page)
    assert not ok
    assert "__next_error__" in reason


def test_guard_js_required():
    from intelligence.content_guard import classify_content
    # 9/8 生产实证 glm JS 壳原文 (promotions id 1083): Vue CLI 标准文案是 "without",
    # 旧签名误写 "with" 导致漏过第一道防线, 靠 sanity check 标黄兜底
    page = ("<html><body>We're sorry but 智谱丨BigModel 平台 doesn't work properly "
            "without JavaScript enabled. Please enable it to continue." + "x" * 600)
    ok, reason = classify_content(page)
    assert not ok
    assert "doesn't work properly without javascript" in reason


def test_guard_404_title():
    from intelligence.content_guard import classify_content
    page = "<html><head><title>404 Not Found</title></head><body>" + "x" * 600
    ok, reason = classify_content(page)
    assert not ok
    assert "<title>404" in reason


def test_guard_chromium_error():
    from intelligence.content_guard import classify_content
    # 9/8 实证的 deepseek 182B 错误页放大版
    page = '<html><head><meta name="color-scheme" content="light dark">' \
           "<body>This site can’t be reached" + "x" * 600
    ok, reason = classify_content(page)
    assert not ok


def test_guard_signature_beyond_scan_window_ignored():
    """特征出现在 4KB 之后不应误报 (扫描窗口限制)"""
    from intelligence.content_guard import classify_content
    filler = "<p>正常内容</p>" * 800  # > 4KB
    page = "<html><body>" + filler + "__next_error__</body></html>"
    ok, reason = classify_content(page)
    assert ok, f"窗口外特征不应触发, reason={reason}"


def test_guard_noscript_not_flagged():
    """"enable javascript" 是 noscript 常见文案, 刻意不收录 (防误杀健康 SPA 页)"""
    from intelligence.content_guard import classify_content
    page = "<html><body><noscript>Please enable JavaScript</noscript>" + "<p>真实内容</p>" * 300
    ok, reason = classify_content(page)
    assert ok, f"含 noscript 的健康页不应误报, reason={reason}"


# ============================================================
# 2. scheduler._run_source_task 挂钩
# ============================================================

def _make_source():
    return SimpleNamespace(
        provider="evilcorp",
        url="https://evilcorp.example/pricing",
        source_type="web_page",
        api_endpoint=None,
        api_key_from=None,
        poll_interval=900,
    )


@pytest.fixture
def guard_env(monkeypatch):
    """crawl_enabled=True + 捕获 handle_fetch_error / has_changed"""
    from config import settings

    monkeypatch.setattr(settings, "crawl_enabled", True, raising=False)

    calls = {"fetch_error": [], "has_changed": 0}

    async def _fake_fetch_error(source, error):
        calls["fetch_error"].append(error)

    async def _fake_has_changed(source, content):
        calls["has_changed"] += 1
        return True

    import intelligence.fetcher as fetcher_mod
    import intelligence.healer as healer_mod
    import intelligence.differ as differ_mod

    async def _fake_fetch(url):
        return "x" * 3000, None  # 默认健康内容, 由各测试覆盖

    monkeypatch.setattr(fetcher_mod, "fetch_page_with_error", _fake_fetch)
    monkeypatch.setattr(healer_mod, "handle_fetch_error", _fake_fetch_error)
    monkeypatch.setattr(differ_mod, "has_changed", _fake_has_changed)

    return calls, fetcher_mod, monkeypatch


def test_run_source_task_unhealthy_content_no_differ(guard_env, monkeypatch):
    """不健康内容 → handle_fetch_error, has_changed 不被调用 (不污染 last_content)"""
    calls, fetcher_mod, monkeypatch = guard_env

    error_page = '<html id="__next_error__">' + "x" * 600

    async def _fetch_error_page(url):
        return error_page, None

    monkeypatch.setattr(fetcher_mod, "fetch_page_with_error", _fetch_error_page)

    from intelligence.scheduler import _run_source_task

    asyncio.run(_run_source_task(_make_source()))

    assert len(calls["fetch_error"]) == 1, "应走 handle_fetch_error 一次"
    assert "__next_error__" in calls["fetch_error"][0]
    assert calls["has_changed"] == 0, "不健康内容不得进 differ (防污染 last_content)"


def test_run_source_task_healthy_content_flows_to_differ(guard_env, monkeypatch):
    """健康内容正常进 differ"""
    calls, fetcher_mod, monkeypatch = guard_env

    async def _fetch_ok(url):
        return "<html><body>" + "正常内容" * 1000 + "</body></html>", None

    monkeypatch.setattr(fetcher_mod, "fetch_page_with_error", _fetch_ok)

    from intelligence.scheduler import _run_source_task

    asyncio.run(_run_source_task(_make_source()))

    assert calls["has_changed"] == 1, "健康内容应进 differ"
    assert len(calls["fetch_error"]) == 0


# ============================================================
# 3. intel_blindspot_audit_task
# ============================================================

@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    """临时 SQLite + 真实 engine, 供盲区审计任务跑真查询"""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

    db_file = tmp_path / "a2_audit.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False, future=True)
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    import db.engine as dbeng
    monkeypatch.setattr(dbeng, "engine", eng)
    monkeypatch.setattr(dbeng, "async_session_factory", factory)

    async def _init():
        from db.models import Base
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_init())

    return factory


def _seed_sources(factory, rows):
    """rows: [(provider, source_type, last_check_at|None, last_changed_at|None)]"""

    async def _run():
        from db.models import IntelSource
        async with factory() as s:
            for provider, stype, check, changed in rows:
                s.add(IntelSource(
                    provider=provider, url=f"https://{provider}.example/x",
                    source_type=stype, status="active",
                    last_check_at=check, last_changed_at=changed,
                    consecutive_no_change=100,
                ))
            await s.commit()

    _run_async(_run())


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_blindspot_alert_fires_when_all_stale(audit_db, monkeypatch):
    """全部在采源 >7 天无变化 → 告警"""
    now = datetime.now()
    _seed_sources(audit_db, [
        ("aaa", "web_page", now, now - timedelta(days=10)),
        ("bbb", "announcement", now, now - timedelta(days=12)),
        ("ccc", "price_aggregator", now, now - timedelta(days=9)),
    ])

    alerts = []

    async def _fake_alert(msg, key=None, silence_seconds=None):
        alerts.append(msg)

    import intelligence.alerter as alerter_mod
    monkeypatch.setattr(alerter_mod, "send_alert", _fake_alert)

    from intelligence.scheduler import intel_blindspot_audit_task
    _run_async(intel_blindspot_audit_task())

    assert len(alerts) == 1, f"应发 1 条失明告警, 实际 {len(alerts)}"
    assert "失明" in alerts[0]
    assert "aaa" in alerts[0] and "ccc" in alerts[0]


def test_blindspot_no_alert_when_one_fresh(audit_db, monkeypatch):
    """任一在采源 7 天内有变化 → 不告警"""
    now = datetime.now()
    _seed_sources(audit_db, [
        ("aaa", "web_page", now, now - timedelta(days=10)),
        ("bbb", "announcement", now, now - timedelta(days=12)),
        ("ccc", "price_aggregator", now, now - timedelta(days=1)),  # 新鲜
    ])

    alerts = []

    async def _fake_alert(msg, key=None, silence_seconds=None):
        alerts.append(msg)

    import intelligence.alerter as alerter_mod
    monkeypatch.setattr(alerter_mod, "send_alert", _fake_alert)

    from intelligence.scheduler import intel_blindspot_audit_task
    _run_async(intel_blindspot_audit_task())

    assert len(alerts) == 0, "有新鲜源时不应告警"


def test_blindspot_skips_when_too_few_sources(audit_db, monkeypatch):
    """在采源不足 (从未检查/长期未检查/非 HTML 类型不参与) → 跳过判定"""
    now = datetime.now()
    _seed_sources(audit_db, [
        ("never", "web_page", None, None),                      # 从未检查
        ("stale_check", "web_page", now - timedelta(days=30), now - timedelta(days=40)),  # 停摆
        ("model_list", "model_list", now, now - timedelta(days=30)),  # 不参与类型
    ])

    alerts = []

    async def _fake_alert(msg, key=None, silence_seconds=None):
        alerts.append(msg)

    import intelligence.alerter as alerter_mod
    monkeypatch.setattr(alerter_mod, "send_alert", _fake_alert)

    from intelligence.scheduler import intel_blindspot_audit_task
    _run_async(intel_blindspot_audit_task())

    assert len(alerts) == 0, "参与源不足 3 个时不应告警"


def test_blindspot_excludes_model_list_sources(audit_db, monkeypatch):
    """model_list (缺 key 空转) 不参与判定 — 3 个 HTML 源 + 1 个 model_list 空转源仍告警"""
    now = datetime.now()
    _seed_sources(audit_db, [
        ("aaa", "web_page", now, now - timedelta(days=10)),
        ("bbb", "announcement", now, now - timedelta(days=12)),
        ("ccc", "price_aggregator", now, now - timedelta(days=9)),
        ("dsm", "model_list", None, None),  # 缺 key 空转, 不参与
    ])

    alerts = []

    async def _fake_alert(msg, key=None, silence_seconds=None):
        alerts.append(msg)

    import intelligence.alerter as alerter_mod
    monkeypatch.setattr(alerter_mod, "send_alert", _fake_alert)

    from intelligence.scheduler import intel_blindspot_audit_task
    _run_async(intel_blindspot_audit_task())

    assert len(alerts) == 1, "model_list 空转源不应阻挡失明判定"


# ============================================================
# 4. last_changed_at 迁移
# ============================================================

def test_migration_adds_last_changed_at(tmp_path, monkeypatch):
    """老库 (无 last_changed_at 列) 跑 ensure_migrations 补列, 且幂等"""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text as _text

    db_file = tmp_path / "a2_migration.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False, future=True)

    import db.engine as dbeng
    monkeypatch.setattr(dbeng, "engine", eng)
    monkeypatch.setattr(dbeng, "async_session_factory", None)  # ensure_migrations 不用 factory

    async def _run():
        from db.models import Base
        from db.init_db import ensure_migrations

        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # 模拟老库: 删掉新列 (SQLite 3.35+ 支持 DROP COLUMN)
            await conn.exec_driver_sql(
                "ALTER TABLE intel_sources DROP COLUMN last_changed_at"
            )

        await ensure_migrations()

        async with eng.connect() as conn:
            result = await conn.execute(_text(
                "SELECT name FROM pragma_table_info('intel_sources')"
            ))
            cols = {row[0] for row in result.fetchall()}

        # 幂等: 再跑一次不报错
        await ensure_migrations()
        return cols

    cols = _run_async(_run())
    assert "last_changed_at" in cols, "迁移应补回 last_changed_at 列"
