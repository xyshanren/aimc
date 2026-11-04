"""2026-11-04 — 采集源代码级停用 (enabled=False) 回归

背景: volcengine 没有渠道/没有 key, 每轮降级抓 HTML 文档页, 产出全是
"只有模型名"的空壳 metadata → pending 队列噪声 (实证: 11-02 那批
Seedream / Seedance / Seed-Evolving / DeepSeek-V4.1-Flash)。

此前 `IntelSourceBase.enabled` 只被 dashboard 展示用, 调度器照样注册 job
和抓取 — 也就是说"禁用"根本不生效。本批把 enabled 变成真闸门, 覆盖:

1. VolcengineArkModelListSource.enabled 为 False, 但仍注册在 get_all_sources()
   (从注册表摘掉会让 DB 行变孤儿: 采集管理页不可见也不可管)
2. 其余源仍默认 enabled=True (防止误伤)
3. scheduler._run_source_task 对 enabled=False 的源第一跳短路, 不落任何抓取
4. dashboard /api/sources 透出 code_disabled 标记 (前端据灰掉按钮)
5. toggle / run-now 对代码级停用源返回 409 — 不允许出现"显示启用但永不采集"

跑法: python -m pytest tests/test_intel_source_code_disabled.py -q
"""

import asyncio
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


# ============================================================
# 1/2: 源定义
# ============================================================
def test_volcengine_source_code_disabled_but_still_registered():
    from intelligence.sources import get_all_sources
    from intelligence.sources.model_lists import VolcengineArkModelListSource

    ark = VolcengineArkModelListSource()
    assert ark.enabled is False, "volcengine 源应代码级停用"

    sources = get_all_sources()
    keys = [(s.provider, s.source_type) for s in sources]
    assert ("volcengine", "model_list") in keys, (
        "停用不等于摘注册 — 摘掉会让 DB 行变孤儿行, 采集管理页不可见不可管"
    )


def test_only_volcengine_is_code_disabled():
    from intelligence.sources import get_all_sources

    disabled = [
        f"{s.provider}/{s.source_type}"
        for s in get_all_sources() if not s.enabled
    ]
    assert disabled == ["volcengine/model_list"], (
        f"当前只应有 volcengine 一个代码级停用源, 实际 {disabled}"
    )


# ============================================================
# 3: 调度器短路 (不抓取)
# ============================================================
def test_run_source_task_short_circuits_when_code_disabled(monkeypatch):
    import intelligence.fetcher as fetcher
    from intelligence.scheduler import _run_source_task
    from intelligence.sources.model_lists import VolcengineArkModelListSource

    calls = []

    async def _boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("代码级停用源不应进入抓取路径")

    monkeypatch.setattr(fetcher, "fetch_page_with_error", _boom)

    asyncio.run(_run_source_task(VolcengineArkModelListSource()))

    assert calls == [], "enabled=False 的源必须在抓取前短路"


# ============================================================
# 4: /api/sources 透出 code_disabled (真 in-memory DB, 不用假 session)
# ============================================================
def _new_memory_session():
    """独立 in-memory SQLite + StaticPool (跨 session 共享同一连接)"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from db.models import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return async_sessionmaker(engine, expire_on_commit=False)

    factory = asyncio.run(_setup())
    return engine, factory


def test_list_sources_exposes_code_disabled_flag():
    from dashboard.routes import list_sources

    engine, factory = _new_memory_session()

    async def _run():
        async with factory() as session:
            return await list_sources(session=session)

    try:
        data = asyncio.run(_run())
    finally:
        asyncio.run(engine.dispose())

    by_key = {(s["provider"], s["source_type"]): s for s in data["sources"]}

    ark = by_key[("volcengine", "model_list")]
    assert ark["code_disabled"] is True
    assert ark["enabled"] is False

    sf = by_key[("siliconflow", "model_list")]
    assert sf["code_disabled"] is False
    assert sf["enabled"] is True


# ============================================================
# 5: toggle / run-now 拒绝唤醒
# ============================================================
def test_toggle_refuses_code_disabled_provider():
    from fastapi import HTTPException

    from dashboard.routes import toggle_source

    with pytest.raises(HTTPException) as exc:
        asyncio.run(toggle_source("volcengine", session=None))
    assert exc.value.status_code == 409


def test_toggle_still_works_for_enabled_provider():
    """反向守卫: 409 闸门不能顺带打断正常 provider 的 toggle 路径

    覆盖 DB 无行 → upsert 行 → 置 disabled 的完整分支 (此前这里漏了
    `toggled` 初始化, 异常只会在真实调用时 NameError, 静态测试抓不到)。
    """
    from dashboard.routes import toggle_source

    engine, factory = _new_memory_session()

    async def _run():
        async with factory() as session:
            return await toggle_source("siliconflow", session=session)

    try:
        res = asyncio.run(_run())
    finally:
        asyncio.run(engine.dispose())

    assert res["provider"] == "siliconflow"
    assert res["toggled"] >= 1
    assert res["new_status"] == "disabled"


def test_run_now_refuses_code_disabled_provider():
    from fastapi import HTTPException

    from dashboard.routes import run_source_now

    with pytest.raises(HTTPException) as exc:
        asyncio.run(run_source_now("volcengine"))
    assert exc.value.status_code == 409


if __name__ == "__main__":
    test_volcengine_source_code_disabled_but_still_registered()
    test_only_volcengine_is_code_disabled()
    test_list_sources_exposes_code_disabled_flag()
    test_toggle_refuses_code_disabled_provider()
    test_run_now_refuses_code_disabled_provider()
    print("all passed (test_run_source_task 短路需 pytest monkeypatch)")
