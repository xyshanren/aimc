"""V4.6.1 多账号 + cooldown 端到端集成测试

V4.6.1 单测 (test_v46_cooldown.py) 已覆盖 CooldownManager + 路由层.
本测试覆盖**端到端流程**:
- HTTP request 触发路由决策
- 上游响应 (mock httpx) 影响 cooldown 状态
- 后续请求自动切到下一个可用账号
- cooldown 过期后账号恢复

设计:
- 用 httpx.MockTransport mock 上游 (按 Authorization header 区分账号)
- 走真实的 forward_request → route_engine 链路
- DB 用内存 SQLite + AIMC_MASTER_KEY 加密 ciphertext
- cooldown 缩短到 2s 让测试跑得快

跑法: python tests/test_v46_e2e.py
"""

import asyncio
import json
import os
import sys
import tempfile
import time as time_module
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config + 临时 SQLite
# ============================================================
def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v46_e2e_")
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
        log_level="warning",  # 静默 logging
        request_timeout=30,
    )


_install_mock_env()


def _setup_mock_upstream():
    """设置 httpx MockTransport: sk-key-1 → 500, 其他 → 200."""
    import httpx

    def mock_handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        # 注意: proxy.py 内部会构造上游请求, mock 整个上游行为
        if "sk-key-1" in auth:
            return httpx.Response(
                500,
                json={"error": {"message": "mock upstream 500", "type": "internal"}},
            )
        elif "sk-key-2" in auth:
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "model": "deepseek-chat",
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            )
        else:
            return httpx.Response(404, json={"error": "unknown auth"})

    transport = httpx.MockTransport(mock_handler)

    # Monkey-patch httpx.AsyncClient 让其使用 mock transport
    original_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs['transport'] = transport
        return original_async_client(*args, **kwargs)

    httpx.AsyncClient = patched_async_client
    return original_async_client


