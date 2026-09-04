"""Phase 2 e2e 测试 (P3-1 重写, 2026-08-19)

原版需本地 uvicorn + 真实上游连通, 默认 skip.
P3-1 改为进程内 MockTransport + 内存 SQLite, 无网络依赖, CI 可直接跑.

覆盖 (跟 phase2-design.md §6.2 验证标准对齐):
1. 精确模型请求 -> 200 + X-AIMC-Actual-Model/Channel (无 Group-Id)
2. 组路由 tier:balanced -> X-AIMC-* 头正确注入 (mock 上游 400)
3. 不存在的组 tier:nonexistent -> 路由 None (503 路径)
4. 偏好过滤 (prefer_family / max_input_price / exclude_providers)

设计 (跟 test_v46_e2e.py 同模式):
- _install_mock_env(): 内存 SQLite + mock settings
- _setup_mock_upstream(): monkey-patch httpx.AsyncClient 注入 MockTransport
- 内联 seed: channel + account + price + ModelGroup + ModelGroupMember + ModelRegistry
- 直接调 forward_request (不经过 ASGI / uvicorn)
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config + 临时 SQLite (跟 test_v46_e2e.py 一致)
# ============================================================
def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_phase2_e2e_")
    db_path = os.path.join(tmp_dir, "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"

    import types
    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = SimpleNamespace(
        openai_api_key="sk-test",
        openai_base_url="https://mock.local/v1",
        parse_model="gpt-4o-mini",
        parse_confidence_threshold=0.6,
        crawl_enabled=True,
        aimc_master_key="a" * 64,
        route_refresh_interval=600,
        db_url=db_url,
        db_path=db_path,
        host="0.0.0.0",
        port=8080,
        log_level="warning",
        request_timeout=30,
        max_fallback_retries=2,
        # V6.x 修复: V4.8.5+ 新增的 settings 字段 (缺了 proxy 转发直接 500)
        prompt_cache_inject_enabled=False,
        transcript_log_enabled=False,
        transcript_log_max_body=4096,
        alert_channel="log",
    )


_install_mock_env()


# tier:balanced member model_id 列表 (跟 seed_model_groups.py 对齐, 用于断言)
BALANCED_MEMBERS = (
    "deepseek-v3",
    "minimax-m2.5",
)
# tier:light member (fallback group)
LIGHT_MEMBERS = (
    "minimax-m2.7-highspeed",
    "qwen3-8b",
    "qwen3.5-4b",
    "minimax-m2.5-highspeed",
    "glm-4-flash",
)


def _setup_mock_upstream(status_code: int = 200, json_body: dict | None = None):
    """Monkey-patch httpx.AsyncClient 注入 MockTransport.

    Args:
        status_code: mock 上游返回的 HTTP status
        json_body: mock 上游返回的 JSON body (默认一个合法 chat.completion)
    Returns:
        original_client (用于 finally 还原)
    """
    import httpx

    if json_body is None:
        json_body = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": "mock-model",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=json_body)

    transport = httpx.MockTransport(mock_handler)
    original_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    httpx.AsyncClient = patched_async_client
    return original_async_client


async def _seed_db():
    """Seed 内存 DB: channel + account + price + ModelGroup + members + registry.

    建两个 ModelGroup:
    - tier:balanced (members: deepseek-v3, minimax-m2.5; fallback -> tier:light)
    - tier:light (members: 5 个轻量模型; fallback -> NULL)

    每个 member 在 model_registry 有一条 (tier=T2/T3, modality=text).
    channel siliconflow_aggregator 对每个 member model 都有 price snapshot.
    """
    from db.init_db import init_db
    from db.engine import async_session_factory
    from db import crud
    from db.models import (
        Channel, Account, PriceSnapshot,
        ModelRegistry, ModelGroup, ModelGroupMember,
        AccountQuota, AccountQuotaUsage, AccountHealthLog,
    )
    from security.crypto import encrypt
    from sqlalchemy import delete

    await init_db()
    async with async_session_factory() as session:
        # 清表 (init_db 可能已建表, 清掉残留)
        # 顺序: 先删 FK 引用 accounts 的表, 再删 accounts
        # (V6.x: AccountHealthLog 是 V4.9 加的 accounts 子表, 漏了会 FK 爆)
        for tbl in (ModelGroupMember, ModelGroup, ModelRegistry,
                    AccountHealthLog,
                    PriceSnapshot, AccountQuotaUsage, AccountQuota,
                    Account, Channel):
            await session.execute(delete(tbl))
        await session.commit()

        # 1) channel: siliconflow_aggregator
        channel = await crud.create_channel(
            session,
            name="siliconflow_aggregator",
            provider="siliconflow",
            base_url="http://mock-upstream/v1",
            priority=10,
            provider_type="aggregator",
            billing_mode="pay_per_token",
        )

        # 2) account (decrypt 用的 master_key = 'a'*64)
        await crud.create_account(
            session, channel_id=channel.id,
            account_name="sf_01",
            api_key="",
            api_key_ciphertext=encrypt("sk-" + "siliconflow-test", "a" * 64),  # 假 key 拼接
            status="active", quota_total=None, quota_used=0,
        )

        # 3) ModelRegistry + price snapshot for 每个 member
        all_members = [
            ("deepseek-v3", "deepseek", "T2"),
            ("minimax-m2.5", "minimax", "T2"),
            ("minimax-m2.7-highspeed", "minimax", "T3"),
            ("qwen3-8b", "qwen", "T3"),
            ("qwen3.5-4b", "qwen", "T3"),
            ("minimax-m2.5-highspeed", "minimax", "T3"),
            ("glm-4-flash", "glm", "T3"),
        ]
        for model_id, family, tier in all_members:
            # ModelRegistry
            session.add(ModelRegistry(
                model_id=model_id,
                family=family,
                tier=tier,
                modality="text",
                context_length=32768,
                status="active",
            ))
            # PriceSnapshot (硅基流动对这个 model 的价格)
            await crud.create_price_snapshot(
                session, channel_id=channel.id, model=model_id,
                price_type="pay_per_token", time_range="全天",
                input_price=1.0, output_price=1.0, source="e2e",
            )

        # 4) ModelGroup: tier:balanced (fallback -> tier:light)
        session.add(ModelGroup(
            group_id="tier:balanced",
            display_name="均衡性价比组",
            tier="T2",
            strategy="cheapest",
            fallback_group="tier:light",
            enabled=1,
        ))
        # tier:balanced members
        for i, model_id in enumerate(BALANCED_MEMBERS):
            session.add(ModelGroupMember(
                group_id="tier:balanced",
                model_id=model_id,
                priority=100 + i * 10,
                weight=1.0,
                is_active=1,
            ))

        # 5) ModelGroup: tier:light (fallback -> NULL)
        session.add(ModelGroup(
            group_id="tier:light",
            display_name="轻量免费组",
            tier="T3",
            strategy="cheapest",
            fallback_group=None,
            enabled=1,
        ))
        for i, model_id in enumerate(LIGHT_MEMBERS):
            session.add(ModelGroupMember(
                group_id="tier:light",
                model_id=model_id,
                priority=100 + i * 10,
                weight=1.0,
                is_active=1,
            ))

        await session.commit()

    # 6) 刷新路由缓存
    from gateway.router import route_engine
    await route_engine.refresh_cache()


# ============================================================
# 测试 1: 精确模型请求
# ============================================================
def test_1_exact_model():
    """精确模型 'Qwen/Qwen2.5-72B-Instruct' -> 200 + 2 头, 无 Group-Id"""
    async def _run():
        from db.init_db import init_db
        from db.engine import async_session_factory
        from db import crud
        from db.models import Channel, Account, PriceSnapshot, AccountQuota, AccountQuotaUsage, AccountHealthLog
        from security.crypto import encrypt
        from sqlalchemy import delete
        from starlette.datastructures import Headers
        from gateway.proxy import forward_request
        from gateway.router import route_engine

        # 精确模型不在 group 里, 单独 seed 一个 price snapshot
        await init_db()
        async with async_session_factory() as session:
            for tbl in (AccountHealthLog, PriceSnapshot, AccountQuotaUsage, AccountQuota, Account, Channel):
                await session.execute(delete(tbl))
            await session.commit()

            channel = await crud.create_channel(
                session,
                name="siliconflow_aggregator",
                provider="siliconflow",
                base_url="http://mock-upstream/v1",
                priority=10,
                provider_type="aggregator",
                billing_mode="pay_per_token",
            )
            await crud.create_account(
                session, channel_id=channel.id,
                account_name="sf_01",
                api_key="",
                api_key_ciphertext=encrypt("sk-" + "siliconflow-test", "a" * 64),  # 假 key 拼接
                status="active", quota_total=None, quota_used=0,
            )
            await crud.create_price_snapshot(
                session, channel_id=channel.id,
                model="Qwen/Qwen2.5-72B-Instruct",
                price_type="pay_per_token", time_range="全天",
                input_price=1.0, output_price=1.0, source="e2e",
            )
            await session.commit()

        await route_engine.refresh_cache()
        assert "Qwen/Qwen2.5-72B-Instruct" in route_engine.get_available_models()

        original_client = _setup_mock_upstream(status_code=200)
        try:
            ch = route_engine.select_channel("Qwen/Qwen2.5-72B-Instruct")
            assert ch is not None, "精确模型应有可用渠道"
            assert ch["name"] == "siliconflow_aggregator"

            body = json.dumps({
                "model": "Qwen/Qwen2.5-72B-Instruct",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5,
            }).encode()
            headers = Headers({"content-type": "application/json"})

            resp = await forward_request(
                ch, body, headers,
                requested_model="Qwen/Qwen2.5-72B-Instruct",
            )

            assert resp.status_code == 200, f"期望 200, 实际 {resp.status_code}"
            assert resp.headers.get("X-AIMC-Actual-Model") == "Qwen/Qwen2.5-72B-Instruct", \
                f"X-AIMC-Actual-Model 应等于请求 model, 实际 {resp.headers.get('X-AIMC-Actual-Model')!r}"
            assert resp.headers.get("X-AIMC-Actual-Channel") == "siliconflow_aggregator"
            assert "X-AIMC-Group-Id" not in resp.headers, \
                "精确路由时不应有 X-AIMC-Group-Id"
        finally:
            import httpx as _httpx
            _httpx.AsyncClient = original_client

    asyncio.run(_run())


# ============================================================
# 测试 2: 组路由
# ============================================================
def test_2_group_route():
    """组路由 'tier:balanced' -> mock 上游 400, 但 X-AIMC-* 头正确注入"""
    async def _run():
        from starlette.datastructures import Headers
        from gateway.proxy import forward_request
        from gateway.router import route_engine
        import gateway.cooldown as cooldown_mod

        await _seed_db()
        # 清 cooldown state (跨测试隔离)
        cooldown_mod.cooldown_manager._state.clear()
        route_engine._account_last_used.clear()

        # mock 上游返 400 (模拟硅基不认 member model 名)
        original_client = _setup_mock_upstream(
            status_code=400,
            json_body={"error": {"message": "model not found", "type": "invalid_request_error"}},
        )
        try:
            ch = route_engine.select_channel("tier:balanced")
            assert ch is not None, "tier:balanced 应有可用渠道"

            body = json.dumps({
                "model": "tier:balanced",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5,
            }).encode()
            headers = Headers({"content-type": "application/json"})

            resp = await forward_request(
                ch, body, headers,
                requested_model="tier:balanced",
            )

            # mock 上游返 400
            assert resp.status_code == 400, f"期望 400, 实际 {resp.status_code}"
            # X-AIMC-* 头应已注入
            actual_model = resp.headers.get("X-AIMC-Actual-Model")
            assert actual_model in BALANCED_MEMBERS, \
                f"组路由应注入 _actual_model ∈ {BALANCED_MEMBERS}, 实际 {actual_model!r}"
            assert resp.headers.get("X-AIMC-Group-Id") == "tier:balanced"
            assert resp.headers.get("X-AIMC-Actual-Channel") == "siliconflow_aggregator"
        finally:
            import httpx as _httpx
            _httpx.AsyncClient = original_client

    asyncio.run(_run())


# ============================================================
# 测试 3: 不存在的组 -> 路由 None (503 路径)
# ============================================================
def test_3_fallback_chain():
    """不存在的组 'tier:nonexistent' -> select_channel 返 None (503 路径)"""
    async def _run():
        from gateway.router import route_engine

        await _seed_db()

        # tier:nonexistent 不在 _groups_cache, fallback chain 也找不到
        ch = route_engine.select_channel("tier:nonexistent")
        assert ch is None, \
            f"不存在的组应返 None (503 路径), 实际 {ch!r}"

    asyncio.run(_run())


# ============================================================
# 测试 4: 偏好过滤 (in-process)
# ============================================================
def test_4_preferences():
    """偏好 4 子场景: 无 prefs / prefer_family / max_input_price / exclude_providers"""
    async def _run():
        from gateway.router import route_engine
        import gateway.cooldown as cooldown_mod

        await _seed_db()
        cooldown_mod.cooldown_manager._state.clear()
        route_engine._account_last_used.clear()

        # 4a) 无 prefs -> 应选 tier:balanced 的某个 member
        ch = route_engine.select_channel("tier:balanced")
        assert ch is not None, "无 prefs 应返 channel"
        assert ch["_registry_model"] in BALANCED_MEMBERS, \
            f"无 prefs 应选 tier:balanced member, 实际 {ch['_registry_model']!r}"

        # 4b) prefer_family='deepseek' -> 应选 deepseek-v3
        ch = route_engine.select_channel(
            "tier:balanced", prefs={"prefer_family": "deepseek"},
        )
        assert ch is not None, "prefer_family=deepseek 应有可用 channel"
        assert ch["_registry_model"] == "deepseek-v3", \
            f"prefer_family=deepseek 应选 deepseek-v3, 实际 {ch['_registry_model']!r}"

        # 4c) max_input_price=0.5 -> tier:balanced member input_price=1.0 全超价
        #     fallback 到 tier:light (input_price 也 1.0) -> 全超价 -> None
        ch = route_engine.select_channel(
            "tier:balanced", prefs={"max_input_price": 0.5},
        )
        assert ch is None, \
            f"max_input_price=0.5 全超价应 None, 实际 {ch['_registry_model'] if ch else None}"

        # 4d) exclude_providers=['siliconflow'] -> 唯一 channel 被排除 -> None
        ch = route_engine.select_channel(
            "tier:balanced", prefs={"exclude_providers": ["siliconflow"]},
        )
        assert ch is None, \
            f"exclude_providers=[siliconflow] 唯一 channel 被排除应 None, 实际 {ch!r}"

    asyncio.run(_run())


# ============================================================
# Main (可直接 python 跑)
# ============================================================
def main():
    tests = [
        test_1_exact_model,
        test_2_group_route,
        test_3_fallback_chain,
        test_4_preferences,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"[PASS] {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"[FAIL] {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n{'=' * 60}")
    print(f"Phase 2 e2e: {passed} passed, {failed} failed")
    print(f"{'=' * 60}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
