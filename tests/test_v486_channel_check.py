"""V4.8.6 — 渠道可用性校验测试

验证:
1. has_available_channel 返回 True (有 active channel 配过价格)
2. has_available_channel 返回 False (无渠道或 channel inactive)
3. 无可用渠道的模型入库时 status=inactive + new_value 含 channel_available: false
4. 有可用渠道的模型入库时 status=active, 不标记
5. approve create log 时, 有渠道则翻 active, 无渠道保持 inactive
6. scheduler 复扫: inactive 模型配好渠道后自动翻 active

跑法: python tests/test_v486_channel_check.py
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


async def _create_channel_and_price(provider, model_id, status="active"):
    """创建一个 channel + price_snapshot, 返回 channel_id"""
    from db.engine import async_session_factory
    from db import crud

    async with async_session_factory() as session:
        ch = await crud.create_channel(
            session,
            name=f"test-{provider}",
            provider=provider,
            base_url="https://mock.local/v1",
            status=status,
        )
        await session.flush()
        ps = await crud.create_price_snapshot(
            session,
            channel_id=ch.id,
            model=model_id,
            price_type="normal",
            input_price=0.001,
            output_price=0.002,
        )
        await session.commit()
        return ch.id


async def _test_has_channel_true():
    """Test 1: 有 active channel 配过价格 → True"""
    print("\n=== Test 1: has_available_channel 返回 True ===")
    await _setup_db()
    from db.engine import async_session_factory
    from db import crud

    await _create_channel_and_price("siliconflow", "Qwen/Qwen2.5-72B", status="active")

    async with async_session_factory() as session:
        result = await crud.has_available_channel(session, "Qwen/Qwen2.5-72B")
        assert result is True, "有 active channel 应返回 True"
    print("  [OK] 有 active channel → True")


async def _test_has_channel_false_no_price():
    """Test 2: 无任何价格记录 → False"""
    print("\n=== Test 2: 无价格记录 → False ===")
    await _setup_db()
    from db.engine import async_session_factory
    from db import crud

    async with async_session_factory() as session:
        result = await crud.has_available_channel(session, "nonexistent-model")
        assert result is False, "无价格记录应返回 False"
    print("  [OK] 无价格记录 → False")


async def _test_has_channel_false_inactive():
    """Test 3: channel 存在但 inactive → False"""
    print("\n=== Test 3: channel inactive → False ===")
    await _setup_db()
    from db.engine import async_session_factory
    from db import crud

    await _create_channel_and_price("deepseek", "deepseek-chat", status="disabled")

    async with async_session_factory() as session:
        result = await crud.has_available_channel(session, "deepseek-chat")
        assert result is False, "channel disabled 应返回 False"
    print("  [OK] channel inactive → False")


async def _test_model_sync_marks_no_channel():
    """Test 4: 新模型入库时无可用渠道 → status=inactive + new_value 含 channel_available: false"""
    print("\n=== Test 4: 入库 status=inactive + channel_available: false ===")
    await _setup_db()

    from intelligence.model_sync import process_promotion
    import json as _json

    # kimi-k3: 只配了 siliconflow channel, 但 price_snapshots 里没有 kimi-k3 的价格
    raw = _json.dumps({
        "_intelligence_kind": "model_metadata",
        "provider": "moonshot",
        "models": [
            {
                "model_id": "kimi-k3-nochan", "family": "moonshot",
                "inferred_tier": "T1", "capabilities": ["code"],
                "context_length": 128000, "confidence": 0.95,
            },
        ],
    })
    results = await process_promotion(77, raw)
    assert len(results) == 1
    assert results[0].action == "auto_joined"

    # 查 registry, 验证 status=inactive
    from db.engine import async_session_factory
    from sqlalchemy import select
    from db.models import ModelCapabilitiesLog, ModelRegistry

    async with async_session_factory() as session:
        reg = (await session.execute(
            select(ModelRegistry).where(ModelRegistry.model_id == "kimi-k3-nochan")
        )).scalar_one_or_none()
        assert reg is not None, "registry 应有该模型"
        assert reg.status == "inactive", (
            f"无可用渠道时 status 应为 inactive, 实际 '{reg.status}'"
        )

        # 查 create log, 验证 new_value 含 channel_available: false
        log = (await session.execute(
            select(ModelCapabilitiesLog).where(
                ModelCapabilitiesLog.model_id == "kimi-k3-nochan",
                ModelCapabilitiesLog.field_changed == "create",
            )
        )).scalar_one()
        new_val = _json.loads(log.new_value)
        assert new_val.get("channel_available") is False, (
            f"无可用渠道时 new_value 应含 channel_available=False, "
            f"实际 new_value={log.new_value[:200]}"
        )
        assert log.provider == "moonshot"
    print("  [OK] kimi-k3-nochan 入库 status=inactive, channel_available=false, provider=moonshot")


async def _test_model_sync_channel_available_true():
    """Test 5: 有可用渠道的模型入库 → status=active, new_value 不含 channel_available: false"""
    print("\n=== Test 5: 有可用渠道 → status=active, 不标记 ===")
    await _setup_db()

    # 先创建一个 active channel + price for the model
    await _create_channel_and_price("siliconflow", "Qwen/Qwen3-235B-avail", status="active")

    from intelligence.model_sync import process_promotion
    import json as _json

    raw = _json.dumps({
        "_intelligence_kind": "model_metadata",
        "provider": "siliconflow",
        "models": [
            {
                "model_id": "Qwen/Qwen3-235B-avail", "family": "qwen",
                "inferred_tier": "T0", "capabilities": ["reasoning"],
                "context_length": 128000, "confidence": 0.95,
            },
        ],
    })
    results = await process_promotion(78, raw)
    assert len(results) == 1

    from db.engine import async_session_factory
    from sqlalchemy import select
    from db.models import ModelCapabilitiesLog, ModelRegistry

    async with async_session_factory() as session:
        reg = (await session.execute(
            select(ModelRegistry).where(ModelRegistry.model_id == "Qwen/Qwen3-235B-avail")
        )).scalar_one_or_none()
        assert reg is not None
        assert reg.status == "active", (
            f"有可用渠道时 status 应为 active, 实际 '{reg.status}'"
        )

        log = (await session.execute(
            select(ModelCapabilitiesLog).where(
                ModelCapabilitiesLog.model_id == "Qwen/Qwen3-235B-avail",
                ModelCapabilitiesLog.field_changed == "create",
            )
        )).scalar_one()
        new_val = _json.loads(log.new_value)
        assert "channel_available" not in new_val or new_val["channel_available"] is not False, (
            f"有可用渠道时不应标记 channel_available=false, "
            f"实际 new_value={log.new_value[:200]}"
        )
    print("  [OK] Qwen/Qwen3-235B-avail 有可用渠道, status=active, 不标记")


async def _test_approve_create_with_channel():
    """Test 6: approve create log 时有渠道 → status 翻 active"""
    print("\n=== Test 6: approve create 有渠道 → 翻 active ===")
    await _setup_db()

    from intelligence.model_sync import process_promotion
    from db.engine import async_session_factory
    from db import crud
    from sqlalchemy import select
    from db.models import ModelCapabilitiesLog, ModelRegistry
    import json as _json

    # 无渠道入库
    raw = _json.dumps({
        "_intelligence_kind": "model_metadata",
        "provider": "moonshot",
        "models": [{
            "model_id": "kimi-k4-approve", "family": "moonshot",
            "inferred_tier": "T1", "confidence": 0.95,
        }],
    })
    await process_promotion(79, raw)

    async with async_session_factory() as session:
        reg = (await session.execute(
            select(ModelRegistry).where(ModelRegistry.model_id == "kimi-k4-approve")
        )).scalar_one_or_none()
        assert reg is not None and reg.status == "inactive", "入库时应为 inactive"

    # 配渠道
    await _create_channel_and_price("moonshot", "kimi-k4-approve", status="active")

    # approve create log
    async with async_session_factory() as session:
        log = (await session.execute(
            select(ModelCapabilitiesLog).where(
                ModelCapabilitiesLog.model_id == "kimi-k4-approve",
                ModelCapabilitiesLog.field_changed == "create",
            )
        )).scalar_one()
        await crud.approve_capability_change(session, log.id)
        await session.commit()

    async with async_session_factory() as session:
        reg = (await session.execute(
            select(ModelRegistry).where(ModelRegistry.model_id == "kimi-k4-approve")
        )).scalar_one_or_none()
        assert reg is not None and reg.status == "active", (
            f"approve create 时有渠道应翻 active, 实际 '{reg.status if reg else None}'"
        )
    print("  [OK] approve create 有渠道 → inactive 翻 active")


async def _test_approve_create_no_channel():
    """Test 7: approve create log 时仍无渠道 → 保持 inactive"""
    print("\n=== Test 7: approve create 无渠道 → 保持 inactive ===")
    await _setup_db()

    from intelligence.model_sync import process_promotion
    from db.engine import async_session_factory
    from db import crud
    from sqlalchemy import select
    from db.models import ModelCapabilitiesLog, ModelRegistry
    import json as _json

    raw = _json.dumps({
        "_intelligence_kind": "model_metadata",
        "provider": "moonshot",
        "models": [{
            "model_id": "kimi-k5-nochan", "family": "moonshot",
            "inferred_tier": "T1", "confidence": 0.95,
        }],
    })
    await process_promotion(80, raw)

    async with async_session_factory() as session:
        log = (await session.execute(
            select(ModelCapabilitiesLog).where(
                ModelCapabilitiesLog.model_id == "kimi-k5-nochan",
                ModelCapabilitiesLog.field_changed == "create",
            )
        )).scalar_one()
        await crud.approve_capability_change(session, log.id)
        await session.commit()

    async with async_session_factory() as session:
        reg = (await session.execute(
            select(ModelRegistry).where(ModelRegistry.model_id == "kimi-k5-nochan")
        )).scalar_one_or_none()
        assert reg is not None and reg.status == "inactive", (
            f"approve create 无渠道应保持 inactive, 实际 '{reg.status if reg else None}'"
        )
    print("  [OK] approve create 无渠道 → 保持 inactive")


async def _test_rescan_activates_inactive():
    """Test 8: scheduler 复扫 — inactive 模型配好渠道后自动翻 active"""
    print("\n=== Test 8: 复扫自动翻 active ===")
    await _setup_db()

    from intelligence.model_sync import process_promotion, process_model_item
    from db.engine import async_session_factory
    from schemas.intel import ModelIntelItem
    from db.models import ModelRegistry
    from sqlalchemy import select
    import json as _json

    # 第一次: 无渠道入库, status=inactive
    raw = _json.dumps({
        "_intelligence_kind": "model_metadata",
        "provider": "moonshot",
        "models": [{
            "model_id": "kimi-k6-rescan", "family": "moonshot",
            "inferred_tier": "T1", "confidence": 0.95,
        }],
    })
    await process_promotion(81, raw)

    async with async_session_factory() as session:
        reg = (await session.execute(
            select(ModelRegistry).where(ModelRegistry.model_id == "kimi-k6-rescan")
        )).scalar_one_or_none()
        assert reg is not None and reg.status == "inactive", (
            f"首次入库应为 inactive, 实际 '{reg.status if reg else None}'"
        )

    # 用户配好渠道
    await _create_channel_and_price("moonshot", "kimi-k6-rescan", status="active")

    # 第二次: 同样的模型再扫一次 (走 existing 分支)
    item = ModelIntelItem(
        model_id="kimi-k6-rescan", family="moonshot",
        inferred_tier="T1", confidence=0.95, confidence_level="high",
        source_provider="moonshot",
    )
    async with async_session_factory() as session:
        await process_model_item(session, item)
        await session.commit()

    async with async_session_factory() as session:
        reg = (await session.execute(
            select(ModelRegistry).where(ModelRegistry.model_id == "kimi-k6-rescan")
        )).scalar_one_or_none()
        assert reg is not None and reg.status == "active", (
            f"配好渠道后复扫应自动翻 active, 实际 '{reg.status if reg else None}'"
        )
    print("  [OK] 复扫: inactive → active (渠道恢复)")


async def _run_tests():
    await _test_has_channel_true()
    await _test_has_channel_false_no_price()
    await _test_has_channel_false_inactive()
    await _test_model_sync_marks_no_channel()
    await _test_model_sync_channel_available_true()
    await _test_approve_create_with_channel()
    await _test_approve_create_no_channel()
    await _test_rescan_activates_inactive()


def main():
    asyncio.run(_run_tests())
    print("\n✅ V4.8.6 channel check 全部测试通过")


if __name__ == "__main__":
    main()