# ============================================================
# Test 1: 多账号 + cooldown 端到端 (least-used + 失败切下一个 + 冷却恢复)
# ============================================================
def test_e2e_multi_account_with_cooldown():
    print("\n=== Test 1: 多账号 + cooldown 端到端 (4 阶段) ===")
    async def _run():
        from db.init_db import init_db
        from db.engine import async_session_factory
        from db import crud
        from db.models import Channel, Account, PriceSnapshot
        from security.crypto import encrypt
        from sqlalchemy import delete
        from starlette.datastructures import Headers
        from gateway.proxy import forward_request
        from gateway.router import route_engine
        import gateway.cooldown as cooldown_mod
        import httpx

        # 1) Mock httpx 上游
        original_client = _setup_mock_upstream()

        # 2) 缩短 cooldown 让测试快
        cooldown_mod.cooldown_manager.cooldown_seconds = 2
        # 跨测试隔离: 清空全部 state (reset(0) 只能刪特定 id)
        cooldown_mod.cooldown_manager._state.clear()
        route_engine._account_last_used.clear()

        try:
            await init_db()
            async with async_session_factory() as session:
                # 清表
                await session.execute(delete(PriceSnapshot))
                await session.execute(delete(Account))
                await session.execute(delete(Channel))
                await session.commit()

                # 建 channel (deepseek_official)
                channel = await crud.create_channel(
                    session,
                    name="deepseek_official",
                    provider="deepseek",
                    base_url="http://mock-upstream/v1",
                    priority=10,
                )

                # 建 2 个账号 (sk-key-1 坏, sk-key-2 好)
                acc1 = await crud.create_account(
                    session, channel_id=channel.id,
                    account_name="deepseek_01",
                    api_key="", api_key_ciphertext=encrypt("sk-key-1", "a" * 64),
                    status="active", quota_total=None, quota_used=0,
                )
                acc2 = await crud.create_account(
                    session, channel_id=channel.id,
                    account_name="deepseek_02",
                    api_key="", api_key_ciphertext=encrypt("sk-key-2", "a" * 64),
                    status="active", quota_total=None, quota_used=0,
                )

                # 加价格 (不区分时段)
                await crud.create_price_snapshot(
                    session, channel_id=channel.id, model="deepseek-chat",
                    price_type="pay_per_token", time_range="全天",
                    input_price=1.0, output_price=1.0, source="e2e",
                )
                await session.commit()
                print(f"  setup: channel_id={channel.id}, "
                      f"acc1={acc1.id}(sk-key-1), acc2={acc2.id}(sk-key-2)")

            # 3) 刷新路由缓存
            await route_engine.refresh_cache()
            assert "deepseek-chat" in route_engine.get_available_models()

            # 构造请求体
            body = json.dumps({
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }).encode()
            headers = Headers({"content-type": "application/json"})

            # ========== 阶段 1: 第 1 次请求 → 路由到 deepseek_01 → 500 → cooldown ==========
            print("\n  阶段 1: 第 1 次请求 (least-used → _01)")
            ch1 = route_engine.select_channel("deepseek-chat")
            assert ch1 is not None, "应能路由"
            assert ch1["account_name"] == "deepseek_01", (
                f"首次 least-used 应选 _01, 实际 {ch1['account_name']}"
            )
            resp1 = await forward_request(ch1, body, headers, "deepseek-chat")
            assert resp1.status_code == 500, (
                f"期望 mock 返 500, 实际 {resp1.status_code}"
            )
            assert cooldown_mod.cooldown_manager.is_in_cooldown(acc1.id), (
                "_01 应已进入 cooldown"
            )
            assert not cooldown_mod.cooldown_manager.is_in_cooldown(acc2.id), (
                "_02 不应受影响"
            )
            print(f"    [OK] _01 返回 500 → 进入 cooldown (remaining="
                  f"{cooldown_mod.cooldown_manager.cooldown_remaining(acc1.id):.2f}s)")

            # ========== 阶段 2: 第 2 次请求 → _01 在 cooldown → 切到 _02 → 200 ==========
            print("  阶段 2: 第 2 次请求 (_01 冷却中 → 切到 _02)")
            ch2 = route_engine.select_channel("deepseek-chat")
            assert ch2 is not None
            assert ch2["account_name"] == "deepseek_02", (
                f"_01 冷却中应切到 _02, 实际 {ch2['account_name']}"
            )
            resp2 = await forward_request(ch2, body, headers, "deepseek-chat")
            assert resp2.status_code == 200, f"期望 200, 实际 {resp2.status_code}"
            # _02 成功后 mark_success 清掉 (本就没有 cooldown, 但确保仍可用)
            assert not cooldown_mod.cooldown_manager.is_in_cooldown(acc2.id)
            print(f"    [OK] 自动切到 _02 → 200")

            # ========== 阶段 3: 第 3 次请求 → _01 仍冷却 → 继续 _02 ==========
            print("  阶段 3: 第 3 次请求 (_01 仍冷却 → 继续 _02)")
            ch3 = route_engine.select_channel("deepseek-chat")
            assert ch3 is not None
            assert ch3["account_name"] == "deepseek_02", (
                f"_01 未恢复应继续 _02, 实际 {ch3['account_name']}"
            )
            resp3 = await forward_request(ch3, body, headers, "deepseek-chat")
            assert resp3.status_code == 200
            print(f"    [OK] _01 仍冷却 → 继续 _02 (last_used 决定)")

            # ========== 阶段 4: 等 cooldown 过期 → _01 恢复, least-used 让 _01 排前 ==========
            print(f"  阶段 4: 等 {cooldown_mod.cooldown_manager.cooldown_seconds}s 让 cooldown 过期")
            await asyncio.sleep(cooldown_mod.cooldown_manager.cooldown_seconds + 0.5)
            assert not cooldown_mod.cooldown_manager.is_in_cooldown(acc1.id), (
                "_01 cooldown 应已过期"
            )
            ch4 = route_engine.select_channel("deepseek-chat")
            assert ch4 is not None
            # _01 last_used 是阶段 1 (远早), _02 last_used 是阶段 3 (最近)
            # least-used 应选 _01 (时间戳最老)
            assert ch4["account_name"] == "deepseek_01", (
                f"cooldown 过期 + least-used 应回 _01, 实际 {ch4['account_name']}"
            )
            print(f"    [OK] cooldown 过期 → _01 重新可用 (least-used 让 _01 排前)")

        finally:
            import httpx as _httpx
            _httpx.AsyncClient = original_client

    asyncio.run(_run())
    print(f"  [OK] 端到端: 4 阶段全通 — 多账号 + cooldown + least-used 协同")


