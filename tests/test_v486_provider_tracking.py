"""V4.8.6 — 渠道来源追踪测试

验证:
1. log_capability_change 传 provider → 落库
2. list_pending_review 返回结果含 provider
3. process_model_item 透传 source_provider 到 log (通过 process_promotion)

跑法: python tests/test_v486_provider_tracking.py
"""

import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config
# ============================================================
def _install_mock_config():
    import types

    class _MockSettings:
        openai_api_key = "sk-test"
        openai_base_url = "https://mock.local/v1"
        parse_model = "gpt-4o-mini"
        parse_confidence_threshold = 0.6
        crawl_enabled = True
        aimc_master_key = "0" * 64
        route_refresh_interval = 600
        db_url = "sqlite+aiosqlite:///:memory:"
        db_path = "./data/aimc.db"

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = _MockSettings()


_install_mock_config()

# 全局初始化一次 (in-memory DB 跨 asyncio.run 共享同一 engine)
_setup_done = False


async def _setup_db():
    global _setup_done
    if not _setup_done:
        from db.init_db import init_db
        await init_db()
        _setup_done = True


async def _test_provider_persisted():
    """Test 1: log_capability_change 传 provider → 落库"""
    print("\n=== Test 1: provider 落库 ===")
    await _setup_db()
    from db.engine import async_session_factory
    from db import crud
    from sqlalchemy import select
    from db.models import ModelCapabilitiesLog

    async with async_session_factory() as session:
        await crud.log_capability_change(
            session, model_id="prov-model-1", field_changed="tier",
            old_value="T2", new_value="T1",
            confidence=0.85, approved=0,
            provider="moonshot",
        )
        await session.commit()

        row = (await session.execute(
            select(ModelCapabilitiesLog).where(
                ModelCapabilitiesLog.model_id == "prov-model-1",
                ModelCapabilitiesLog.approved == 0,
            )
        )).scalar_one()
        assert row.provider == "moonshot", (
            f"provider 应 'moonshot', 实际 '{row.provider}'"
        )
    print("  [OK] provider='moonshot' 正确落库")


async def _test_provider_null_for_legacy():
    """Test 2: 不传 provider → 落库为 None (兼容老逻辑)"""
    print("\n=== Test 2: 不传 provider → None ===")
    await _setup_db()
    from db.engine import async_session_factory
    from db import crud
    from sqlalchemy import select
    from db.models import ModelCapabilitiesLog

    async with async_session_factory() as session:
        await crud.log_capability_change(
            session, model_id="legacy-model", field_changed="tier",
            old_value=None, new_value="T2",
            confidence=0.7, approved=0,
        )
        await session.commit()
        row = (await session.execute(
            select(ModelCapabilitiesLog).where(
                ModelCapabilitiesLog.model_id == "legacy-model",
            )
        )).scalar_one()
        assert row.provider is None, (
            f"不传 provider 应 None, 实际 '{row.provider}'"
        )
    print("  [OK] 不传 provider → None")


async def _test_list_pending_returns_provider():
    """Test 3: list_pending_review 返回结果含 provider"""
    print("\n=== Test 3: list_pending_review 含 provider ===")
    await _setup_db()
    from db.engine import async_session_factory
    from db import crud

    async with async_session_factory() as session:
        await crud.log_capability_change(
            session, model_id="list-model", field_changed="tier",
            old_value="T2", new_value="T1",
            confidence=0.8, approved=0,
            provider="siliconflow",
        )
        await session.commit()

        rows = await crud.list_pending_review(session, limit=100)
        # 找到 list-model 的行
        target = [(log, reg) for log, reg in rows if log.model_id == "list-model"]
        assert len(target) == 1, (
            f"应找到 1 条 list-model pending, 实际 {len(target)}"
        )
        log, registry = target[0]
        assert log.provider == "siliconflow", (
            f"list_pending_review 应返回 provider='siliconflow', "
            f"实际 '{log.provider}'"
        )
    print("  [OK] list_pending_review 返回 provider='siliconflow'")


async def _test_process_promotion_injects_provider():
    """Test 4: process_promotion 从 raw_data 顶层注入 provider 到 item"""
    print("\n=== Test 4: process_promotion 注入 source_provider ===")
    await _setup_db()
    from intelligence.model_sync import process_promotion

    raw = json.dumps({
        "_intelligence_kind": "model_metadata",
        "provider": "deepseek",
        "models": [
            {
                "model_id": "deepseek-v4-pro", "family": "deepseek",
                "inferred_tier": "T1", "capabilities": ["code"],
                "context_length": 64000, "confidence": 0.95,
            },
        ],
    })
    results = await process_promotion(99, raw)
    assert len(results) == 1
    assert results[0].action == "auto_joined"

    # 查 log 表, 验证 provider 列
    from db.engine import async_session_factory
    from sqlalchemy import select
    from db.models import ModelCapabilitiesLog

    async with async_session_factory() as session:
        logs = (await session.execute(
            select(ModelCapabilitiesLog).where(
                ModelCapabilitiesLog.model_id == "deepseek-v4-pro",
            )
        )).scalars().all()
        assert len(logs) >= 1
        for log in logs:
            assert log.provider == "deepseek", (
                f"log.provider 应 'deepseek', 实际 '{log.provider}'"
            )
    print("  [OK] process_promotion 注入 provider='deepseek' 到所有 log")


async def _test_scheduler_models_meta_has_source_provider():
    """Test 5: scheduler models_meta 构造的 dict 含 source_provider 字段"""
    print("\n=== Test 5: scheduler models_meta 含 source_provider ===")
    import inspect
    from intelligence import scheduler
    source = inspect.getsource(scheduler)
    assert '"source_provider"' in source, (
        "scheduler.py 应在 models_meta 构造中包含 'source_provider' 字段"
    )
    print("  [OK] scheduler.py models_meta 含 source_provider 字段")


async def _run_tests():
    await _test_provider_persisted()
    await _test_provider_null_for_legacy()
    await _test_list_pending_returns_provider()
    await _test_process_promotion_injects_provider()
    await _test_scheduler_models_meta_has_source_provider()


def main():
    asyncio.run(_run_tests())
    print("\n✅ V4.8.6 provider tracking 全部测试通过")


if __name__ == "__main__":
    main()

