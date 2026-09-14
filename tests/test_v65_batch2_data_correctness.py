"""V6.5 批次二: 数据正确性修复回归.

- S8 路由成功率 LIMIT 截断: unmatched 总量改不带 limit 的 count
- S9 cache 命中率: cutoff 本地时区 + channel_id 关联 (原 UTC 窗口偏移 8h /
  name 子查询重名串号)
- S11 sync_engine PRAGMA: busy_timeout/synchronous 与 async 侧一致
- S12 调度器旧 job 残留: 改 poll_interval 后旧 batch_crawl_* 继续跑
- S4 differ peek/commit: 解析失败不再永久吞掉该次变化
"""

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import dashboard.routes as dash_routes
import intelligence.differ as differ_mod
import intelligence.healer as healer_mod
from db.models import Base
from gateway.router import RouteEngine
from intelligence.scheduler import _prune_stale_crawl_jobs


# ============================================================
# S11: sync_engine PRAGMA
# ============================================================

def test_sync_engine_busy_timeout_matches_async():
    from db.engine import sync_engine

    with sync_engine.connect() as conn:
        busy = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
        sync_mode = conn.exec_driver_sql("PRAGMA synchronous").scalar()
    assert int(busy) == 5000, "sync_engine 缺 busy_timeout, job_health 写入易 locked 且被吞"
    assert int(sync_mode) in (1, 2), "synchronous 未设置 (NORMAL=1)"


# ============================================================
# S12: 旧 batch_crawl job 清理
# ============================================================

def test_prune_stale_crawl_jobs():
    removed = []
    fake = SimpleNamespace(
        get_jobs=lambda: [
            SimpleNamespace(id="batch_crawl_sensenova_3600"),
            SimpleNamespace(id="batch_crawl_sensenova_1800"),   # 旧 interval 残留
            SimpleNamespace(id="batch_crawl_deepseek_900"),     # 已下线渠道残留
            SimpleNamespace(id="refresh_route_cache"),          # 非采集 job 不动
        ],
        remove_job=lambda jid: removed.append(jid),
    )
    n = _prune_stale_crawl_jobs(fake, {"batch_crawl_sensenova_3600"})
    assert n == 2
    assert sorted(removed) == [
        "batch_crawl_deepseek_900", "batch_crawl_sensenova_1800",
    ]


# ============================================================
# S9: cache 命中率 本地时区 + channel_id
# ============================================================