# ============================================================
# Test 2: cooldown 期内 _02 也失败 → 两个都不可用 → 503
# ============================================================
def test_e2e_all_accounts_in_cooldown_returns_503():
    print("\n=== Test 2: 所有账号 cooldown → 路由返回 503 ===")
    async def _run():
        from db.init_db import init_db
        from db.engine import async_session_factory
        from db import crud
        from db.models import Channel, Account, PriceSnapshot
        from security.crypto import encrypt
        from sqlalchemy import delete
        import gateway.cooldown as cooldown_mod
        from gateway.router import route_engine
        import httpx
        from starlette.datastructures import Headers
        from gateway.proxy import forward_request

        # 重设 mock: 两个账号都返 500
        def mock_handler_500(request):
            return httpx.Response(500, json={"error": "mock 500"})

        transport = httpx.MockTransport(mock_handler_500)
        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs['transport'] = transport
            return original_client(*args, **kwargs)

        httpx.AsyncClient = patched

        # 跨测试隔离: 清空 cooldown state + 路由 last_used state
        cooldown_mod.cooldown_manager.cooldown_seconds = 60
        cooldown_mod.cooldown_manager._state.clear()
        route_engine._account_last_used.clear()

        try:
            await init_db()
            async with async_session_factory() as session:
                await session.execute(delete(PriceSnapshot))
                await session.execute(delete(Account))
                await session.execute(delete(Channel))
                await session.commit()

                channel = await crud.create_channel(
                    session, name="deepseek_official", provider="deepseek",
                    base_url="http://mock-upstream/v1", priority=10,
                )
                for i in range(2):
                    await crud.create_account(
                        session, channel_id=channel.id,
                        account_name=f"deepseek_{i+1:02d}",
                        api_key="",
                        api_key_ciphertext=encrypt(f"sk-key-{i+1}", "a" * 64),
                        status="active", quota_total=None, quota_used=0,
                    )
                await crud.create_price_snapshot(
                    session, channel_id=channel.id, model="deepseek-chat",
                    price_type="pay_per_token", time_range="全天",
                    input_price=1.0, output_price=1.0, source="e2e",
                )
                await session.commit()

            await route_engine.refresh_cache()

            body = json.dumps({"model": "deepseek-chat",
                              "messages": [{"role": "user", "content": "hi"}],
                              "stream": False}).encode()
            headers = Headers({"content-type": "application/json"})

            # 不依赖具体 account_name: 用 account_id 做断言
            ch1 = route_engine.select_channel("deepseek-chat")
            assert ch1 is not None, "应有可用账号"
            resp_a = await forward_request(ch1, body, headers, "deepseek-chat")
            assert resp_a.status_code == 500
            assert cooldown_mod.cooldown_manager.is_in_cooldown(ch1["account_id"]), (
                f"ch1 (account_id={ch1['account_id']}) 应已进入 cooldown"
            )
            print(f"  [step 1] ch1={ch1['account_name']} → 500 → cooldown ✓")

            # 第二次路由: ch1 冷却中 → 应切到另一个账号
            ch2 = route_engine.select_channel("deepseek-chat")
            assert ch2 is not None
            assert ch2["account_id"] != ch1["account_id"], (
                f"ch1 冷却中应切到另一个账号, 但仍选了 {ch2['account_name']}"
            )
            resp_b = await forward_request(ch2, body, headers, "deepseek-chat")
            assert resp_b.status_code == 500
            assert cooldown_mod.cooldown_manager.is_in_cooldown(ch2["account_id"])
            print(f"  [step 2] ch2={ch2['account_name']} → 500 → cooldown ✓")

            # 两个都在 cooldown, 下次 select_channel 应返 None
            ch3 = route_engine.select_channel("deepseek-chat")
            assert ch3 is None, (
                f"两个都冷却时路由应 None, 实际 {ch3['account_name'] if ch3 else None}"
            )
            print(f"  [OK] 两个账号都 cooldown → select_channel 返 None → 上层返 503")

        finally:
            httpx.AsyncClient = original_client

    asyncio.run(_run())


# ============================================================
# Test 3: 4xx 客户端错误不应触发 cooldown (避免误伤)
# ============================================================
def test_e2e_4xx_does_not_cooldown():
    print("\n=== Test 3: 4xx 客户端错误不触发 cooldown ===")
    async def _run():
        from db.init_db import init_db
        from db.engine import async_session_factory
        from db import crud
        from db.models import Channel, Account, PriceSnapshot
        from security.crypto import encrypt
        from sqlalchemy import delete
        import gateway.cooldown as cooldown_mod
        from gateway.router import route_engine
        from gateway.proxy import forward_request
        from starlette.datastructures import Headers
        import httpx

        # Mock: 返 400 (客户端错误)
        def mock_handler_400(request):
            return httpx.Response(400, json={"error": "bad request"})

        transport = httpx.MockTransport(mock_handler_400)
        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs['transport'] = transport
            return original_client(*args, **kwargs)

        httpx.AsyncClient = patched
        cooldown_mod.cooldown_manager.cooldown_seconds = 60
        cooldown_mod.cooldown_manager._state.clear()
        route_engine._account_last_used.clear()

        try:
            await init_db()
            async with async_session_factory() as session:
                await session.execute(delete(PriceSnapshot))
                await session.execute(delete(Account))
                await session.execute(delete(Channel))
                await session.commit()

                channel = await crud.create_channel(
                    session, name="deepseek_official", provider="deepseek",
                    base_url="http://mock-upstream/v1", priority=10,
                )
                for i in range(2):
                    await crud.create_account(
                        session, channel_id=channel.id,
                        account_name=f"deepseek_{i+1:02d}",
                        api_key="",
                        api_key_ciphertext=encrypt(f"sk-key-{i+1}", "a" * 64),
                        status="active", quota_total=None, quota_used=0,
                    )
                await crud.create_price_snapshot(
                    session, channel_id=channel.id, model="deepseek-chat",
                    price_type="pay_per_token", time_range="全天",
                    input_price=1.0, output_price=1.0, source="e2e",
                )
                await session.commit()

            await route_engine.refresh_cache()

            body = json.dumps({"model": "deepseek-chat",
                              "messages": [{"role": "user", "content": "hi"}],
                              "stream": False}).encode()
            headers = Headers({"content-type": "application/json"})

            # 第 1 次: 返 400 → 不应 cooldown
            ch1 = route_engine.select_channel("deepseek-chat")
            resp = await forward_request(ch1, body, headers, "deepseek-chat")
            assert resp.status_code == 400, f"mock 期望 400, 实际 {resp.status_code}"
            assert not cooldown_mod.cooldown_manager.is_in_cooldown(ch1["account_id"]), (
                f"4xx 不应触发 cooldown, 但 {ch1['account_name']} 已 cooldown"
            )
            print(f"  [OK] 400 不触发 cooldown (account_id={ch1['account_id']} 仍可用)")

        finally:
            httpx.AsyncClient = original_client

    asyncio.run(_run())


def main():
    tests = [
        test_e2e_multi_account_with_cooldown,
        test_e2e_all_accounts_in_cooldown_returns_503,
        test_e2e_4xx_does_not_cooldown,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"\n[FAIL] {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n{'=' * 60}")
    print(f"V4.6.1 多账号 + cooldown e2e 测试: {passed} passed, {failed} failed")
    print(f"{'=' * 60}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()