@pytest.fixture
def hit_db(tmp_path, monkeypatch):
    """最小 usage_logs 库: ch1 窗口内命中 50% / ch1 窗口外行不计入 / ch2 零命中。"""
    db = tmp_path / "hit.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE usage_logs (
            id INTEGER PRIMARY KEY, channel_id INTEGER,
            input_tokens INTEGER, cached_input_tokens INTEGER,
            cache_read_input_tokens INTEGER, created_at TEXT
        );
        """
    )
    from datetime import datetime, timedelta
    now = datetime.now()
    rows = [
        # ch1 窗口内: 500 命中 / 1000 输入 → 50%
        (1, 1000, 500, 0, (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")),
        # ch1 本地 7d 窗口外 (旧 UTC 实现 datetime('now','-7 days') 会误算入)
        (1, 1000, 1000, 0, (now - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")),
        # ch2 窗口内零命中
        (2, 1000, 0, 0, (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")),
    ]
    conn.executemany("INSERT INTO usage_logs VALUES (NULL,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    from config import settings
    monkeypatch.setattr(settings, "db_path", str(db))
    saved = dict(RouteEngine._cache_hit_cache)
    RouteEngine._cache_hit_cache.clear()
    yield db
    RouteEngine._cache_hit_cache.clear()
    RouteEngine._cache_hit_cache.update(saved)


def test_cache_hit_pct_local_window_and_channel_id(hit_db):
    eng = RouteEngine()
    assert eng._cache_hit_pct(1) == pytest.approx(0.5), (
        "窗口外(本地8天前)行被计入 → UTC/本地窗口未修复, 或 channel 过滤失效"
    )
    assert eng._cache_hit_pct(2) == 0.0
    assert eng._cache_hit_pct(None) == 0.0
    # 缓存按 channel_id 键控
    assert 1 in RouteEngine._cache_hit_cache and 2 in RouteEngine._cache_hit_cache


# ============================================================
# S4: differ peek/commit 两段式
# ============================================================

def _mk_source():
    return SimpleNamespace(last_hash=None, last_content=None,
                           provider="test_src", url="http://t",
                           source_type="web_page")


@pytest.fixture
def differ_isolated(monkeypatch):
    """隔离 DB: _load_hash_from_db 恒空, _update_source_hash/_increment_no_change 只记录。"""
    calls = {"updated": [], "no_change": []}

    async def _fake_load(source):
        return None, None

    async def _fake_update(source, new_hash, new_content=None):
        calls["updated"].append(new_hash)
        source.last_hash = new_hash
        if new_content is not None:
            source.last_content = new_content[:100]

    async def _fake_no_change(source):
        calls["no_change"].append(source.provider)

    monkeypatch.setattr(differ_mod, "_load_hash_from_db", _fake_load)
    monkeypatch.setattr(differ_mod, "_update_source_hash", _fake_update)
    monkeypatch.setattr(differ_mod, "_increment_no_change", _fake_no_change)
    return calls


@pytest.mark.asyncio
async def test_has_changed_peek_does_not_commit(differ_isolated):
    """peek 模式判定有变化但不落 hash — 解析失败后可重试的核心语义。"""
    src = _mk_source()
    changed = await differ_mod.has_changed(src, "content-v1", commit=False)
    assert changed is True
    assert differ_isolated["updated"] == [], "peek 模式不得提交 hash"
    assert src.last_hash is None

    # 解析"成功"后调用方补提交
    await differ_mod.commit_source_hash(src, "content-v1")
    assert len(differ_isolated["updated"]) == 1
    assert src.last_hash is not None


@pytest.mark.asyncio
async def test_has_changed_commit_default_writes(differ_isolated):
    src = _mk_source()
    changed = await differ_mod.has_changed(src, "content-v1")
    assert changed is True
    assert len(differ_isolated["updated"]) == 1, "默认 commit=True 保持旧行为"


@pytest.mark.asyncio
async def test_has_changed_same_hash_after_commit_no_change(differ_isolated):
    """提交后同内容再采集 → 无变化分支 (peek 模式同样照常计数)。"""
    src = _mk_source()
    await differ_mod.has_changed(src, "same", commit=False)
    await differ_mod.commit_source_hash(src, "same")
    changed = await differ_mod.has_changed(src, "same", commit=False)
    assert changed is False
    assert differ_isolated["no_change"] == ["test_src"]


# ============================================================
# S4: handle_parse_result 返回成功性
# ============================================================

class _FakeResult:
    def __init__(self, is_promotion=True, confidence=0.95, models=None):
        self.is_promotion = is_promotion
        self.confidence = confidence
        self.models = models or []
        self.provider = "test_src"
        self.title = "t"
        self.summary = ""
        self.discount_desc = ""
        self.content = ""


@pytest.fixture
def healer_stubs(monkeypatch):
    calls = {"saved": 0, "css": 0, "alert": 0}
    monkeypatch.setattr(
        healer_mod, "_save_promotion",
        _async_counter(calls, "saved"),
    )
    return calls


def _async_counter(calls, key):
    async def _f(*a, **k):
        calls[key] += 1
    return _f


@pytest.mark.asyncio
async def test_handle_parse_result_ai_success_true(healer_stubs, monkeypatch):
    async def _fake_parse(content, provider, url, source_type):
        return _FakeResult()
    monkeypatch.setattr("intelligence.parser.parse_content", _fake_parse)
    ok = await healer_mod.handle_parse_result("c", _mk_source())
    assert ok is True and healer_stubs["saved"] == 1


@pytest.mark.asyncio
async def test_handle_parse_result_css_fallback_true(healer_stubs, monkeypatch):
    async def _fake_parse_fail(*a, **k):
        raise RuntimeError("llm down")
    async def _fake_css(content, source):
        return _FakeResult(confidence=0.6)
    monkeypatch.setattr("intelligence.parser.parse_content", _fake_parse_fail)
    monkeypatch.setattr(healer_mod, "_try_css_fallback", _fake_css)
    ok = await healer_mod.handle_parse_result("c", _mk_source())
    assert ok is True and healer_stubs["saved"] == 1


@pytest.mark.asyncio
async def test_handle_parse_result_all_failed_false(healer_stubs, monkeypatch):
    """全部失败 → False (调用方保留旧 hash 重试); 低置信度告警路径同样 False。"""
    async def _fake_parse_fail(*a, **k):
        raise RuntimeError("llm down")
    async def _fake_css_none(content, source):
        return None
    async def _fake_alert(result, source):
        healer_stubs["alert"] += 1
    monkeypatch.setattr("intelligence.parser.parse_content", _fake_parse_fail)
    monkeypatch.setattr(healer_mod, "_try_css_fallback", _fake_css_none)
    monkeypatch.setattr(healer_mod, "_alert_low_confidence", _fake_alert)
    ok = await healer_mod.handle_parse_result("c", _mk_source())
    assert ok is False

    # AI 判定是优惠但置信度不够 + CSS 无救 → 告警分支, 仍 False
    async def _fake_parse_low(content, provider, url, source_type):
        return _FakeResult(confidence=0.5)
    monkeypatch.setattr("intelligence.parser.parse_content", _fake_parse_low)
    ok2 = await healer_mod.handle_parse_result("c", _mk_source())
    assert ok2 is False and healer_stubs["alert"] == 1


# ============================================================
# S8: 路由成功率 LIMIT 截断
# ============================================================

@pytest.fixture
async def stats_db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        # 27 个精确路由 model (旧实现 limit(20) 只能数到 20) + 5 个组路由
        from datetime import datetime, timedelta
        from db.models import UsageLog
        cutoff = datetime.now() - timedelta(days=29)
        for i in range(27):
            session.add(UsageLog(
                model=f"model-{i:02d}", group_id=None, input_tokens=10,
                output_tokens=0, status_code=200, created_at=cutoff,
            ))
        for i in range(5):
            session.add(UsageLog(
                model="m", group_id="tier:strong", input_tokens=10,
                output_tokens=0, status_code=200, created_at=cutoff,
            ))
        await session.commit()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_group_routing_unmatched_not_truncated(stats_db, monkeypatch):
    """S8: 精确路由 27 个 distinct model 时 total/exact 不再被 limit(20) 截断。"""
    resp = await dash_routes.get_group_routing_stats(days=30, session=stats_db)
    s = resp["summary"]
    assert s["exact_model_requests"] == 27, (
        "unmatched 总量仍被 limit(20) 截断 (S8 未生效)"
    )
    assert s["total_requests"] == 32
    assert len(resp["unmatched"]["top_models"]) == 20, "top_models 展示仍限 20"
    assert s["group_routed_requests"] == 5
